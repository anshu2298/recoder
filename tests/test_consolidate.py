from __future__ import annotations

import json
from pathlib import Path

import pytest

from recoder.analysis import routing
from recoder.analysis.consolidate import (
    ConsolidationError,
    commit_id_num,
    consolidate,
    consolidate_group,
)
from recoder.config import Config


def _no_sleep(_seconds: float) -> None:
    return None


class FakeRunner:
    """Captures (prompt, options) per call and replays scripted replies.

    Pass one or more replies; call N returns reply N (the last reply is reused if
    more calls are made). A reply that is an Exception is raised.
    """

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, object]] = []

    def __call__(self, prompt: str, options: object) -> str:
        self.calls.append((prompt, options))
        idx = min(len(self.calls) - 1, len(self.replies) - 1)
        reply = self.replies[idx]
        if isinstance(reply, Exception):
            raise reply
        return reply


def _make_store(root: Path, name: str) -> Path:
    proj = root / name
    ccr = proj / ".ccr"
    ccr.mkdir(parents=True)
    (ccr / "metadata.yaml").write_text("created: 2026-01-01\n", encoding="utf-8")
    return proj


def _config(
    tmp_path: Path,
    *,
    registry_rows: list[dict] | None = None,
    groups: dict | None = None,
) -> Config:
    registry = tmp_path / "projects.json"
    registry.write_text(
        json.dumps(registry_rows or [], indent=2) + "\n", encoding="utf-8"
    )
    return Config(
        meetings_dir=tmp_path / "meetings",
        ccr_registry_path=registry,
        consolidation_archive_dir=tmp_path / "archives" / "ccr",
        consolidation_state_path=tmp_path / "consolidation-state.json",
        consolidation_groups=groups or {},
    )


def _seed_state(config: Config, mapping: dict) -> None:
    config.consolidation_state_path.write_text(
        json.dumps(mapping, indent=2) + "\n", encoding="utf-8"
    )


def _read_state(config: Config) -> dict:
    return json.loads(config.consolidation_state_path.read_text(encoding="utf-8"))


def _run(source, target, config, runner, **kw):
    return consolidate(
        source,
        target,
        config,
        session_runner=runner,
        sleep=_no_sleep,
        **kw,
    )


# --- commit id helper ---------------------------------------------------------
def test_commit_id_num_parses_zero_padded() -> None:
    assert commit_id_num("C047") == 47
    assert commit_id_num("C7") == 7
    with pytest.raises(ValueError):
        commit_id_num("not-an-id")


# --- first run / watermark write ----------------------------------------------
def test_first_run_writes_watermark_no_since_in_prompt(tmp_path: Path) -> None:
    source = _make_store(tmp_path, "sherpa-linkedin-enrich")
    target = _make_store(tmp_path, "sherpa")
    config = _config(tmp_path)
    runner = FakeRunner("Created C081, C082, C083\nHIGHEST_SOURCE_COMMIT: C066")

    result = _run(source, target, config, runner)

    assert result.commit_ids == ["C081", "C082", "C083"]
    assert result.highest_source_commit == "C066"
    assert result.no_new is False
    assert result.archived_to is None

    # First-run prompt has full-history wording, no watermark reference.
    prompt = runner.calls[0][0]
    assert "FIRST consolidation" in prompt
    assert "NO_NEW_COMMITS" not in prompt

    # Watermark stored under the normalized source path.
    state = _read_state(config)
    key = routing.norm_path(source)
    assert state[key]["last_commit_id"] == "C066"
    assert state[key]["runs"] == 1
    assert state[key]["target"] == str(target)
    assert "last_consolidated_at" in state[key]
    # Atomic write leaves no tmp file behind.
    assert not list(tmp_path.glob("consolidation-state.json.tmp"))


