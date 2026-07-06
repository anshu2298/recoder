"""Claude Agent SDK analysis session + CCR write-back (spec §4.2 steps 2-3).

Productionizes the proven Spike C pattern (``spikes/spike_c_sdk.py``): an
unattended SDK session with the CCR MCP server wired into ``mcp_servers`` and
``permission_mode="bypassPermissions"`` so the pipeline never hangs on a
tool-approval prompt.

Public contract (imported verbatim by the pipeline runner):
    analyze(meeting_folder: Path, config) -> None
    commit_to_ccr(meeting_folder: Path, config) -> None

The SDK invocation lives behind an injectable ``session_runner`` callable
(``session_runner(prompt, options) -> str``) so tests never touch the real SDK.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from recoder.analysis import routing
from recoder.analysis.prompts import (
    REQUIRED_SECTIONS,
    build_analysis_prompt,
    build_commit_prompt,
)
from recoder.store import MeetingStore

logger = logging.getLogger(__name__)

# --- tuning constants (config-free, per spec) --------------------------------
SESSION_TIMEOUT_S = 20 * 60  # 20 minutes; wraps the whole SDK session.
ANALYZE_MAX_TURNS = 40  # generous enough for multi-frame Read turns.
COMMIT_MAX_TURNS = 8  # a single gcc_commit call plus reply.
MAX_ATTEMPTS = 3  # transport-level retries.
BACKOFF_BASE_S = 2.0  # exponential backoff base between transport retries.

SessionRunner = Callable[[str, object], str]


class AnalysisError(Exception):
    """Raised when analysis or CCR write-back cannot be completed."""


# --- SDK glue (the only code that imports claude_agent_sdk) ------------------
def _extract_text(message: object) -> str:
    """Collect assistant text from an SDK message (mirrors the spike)."""
    parts: list[str] = []
    content = getattr(message, "content", None)
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
    result = getattr(message, "result", None)
    if isinstance(result, str):
        parts.append(result)
    return "\n".join(parts)


async def _run_query(prompt: str, options: object) -> str:
    from claude_agent_sdk import query

    collected: list[str] = []
    async for message in query(prompt=prompt, options=options):
        text = _extract_text(message)
        if text.strip():
            collected.append(text)
    return "\n".join(collected)


def _default_session_runner(prompt: str, options: object) -> str:
    """Real runner: run one SDK session to completion under the timeout."""

    async def _run_with_timeout() -> str:
        return await asyncio.wait_for(
            _run_query(prompt, options), timeout=SESSION_TIMEOUT_S
        )

    return asyncio.run(_run_with_timeout())


# --- CCR MCP-server wiring (shared with consolidate.py) ----------------------
def mcp_args_for_project(config: object, project_path: str | Path) -> list[str]:
    """Copy ``config.ccr_mcp_args`` but point ``--project`` at ``project_path``."""
    args = list(config.ccr_mcp_args)
    if "--project" in args:
        idx = args.index("--project")
        if idx + 1 < len(args):
            args[idx + 1] = str(project_path)
        else:
            args.append(str(project_path))
    else:
        args += ["--project", str(project_path)]
    return args


def ccr_server_for_project(config: object, project_path: str | Path) -> dict:
    """A stdio MCP-server spec launching the CCR server on ``project_path``."""
    return {
        "type": "stdio",
        "command": config.ccr_mcp_command,
        "args": mcp_args_for_project(config, project_path),
    }


def build_session_options(
    cwd: Path,
    config: object,
    max_turns: int,
    mcp_servers: dict,
    allowed_tools: list[str],
) -> object:
    """Assemble ClaudeAgentOptions from explicit servers/tools (Spike C config)."""
    from claude_agent_sdk import ClaudeAgentOptions

    kwargs: dict[str, object] = {
        "cwd": str(cwd),
        "permission_mode": "bypassPermissions",
        "mcp_servers": mcp_servers,
        "allowed_tools": allowed_tools,
        "max_turns": max_turns,
    }
    model = getattr(config, "analysis_model", None) or getattr(config, "model", None)
    if model:
        kwargs["model"] = model
    return ClaudeAgentOptions(**kwargs)


def _project_path_from_args(config: object) -> str:
    """The recoder store path baked into ``config.ccr_mcp_args`` (for exclusion)."""
    args = list(config.ccr_mcp_args)
    if "--project" in args:
        idx = args.index("--project")
        if idx + 1 < len(args):
            return str(args[idx + 1])
    return ""


def _slugify_project(name: str, used: set[str]) -> str:
    """Lowercase alnum/underscore slug from a project name, deduped in ``used``."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "proj"
    candidate = slug
    n = 2
    while candidate in used:
        candidate = f"{slug}_{n}"
        n += 1
    used.add(candidate)
    return candidate


