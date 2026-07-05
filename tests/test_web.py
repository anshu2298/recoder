"""Tests for the Phase 4 web UI (recoder.web).

Everything is faked: audio recorder, snapshot capturer, and pipeline runner are
injected doubles, so no real hardware, screen, network, or pipeline runs. Only
the meeting store touches disk (a tmp dir).
"""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from recoder.config import Config
from recoder.store import MeetingState, MeetingStore
from recoder.web.app import create_app
from recoder.web.recording import RecordingManager


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeRecorder:
    def __init__(self, mic_path, system_path, timing_index_path, on_level):
        self.mic_path = Path(mic_path)
        self.on_level = on_level
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True
        # Simulate a level callback for the mic channel.
        self.on_level("mic", 0.1)

    def stop(self):
        self.stopped = True
        return object()


class FakeCapturer:
    def __init__(self, frames_dir, config):
        self.frames_dir = Path(frames_dir)
        self.saved_count = 0
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True
        self.saved_count = 3

    def stop(self):
        self.stopped = True
        return object()


class PipelineSpy:
    """Records invocations; advances the meeting so state changes are visible."""

    def __init__(self, store: MeetingStore):
        self.store = store
        self.calls: list[Path] = []
        self.event = threading.Event()

    def __call__(self, folder: Path, config):
        self.calls.append(Path(folder))
        meeting = self.store.load(folder)
        # Mimic a run that walks recorded -> committed if starting fresh.
        state = meeting.state
        if state == MeetingState.error:
            prev = meeting.read_meta().get("prev_state")
            if prev:
                meeting.advance(prev)
        # Drive to committed from whatever state we're in (best effort).
        order = [
            MeetingState.recorded,
            MeetingState.transcribed,
            MeetingState.diarized,
            MeetingState.analyzed,
            MeetingState.committed,
        ]
        cur = meeting.state
        if cur in order:
            for nxt in order[order.index(cur) + 1 :]:
                meeting.advance(nxt)
        self.event.set()
        return meeting


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def config(tmp_path) -> Config:
    return replace(Config(), meetings_dir=tmp_path / "meetings")


@pytest.fixture
def store(config) -> MeetingStore:
    return MeetingStore(config)


@pytest.fixture
def pipeline_spy(store) -> PipelineSpy:
    return PipelineSpy(store)


@pytest.fixture
def manager(config, store, pipeline_spy) -> RecordingManager:
    return RecordingManager(
        config,
        store,
        audio_recorder_factory=FakeRecorder,
        snapshot_capturer_factory=FakeCapturer,
        pipeline_runner=pipeline_spy,
    )


@pytest.fixture
def client(config, manager) -> TestClient:
    return TestClient(create_app(config, manager))


def _seed_meeting(store: MeetingStore, title: str, *, state=None, summary=None,
                  transcript=None, frames=None, error=None):
    meeting = store.create_meeting(title, "ctx")
    if state is not None and state != MeetingState.recording:
        # Walk linearly to the requested state.
        from recoder.store import LINEAR_ORDER
        for nxt in LINEAR_ORDER[1:]:
            meeting.advance(nxt)
            if nxt == state:
                break
    if error is not None:
        meeting.set_error(error[0], error[1])
    if summary is not None:
        meeting.summary_md.write_text(summary, encoding="utf-8")
    if transcript is not None:
        meeting.transcript_md.write_text(transcript, encoding="utf-8")
    for name, data in (frames or {}).items():
        (meeting.frames_dir / name).write_bytes(data)
    return meeting


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


def test_start_status_stop_happy_path(client, manager, pipeline_spy):
    r = client.post("/api/record/start", json={"title": "Sync", "context_note": "x"})
    assert r.status_code == 200
    folder = r.json()["folder"]

    st = client.get("/api/status").json()
    assert st["recording"] is True
    assert st["folder"] == folder
    assert st["title"] == "Sync"
    assert st["frames_saved"] == 3
    assert st["levels"]["mic"] == pytest.approx(0.1)

    r = client.post("/api/record/stop")
    assert r.status_code == 200

    # Pipeline ran in a background thread; join it deterministically.
    manager._pipeline_threads[-1].join(timeout=5)
    assert pipeline_spy.calls
    assert pipeline_spy.calls[-1].name == folder

    st = client.get("/api/status").json()
    assert st["recording"] is False


def test_double_start_rejected(client):
    assert client.post("/api/record/start", json={"title": "A"}).status_code == 200
    r = client.post("/api/record/start", json={"title": "B"})
    assert r.status_code == 409


def test_stop_when_idle_rejected(client):
    r = client.post("/api/record/stop")
    assert r.status_code == 409


def test_status_shape_when_idle(client):
    st = client.get("/api/status").json()
    assert set(st) >= {
        "recording", "folder", "title", "elapsed_s",
        "frames_saved", "levels", "processing", "last_error",
    }
    assert st["recording"] is False
    assert st["processing"] == []
    assert st["levels"] == {"mic": 0.0, "system": 0.0}


