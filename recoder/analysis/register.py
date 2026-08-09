"""Live worktree register: a read-only rollup of a family of CCR stores.

The user's active work project (sherpa) is split across several git worktrees,
each accumulating its own CCR memory (often two stores per tree: the root and
``frontend/``). Nothing rolls those up — the nominal parent store goes stale
while activity piles up in the branches.

This module computes that rollup **live, on read**. It never writes to any
store and never calls a model. Three deterministic sources are collated per
tree:

  * ``.ccr/main.md`` — Current Focus and Recent Milestones;
  * ``.ccr/branches/main/commits.md`` — the full structured commit blocks
    (``**What** / **Why** / **Files** / **Next** / **Score**``), which carry
    far more detail than the one-line milestones;
  * **git** — the repository the tree is checked out from, the branch it sits
    on, how far it has drifted from its upstream, how much is uncommitted, and
    the last few real code commits.

The CCR side answers "what is the agent working on and why"; the git side
answers "where is that work physically happening". Together they are the
report the register renders.

Consumers:

  * the web UI's home screen (``GET /api/register``) — "what is happening in
    my four trees" at a glance when the app opens;
  * meeting analysis — when a routed project store lives inside a registered
    tree, the rendered register is injected into the analyst prompt so the
    summary sees the whole family, not just the mounted stores.

Because the register is derived state, there is no sync to break and no
staleness beyond the stores themselves.
"""

from __future__ import annotations

import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

__all__ = [
    "CommitEntry",
    "GitState",
    "StoreState",
    "TreeState",
    "build_register",
    "parse_commit_blocks",
    "read_git",
    "read_store",
    "render_register_md",
    "register_covers",
]

# Milestone bullets look like: "- [2026-08-04 11:12] (main) TECH-037 v3.0: ..."
_MILESTONE_RE = re.compile(
    r"^-\s*\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]\s*(?:\([^)]*\)\s*)?(.*)$"
)
_SECTION_RE = re.compile(r"^##\s+(.*)$", re.MULTILINE)
# Commit blocks: "## [C143] 2026-07-28 14:19 | branch:main | title"
_COMMIT_HEADER_RE = re.compile(
    r"^##\s+\[(C\d+)\]\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*"
    r"(?:\|\s*branch:\s*([^|\n]*?)\s*)?(?:\|\s*(.*?))?\s*$",
    re.MULTILINE,
)
# `[^\S\r\n]` (horizontal whitespace only) is load-bearing: a plain `\s*` here
# eats the newline after an EMPTY field and captures the next line — an empty
# `**Next**:` would report the block's `---` separator as the next step.
_FIELD_RE = re.compile(
    r"^\*\*(What|Why|Files|Next|Score)\*\*:[^\S\r\n]*(.*?)[^\S\r\n]*$", re.MULTILINE
)

# CCR writes a baseline commit whenever a session ends without an explicit one.
# Those entries restate whatever the user last typed and carry no decision, so
# they must never win the "current task" slot or the focus line.
_NOISE_PREFIXES = (
    "[auto]",
    "auto-commit:",
    "auto commit:",
    "session baseline",
    "rolling summary compression",   # CCR compacting its own file, not work
)
_NOISE_WHY_MARKERS = (
    "auto-committed by session hook",
    "session baseline",
    "auto-captured when no explicit commit",
    "ccr flagged the rolling summary",
)
_NOISE_SUBSTRINGS = ("rolling summary compression", "compressed rolling summary")

_GIT_TIMEOUT_S = 5.0
_GIT_LOG_COUNT = 5
_UNIT = "\x1f"


def _is_noise(title: str, why: str = "") -> bool:
    """True for CCR bookkeeping entries that carry no project decision.

    Two kinds: session-hook baselines (restating whatever the user last typed)
    and rolling-summary compactions (CCR maintaining its own file). Both are
    real commits with real timestamps, so nothing but content tells them apart
    from work — and left alone they win every "most recent" contest.
    """
    text = (title or "").strip().lower()
    if not text:
        return True
    if text.startswith(_NOISE_PREFIXES):
        return True
    if any(fragment in text for fragment in _NOISE_SUBSTRINGS):
        return True
    lowered = (why or "").lower()
    return any(marker in lowered for marker in _NOISE_WHY_MARKERS)


@dataclass
class CommitEntry:
    """One structured CCR commit block."""

    cid: str = ""
    ts: str = ""
    branch: str = ""
    title: str = ""
    what: str = ""
    why: str = ""
    files: list[str] = field(default_factory=list)
    next_step: str = ""
    score: str = ""

    @property
    def is_noise(self) -> bool:
        return _is_noise(self.title, self.why)


