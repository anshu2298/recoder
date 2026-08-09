"""Deciding whether a recording's frames are worth extracting (spec §4.6).

The question is narrow and visual: *did anyone share a screen?* A recording
where they did holds the only surviving copy of whatever was on it; one where
they did not holds nothing but webcam tiles, and downloading hundreds of
megabytes to prove that is waste.

Pixel statistics were tried first and rejected on measured evidence. Across
real recordings the two classes do not separate: a dark-themed dashboard scores
*lower* on brightness-and-neutrality than a webcam pointed at a white wall,
putting screen-share frames on both sides of every threshold with plain
gallery views in between. Any rule that fits is fitting two meetings, not the
problem.

So triage looks at the survey sheet. It is a single vision turn with only the
``Read`` tool and no MCP servers — the same capability that already reads
contact sheets during analysis, and free under the subscription auth the rest
of the pipeline uses. When the session is unavailable or ambiguous, the answer
defaults to "extract": paying for a download beats silently discarding the
only record of a shared screen.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

__all__ = ["TriageResult", "build_triage_prompt", "triage_survey"]

TRIAGE_MAX_TURNS = 6
_VERDICT_RE = re.compile(r"^\s*VERDICT:\s*(SHARE|NO_SHARE)\s*$", re.MULTILINE)


@dataclass
class TriageResult:
    """Whether to extract frames, and why."""

    has_screen_content: bool
    reason: str = ""
    raw: str = ""
    # True when we never got a usable answer and defaulted to extracting.
    defaulted: bool = False


def build_triage_prompt(sheet_name: str, frame_count: int) -> str:
    """Prompt for the one-turn look at the survey sheet."""
    return f"""You are triaging a recorded meeting to decide whether its video is
worth downloading in full.

Read the image `{sheet_name}` in your current directory. It is a contact sheet
of {frame_count} frames sampled evenly across the whole recording, each labelled
with its timestamp.

The recording is a video-call notetaker's capture. Every frame therefore shows
ONE of two things:

- **Gallery view** — participants' webcam tiles (faces, rooms, "Recording and
  taking notes" placeholders) and nothing else. These frames carry no
  information beyond who was on the call.
- **Shared screen** — someone presenting: documents, slides, dashboards, code,
  a browser, an IDE, a terminal, a design tool. In this layout the shared
  content usually fills most of the frame with participant tiles pushed to a
  narrow strip at one side.

Decide whether ANY sampled frame shows shared-screen content. A single frame is
enough — a screen shared briefly still matters. Judge what is actually on
screen, not how interesting it looks; a mostly-empty shared window still counts
as a shared screen. Webcam backgrounds that happen to contain a whiteboard,
poster, or monitor in the room do NOT count: the content must be shared into
the call, filling a region of the frame as its own surface.

Reply with EXACTLY two lines and nothing else:

    VERDICT: SHARE
    REASON: <one sentence naming what you saw and roughly when>

or

    VERDICT: NO_SHARE
    REASON: <one sentence>
"""


def _parse(reply: str) -> TriageResult | None:
    match = _VERDICT_RE.search(reply or "")
    if not match:
        return None
    reason = ""
    for line in (reply or "").splitlines():
        if line.strip().upper().startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()
            break
    return TriageResult(
        has_screen_content=match.group(1) == "SHARE",
        reason=reason,
        raw=reply,
    )


def triage_survey(
    survey_dir: Path,
    sheet_path: Path,
    frame_count: int,
    config: object,
    *,
    session_runner: Callable[[str, object], str] | None = None,
) -> TriageResult:
    """Ask whether the survey sheet shows any shared-screen content.

    Never raises: any failure (no SDK, timeout, unparseable reply) resolves to
    "extract anyway", flagged via :attr:`TriageResult.defaulted`, because the
    cost of a wasted download is recoverable and the cost of dropping the only
    record of a shared screen is not.
    """
    from recoder.analysis.session import (
        _default_session_runner,
        build_session_options,
    )

    runner = session_runner or _default_session_runner
    prompt = build_triage_prompt(sheet_path.name, frame_count)
    try:
        options = build_session_options(
            Path(survey_dir), config, TRIAGE_MAX_TURNS, {}, ["Read"]
        )
        reply = runner(prompt, options)
    except Exception as exc:  # noqa: BLE001 - triage must never sink ingestion
        logger.warning("frame triage unavailable (%s); extracting anyway", exc)
        return TriageResult(
            has_screen_content=True,
            reason=f"triage unavailable ({exc}); extracted without checking",
            defaulted=True,
        )

    parsed = _parse(reply)
    if parsed is None:
        logger.warning("frame triage gave no verdict; extracting anyway")
        return TriageResult(
            has_screen_content=True,
            reason="triage returned no verdict; extracted without checking",
            raw=reply,
            defaulted=True,
        )
    return parsed
