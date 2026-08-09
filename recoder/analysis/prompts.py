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
    "## Action Items JSON",
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


def render_frame_table(
    frame_inventory: list[dict], evidence: list[dict] | None = None
) -> str:
    """Render the frames inventory as a markdown table.

    Columns: filename, transcript offset, nearby speech (from the evidence
    join, when available), window title, source, fallback flag. The fallback
    flag surfaces the occlusion limitation (spec §4.1): a fullscreen fallback
    grab may show unrelated desktop content rather than the meeting.
    """
    by_file: dict[str, dict] = {
        str(e.get("file")): e for e in (evidence or [])
    }
    header = (
        "| Filename | Offset | Nearby speech | Window title | Source | Fallback |\n"
        "| --- | --- | --- | --- | --- | --- |"
    )
    if not frame_inventory:
        return header + "\n| (no frames captured) | | | | | |"

    rows: list[str] = []
    for entry in frame_inventory:
        filename = str(entry.get("file") or entry.get("filename") or "").strip()
        ev = by_file.get(filename, {})
        mmss = str(ev.get("mmss") or "")
        speech = str(ev.get("speech") or "").replace("|", "\\|")
        title = str(entry.get("window_title") or "").strip().replace("|", "\\|")
        source = str(entry.get("source") or "window")
        if entry.get("presenting"):
            source += " (screen-share active)"
        fallback = bool(entry.get("fallback_fullscreen", False))
        flag = "yes" if fallback else "no"
        rows.append(
            f"| {filename} | {mmss} | {speech} | {title} | {source} | {flag} |"
        )
    return header + "\n" + "\n".join(rows)


def render_sheet_table(sheets: list[dict]) -> str:
    """Render the contact-sheet index as a markdown table."""
    header = "| Sheet | Covers | Frames |\n| --- | --- | --- |"
    rows = [
        f"| frames/sheets/{s.get('file')} | {s.get('range') or '?'} "
        f"| {len(s.get('frames') or [])} |"
        for s in sheets
    ]
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
    register_md: str = "",
    evidence: list[dict] | None = None,
    sheets: list[dict] | None = None,
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
    frame_table = render_frame_table(frame_inventory, evidence)
    mounts_block = render_mounted_projects(mounted_projects or [])

    sheets_block = ""
    if sheets:
        sheets_block = f"""
### Contact sheets — READ THESE FIRST
Every frame is montaged into 4x4 contact sheets under `frames/sheets/`; each
tile is stamped `[MM:SS] #seq` (transcript offset + frame number). Read ALL
the sheets first — they are few and small — so you can SEE the whole meeting
before choosing frames. Then open at full resolution ONLY the frames whose
tile shows something worth reading closely (slides, documents, code, demos).
Do not open full-res frames that the sheet already shows to be talking heads
or off-topic desktop content.

{render_sheet_table(sheets)}
"""

    register_block = ""
    if register_md.strip():
        register_block = f"""
### Worktree register (live rollup)
The user's active project is split across several worktrees. This register is
a machine-collated snapshot of EVERY tree's current focus, next step, and
recent milestones — including trees NOT mounted above. Use it to place the
discussion in the whole family's context (e.g. work discussed here may extend
or conflict with another tree's focus). It is read-only derived state.

{register_md.strip()}
"""

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

Frames whose Source is `monitor<N>` were captured from the user's OTHER
screens while a screen-share was active — when the user was presenting, these
show the content being presented (slides, demos, code) and are usually the
most informative frames. A monitor frame can still be an unshared side screen,
so ignore any that are clearly unrelated to the discussion.
{sheets_block}
### Frame inventory
The "Offset" column is the frame's position on the transcript timeline and
"Nearby speech" is what was being said around that moment — use them to pick
frames that coincide with the discussion points you are summarizing.

{frame_table}

## Project memory (CCR)
BEFORE you write the summary, use `gcc_search` and `gcc_context` on the recoder
store to pull related project memory. Search for the people, projects, and topics
named in the transcript so your summary connects this meeting to the user's
existing work.

