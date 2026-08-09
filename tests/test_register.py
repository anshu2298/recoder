"""Tests for the live worktree register (read-only CCR + git rollup)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from recoder.analysis.register import (
    build_register,
    parse_commit_blocks,
    read_git,
    read_store,
    register_covers,
    render_register_md,
)
from recoder.config import Config

_MAIN_MD = """# Project Context

## Current Focus
TECH-037 v3.0: Phase 3 re-scoped. Next: gate run.

## Recent Milestones
- [2026-08-04 11:12] (main) Phase 3 re-scoped
- [2026-08-04 10:25] (main) PR #381 MERGED into main
- [2026-08-01 09:00] (main) Convergence sign-off

## Open Branches
(none)
"""

_COMMITS_MD = """# Branch: main ## Rolling Summary
Long rolling summary text.

---

## [C143] 2026-08-04 11:12 | branch:main | Phase 3 re-scoped
**What**: Split the gate run into two passes so the slow suite stops blocking.
**Why**: Owner wanted W4.1 unblocked before the review.
**Files**: a.js, b/c.ts
**Next**: Standing rule: plan review before W4.1 implementation.
**Score**: 1.00

---

## [C142] 2026-08-01 09:00 | branch:main | older
**What**: older stuff
**Next**: old next step
"""


def _make_store(root: Path, main_md: str = _MAIN_MD, commits_md: str = _COMMITS_MD) -> Path:
    ccr = root / ".ccr"
    (ccr / "branches" / "main").mkdir(parents=True)
    (ccr / "main.md").write_text(main_md, encoding="utf-8")
    (ccr / "branches" / "main" / "commits.md").write_text(commits_md, encoding="utf-8")
    return root


def _cfg(tmp_path: Path, trees: dict) -> Config:
    return Config(
        meetings_dir=tmp_path / "meetings",
        gladia_api_key="k",
        register_trees=trees,
    )


# --- CCR parsing --------------------------------------------------------------
def test_read_store_parses_focus_milestones_next(tmp_path: Path) -> None:
    store = _make_store(tmp_path / "tree")
    state = read_store(store)
    assert state.exists
    assert state.current_focus.startswith("TECH-037 v3.0")
    assert len(state.milestones) == 3
    assert state.milestones[0] == ("2026-08-04 11:12", "Phase 3 re-scoped")
    # Next comes from the FIRST (newest) commit block
    assert state.next_step.startswith("Standing rule")
    assert state.last_active == "2026-08-04 11:12"


def test_read_store_missing_is_calm(tmp_path: Path) -> None:
    state = read_store(tmp_path / "nothing-here")
    assert not state.exists
    assert state.current_focus == ""
    assert state.milestones == []


def test_parse_commit_blocks_full_detail() -> None:
    entries = parse_commit_blocks(_COMMITS_MD)
    assert [e.cid for e in entries] == ["C143", "C142"]
    top = entries[0]
    assert top.ts == "2026-08-04 11:12"
    assert top.branch == "main"
    assert top.title == "Phase 3 re-scoped"
    assert top.what.startswith("Split the gate run")
    assert top.why.startswith("Owner wanted")
    assert top.files == ["a.js", "b/c.ts"]
    assert top.next_step.startswith("Standing rule")
    assert top.score == "1.00"
    # a block with no Files/Why still parses, with empty fields
    assert entries[1].files == []
    assert entries[1].why == ""


def test_parse_commit_blocks_treats_none_files_as_empty() -> None:
    doc = "## [C1] 2026-08-01 10:00 | branch:main | t\n**Files**: (none)\n"
    assert parse_commit_blocks(doc)[0].files == []


def test_parse_commit_blocks_handles_empty_and_junk() -> None:
    assert parse_commit_blocks("") == []
    assert parse_commit_blocks("no commit headers at all") == []


# --- noise filtering ----------------------------------------------------------
_NOISY_COMMITS = """## [C224] 2026-08-06 13:35 | branch:main | [auto] Okay so the goal is
**What**: whatever the user last typed
**Why**: Session baseline: auto-captured when no explicit commit was made
**Next**:

---

## [C223] 2026-08-05 09:00 | branch:main | Real deliberate milestone
**What**: The actual work.
**Why**: Because it mattered.
**Next**: Do the next real thing.
"""

_NOISY_MAIN = """# Project Context

## Current Focus
[auto] Okay so the goal is. Next:

