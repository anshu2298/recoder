"""On-demand build specs from meeting action items (spec-per-todo).

An action item of kind "build" can be expanded — when the user clicks it —
into a full build spec: a fresh Claude session (same subscription auth as
analysis) grounded in three things:

  * the item's meeting evidence (the transcript around its cited moments,
    the finished summary);
  * the CURRENT state of the target project's CCR store, mounted live —
    which may have moved since the meeting;
  * the worktree register (cross-tree context).

Specs are deliberately NOT generated at analysis time: most action items are
never built, and a spec written at meeting time would be stale by click time.

Artifacts live under ``<meeting>/specs/``:
  * ``<item-id>.md``      — the spec document (done)
  * ``<item-id>.json``    — metadata: generated_at, CCR write-back commit id
  * ``<item-id>.running`` — pid marker while a generation session is live
  * ``<item-id>.err``     — last failure message (cleared on success)

The spec is also committed into the target project's CCR store as a
``[spec] ...`` note, so the next meeting that touches that project sees it.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from recoder.analysis.action_items import extract_action_items
from recoder.analysis.session import (
    AnalysisError,
    SessionRunner,
    _atomic_write,
    _default_session_runner,
    _run_with_retries,
    build_session_options,
    ccr_server_for_project,
)

__all__ = [
    "load_action_items",
    "generate_spec",
    "spec_status",
    "SPEC_MARKER",
]

import time

SPEC_MAX_TURNS = 24
SPEC_MARKER = "# Build Spec"
_EXCERPT_WINDOW_S = 60.0


# --- action-item loading (JSON first, table fallback) -------------------------
def load_action_items(meeting_folder: Path | str) -> list[dict]:
    """Items from ``action-items.json``; fallback: the summary's table.

    Table-fallback items get synthetic ids and ``kind: "other"`` (no evidence,
    no project), so pre-JSON meetings still list their todos.
    """
    folder = Path(meeting_folder)
    path = folder / "action-items.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data.get("items")
        if isinstance(items, list):
            return items
    except (OSError, json.JSONDecodeError):
        pass

    try:
        summary = (folder / "summary.md").read_text(encoding="utf-8")
    except OSError:
        return []
    fallback = []
    for i, row in enumerate(extract_action_items(summary), 1):
        fallback.append(
            {
                "id": f"ai-{i}",
                "owner": row.get("owner", ""),
                "task": row.get("task", ""),
                "due": row.get("due", ""),
                "kind": "other",
                "project": None,
                "evidence": {"segments": [], "frames": []},
                "state_relation": "",
            }
        )
    return fallback


def _find_item(meeting_folder: Path, item_id: str) -> dict:
    for item in load_action_items(meeting_folder):
        if item.get("id") == item_id:
            return item
    raise AnalysisError(f"no action item {item_id!r} in {meeting_folder}")


# --- target store resolution --------------------------------------------------
def _mmss_to_s(stamp: str) -> float | None:
    m = re.fullmatch(r"(\d+):(\d{2})", stamp.strip())
    if not m:
        return None
    return int(m.group(1)) * 60.0 + int(m.group(2))


def resolve_target_store(config, project: str | None) -> Path | None:
    """Map an item's ``project`` name to a CCR store path.

    Matched (case-insensitive, punctuation-tolerant) against the register
    trees first — the tree's most recently active existing store wins — then
    against store directory basenames. None when nothing matches; the spec is
    still generated, just without a write-back target.
    """
    if not project:
        return None

    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", s.lower())

    wanted = norm(str(project))
    if not wanted:
        return None

    try:
        from recoder.analysis.register import read_store

        for name, spec_ in (config.register_trees or {}).items():
            stores = spec_.get("stores") if isinstance(spec_, dict) else None
            if not stores:
                continue
            hit = wanted in norm(name) or norm(name) in wanted
            if not hit:
                hit = any(norm(Path(s).name) == wanted for s in stores)
            if not hit:
                continue
            states = [read_store(s) for s in stores]
            live = [s for s in states if s.exists]
            if not live:
                continue
            lead = max(live, key=lambda s: s.last_active or "")
            return lead.path
    except Exception:  # noqa: BLE001 - resolution is best-effort
        pass
    return None


# --- prompt -------------------------------------------------------------------
def _transcript_excerpt(meeting_folder: Path, item: dict) -> str:
    """[MM:SS] Speaker: text lines around the item's evidence timestamps."""
    try:
        data = json.loads(
            (meeting_folder / "transcript.json").read_text(encoding="utf-8")
        )
        segments = data.get("segments") or []
    except (OSError, json.JSONDecodeError):
        return ""

    stamps = [
        _mmss_to_s(str(s.get("t") or ""))
        for s in (item.get("evidence") or {}).get("segments") or []
    ]
    stamps = [s for s in stamps if s is not None]
    if not stamps:
        return ""

    from recoder.analysis.prompts import render_transcript

    picked = [
        seg
        for seg in segments
        if any(
            abs(float(seg.get("start", 0.0)) - t) <= _EXCERPT_WINDOW_S
            for t in stamps
        )
    ]
    return render_transcript(picked)