### Project memory available
{mounts_block}
{register_block}
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
- `## Action Items JSON`: the SAME action items as the table above, but as ONE
  fenced ```json block — an object {{"items": [...]}} where each item is:
    {{"id": "ai-1",                     // sequential
      "owner": "...", "task": "...", "due": "",
      "kind": "build" | "coordination" | "other",
        // "build" ONLY for concrete engineering work on the user's own
        // projects (a feature, fix, migration, prototype); meetings,
        // emails, walkthroughs are "coordination".
      "project": "<name>" | null,
        // for build items: the CCR project store or worktree-register tree
        // this work belongs to (e.g. "linkedin-enrich"), else null
      "evidence": {{"segments": [{{"t": "MM:SS", "quote": "<short verbatim>"}}],
                   "frames": ["<filename from the inventory>"]}},
        // the transcript moments (and any frames) that establish this item
      "state_relation": "..."
        // one sentence on how it relates to the project's CURRENT state
        // from the memory you searched (extends/conflicts/unblocks what),
        // or "" if no relation was found
    }}
  Valid JSON only inside the fence — no comments, no trailing commas.

Reference on-screen content wherever a frame informed the summary. Write the
final document as your LAST message, with nothing after it.
"""


def render_participants(participants: list[str], host_email: str = "") -> str:
    """Render the speaker roster of an ingested meeting."""
    if not participants:
        return "(the recording named no speakers)"
    lines = [f"- {name}" for name in participants]
    if host_email:
        lines.append(f"\nThe recording was made from the account: {host_email}")
    return "\n".join(lines)


def build_ingested_analysis_prompt(
    meta: dict,
    transcript_md: str,
    frame_inventory: list[dict],
    duration_s: float,
    mounted_projects: list[dict] | None = None,
    register_md: str = "",
    evidence: list[dict] | None = None,
    sheets: list[dict] | None = None,
) -> str:
    """Build the analysis prompt for a meeting Recoder did not record (§4.6).

    Deliberately NOT a flag on :func:`build_analysis_prompt`. An ingested
    meeting differs in kind, not degree:

    * **Speakers are real names**, not ``Me``/``SPEAKER_n``. There is no
      diarization to second-guess, and no microphone marking which voice is
      the user's — so the prompt must work out the user's involvement from
      the content, rather than being told.
    * **There is no context note.** The user was not there to write one, so
      the CCR memory and the worktree register carry the whole burden of
      grounding.
    * **Frames may legitimately not exist.** A notetaker records the call's
      gallery view, so unless somebody shared a screen there is nothing to
      show, and their absence is information rather than a failure.

    The output contract is identical to the captured-meeting prompt (same
    REQUIRED_SECTIONS, same Action Items JSON), so every downstream consumer —
    validation, action items, specs, the UI — is unchanged.
    """
    title = str(meta.get("title") or "Untitled meeting")
    started_at = str(meta.get("started_at") or "unknown")
    source_url = str(meta.get("source_url") or "")
    host_email = str(meta.get("host_email") or "")
    participants = [str(p) for p in (meta.get("participants") or [])]
    frames_status = str(meta.get("frames_status") or "")
    frames_reason = str(meta.get("frames_reason") or "")
    duration = _fmt_duration(duration_s)
    mounts_block = render_mounted_projects(mounted_projects or [])

    if frame_inventory:
        sheets_block = ""
        if sheets:
            sheets_block = f"""
### Contact sheets — READ THESE FIRST
Every frame is montaged into 4x4 contact sheets under `frames/sheets/`, each
tile stamped `[MM:SS] #seq`. Read ALL the sheets first, then open at full
resolution ONLY the frames showing something worth reading closely.

{render_sheet_table(sheets)}
"""
        frames_block = f"""## On-screen frames
Somebody shared their screen during this meeting, and the recording is the ONLY
copy of what was on it. Frames were extracted from the recording roughly every
20 seconds and de-duplicated; your working directory is the meeting folder.

