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
import os
import re
import time
from pathlib import Path
from typing import Callable

from recoder.analysis.prompts import (
    REQUIRED_SECTIONS,
    build_analysis_prompt,
    build_commit_prompt,
)
from recoder.store import MeetingStore

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


def _build_options(meeting_folder: Path, config: object, max_turns: int) -> object:
    """Build ClaudeAgentOptions mirroring Spike C's proven config."""
    from claude_agent_sdk import ClaudeAgentOptions

    kwargs: dict[str, object] = {
        "cwd": str(meeting_folder),
        "permission_mode": "bypassPermissions",
        "mcp_servers": {
            "ccr": {
                "type": "stdio",
                "command": config.ccr_mcp_command,
                "args": list(config.ccr_mcp_args),
            }
        },
        "allowed_tools": [
            "Read",
            "Glob",
            "mcp__ccr__gcc_search",
            "mcp__ccr__gcc_context",
            "mcp__ccr__gcc_commit",
        ],
        "max_turns": max_turns,
    }
    model = getattr(config, "analysis_model", None) or getattr(config, "model", None)
    if model:
        kwargs["model"] = model
    return ClaudeAgentOptions(**kwargs)


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
    """Slice from the first '# Meeting Summary' header to the end.

    Drops any preamble/reasoning the model emitted before the document.
    """
    idx = text.find("# Meeting Summary")
    if idx == -1:
        return text.strip()
    return text[idx:].strip()


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

    transcript_md = render_transcript(segments)
    prompt = build_analysis_prompt(meta, transcript_md, frame_inventory, duration_s)
    options = _build_options(meeting_folder, config, ANALYZE_MAX_TURNS)

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


def commit_to_ccr(
    meeting_folder: Path,
    config: object,
    *,
    session_runner: SessionRunner | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Commit the distilled summary to CCR memory; record the id in meta.json.

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

    prompt = build_commit_prompt(summary_md, meta)
    options = _build_options(meeting_folder, config, COMMIT_MAX_TURNS)

    reply = _run_with_retries(runner, prompt, options, sleep).strip()

    match = _COMMIT_ID_RE.search(reply)
    if match:
        commit_id: str = match.group(0)
    elif "commit" in reply.lower() and reply:
        commit_id = reply
    else:
        raise AnalysisError(
            f"CCR commit reply carried no commit id: {reply!r}"
        )

    store = MeetingStore(config)
    store.load(meeting_folder).update_meta(ccr_commit=commit_id)
