from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from recoder import store as store_mod
from recoder.config import Config
from recoder.store import (
    InvalidTransition,
    Meeting,
    MeetingState,
    MeetingStore,
)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(meetings_dir=tmp_path / "meetings")


@pytest.fixture
def pin_clock(monkeypatch: pytest.MonkeyPatch):
    """Return a setter that pins store._now() to a fixed datetime."""

    def _set(dt: datetime) -> None:
        monkeypatch.setattr(store_mod, "_now", lambda: dt)

    _set(datetime(2026, 7, 5, 14, 30, 0))
    return _set


# --- folder naming + slug -----------------------------------------------------
def test_create_meeting_layout_and_meta(cfg: Config, pin_clock) -> None:
    store = MeetingStore(cfg)
    m = store.create_meeting("Weekly Billing Sync", "sync with Rahul")

    assert m.folder.name == "2026-07-05-1430-weekly-billing-sync"
    assert m.folder.is_dir()
    assert m.frames_dir.is_dir()

    meta = m.read_meta()
    assert meta["state"] == "recording"
    assert meta["title"] == "Weekly Billing Sync"
    assert meta["context_note"] == "sync with Rahul"
    assert meta["schema_version"] == 1
    assert meta["stages"] == {}
    assert meta["started_at"] == "2026-07-05T14:30:00"


def test_slug_fallback_for_empty_and_symbolic_titles(cfg: Config, pin_clock) -> None:
    store = MeetingStore(cfg)
    assert store.create_meeting(None, None).folder.name.endswith("-meeting")
    # advance the clock so folders do not collide
    pin_clock(datetime(2026, 7, 5, 14, 31, 0))
    assert store.create_meeting("***", None).folder.name.endswith("-meeting")


def test_slug_collision_appends_suffix(cfg: Config, pin_clock) -> None:
    store = MeetingStore(cfg)
    a = store.create_meeting("Standup", None)
    b = store.create_meeting("Standup", None)  # same pinned minute -> collision
    c = store.create_meeting("Standup", None)

    assert a.folder.name == "2026-07-05-1430-standup"
    assert b.folder.name == "2026-07-05-1430-standup-2"
    assert c.folder.name == "2026-07-05-1430-standup-3"


# --- standard file paths ------------------------------------------------------
def test_standard_paths(cfg: Config, pin_clock) -> None:
    m = MeetingStore(cfg).create_meeting("x", None)
    assert m.audio_mic.name == "audio-mic.flac"
    assert m.audio_system.name == "audio-system.flac"
    assert m.timing_index.name == "timing.jsonl"
    assert m.transcript_json.name == "transcript.json"
    assert m.transcript_md.name == "transcript.md"
    assert m.summary_md.name == "summary.md"
    assert m.pipeline_log.name == "pipeline.log"
    assert m.meta_path.name == "meta.json"


# --- legal / illegal transitions ---------------------------------------------
def test_full_linear_transition_path(cfg: Config, pin_clock) -> None:
    m = MeetingStore(cfg).create_meeting("x", None)
    for nxt in (
        MeetingState.recorded,
        MeetingState.transcribed,
        MeetingState.diarized,
        MeetingState.analyzed,
        MeetingState.committed,
        MeetingState.done,
    ):
        m.advance(nxt)
        assert m.state == nxt


def test_illegal_transitions_raise(cfg: Config, pin_clock) -> None:
    m = MeetingStore(cfg).create_meeting("x", None)
    # skip a state
    with pytest.raises(InvalidTransition):
        m.advance(MeetingState.transcribed)
    # backward
    m.advance(MeetingState.recorded)
    with pytest.raises(InvalidTransition):
        m.advance(MeetingState.recording)


def test_advance_to_error_from_any_state(cfg: Config, pin_clock) -> None:
    m = MeetingStore(cfg).create_meeting("x", None)
    m.advance(MeetingState.recorded)
    m.advance(MeetingState.transcribed)
    m.advance(MeetingState.error)
    assert m.state == MeetingState.error
    assert m.read_meta()["prev_state"] == "transcribed"