def build_spec_prompt(
    item: dict,
    meta: dict,
    transcript_excerpt: str,
    summary_md: str,
    register_md: str,
    target: Path | None,
) -> str:
    title = str(meta.get("title") or "Untitled meeting")
    date = str(meta.get("started_at") or "")[:10]
    evidence = item.get("evidence") or {}
    quotes = "\n".join(
        f"- [{s.get('t')}] {s.get('quote')}"
        for s in evidence.get("segments") or []
    ) or "(none recorded)"

    register_block = ""
    if register_md.strip():
        register_block = f"## Worktree register (live)\n{register_md.strip()}\n"

    if target is not None:
        store_block = f"""## Target project store (LIVE, mounted as `target`)
The store for **{item.get('project')}** is mounted at {target}. BEFORE writing
the spec, call `mcp__target__gcc_context` (level 2-3) and `mcp__target__gcc_search`
for the topics in the task. The project may have MOVED since the meeting — the
spec must be grounded in the store's CURRENT state, and must say explicitly
where it extends, conflicts with, or is already partially done in that state.

AFTER the spec document is complete, call `mcp__target__gcc_commit` EXACTLY ONCE:
- title: "[spec] {str(item.get('task'))[:80]}"
- what: a 5-10 line distillation of the spec (goal, approach, acceptance)
- why: "spec generated from meeting '{title}' ({date})"
- files_changed: []
- next_step: the first implementation step from the spec
"""
    else:
        store_block = """## Target project store
No CCR store could be resolved for this item's project — write the spec from
the meeting evidence alone and skip any memory commit.
"""

    return f"""You are expanding a meeting action item into a full build spec.

## The action item
- Task: {item.get('task')}
- Owner: {item.get('owner') or 'unassigned'}
- Kind: {item.get('kind')}
- Project: {item.get('project') or 'unknown'}
- Relation to project state at meeting time: {item.get('state_relation') or '(none recorded)'}

## Meeting evidence
From "{title}" ({date}). Verbatim quotes that established this item:
{quotes}

### Transcript around those moments
{transcript_excerpt or '(no excerpt available)'}

### Full meeting summary
{summary_md}

{register_block}
{store_block}
## Required output
Write ONE markdown document, as your LAST message, with EXACTLY this structure:

{SPEC_MARKER}: <short imperative title>

## Goal
What is being built and why (2-4 sentences, grounded in the meeting evidence).

## Current state
What already exists in the project that this touches — cite CCR commits/ids
you actually found. State clearly if the item appears already done or
overtaken by later work.

## Approach
The proposed implementation, concrete enough to start from. Numbered steps.

## Files & areas likely touched
Best-effort list from the project memory (module/path names, not guesses
presented as certainty).

## Acceptance criteria
Verifiable statements of done.

## Open questions
Decisions the owner must make before or during the build.
"""