@dataclass
class GitState:
    """Where the tree physically lives, straight from git."""

    root: Path
    is_repo: bool = False
    repo: str = ""            # origin basename, else directory name
    branch: str = ""          # "" when detached
    detached: bool = False
    upstream: str = ""
    ahead: int = 0
    behind: int = 0
    dirty: int = 0            # tracked files with uncommitted changes
    head: str = ""            # short sha
    commits: list[dict] = field(default_factory=list)  # sha/subject/author/date


@dataclass
class StoreState:
    """Parsed snapshot of one CCR store."""

    path: Path
    exists: bool = False
    current_focus: str = ""
    milestones: list[tuple[str, str]] = field(default_factory=list)  # (ts, text)
    next_step: str = ""
    last_active: str = ""  # "YYYY-MM-DD HH:MM" or ""
    commits: list[CommitEntry] = field(default_factory=list)


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
    git: GitState | None = None
    task: CommitEntry | None = None          # the current task, in full
    recent: list[CommitEntry] = field(default_factory=list)  # prior major changes


# ---------------------------------------------------------------------------
# CCR parsing
# ---------------------------------------------------------------------------


def _section_body(text: str, header: str) -> str:
    match = re.search(rf"^##\s+{re.escape(header)}\s*$", text, re.MULTILINE)
    if match is None:
        return ""
    body = text[match.end():]
    nxt = _SECTION_RE.search(body)
    if nxt is not None:
        body = body[: nxt.start()]
    return body.strip()


def _flatten(value: str) -> str:
    return " ".join((value or "").split())