def test_second_run_passes_since_into_prompt(tmp_path: Path) -> None:
    source = _make_store(tmp_path, "wt")
    target = _make_store(tmp_path, "parent")
    config = _config(tmp_path)
    _seed_state(
        config,
        {routing.norm_path(source): {
            "target": str(target),
            "last_commit_id": "C066",
            "runs": 1,
        }},
    )
    runner = FakeRunner("Created C081\nHIGHEST_SOURCE_COMMIT: C070")

    result = _run(source, target, config, runner)

    prompt = runner.calls[0][0]
    assert "C066" in prompt  # watermark flowed into the prompt
    assert "GREATER" in prompt  # incremental-scope wording

    state = _read_state(config)
    entry = state[routing.norm_path(source)]
    assert entry["last_commit_id"] == "C070"
    assert entry["runs"] == 2


def test_no_new_commits_leaves_watermark_untouched(tmp_path: Path) -> None:
    source = _make_store(tmp_path, "wt")
    target = _make_store(tmp_path, "parent")
    config = _config(tmp_path)
    _seed_state(
        config,
        {routing.norm_path(source): {
            "target": str(target),
            "last_commit_id": "C066",
            "runs": 3,
        }},
    )
    runner = FakeRunner("NO_NEW_COMMITS since C066")

    result = _run(source, target, config, runner)

    assert result.no_new is True
    assert result.commit_ids == []
    assert result.highest_source_commit == "C066"
    # Watermark unchanged (not even runs).
    entry = _read_state(config)[routing.norm_path(source)]
    assert entry["last_commit_id"] == "C066"
    assert entry["runs"] == 3


# --- marker corrective turn ---------------------------------------------------
def test_marker_missing_triggers_corrective_then_succeeds(tmp_path: Path) -> None:
    source = _make_store(tmp_path, "wt")
    target = _make_store(tmp_path, "parent")
    config = _config(tmp_path)
    # First reply has commits but no marker; corrective reply supplies it.
    runner = FakeRunner("Created C081, C082", "HIGHEST_SOURCE_COMMIT: C066")

    result = _run(source, target, config, runner)

    assert result.commit_ids == ["C081", "C082"]
    assert result.highest_source_commit == "C066"
    assert len(runner.calls) == 2
    # The corrective turn asks only for the marker.
    assert "HIGHEST_SOURCE_COMMIT" in runner.calls[1][0]
    assert _read_state(config)[routing.norm_path(source)]["last_commit_id"] == "C066"


def test_marker_still_missing_raises_and_watermark_untouched(tmp_path: Path) -> None:
    source = _make_store(tmp_path, "wt")
    target = _make_store(tmp_path, "parent")
    config = _config(tmp_path)
    runner = FakeRunner("Created C081", "still no marker here")

    with pytest.raises(ConsolidationError, match="HIGHEST_SOURCE_COMMIT"):
        _run(source, target, config, runner)

    assert len(runner.calls) == 2
    # Watermark never written -> next run re-covers the span.
    assert not config.consolidation_state_path.exists()


def test_no_commit_ids_raises(tmp_path: Path) -> None:
    source = _make_store(tmp_path, "src")
    target = _make_store(tmp_path, "tgt")
    config = _config(tmp_path)
    runner = FakeRunner("I could not find anything to consolidate.")

    with pytest.raises(ConsolidationError, match="no commit ids"):
        _run(source, target, config, runner)
    assert (source / ".ccr").is_dir()
    assert not config.consolidation_state_path.exists()


def test_reply_commit_ids_are_deduped(tmp_path: Path) -> None:
    source = _make_store(tmp_path, "src")
    target = _make_store(tmp_path, "tgt")
    config = _config(tmp_path)
    runner = FakeRunner("C10, C11, C10, C11\nHIGHEST_SOURCE_COMMIT: C011")

    result = _run(source, target, config, runner)
    assert result.commit_ids == ["C10", "C11"]
    assert result.highest_source_commit == "C011"


