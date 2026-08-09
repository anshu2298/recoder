"""Fathom share-link client (spec §4.6).

Fathom is the team's primary notetaker: it joins meetings and, when the call
ends, emits a share URL. Everything Recoder needs is reachable from that URL
alone — no API key, no login, no browser. The share page is an Inertia (Rails)
app, so the same URL returns JSON instead of HTML when asked politely:

  * ``X-Inertia: true``                      -> page props (call metadata)
  * ``+ X-Inertia-Partial-Data: <prop,...>`` -> just those props (the transcript)

The transcript is the point. Fathom's is strictly better than what Recoder's
own pipeline produces: real speaker names (not ``SPEAKER_1``) with per-cue
timings and an absolute recording start, which means an ingested meeting needs
no transcription at all and costs nothing to bring in.

These are the share page's own private endpoints, not a documented API, so
every field is treated as optional and a shape change degrades to a clear
:class:`FathomError` rather than a stack trace.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "FathomError",
    "FathomCall",
    "parse_share_url",
    "fetch_call",
    "fetch_transcript_cues",
    "cues_to_segments",
    "fetch",
]

_SHARE_RE = re.compile(
    r"^https?://(?:www\.)?fathom\.video/share/([A-Za-z0-9_-]+)/?$"
)
_BASE = "https://fathom.video"
# Long enough for Fathom to render a 2h transcript, short enough to fail fast.
_TIMEOUT_S = 60.0
# Sent on every request. Fathom serves the SPA shell to unknown agents.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept": "text/html, application/xhtml+xml",
}
_INERTIA_COMPONENT = "page-call-detail"


class FathomError(RuntimeError):
    """A share link could not be read (bad URL, network, or shape change)."""


@dataclass
class FathomCall:
    """Everything the share page knows about one recording."""

    token: str
    share_url: str
    call_id: str = ""
    title: str = ""
    started_at: str = ""
    recording_started_at: str = ""
    duration_s: float = 0.0
    host_email: str = ""
    video_url: str = ""
    # Populated by fetch(); the roster is derived from the transcript because
    # the share page carries no invitee list.
    speakers: list[str] = field(default_factory=list)
    segments: list[dict] = field(default_factory=list)

    @property
    def t0(self) -> float:
        """Recording start as a POSIX timestamp — the transcript's ``t=0``.

        This is what turns a frame's video offset into a wall clock, which is
        what :mod:`recoder.analysis.evidence` joins frames to speech on.
        Falls back to the scheduled start, then to 0.0 for "unknown".
        """
        for value in (self.recording_started_at, self.started_at):
            stamp = _parse_iso(value)
            if stamp is not None:
                return stamp
        return 0.0


def _parse_iso(value: str) -> float | None:
    """Parse Fathom's ISO-8601 (``...Z``, microseconds) to a POSIX timestamp."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def parse_share_url(url: str) -> str:
    """Extract the share token from a Fathom share URL.

    Accepts the URL with or without a trailing slash and with surrounding
    whitespace (it is normally pasted). Anything else is rejected up front so
    a typo surfaces immediately rather than as a confusing HTTP error.
    """
    text = str(url or "").strip()
    # A pasted URL may carry tracking/query junk; the token is the path.
    text = text.split("?", 1)[0].split("#", 1)[0]
    match = _SHARE_RE.match(text)
    if not match:
        raise FathomError(
            f"not a Fathom share link: {url!r}\n"
            "expected https://fathom.video/share/<token>"
        )
    return match.group(1)


def _get(client: Any, url: str, headers: dict[str, str]) -> Any:
    try:
        response = client.get(url, headers=headers, follow_redirects=True)
    except Exception as exc:  # noqa: BLE001 - httpx has many transport errors
        raise FathomError(f"could not reach Fathom ({exc})") from exc
    if response.status_code == 404:
        raise FathomError(
            "Fathom returned 404 — the share link is wrong, was revoked, or "
            "the recording was deleted"
        )
    if response.status_code >= 400:
        raise FathomError(
            f"Fathom returned HTTP {response.status_code} for {url}"
        )
    return response


def _json(response: Any, what: str) -> dict:
    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise FathomError(
            f"Fathom returned non-JSON for {what}; the share page format has "
            "likely changed"
        ) from exc
    if not isinstance(data, dict):
        raise FathomError(f"unexpected {what} payload from Fathom")
    return data


def _client(http_client: Any | None) -> tuple[Any, bool]:
    """Return ``(client, should_close)`` — injected clients are not closed."""
    if http_client is not None:
        return http_client, False
    import httpx

    return httpx.Client(timeout=_TIMEOUT_S), True