# --- status + generation ------------------------------------------------------
def _specs_dir(meeting_folder: Path) -> Path:
    return Path(meeting_folder) / "specs"


def spec_status(meeting_folder: Path | str, item_id: str) -> dict:
    """One of ``done`` / ``running`` / ``error`` / ``none`` (+ details)."""
    specs = _specs_dir(Path(meeting_folder))
    md = specs / f"{item_id}.md"
    running = specs / f"{item_id}.running"
    err = specs / f"{item_id}.err"

    if md.exists():
        meta = {}
        try:
            meta = json.loads(
                (specs / f"{item_id}.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            pass
        return {"status": "done", "meta": meta}
    if running.exists():
        try:
            pid = int(running.read_text(encoding="utf-8").strip())
            import psutil

            if psutil.pid_exists(pid):
                return {"status": "running"}
        except (OSError, ValueError):
            pass
        running.unlink(missing_ok=True)  # stale marker from a dead run
    if err.exists():
        try:
            return {
                "status": "error",
                "error": err.read_text(encoding="utf-8").strip(),
            }
        except OSError:
            return {"status": "error", "error": "unknown"}
    return {"status": "none"}


def generate_spec(
    meeting_folder: Path | str,
    item_id: str,
    config,
    *,
    session_runner: SessionRunner | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Path:
    """Run the spec session for one item; returns the written spec path."""
    meeting_folder = Path(meeting_folder)
    runner = session_runner or _default_session_runner
    specs = _specs_dir(meeting_folder)
    specs.mkdir(exist_ok=True)
    running = specs / f"{item_id}.running"
    err_path = specs / f"{item_id}.err"
    running.write_text(str(os.getpid()), encoding="utf-8")

    try:
        item = _find_item(meeting_folder, item_id)
        try:
            meta = json.loads(
                (meeting_folder / "meta.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            meta = {}
        try:
            summary_md = (meeting_folder / "summary.md").read_text(
                encoding="utf-8"
            )
        except OSError:
            summary_md = ""

        register_md = ""
        try:
            from recoder.analysis.register import (
                build_register,
                render_register_md,
            )

            register_md = render_register_md(build_register(config))
        except Exception:  # noqa: BLE001 - best-effort context
            register_md = ""

        target = resolve_target_store(config, item.get("project"))

        prompt = build_spec_prompt(
            item,
            meta,
            _transcript_excerpt(meeting_folder, item),
            summary_md,
            register_md,
            target,
        )

        mcp_servers: dict[str, object] = {}
        allowed_tools = ["Read", "Glob"]
        if target is not None:
            mcp_servers["target"] = ccr_server_for_project(config, target)
            allowed_tools += [
                "mcp__target__gcc_search",
                "mcp__target__gcc_context",
                "mcp__target__gcc_commit",
            ]
        options = build_session_options(
            meeting_folder, config, SPEC_MAX_TURNS, mcp_servers, allowed_tools
        )

        raw = _run_with_retries(runner, prompt, options, sleep)
        idx = raw.find(SPEC_MARKER)
        if idx == -1:
            raise AnalysisError(
                f"spec session reply carried no '{SPEC_MARKER}' document"
            )
        doc = raw[idx:]
        repeat = doc.find(SPEC_MARKER, len(SPEC_MARKER))
        if repeat != -1:
            doc = doc[:repeat]
        doc = doc.strip() + "\n"

        spec_path = specs / f"{item_id}.md"
        _atomic_write(spec_path, doc)
        _atomic_write(
            specs / f"{item_id}.json",
            json.dumps(
                {
                    "generated_at": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                    "target_store": str(target) if target else None,
                    "task": item.get("task"),
                },
                indent=2,
            )
            + "\n",
        )
        err_path.unlink(missing_ok=True)
        return spec_path
    except Exception as exc:
        try:
            err_path.write_text(str(exc), encoding="utf-8")
        except OSError:
            pass
        raise
    finally:
        running.unlink(missing_ok=True)
