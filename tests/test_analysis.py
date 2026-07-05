from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from recoder.analysis import prompts, session
from recoder.analysis.session import AnalysisError, analyze, commit_to_ccr
from recoder.config import Config


# --- fixtures / helpers -------------------------------------------------------
FULL_SUMMARY = """# Meeting Summary

## TL;DR
We agreed to ship billing v2.

## Discussion
Reviewed the billing dashboard shown at 00:15.

## Decisions
Ship billing v2 on Friday.

## Action Items
| Owner | Task | Due |
| --- | --- | --- |
| Rahul | Fix invoice bug | Friday |

## Open Questions
Who owns the migration?

## Project Mapping
Relates to the recoder billing project.

## Speakers
| Speaker | Name | Evidence |
| --- | --- | --- |
| SPEAKER_1 | Rahul | addressed by name |
"""


def _make_meeting(tmp_path: Path, *, segments=None, with_summary=False) -> Path:
    folder = tmp_path / "meeting"
    (folder / "frames").mkdir(parents=True)

    meta = {
        "schema_version": 1,
        "state": "diarized",
        "title": "Weekly Billing Sync",
        "context_note": "sync with Rahul about billing",
        "started_at": "2026-07-05T14:30:00",
        "stages": {},
    }
    (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    if segments is None:
        segments = [
            {"speaker": "Me", "start": 0.0, "end": 5.0, "text": "Hi Rahul", "language": "en"},
            {"speaker": "SPEAKER_1", "start": 5.0, "end": 12.0, "text": "Hey there", "language": "en"},
        ]
    transcript = {"segments": segments, "source": "test", "generated_at": "2026-07-05T15:00:00"}
    (folder / "transcript.json").write_text(json.dumps(transcript), encoding="utf-8")

    index = folder / "frames" / "index.jsonl"
    index.write_text(
        json.dumps({"file": "000001_143512.jpg", "wall": "14:35:12", "window_title": "Zoom Meeting", "fallback_fullscreen": False})
        + "\n"
        + json.dumps({"file": "000002_143540.jpg", "wall": "14:35:40", "window_title": "Desktop", "fallback_fullscreen": True})
        + "\n",
        encoding="utf-8",
    )

    if with_summary:
        (folder / "summary.md").write_text(FULL_SUMMARY, encoding="utf-8")

    return folder


def _write_registry(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    # Empty registry keeps analyze() hermetic (no foreign mounts) by default.
    registry = _write_registry(tmp_path, [])
    return Config(meetings_dir=tmp_path / "meetings", ccr_registry_path=registry)


class CapturingRunner:
    """session_runner that records the options each call received."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.options: list[object] = []
        self.prompts: list[str] = []

    def __call__(self, prompt: str, options: object) -> str:
        self.prompts.append(prompt)
        self.options.append(options)
        item = self._replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeRunner:
    """Injectable session_runner replacement; records calls, replays scripted replies."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls: list[str] = []

    def __call__(self, prompt: str, options: object) -> str:
        self.calls.append(prompt)
        item = self._replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _no_sleep(_seconds: float) -> None:
    return None


# --- prompt assembly ----------------------------------------------------------
def test_analysis_prompt_has_required_section_contract() -> None:
    meta = {"title": "T", "context_note": "note", "started_at": "2026-07-05T14:30:00"}
    inv = [{"file": "000001_143512.jpg", "wall": "14:35:12", "window_title": "Zoom", "fallback_fullscreen": True}]
    tx = prompts.render_transcript([{"speaker": "Me", "start": 65.0, "end": 70.0, "text": "hello"}])
    prompt = prompts.build_analysis_prompt(meta, tx, inv, 120.0)

    for section in prompts.REQUIRED_SECTIONS:
        assert section in prompt


def test_analysis_prompt_renders_transcript_and_frames_and_ccr() -> None:
    meta = {"title": "T", "context_note": "note", "started_at": "2026-07-05T14:30:00"}
    inv = [{"file": "000001_143512.jpg", "wall": "14:35:12", "window_title": "Zoom", "fallback_fullscreen": True}]
    tx = prompts.render_transcript(
        [{"speaker": "SPEAKER_1", "start": 65.0, "end": 70.0, "text": "hello world"}]
    )
    prompt = prompts.build_analysis_prompt(meta, tx, inv, 120.0)

    # timestamp + speaker rendering
    assert "[01:05] SPEAKER_1: hello world" in prompt
    # frames table with fallback flag column and value
    assert "Fallback fullscreen" in prompt
    assert "000001_143512.jpg" in prompt
    assert "| yes |" in prompt
    # CCR instruction before summarizing
    assert "gcc_search" in prompt and "gcc_context" in prompt
    assert "BEFORE" in prompt
    # occlusion warning
    assert "desktop content" in prompt


def test_frame_table_source_column_marks_presented_monitors() -> None:
    inv = [
        {"file": "a.jpg", "wall": "14:35:12", "window_title": "Zoom", "fallback_fullscreen": False},
        {"file": "b.jpg", "wall": "14:35:40", "window_title": None, "source": "monitor2", "presenting": True},
    ]
    table = prompts.render_frame_table(inv)

    assert "| Source |" in table
    assert "| window |" in table  # legacy entries default to window
    assert "monitor2 (screen-share active)" in table

    meta = {"title": "T", "context_note": "", "started_at": "2026-07-05T14:30:00"}
    prompt = prompts.build_analysis_prompt(meta, "tx", inv, 60.0)
    assert "content being presented" in prompt


def test_commit_prompt_instructs_single_commit_and_id_reply() -> None:
    meta = {"title": "Weekly Sync", "started_at": "2026-07-05T14:30:00", "context_note": "billing"}
    p = prompts.build_commit_prompt(FULL_SUMMARY, meta)

    assert "mcp__ccr__gcc_commit" in p
    assert "EXACTLY ONCE" in p
    assert 'Meeting: Weekly Sync (2026-07-05)' in p
    assert "files_changed: []" in p
    assert "commit id" in p


# --- analyze ------------------------------------------------------------------
def test_analyze_happy_path_writes_summary(tmp_path: Path, cfg: Config) -> None:
    folder = _make_meeting(tmp_path)
    runner = FakeRunner([FULL_SUMMARY])

    analyze(folder, cfg, session_runner=runner, sleep=_no_sleep)

    summary_path = folder / "summary.md"
    assert summary_path.exists()
    text = summary_path.read_text(encoding="utf-8")
    assert text.startswith("# Meeting Summary")
    for section in prompts.REQUIRED_SECTIONS:
        assert section in text
    # atomic write leaves no tmp behind
    assert not (folder / "summary.md.tmp").exists()
    assert len(runner.calls) == 1


def test_analyze_strips_preamble_before_document(tmp_path: Path, cfg: Config) -> None:
    folder = _make_meeting(tmp_path)
    runner = FakeRunner(["Sure, here is the summary:\n\n" + FULL_SUMMARY])

    analyze(folder, cfg, session_runner=runner, sleep=_no_sleep)

    text = (folder / "summary.md").read_text(encoding="utf-8")
    assert text.startswith("# Meeting Summary")
    assert "Sure, here is" not in text


def test_analyze_missing_transcript_raises(tmp_path: Path, cfg: Config) -> None:
    folder = _make_meeting(tmp_path)
    (folder / "transcript.json").unlink()
    runner = FakeRunner([FULL_SUMMARY])

    with pytest.raises(AnalysisError, match="transcript.json"):
        analyze(folder, cfg, session_runner=runner, sleep=_no_sleep)


def test_analyze_empty_segments_raises(tmp_path: Path, cfg: Config) -> None:
    folder = _make_meeting(tmp_path, segments=[])
    runner = FakeRunner([FULL_SUMMARY])

    with pytest.raises(AnalysisError, match="no segments"):
        analyze(folder, cfg, session_runner=runner, sleep=_no_sleep)


def test_analyze_missing_sections_triggers_corrective_turn(tmp_path: Path, cfg: Config) -> None:
    folder = _make_meeting(tmp_path)
    bad = "# Meeting Summary\n\n## TL;DR\nincomplete document"
    runner = FakeRunner([bad, FULL_SUMMARY])

    analyze(folder, cfg, session_runner=runner, sleep=_no_sleep)

    assert (folder / "summary.md").exists()
    assert len(runner.calls) == 2
    # the corrective prompt names the missing sections
    assert "## Decisions" in runner.calls[1]
    assert "missing" in runner.calls[1].lower()


def test_analyze_corrective_turn_still_bad_raises(tmp_path: Path, cfg: Config) -> None:
    folder = _make_meeting(tmp_path)
    bad = "# Meeting Summary\n\n## TL;DR\nstill incomplete"
    runner = FakeRunner([bad, bad])

    with pytest.raises(AnalysisError, match="missing required sections"):
        analyze(folder, cfg, session_runner=runner, sleep=_no_sleep)
    assert len(runner.calls) == 2
    assert not (folder / "summary.md").exists()


def test_analyze_transport_error_retries_then_succeeds(tmp_path: Path, cfg: Config) -> None:
    folder = _make_meeting(tmp_path)
    runner = FakeRunner([RuntimeError("connection dropped"), FULL_SUMMARY])

    analyze(folder, cfg, session_runner=runner, sleep=_no_sleep)

    assert (folder / "summary.md").exists()
    assert len(runner.calls) == 2


def test_analyze_transport_error_exhausts_retries(tmp_path: Path, cfg: Config) -> None:
    folder = _make_meeting(tmp_path)
    runner = FakeRunner([RuntimeError("boom"), RuntimeError("boom"), RuntimeError("boom")])

    with pytest.raises(AnalysisError, match="after 3 attempts"):
        analyze(folder, cfg, session_runner=runner, sleep=_no_sleep)
    assert len(runner.calls) == session.MAX_ATTEMPTS


# --- commit_to_ccr ------------------------------------------------------------
def test_commit_happy_path_updates_meta(tmp_path: Path, cfg: Config) -> None:
    folder = _make_meeting(tmp_path, with_summary=True)
    runner = FakeRunner(["C12345"])

    commit_to_ccr(folder, cfg, session_runner=runner, sleep=_no_sleep)

    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    assert meta["ccr_commit"] == "C12345"


def test_commit_missing_summary_raises(tmp_path: Path, cfg: Config) -> None:
    folder = _make_meeting(tmp_path, with_summary=False)
    runner = FakeRunner(["C1"])

    with pytest.raises(AnalysisError, match="summary.md is missing"):
        commit_to_ccr(folder, cfg, session_runner=runner, sleep=_no_sleep)


def test_commit_garbage_reply_raises(tmp_path: Path, cfg: Config) -> None:
    folder = _make_meeting(tmp_path, with_summary=True)
    runner = FakeRunner(["I was unable to do that right now."])

    with pytest.raises(AnalysisError, match="no commit id"):
        commit_to_ccr(folder, cfg, session_runner=runner, sleep=_no_sleep)


# --- meeting write-back into routed project stores ----------------------------
def _routed_config(tmp_path: Path) -> Config:
    other = tmp_path / "billing-service"
    (other / ".ccr").mkdir(parents=True)
    registry = _write_registry(
        tmp_path,
        [
            {
                "path": str(other),
                "name": "billing-service",
                "last_used": "2026-07-04T10:00:00+00:00",
                "commit_count": 12,
            }
        ],
    )
    return Config(meetings_dir=tmp_path / "meetings", ccr_registry_path=registry)


def test_commit_prompt_with_mounts_instructs_writeback() -> None:
    meta = {"title": "Weekly Sync", "started_at": "2026-07-05T14:30:00", "context_note": "billing"}
    mounted = [{"slug": "billing_service", "name": "billing-service", "reason": "recent"}]
    p = prompts.build_commit_prompt(FULL_SUMMARY, meta, mounted_projects=mounted)

    assert "mcp__ccr__gcc_commit" in p
    assert "mcp__ccr_billing_service__gcc_commit" in p
    assert "Project write-back" in p
    assert "<slug>: <commit id>" in p
    assert "Skip any mounted project the meeting did not actually concern" in p


def test_commit_mounts_routed_store_writable(tmp_path: Path) -> None:
    folder = _make_meeting(tmp_path, with_summary=True)
    config = _routed_config(tmp_path)
    runner = CapturingRunner(["C12345\nbilling_service: C88"])

    commit_to_ccr(folder, config, session_runner=runner, sleep=_no_sleep)

    options = runner.options[0]
    assert "ccr_billing_service" in options.mcp_servers
    tools = set(options.allowed_tools)
    assert "mcp__ccr_billing_service__gcc_commit" in tools

    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    assert meta["ccr_commit"] == "C12345"
    assert meta["ccr_writebacks"] == {"billing_service": "C88"}


def test_commit_writeback_lines_do_not_steal_recoder_id(tmp_path: Path) -> None:
    folder = _make_meeting(tmp_path, with_summary=True)
    config = _routed_config(tmp_path)
    # Model replied with the write-back line first, recoder id after.
    runner = CapturingRunner(["billing_service: C88\nC12345"])

    commit_to_ccr(folder, config, session_runner=runner, sleep=_no_sleep)

    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    assert meta["ccr_commit"] == "C12345"
    assert meta["ccr_writebacks"] == {"billing_service": "C88"}


def test_commit_no_writeback_leaves_meta_clean(tmp_path: Path, cfg: Config) -> None:
    folder = _make_meeting(tmp_path, with_summary=True)
    runner = FakeRunner(["C12345"])

    commit_to_ccr(folder, cfg, session_runner=runner, sleep=_no_sleep)

    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    assert meta["ccr_commit"] == "C12345"
    assert "ccr_writebacks" not in meta


# --- project-memory routing into analysis (Piece A) ---------------------------
def test_analyze_mounts_routed_project(tmp_path: Path) -> None:
    folder = _make_meeting(tmp_path)  # title mentions "Billing"
    other = tmp_path / "billing-service"
    (other / ".ccr").mkdir(parents=True)
    registry = _write_registry(
        tmp_path,
        [
            {
                "path": str(other),
                "name": "billing-service",
                "last_used": "2026-07-04T10:00:00+00:00",
                "commit_count": 12,
            }
        ],
    )
    config = Config(meetings_dir=tmp_path / "meetings", ccr_registry_path=registry)
    runner = CapturingRunner([FULL_SUMMARY])

    analyze(folder, config, session_runner=runner, sleep=_no_sleep)

    options = runner.options[0]
    # recoder store always mounted, plus the routed foreign store (read-only).
    assert "ccr" in options.mcp_servers
    assert "ccr_billing_service" in options.mcp_servers
    tools = set(options.allowed_tools)
    assert "mcp__ccr_billing_service__gcc_search" in tools
    assert "mcp__ccr_billing_service__gcc_context" in tools
    assert "mcp__ccr_billing_service__gcc_commit" not in tools
    # The prompt names the mounted project.
    assert "billing-service" in runner.prompts[0]
    assert "READ-ONLY" in runner.prompts[0]


def test_analyze_registry_failure_still_runs_with_recoder_only(tmp_path: Path) -> None:
    folder = _make_meeting(tmp_path)
    corrupt = tmp_path / "projects.json"
    corrupt.write_text("{ not valid json ]", encoding="utf-8")
    config = Config(meetings_dir=tmp_path / "meetings", ccr_registry_path=corrupt)
    runner = CapturingRunner([FULL_SUMMARY])

    analyze(folder, config, session_runner=runner, sleep=_no_sleep)

    assert (folder / "summary.md").exists()
    options = runner.options[0]
    assert set(options.mcp_servers) == {"ccr"}


def test_analysis_prompt_lists_mounted_projects() -> None:
    meta = {"title": "T", "context_note": "note", "started_at": "2026-07-05T14:30:00"}
    mounted = [{"slug": "sherpa_enrich", "name": "sherpa-linkedin-enrich", "reason": "matched 'sherpa'"}]
    prompt = prompts.build_analysis_prompt(meta, "tx", [], 60.0, mounted_projects=mounted)

    assert "sherpa-linkedin-enrich" in prompt
    assert "mcp__ccr_sherpa_enrich__gcc_search" in prompt
    assert "Project memory available" in prompt


def test_consolidation_prompt_mounts_and_roles() -> None:
    p = prompts.build_consolidation_prompt("worktree-a", "parent-b")
    assert "mcp__ccr_source__gcc_context" in p
    assert "mcp__ccr_target__gcc_commit" in p
    assert "[from worktree-a]" in p
    assert "READ-ONLY" in p


# --- no import-time dependency on the pipeline package ------------------------
def test_no_pipeline_import_at_module_load() -> None:
    code = (
        "import sys; import recoder.analysis.session; "
        "assert 'recoder.pipeline' not in sys.modules, "
        "'session must not import recoder.pipeline at load time'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