def fetch_call(token: str, *, http_client: Any | None = None) -> FathomCall:
    """Fetch the share page's props — title, timing, host, video URL."""
    share_url = f"{_BASE}/share/{token}"
    client, close = _client(http_client)
    try:
        response = _get(client, share_url, {**_HEADERS, "X-Inertia": "true"})
        payload = _json(response, "the share page")
    finally:
        if close:
            client.close()

    props = payload.get("props")
    if not isinstance(props, dict):
        raise FathomError("Fathom share payload had no props")
    call = props.get("call")
    if not isinstance(call, dict):
        raise FathomError(
            "Fathom share payload had no call object — the link may point at "
            "something other than a recording"
        )

    recording = call.get("recording")
    recording_started = ""
    if isinstance(recording, dict):
        recording_started = str(recording.get("started_at") or "")
    host = call.get("host")
    host_email = ""
    if isinstance(host, dict):
        host_email = str(host.get("email") or "")

    try:
        duration = float(props.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0

    return FathomCall(
        token=token,
        share_url=share_url,
        call_id=str(call.get("id") or ""),
        title=str(call.get("title") or call.get("topic") or "").strip(),
        started_at=str(call.get("started_at") or ""),
        recording_started_at=recording_started,
        duration_s=duration,
        host_email=host_email,
        video_url=str(call.get("video_url") or ""),
    )


def fetch_transcript_cues(
    token: str, *, http_client: Any | None = None
) -> list[list[dict]]:
    """Fetch the transcript via an Inertia partial reload.

    Returns Fathom's own shape: a list of speaker *groups*, each a list of
    utterances that in turn hold word-level ``cues``.
    """
    share_url = f"{_BASE}/share/{token}"
    headers = {
        **_HEADERS,
        "X-Inertia": "true",
        "X-Inertia-Partial-Component": _INERTIA_COMPONENT,
        "X-Inertia-Partial-Data": "transcriptCues",
    }
    client, close = _client(http_client)
    try:
        response = _get(client, share_url, headers)
        payload = _json(response, "the transcript")
    finally:
        if close:
            client.close()

    props = payload.get("props")
    cues = props.get("transcriptCues") if isinstance(props, dict) else None
    if not isinstance(cues, list):
        raise FathomError(
            "Fathom returned no transcript for this recording — it may still "
            "be processing, or the share link may be audio-only"
        )
    return [group for group in cues if isinstance(group, list)]


def cues_to_segments(cues: list[list[dict]]) -> list[dict]:
    """Flatten Fathom's grouped cues into Recoder transcript segments.

    Emits the exact shape :func:`recoder.pipeline.merge.write_transcript`
    writes — ``{speaker, start, end, text, language}`` — so everything
    downstream (evidence, prompts, the UI) is unchanged.

    The *utterance* level is used, not the finer word-level ``cues`` inside
    it: utterances are already sentence-shaped, which is what the analyst
    reads. Segments are sorted by start time because a group's ordering is a
    display concern, not a guarantee.
    """
    segments: list[dict] = []
    for group in cues:
        for utterance in group:
            if not isinstance(utterance, dict):
                continue
            text = str(utterance.get("text") or "").strip()
            if not text:
                continue
            try:
                start = float(utterance.get("start_time") or 0.0)
            except (TypeError, ValueError):
                start = 0.0
            try:
                end = float(utterance.get("end_time") or start)
            except (TypeError, ValueError):
                end = start
            speaker = str(utterance.get("speaker_name") or "").strip()
            segments.append(
                {
                    "speaker": speaker or "unknown",
                    "start": start,
                    "end": max(start, end),
                    "text": text,
                    "language": None,
                }
            )
    segments.sort(key=lambda s: (s["start"], s["end"]))
    return segments


def speakers_in(segments: list[dict]) -> list[str]:
    """Distinct speaker names, in order of first appearance."""
    seen: list[str] = []
    for segment in segments:
        name = str(segment.get("speaker") or "").strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def fetch(url: str, *, http_client: Any | None = None) -> FathomCall:
    """Read a share link end to end: metadata plus the parsed transcript."""
    token = parse_share_url(url)
    call = fetch_call(token, http_client=http_client)
    call.segments = cues_to_segments(
        fetch_transcript_cues(token, http_client=http_client)
    )
    if not call.segments:
        raise FathomError(
            "Fathom returned an empty transcript for this recording"
        )
    call.speakers = speakers_in(call.segments)
    # A recording with no stated duration still has a transcript; the last
    # segment is a good enough length for the summary header.
    if call.duration_s <= 0 and call.segments:
        call.duration_s = float(call.segments[-1]["end"])
    return call
