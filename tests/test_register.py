"""Tests for the live worktree register (read-only CCR rollup)."""

from __future__ import annotations

from pathlib import Path

import pytest

from recoder.analysis.register import (
    build_register,
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
**What**: stuff happened
**Why**: because
**Files**: a.js
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


def _cfg(tmp_path: Path, trees: dict) -> Config:
    return Config(
        meetings_dir=tmp_path / "meetings",
        gladia_api_key="k",
        register_trees=trees,
    )


def test_build_register_lead_store_speaks(tmp_path: Path) -> None:
    """Within a tree the most recently active store provides focus/next."""
    old = _make_store(
        tmp_path / "root",
        _MAIN_MD.replace("2026-08-04 11:12", "2026-07-01 08:00").replace(
            "TECH-037 v3.0: Phase 3 re-scoped. Next: gate run.", "OLD focus"
        ),
    )
    new = _make_store(tmp_path / "root" / "frontend")
    cfg = _cfg(
        tmp_path, {"mytree": {"stores": [str(old), str(new)]}}
    )
    trees = build_register(cfg)
    assert len(trees) == 1
    t = trees[0]
    assert t.name == "mytree"
    assert t.current_focus.startswith("TECH-037")  # from the newer store
    assert t.last_active == "2026-08-04 11:12"
    # milestones merged + deduped across stores, newest first
    assert t.milestones[0][0] == "2026-08-04 11:12"


def test_build_register_orders_trees_by_activity(tmp_path: Path) -> None:
    _make_store(tmp_path / "a")
    _make_store(
        tmp_path / "b",
        _MAIN_MD.replace("2026-08-04 11:12", "2026-08-07 09:00"),
    )
    cfg = _cfg(
        tmp_path,
        {
            "alpha": {"stores": [str(tmp_path / "a")]},
            "beta": {"stores": [str(tmp_path / "b")]},
        },
    )
    trees = build_register(cfg)
    assert [t.name for t in trees] == ["beta", "alpha"]


def test_register_covers(tmp_path: Path) -> None:
    store = tmp_path / "tree" / "frontend"
    store.mkdir(parents=True)
    cfg = _cfg(tmp_path, {"t": {"stores": [str(store)]}})
    assert register_covers(cfg, store)
    assert register_covers(cfg, store / "src" / "deep")
    assert register_covers(cfg, tmp_path / "tree")  # parent of a store
    assert not register_covers(cfg, tmp_path / "unrelated")


def test_render_register_md(tmp_path: Path) -> None:
    _make_store(tmp_path / "a")
    cfg = _cfg(tmp_path, {"alpha": {"stores": [str(tmp_path / "a")]}})
    md = render_register_md(build_register(cfg))
    assert "# Worktree register" in md
    assert "## alpha — last active 2026-08-04 11:12" in md
    assert "**Focus:** TECH-037" in md
    assert "**Next:** Standing rule" in md
    assert "- [2026-08-04 11:12] Phase 3 re-scoped" in md


def test_render_empty_register_is_empty() -> None:
    assert render_register_md([]) == ""


def test_missing_tree_renders_placeholder(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, {"ghost": {"stores": [str(tmp_path / "gone")]}})
    md = render_register_md(build_register(cfg))
    assert "ghost" in md
    assert "(no CCR store found)" in md