def test_meetings_list(client, store):
    _seed_meeting(store, "First", state=MeetingState.diarized)
    _seed_meeting(store, "Second", state=MeetingState.committed, summary="done")
    rows = client.get("/api/meetings").json()
    titles = {r["title"] for r in rows}
    assert titles == {"First", "Second"}
    by_title = {r["title"]: r for r in rows}
    assert by_title["Second"]["has_summary"] is True
    assert by_title["First"]["has_summary"] is False


def test_meeting_detail_with_summary(client, store):
    m = _seed_meeting(
        store, "Detail", state=MeetingState.committed,
        summary="# TL;DR\nall good", transcript="SPEAKER_1: hi",
    )
    d = client.get(f"/api/meetings/{m.folder.name}").json()
    assert d["summary"] == "# TL;DR\nall good"
    assert d["transcript"] == "SPEAKER_1: hi"
    assert d["meta"]["title"] == "Detail"


def test_meeting_detail_action_items(client, store):
    summary = (
        "# Meeting Summary\n\n## Action Items\n"
        "| Owner | Task | Due |\n| --- | --- | --- |\n"
        "| Rahul | Fix invoice bug | Friday |\n\n## Speakers\nnone\n"
    )
    m = _seed_meeting(store, "AI", state=MeetingState.committed, summary=summary)
    d = client.get(f"/api/meetings/{m.folder.name}").json()
    assert d["action_items"] == [
        {"owner": "Rahul", "task": "Fix invoice bug", "due": "Friday"}
    ]


def test_meeting_detail_action_items_empty_without_summary(client, store):
    m = _seed_meeting(store, "NoSum", state=MeetingState.recorded)
    d = client.get(f"/api/meetings/{m.folder.name}").json()
    assert d["action_items"] == []


def test_frame_serving(client, store):
    jpeg = b"\xff\xd8\xff\xe0fake"
    m = _seed_meeting(store, "Frames", state=MeetingState.committed,
                      frames={"000001_120000.jpg": jpeg})
    r = client.get(f"/api/meetings/{m.folder.name}/frames/000001_120000.jpg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content == jpeg


def test_frame_traversal_blocked(client, store):
    m = _seed_meeting(store, "Trav", state=MeetingState.committed,
                      frames={"a.jpg": b"x"})
    name = m.folder.name
    # Encoded traversal and a plain parent reference must both be refused.
    for attempt in ["..%2fmeta.json", "..%2f..%2fmeta.json", "sub%2fa.jpg"]:
        r = client.get(f"/api/meetings/{name}/frames/{attempt}")
        assert r.status_code in (400, 404), attempt


def test_reprocess_resets_state_and_runs(client, store, manager, pipeline_spy):
    m = _seed_meeting(store, "Repro", state=MeetingState.committed, summary="old")
    r = client.post(f"/api/meetings/{m.folder.name}/reprocess",
                    json={"context_note": "corrected"})
    assert r.status_code == 200
    manager._pipeline_threads[-1].join(timeout=5)
    assert pipeline_spy.calls[-1].name == m.folder.name
    # context_note updated; pipeline started from diarized.
    assert store.load(m.folder).read_meta()["context_note"] == "corrected"


def test_resume_triggers_runner(client, store, manager, pipeline_spy):
    m = _seed_meeting(store, "Resume", state=MeetingState.diarized,
                      error=("analyze", "boom"))
    assert store.load(m.folder).state == MeetingState.error
    r = client.post(f"/api/meetings/{m.folder.name}/resume")
    assert r.status_code == 200
    manager._pipeline_threads[-1].join(timeout=5)
    assert pipeline_spy.calls[-1].name == m.folder.name


def test_processing_list_includes_in_flight_and_errors(client, store):
    _seed_meeting(store, "Working", state=MeetingState.transcribed)
    _seed_meeting(store, "Broken", state=MeetingState.diarized,
                  error=("analyze", "kaboom"))
    _seed_meeting(store, "Finished", state=MeetingState.committed)
    st = client.get("/api/status").json()
    procs = {p["title"]: p for p in st["processing"]}
    assert "Working" in procs
    assert "Broken" in procs
    assert "Finished" not in procs  # committed is excluded
    assert procs["Broken"]["state"] == "error"
    assert procs["Broken"]["error"]["stage"] == "analyze"


def test_unknown_meeting_404(client):
    assert client.get("/api/meetings/nope-does-not-exist").status_code == 404
    assert client.post("/api/meetings/nope/resume").status_code == 404
    assert client.post("/api/meetings/nope/reprocess", json={}).status_code == 404
    assert client.get("/api/meetings/nope/frames/a.jpg").status_code == 404


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Recoder" in r.text
