"""Worktree memory consolidation (Piece B) — incremental checkpoint sync.

The user's work lives in per-worktree CCR stores (e.g.
``sherpa-linkedin-enrich/frontend`` has 66+ commits of its own). Those stores
stay ALIVE and registered. Consolidation is an incremental checkpoint sync: each
run mounts a source store read-only alongside a writable target store, distills
only the source commits NEWER than a per-source watermark into a handful of
milestone commits on the target, and advances the watermark.

Archiving is now an explicit opt-in (``mode="archive"``): after the distill +
watermark update, the source ``.ccr`` is archived (moved, never deleted) and
de-registered from the global CCR registry. The default mode leaves the source
directory and the registry untouched.

Reuses the SDK glue in :mod:`recoder.analysis.session` (options builder, retry
runner) rather than duplicating it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from recoder.analysis import routing, session
from recoder.analysis.prompts import build_consolidation_prompt

logger = logging.getLogger(__name__)

__all__ = [
    "ConsolidationError",
    "ConsolidationResult",
    "GroupOutcome",
    "consolidate",
    "consolidate_group",
    "commit_id_num",
    "load_state",
    "watermark_for",
]

CONSOLIDATE_MAX_TURNS = 60  # paging a whole store's history takes many turns.

_COMMIT_ID_RE = re.compile(r"C\d+")
_HIGHEST_RE = re.compile(r"HIGHEST_SOURCE_COMMIT:\s*(C\d+)", re.IGNORECASE)
_NO_NEW_RE = re.compile(r"NO_NEW_COMMITS", re.IGNORECASE)


class ConsolidationError(Exception):
    """Raised when consolidation cannot be completed safely."""


@dataclass
class ConsolidationResult:
    commit_ids: list[str]
    archived_to: Path | None
    registry_updated: bool
    no_new: bool = False
    highest_source_commit: str | None = None


@dataclass
class GroupOutcome:
    """One source's result within a group run (error XOR result)."""

    source: Path
    result: ConsolidationResult | None = None
    error: str | None = None


# --- commit-id helpers --------------------------------------------------------
def commit_id_num(cid: str) -> int:
    """Parse a CCR commit id (``"C047"``) into its sequential integer (``47``)."""
    m = re.fullmatch(r"[Cc](\d+)", str(cid).strip())
    if not m:
        raise ValueError(f"not a commit id: {cid!r}")
    return int(m.group(1))


def commit_id_gt(a: str, b: str) -> bool:
    """True if commit id ``a`` is strictly newer (higher number) than ``b``."""
    return commit_id_num(a) > commit_id_num(b)


# --- watermark state (per-source, keyed by normalized path) -------------------
def _state_path(config: object) -> Path:
    return Path(config.consolidation_state_path)


def _state_key(source: Path) -> str:
    return routing.norm_path(source)


def load_state(config: object) -> dict:
    """Read the watermark state file. Missing/corrupt/non-dict -> ``{}``."""
    p = _state_path(config)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("consolidation state %s is unreadable: %s", p, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(config: object, state: dict) -> None:
    """Atomically write the watermark state (tmp + os.replace)."""
    p = _state_path(config)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(state, indent=2) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)


def watermark_for(config: object, source: Path) -> dict | None:
    """The stored watermark entry for ``source`` (normalized key), or None."""
    return load_state(config).get(_state_key(Path(source)))


# --- reply parsing ------------------------------------------------------------
def _parse_highest(text: str) -> str | None:
    m = _HIGHEST_RE.search(text)
    if not m:
        return None
    return m.group(1).upper()


def _parse_target_commit_ids(text: str) -> list[str]:
    """Target commit ids in the reply, excluding the HIGHEST_SOURCE_COMMIT line."""
    cleaned = _HIGHEST_RE.sub("", text)
    seen: dict[str, None] = {}
    for cid in _COMMIT_ID_RE.findall(cleaned):
        seen.setdefault(cid, None)
    return list(seen.keys())


def _build_marker_corrective_prompt(prior: str) -> str:
    return f"""Your previous reply did not end with the required marker line reporting
the highest source commit id you examined.

Do NOT create any new commits. Reply with ONLY that line, in EXACTLY this format
(nothing else):

    HIGHEST_SOURCE_COMMIT: C<nnn>

Your previous reply is below for reference.

## Previous reply
{prior}
"""


# --- SDK options wiring -------------------------------------------------------
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


# --- archive + deregister (opt-in, mode="archive") ----------------------------
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
    """Drop the source entry from the global registry (after backup)."""
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


