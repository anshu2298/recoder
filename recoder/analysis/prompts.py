"""Prompt assembly for the Claude analysis + CCR commit sessions (spec §4.2).

Pure functions only — no I/O, no SDK. Everything here is a string builder so
the prompts can be snapshot-tested without touching Claude or the filesystem.
"""

from __future__ import annotations

# The summary.md section contract. The analysis session MUST emit a markdown
# document containing exactly these headers, in this order. session.py validates
# their presence and issues one corrective turn if any are missing.
REQUIRED_SECTIONS: tuple[str, ...] = (
    "# Meeting Summary",
    "## TL;DR",
    "## Discussion",
    "## Decisions",
    "## Action Items",
    "## Open Questions",
    "## Project Mapping",
    "## Speakers",
)


def _fmt_timestamp(seconds: float) -> str:
    """Render a segment start offset as ``[MM:SS]`` (minutes may exceed 60)."""
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        total = 0
    if total < 0:
        total = 0
    minutes, secs = divmod(total, 60)
    return f"[{minutes:02d}:{secs:02d}]"


def render_transcript(segments: list[dict]) -> str:
    """Render diarized segments as ``[MM:SS] Speaker: text`` lines."""
    lines: list[str] = []
    for seg in segments:
        speaker = str(seg.get("speaker") or "unknown")
        text = str(seg.get("text") or "").strip()
        stamp = _fmt_timestamp(seg.get("start", 0.0))
        lines.append(f"{stamp} {speaker}: {text}")
    return "\n".join(lines)


def render_frame_table(frame_inventory: list[dict]) -> str:
    """Render the frames inventory as a markdown table.

    Columns: filename, clock time (wall), window title, fallback flag. The
    fallback flag surfaces the occlusion limitation (spec §4.1): a fullscreen
    fallback grab may show unrelated desktop content rather than the meeting.
    """
    header = (
        "| Filename | Clock time | Window title | Fallback fullscreen |\n"
        "| --- | --- | --- | --- |"
    )
    if not frame_inventory:
        return header + "\n| (no frames captured) | | | |"

    rows: list[str] = []
    for entry in frame_inventory:
        filename = str(entry.get("file") or entry.get("filename") or "").strip()
        wall = str(entry.get("wall") or "").strip()
        title = str(entry.get("window_title") or "").strip().replace("|", "\\|")
        fallback = bool(entry.get("fallback_fullscreen", False))
        flag = "yes" if fallback else "no"
        rows.append(f"| {filename} | {wall} | {title} | {flag} |")
    return header + "\n" + "\n".join(rows)


def _fmt_duration(duration_s: float) -> str:
    try:
        total = int(round(float(duration_s)))
    except (TypeError, ValueError):
        total = 0
    if total < 0:
        total = 0
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    return f"{minutes}m {secs}s"


def build_analysis_prompt(
    meta: dict,
    transcript_md: str,
    frame_inventory: list[dict],
    duration_s: float,
) -> str:
    """Build the full analysis prompt for one meeting.

    Frames are delivered via the filesystem: the session's cwd is the meeting
    folder and this prompt lists the ``frames/`` inventory; Claude reads the
    images it deems relevant with the Read tool.
    """
    title = str(meta.get("title") or "Untitled meeting")
    context_note = str(meta.get("context_note") or "").strip() or "(none provided)"
    started_at = str(meta.get("started_at") or "unknown")
    duration = _fmt_duration(duration_s)
    frame_table = render_frame_table(frame_inventory)

    sections_list = "\n".join(f"  - {s}" for s in REQUIRED_SECTIONS)

    return f"""You are analyzing a recorded meeting to produce a context-aware summary.

## Meeting metadata
- Title: {title}
- Started at: {started_at}
- Duration: {duration}
- Context note (from the user): {context_note}

## Speaker-labeled transcript
Segments are rendered as `[MM:SS] Speaker: text`. "Me" is the user (this PC's
microphone); SPEAKER_1, SPEAKER_2, ... are other participants from diarization.

{transcript_md}

## On-screen frames
The meeting window was snapshotted roughly every 20 seconds into the `frames/`
directory (your current working directory is the meeting folder). Below is the
inventory. Use the `Read` tool to open the frames you judge relevant — slides,
demos, shared documents, screens referenced in the discussion.

IMPORTANT limitation: frame capture is a coordinate-region grab. If the meeting
window was covered or minimized, a frame may show unrelated desktop content
instead of the meeting. The "Fallback fullscreen" column flags full-screen
fallback grabs, which are the most likely to be unrelated. Treat frames as
supporting evidence, not ground truth, and ignore ones that are clearly
off-topic desktop content.

{frame_table}

## Project memory (CCR)
BEFORE you write the summary, use `gcc_search` and `gcc_context` to pull related
project memory. Search for the people, projects, and topics named in the
transcript so your summary connects this meeting to the user's existing work.

## Required output
Write ONE complete markdown document with EXACTLY these sections, in this order:
{sections_list}

Section requirements:
- `## TL;DR`: 2-4 sentence executive summary.
- `## Discussion`: the discussion organized by topic. Where a frame informed a
  point, reference the on-screen content explicitly (e.g. "the billing dashboard
  shown at 14:32").
- `## Decisions`: concrete decisions reached.
- `## Action Items`: a markdown table with columns Owner, Task, Due (leave Due
  blank unless a due date/time was actually stated).
- `## Open Questions`: unresolved questions or follow-ups.
- `## Project Mapping`: which CCR projects/memories this meeting relates to,
  based on your gcc_search/gcc_context results.
- `## Speakers`: a table mapping each SPEAKER_n to a probable real name with the
  evidence for it (e.g. "addressed by name at 14:32"), or "unknown" if there is
  no evidence.

Reference on-screen content wherever a frame informed the summary. Write the
final document as your LAST message, with nothing after it.
"""


def build_commit_prompt(summary_md: str, meta: dict) -> str:
    """Build the prompt for the short CCR write-back session.

    Instructs the session to call ``mcp__ccr__gcc_commit`` exactly once with a
    condensed record of the meeting, then reply with only the commit id.
    """
    title = str(meta.get("title") or "Untitled meeting")
    started_at = str(meta.get("started_at") or "")
    date = started_at[:10] if started_at else "unknown date"
    context_note = str(meta.get("context_note") or "").strip() or "meeting record"

    return f"""You are recording a meeting summary into CCR project memory.

Below is the finished meeting summary. Call `mcp__ccr__gcc_commit` EXACTLY ONCE
with these arguments:
- title: "Meeting: {title} ({date})"
- what: a condensed record combining the TL;DR, the decisions, and the action
  items from the summary below.
- why: "{context_note}"
- files_changed: []
- next_step: the first open action item from the summary, or "" if there are none.

After the commit returns, reply with ONLY the commit id it returned. Do not add
any other text.

## Meeting summary
{summary_md}
"""