Because these come from the call recording rather than the user's own screen,
a frame shows the shared content as everyone in the meeting saw it, usually
with participant webcam tiles along one edge. Read the shared content closely —
dashboards, code, documents and designs discussed here are frequently the
substance of the meeting, and the transcript alone will refer to them only as
"this" and "here".
{sheets_block}
### Frame inventory
"Offset" places the frame on the transcript timeline and "Nearby speech" is
what was being said at that moment — use them to tie what is on screen to what
was being discussed.

{render_frame_table(frame_inventory, evidence)}
"""
    else:
        explanation = {
            "no-screen-content": (
                "Nobody shared a screen, so the recording contains only "
                "webcam video and there is nothing on-screen to read."
            ),
            "unavailable": "The recording's video could not be read.",
            "failed": "Frame extraction failed.",
            "skipped": "Frame extraction was skipped for this import.",
        }.get(frames_status, "No frames were extracted.")
        frames_block = f"""## On-screen frames
None. {explanation}{f' ({frames_reason})' if frames_reason else ''}

This is expected, not a defect — work entirely from the transcript, and do not
speculate about visual content you cannot see.
"""

    register_block = ""
    if register_md.strip():
        register_block = f"""
### Worktree register (live rollup)
The user's active project spans several worktrees. This machine-collated
snapshot gives every tree's current focus, next step and recent changes. The
user did not write a context note for this meeting — they were not necessarily
in it — so this register and the CCR stores are your ONLY grounding for what
the user is actually working on. Use it to judge which discussion points touch
their work.

{register_md.strip()}
"""

    sections_list = "\n".join(f"  - {s}" for s in REQUIRED_SECTIONS)

    return f"""You are analyzing a meeting that was recorded by a third-party
notetaker and imported. The user did NOT capture this meeting themselves.

## Meeting metadata
- Title: {title}
- Started at: {started_at}
- Duration: {duration}
- Imported from: {source_url or "a shared recording link"}

## Participants (as labelled by the recording)
{render_participants(participants, host_email)}

## Transcript
Segments are rendered as `[MM:SS] Speaker: text`. These are REAL names supplied
by the notetaker, not diarization guesses — you can trust who said what, and
you do not need to infer speaker identities.

{transcript_md}

## Your reader, and what they need from this
The user may have been in this meeting or may not have been. **Do not treat
that as the question, and do not hedge on it.** Summarize the meeting
completely and on its own terms either way: everything discussed, decided,
disputed and left open, as a full record for someone who needs to know what
happened.

Then, on top of that complete record, do the thing that only you can do here:
work out what lands on the user. They were not necessarily present to take
notes, push back, or accept the work assigned to them, so anything aimed at
them is arriving late and second-hand.

Establish their involvement from the evidence rather than assuming it. Search
the CCR memory below for the people, projects and topics named in the
transcript; the register tells you which worktrees they own and what they are
mid-way through. From that, work out which of the named participants is the
user, if any, and treat these as first-class findings wherever they occur:

- decisions that change, block, or contradict work the user has in flight
- commitments made **on the user's behalf**, or work assigned to them in
  absentia — call these out explicitly, naming who assigned them and when
- claims made about the user's work, its status, or its quality
- things they would obviously have objected to or corrected had they been there
- context they now need in order to act on any of the above

If the meeting genuinely has no bearing on the user's work, say so plainly in
the TL;DR rather than manufacturing relevance.

{frames_block}
## Project memory (CCR)
BEFORE writing the summary, use `gcc_search` and `gcc_context` on the recoder
store to pull related project memory for the people, projects and topics named
in the transcript. This is how you establish what the discussion actually
refers to — you have no context note to lean on.

### Project memory available
{mounts_block}
{register_block}
## Required output
Write ONE complete markdown document with EXACTLY these sections, in this order:
{sections_list}

Section requirements:
- `## TL;DR`: 2-4 sentences. Say what the meeting was about AND, in one
  sentence, what it means for the user (including "nothing directly" when that
  is the honest answer).
- `## Discussion`: the discussion organized by topic, as a complete record.
- `## Decisions`: concrete decisions reached. For each, note whether it affects
  work the user owns, citing the project or worktree.