# --- public contract ----------------------------------------------------------
def consolidate(
    source: Path,
    target: Path,
    config: object,
    *,
    mode: str = "incremental",
    session_runner: session.SessionRunner | None = None,
    archive_dir: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> ConsolidationResult:
    """Incrementally sync ``source``'s NEW CCR commits into ``target``.

    The distillation session always runs and writes target commits — that IS the
    operation. It examines only source commits newer than the per-source
    watermark and, on success, advances the watermark BEFORE any archive step.

    ``mode="incremental"`` (default) leaves the source dir and registry
    untouched. ``mode="archive"`` additionally archives the source ``.ccr`` and
    de-registers it after the watermark update.

    Raises ConsolidationError on a missing store, a reply with no target commit
    ids, or a missing HIGHEST_SOURCE_COMMIT marker that survives one corrective
    turn (in which case the watermark is left unchanged so the next run re-covers
    the span; target-side dedup absorbs any repeats).
    """
    source = Path(source)
    target = Path(target)
    if mode not in ("incremental", "archive"):
        raise ConsolidationError(f"unknown consolidation mode: {mode!r}")
    runner = session_runner or session._default_session_runner

    if not (source / ".ccr").is_dir():
        raise ConsolidationError(
            f"source store not found: {source / '.ccr'} does not exist"
        )
    if not (target / ".ccr").is_dir():
        raise ConsolidationError(
            f"target store not found: {target / '.ccr'} does not exist"
        )

    state = load_state(config)
    key = _state_key(source)
    entry = state.get(key) or {}
    since = entry.get("last_commit_id") or None

    prompt = build_consolidation_prompt(source.name, target.name, since)
    options = _build_options(source, target, config)

    reply = session._run_with_retries(runner, prompt, options, sleep).strip()

    # No new commits since the watermark: nothing written, watermark untouched.
    if _NO_NEW_RE.search(reply):
        logger.info(
            "consolidation %s -> %s: no new commits since %s",
            source.name,
            target.name,
            since,
        )
        return ConsolidationResult(
            commit_ids=[],
            archived_to=None,
            registry_updated=False,
            no_new=True,
            highest_source_commit=since,
        )

    commit_ids = _parse_target_commit_ids(reply)
    if not commit_ids:
        raise ConsolidationError(
            f"consolidation reply carried no commit ids: {reply!r}"
        )

    highest = _parse_highest(reply)
    if highest is None:
        # The session created commits but omitted the watermark marker. One
        # corrective turn asking ONLY for the marker (analyze's pattern).
        corrective = _build_marker_corrective_prompt(reply)
        reply2 = session._run_with_retries(runner, corrective, options, sleep).strip()
        highest = _parse_highest(reply2)
        if highest is None:
            raise ConsolidationError(
                "consolidation reply missing HIGHEST_SOURCE_COMMIT marker after a "
                "corrective turn; watermark left unchanged so the next run "
                f"re-covers the span. Reply was: {reply2!r}"
            )

    # Advance the watermark BEFORE any archive step.
    state[key] = {
        "target": str(target),
        "last_commit_id": highest,
        "last_consolidated_at": datetime.now(timezone.utc).isoformat(),
        "runs": int(entry.get("runs") or 0) + 1,
    }
    _save_state(config, state)

    archived_to: Path | None = None
    registry_updated = False
    if mode == "archive":
        archived_to = _archive_source(source, config, archive_dir)
        registry_updated = _deregister(source, config)
        logger.info(
            "consolidated %s -> %s (%d commits, watermark %s); archived to %s",
            source.name,
            target.name,
            len(commit_ids),
            highest,
            archived_to,
        )
    else:
        logger.info(
            "synced %s -> %s (%d commits, watermark now %s); "
            "source left in place, registry untouched",
            source.name,
            target.name,
            len(commit_ids),
            highest,
        )

    return ConsolidationResult(
        commit_ids=commit_ids,
        archived_to=archived_to,
        registry_updated=registry_updated,
        no_new=False,
        highest_source_commit=highest,
    )


def consolidate_group(
    name: str,
    config: object,
    *,
    mode: str = "incremental",
    session_runner: session.SessionRunner | None = None,
    archive_dir: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[GroupOutcome]:
    """Sync every source in the named group against the group's target.

    Sources are processed sequentially; an individual ConsolidationError is
    captured as an outcome and processing continues. The caller derives the exit
    code from the number of ``error`` outcomes.
    """
    groups = getattr(config, "consolidation_groups", {}) or {}
    group = groups.get(name)
    if not group:
        raise ConsolidationError(f"unknown consolidation group: {name!r}")
    target = Path(group["target"])
    sources = [Path(s) for s in group.get("sources", [])]

    outcomes: list[GroupOutcome] = []
    for src in sources:
        try:
            result = consolidate(
                src,
                target,
                config,
                mode=mode,
                session_runner=session_runner,
                archive_dir=archive_dir,
                sleep=sleep,
            )
            outcomes.append(GroupOutcome(source=src, result=result))
        except ConsolidationError as exc:
            logger.warning("group %s: source %s failed: %s", name, src.name, exc)
            outcomes.append(GroupOutcome(source=src, error=str(exc)))
    return outcomes
