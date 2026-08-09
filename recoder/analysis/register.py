"""Live worktree register: a read-only rollup of a family of CCR stores.

The user's active work project (sherpa) is split across several git worktrees,
each accumulating its own CCR memory (often two stores per tree: the root and
``frontend/``). Nothing rolls those up — the nominal parent store goes stale
while activity piles up in the branches.

This module computes that rollup **live, on read**. It never writes to any
store and never calls a model: each configured store's ``.ccr/main.md``
(Current Focus / Recent Milestones) and ``.ccr/branches/main/commits.md``
(latest commit's Next step) are parsed deterministically and collated per
tree. Consumers:

  * the web UI's home screen (``GET /api/register``) — "what is happening in
    my four trees" at a glance when the app opens;
  * meeting analysis — when a routed project store lives inside a registered
    tree, the rendered register is injected into the analyst prompt so the
    summary sees the whole family, not just the mounted stores.

Because the register is derived state, there is no sync to break and no
staleness beyond the stores themselves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

__all__ = [
    "StoreState",
    "TreeState",
    "build_register",
    "render_register_md",
    "register_covers",
]

# Milestone bullets look like: "- [2026-08-04 11:12] (main) TECH-037 v3.0: ..."
_MILESTONE_RE = re.compile(
    r"^-\s*\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]\s*(?:\([^)]*\)\s*)?(.*)$"
)
_SECTION_RE = re.compile(r"^##\s+(.*)$", re.MULTILINE)
# Commit blocks in commits.md: "## [C143] 2026-07-28 14:19 | branch:main | title"
_COMMIT_HEADER_RE = re.compile(
    r"^##\s+\[C\d+\]\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", re.MULTILINE
)
_NEXT_RE = re.compile(r"^\*\*Next\*\*:\s*(.+?)\s*$", re.MULTILINE)


@dataclass
class StoreState:
    """Parsed snapshot of one CCR store."""

    path: Path
    exists: bool = False
    current_focus: str = ""
    milestones: list[tuple[str, str]] = field(default_factory=list)  # (ts, text)
    next_step: str = ""
    last_active: str = ""  # "YYYY-MM-DD HH:MM" or ""


@dataclass
class TreeState:
    """Collated state of one worktree (one or more stores)."""

    name: str
    stores: list[StoreState] = field(default_factory=list)
    current_focus: str = ""
    milestones: list[tuple[str, str]] = field(default_factory=list)
    next_step: str = ""
    last_active: str = ""
    stale_days: int | None = None


def _section_body(text: str, header: str) -> str:
    match = re.search(rf"^##\s+{re.escape(header)}\s*$", text, re.MULTILINE)
    if match is None:
        return ""
    body = text[match.end():]
    nxt = _SECTION_RE.search(body)
    if nxt is not None:
        body = body[: nxt.start()]
    return body.strip()


def read_store(store_root: Path | str) -> StoreState:
    """Parse one store's ``.ccr`` directory into a :class:`StoreState`.

    ``store_root`` is the *project* directory (the one holding ``.ccr``).
    Missing/partial stores yield ``exists=False`` or empty fields — never an
    exception; the register must render whatever is there.
    """
    root = Path(store_root)
    ccr = root / ".ccr"
    state = StoreState(path=root)
    main_md = ccr / "main.md"
    if not main_md.exists():
        return state
    state.exists = True

    try:
        text = main_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return state

    focus = _section_body(text, "Current Focus")
    state.current_focus = " ".join(focus.split())

    for line in _section_body(text, "Recent Milestones").splitlines():
        m = _MILESTONE_RE.match(line.strip())
        if m:
            state.milestones.append((m.group(1), m.group(2).strip()))

    # Latest commit's Next step — the most actionable line in the store.
    commits_md = ccr / "branches" / "main" / "commits.md"
    try:
        commits_text = commits_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        commits_text = ""
    if commits_text:
        first_next = _NEXT_RE.search(commits_text)
        if first_next:
            state.next_step = " ".join(first_next.group(1).split())

    if state.milestones:
        state.last_active = max(ts for ts, _ in state.milestones)
    else:
        header = _COMMIT_HEADER_RE.search(commits_text)
        if header:
            state.last_active = header.group(1)
    return state


def _collate_tree(name: str, store_roots: list[str]) -> TreeState:
    tree = TreeState(name=name)
    tree.stores = [read_store(p) for p in store_roots]
    live = [s for s in tree.stores if s.exists]
    if not live:
        return tree

    # The most recently active store speaks for the tree's focus/next.
    lead = max(live, key=lambda s: s.last_active or "")
    tree.current_focus = lead.current_focus
    tree.next_step = lead.next_step

    merged = sorted(
        {(ts, text) for s in live for ts, text in s.milestones},
        reverse=True,
    )
    # CCR auto-baseline commits ("[auto] ...", "Auto-commit: ...") are noise
    # next to deliberate milestones; drop them unless they are all there is.
    substantive = [
        (ts, text)
        for ts, text in merged
        if not text.startswith(("[auto]", "Auto-commit:"))
    ]
    tree.milestones = (substantive or merged)[:8]
    tree.last_active = max((s.last_active for s in live), default="")
    if tree.last_active:
        try:
            then = datetime.strptime(tree.last_active, "%Y-%m-%d %H:%M")
            tree.stale_days = max(0, (datetime.now() - then).days)
        except ValueError:
            pass
    return tree


def build_register(config) -> list[TreeState]:
    """Collate every configured tree, most recently active first."""
    trees: list[TreeState] = []
    for name, spec in (config.register_trees or {}).items():
        stores = spec.get("stores") if isinstance(spec, dict) else None
        if not isinstance(stores, list) or not stores:
            continue
        trees.append(_collate_tree(name, [str(s) for s in stores]))
    trees.sort(key=lambda t: t.last_active or "", reverse=True)
    return trees


def register_covers(config, path: Path | str) -> bool:
    """True iff ``path`` is inside (or equals) any registered store."""
    try:
        target = Path(path).resolve()
    except OSError:
        return False
    for spec in (config.register_trees or {}).values():
        stores = spec.get("stores") if isinstance(spec, dict) else None
        for store in stores or []:
            try:
                root = Path(store).resolve()
            except OSError:
                continue
            if target == root or root in target.parents or target in root.parents:
                return True
    return False


def render_register_md(trees: list[TreeState], *, milestones_per_tree: int = 4) -> str:
    """Render the register as compact markdown (analyst prompt + UI)."""
    if not trees:
        return ""
    lines: list[str] = ["# Worktree register", ""]
    for tree in trees:
        staleness = ""
        if tree.stale_days is not None and tree.stale_days >= 7:
            staleness = f"  (stale {tree.stale_days}d)"
        lines.append(f"## {tree.name} — last active {tree.last_active or 'never'}{staleness}")
        if tree.current_focus:
            lines.append(f"**Focus:** {tree.current_focus}")
        if tree.next_step:
            lines.append(f"**Next:** {tree.next_step}")
        if tree.milestones:
            lines.append("Recent:")
            for ts, text in tree.milestones[:milestones_per_tree]:
                lines.append(f"- [{ts}] {text}")
        if not tree.current_focus and not tree.milestones:
            lines.append("_(no CCR store found)_")
        lines.append("")
    return "\n".join(lines).strip() + "\n"