## Recent Milestones
- [2026-08-06 13:35] (main) [auto] Okay so the goal is
- [2026-08-05 09:00] (main) Real deliberate milestone
"""


def test_auto_baseline_commits_never_become_the_current_task(tmp_path: Path) -> None:
    """CCR's session-hook baselines restate user chatter — they must not win."""
    _make_store(tmp_path / "t", _NOISY_MAIN, _NOISY_COMMITS)
    cfg = _cfg(tmp_path, {"noisy": {"stores": [str(tmp_path / "t")]}})
    tree = build_register(cfg, with_git=False)[0]

    assert tree.task is not None
    assert tree.task.cid == "C223"
    assert tree.task.title == "Real deliberate milestone"
    # the junk focus line is replaced by the real task, not shown verbatim
    assert "[auto]" not in tree.current_focus
    assert tree.current_focus.startswith("The actual work")
    assert tree.next_step == "Do the next real thing."
    # and the milestone list drops the auto entry too
    assert all("[auto]" not in text for _, text in tree.milestones)


def test_all_noise_still_reports_something(tmp_path: Path) -> None:
    """A tree with only auto-commits must not render as blank."""
    only_noise = _NOISY_COMMITS.split("---")[0]
    main = "# Project Context\n\n## Recent Milestones\n- [2026-08-06 13:35] (main) [auto] x\n"
    _make_store(tmp_path / "t", main, only_noise)
    cfg = _cfg(tmp_path, {"n": {"stores": [str(tmp_path / "t")]}})
    tree = build_register(cfg, with_git=False)[0]
    assert tree.task is None
    assert tree.milestones  # falls back to showing the noisy ones
    assert tree.last_active == "2026-08-06 13:35"


# --- collation ----------------------------------------------------------------
def test_build_register_lead_store_speaks(tmp_path: Path) -> None:
    """Within a tree the most recently active store provides focus/next."""
    old = _make_store(
        tmp_path / "root",
        _MAIN_MD.replace("2026-08-04 11:12", "2026-07-01 08:00").replace(
            "TECH-037 v3.0: Phase 3 re-scoped. Next: gate run.", "OLD focus"
        ),
    )
    new = _make_store(tmp_path / "root" / "frontend")
    cfg = _cfg(tmp_path, {"mytree": {"stores": [str(old), str(new)]}})
    trees = build_register(cfg, with_git=False)
    assert len(trees) == 1
    t = trees[0]
    assert t.name == "mytree"
    assert t.current_focus.startswith("TECH-037")  # from the newer store
    assert t.last_active == "2026-08-04 11:12"
    assert t.milestones[0][0] == "2026-08-04 11:12"


def test_current_task_and_priors_come_from_commit_blocks(tmp_path: Path) -> None:
    _make_store(tmp_path / "a")
    cfg = _cfg(tmp_path, {"alpha": {"stores": [str(tmp_path / "a")]}})
    tree = build_register(cfg, with_git=False)[0]
    assert tree.task.cid == "C143"
    assert tree.task.what.startswith("Split the gate run")
    assert tree.task.files == ["a.js", "b/c.ts"]
    # the timeline continues with everything older, richest version per moment
    assert [c.ts for c in tree.recent] == ["2026-08-04 10:25", "2026-08-01 09:00"]
    assert tree.recent[-1].cid == "C142"  # the commit block, not the milestone


def test_timeline_merges_milestones_fresher_than_commits(tmp_path: Path) -> None:
    """CCR rotates commits.md, so main.md milestones often run fresher.

    Reading commits alone would report a task days behind the real one.
    """
    fresh_main = _MAIN_MD.replace(
        "- [2026-08-04 11:12] (main) Phase 3 re-scoped",
        "- [2026-08-09 07:00] (main) Newest work, milestone only\n"
        "- [2026-08-04 11:12] (main) Phase 3 re-scoped",
    )
    _make_store(tmp_path / "a", fresh_main)
    cfg = _cfg(tmp_path, {"alpha": {"stores": [str(tmp_path / "a")]}})
    tree = build_register(cfg, with_git=False)[0]

    assert tree.task.ts == "2026-08-09 07:00"
    assert tree.task.title == "Newest work, milestone only"
    assert tree.task.cid == ""          # no commit block backs it
    assert tree.last_active == "2026-08-09 07:00"
    # the detailed commit is still reported, one step down the timeline
    assert tree.recent[0].cid == "C143"
    assert tree.recent[0].what.startswith("Split the gate run")