# --- error / resume -----------------------------------------------------------
def test_set_error_records_details_and_predecessor(cfg: Config, pin_clock) -> None:
    m = MeetingStore(cfg).create_meeting("x", None)
    m.advance(MeetingState.recorded)
    m.advance(MeetingState.transcribed)
    m.advance(MeetingState.diarized)
    m.set_error("analyze", "claude timed out")

    meta = m.read_meta()
    assert meta["state"] == "error"
    assert meta["prev_state"] == "diarized"
    assert meta["error"]["stage"] == "analyze"
    assert meta["error"]["message"] == "claude timed out"


def test_error_resume_to_predecessor_only(cfg: Config, pin_clock) -> None:
    m = MeetingStore(cfg).create_meeting("x", None)
    m.advance(MeetingState.recorded)
    m.advance(MeetingState.transcribed)
    m.advance(MeetingState.diarized)
    m.set_error("analyze", "boom")

    # cannot resume to some arbitrary state
    with pytest.raises(InvalidTransition):
        m.advance(MeetingState.analyzed)

    # resume to the exact predecessor clears the error
    m.advance(MeetingState.diarized)
    assert m.state == MeetingState.diarized
    meta = m.read_meta()
    assert meta["error"] is None
    assert meta["prev_state"] is None


# --- atomic write survives a simulated crash ---------------------------------
def test_atomic_write_survives_leftover_tmp(cfg: Config, pin_clock) -> None:
    m = MeetingStore(cfg).create_meeting("x", None)
    m.advance(MeetingState.recorded)

    # simulate a crash mid-write: a garbage .tmp left behind, meta.json intact
    (m.folder / "meta.json.tmp").write_text("{ this is not json", encoding="utf-8")

    reloaded = MeetingStore(cfg).load(m.folder)
    assert reloaded.state == MeetingState.recorded  # last good copy readable
    assert not (m.folder / "meta.json.tmp").exists()  # stale tmp cleaned up

    # and subsequent writes still work
    reloaded.advance(MeetingState.transcribed)
    assert reloaded.state == MeetingState.transcribed


# --- next_pending_stage for every state --------------------------------------
@pytest.mark.parametrize(
    "state, expected",
    [
        (MeetingState.recording, None),
        (MeetingState.recorded, "transcribe"),
        (MeetingState.transcribed, "diarize"),
        (MeetingState.diarized, "analyze"),
        (MeetingState.analyzed, "commit"),
        (MeetingState.committed, None),
        (MeetingState.done, None),
    ],
)
def test_next_pending_stage_per_state(
    cfg: Config, pin_clock, state: MeetingState, expected: str | None
) -> None:
    store = MeetingStore(cfg)
    m = store.create_meeting("x", None)
    m.update_meta(state=state.value)
    assert store.next_pending_stage(m) == expected


def test_next_pending_stage_for_error_is_failed_stage(cfg: Config, pin_clock) -> None:
    store = MeetingStore(cfg)
    m = store.create_meeting("x", None)
    m.advance(MeetingState.recorded)
    m.advance(MeetingState.transcribed)
    m.advance(MeetingState.diarized)
    m.set_error("analyze", "boom")
    assert store.next_pending_stage(m) == "analyze"


# --- list ordering ------------------------------------------------------------
def test_list_meetings_newest_first(cfg: Config, pin_clock) -> None:
    store = MeetingStore(cfg)
    pin_clock(datetime(2026, 7, 5, 9, 0, 0))
    store.create_meeting("early", None)
    pin_clock(datetime(2026, 7, 5, 12, 0, 0))
    store.create_meeting("noon", None)
    pin_clock(datetime(2026, 7, 5, 16, 0, 0))
    store.create_meeting("late", None)

    names = [m.folder.name for m in store.list_meetings()]
    assert names == [
        "2026-07-05-1600-late",
        "2026-07-05-1200-noon",
        "2026-07-05-0900-early",
    ]


def test_list_meetings_ignores_non_meeting_dirs(cfg: Config, pin_clock) -> None:
    store = MeetingStore(cfg)
    store.create_meeting("real", None)
    (cfg.meetings_dir / "junk").mkdir()  # no meta.json
    assert len(store.list_meetings()) == 1


# --- stage recording ----------------------------------------------------------
def test_record_stage(cfg: Config, pin_clock) -> None:
    m = MeetingStore(cfg).create_meeting("x", None)
    m.record_stage("transcribe", 42.5)
    stages = m.read_meta()["stages"]
    assert stages["transcribe"]["duration_s"] == 42.5
    assert stages["transcribe"]["completed_at"] == "2026-07-05T14:30:00"
