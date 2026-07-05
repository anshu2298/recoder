"""Recording orchestration for the web UI (spec §4.3).

:class:`RecordingManager` is the single stateful object the FastAPI app owns.
It wires the capture layer (audio + snapshots) to the meeting store and, on
stop, hands the finished folder to the post-call pipeline on a daemon thread so
the HTTP request never blocks.

The capture and pipeline collaborators are injected as factories so the whole
manager is exercisable in tests without real audio hardware, a screen, or the
network.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from recoder.config import Config
from recoder.store import Meeting, MeetingState, MeetingStore

__all__ = ["RecordingManager"]


# States that mean "handed off to / moving through the pipeline". A meeting is
# shown in the Processing view while it is in one of these; recording, committed
# and done are excluded.
_PROCESSING_STATES = frozenset(
    {
        MeetingState.recorded,
        MeetingState.transcribed,
        MeetingState.diarized,
        MeetingState.analyzed,
        MeetingState.error,
    }
)


# --- default production collaborators --------------------------------------


def _default_audio_factory(
    mic_path: Path,
    system_path: Path,
    timing_index_path: Path,
    on_level: Callable[[str, float], None],
):
    from recoder.capture.audio import AudioRecorder

    return AudioRecorder(
        mic_path, system_path, timing_index_path, on_level=on_level
    )


def _default_snapshot_factory(frames_dir: Path, config: Config):
    from recoder.capture.snapshots import SnapshotCapturer

    return SnapshotCapturer(frames_dir, config)


def _default_pipeline_runner(folder: Path, config: Config):
    from recoder.pipeline.runner import run_pipeline

    return run_pipeline(folder, config)


class RecordingManager:
    """Owns the live recording session and pipeline hand-off for the UI."""

    def __init__(
        self,
        config: Config,
        store: MeetingStore | None = None,
        *,
        audio_recorder_factory: Callable[..., object] | None = None,
        snapshot_capturer_factory: Callable[..., object] | None = None,
        pipeline_runner: Callable[[Path, Config], object] | None = None,
    ) -> None:
        self.config = config
        self.store = store or MeetingStore(config)
        self._audio_factory = audio_recorder_factory or _default_audio_factory
        self._snapshot_factory = (
            snapshot_capturer_factory or _default_snapshot_factory
        )
        self._pipeline_runner = pipeline_runner or _default_pipeline_runner

        self._lock = threading.Lock()
        self._recording = False
        self._meeting: Meeting | None = None
        self._recorder = None
        self._capturer = None
        self._started_at: float = 0.0
        self._levels: dict[str, float] = {"mic": 0.0, "system": 0.0}
        self._last_error: str | None = None
        # Kept so tests can join the background pipeline thread deterministically.
        self._pipeline_threads: list[threading.Thread] = []

    # -- live recording -----------------------------------------------------

    def start(self, title: str | None, context_note: str | None) -> Meeting:
        """Create a meeting and begin audio + snapshot capture.

        Raises :class:`RuntimeError` if a recording is already in progress.
        """
        with self._lock:
            if self._recording:
                raise RuntimeError("a recording is already in progress")

            meeting = self.store.create_meeting(title, context_note or None)
            recorder = self._audio_factory(
                meeting.audio_mic,
                meeting.audio_system,
                meeting.timing_index,
                self._on_level,
            )
            capturer = self._snapshot_factory(meeting.frames_dir, self.config)

            # Reset levels before starting so the recorder's first on_level
            # callbacks are not clobbered.
            self._levels = {"mic": 0.0, "system": 0.0}

            # Audio is sacred: start it first so a device failure aborts before
            # we claim to be recording (snapshots never raise on start).
            recorder.start()
            try:
                capturer.start()
            except Exception:  # noqa: BLE001 - snapshot failure must not stop audio
                pass

            self._meeting = meeting
            self._recorder = recorder
            self._capturer = capturer
            self._last_error = None
            self._started_at = time.monotonic()
            self._recording = True
            return meeting

    def stop(self) -> Meeting:
        """Stop capture, advance to ``recorded`` and launch the pipeline.

        Raises :class:`RuntimeError` if no recording is in progress. The
        pipeline runs on a daemon thread; failures are recorded in the
        meeting's ``meta.json`` (error state) for the UI to surface.
        """
        with self._lock:
            if not self._recording or self._meeting is None:
                raise RuntimeError("no recording is in progress")

            meeting = self._meeting
            capturer = self._capturer
            recorder = self._recorder

            # Snapshots first (best-effort), then audio (authoritative).
            if capturer is not None:
                try:
                    capturer.stop()
                except Exception:  # noqa: BLE001
                    pass
            if recorder is not None:
                try:
                    recorder.stop()
                except Exception as exc:  # noqa: BLE001
                    self._last_error = f"audio stop failed: {exc}"

            self._recording = False
            self._meeting = None
            self._recorder = None
            self._capturer = None
            self._started_at = 0.0
            self._levels = {"mic": 0.0, "system": 0.0}

        meeting.advance(MeetingState.recorded)
        self.start_pipeline(meeting.folder)
        return meeting

    def _on_level(self, channel: str, rms: float) -> None:
        # Called from the audio capture threads; a plain dict assignment is
        # atomic enough for a status read.
        self._levels[channel] = float(rms)

    # -- pipeline hand-off --------------------------------------------------

    def start_pipeline(self, folder: Path | str) -> threading.Thread:
        """Run (or resume) the pipeline for ``folder`` on a daemon thread."""
        folder = Path(folder)

        def _target() -> None:
            try:
                self._pipeline_runner(folder, self.config)
            except Exception as exc:  # noqa: BLE001 - meta.json holds the truth
                # The runner already parked the meeting in the error state; keep
                # a copy for the status endpoint's convenience.
                self._last_error = f"pipeline error ({folder.name}): {exc}"

        thread = threading.Thread(
            target=_target, name=f"pipeline-{folder.name}", daemon=True
        )
        with self._lock:
            self._pipeline_threads.append(thread)
            # Bound the bookkeeping list.
            if len(self._pipeline_threads) > 32:
                self._pipeline_threads = self._pipeline_threads[-32:]
        thread.start()
        return thread

    # -- status -------------------------------------------------------------

    def status(self) -> dict:
        """Snapshot of live recording + in-flight pipeline meetings."""
        with self._lock:
            recording = self._recording
            meeting = self._meeting
            capturer = self._capturer
            started_at = self._started_at
            levels = dict(self._levels)
            last_error = self._last_error

        folder = None
        title = None
        elapsed_s = 0.0
        frames_saved = 0
        if recording and meeting is not None:
            folder = meeting.folder.name
            try:
                title = meeting.read_meta().get("title")
            except Exception:  # noqa: BLE001
                title = None
            elapsed_s = max(0.0, time.monotonic() - started_at)
            if capturer is not None:
                try:
                    frames_saved = int(capturer.saved_count)
                except Exception:  # noqa: BLE001
                    frames_saved = 0

        return {
            "recording": recording,
            "folder": folder,
            "title": title,
            "elapsed_s": elapsed_s,
            "frames_saved": frames_saved,
            "levels": {
                "mic": levels.get("mic", 0.0),
                "system": levels.get("system", 0.0),
            },
            "processing": self._processing_list(),
            "last_error": last_error,
        }

    def _processing_list(self) -> list[dict]:
        items: list[dict] = []
        for meeting in self.store.list_meetings():
            try:
                meta = meeting.read_meta()
            except Exception:  # noqa: BLE001 - skip unreadable folders
                continue
            try:
                state = MeetingState(meta.get("state"))
            except ValueError:
                continue
            if state not in _PROCESSING_STATES:
                continue
            entry = {
                "folder": meeting.folder.name,
                "title": meta.get("title"),
                "state": state.value,
            }
            if state == MeetingState.error:
                entry["error"] = meta.get("error")
            items.append(entry)
        return items