def _route_mounts(meta: dict, config: object) -> list[tuple[str, routing.RoutedProject]]:
    """Route the meeting to foreign stores; return (slug, RoutedProject) pairs.

    Any registry read/route failure degrades to no mounts (analysis proceeds
    with just the recoder store).
    """
    try:
        entries = routing.load_registry(config.ccr_registry_path)
        routed = routing.route_projects(
            entries,
            str(meta.get("title") or ""),
            str(meta.get("context_note") or ""),
            now=datetime.now(timezone.utc),
            recency_days=getattr(config, "routing_recency_days", 7),
            max_mounts=getattr(config, "routing_max_mounts", 4),
            exclude_paths=(_project_path_from_args(config),),
        )
    except Exception as exc:  # noqa: BLE001 - routing must never break analysis
        logger.warning("project routing failed; mounting recoder store only: %r", exc)
        return []

    used: set[str] = set()
    return [(_slugify_project(rp.name, used), rp) for rp in routed]


def _build_options(
    meeting_folder: Path,
    config: object,
    max_turns: int,
    *,
    mounts: list[tuple[str, routing.RoutedProject]] | None = None,
    mount_write: bool = False,
) -> object:
    """Build ClaudeAgentOptions: the recoder store plus any routed mounts.

    Mounts are read-only during analysis. ``mount_write=True`` (commit stage
    only) additionally allows ``gcc_commit`` on each mounted store so the
    meeting note can be written back into the projects it concerned.
    """
    mcp_servers: dict[str, object] = {
        "ccr": {
            "type": "stdio",
            "command": config.ccr_mcp_command,
            "args": list(config.ccr_mcp_args),
        }
    }
    allowed_tools: list[str] = [
        "Read",
        "Glob",
        "mcp__ccr__gcc_search",
        "mcp__ccr__gcc_context",
        "mcp__ccr__gcc_commit",
    ]
    for slug, rp in mounts or []:
        mcp_servers[f"ccr_{slug}"] = ccr_server_for_project(config, rp.path)
        allowed_tools.append(f"mcp__ccr_{slug}__gcc_search")
        allowed_tools.append(f"mcp__ccr_{slug}__gcc_context")
        if mount_write:
            allowed_tools.append(f"mcp__ccr_{slug}__gcc_commit")

    return build_session_options(
        meeting_folder, config, max_turns, mcp_servers, allowed_tools
    )


# --- helpers ------------------------------------------------------------------
def _run_with_retries(
    session_runner: SessionRunner,
    prompt: str,
    options: object,
    sleep: Callable[[float], None],
) -> str:
    """Call ``session_runner`` with up to MAX_ATTEMPTS on transport failures.

    Transport/SDK exceptions retry with exponential backoff; the last error is
    re-raised as AnalysisError. Validation is the caller's concern, not this.
    """
    last_exc: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            return session_runner(prompt, options)
        except AnalysisError:
            raise
        except Exception as exc:  # noqa: BLE001 - transport layer is opaque
            last_exc = exc
            if attempt + 1 < MAX_ATTEMPTS:
                sleep(BACKOFF_BASE_S * (2**attempt))
    raise AnalysisError(
        f"Claude session failed after {MAX_ATTEMPTS} attempts: {last_exc!r}"
    )


def _extract_summary(text: str) -> str:
    """Slice out the first complete '# Meeting Summary' document.

    Drops any preamble/reasoning before the header. The SDK's final
    ResultMessage repeats the last assistant text, so the document often
    appears twice in the collected output — truncate at a repeated header.
    """
    marker = "# Meeting Summary"
    idx = text.find(marker)
    if idx == -1:
        return text.strip()
    doc = text[idx:]
    repeat = doc.find(marker, len(marker))
    if repeat != -1:
        doc = doc[:repeat]
    return doc.strip()


