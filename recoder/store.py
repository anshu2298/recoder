"""Meeting storage + state machine (spec §4.2 / §4.4).

Owns the on-disk layout of a single meeting folder and the resumable
pipeline state machine persisted in ``meta.json``. Has zero heavy deps so
the capture layer can import it freely.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from recoder.config import Config

SCHEMA_VERSION = 1

# Pipeline stage names consumed by the pipeline runner. These are distinct
# from states: a stage *produces* the next state on completion.
STAGE_TRANSCRIBE = "transcribe"
STAGE_DIARIZE = "diarize"
STAGE_ANALYZE = "analyze"
STAGE_COMMIT = "commit"


class MeetingState(str, Enum):
    """Lifecycle of a meeting. Linear happy path plus a terminal-ish error."""

    recording = "recording"
    recorded = "recorded"
    transcribed = "transcribed"
    diarized = "diarized"
    analyzed = "analyzed"
    committed = "committed"
    done = "done"
    error = "error"


# Linear happy-path order. `advance` permits exactly one step forward here,
# plus any-state -> error, plus error -> the state it came from (resume).
LINEAR_ORDER: tuple[MeetingState, ...] = (
    MeetingState.recording,
    MeetingState.recorded,
    MeetingState.transcribed,
    MeetingState.diarized,
    MeetingState.analyzed,
    MeetingState.committed,
    MeetingState.done,
)

# For a given *current* state, the pipeline stage that must run next to move
# it forward. States absent here (recording, committed, done) have no pending
# stage.
_STAGE_FOR_STATE: dict[MeetingState, str] = {
    MeetingState.recorded: STAGE_TRANSCRIBE,
    MeetingState.transcribed: STAGE_DIARIZE,
    MeetingState.diarized: STAGE_ANALYZE,
    MeetingState.analyzed: STAGE_COMMIT,
}


class InvalidTransition(Exception):
    """Raised when an illegal state transition is attempted."""


def _now() -> datetime:
    """Indirection so tests can pin the clock (folder names, timestamps)."""
    return datetime.now()


def _slugify(title: str | None) -> str:
    """Lowercase, hyphenated, filesystem-safe slug; fallback ``meeting``."""
    if not title:
        return "meeting"
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    slug = slug[:50].strip("-")
    return slug or "meeting"


@dataclass
class Meeting:
    """Handle to one meeting folder. Reads/writes its own ``meta.json``."""

    folder: Path

    # --- standard file paths -------------------------------------------------
    @property
    def meta_path(self) -> Path:
        return self.folder / "meta.json"

    @property
    def audio_mic(self) -> Path:
        return self.folder / "audio-mic.flac"

    @property
    def audio_system(self) -> Path:
        return self.folder / "audio-system.flac"

    @property
    def frames_dir(self) -> Path:
        return self.folder / "frames"

    @property
    def timing_index(self) -> Path:
        return self.folder / "timing.jsonl"

    @property
    def transcript_json(self) -> Path:
        return self.folder / "transcript.json"

    @property
    def transcript_md(self) -> Path:
        return self.folder / "transcript.md"

    @property
    def summary_md(self) -> Path:
        return self.folder / "summary.md"

    @property
    def pipeline_log(self) -> Path:
        return self.folder / "pipeline.log"

    @property
    def _meta_tmp(self) -> Path:
        return self.folder / "meta.json.tmp"

    # --- meta.json I/O (atomic write, tolerant read) -------------------------
    def _read_meta(self) -> dict:
        # A leftover .tmp means a previous write crashed before os.replace;
        # meta.json is still the last good copy, so the stale tmp is garbage.
        tmp = self._meta_tmp
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        with self.meta_path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def _write_meta(self, meta: dict) -> None:
        tmp = self._meta_tmp
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.meta_path)

    def read_meta(self) -> dict:
        """Public snapshot of ``meta.json``."""
        return self._read_meta()

    def update_meta(self, **fields: object) -> dict:
        """Merge ``fields`` into meta.json with an atomic write."""
        meta = self._read_meta()
        meta.update(fields)
        self._write_meta(meta)
        return meta

    # --- state machine -------------------------------------------------------
    @property
    def state(self) -> MeetingState:
        return MeetingState(self._read_meta()["state"])

    def advance(self, to_state: MeetingState | str) -> None:
        """Move to ``to_state``, enforcing legal transitions.

        Legal: one step forward along LINEAR_ORDER; any state -> error;
        error -> the exact state it came from (resume).
        """
        to = MeetingState(to_state)
        cur = self.state

        if to == MeetingState.error:
            updates: dict[str, object] = {"state": to.value}
            if cur != MeetingState.error:
                updates["prev_state"] = cur.value
            self.update_meta(**updates)
            return

        if cur == MeetingState.error:
            prev = self._read_meta().get("prev_state")
            if prev is None or MeetingState(prev) != to:
                raise InvalidTransition(
                    f"cannot resume from error to {to.value}; "
                    f"predecessor was {prev}"
                )
            self.update_meta(state=to.value, prev_state=None, error=None)
            return

        idx = LINEAR_ORDER.index(cur)
        if idx + 1 < len(LINEAR_ORDER) and LINEAR_ORDER[idx + 1] == to:
            self.update_meta(state=to.value)
            return

        raise InvalidTransition(f"illegal transition {cur.value} -> {to.value}")

    def set_error(self, stage: str, message: str) -> None:
        """Park the meeting in the error state, remembering where it failed."""
        cur = self.state
        error = {
            "stage": stage,
            "message": message,
            "at": _now().isoformat(),
        }
        updates: dict[str, object] = {"state": MeetingState.error.value, "error": error}
        # Preserve the original predecessor if we error again while in error.
        if cur != MeetingState.error:
            updates["prev_state"] = cur.value
        self.update_meta(**updates)

    def record_stage(self, name: str, duration_s: float) -> None:
        """Record completion of a pipeline stage in the free-form stages dict."""
        meta = self._read_meta()
        stages = dict(meta.get("stages") or {})
        stages[name] = {
            "completed_at": _now().isoformat(),
            "duration_s": duration_s,
        }
        self._write_meta({**meta, "stages": stages})


class MeetingStore:
    """Creates, lists and loads meeting folders under ``config.meetings_dir``."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.meetings_dir = Path(config.meetings_dir)

    def create_meeting(
        self, title: str | None, context_note: str | None
    ) -> Meeting:
        self.meetings_dir.mkdir(parents=True, exist_ok=True)
        stamp = _now().strftime("%Y-%m-%d-%H%M")
        slug = _slugify(title)
        base = f"{stamp}-{slug}"

        folder = self.meetings_dir / base
        n = 2
        while folder.exists():
            folder = self.meetings_dir / f"{base}-{n}"
            n += 1

        folder.mkdir(parents=True)
        (folder / "frames").mkdir()

        meeting = Meeting(folder)
        meeting._write_meta(
            {
                "schema_version": SCHEMA_VERSION,
                "state": MeetingState.recording.value,
                "title": title,
                "context_note": context_note,
                "started_at": _now().isoformat(),
                "stages": {},
            }
        )
        return meeting

    def load(self, folder: Path | str) -> Meeting:
        return Meeting(Path(folder))

    def list_meetings(self) -> list[Meeting]:
        """All meetings, newest first (by started_at, then folder name)."""
        if not self.meetings_dir.exists():
            return []

        found: list[tuple[str, str, Meeting]] = []
        for child in self.meetings_dir.iterdir():
            if not child.is_dir() or not (child / "meta.json").exists():
                continue
            meeting = Meeting(child)
            try:
                started = str(meeting._read_meta().get("started_at") or "")
            except (OSError, json.JSONDecodeError):
                started = ""
            found.append((started, child.name, meeting))

        found.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [m for _, _, m in found]

    def next_pending_stage(self, meeting: Meeting) -> str | None:
        """Name the next pipeline stage for ``meeting``, or None if none.

        In the error state, the pending stage is the one that failed (so the
        runner reruns it after ``advance`` moves error -> predecessor).
        """
        state = meeting.state
        if state == MeetingState.error:
            error = meeting._read_meta().get("error") or {}
            return error.get("stage")
        return _STAGE_FOR_STATE.get(state)
