"""Speech-to-text via the Gladia hosted API (spec §4.2 step 1, §5).

The default engine is Gladia's async pre-recorded API. The two meeting
channels are transcribed separately (never mixed): the mic channel without
diarization (everything is "Me") and the system channel with diarization
(SPEAKER_1..n). This module owns only the STT call; channel merging and
speaker labelling live in :mod:`recoder.pipeline.merge`.

The :class:`Transcriber` protocol keeps the engine pluggable; a local
whisperX fallback can implement the same shape behind the ``[ml]`` extras.

Networking is done with httpx. The client is injectable so tests exercise the
full upload -> submit -> poll flow against an httpx ``MockTransport`` with no
real network.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import httpx

__all__ = [
    "RawSegment",
    "Transcriber",
    "GladiaTranscriber",
    "TranscriptionError",
]

# Retryable-failure backoff schedule (seconds). One initial attempt plus one
# retry per entry -> 4 total tries for network errors / 5xx / 429.
_BACKOFF_SCHEDULE: tuple[float, ...] = (1.0, 4.0, 15.0)


class TranscriptionError(RuntimeError):
    """A transcription attempt failed unrecoverably (surfaced to the runner)."""


@dataclass(frozen=True)
class RawSegment:
    """One utterance as returned by an STT engine, in that channel's file-time.

    ``speaker`` is the engine's raw speaker id (an int when diarized, else
    ``None``). ``start``/``end`` are seconds from the start of the audio file.
    ``language`` is a BCP-47-ish code when the engine reports one, else ``None``.
    """

    speaker: int | None
    start: float
    end: float
    text: str
    language: str | None


@runtime_checkable
class Transcriber(Protocol):
    """Pluggable STT engine contract."""

    def transcribe(
        self,
        audio_path: Path,
        *,
        diarize: bool,
        raw_dump_path: Path | None = None,
    ) -> list[RawSegment]: ...


class GladiaTranscriber:
    """Default :class:`Transcriber` backed by the Gladia pre-recorded API.

    Parameters
    ----------
    config:
        Provides ``gladia_api_key``, ``gladia_base_url``,
        ``gladia_poll_interval_s`` and ``gladia_timeout_s``.
    http_client:
        Optional pre-built :class:`httpx.Client`. Tests inject one wired to an
        ``httpx.MockTransport``. When omitted a client is created against
        ``config.gladia_base_url`` and closed by :meth:`close`.
    """

    def __init__(self, config, http_client: httpx.Client | None = None) -> None:
        api_key = getattr(config, "gladia_api_key", None)
        if not api_key:
            raise TranscriptionError(
                "Gladia API key missing: set GLADIA_API_KEY in the environment "
                "or gladia_api_key in recoder.toml"
            )
        self._config = config
        self._api_key = api_key
        if http_client is None:
            self._client = httpx.Client(
                base_url=config.gladia_base_url,
                timeout=60.0,
            )
            self._owns_client = True
        else:
            self._client = http_client
            self._owns_client = False

    # ------------------------------------------------------------------ API

    def transcribe(
        self,
        audio_path: Path,
        *,
        diarize: bool,
        raw_dump_path: Path | None = None,
    ) -> list[RawSegment]:
        """Upload ``audio_path``, run transcription, return raw segments.

        The full final API response is written to ``raw_dump_path`` (if given)
        for debuggability / reprocessing without re-spending API hours. An
        empty-utterances result (a silent file) is valid and yields ``[]``.
        """
        audio_path = Path(audio_path)
        audio_url = self._upload(audio_path)
        result_id = self._submit(audio_url, diarize=diarize)
        response = self._poll(result_id)

        if raw_dump_path is not None:
            raw_dump_path = Path(raw_dump_path)
            raw_dump_path.parent.mkdir(parents=True, exist_ok=True)
            with raw_dump_path.open("w", encoding="utf-8") as fh:
                json.dump(response, fh, indent=2, ensure_ascii=False)

        return _parse_utterances(response)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "GladiaTranscriber":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -------------------------------------------------------------- steps

    def _upload(self, audio_path: Path) -> str:
        with audio_path.open("rb") as fh:
            files = {"audio": (audio_path.name, fh, "audio/x-flac")}
            resp = self._request_with_retry("POST", "/v2/upload", files=files)
        data = resp.json()
        audio_url = data.get("audio_url")
        if not audio_url:
            raise TranscriptionError(
                f"Gladia upload returned no audio_url: {data!r}"
            )
        return audio_url

    def _submit(self, audio_url: str, *, diarize: bool) -> str:
        body: dict[str, object] = {
            "audio_url": audio_url,
            "diarization": diarize,
            "language_config": {
                "languages": ["en", "hi"],
                "code_switching": True,
            },
        }
        # Only send diarization_config when diarization is actually requested.
        if diarize:
            body["diarization_config"] = {"min_speakers": 1, "max_speakers": 6}
        resp = self._request_with_retry("POST", "/v2/pre-recorded", json=body)
        data = resp.json()
        result_id = data.get("id")
        if not result_id:
            raise TranscriptionError(
                f"Gladia pre-recorded submit returned no id: {data!r}"
            )
        return result_id

    def _poll(self, result_id: str) -> dict:
        timeout_s = float(self._config.gladia_timeout_s)
        poll_interval = float(self._config.gladia_poll_interval_s)
        deadline = time.monotonic() + timeout_s

        while True:
            resp = self._request_with_retry(
                "GET", f"/v2/pre-recorded/{result_id}"
            )
            data = resp.json()
            status = data.get("status")
            if status == "done":
                return data
            if status == "error":
                raise TranscriptionError(
                    f"Gladia transcription failed: {_extract_error(data)}"
                )
            if time.monotonic() >= deadline:
                raise TranscriptionError(
                    f"Gladia transcription timed out after {timeout_s:.0f}s "
                    f"(last status: {status!r})"
                )
            time.sleep(poll_interval)

    # ---------------------------------------------------------- networking

    def _request_with_retry(
        self, method: str, url: str, **kwargs: object
    ) -> httpx.Response:
        """Issue a request, retrying network errors / 5xx / 429 with backoff.

        4xx other than 429 fail fast with the response body in the message.
        """
        headers = dict(kwargs.pop("headers", {}) or {})  # type: ignore[arg-type]
        headers["x-gladia-key"] = self._api_key

        for attempt in range(len(_BACKOFF_SCHEDULE) + 1):
            retryable = False
            detail = ""
            try:
                resp = self._client.request(method, url, headers=headers, **kwargs)  # type: ignore[arg-type]
            except httpx.HTTPError as exc:
                retryable = True
                detail = f"network error: {exc}"
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    retryable = True
                    detail = f"HTTP {resp.status_code}: {resp.text}"
                elif resp.status_code >= 400:
                    # Non-retryable client error: fail fast with the body.
                    raise TranscriptionError(
                        f"Gladia {method} {url} -> HTTP {resp.status_code}: "
                        f"{resp.text}"
                    )
                else:
                    return resp

            if attempt < len(_BACKOFF_SCHEDULE):
                time.sleep(_BACKOFF_SCHEDULE[attempt])
                continue
            raise TranscriptionError(
                f"Gladia {method} {url} failed after "
                f"{len(_BACKOFF_SCHEDULE) + 1} attempts ({detail})"
            )

        # Unreachable, but keeps type checkers happy.
        raise TranscriptionError(f"Gladia {method} {url} failed")


def _parse_utterances(response: dict) -> list[RawSegment]:
    """Defensively pull utterances out of a Gladia 'done' response.

    Expected shape is ``result.transcription.utterances`` as a list of
    ``{speaker?, start, end, text, language?}`` dicts, but every key is
    treated as optional. An absent/empty list yields ``[]``.
    """
    result = response.get("result") or {}
    transcription = result.get("transcription") or {}
    utterances = transcription.get("utterances") or []

    segments: list[RawSegment] = []
    for utt in utterances:
        if not isinstance(utt, dict):
            continue
        speaker = utt.get("speaker")
        speaker_id = int(speaker) if isinstance(speaker, (int, float)) else None
        segments.append(
            RawSegment(
                speaker=speaker_id,
                start=_as_float(utt.get("start")),
                end=_as_float(utt.get("end")),
                text=(utt.get("text") or "").strip(),
                language=utt.get("language"),
            )
        )
    return segments


def _extract_error(data: dict) -> str:
    """Best-effort human message from a Gladia 'error' response."""
    for key in ("error", "message", "error_code"):
        val = data.get(key)
        if isinstance(val, str) and val:
            return val
        if isinstance(val, dict):
            msg = val.get("message") or val.get("error")
            if isinstance(msg, str) and msg:
                return msg
    result = data.get("result")
    if isinstance(result, dict):
        err = result.get("error")
        if isinstance(err, str) and err:
            return err
    return "unknown error"


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