# --- default (incremental) vs archive mode ------------------------------------
def test_default_mode_leaves_source_and_registry(tmp_path: Path) -> None:
    source = _make_store(tmp_path, "wt")
    target = _make_store(tmp_path, "parent")
    config = _config(
        tmp_path,
        registry_rows=[{"path": str(source), "name": source.name, "commit_count": 66}],
    )
    runner = FakeRunner("C081, C082, C083\nHIGHEST_SOURCE_COMMIT: C066")

    result = _run(source, target, config, runner)

    assert result.commit_ids == ["C081", "C082", "C083"]
    assert result.archived_to is None
    assert result.registry_updated is False
    # Source store + registry untouched.
    assert (source / ".ccr").is_dir()
    assert json.loads(config.ccr_registry_path.read_text(encoding="utf-8"))
    assert not list(tmp_path.glob("projects.json.bak-*"))


def test_archive_mode_archives_after_watermark_update(tmp_path: Path) -> None:
    source = _make_store(tmp_path, "sherpa-delivery-process-engine")
    target = _make_store(tmp_path, "sherpa")
    config = _config(
        tmp_path,
        registry_rows=[
            {"path": str(source), "name": source.name, "commit_count": 73},
            {"path": str(target), "name": target.name, "commit_count": 26},
        ],
    )
    runner = FakeRunner("C090, C091\nHIGHEST_SOURCE_COMMIT: C073")

    result = _run(source, target, config, runner, mode="archive")

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
    assert len(list(config.ccr_registry_path.parent.glob("projects.json.bak-*"))) == 1

    # Watermark advanced despite the archive.
    assert _read_state(config)[routing.norm_path(source)]["last_commit_id"] == "C073"


def test_archive_dir_override(tmp_path: Path) -> None:
    source = _make_store(tmp_path, "worktree")
    target = _make_store(tmp_path, "parent")
    config = _config(tmp_path)
    override = tmp_path / "custom-archive"
    runner = FakeRunner("C1\nHIGHEST_SOURCE_COMMIT: C001")

    result = _run(source, target, config, runner, mode="archive", archive_dir=override)

    assert result.archived_to is not None
    assert override in result.archived_to.parents


def test_archive_mode_source_absent_from_registry(tmp_path: Path) -> None:
    source = _make_store(tmp_path, "orphan")
    target = _make_store(tmp_path, "parent")
    config = _config(tmp_path, registry_rows=[])  # empty registry
    runner = FakeRunner("C1\nHIGHEST_SOURCE_COMMIT: C001")

    result = _run(source, target, config, runner, mode="archive")

    assert result.archived_to is not None  # still archived
    assert result.registry_updated is False  # nothing to remove


# --- state file robustness ----------------------------------------------------
def test_corrupt_state_file_tolerated(tmp_path: Path) -> None:
    source = _make_store(tmp_path, "wt")
    target = _make_store(tmp_path, "parent")
    config = _config(tmp_path)
    config.consolidation_state_path.write_text("{ not json", encoding="utf-8")
    runner = FakeRunner("C1\nHIGHEST_SOURCE_COMMIT: C009")

    result = _run(source, target, config, runner)

    # Corrupt file treated as empty -> first-run wording, then rewritten cleanly.
    assert "FIRST consolidation" in runner.calls[0][0]
    assert result.commit_ids == ["C1"]
    assert _read_state(config)[routing.norm_path(source)]["last_commit_id"] == "C009"


def test_state_key_normalized_across_slash_and_case(tmp_path: Path) -> None:
    source = _make_store(tmp_path, "WorkTree")
    target = _make_store(tmp_path, "parent")
    config = _config(tmp_path)

    # First run stores the watermark under the canonical normalized key.
    _run(source, target, config, FakeRunner("C1\nHIGHEST_SOURCE_COMMIT: C050"))
    assert routing.norm_path(source) in _read_state(config)

    # Second run: same directory, path spelled with backslashes + upper case.
    variant = Path(str(source).replace("/", "\\").upper())
    runner2 = FakeRunner("NO_NEW_COMMITS since C050")
    result = _run(variant, target, config, runner2)

    assert result.no_new is True
    # The watermark matched despite the different spelling.
    assert "C050" in runner2.calls[0][0]


