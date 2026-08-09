"""Tests for the per-meeting pipeline lock and the startup resume sweep."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from recoder.config import Config
from recoder.pipeline import lock as pipeline_lock
from recoder.pipeline.runner import PipelineError, run_pipeline
from recoder.store import MeetingState, MeetingStore
from recoder.web.recording import RecordingManager


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(meetings_dir=tmp_path / "meetings", gladia_api_key="k")


def _make_meeting(cfg: Config, state: MeetingState):
    store = MeetingStore(cfg)
    m = store.create_meeting("Locked Meeting", None)
    order = [
        MeetingState.recorded,
        MeetingState.transcribed,
        MeetingState.diarized,
        MeetingState.analyzed,
    ]
    for s in order:
        if MeetingState(m.read_meta()["state"]) == state:
            break
        m.advance(s)
        if s == state:
            break
    return m


# --------------------------------------------------------------------------
# Lock primitives
# --------------------------------------------------------------------------


def test_acquire_release_roundtrip(tmp_path: Path) -> None:
    assert not pipeline_lock.is_live(tmp_path)
    pipeline_lock.acquire(tmp_path)
    assert pipeline_lock.is_live(tmp_path)  # we are the live owner
    pipeline_lock.release(tmp_path)
    assert not pipeline_lock.is_live(tmp_path)


def test_live_lock_blocks_second_acquire(tmp_path: Path) -> None:
    pipeline_lock.acquire(tmp_path)
    with pytest.raises(pipeline_lock.LockHeld):
        pipeline_lock.acquire(tmp_path)
    pipeline_lock.release(tmp_path)


def test_stale_lock_is_reclaimed(tmp_path: Path) -> None:
    """A lock whose owner pid is dead must not block acquisition."""
    # Find a pid that does not exist.
    dead_pid = 4_000_000
    import psutil

    while psutil.pid_exists(dead_pid):
        dead_pid += 1
    (tmp_path / pipeline_lock.LOCK_NAME).write_text(
        json.dumps({"pid": dead_pid, "create_time": 1.0}), encoding="utf-8"
    )
    assert not pipeline_lock.is_live(tmp_path)
    pipeline_lock.acquire(tmp_path)  # must not raise
    pipeline_lock.release(tmp_path)


def test_recycled_pid_is_stale(tmp_path: Path) -> None:
    """Our own pid with a wildly wrong create-time = recycled pid = stale."""
    (tmp_path / pipeline_lock.LOCK_NAME).write_text(
        json.dumps({"pid": os.getpid(), "create_time": 1.0}), encoding="utf-8"
    )
    assert not pipeline_lock.is_live(tmp_path)


def test_garbage_lock_file_is_stale(tmp_path: Path) -> None:
    (tmp_path / pipeline_lock.LOCK_NAME).write_text("not json", encoding="utf-8")
    assert not pipeline_lock.is_live(tmp_path)


# --------------------------------------------------------------------------
# Runner integration
# --------------------------------------------------------------------------


def test_run_pipeline_refuses_live_lock(cfg: Config) -> None:
    m = _make_meeting(cfg, MeetingState.recorded)
    pipeline_lock.acquire(m.folder)  # simulate a live concurrent runner
    try:
        with pytest.raises(PipelineError, match="already running"):
            run_pipeline(m.folder, cfg)
    finally:
        pipeline_lock.release(m.folder)


def test_run_pipeline_releases_lock_on_failure(cfg: Config) -> None:
    m = _make_meeting(cfg, MeetingState.recorded)

    class Boom:
        def transcribe(self, *a, **k):
            raise RuntimeError("boom")

    with pytest.raises(PipelineError):
        run_pipeline(m.folder, cfg, transcriber=Boom())
    assert not pipeline_lock.is_live(m.folder)
    assert not (m.folder / pipeline_lock.LOCK_NAME).exists()


# --------------------------------------------------------------------------
# Startup sweep
# --------------------------------------------------------------------------


def _manager_with_spy(cfg: Config) -> tuple[RecordingManager, list[str]]:
    launched: list[str] = []

    def fake_runner(folder: Path, config: Config) -> None:
        launched.append(Path(folder).name)

    manager = RecordingManager(cfg, pipeline_runner=fake_runner)
    return manager, launched


def _join_threads(manager: RecordingManager) -> None:
    for t in manager._pipeline_threads:
        t.join(timeout=5)


def test_sweep_resumes_orphaned_intermediate_states(cfg: Config) -> None:
    stuck = _make_meeting(cfg, MeetingState.transcribed)
    manager, launched = _manager_with_spy(cfg)

    resumed = manager.resume_pending()
    _join_threads(manager)

    assert resumed == [stuck.folder.name]
    assert launched == [stuck.folder.name]


def test_sweep_skips_error_and_done(cfg: Config) -> None:
    errored = _make_meeting(cfg, MeetingState.recorded)
    errored.set_error("transcribe", "boom")
    finished = _make_meeting(cfg, MeetingState.recorded)
    for s in (
        MeetingState.transcribed,
        MeetingState.diarized,
        MeetingState.analyzed,
        MeetingState.committed,
        MeetingState.done,
    ):
        finished.advance(s)

    manager, launched = _manager_with_spy(cfg)
    assert manager.resume_pending() == []
    assert launched == []


def test_sweep_skips_meeting_with_live_lock(cfg: Config) -> None:
    stuck = _make_meeting(cfg, MeetingState.diarized)
    pipeline_lock.acquire(stuck.folder)
    try:
        manager, launched = _manager_with_spy(cfg)
        assert manager.resume_pending() == []
        assert launched == []
    finally:
        pipeline_lock.release(stuck.folder)


def test_sweep_salvages_crashed_recording(cfg: Config) -> None:
    """A folder stuck in `recording` with audio but no live capture was a
    crashed recording: it is advanced to `recorded` and processed."""
    store = MeetingStore(cfg)
    crashed = store.create_meeting("Crashed", None)  # state == recording
    crashed.audio_mic.write_bytes(b"fLaC")  # audio exists up to the crash

    manager, launched = _manager_with_spy(cfg)
    resumed = manager.resume_pending()
    _join_threads(manager)

    assert resumed == [crashed.folder.name]
    assert launched == [crashed.folder.name]
    assert MeetingState(crashed.read_meta()["state"]) == MeetingState.recorded


def test_sweep_leaves_recording_without_audio(cfg: Config) -> None:
    store = MeetingStore(cfg)
    empty = store.create_meeting("Empty crash", None)  # no audio written

    manager, launched = _manager_with_spy(cfg)
    assert manager.resume_pending() == []
    assert launched == []
    assert MeetingState(empty.read_meta()["state"]) == MeetingState.recording
