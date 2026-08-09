"""Ingesting a Fathom share link into a meeting folder (spec §4.6).

Ties the pieces together and lands a meeting that is indistinguishable from a
locally captured one to everything downstream:

    share URL
      -> transcript.json / transcript.md   (real speaker names, no cost)
      -> survey ~10 chunks in parallel     (~8s, ~1 MB)
      -> triage the survey sheet           (screen content? free vision turn)
      -> [only if yes] full frame extraction + perceptual dedupe
      -> frames/index.jsonl with synthesized wall clocks
      -> meta.json at state `diarized`

The meeting enters the pipeline at ``diarized``, so ``analyze`` and ``commit``
run exactly as they do for a captured meeting — and ``transcribe``/``diarize``,
the only stages that spend money, never run at all.

Frames are best-effort throughout: a meeting with no screen-share (or with no
ffmpeg on the machine) still ingests, with its transcript intact and the reason
recorded in ``meta.json``. The transcript is the payload; frames are a bonus
that happens to be priceless when they exist.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from recoder.ingest import fathom, media, triage
from recoder.pipeline.merge import Segment, write_transcript
from recoder.store import Meeting, MeetingState, MeetingStore

logger = logging.getLogger(__name__)

__all__ = ["IngestError", "IngestResult", "ingest_share_url"]

_TRANSCRIPT_SOURCE = "fathom"


class IngestError(RuntimeError):
    """Ingestion could not produce a usable meeting."""


@dataclass
class IngestResult:
    """What ingestion produced, for the UI and the CLI to report."""

    meeting: Meeting
    title: str = ""
    segments: int = 0
    speakers: list[str] = field(default_factory=list)
    frames: int = 0
    frames_status: str = ""
    frames_reason: str = ""


def _write_transcript(meeting: Meeting, call: fathom.FathomCall) -> None:
    segments = [
        Segment(
            speaker=str(s["speaker"]),
            start=float(s["start"]),
            end=float(s["end"]),
            text=str(s["text"]),
            language=s.get("language"),
        )
        for s in call.segments
    ]
    write_transcript(
        segments,
        meeting.transcript_json,
        meeting.transcript_md,
        source=_TRANSCRIPT_SOURCE,
    )


def _write_timing_index(meeting: Meeting, t0: float) -> None:
    """Anchor the meeting's timeline at the recording's start.

    :mod:`recoder.analysis.evidence` places a frame on the transcript by
    subtracting the *mic channel's start wall clock* — read from
    ``timing.jsonl`` — from the frame's own. A captured meeting gets that file
    from the audio recorder; an ingested one has no audio capture at all, so
    without this single anchor line every frame joins to offset ``null`` and
    the analyst gets an inventory with no timestamps and no nearby speech.

    Writing it here rather than special-casing evidence keeps the adapter in
    one place: downstream code stays unaware that these frames came from a
    video.
    """
    entry = {"ch": "mic", "event": "start", "wall": t0, "frames_written": 0}
    meeting.timing_index.write_text(json.dumps(entry) + "\n", encoding="utf-8")


def _ingest_frames(
    meeting: Meeting,
    call: fathom.FathomCall,
    config,
    *,
    session_runner: Callable[[str, object], str] | None,
    want_frames: bool,
) -> tuple[int, str, str]:
    """Survey -> triage -> maybe extract. Returns ``(count, status, reason)``.

    Never raises: every failure mode here is a *degraded* ingest, not a failed
    one, because the transcript is already on disk by this point.
    """
    if not want_frames:
        return 0, "skipped", "frame extraction disabled for this import"
    if not call.video_url:
        return 0, "unavailable", "the recording has no video stream (audio only)"
    if not media.ffmpeg_available():
        return (
            0,
            "unavailable",
            "ffmpeg is not installed, so no frames could be extracted",
        )

    survey_dir = meeting.folder / "survey"
    try:
        chunks = media.fetch_playlist(call.video_url)
        entries = media.survey_chunks(chunks, survey_dir)
    except media.MediaError as exc:
        return 0, "unavailable", str(exc)
    if not entries:
        return 0, "unavailable", "no frames could be sampled from the recording"

    sheet = media.build_survey_sheet(entries, survey_dir)
    if sheet is None:
        return 0, "unavailable", "the sampled frames could not be montaged"

    verdict = triage.triage_survey(
        survey_dir, sheet, len(entries), config, session_runner=session_runner
    )
    if not verdict.has_screen_content:
        # Keep the survey frames: they are a cheap visual record of who was on
        # the call, and they cost nothing more now that they are downloaded.
        return (
            0,
            "no-screen-content",
            verdict.reason or "no shared screen appeared in the sampled frames",
        )

    interval = int(getattr(config, "snapshot_interval_s", 20) or 20)
    try:
        raw = media.extract_frames(
            call.video_url,
            meeting.folder / "frames",
            interval_s=interval,
            max_width=int(getattr(config, "max_frame_width", 1568) or 1568),
            quality=int(getattr(config, "jpeg_quality", 80) or 80),
        )
    except media.MediaError as exc:
        return 0, "failed", str(exc)

    # Offsets must be captured BEFORE dedupe: a frame's position on the
    # transcript timeline is its position in the ORIGINAL sequence, and dedupe
    # removes entries from the middle.
    offsets = {path.name: i * interval for i, path in enumerate(raw)}
    threshold = int(getattr(config, "phash_hamming_threshold", 4) or 4)
    kept = media.dedupe_frames(raw, threshold)
    if not kept:
        return 0, "failed", "every extracted frame was discarded as duplicate"

    media.write_frame_index(
        kept,
        meeting.folder / "frames",
        t0=call.t0,
        interval_s=interval,
        offsets=[float(offsets.get(p.name, 0)) for p in kept],
    )
    reason = verdict.reason or "shared screen content found"
    if verdict.defaulted:
        reason = verdict.reason
    return len(kept), "extracted", reason


def ingest_share_url(
    url: str,
    config,
    *,
    context_note: str | None = None,
    want_frames: bool = True,
    http_client=None,
    session_runner: Callable[[str, object], str] | None = None,
) -> IngestResult:
    """Ingest a Fathom share link into a new meeting folder.

    The returned meeting sits at ``diarized`` and is ready for the normal
    pipeline runner to analyze and commit.
    """
    try:
        call = fathom.fetch(url, http_client=http_client)
    except fathom.FathomError as exc:
        raise IngestError(str(exc)) from exc

    store = MeetingStore(config)
    meeting = store.import_meeting(
        call.title or "Imported meeting",
        started_at=call.recording_started_at or call.started_at,
        state=MeetingState.diarized,
        context_note=context_note,
        source="fathom",
        source_url=call.share_url,
        source_call_id=call.call_id,
        attended=False,
        host_email=call.host_email,
        participants=call.speakers,
        duration_s=call.duration_s,
    )

    try:
        _write_transcript(meeting, call)
        _write_timing_index(meeting, call.t0)
    except OSError as exc:
        # Nothing usable was produced; don't leave a half-meeting behind.
        shutil.rmtree(meeting.folder, ignore_errors=True)
        raise IngestError(f"could not write the transcript ({exc})") from exc

    count, status, reason = _ingest_frames(
        meeting,
        call,
        config,
        session_runner=session_runner,
        want_frames=want_frames,
    )
    meeting.update_meta(
        frames_status=status, frames_reason=reason, frames_count=count
    )
    logger.info(
        "ingested %s: %d segments, frames=%s (%s)",
        meeting.folder.name,
        len(call.segments),
        status,
        reason,
    )

    return IngestResult(
        meeting=meeting,
        title=call.title,
        segments=len(call.segments),
        speakers=call.speakers,
        frames=count,
        frames_status=status,
        frames_reason=reason,
    )