def _missing_sections(summary: str) -> list[str]:
    return [s for s in REQUIRED_SECTIONS if s not in summary]


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _load_segments(meeting_folder: Path) -> list[dict]:
    transcript_path = meeting_folder / "transcript.json"
    if not transcript_path.exists():
        raise AnalysisError(
            f"transcript.json is missing from {meeting_folder}; "
            "run transcription/diarization before analysis."
        )
    try:
        data = json.loads(transcript_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AnalysisError(f"transcript.json is unreadable: {exc}") from exc
    segments = data.get("segments") if isinstance(data, dict) else None
    if not segments:
        raise AnalysisError(
            "transcript.json contains no segments; an empty transcript "
            "cannot be analyzed."
        )
    return list(segments)


def _load_frame_inventory(meeting_folder: Path) -> list[dict]:
    index_path = meeting_folder / "frames" / "index.jsonl"
    if not index_path.exists():
        return []
    inventory: list[dict] = []
    for line in index_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            inventory.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return inventory


def _duration_from_segments(segments: list[dict]) -> float:
    end = 0.0
    for seg in segments:
        try:
            end = max(end, float(seg.get("end", 0.0)))
        except (TypeError, ValueError):
            continue
    return end


def _build_corrective_prompt(prior: str, missing: list[str]) -> str:
    missing_list = "\n".join(f"  - {s}" for s in missing)
    required_list = "\n".join(f"  - {s}" for s in REQUIRED_SECTIONS)
    return f"""Your previous summary was missing these required sections:
{missing_list}

Re-emit the COMPLETE meeting summary as a single markdown document containing
ALL of these sections, in order, with content:
{required_list}

Your previous response is below for reference — fix it and output the full
corrected document as your last message, nothing after it.

## Previous response
{prior}
"""


# --- public contract ----------------------------------------------------------
def analyze(
    meeting_folder: Path,
    config: object,
    *,
    session_runner: SessionRunner | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Read meeting inputs, run the Claude session, write ``summary.md``.

    Raises AnalysisError on missing/empty transcript, unrecoverable transport
    failure, or a response that still lacks required sections after one
    corrective turn.
    """
    meeting_folder = Path(meeting_folder)
    runner = session_runner or _default_session_runner

    segments = _load_segments(meeting_folder)
    frame_inventory = _load_frame_inventory(meeting_folder)
    duration_s = _duration_from_segments(segments)

    try:
        meta = json.loads((meeting_folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        meta = {}

    from recoder.analysis.prompts import render_transcript

    mounts = _route_mounts(meta, config)
    mounted_projects = [
        {"slug": slug, "name": rp.name, "reason": rp.reason} for slug, rp in mounts
    ]

    transcript_md = render_transcript(segments)
    prompt = build_analysis_prompt(
        meta,
        transcript_md,
        frame_inventory,
        duration_s,
        mounted_projects=mounted_projects,
    )
    options = _build_options(
        meeting_folder, config, ANALYZE_MAX_TURNS, mounts=mounts
    )

    raw = _run_with_retries(runner, prompt, options, sleep)
    summary = _extract_summary(raw)

    missing = _missing_sections(summary)
    if missing:
        # One corrective follow-up turn quoting the missing sections. This is a
        # validation issue, not a transport failure, so no retry loop here.
        corrective = _build_corrective_prompt(summary, missing)
        raw = _run_with_retries(runner, corrective, options, sleep)
        summary = _extract_summary(raw)
        missing = _missing_sections(summary)
        if missing:
            raise AnalysisError(
                "Claude response is missing required sections after a "
                f"corrective turn: {', '.join(missing)}"
            )

    _atomic_write(meeting_folder / "summary.md", summary + "\n")


_COMMIT_ID_RE = re.compile(r"C\d+")
_WRITEBACK_LINE_RE = re.compile(r"^\s*([a-z0-9_]+)\s*:\s*(C\d+)\s*$")


def _parse_commit_reply(reply: str, slugs: set[str]) -> tuple[str | None, dict[str, str]]:
    """Split the commit reply into (recoder commit id, {slug: writeback id}).

    Write-back lines are ``<slug>: C<nnn>`` for a known mounted slug; the
    recoder id is the first bare ``C<nnn>`` found outside those lines.
    """
    writebacks: dict[str, str] = {}
    other_lines: list[str] = []
    for line in reply.splitlines():
        m = _WRITEBACK_LINE_RE.match(line)
        if m and m.group(1) in slugs:
            writebacks[m.group(1)] = m.group(2)
        else:
            other_lines.append(line)
    match = _COMMIT_ID_RE.search("\n".join(other_lines))
    return (match.group(0) if match else None), writebacks


def commit_to_ccr(
    meeting_folder: Path,
    config: object,
    *,
    session_runner: SessionRunner | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Commit the distilled summary to CCR memory; record the id in meta.json.

    Also writes a short "Meeting:" note back into each routed project store the
    meeting concerned (write-back), recording those ids in meta as
    ``ccr_writebacks``. A live Claude Code session in one of those projects
    picks the note up on the user's next prompt via CCR's context injection.

    Raises AnalysisError if ``summary.md`` is missing, the session fails, or the
    reply carries no plausible commit id.
    """
    meeting_folder = Path(meeting_folder)
    runner = session_runner or _default_session_runner

    summary_path = meeting_folder / "summary.md"
    if not summary_path.exists():
        raise AnalysisError(
            f"summary.md is missing from {meeting_folder}; run analyze first."
        )
    summary_md = summary_path.read_text(encoding="utf-8")

    try:
        meta = json.loads((meeting_folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        meta = {}

    mounts = _route_mounts(meta, config)
    mounted_projects = [
        {"slug": slug, "name": rp.name, "reason": rp.reason} for slug, rp in mounts
    ]

    prompt = build_commit_prompt(summary_md, meta, mounted_projects=mounted_projects)
    # Two extra turns per mounted store: its gcc_commit call + result.
    options = _build_options(
        meeting_folder,
        config,
        COMMIT_MAX_TURNS + 2 * len(mounts),
        mounts=mounts,
        mount_write=True,
    )

    reply = _run_with_retries(runner, prompt, options, sleep).strip()

    slugs = {slug for slug, _ in mounts}
    parsed_id, writebacks = _parse_commit_reply(reply, slugs)
    if parsed_id:
        commit_id: str = parsed_id
    elif "commit" in reply.lower() and reply:
        commit_id = reply
    else:
        raise AnalysisError(
            f"CCR commit reply carried no commit id: {reply!r}"
        )

    updates: dict[str, object] = {"ccr_commit": commit_id}
    if writebacks:
        updates["ccr_writebacks"] = writebacks
    store = MeetingStore(config)
    store.load(meeting_folder).update_meta(**updates)
