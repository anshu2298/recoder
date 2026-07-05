"""Prompt assembly for the Claude analysis + CCR commit sessions (spec §4.2).

Pure functions only — no I/O, no SDK. Everything here is a string builder so
the prompts can be snapshot-tested without touching Claude or the filesystem.
"""

from __future__ import annotations

from datetime import date as _date

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


def render_mounted_projects(mounted_projects: list[dict]) -> str:
    """Render the routed foreign-store mounts as an instruction block.

    Each mount exposes ``mcp__ccr_<slug>__gcc_search`` and
    ``mcp__ccr_<slug>__gcc_context`` (read-only). Tells Claude to search the
    relevant stores BEFORE summarizing and cite matching work concretely.
    """
    if not mounted_projects:
        return (
            "No additional project stores were mounted for this meeting. Use the "
            "recoder store's `gcc_search`/`gcc_context` for any relevant history."
        )

    lines = [
        "In addition to the recoder store, these project memory stores are "
        "mounted READ-ONLY for this meeting (they were selected because they "
        "match the meeting topic or are actively worked on):",
        "",
    ]
    for proj in mounted_projects:
        slug = str(proj.get("slug") or "")
        name = str(proj.get("name") or "")
        reason = str(proj.get("reason") or "")
        lines.append(
            f"- **{name}** ({reason}) — search with "
            f"`mcp__ccr_{slug}__gcc_search`, read context with "
            f"`mcp__ccr_{slug}__gcc_context`."
        )
    lines += [
        "",
        "BEFORE you write the summary, search the relevant project stores above "
        "for work related to what was discussed (recent commits, decisions, open "
        "threads). When the meeting clearly relates to that work, cite it "
        "concretely in the summary — e.g. \"relates to the retry-queue refactor "
        "(C078, sherpa-linkedin-enrich)\". Do NOT write to these stores; they are "
        "read-only.",
    ]
    return "\n".join(lines)


def build_analysis_prompt(
    meta: dict,
    transcript_md: str,
    frame_inventory: list[dict],
    duration_s: float,
    mounted_projects: list[dict] | None = None,
) -> str:
    """Build the full analysis prompt for one meeting.

    Frames are delivered via the filesystem: the session's cwd is the meeting
    folder and this prompt lists the ``frames/`` inventory; Claude reads the
    images it deems relevant with the Read tool. ``mounted_projects`` lists the
    foreign CCR stores routed into this session (see :mod:`recoder.analysis.routing`).
    """
    title = str(meta.get("title") or "Untitled meeting")
    context_note = str(meta.get("context_note") or "").strip() or "(none provided)"
    started_at = str(meta.get("started_at") or "unknown")
    duration = _fmt_duration(duration_s)
    frame_table = render_frame_table(frame_inventory)
    mounts_block = render_mounted_projects(mounted_projects or [])

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
BEFORE you write the summary, use `gcc_search` and `gcc_context` on the recoder
store to pull related project memory. Search for the people, projects, and topics
named in the transcript so your summary connects this meeting to the user's
existing work.

### Project memory available
{mounts_block}

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
- `## Project Mapping`: which CCR project store(s) each discussion topic maps
  to, naming the specific store (e.g. "billing -> sherpa-linkedin-enrich") and
  citing concrete commits/decisions where your searches found them.
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


def build_consolidation_prompt(source_name: str, target_name: str) -> str:
    """Prompt for distilling a worktree store into its parent store (Piece B).

    Two stores are mounted: ``ccr_source`` (READ-ONLY: gcc_search + gcc_context)
    and ``ccr_target`` (gcc_search + gcc_context + gcc_commit). The session reads
    the source's full history and writes 3-8 milestone commits onto the target.
    """
    today = _date.today().isoformat()
    return f"""You are consolidating one CCR project-memory store into another so a
fragmented worktree's history is preserved in its parent project before the
worktree store is archived.

## Mounted stores
- SOURCE = "{source_name}" — READ-ONLY. Read it with `mcp__ccr_source__gcc_search`
  and `mcp__ccr_source__gcc_context`. Do NOT write to it.
- TARGET = "{target_name}" — write here with `mcp__ccr_target__gcc_commit`
  (you may also `mcp__ccr_target__gcc_search` / `mcp__ccr_target__gcc_context`).

## Step 1 — read the source's full history
Call `mcp__ccr_source__gcc_context` at a deep level (level=4, and level=5 with
search terms for specific threads) with a generous `result_limit` and
`include_summaries=true` to page through the ENTIRE source history — every
milestone, decision, and dead end. Use `mcp__ccr_source__gcc_search` to fill in
gaps around notable topics. Do not summarize until you have surveyed it all.

## Step 2 — distill into milestone commits on the target
Write between 3 and 8 `mcp__ccr_target__gcc_commit` calls. Each commit must cover
one coherent theme (a feature shipped, a cluster of key decisions, a pattern or
convention learned, or a dead end worth remembering — NOT one-per-original-commit).
For each commit:
- title: prefix with "[from {source_name}] " then a concise theme title.
- what: the substance — what was built/decided/learned for that theme.
- why: include the consolidation provenance — "consolidated from {source_name} on
  {today}" plus the date range of the source work it covers.
- files_changed: [] (this is a memory consolidation, not a code change).
- next_step: a genuinely open thread from the source if one exists, else "".

## Step 3 — reply
After all commits succeed, reply with ONLY the list of target commit ids you
created (e.g. "C081, C082, C083"). Nothing else.
"""
