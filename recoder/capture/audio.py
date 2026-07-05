"""Dual-channel audio capture for Recoder (spec 4.1, error handling 5).

Records two synchronized WASAPI streams to streaming FLAC files:

  * ``system`` — WASAPI loopback of the default output device (everyone else).
  * ``mic``    — the default input device (the user).

Each channel runs in its own thread, reading fixed-size buffers and writing
them straight into a ``soundfile.SoundFile`` (streaming FLAC), flushing at
least once per second so a hard kill loses < 1s of audio. A shared JSONL
timing index records, per channel, wall-clock timestamps against frame
offsets so the two independent sample clocks can be aligned later, plus
``start``/``stop``/``gap`` events. A ``gap`` is logged whenever a stream read
raises and the stream is re-opened (device change / sleep-resume); re-open
retries with backoff and the healthy channel keeps recording meanwhile.

The device layer is isolated behind the :class:`StreamSource` protocol so the
recorder can be tested with fakes and never requires real audio hardware.

This module has ZERO imports from pipeline/analysis/web — recording is sacred.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

import numpy as np
import soundfile as sf

__all__ = [
    "AudioDeviceError",
    "StreamSource",
    "AudioRecorder",
    "RecordingResult",
    "ChannelResult",
]

# Frames per read/write buffer. ~21ms at 48kHz — cheap, low-latency.
_DEFAULT_CHUNK_FRAMES = 1024
# Flush (and emit a timing/level entry) at least this often.
_DEFAULT_FLUSH_INTERVAL_S = 1.0
# Re-open backoff schedule; the last value repeats until success or stop.
_DEFAULT_REOPEN_DELAYS: tuple[float, ...] = (0.5, 1.0, 2.0, 5.0)


class AudioDeviceError(RuntimeError):
    """A required audio device could not be opened. Recording must stop loudly."""


@runtime_checkable
class StreamSource(Protocol):
    """Minimal device abstraction: describe rate/channels, open/read/close.

    ``rate`` and ``channels`` must be valid after :meth:`open` returns. ``read``
    returns a float32 array shaped ``(frames, channels)`` in ``[-1.0, 1.0]``;
    it may return fewer frames than requested. A read that raises signals a
    device fault: the recorder logs a ``gap`` and re-opens via close()+open().
    """

    @property
    def rate(self) -> int: ...

    @property
    def channels(self) -> int: ...

    def open(self) -> None: ...

    def read(self, frames: int) -> np.ndarray: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class ChannelResult:
    """Outcome for one channel after :meth:`AudioRecorder.stop`."""

    channel: str
    frames_written: int
    sample_rate: int
    channels: int
    gap_count: int

    @property
    def duration_s(self) -> float:
        if self.sample_rate <= 0:
            return 0.0
        return self.frames_written / self.sample_rate


@dataclass(frozen=True)
class RecordingResult:
    """Aggregate outcome returned by :meth:`AudioRecorder.stop`."""

    mic: ChannelResult
    system: ChannelResult
    duration_s: float  # wall-clock start->stop

    @property
    def gap_count(self) -> int:
        return self.mic.gap_count + self.system.gap_count


class AudioRecorder:
    """Records the ``mic`` and ``system`` channels concurrently to FLAC.

    Parameters
    ----------
    mic_path, system_path:
        Destination FLAC files for the two channels.
    timing_index_path:
        Shared JSONL sidecar for wall-clock timing entries and events.
    source_factory:
        Optional callable returning ``(mic_source, system_source)`` as
        :class:`StreamSource` instances (already constructed, not yet opened).
        Defaults to the production pyaudiowpatch factory. Tests inject fakes.
    on_level:
        Optional callback ``(channel, rms)`` invoked ~once per second per
        channel for a live-level UI.
    """

    def __init__(
        self,
        mic_path: Path,
        system_path: Path,
        timing_index_path: Path,
        *,
        source_factory: Callable[[], tuple[StreamSource, StreamSource]] | None = None,
        on_level: Callable[[str, float], None] | None = None,
        chunk_frames: int = _DEFAULT_CHUNK_FRAMES,
        flush_interval_s: float = _DEFAULT_FLUSH_INTERVAL_S,
        reopen_delays: Sequence[float] = _DEFAULT_REOPEN_DELAYS,
    ) -> None:
        self._mic_path = Path(mic_path)
        self._system_path = Path(system_path)
        self._timing_index_path = Path(timing_index_path)
        self._source_factory = source_factory or _default_source_factory
        self._on_level = on_level
        self._chunk_frames = int(chunk_frames)
        self._flush_interval_s = float(flush_interval_s)
        self._reopen_delays = tuple(reopen_delays) or _DEFAULT_REOPEN_DELAYS

        self._stop_event = threading.Event()
        self._index_lock = threading.Lock()
        self._recording = False

        self._index_fh = None
        self._threads: list[threading.Thread] = []
        self._sources: dict[str, StreamSource] = {}
        self._files: dict[str, sf.SoundFile] = {}
        self._results: dict[str, ChannelResult] = {}
        self._start_wall: float = 0.0

    # ------------------------------------------------------------------ API

    @property
    def is_recording(self) -> bool:
        return self._recording

    def start(self) -> None:
        """Open both devices and begin recording. Raises on device failure."""
        if self._recording:
            raise RuntimeError("recorder already started")

        mic_source, system_source = self._source_factory()
        self._sources = {"mic": mic_source, "system": system_source}

        # Open both devices up front so a missing/failing device fails loudly
        # before we claim to be recording (spec 5: audio failures stop loudly).
        opened: list[str] = []
        try:
            for ch in ("system", "mic"):
                try:
                    self._sources[ch].open()
                except Exception as exc:  # noqa: BLE001 - surfaced as a clear error
                    raise AudioDeviceError(
                        f"failed to open {ch} audio device: {exc}"
                    ) from exc
                opened.append(ch)
        except AudioDeviceError:
            for ch in opened:
                _safe_close(self._sources[ch])
            self._sources = {}
            raise

        # Create streaming FLAC writers at each device's native rate/channels.
        try:
            for ch, path in (
                ("mic", self._mic_path),
                ("system", self._system_path),
            ):
                src = self._sources[ch]
                path.parent.mkdir(parents=True, exist_ok=True)
                self._files[ch] = sf.SoundFile(
                    str(path),
                    mode="w",
                    samplerate=int(src.rate),
                    channels=int(src.channels),
                    format="FLAC",
                )
            self._index_fh = self._timing_index_path.open("a", encoding="utf-8")
        except Exception:
            for f in self._files.values():
                _safe_close(f)
            self._files = {}
            for src in self._sources.values():
                _safe_close(src)
            self._sources = {}
            raise

        self._stop_event.clear()
        self._results = {}
        self._start_wall = time.time()
        self._recording = True

        for ch in ("mic", "system"):
            t = threading.Thread(
                target=self._run_channel,
                args=(ch,),
                name=f"audio-{ch}",
                daemon=True,
            )
            self._threads.append(t)
            t.start()

    def stop(self) -> RecordingResult:
        """Signal both threads to stop, join, close everything, return result."""
        if not self._recording:
            raise RuntimeError("recorder not started")

        self._stop_event.set()
        for t in self._threads:
            t.join()
        self._threads = []

        for src in self._sources.values():
            _safe_close(src)
        for f in self._files.values():
            _safe_close(f)
        if self._index_fh is not None:
            _safe_close(self._index_fh)
            self._index_fh = None

        self._recording = False
        duration = time.time() - self._start_wall

        mic = self._results.get("mic") or _empty_result("mic", self._sources)
        system = self._results.get("system") or _empty_result("system", self._sources)
        self._sources = {}
        self._files = {}
        return RecordingResult(mic=mic, system=system, duration_s=duration)

    # -------------------------------------------------------------- internals

    def _run_channel(self, ch: str) -> None:
        source = self._sources[ch]
        sound_file = self._files[ch]
        frames_written = 0
        gap_count = 0

        self._emit(ch, event="start")

        last_flush = time.monotonic()
        acc_sq = 0.0
        acc_n = 0

        while not self._stop_event.is_set():
            try:
                block = source.read(self._chunk_frames)
            except Exception:  # noqa: BLE001 - device fault -> gap + re-open
                gap_count += 1
                self._emit(ch, event="gap")
                if not self._reopen(source):
                    break  # stop requested (or gave up) while re-opening
                last_flush = time.monotonic()
                continue

            if block is None or len(block) == 0:
                continue

            sound_file.write(block)
            frames_written += len(block)

            arr = np.asarray(block, dtype=np.float32)
            acc_sq += float(np.dot(arr.reshape(-1), arr.reshape(-1)))
            acc_n += arr.size

            now = time.monotonic()
            if now - last_flush >= self._flush_interval_s:
                sound_file.flush()
                self._emit(ch, frames_written=frames_written)
                if self._on_level is not None and acc_n > 0:
                    rms = float(np.sqrt(acc_sq / acc_n))
                    try:
                        self._on_level(ch, rms)
                    except Exception:  # noqa: BLE001 - UI callback never breaks audio
                        pass
                acc_sq = 0.0
                acc_n = 0
                last_flush = now

        # Final flush so the last <1s of audio is durable, then record outcome.
        try:
            sound_file.flush()
        except Exception:  # noqa: BLE001
            pass
        self._emit(ch, frames_written=frames_written)
        self._emit(ch, event="stop")

        self._results[ch] = ChannelResult(
            channel=ch,
            frames_written=frames_written,
            sample_rate=int(source.rate),
            channels=int(source.channels),
            gap_count=gap_count,
        )

    def _reopen(self, source: StreamSource) -> bool:
        """Re-open a faulted stream with backoff. False if stop requested."""
        _safe_close(source)
        for delay in self._reopen_schedule():
            if self._stop_event.wait(delay):
                return False
            try:
                source.open()
                return True
            except Exception:  # noqa: BLE001 - keep retrying on the schedule
                continue
        return False

    def _reopen_schedule(self) -> Iterator[float]:
        for d in self._reopen_delays:
            yield d
        while True:  # repeat the final (longest) delay indefinitely
            yield self._reopen_delays[-1]

    def _emit(self, ch: str, wall: float | None = None, **fields: object) -> None:
        entry: dict[str, object] = {"ch": ch}
        entry.update(fields)
        entry["wall"] = time.time() if wall is None else wall
        line = json.dumps(entry)
        with self._index_lock:
            if self._index_fh is None:
                return
            self._index_fh.write(line + "\n")
            self._index_fh.flush()


def _empty_result(ch: str, sources: dict[str, StreamSource]) -> ChannelResult:
    src = sources.get(ch)
    rate = int(src.rate) if src is not None else 0
    channels = int(src.channels) if src is not None else 0
    return ChannelResult(
        channel=ch,
        frames_written=0,
        sample_rate=rate,
        channels=channels,
        gap_count=0,
    )


def _safe_close(obj: object) -> None:
    try:
        close = getattr(obj, "close", None)
        if close is not None:
            close()
    except Exception:  # noqa: BLE001 - shutdown is best-effort
        pass


# --------------------------------------------------------------------------
# Production device layer (pyaudiowpatch). Imported lazily so tests that inject
# fakes never require the native library or real hardware.
# --------------------------------------------------------------------------


class _PyAudioSource:
    """A :class:`StreamSource` backed by a pyaudiowpatch WASAPI input stream.

    Mirrors ``spikes/spike_a_loopback.py``: it queries a device descriptor via
    a resolver callback (default loopback or default input), then opens an
    int16 input stream and yields float32 buffers. Re-open re-queries the
    device so a post-resume default-device change is picked up.
    """

    def __init__(self, resolve: Callable[[object], dict], max_channels: int | None = None):
        self._resolve = resolve
        self._max_channels = max_channels
        self._pa = None
        self._stream = None
        self._rate = 0
        self._channels = 0

    @property
    def rate(self) -> int:
        return self._rate

    @property
    def channels(self) -> int:
        return self._channels

    def open(self) -> None:
        import pyaudiowpatch as pyaudio

        self._pa = pyaudio.PyAudio()
        info = self._resolve(self._pa)
        index = int(info["index"])
        self._rate = int(info["defaultSampleRate"])
        channels = int(info["maxInputChannels"])
        if self._max_channels is not None:
            channels = min(channels, self._max_channels)
        self._channels = channels
        self._stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=self._channels,
            rate=self._rate,
            frames_per_buffer=_DEFAULT_CHUNK_FRAMES,
            input=True,
            input_device_index=index,
        )

    def read(self, frames: int) -> np.ndarray:
        if self._stream is None:
            raise RuntimeError("stream not open")
        data = self._stream.read(frames, exception_on_overflow=False)
        arr = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        return arr.reshape(-1, self._channels)

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop_stream()
            finally:
                self._stream.close()
            self._stream = None
        if self._pa is not None:
            self._pa.terminate()
            self._pa = None


def _default_source_factory() -> tuple[StreamSource, StreamSource]:
    """Production factory: (mic default input, system WASAPI loopback)."""

    def resolve_loopback(pa: object) -> dict:
        return pa.get_default_wasapi_loopback()  # type: ignore[attr-defined]

    def resolve_mic(pa: object) -> dict:
        return pa.get_default_input_device_info()  # type: ignore[attr-defined]

    mic = _PyAudioSource(resolve_mic, max_channels=2)
    system = _PyAudioSource(resolve_loopback)
    return mic, system
