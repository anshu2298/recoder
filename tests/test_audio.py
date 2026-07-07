"""Tests for recoder.capture.audio using injected fake StreamSources.

No test requires real audio hardware: every device is a FakeSource that emits
deterministic sine buffers and can be told to fault to exercise the gap /
re-open path.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from recoder.capture.audio import (
    AudioDeviceError,
    AudioRecorder,
    ChannelResult,
    RecordingResult,
    StreamSource,
)


class FakeSource:
    """A deterministic StreamSource emitting sine buffers in real-ish time.

    ``fail_after`` reads (once) raises to simulate a device fault; the stream
    then recovers on the next open(). ``open_fail`` makes the first open()
    raise, to exercise start-time device failure.
    """

    def __init__(
        self,
        rate: int = 8000,
        channels: int = 1,
        freq: float = 220.0,
        fail_after: int | None = None,
        open_fail: bool = False,
        realtime: bool = True,
    ) -> None:
        self._rate = rate
        self._channels = channels
        self._freq = freq
        self._fail_after = fail_after
        self._open_fail = open_fail
        self._realtime = realtime
        self._phase = 0
        self._reads = 0
        self.open_count = 0
        self.close_count = 0
        self._faulted = False

    @property
    def rate(self) -> int:
        return self._rate

    @property
    def channels(self) -> int:
        return self._channels

    def open(self) -> None:
        if self._open_fail and self.open_count == 0:
            self.open_count += 1
            raise OSError("simulated device open failure")
        self.open_count += 1

    def read(self, frames: int) -> np.ndarray:
        self._reads += 1
        if (
            self._fail_after is not None
            and not self._faulted
            and self._reads > self._fail_after
        ):
            self._faulted = True
            raise OSError("simulated device read fault")
        if self._realtime:
            time.sleep(frames / self._rate)
        t = (np.arange(self._phase, self._phase + frames) / self._rate)
        self._phase += frames
        wave = 0.5 * np.sin(2 * np.pi * self._freq * t).astype(np.float32)
        return np.repeat(wave.reshape(-1, 1), self._channels, axis=1)

    def close(self) -> None:
        self.close_count += 1


def make_recorder(tmp_path: Path, mic: FakeSource, system: FakeSource, **kw) -> AudioRecorder:
    return AudioRecorder(
        tmp_path / "audio-mic.flac",
        tmp_path / "audio-system.flac",
        tmp_path / "timing.jsonl",
        source_factory=lambda: (mic, system),
        chunk_frames=kw.pop("chunk_frames", 256),
        flush_interval_s=kw.pop("flush_interval_s", 0.1),
        reopen_delays=kw.pop("reopen_delays", (0.02,)),
        **kw,
    )


def read_index(tmp_path: Path) -> list[dict]:
    text = (tmp_path / "timing.jsonl").read_text(encoding="utf-8").strip()
    return [json.loads(line) for line in text.splitlines() if line]


# --------------------------------------------------------------------------


def test_records_both_channels_to_flac(tmp_path: Path) -> None:
    mic = FakeSource(rate=8000, channels=1)
    system = FakeSource(rate=16000, channels=2)
    rec = make_recorder(tmp_path, mic, system)

    rec.start()
    assert rec.is_recording
    time.sleep(0.5)
    result = rec.stop()
    assert not rec.is_recording

    mic_data, mic_sr = sf.read(str(tmp_path / "audio-mic.flac"))
    sys_data, sys_sr = sf.read(str(tmp_path / "audio-system.flac"))
    assert mic_sr == 8000
    assert sys_sr == 16000
    # FLAC frame count matches what the recorder reported.
    assert len(mic_data) == result.mic.frames_written
    assert len(sys_data) == result.system.frames_written
    assert result.mic.frames_written > 0
    assert result.system.frames_written > 0
    assert sys_data.ndim == 2 and sys_data.shape[1] == 2


def test_result_fields_correct(tmp_path: Path) -> None:
    mic = FakeSource(rate=8000, channels=1)
    system = FakeSource(rate=8000, channels=1)
    rec = make_recorder(tmp_path, mic, system)

    rec.start()
    time.sleep(0.4)
    result = rec.stop()

    assert isinstance(result, RecordingResult)
    assert isinstance(result.mic, ChannelResult)
    assert result.mic.channel == "mic"
    assert result.system.channel == "system"
    assert result.mic.sample_rate == 8000
    assert result.duration_s > 0
    # duration derived from frames matches the sample rate.
    assert result.mic.duration_s == pytest.approx(
        result.mic.frames_written / 8000, rel=1e-6
    )
    assert result.gap_count == 0


def test_frame_count_roughly_matches_wallclock(tmp_path: Path) -> None:
    mic = FakeSource(rate=8000, channels=1)
    system = FakeSource(rate=8000, channels=1)
    rec = make_recorder(tmp_path, mic, system)

    rec.start()
    time.sleep(0.6)
    result = rec.stop()

    # ~0.6s at 8000 Hz -> a few thousand frames; allow slack for scheduling.
    assert 2000 < result.mic.frames_written < 8000


def test_flush_grows_file_during_recording(tmp_path: Path) -> None:
    mic = FakeSource(rate=8000, channels=1)
    system = FakeSource(rate=8000, channels=1)
    rec = make_recorder(tmp_path, mic, system, flush_interval_s=0.05)

    mic_file = tmp_path / "audio-mic.flac"
    rec.start()
    try:
        time.sleep(0.2)
        size_early = mic_file.stat().st_size
        time.sleep(0.4)
        size_late = mic_file.stat().st_size
    finally:
        rec.stop()

    # Streaming flush means the file grows on disk while recording continues.
    assert size_early > 0
    assert size_late > size_early


def test_timing_index_well_formed_and_monotonic(tmp_path: Path) -> None:
    mic = FakeSource(rate=8000, channels=1)
    system = FakeSource(rate=8000, channels=1)
    rec = make_recorder(tmp_path, mic, system)

    rec.start()
    time.sleep(0.5)
    rec.stop()

    entries = read_index(tmp_path)
    assert entries, "timing index should not be empty"

    for e in entries:
        assert e["ch"] in ("mic", "system")
        assert isinstance(e["wall"], float)
        assert "event" in e or "frames_written" in e

    for ch in ("mic", "system"):
        ch_entries = [e for e in entries if e["ch"] == ch]
        assert ch_entries[0].get("event") == "start"
        assert ch_entries[-1].get("event") == "stop"
        walls = [e["wall"] for e in ch_entries]
        assert walls == sorted(walls), "wall clock must be monotonic per channel"
        frames = [e["frames_written"] for e in ch_entries if "frames_written" in e]
        assert frames == sorted(frames), "frames_written must be monotonic"


def test_gap_event_and_successful_reopen(tmp_path: Path) -> None:
    # Mic faults once after 2 reads, then recovers on re-open.
    mic = FakeSource(rate=8000, channels=1, fail_after=2)
    system = FakeSource(rate=8000, channels=1)
    rec = make_recorder(tmp_path, mic, system)

    rec.start()
    time.sleep(0.6)
    result = rec.stop()

    entries = read_index(tmp_path)
    mic_events = [e.get("event") for e in entries if e["ch"] == "mic"]
    assert "gap" in mic_events
    assert result.mic.gap_count == 1
    # Re-open happened: open() called twice (initial + after fault).
    assert mic.open_count >= 2
    # Recording continued afterwards -> more frames than the pre-fault reads.
    assert result.mic.frames_written > 0
    # Healthy channel unaffected.
    assert result.system.gap_count == 0
    assert result.system.frames_written > 0


def test_healthy_channel_records_during_other_gap(tmp_path: Path) -> None:
    # Mic faults; system must keep producing audio the whole time.
    mic = FakeSource(rate=8000, channels=1, fail_after=1)
    system = FakeSource(rate=8000, channels=1)
    rec = make_recorder(tmp_path, mic, system, reopen_delays=(0.2,))

    rec.start()
    time.sleep(0.6)
    result = rec.stop()

    sys_data, _ = sf.read(str(tmp_path / "audio-system.flac"))
    assert len(sys_data) == result.system.frames_written
    assert result.system.frames_written > 1000


def test_concurrent_channels_no_corruption(tmp_path: Path) -> None:
    # Distinct rates/channels; each FLAC must decode cleanly and independently.
    mic = FakeSource(rate=8000, channels=1, freq=220.0)
    system = FakeSource(rate=16000, channels=2, freq=440.0)
    rec = make_recorder(tmp_path, mic, system)

    rec.start()
    time.sleep(0.5)
    result = rec.stop()

    mic_data, mic_sr = sf.read(str(tmp_path / "audio-mic.flac"))
    sys_data, sys_sr = sf.read(str(tmp_path / "audio-system.flac"))
    assert mic_sr == 8000 and mic_data.ndim == 1
    assert sys_sr == 16000 and sys_data.shape[1] == 2
    assert len(mic_data) == result.mic.frames_written
    assert len(sys_data) == result.system.frames_written
    # Interleaving corruption would desync frame counts from the index tallies.
    entries = read_index(tmp_path)
    sys_frames = [e["frames_written"] for e in entries
                  if e["ch"] == "system" and "frames_written" in e]
    assert sys_frames[-1] == result.system.frames_written


def test_on_level_callback_invoked(tmp_path: Path) -> None:
    mic = FakeSource(rate=8000, channels=1)
    system = FakeSource(rate=8000, channels=1)
    levels: list[tuple[str, float]] = []
    lock = threading.Lock()

    def on_level(ch: str, rms: float) -> None:
        with lock:
            levels.append((ch, rms))

    rec = make_recorder(tmp_path, mic, system, on_level=on_level, flush_interval_s=0.1)
    rec.start()
    time.sleep(0.5)
    rec.stop()

    channels = {ch for ch, _ in levels}
    assert channels == {"mic", "system"}
    # Sine at amplitude 0.5 -> RMS ~0.35; must be a sane positive number.
    assert all(0.0 < rms < 1.0 for _, rms in levels)


def test_start_failure_raises_audio_device_error(tmp_path: Path) -> None:
    mic = FakeSource(rate=8000, channels=1)
    system = FakeSource(rate=8000, channels=1, open_fail=True)
    rec = make_recorder(tmp_path, mic, system)

    with pytest.raises(AudioDeviceError):
        rec.start()
    assert not rec.is_recording
    # The mic (opened first as "system"? order is system then mic) is cleaned up.
    # No FLAC left half-open — both sources closed.
    assert mic.close_count >= 1 or mic.open_count == 0


def test_mic_missing_raises_audio_device_error(tmp_path: Path) -> None:
    mic = FakeSource(rate=8000, channels=1, open_fail=True)
    system = FakeSource(rate=8000, channels=1)
    rec = make_recorder(tmp_path, mic, system)

    with pytest.raises(AudioDeviceError):
        rec.start()
    assert not rec.is_recording
    # System was opened first and must be closed again on the failure path.
    assert system.close_count >= 1


def test_fake_source_satisfies_protocol() -> None:
    assert isinstance(FakeSource(), StreamSource)


# --------------------------------------------------------------------------
# multiple system loopback devices
# --------------------------------------------------------------------------


def make_multi_recorder(
    tmp_path: Path, mic: FakeSource, systems: list[FakeSource], **kw
) -> AudioRecorder:
    return AudioRecorder(
        tmp_path / "audio-mic.flac",
        tmp_path / "audio-system.flac",
        tmp_path / "timing.jsonl",
        source_factory=lambda: (mic, systems),
        chunk_frames=kw.pop("chunk_frames", 256),
        flush_interval_s=kw.pop("flush_interval_s", 0.1),
        reopen_delays=kw.pop("reopen_delays", (0.02,)),
        **kw,
    )


def test_multi_system_records_extra_files(tmp_path: Path) -> None:
    mic = FakeSource(rate=8000, channels=1)
    systems = [FakeSource(rate=8000, channels=1), FakeSource(rate=8000, channels=2)]
    rec = make_multi_recorder(tmp_path, mic, systems)

    rec.start()
    time.sleep(0.4)
    result = rec.stop()

    assert (tmp_path / "audio-system.flac").exists()
    assert (tmp_path / "audio-system-2.flac").exists()
    data2, sr2 = sf.read(str(tmp_path / "audio-system-2.flac"))
    assert sr2 == 8000 and data2.shape[1] == 2
    assert len(result.extras) == 1
    assert result.extras[0].channel == "system2"
    assert result.extras[0].frames_written == len(data2)
    # timing index carries the extra channel
    channels = {e["ch"] for e in read_index(tmp_path)}
    assert channels == {"mic", "system", "system2"}


def test_extra_system_open_failure_dropped_silently(tmp_path: Path) -> None:
    mic = FakeSource(rate=8000, channels=1)
    systems = [
        FakeSource(rate=8000, channels=1),
        FakeSource(rate=8000, channels=1, open_fail=True),
    ]
    rec = make_multi_recorder(tmp_path, mic, systems)

    rec.start()  # must NOT raise: only the default system device is required
    time.sleep(0.3)
    result = rec.stop()

    assert result.mic.frames_written > 0
    assert result.system.frames_written > 0
    assert result.extras == ()
    assert not (tmp_path / "audio-system-2.flac").exists()


class BlockingSource(FakeSource):
    """A source whose read() blocks until close(), like a silent WASAPI
    loopback device that delivers no frames while nothing is playing."""

    def __init__(self, **kw) -> None:
        super().__init__(realtime=False, **kw)
        self._closed = threading.Event()

    def read(self, frames: int) -> np.ndarray:
        self._closed.wait()
        raise OSError("stream aborted by close")

    def open(self) -> None:
        if self._closed.is_set():
            raise OSError("device gone")
        super().open()

    def close(self) -> None:
        self._closed.set()
        super().close()


def test_stop_returns_despite_source_blocked_in_read(tmp_path: Path) -> None:
    # Regression: a loopback device with nothing playing blocks read()
    # forever; stop() must not hang on its channel thread (real meeting
    # 2026-07-06-1429 hung exactly this way and had to be force-killed).
    mic = FakeSource(rate=8000, channels=1)
    systems: list[FakeSource] = [
        FakeSource(rate=8000, channels=1),
        BlockingSource(rate=8000, channels=1),
    ]
    rec = make_multi_recorder(
        tmp_path, mic, systems, stop_join_timeout_s=0.3
    )

    rec.start()
    time.sleep(0.3)
    t0 = time.monotonic()
    result = rec.stop()
    elapsed = time.monotonic() - t0

    assert elapsed < 5.0, f"stop() took {elapsed:.1f}s"
    assert not rec.is_recording
    # The healthy channels survived intact and are readable.
    assert result.mic.frames_written > 0
    assert result.system.frames_written > 0
    sf.read(str(tmp_path / "audio-mic.flac"))
    sf.read(str(tmp_path / "audio-system.flac"))
    # Recording can start again after a blocked stop.
    mic2 = FakeSource(rate=8000, channels=1)
    rec2 = make_recorder(tmp_path, mic2, FakeSource(rate=8000, channels=1))
    rec2.start()
    rec2.stop()


def test_single_source_factory_still_supported(tmp_path: Path) -> None:
    # The legacy (mic, system) two-source contract keeps working.
    mic = FakeSource(rate=8000, channels=1)
    system = FakeSource(rate=8000, channels=1)
    rec = make_recorder(tmp_path, mic, system)

    rec.start()
    time.sleep(0.3)
    result = rec.stop()
    assert result.extras == ()
    assert result.system.frames_written > 0
