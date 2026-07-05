from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from recoder.config import Config
from recoder.pipeline.transcribe import (
    GladiaTranscriber,
    RawSegment,
    TranscriptionError,
)


# --------------------------------------------------------------------------
# Test doubles / helpers
# --------------------------------------------------------------------------


def _done_body(utterances: list[dict]) -> dict:
    return {
        "status": "done",
        "result": {"transcription": {"utterances": utterances}},
    }


class FakeGladia:
    """Stateful httpx.MockTransport handler emulating the Gladia API."""

    def __init__(
        self,
        *,
        poll_statuses: list[str] | None = None,
        done_response: dict | None = None,
        upload_429_times: int = 0,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.upload_calls = 0
        self.poll_calls = 0
        self.poll_statuses = poll_statuses or ["done"]
        self.done_response = done_response
        self.upload_429_times = upload_429_times

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        method = request.method

        if path == "/v2/upload" and method == "POST":
            self.upload_calls += 1
            if self.upload_calls <= self.upload_429_times:
                return httpx.Response(429, json={"error": "rate limited"})
            return httpx.Response(200, json={"audio_url": "https://gladia/audio-1"})

        if path == "/v2/pre-recorded" and method == "POST":
            return httpx.Response(
                201, json={"id": "job-1", "result_url": "https://gladia/job-1"}
            )

        if path.startswith("/v2/pre-recorded/") and method == "GET":
            i = min(self.poll_calls, len(self.poll_statuses) - 1)
            status = self.poll_statuses[i]
            self.poll_calls += 1
            if status == "done":
                return httpx.Response(200, json=self.done_response or _done_body([]))
            if status == "error":
                return httpx.Response(
                    200, json={"status": "error", "error": "bad audio format"}
                )
            return httpx.Response(200, json={"status": "processing"})

        return httpx.Response(404, json={"error": f"no route {method} {path}"})


def _client(handler) -> httpx.Client:
    return httpx.Client(
        base_url="https://api.gladia.io",
        transport=httpx.MockTransport(handler),
    )


@pytest.fixture
def cfg() -> Config:
    return Config(
        gladia_api_key="test-key",
        gladia_poll_interval_s=0.01,
        gladia_timeout_s=5,
    )


@pytest.fixture
def audio(tmp_path: Path) -> Path:
    p = tmp_path / "audio-mic.flac"
    p.write_bytes(b"fake flac bytes")
    return p


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(time, "sleep", lambda *a, **k: None)


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------


def test_missing_api_key_raises_at_construction() -> None:
    with pytest.raises(TranscriptionError, match="API key"):
        GladiaTranscriber(Config(gladia_api_key=None))


# --------------------------------------------------------------------------
# Happy path + request shape
# --------------------------------------------------------------------------


def test_happy_path_upload_submit_poll(cfg: Config, audio: Path, tmp_path: Path) -> None:
    fake = FakeGladia(
        done_response=_done_body(
            [
                {"speaker": 0, "start": 0.0, "end": 1.5, "text": "hello", "language": "en"},
                {"speaker": 0, "start": 1.5, "end": 3.0, "text": "world", "language": "en"},
            ]
        )
    )
    tr = GladiaTranscriber(cfg, http_client=_client(fake.handler))
    raw_dump = tmp_path / "gladia-mic.json"

    segments = tr.transcribe(audio, diarize=False, raw_dump_path=raw_dump)

    assert segments == [
        RawSegment(speaker=0, start=0.0, end=1.5, text="hello", language="en"),
        RawSegment(speaker=0, start=1.5, end=3.0, text="world", language="en"),
    ]

    # raw dump written with the full response
    assert raw_dump.exists()
    dumped = json.loads(raw_dump.read_text(encoding="utf-8"))
    assert dumped["status"] == "done"

    # x-gladia-key header on every request
    assert all(r.headers.get("x-gladia-key") == "test-key" for r in fake.requests)


def test_submit_request_fields_no_diarize(cfg: Config, audio: Path) -> None:
    fake = FakeGladia(done_response=_done_body([]))
    tr = GladiaTranscriber(cfg, http_client=_client(fake.handler))
    tr.transcribe(audio, diarize=False)

    submit = next(
        r for r in fake.requests if r.url.path == "/v2/pre-recorded"
    )
    body = json.loads(submit.content)
    assert body["diarization"] is False
    assert "diarization_config" not in body
    assert body["language_config"] == {
        "languages": ["en", "hi"],
        "code_switching": True,
    }
    assert body["audio_url"] == "https://gladia/audio-1"


def test_submit_request_fields_with_diarize(cfg: Config, audio: Path) -> None:
    fake = FakeGladia(done_response=_done_body([]))
    tr = GladiaTranscriber(cfg, http_client=_client(fake.handler))
    tr.transcribe(audio, diarize=True)

    submit = next(r for r in fake.requests if r.url.path == "/v2/pre-recorded")
    body = json.loads(submit.content)
    assert body["diarization"] is True
    assert body["diarization_config"] == {"min_speakers": 1, "max_speakers": 6}

    # multipart upload uses field name "audio"
    upload = next(r for r in fake.requests if r.url.path == "/v2/upload")
    assert b'name="audio"' in upload.content


def test_poll_until_done(cfg: Config, audio: Path) -> None:
    fake = FakeGladia(
        poll_statuses=["queued", "processing", "done"],
        done_response=_done_body(
            [{"speaker": 1, "start": 0.0, "end": 2.0, "text": "hi", "language": "en"}]
        ),
    )
    tr = GladiaTranscriber(cfg, http_client=_client(fake.handler))
    segments = tr.transcribe(audio, diarize=True)
    assert len(segments) == 1
    assert fake.poll_calls == 3


# --------------------------------------------------------------------------
# Error handling
# --------------------------------------------------------------------------


def test_api_error_status_raises(cfg: Config, audio: Path) -> None:
    fake = FakeGladia(poll_statuses=["processing", "error"])
    tr = GladiaTranscriber(cfg, http_client=_client(fake.handler))
    with pytest.raises(TranscriptionError, match="bad audio format"):
        tr.transcribe(audio, diarize=False)


def test_429_retry_then_success(cfg: Config, audio: Path) -> None:
    fake = FakeGladia(upload_429_times=2, done_response=_done_body([]))
    tr = GladiaTranscriber(cfg, http_client=_client(fake.handler))
    segments = tr.transcribe(audio, diarize=False)
    assert segments == []
    # 2 rejected + 1 accepted upload
    assert fake.upload_calls == 3


def test_4xx_non_429_fails_fast(cfg: Config, audio: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid request payload"})

    tr = GladiaTranscriber(cfg, http_client=_client(handler))
    with pytest.raises(TranscriptionError, match="invalid request payload"):
        tr.transcribe(audio, diarize=False)


def test_timeout_deadline(cfg: Config, audio: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGladia(poll_statuses=["processing"])
    tr = GladiaTranscriber(cfg, http_client=_client(fake.handler))

    ticks = iter([0.0, 100.0, 200.0, 300.0, 400.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))

    with pytest.raises(TranscriptionError, match="timed out"):
        tr.transcribe(audio, diarize=False)


# --------------------------------------------------------------------------
# Defensive parsing
# --------------------------------------------------------------------------


def test_defensive_parsing_missing_keys(cfg: Config, audio: Path) -> None:
    fake = FakeGladia(
        done_response=_done_body(
            [
                {"start": 0.0, "end": 1.0, "text": "no speaker no lang"},
                {"speaker": 2, "text": "no times"},
            ]
        )
    )
    tr = GladiaTranscriber(cfg, http_client=_client(fake.handler))
    segments = tr.transcribe(audio, diarize=True)

    assert segments[0].speaker is None
    assert segments[0].language is None
    assert segments[0].text == "no speaker no lang"
    # missing start/end default to 0.0
    assert segments[1].start == 0.0
    assert segments[1].end == 0.0
    assert segments[1].speaker == 2


def test_empty_utterances_is_valid(cfg: Config, audio: Path) -> None:
    fake = FakeGladia(done_response=_done_body([]))
    tr = GladiaTranscriber(cfg, http_client=_client(fake.handler))
    assert tr.transcribe(audio, diarize=False) == []


def test_missing_transcription_block_tolerated(cfg: Config, audio: Path) -> None:
    fake = FakeGladia(done_response={"status": "done", "result": {}})
    tr = GladiaTranscriber(cfg, http_client=_client(fake.handler))
    assert tr.transcribe(audio, diarize=False) == []
