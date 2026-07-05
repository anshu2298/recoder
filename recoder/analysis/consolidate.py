"""Worktree memory consolidation (Piece B).

The user's work is fragmented across per-worktree CCR stores (e.g.
``sherpa-linkedin-enrich/frontend`` has 66 commits of its own). This module runs
one Claude Agent SDK session with BOTH the source and target stores mounted,
distills the source's full history into a handful of milestone commits on the
target, and — only when ``apply=True`` — archives the source ``.ccr`` (never
deletes it) and de-registers it from the global CCR registry.

Reuses the SDK glue in :mod:`recoder.analysis.session` (options builder, retry
runner) rather than duplicating it.
"""

from __future__ import annotations

import logging
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from recoder.analysis import routing, session
from recoder.analysis.prompts import build_consolidation_prompt

logger = logging.getLogger(__name__)

__all__ = ["ConsolidationError", "ConsolidationResult", "consolidate"]

CONSOLIDATE_MAX_TURNS = 60  # paging a whole store's history takes many turns.

_COMMIT_ID_RE = re.compile(r"C\d+")


class ConsolidationError(Exception):
    """Raised when consolidation cannot be completed safely."""


@dataclass
class ConsolidationResult:
    commit_ids: list[str]
    archived_to: Path | None
    registry_updated: bool


def _build_options(source: Path, target: Path, config: object) -> object:
    """Two-store options: source read-only, target read+commit. cwd = target."""
    mcp_servers = {
        "ccr_source": session.ccr_server_for_project(config, source),
        "ccr_target": session.ccr_server_for_project(config, target),
    }
    allowed_tools = [
        # SOURCE is read-only — never gcc_commit.
        "mcp__ccr_source__gcc_search",
        "mcp__ccr_source__gcc_context",
        # TARGET receives the distilled milestones.
        "mcp__ccr_target__gcc_search",
        "mcp__ccr_target__gcc_context",
        "mcp__ccr_target__gcc_commit",
    ]
    return session.build_session_options(
        target, config, CONSOLIDATE_MAX_TURNS, mcp_servers, allowed_tools
    )


def _archive_source(source: Path, config: object, archive_dir: Path | None) -> Path:
    """Move ``source/.ccr`` into the archive (never delete). Returns the dest."""
    base = Path(archive_dir) if archive_dir else Path(config.consolidation_archive_dir)
    stamp = datetime.now().strftime("%Y%m%d")
    dest = base / f"{source.name}-{stamp}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        # Never clobber a prior archive; disambiguate with a time suffix.
        dest = base / f"{source.name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    shutil.move(str(source / ".ccr"), str(dest))
    return dest


def _deregister(source: Path, config: object) -> bool:
    """Drop the source entry from the global registry (after backup). """
    registry_path = config.ccr_registry_path
    raw = routing.read_raw_registry(registry_path)
    if not raw:
        return False
    target_key = routing.norm_path(source)
    kept = [d for d in raw if routing.norm_path(d.get("path") or "") != target_key]
    if len(kept) == len(raw):
        return False
    routing.backup_registry(registry_path)
    routing.write_registry(registry_path, kept)
    return True


def consolidate(
    source: Path,
    target: Path,
    config: object,
    *,
    session_runner: session.SessionRunner | None = None,
    archive_dir: Path | None = None,
    apply: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> ConsolidationResult:
    """Distill ``source``'s CCR history into ``target``; optionally archive source.

    Runs the distillation session in all modes. When ``apply`` is False (the
    default, i.e. dry-run) the source store is left in place and the registry is
    untouched. When ``apply`` is True and the session produced commit ids, the
    source ``.ccr`` is archived and its registry entry removed.

    Raises ConsolidationError on a missing store or a reply with no commit ids.
    """
    source = Path(source)
    target = Path(target)
    runner = session_runner or session._default_session_runner

    if not (source / ".ccr").is_dir():
        raise ConsolidationError(
            f"source store not found: {source / '.ccr'} does not exist"
        )
    if not (target / ".ccr").is_dir():
        raise ConsolidationError(
            f"target store not found: {target / '.ccr'} does not exist"
        )

    prompt = build_consolidation_prompt(source.name, target.name)
    options = _build_options(source, target, config)

    reply = session._run_with_retries(runner, prompt, options, sleep).strip()

    commit_ids = _COMMIT_ID_RE.findall(reply)
    if not commit_ids:
        raise ConsolidationError(
            f"consolidation reply carried no commit ids: {reply!r}"
        )
    # Dedupe while preserving order.
    seen: dict[str, None] = {}
    for cid in commit_ids:
        seen.setdefault(cid, None)
    commit_ids = list(seen.keys())

    archived_to: Path | None = None
    registry_updated = False
    if apply:
        archived_to = _archive_source(source, config, archive_dir)
        registry_updated = _deregister(source, config)
        logger.info(
            "consolidated %s -> %s (%d commits); archived to %s",
            source.name,
            target.name,
            len(commit_ids),
            archived_to,
        )
    else:
        logger.info(
            "dry-run consolidation %s -> %s produced %d commits; "
            "source left in place, registry untouched",
            source.name,
            target.name,
            len(commit_ids),
        )

    return ConsolidationResult(
        commit_ids=commit_ids,
        archived_to=archived_to,
        registry_updated=registry_updated,
    )
