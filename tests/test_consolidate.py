from __future__ import annotations

import json
from pathlib import Path

import pytest

from recoder.analysis.consolidate import (
    ConsolidationError,
    consolidate,
)
from recoder.config import Config


def _no_sleep(_seconds: float) -> None:
    return None


class FakeRunner:
    """Captures (prompt, options) and replays one scripted reply."""

    def __init__(self, reply):
        self.reply = reply
        self.calls: list[tuple[str, object]] = []

    def __call__(self, prompt: str, options: object) -> str:
        self.calls.append((prompt, options))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _make_store(root: Path, name: str) -> Path:
    proj = root / name
    ccr = proj / ".ccr"
    ccr.mkdir(parents=True)
    (ccr / "metadata.yaml").write_text("created: 2026-01-01\n", encoding="utf-8")
    return proj


def _config(tmp_path: Path, *, registry_rows: list[dict] | None = None) -> Config:
    registry = tmp_path / "projects.json"
    registry.write_text(
        json.dumps(registry_rows or [], indent=2) + "\n", encoding="utf-8"
    )
    return Config(
        meetings_dir=tmp_path / "meetings",
        ccr_registry_path=registry,
        consolidation_archive_dir=tmp_path / "archives" / "ccr",
    )


def _run(source, target, config, runner, **kw):
    return consolidate(
        source,
        target,
        config,
        session_runner=runner,
        sleep=_no_sleep,
        **kw,
    )


# --- happy path / dry run -----------------------------------------------------
def test_dry_run_distills_without_archiving(tmp_path: Path) -> None:
    source = _make_store(tmp_path, "sherpa-linkedin-enrich")
    target = _make_store(tmp_path, "sherpa")
    config = _config(
        tmp_path,
        registry_rows=[{"path": str(source), "name": source.name, "commit_count": 66}],
    )
    runner = FakeRunner("Created C081, C082, C083")

    result = _run(source, target, config, runner)

    assert result.commit_ids == ["C081", "C082", "C083"]
    assert result.archived_to is None
    assert result.registry_updated is False
    # Source store untouched, registry unchanged.
    assert (source / ".ccr").is_dir()
    assert json.loads(config.ccr_registry_path.read_text(encoding="utf-8"))
    # No backup written in dry run.
    assert not list(tmp_path.glob("projects.json.bak-*"))


def test_reply_commit_ids_are_deduped(tmp_path: Path) -> None:
    source = _make_store(tmp_path, "src")
    target = _make_store(tmp_path, "tgt")
    config = _config(tmp_path)
    runner = FakeRunner("C10, C11, C10, C11")

    result = _run(source, target, config, runner)
    assert result.commit_ids == ["C10", "C11"]


# --- apply --------------------------------------------------------------------
def test_apply_archives_and_deregisters(tmp_path: Path) -> None:
    source = _make_store(tmp_path, "sherpa-delivery-process-engine")
    target = _make_store(tmp_path, "sherpa")
    config = _config(
        tmp_path,
        registry_rows=[
            {"path": str(source), "name": source.name, "commit_count": 73},
            {"path": str(target), "name": target.name, "commit_count": 26},
        ],
    )
    runner = FakeRunner("C090, C091")

    result = _run(source, target, config, runner, apply=True)

    # .ccr moved into the archive dir.
    assert result.archived_to is not None
    assert result.archived_to.is_dir()
    assert (result.archived_to / "metadata.yaml").exists()
    assert not (source / ".ccr").exists()
    assert result.archived_to.name.startswith("sherpa-delivery-process-engine-")

    # Registry entry removed + backup written.
    assert result.registry_updated is True
    remaining = json.loads(config.ccr_registry_path.read_text(encoding="utf-8"))
    paths = {r["path"] for r in remaining}
    assert str(source) not in paths
    assert str(target) in paths
    backups = list(config.ccr_registry_path.parent.glob("projects.json.bak-*"))
    assert len(backups) == 1


def test_apply_archive_dir_override(tmp_path: Path) -> None:
    source = _make_store(tmp_path, "worktree")
    target = _make_store(tmp_path, "parent")
    config = _config(tmp_path)
    override = tmp_path / "custom-archive"
    runner = FakeRunner("C1")

    result = _run(source, target, config, runner, apply=True, archive_dir=override)

    assert result.archived_to is not None
    assert override in result.archived_to.parents


def test_apply_source_absent_from_registry(tmp_path: Path) -> None:
    source = _make_store(tmp_path, "orphan")
    target = _make_store(tmp_path, "parent")
    config = _config(tmp_path, registry_rows=[])  # empty registry
    runner = FakeRunner("C1")

    result = _run(source, target, config, runner, apply=True)

    assert result.archived_to is not None  # still archived
    assert result.registry_updated is False  # nothing to remove


# --- error paths --------------------------------------------------------------
def test_no_commit_ids_raises(tmp_path: Path) -> None:
    source = _make_store(tmp_path, "src")
    target = _make_store(tmp_path, "tgt")
    config = _config(tmp_path)
    runner = FakeRunner("I could not find anything to consolidate.")

    with pytest.raises(ConsolidationError, match="no commit ids"):
        _run(source, target, config, runner)
    # No archiving happened on failure.
    assert (source / ".ccr").is_dir()


def test_missing_source_store_raises(tmp_path: Path) -> None:
    source = tmp_path / "ghost"
    source.mkdir()  # no .ccr inside
    target = _make_store(tmp_path, "tgt")
    config = _config(tmp_path)
    runner = FakeRunner("C1")

    with pytest.raises(ConsolidationError, match="source store not found"):
        _run(source, target, config, runner)
    assert runner.calls == []  # never ran the session


def test_missing_target_store_raises(tmp_path: Path) -> None:
    source = _make_store(tmp_path, "src")
    target = tmp_path / "ghost"
    target.mkdir()
    config = _config(tmp_path)
    runner = FakeRunner("C1")

    with pytest.raises(ConsolidationError, match="target store not found"):
        _run(source, target, config, runner)


# --- options wiring -----------------------------------------------------------
def test_session_options_mount_source_readonly_target_writable(tmp_path: Path) -> None:
    source = _make_store(tmp_path, "src")
    target = _make_store(tmp_path, "tgt")
    config = _config(tmp_path)
    runner = FakeRunner("C1")

    _run(source, target, config, runner)

    _, options = runner.calls[0]
    servers = options.mcp_servers
    assert set(servers) == {"ccr_source", "ccr_target"}
    # Each server points --project at its own store.
    assert str(source) in servers["ccr_source"]["args"]
    assert str(target) in servers["ccr_target"]["args"]

    tools = set(options.allowed_tools)
    # Target is writable.
    assert "mcp__ccr_target__gcc_commit" in tools
    assert "mcp__ccr_target__gcc_search" in tools
    assert "mcp__ccr_target__gcc_context" in tools
    # Source is strictly read-only.
    assert "mcp__ccr_source__gcc_search" in tools
    assert "mcp__ccr_source__gcc_context" in tools
    assert "mcp__ccr_source__gcc_commit" not in tools
