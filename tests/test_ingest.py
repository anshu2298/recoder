"""Tests for Fathom share-link ingestion (spec §4.6).

No network: the share page is exercised through a stub HTTP client returning
payloads shaped exactly like the real ones (verified against two live share
links), and ffmpeg/SDK boundaries are injected.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from recoder.config import Config
from recoder.ingest import fathom, media, triage
from recoder.ingest.fathom import FathomError
from recoder.ingest.runner import IngestError, ingest_share_url
from recoder.store import MeetingState, MeetingStore

TOKEN = "4Pi2yedvVr_PFoPywbJy7AzZXGPtnzaT"
SHARE_URL = f"https://fathom.video/share/{TOKEN}"


# --- stub transport -----------------------------------------------------------
@dataclass
class _Response:
    status_code: int
    payload: object

    def json(self) -> object:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    @property
    def text(self) -> str:
        return str(self.payload)


class _StubClient:
    """Mimics httpx.Client for the two Inertia requests the client makes."""

    def __init__(self, shallow: object, partial: object, status: int = 200) -> None:
        self.shallow = shallow
        self.partial = partial
        self.status = status
        self.calls: list[dict[str, str]] = []

    def get(self, url, headers=None, follow_redirects=False):
        headers = headers or {}
        self.calls.append(dict(headers))
        payload = (
            self.partial if "X-Inertia-Partial-Data" in headers else self.shallow
        )
        return _Response(self.status, payload)

    def close(self) -> None:  # pragma: no cover - never called on injection
        pass


def _shallow(**over) -> dict:
    call = {
        "id": 777036109,
        "title": "Impromptu Google Meet Meeting",
        "topic": "Impromptu Google Meet Meeting",
        "started_at": "2026-08-07T13:33:11.000000Z",
        "recording": {"started_at": "2026-08-07T13:33:34.870976Z"},
        "host": {"email": "host@example.com"},
        "video_url": f"{SHARE_URL}/video.m3u8",
    }
    call.update(over)
    return {"component": "page-call-detail", "props": {"call": call, "duration": 941.0}}


def _cue(text, speaker, start, end):
    return {
        "text": text,
        "speaker_name": speaker,
        "start_time": start,
        "end_time": end,
    }


def _partial() -> dict:
    return {
        "props": {
            "transcriptCues": [
                [_cue("First thing.", "Ada Lovelace", 0, 6.0),
                 _cue("Second thing.", "Ada Lovelace", 6.5, 9.0)],
                [_cue("A reply.", "Alan Turing", 10.0, 12.0)],
            ]
        }
    }


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(meetings_dir=tmp_path / "meetings")


# --- URL parsing --------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        SHARE_URL,
        SHARE_URL + "/",
        f"  {SHARE_URL}  ",
        SHARE_URL + "?utm_source=slack",
        f"http://fathom.video/share/{TOKEN}",
    ],
)
def test_parse_share_url_accepts_real_world_forms(url: str) -> None:
    assert fathom.parse_share_url(url) == TOKEN


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://fathom.video/calls/777036109",
        "https://example.com/share/abc",
        "not a url",
    ],
)
def test_parse_share_url_rejects_everything_else(url: str) -> None:
    with pytest.raises(FathomError):
        fathom.parse_share_url(url)


# --- payload parsing ----------------------------------------------------------
def test_fetch_call_reads_metadata() -> None:
    client = fathom.fetch_call(TOKEN, http_client=_StubClient(_shallow(), _partial()))
    assert client.title == "Impromptu Google Meet Meeting"
    assert client.call_id == "777036109"
    assert client.duration_s == 941.0
    assert client.host_email == "host@example.com"
    assert client.video_url.endswith("/video.m3u8")


def test_t0_prefers_recording_start_over_scheduled_start() -> None:
    call = fathom.fetch_call(TOKEN, http_client=_StubClient(_shallow(), _partial()))
    expected = datetime(
        2026, 8, 7, 13, 33, 34, 870976, tzinfo=timezone.utc
    ).timestamp()
    assert call.t0 == pytest.approx(expected)


def test_t0_falls_back_to_scheduled_start_then_zero() -> None:
    call = fathom.fetch_call(
        TOKEN, http_client=_StubClient(_shallow(recording=None), _partial())
    )
    assert call.t0 == pytest.approx(
        datetime(2026, 8, 7, 13, 33, 11, tzinfo=timezone.utc).timestamp()
    )
    blank = fathom.fetch_call(
        TOKEN,
        http_client=_StubClient(_shallow(recording=None, started_at=""), _partial()),
    )
    assert blank.t0 == 0.0


def test_cues_flatten_to_segments_sorted_by_time() -> None:
    segments = fathom.cues_to_segments(_partial()["props"]["transcriptCues"])
    assert [s["speaker"] for s in segments] == [
        "Ada Lovelace",
        "Ada Lovelace",
        "Alan Turing",
    ]
    assert [s["start"] for s in segments] == [0.0, 6.5, 10.0]
    assert segments[0]["text"] == "First thing."


def test_cues_drop_empty_text_and_default_missing_speaker() -> None:
    cues = [[
        {"text": "   ", "speaker_name": "X", "start_time": 0, "end_time": 1},
        {"text": "kept", "start_time": 2, "end_time": 3},
    ]]
    segments = fathom.cues_to_segments(cues)
    assert len(segments) == 1
    assert segments[0]["speaker"] == "unknown"


def test_end_never_precedes_start() -> None:
    cues = [[{"text": "x", "speaker_name": "A", "start_time": 10, "end_time": 2}]]
    assert fathom.cues_to_segments(cues)[0]["end"] == 10.0


def test_missing_transcript_is_a_clear_error() -> None:
    with pytest.raises(FathomError, match="no transcript"):
        fathom.fetch_transcript_cues(
            TOKEN, http_client=_StubClient(_shallow(), {"props": {}})
        )


def test_404_explains_the_link_is_gone() -> None:
    client = _StubClient(_shallow(), _partial(), status=404)
    with pytest.raises(FathomError, match="revoked|404"):
        fathom.fetch_call(TOKEN, http_client=client)


def test_fetch_sends_the_inertia_headers() -> None:
    client = _StubClient(_shallow(), _partial())
    fathom.fetch(SHARE_URL, http_client=client)
    assert client.calls[0]["X-Inertia"] == "true"
    assert client.calls[1]["X-Inertia-Partial-Data"] == "transcriptCues"


def test_fetch_derives_speaker_roster() -> None:
    call = fathom.fetch(SHARE_URL, http_client=_StubClient(_shallow(), _partial()))
    assert call.speakers == ["Ada Lovelace", "Alan Turing"]
    assert len(call.segments) == 3


# --- playlist -----------------------------------------------------------------
def test_parse_playlist_resolves_relative_chunk_urls() -> None:
    playlist = (
        "#EXTM3U\n#EXT-X-VERSION:4\n#EXTINF:6.0,\n"
        f"/share/{TOKEN}/video_chunk?key=chunk%2F1%2F1_00001.ts\n"
        "#EXTINF:6.0,\n"
        f"/share/{TOKEN}/video_chunk?key=chunk%2F1%2F1_00002.ts\n"
    )
    chunks = media.parse_playlist(playlist, f"{SHARE_URL}/video.m3u8")
    assert len(chunks) == 2
    assert chunks[0].startswith("https://fathom.video/share/")
    assert "00001.ts" in chunks[0]


def test_parse_playlist_ignores_comments_and_blanks() -> None:
    assert media.parse_playlist("#EXTM3U\n\n#EXT-X-ENDLIST\n", "https://x/y.m3u8") == []


# --- frame index --------------------------------------------------------------
def test_write_frame_index_synthesizes_wall_clocks(tmp_path: Path) -> None:
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    raw = []
    for i in range(3):
        p = frames_dir / f"raw-{i:05d}.jpg"
        p.write_bytes(b"x")
        raw.append(p)

    t0 = 1_700_000_000.0
    entries = media.write_frame_index(raw, frames_dir, t0=t0, interval_s=20)

    assert [e["wall"] for e in entries] == [t0, t0 + 20, t0 + 40]
    assert all(e["source"] == "fathom" for e in entries)
    # Renamed to the capture convention so the archive UI and evidence join
    # cannot tell an ingested frame from a captured one.
    assert entries[0]["file"].startswith("000000_")
    assert not list(frames_dir.glob("raw-*.jpg"))
    lines = (frames_dir / "index.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[1])["file"] == entries[1]["file"]


def test_frame_index_uses_original_offsets_after_dedupe(tmp_path: Path) -> None:
    """Dedupe removes frames from the middle; offsets must not re-compact.

    A frame's place on the transcript timeline is where it was in the ORIGINAL
    sequence. Re-deriving offsets from the kept index would slide every frame
    after a dropped one earlier in time and mis-join it to the wrong speech.
    """
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    kept = []
    for i in (0, 3, 7):  # frames 1,2,4,5,6 were dropped as duplicates
        p = frames_dir / f"raw-{i:05d}.jpg"
        p.write_bytes(b"x")
        kept.append(p)

    t0 = 1_700_000_000.0
    entries = media.write_frame_index(
        kept, frames_dir, t0=t0, interval_s=20, offsets=[0.0, 60.0, 140.0]
    )
    assert [e["wall"] - t0 for e in entries] == [0.0, 60.0, 140.0]


# --- triage -------------------------------------------------------------------
def test_triage_parses_a_share_verdict(tmp_path: Path, cfg: Config) -> None:
    reply = "VERDICT: SHARE\nREASON: an admin dashboard is shown around 20:48"
    result = triage.triage_survey(
        tmp_path, tmp_path / "survey-sheet.jpg", 10, cfg,
        session_runner=lambda p, o: reply,
    )
    assert result.has_screen_content is True
    assert "admin dashboard" in result.reason
    assert result.defaulted is False


def test_triage_parses_a_no_share_verdict(tmp_path: Path, cfg: Config) -> None:
    result = triage.triage_survey(
        tmp_path, tmp_path / "survey-sheet.jpg", 10, cfg,
        session_runner=lambda p, o: "VERDICT: NO_SHARE\nREASON: webcam tiles only",
    )
    assert result.has_screen_content is False
    assert result.defaulted is False


def test_triage_defaults_to_extracting_when_it_fails(tmp_path: Path, cfg: Config) -> None:
    """A broken triage must never silently discard the only record of a screen."""

    def _boom(prompt, options):
        raise RuntimeError("sdk unavailable")

    result = triage.triage_survey(
        tmp_path, tmp_path / "survey-sheet.jpg", 10, cfg, session_runner=_boom
    )
    assert result.has_screen_content is True
    assert result.defaulted is True


def test_triage_defaults_to_extracting_on_an_unparseable_reply(
    tmp_path: Path, cfg: Config
) -> None:
    result = triage.triage_survey(
        tmp_path, tmp_path / "survey-sheet.jpg", 10, cfg,
        session_runner=lambda p, o: "I think maybe there was a screen?",
    )
    assert result.has_screen_content is True
    assert result.defaulted is True


def test_triage_prompt_names_the_sheet_and_forbids_room_content() -> None:
    prompt = triage.build_triage_prompt("survey-sheet.jpg", 10)
    assert "survey-sheet.jpg" in prompt
    assert "VERDICT: SHARE" in prompt
    # A whiteboard behind someone's head is not a screen-share.
    assert "whiteboard" in prompt


# --- store import entry point -------------------------------------------------
def test_import_meeting_enters_at_diarized(cfg: Config) -> None:
    store = MeetingStore(cfg)
    meeting = store.import_meeting(
        "Imported call", started_at="2026-07-31T10:01:46.101638Z", source="fathom"
    )
    assert meeting.state == MeetingState.diarized
    assert meeting.read_meta()["source"] == "fathom"
    assert meeting.frames_dir.exists()


def test_import_meeting_folder_uses_the_real_meeting_time(cfg: Config) -> None:
    """The archive sorts by when the meeting happened, not when it was pasted."""
    store = MeetingStore(cfg)
    meeting = store.import_meeting(
        "Old call", started_at="2026-07-31T10:01:46+00:00"
    )
    assert meeting.folder.name.startswith("2026-07-31-")


def test_import_meeting_tolerates_a_broken_timestamp(cfg: Config) -> None:
    store = MeetingStore(cfg)
    meeting = store.import_meeting("Odd call", started_at="not-a-date")
    assert meeting.state == MeetingState.diarized
    assert meeting.read_meta()["started_at"] == "not-a-date"


def test_import_meeting_collision_gets_a_suffix(cfg: Config) -> None:
    store = MeetingStore(cfg)
    a = store.import_meeting("Dup", started_at="2026-07-31T10:00:00+00:00")
    b = store.import_meeting("Dup", started_at="2026-07-31T10:00:00+00:00")
    assert a.folder != b.folder
    assert b.folder.name.endswith("-2")


# --- end-to-end ingest --------------------------------------------------------
def test_ingest_writes_a_diarized_meeting_with_a_transcript(cfg: Config) -> None:
    result = ingest_share_url(
        SHARE_URL,
        cfg,
        want_frames=False,
        http_client=_StubClient(_shallow(), _partial()),
    )
    meeting = result.meeting
    assert meeting.state == MeetingState.diarized
    assert result.segments == 3
    assert result.speakers == ["Ada Lovelace", "Alan Turing"]

    payload = json.loads(meeting.transcript_json.read_text(encoding="utf-8"))
    assert payload["source"] == "fathom"
    assert payload["segments"][0]["speaker"] == "Ada Lovelace"
    # Real names, so no SPEAKER_n placeholders anywhere.
    assert "SPEAKER_" not in meeting.transcript_md.read_text(encoding="utf-8")
    assert meeting.transcript_md.read_text(encoding="utf-8").startswith("[00:00]")

    meta = meeting.read_meta()
    assert meta["source"] == "fathom"
    assert meta["source_url"] == SHARE_URL
    assert meta["participants"] == ["Ada Lovelace", "Alan Turing"]
    assert meta["frames_status"] == "skipped"


def test_ingest_anchors_the_timeline_so_frames_join_to_speech(cfg: Config) -> None:
    """Regression: evidence takes t=0 from timing.jsonl, which capture writes.

    An ingested meeting has no audio capture, so without an explicit anchor
    every frame joined at offset ``null`` — the analyst got a frame inventory
    with no timestamps and no nearby speech, silently.
    """
    from recoder.analysis.evidence import build_evidence

    result = ingest_share_url(
        SHARE_URL, cfg, want_frames=False,
        http_client=_StubClient(_shallow(), _partial()),
    )
    meeting = result.meeting

    anchor = json.loads(meeting.timing_index.read_text(encoding="utf-8").strip())
    assert anchor["ch"] == "mic" and anchor["event"] == "start"
    t0 = anchor["wall"]
    assert t0 == pytest.approx(
        datetime(2026, 8, 7, 13, 33, 34, 870976, tzinfo=timezone.utc).timestamp()
    )

    # A frame 20s into the recording must land at 00:20 with the speech there.
    media.write_frame_index(
        [_stub_jpeg(meeting.frames_dir / "raw-00001.jpg")],
        meeting.frames_dir,
        t0=t0,
        interval_s=20,
        offsets=[20.0],
    )
    entries = build_evidence(meeting.folder)
    assert entries[0]["offset_s"] == 20.0
    assert entries[0]["mmss"] == "00:20"


def _stub_jpeg(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


def test_ingest_never_runs_the_billed_stages(cfg: Config) -> None:
    """An ingested meeting must skip transcribe/diarize — they cost money."""
    result = ingest_share_url(
        SHARE_URL, cfg, want_frames=False,
        http_client=_StubClient(_shallow(), _partial()),
    )
    store = MeetingStore(cfg)
    assert store.next_pending_stage(result.meeting) == "analyze"


def test_ingest_reports_missing_ffmpeg_without_failing(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(media, "ffmpeg_available", lambda: False)
    result = ingest_share_url(
        SHARE_URL, cfg, http_client=_StubClient(_shallow(), _partial())
    )
    assert result.frames == 0
    assert result.frames_status == "unavailable"
    assert "ffmpeg" in result.frames_reason
    # The transcript still landed: frames are a bonus, not the payload.
    assert result.meeting.transcript_json.exists()


def test_ingest_handles_an_audio_only_recording(cfg: Config) -> None:
    result = ingest_share_url(
        SHARE_URL, cfg,
        http_client=_StubClient(_shallow(video_url=""), _partial()),
    )
    assert result.frames_status == "unavailable"
    assert "audio only" in result.frames_reason
    assert result.meeting.transcript_json.exists()


def test_ingest_rejects_a_bad_link_before_creating_anything(cfg: Config) -> None:
    with pytest.raises(IngestError):
        ingest_share_url("https://example.com/nope", cfg, want_frames=False)
    assert not (cfg.meetings_dir).exists() or not list(cfg.meetings_dir.iterdir())


def test_ingest_surfaces_an_empty_transcript_as_an_error(cfg: Config) -> None:
    empty = {"props": {"transcriptCues": []}}
    with pytest.raises(IngestError, match="empty transcript"):
        ingest_share_url(
            SHARE_URL, cfg, want_frames=False,
            http_client=_StubClient(_shallow(), empty),
        )