# --- store validation ---------------------------------------------------------
def test_missing_source_store_raises(tmp_path: Path) -> None:
    source = tmp_path / "ghost"
    source.mkdir()  # no .ccr inside
    target = _make_store(tmp_path, "tgt")
    config = _config(tmp_path)
    runner = FakeRunner("C1\nHIGHEST_SOURCE_COMMIT: C001")

    with pytest.raises(ConsolidationError, match="source store not found"):
        _run(source, target, config, runner)
    assert runner.calls == []  # never ran the session


def test_missing_target_store_raises(tmp_path: Path) -> None:
    source = _make_store(tmp_path, "src")
    target = tmp_path / "ghost"
    target.mkdir()
    config = _config(tmp_path)
    runner = FakeRunner("C1\nHIGHEST_SOURCE_COMMIT: C001")

    with pytest.raises(ConsolidationError, match="target store not found"):
        _run(source, target, config, runner)


# --- options wiring -----------------------------------------------------------
def test_session_options_mount_source_readonly_target_writable(tmp_path: Path) -> None:
    source = _make_store(tmp_path, "src")
    target = _make_store(tmp_path, "tgt")
    config = _config(tmp_path)
    runner = FakeRunner("C1\nHIGHEST_SOURCE_COMMIT: C001")

    _run(source, target, config, runner)

    _, options = runner.calls[0]
    servers = options.mcp_servers
    assert set(servers) == {"ccr_source", "ccr_target"}
    assert str(source) in servers["ccr_source"]["args"]
    assert str(target) in servers["ccr_target"]["args"]

    tools = set(options.allowed_tools)
    assert "mcp__ccr_target__gcc_commit" in tools
    assert "mcp__ccr_target__gcc_search" in tools
    assert "mcp__ccr_target__gcc_context" in tools
    assert "mcp__ccr_source__gcc_search" in tools
    assert "mcp__ccr_source__gcc_context" in tools
    assert "mcp__ccr_source__gcc_commit" not in tools


# --- group command ------------------------------------------------------------
def test_group_processes_all_sources_and_counts_failures(tmp_path: Path) -> None:
    target = _make_store(tmp_path, "sherpa")
    s1 = _make_store(tmp_path, "frontend-a")
    s2 = _make_store(tmp_path, "frontend-b")
    s3 = _make_store(tmp_path, "frontend-c")
    config = _config(
        tmp_path,
        groups={
            "sherpa": {
                "target": str(target),
                "sources": [str(s1), str(s2), str(s3)],
            }
        },
    )
    # s1 ok, s2 fails (no commit ids), s3 ok — despite s2's failure.
    runner = FakeRunner(
        "C1\nHIGHEST_SOURCE_COMMIT: C010",
        "nothing to consolidate here",
        "C2\nHIGHEST_SOURCE_COMMIT: C020",
    )

    outcomes = consolidate_group(
        "sherpa", config, session_runner=runner, sleep=_no_sleep
    )

    failures = sum(1 for o in outcomes if o.error is not None)
    assert failures == 1  # exit code the CLI would return
    assert len(outcomes) == 3
    # The third source was still processed past the middle failure.
    assert outcomes[2].result is not None
    assert outcomes[2].result.commit_ids == ["C2"]

    # Watermarks written for the two successes only.
    state = _read_state(config)
    assert routing.norm_path(s1) in state
    assert routing.norm_path(s3) in state
    assert routing.norm_path(s2) not in state


def test_group_unknown_name_raises(tmp_path: Path) -> None:
    config = _config(tmp_path, groups={})
    with pytest.raises(ConsolidationError, match="unknown consolidation group"):
        consolidate_group("nope", config, session_runner=FakeRunner("x"))