def parse_commit_blocks(text: str, *, limit: int = 40) -> list[CommitEntry]:
    """Parse ``commits.md`` into structured entries, newest first.

    CCR writes newest-first, so the file order is preserved. ``limit`` bounds
    the work on stores with hundreds of commits — the register only ever shows
    a handful, and the rest would be parsed and thrown away.
    """
    if not text:
        return []
    headers = list(_COMMIT_HEADER_RE.finditer(text))
    entries: list[CommitEntry] = []
    for idx, header in enumerate(headers[:limit]):
        end = headers[idx + 1].start() if idx + 1 < len(headers) else len(text)
        body = text[header.end():end]
        entry = CommitEntry(
            cid=header.group(1) or "",
            ts=header.group(2) or "",
            branch=_flatten(header.group(3) or ""),
            title=_flatten(header.group(4) or ""),
        )
        for name, value in _FIELD_RE.findall(body):
            value = _flatten(value)
            if name == "What":
                entry.what = value
            elif name == "Why":
                entry.why = value
            elif name == "Files":
                if value and value.lower() not in ("(none)", "none", "-"):
                    entry.files = [f.strip() for f in value.split(",") if f.strip()]
            elif name == "Next":
                entry.next_step = value
            elif name == "Score":
                entry.score = value
        entries.append(entry)
    return entries


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

    state.current_focus = _flatten(_section_body(text, "Current Focus"))

    for line in _section_body(text, "Recent Milestones").splitlines():
        m = _MILESTONE_RE.match(line.strip())
        if m:
            state.milestones.append((m.group(1), m.group(2).strip()))

    commits_md = ccr / "branches" / "main" / "commits.md"
    try:
        commits_text = commits_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        commits_text = ""
    state.commits = parse_commit_blocks(commits_text)

    # The most actionable line in the store: the newest commit's Next step.
    if state.commits:
        state.next_step = state.commits[0].next_step

    if state.milestones:
        state.last_active = max(ts for ts, _ in state.milestones)
    elif state.commits:
        state.last_active = state.commits[0].ts
    return state


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    """Run one git command in ``root``; "" on any failure. Never raises."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_S,
            # a console window would flash on every poll on Windows
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if os.name == "nt"
            else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout


def read_git(root: Path | str) -> GitState:
    """Read repo / branch / drift / recent commits for a worktree.

    Everything is best-effort: a missing git, a non-repo path, or a slow
    command degrades to ``is_repo=False`` rather than breaking the register.
    """
    path = Path(root)
    state = GitState(root=path, repo=path.name)
    if not path.exists():
        return state

    status = _git(path, "status", "--porcelain=v2", "--branch", "--untracked-files=no")
    if not status:
        return state
    state.is_repo = True

    for line in status.splitlines():
        if line.startswith("# branch.head "):
            head = line[len("# branch.head "):].strip()
            if head == "(detached)":
                state.detached = True
            else:
                state.branch = head
        elif line.startswith("# branch.oid "):
            oid = line[len("# branch.oid "):].strip()
            if oid and oid != "(initial)":
                state.head = oid[:8]
        elif line.startswith("# branch.upstream "):
            state.upstream = line[len("# branch.upstream "):].strip()
        elif line.startswith("# branch.ab "):
            for token in line[len("# branch.ab "):].split():
                try:
                    if token.startswith("+"):
                        state.ahead = int(token[1:])
                    elif token.startswith("-"):
                        state.behind = int(token[1:])
                except ValueError:
                    pass
        elif line[:1] in ("1", "2", "u"):
            state.dirty += 1

    origin = _git(path, "config", "--get", "remote.origin.url").strip()
    if origin:
        state.repo = re.sub(r"\.git$", "", origin.rstrip("/").rsplit("/", 1)[-1])

    log = _git(
        path,
        "log",
        f"-n{_GIT_LOG_COUNT}",
        f"--format=%h{_UNIT}%s{_UNIT}%an{_UNIT}%ad",
        "--date=format:%Y-%m-%d %H:%M",
    )
    for line in log.splitlines():
        parts = line.split(_UNIT)
        if len(parts) == 4:
            state.commits.append(
                {
                    "sha": parts[0],
                    "subject": parts[1],
                    "author": parts[2],
                    "date": parts[3],
                }
            )
    return state


# ---------------------------------------------------------------------------
# collation
# ---------------------------------------------------------------------------


def _collate_tree(name: str, store_roots: list[str], *, with_git: bool = True) -> TreeState:
    tree = TreeState(name=name)
    tree.stores = [read_store(p) for p in store_roots]
    live = [s for s in tree.stores if s.exists]

    # git is keyed on the configured roots, not on whether a CCR store exists:
    # a freshly branched tree has a repo long before it has memory.
    if with_git:
        for root in store_roots:
            git = read_git(root)
            if git.is_repo:
                tree.git = git
                break
        else:
            tree.git = read_git(store_roots[0]) if store_roots else None

    if not live:
        return tree

    # The most recently active store speaks for the tree.
    lead = max(live, key=lambda s: s.last_active or "")

    merged = sorted(
        {(ts, text) for s in live for ts, text in s.milestones},
        reverse=True,
    )
    substantive = [(ts, text) for ts, text in merged if not _is_noise(text)]
    tree.milestones = (substantive or merged)[:8]

    # The spine of the report is a single timeline: newest entry is the current
    # task, the rest are the changes that led to it.
    #
    # It has to merge both sources. commits.md carries the rich What/Why/Files
    # blocks but CCR rotates it, so main.md's milestone list often runs fresher
    # — reading commits alone reports a task that is days stale. Commits are
    # inserted first so a timestamp present in both keeps the detailed version.
    signal = [c for s in live for c in s.commits if not c.is_noise]
    timeline: dict[str, CommitEntry] = {}
    for commit in sorted(signal, key=lambda c: c.ts, reverse=True):
        timeline.setdefault(commit.ts, commit)
    for ts, text in substantive:
        timeline.setdefault(ts, CommitEntry(ts=ts, title=text))

    ordered = sorted(timeline.values(), key=lambda c: c.ts, reverse=True)
    if ordered:
        tree.task = ordered[0]
        tree.recent = ordered[1:6]

    # Focus: prefer main.md, but it mirrors the last commit — so when that was
    # an auto-baseline the focus line is junk and the newest real commit wins.
    focus = lead.current_focus
    if _is_noise(focus):
        focus = ""
    if not focus and tree.task:
        focus = tree.task.what or tree.task.title
    tree.current_focus = focus

    # Walk the timeline for the most recent entry that actually recorded a next
    # step. Taking it from the lead store instead lets a stale commit in one
    # store contradict a task reported from another.
    tree.next_step = next((c.next_step for c in ordered if c.next_step), "")
    if not tree.next_step and not _is_noise(lead.next_step):
        tree.next_step = lead.next_step

    # Staleness must answer "when did real work last happen here", so it is
    # measured from the substantive timeline. A tree whose only recent write is
    # a session-hook baseline is stale, however fresh that baseline's timestamp.
    tree.last_active = (
        ordered[0].ts if ordered else max((s.last_active for s in live), default="")
    )
    if tree.last_active:
        try:
            then = datetime.strptime(tree.last_active, "%Y-%m-%d %H:%M")
            tree.stale_days = max(0, (datetime.now() - then).days)
        except ValueError:
            pass
    return tree


def build_register(config, *, with_git: bool = True) -> list[TreeState]:
    """Collate every configured tree, most recently active first.

    Trees are collated concurrently: each one shells out to git a few times,
    and doing that serially across four worktrees is the only part of this
    module that is not instant.
    """
    specs: list[tuple[str, list[str]]] = []
    for name, spec in (config.register_trees or {}).items():
        stores = spec.get("stores") if isinstance(spec, dict) else None
        if not isinstance(stores, list) or not stores:
            continue
        specs.append((name, [str(s) for s in stores]))
    if not specs:
        return []

    if with_git and len(specs) > 1:
        with ThreadPoolExecutor(max_workers=min(8, len(specs))) as pool:
            trees = list(
                pool.map(lambda sp: _collate_tree(sp[0], sp[1], with_git=True), specs)
            )
    else:
        trees = [_collate_tree(n, s, with_git=with_git) for n, s in specs]

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


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    text = _flatten(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _git_line(git: GitState | None) -> str:
    if git is None or not git.is_repo:
        return ""
    parts = [f"`{git.repo}`"]
    parts.append(f"on `{git.branch}`" if git.branch else "detached HEAD")
    if git.head:
        parts.append(f"@ {git.head}")
    drift = []
    if git.ahead:
        drift.append(f"{git.ahead} ahead")
    if git.behind:
        drift.append(f"{git.behind} behind")
    if drift:
        parts.append(f"({', '.join(drift)} of {git.upstream or 'upstream'})")
    elif git.upstream:
        parts.append(f"(in sync with {git.upstream})")
    if git.dirty:
        parts.append(f"— {git.dirty} uncommitted file{'s' if git.dirty != 1 else ''}")
    return " ".join(parts)


def render_register_md(trees: list[TreeState], *, recent_per_tree: int = 5) -> str:
    """Render the register as markdown (analyst prompt + UI fallback).

    Detail level is deliberate: the analyst needs enough to tell whether a
    meeting's action item is already done, conflicts with in-flight work, or
    is genuinely new — a one-line focus was never enough for that call.
    """
    if not trees:
        return ""
    lines: list[str] = ["# Worktree register", ""]
    for tree in trees:
        staleness = ""
        if tree.stale_days is not None and tree.stale_days >= 7:
            staleness = f"  (stale {tree.stale_days}d)"
        lines.append(
            f"## {tree.name} — last active {tree.last_active or 'never'}{staleness}"
        )

        git_line = _git_line(tree.git)
        if git_line:
            lines.append(f"**Repo:** {git_line}")
        elif tree.git is not None:
            lines.append("**Repo:** _(not a git checkout)_")

        if tree.task:
            task = tree.task
            lines.append("")
            lines.append(f"**Current task** ({task.ts}, {task.cid}): {task.title}")
            if task.what:
                lines.append(f"- What: {_clip(task.what, 600)}")
            if task.why:
                lines.append(f"- Why: {_clip(task.why, 400)}")
            if task.files:
                shown = ", ".join(task.files[:6])
                more = len(task.files) - 6
                lines.append(f"- Files: {shown}{f' (+{more} more)' if more > 0 else ''}")
            if task.next_step:
                lines.append(f"- Next: {_clip(task.next_step, 400)}")
        elif tree.current_focus:
            lines.append(f"**Focus:** {_clip(tree.current_focus, 400)}")

        if tree.next_step and not (tree.task and tree.task.next_step):
            lines.append(f"**Next:** {_clip(tree.next_step, 400)}")

        if tree.recent:
            lines.append("")
            lines.append("**Preceding changes:**")
            for entry in tree.recent[:recent_per_tree]:
                lines.append(f"- [{entry.ts}] {entry.title}")
                detail = entry.what or entry.why
                if detail:
                    lines.append(f"  - {_clip(detail, 240)}")
        elif tree.milestones:
            lines.append("")
            lines.append("**Preceding changes:**")
            for ts, text in tree.milestones[:recent_per_tree]:
                lines.append(f"- [{ts}] {text}")

        if tree.git and tree.git.commits:
            lines.append("")
            lines.append("**Recent code commits:**")
            for commit in tree.git.commits[:recent_per_tree]:
                lines.append(
                    f"- {commit['sha']} {commit['subject']} "
                    f"({commit['author']}, {commit['date']})"
                )

        if not tree.task and not tree.milestones and not (tree.git and tree.git.is_repo):
            lines.append("_(no CCR store or git checkout found)_")
        lines.append("")
    return "\n".join(lines).strip() + "\n"
