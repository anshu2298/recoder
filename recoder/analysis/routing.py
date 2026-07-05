"""Meeting -> project-memory routing + CCR registry hygiene (Piece A).

Given the global CCR registry (``~/.ccr/projects.json``) and a meeting's
title/context note, decide which foreign project stores to mount read-only into
the analysis session so Claude can connect the meeting to the user's active
work. Pure logic + tolerant registry I/O — no SDK, no MCP.

Registry shape (confirmed against the installed CCR, v0.4.0):
    a JSON list of objects, each ``{path, name, last_used, commit_count}`` where
    ``last_used`` is an ISO-8601 timestamp with a UTC offset
    (e.g. ``"2026-07-05T04:55:05+00:00"``). CCR registers ``--project`` (default
    cwd) on every MCP-server init and there is NO flag/env to suppress it, so the
    registry accretes junk (node_modules, __pycache__, meeting folders, drive
    roots). Filtering that junk out is this module's job.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "ProjectEntry",
    "RoutedProject",
    "load_registry",
    "read_raw_registry",
    "is_junk",
    "is_prunable",
    "route_projects",
    "backup_registry",
    "write_registry",
    "norm_path",
]

# Path substrings that mark an entry as never-mountable junk.
_JUNK_SUBSTRINGS = (
    "node_modules",
    "__pycache__",
    ".claude",
    "site-packages",
    "/meetings/",  # recoder meeting subfolders (spuriously auto-registered)
)

_MIN = datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class ProjectEntry:
    """One row of the CCR global registry."""

    name: str
    path: str
    commit_count: int
    last_used: datetime | None


@dataclass(frozen=True)
class RoutedProject:
    """A foreign store selected for read-only mounting into a session."""

    name: str
    path: str
    reason: str


# --- path helpers -------------------------------------------------------------
def norm_path(path: str | Path) -> str:
    """Normalize a path for comparison: forward slashes, no trailing slash, lower."""
    text = str(path).replace("\\", "/").rstrip("/")
    return text.lower()


def _final_component(path: str) -> str:
    return norm_path(path).rsplit("/", 1)[-1]


def _is_drive_root(path: str) -> bool:
    stripped = str(path).replace("\\", "/").rstrip("/")
    # "" (was "/" or ""), or a bare drive like "G:".
    return stripped == "" or re.fullmatch(r"[a-zA-Z]:", stripped) is not None


# --- registry I/O (tolerant: never raise on bad input) ------------------------
def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def read_raw_registry(path: str | Path) -> list[dict]:
    """Return the registry as raw dicts, or [] on missing/corrupt/non-list."""
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("CCR registry %s is unreadable: %s", p, exc)
        return []
    if not isinstance(data, list):
        logger.warning("CCR registry %s is not a JSON list; ignoring", p)
        return []
    return [d for d in data if isinstance(d, dict)]


def entry_from_dict(d: dict) -> ProjectEntry:
    try:
        commit_count = int(d.get("commit_count") or 0)
    except (TypeError, ValueError):
        commit_count = 0
    return ProjectEntry(
        name=str(d.get("name") or ""),
        path=str(d.get("path") or ""),
        commit_count=commit_count,
        last_used=_parse_dt(d.get("last_used")),
    )


def load_registry(path: str | Path) -> list[ProjectEntry]:
    """Parse the registry into ProjectEntry rows. Corrupt/missing -> []."""
    return [entry_from_dict(d) for d in read_raw_registry(path)]


# --- classification -----------------------------------------------------------
def is_junk(entry: ProjectEntry) -> bool:
    """True if an entry can never be a legitimate mount target.

    Junk: path contains node_modules / __pycache__ / .claude / site-packages /
    a meetings dir, OR the name is empty, OR the path is a drive root.
    """
    if not entry.name.strip():
        return True
    if _is_drive_root(entry.path):
        return True
    normalized = norm_path(entry.path)
    if any(sub in normalized for sub in _JUNK_SUBSTRINGS):
        return True
    # A bare ".../meetings" leaf (the recoder meetings dir itself).
    if normalized.endswith("/meetings"):
        return True
    return False


def is_prunable(
    entry: ProjectEntry, *, now: datetime, empty_max_age_days: int = 30
) -> bool:
    """True if ``memory-clean`` should drop this entry.

    Junk, OR an empty store (commit_count == 0) unused for > ``empty_max_age_days``.
    """
    if is_junk(entry):
        return True
    if entry.commit_count == 0:
        last = entry.last_used or _MIN
        if (now - last).days > empty_max_age_days:
            return True
    return False


# --- routing ------------------------------------------------------------------
_WORD_RE = re.compile(r"[a-z0-9]+")


def _keywords(*texts: str) -> list[str]:
    """Ordered-unique lowercase words >= 4 chars from the given texts."""
    seen: dict[str, None] = {}
    for text in texts:
        for word in _WORD_RE.findall(text.lower()):
            if len(word) >= 4:
                seen.setdefault(word, None)
    return list(seen.keys())


def _keyword_match(entry: ProjectEntry, keywords: list[str]) -> str | None:
    """First keyword that appears in the entry name or final path component."""
    haystacks = (entry.name.lower(), _final_component(entry.path))
    for word in keywords:
        if any(word in h for h in haystacks):
            return word
    return None


def _used_str(now: datetime, last_used: datetime | None) -> str:
    if last_used is None:
        return "unknown"
    days = (now - last_used).days
    if days <= 0:
        return "today"
    return f"{days}d ago"


def route_projects(
    entries: list[ProjectEntry],
    title: str,
    context_note: str,
    *,
    now: datetime,
    recency_days: int = 7,
    max_mounts: int = 4,
    exclude_paths: tuple[str, ...] = (),
) -> list[RoutedProject]:
    """Select foreign stores to mount for a meeting.

    A candidate qualifies if it is non-junk, has commits, is not excluded, and
    is either *recent* (used within ``recency_days``) or *keyword*-matched (a
    >=4-char word from title/context appears in its name or final path
    component). Results are deduped by path, ranked keyword+recent > recent
    (newest first) > keyword, and capped at ``max_mounts``. The recoder project
    is always excluded (it is mounted separately).
    """
    keywords = _keywords(title, context_note)
    excluded = {norm_path(p) for p in exclude_paths}

    # rank: 0 = keyword+recent, 1 = recent only, 2 = keyword only.
    best: dict[str, tuple[int, datetime, RoutedProject]] = {}
    for entry in entries:
        if is_junk(entry) or entry.commit_count <= 0:
            continue
        key = norm_path(entry.path)
        if key in excluded:
            continue

        recent = (
            entry.last_used is not None
            and (now - entry.last_used).days <= recency_days
            and now >= entry.last_used
        )
        matched = _keyword_match(entry, keywords)
        if not recent and matched is None:
            continue

        if matched is not None and recent:
            rank = 0
            reason = f"matched '{matched}', used {_used_str(now, entry.last_used)}"
        elif recent:
            rank = 1
            reason = f"recent: used {_used_str(now, entry.last_used)}"
        else:  # keyword only
            rank = 2
            reason = f"matched '{matched}'"

        routed = RoutedProject(name=entry.name, path=entry.path, reason=reason)
        sort_dt = entry.last_used or _MIN
        prior = best.get(key)
        if prior is None or rank < prior[0]:
            best[key] = (rank, sort_dt, routed)

    ordered = sorted(best.values(), key=lambda item: (item[0], -item[1].timestamp()))
    return [routed for _, _, routed in ordered[:max_mounts]]


# --- registry mutation (used by memory-clean + consolidate deregister) --------
def backup_registry(path: str | Path, *, now: datetime | None = None) -> Path:
    """Copy ``projects.json`` to ``projects.json.bak-<timestamp>``; return it."""
    p = Path(path)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    backup = p.with_name(p.name + f".bak-{stamp}")
    backup.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    return backup


def write_registry(path: str | Path, raw_list: list[dict]) -> None:
    """Write the registry back in CCR's own format (indent=2 + trailing NL)."""
    p = Path(path)
    p.write_text(json.dumps(raw_list, indent=2) + "\n", encoding="utf-8")