- `## Action Items`: a markdown table with columns Owner, Task, Due (leave Due
  blank unless a due date/time was actually stated). Include items assigned to
  the user in their absence, and items owned by others that the user is
  waiting on.
- `## Open Questions`: unresolved questions, plus anything the user would
  likely want to challenge or correct given what the memory says about their
  work.
- `## Project Mapping`: which CCR project store(s) or worktrees each topic maps
  to, citing concrete commits or decisions your searches found.
- `## Speakers`: a table of each named participant with their apparent role and
  what they were responsible for in this discussion. Mark which one is the user
  (or state that they do not appear to have spoken), with your evidence.
- `## Action Items JSON`: the SAME action items as the table above, as ONE
  fenced ```json block — an object {{"items": [...]}} where each item is:
    {{"id": "ai-1",                     // sequential
      "owner": "...", "task": "...", "due": "",
      "kind": "build" | "coordination" | "other",
        // "build" ONLY for concrete engineering work on the user's own
        // projects; meetings, emails, walkthroughs are "coordination".
      "project": "<name>" | null,
        // for build items: the CCR project store or worktree-register tree
        // this work belongs to, else null
      "evidence": {{"segments": [{{"t": "MM:SS", "quote": "<short verbatim>"}}],
                   "frames": ["<filename from the inventory>"]}},
      "state_relation": "..."
        // one sentence on how it relates to that project's CURRENT state from
        // the memory you searched (extends/conflicts/unblocks what), or "" if
        // no relation was found
    }}
  Valid JSON only inside the fence — no comments, no trailing commas.

Write the final document as your LAST message, with nothing after it.
"""


def build_commit_prompt(
    summary_md: str, meta: dict, mounted_projects: list[dict] | None = None
) -> str:
    """Build the prompt for the short CCR write-back session.

    Always: one ``mcp__ccr__gcc_commit`` recording the meeting in the recoder
    store. When ``mounted_projects`` routed foreign stores are mounted, also
    instructs a per-project write-back: a short "Meeting:" note committed into
    each project store the meeting actually concerned, so the user's next
    prompt in a live Claude Code session inside that project picks it up via
    CCR's context injection.
    """
    title = str(meta.get("title") or "Untitled meeting")
    started_at = str(meta.get("started_at") or "")
    date = started_at[:10] if started_at else "unknown date"
    context_note = str(meta.get("context_note") or "").strip() or "meeting record"

    base = f"""You are recording a meeting summary into CCR project memory.

Below is the finished meeting summary. Call `mcp__ccr__gcc_commit` EXACTLY ONCE
with these arguments:
- title: "Meeting: {title} ({date})"
- what: a condensed record combining the TL;DR, the decisions, and the action
  items from the summary below.
- why: "{context_note}"
- files_changed: []
- next_step: the first open action item from the summary, or "" if there are none.
"""

    if not mounted_projects:
        return base + f"""
After the commit returns, reply with ONLY the commit id it returned. Do not add
any other text.

## Meeting summary
{summary_md}
"""

    proj_lines = "\n".join(
        f"- **{p.get('name')}** ({p.get('reason')}) — commit with "
        f"`mcp__ccr_{p.get('slug')}__gcc_commit`."
        for p in mounted_projects
    )
    return base + f"""
## Project write-back
These project memory stores are ALSO mounted, writable, because the meeting was
routed to them:

{proj_lines}

AFTER the recoder commit, look at the summary's `## Project Mapping` and
`## Action Items` sections. For EACH mounted project the meeting genuinely
concerned, call that project's `gcc_commit` ONCE with:
- title: "Meeting: {title} ({date})"
- what: ONLY the parts relevant to that project — the decisions that affect it
  and its action items. Keep it short; this lands in a coding session's context.
- why: "meeting write-back: {context_note}"
- files_changed: []
- next_step: that project's first open action item, or "".

Skip any mounted project the meeting did not actually concern — an irrelevant
note pollutes that project's memory.