def test_build_register_orders_trees_by_activity(tmp_path: Path) -> None:
    _make_store(tmp_path / "a")
    _make_store(tmp_path / "b", _MAIN_MD.replace("2026-08-04 11:12", "2026-08-07 09:00"))
    cfg = _cfg(
        tmp_path,
        {
            "alpha": {"stores": [str(tmp_path / "a")]},
            "beta": {"stores": [str(tmp_path / "b")]},
        },
    )
    assert [t.name for t in build_register(cfg, with_git=False)] == ["beta", "alpha"]


def test_register_covers(tmp_path: Path) -> None:
    store = tmp_path / "tree" / "frontend"
    store.mkdir(parents=True)
    cfg = _cfg(tmp_path, {"t": {"stores": [str(store)]}})
    assert register_covers(cfg, store)
    assert register_covers(cfg, store / "src" / "deep")
    assert register_covers(cfg, tmp_path / "tree")  # parent of a store
    assert not register_covers(cfg, tmp_path / "unrelated")


# --- git ----------------------------------------------------------------------
def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True, capture_output=True, text=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "feature/dossier")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Tester")
    (root / "a.txt").write_text("one", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first commit")
    (root / "b.txt").write_text("two", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "second commit")
    return root


def test_read_git_reports_branch_head_and_log(repo: Path) -> None:
    g = read_git(repo)
    assert g.is_repo
    assert g.branch == "feature/dossier"
    assert not g.detached
    assert len(g.head) == 8
    assert g.dirty == 0
    assert [c["subject"] for c in g.commits] == ["second commit", "first commit"]
    assert g.commits[0]["author"] == "Tester"
    assert g.commits[0]["date"][:2] == "20"


def test_read_git_counts_uncommitted_tracked_changes(repo: Path) -> None:
    (repo / "a.txt").write_text("changed", encoding="utf-8")
    assert read_git(repo).dirty == 1


def test_read_git_on_non_repo_is_calm(tmp_path: Path) -> None:
    g = read_git(tmp_path / "definitely-missing")
    assert not g.is_repo
    assert g.branch == ""
    assert g.commits == []


def test_register_attaches_git_to_tree(repo: Path, tmp_path: Path) -> None:
    _make_store(repo)
    cfg = _cfg(tmp_path, {"tree": {"stores": [str(repo)]}})
    tree = build_register(cfg)[0]
    assert tree.git is not None and tree.git.is_repo
    assert tree.git.branch == "feature/dossier"


def test_register_finds_git_even_without_ccr(repo: Path, tmp_path: Path) -> None:
    """A freshly branched tree has a repo long before it has memory."""
    cfg = _cfg(tmp_path, {"fresh": {"stores": [str(repo)]}})
    tree = build_register(cfg)[0]
    assert tree.git.is_repo
    assert tree.git.branch == "feature/dossier"
    assert tree.task is None


# --- rendering ----------------------------------------------------------------
def test_render_register_md(tmp_path: Path) -> None:
    _make_store(tmp_path / "a")
    cfg = _cfg(tmp_path, {"alpha": {"stores": [str(tmp_path / "a")]}})
    md = render_register_md(build_register(cfg, with_git=False))
    assert "# Worktree register" in md
    assert "## alpha — last active 2026-08-04 11:12" in md
    # the current task is reported in full, not as a one-line focus
    assert "**Current task** (2026-08-04 11:12, C143): Phase 3 re-scoped" in md
    assert "- What: Split the gate run" in md
    assert "- Why: Owner wanted" in md
    assert "- Files: a.js, b/c.ts" in md
    assert "- Next: Standing rule" in md
    assert "**Preceding changes:**" in md
    assert "- [2026-08-01 09:00] older" in md


def test_render_includes_repo_and_code_commits(repo: Path, tmp_path: Path) -> None:
    _make_store(repo)
    cfg = _cfg(tmp_path, {"tree": {"stores": [str(repo)]}})
    md = render_register_md(build_register(cfg))
    assert "**Repo:**" in md
    assert "on `feature/dossier`" in md
    assert "**Recent code commits:**" in md
    assert "second commit" in md


def test_render_empty_register_is_empty() -> None:
    assert render_register_md([]) == ""


def test_missing_tree_renders_placeholder(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, {"ghost": {"stores": [str(tmp_path / "gone")]}})
    md = render_register_md(build_register(cfg, with_git=False))
    assert "ghost" in md
    assert "(no CCR store or git checkout found)" in md