## Reply format
Reply with the recoder commit id on the FIRST line by itself. Then one line per
project write-back you made, in exactly the form `<slug>: <commit id>`, e.g.:

    C042
    sherpa: C481
    billing_service: C102

No other text.

## Meeting summary
{summary_md}
"""


def build_consolidation_prompt(
    source_name: str,
    target_name: str,
    since_commit_id: str | None = None,
) -> str:
    """Prompt for the incremental checkpoint sync of a worktree store (Piece B).

    Two stores are mounted: ``ccr_source`` (READ-ONLY: gcc_search + gcc_context)
    and ``ccr_target`` (gcc_search + gcc_context + gcc_commit). This is an
    incremental sync of a *living* store: only source commits NEWER than
    ``since_commit_id`` are distilled onto the target. When ``since_commit_id`` is
    None this is the first sync and the whole history is in scope.

    The reply must end with a ``HIGHEST_SOURCE_COMMIT: C<nnn>`` marker line so the
    caller can advance the per-source watermark. When ``since_commit_id`` is set
    and there are no newer commits, the session replies ``NO_NEW_COMMITS since
    <id>`` and writes nothing.
    """
    today = _date.today().isoformat()

    if since_commit_id:
        scope = f"""## Step 1 — read ONLY the new source commits
This is an incremental checkpoint sync. This source has already been consolidated
up to commit {since_commit_id}. Examine ONLY source commits whose id is GREATER
than {since_commit_id}. Call `mcp__ccr_source__gcc_context` at level=4 (and
level=5 with search terms for specific threads) with a generous `result_limit`
and `include_summaries=true`; commit ids (e.g. "C047") are shown at these levels.
Ignore every commit with an id at or below {since_commit_id}.

If there are NO source commits newer than {since_commit_id}, do NOT call
`mcp__ccr_target__gcc_commit` at all. Reply with EXACTLY this single line and
nothing else:

    NO_NEW_COMMITS since {since_commit_id}
"""
    else:
        scope = """## Step 1 — read the source's full history
This is the FIRST consolidation of this source, so its entire history is in
scope. Call `mcp__ccr_source__gcc_context` at a deep level (level=4, and level=5
with search terms for specific threads) with a generous `result_limit` and
`include_summaries=true` to page through the ENTIRE source history — every
milestone, decision, and dead end. Commit ids (e.g. "C047") are shown at these
levels. Use `mcp__ccr_source__gcc_search` to fill in gaps around notable topics.
Do not summarize until you have surveyed it all.
"""

    return f"""You are incrementally syncing one CCR project-memory store into another
so a living worktree's newest work is checkpointed into its parent project. The
source store stays alive and untouched; you only distill its NEW commits here.

## Mounted stores
- SOURCE = "{source_name}" — READ-ONLY. Read it with `mcp__ccr_source__gcc_search`
  and `mcp__ccr_source__gcc_context`. Do NOT write to it.
- TARGET = "{target_name}" — write here with `mcp__ccr_target__gcc_commit`
  (you may also `mcp__ccr_target__gcc_search` / `mcp__ccr_target__gcc_context`).

{scope}
## Step 2 — distill the new work into milestone commits on the target
Write between 1 and 5 `mcp__ccr_target__gcc_commit` calls covering ONLY the new
source work in scope. Each commit must cover one coherent theme (a feature
shipped, a cluster of key decisions, a pattern or convention learned, or a dead
end worth remembering — NOT one-per-original-commit). For each commit:
- title: prefix with "[from {source_name}] " then a concise theme title.
- what: the substance — what was built/decided/learned for that theme.
- why: include the consolidation provenance — "consolidated from {source_name} on
  {today}" plus the date range of the source work it covers.
- files_changed: [] (this is a memory consolidation, not a code change).
- next_step: a genuinely open thread from the source if one exists, else "".

## Step 3 — reply
After all commits succeed, reply with the list of target commit ids you created
(e.g. "C081, C082, C083"), and END your reply with a line reporting the HIGHEST
source commit id you examined, in EXACTLY this format (nothing after it):

    HIGHEST_SOURCE_COMMIT: C<nnn>
"""
