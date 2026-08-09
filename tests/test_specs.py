"""Tests for structured action items + on-demand spec generation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from recoder.analysis.action_items import (
    extract_action_items_json,
    strip_action_items_json,
)
from recoder.analysis.spec import (
    SPEC_MARKER,
    generate_spec,
    load_action_items,
    resolve_target_store,
    spec_status,
)
from recoder.analysis.session import AnalysisError
from recoder.config import Config

_SUMMARY_WITH_JSON = """# Meeting Summary

## Action Items
| Owner | Task | Due |
| --- | --- | --- |
| Anshu | Build the dossier prototype | |

## Action Items JSON
```json
{"items": [
  {"id": "ai-1", "owner": "Anshu", "task": "Build the dossier prototype",
   "due": "", "kind": "build", "project": "linkedin-enrich",
   "evidence": {"segments": [{"t": "12:30", "quote": "we need live context"}],
                "frames": ["000031_x.jpg"]},
   "state_relation": "extends the dossier flow (C150)"}
]}
```
"""


# --- extraction ---------------------------------------------------------------
def test_extract_action_items_json() -> None:
    items = extract_action_items_json(_SUMMARY_WITH_JSON)
    assert items is not None and len(items) == 1
    item = items[0]
    assert item["kind"] == "build"
    assert item["project"] == "linkedin-enrich"
    assert item["evidence"]["segments"][0]["t"] == "12:30"
    assert item["evidence"]["frames"] == ["000031_x.jpg"]
    assert item["state_relation"].startswith("extends")


def test_extract_returns_none_without_section() -> None:
    assert extract_action_items_json("# Meeting Summary\n## Action Items\n") is None


def test_extract_returns_none_on_bad_json() -> None:
    bad = "## Action Items JSON\n```json\n{not json}\n```\n"
    assert extract_action_items_json(bad) is None


def test_extract_backfills_ids_and_drops_taskless() -> None:
    doc = (
        '## Action Items JSON\n```json\n{"items": ['
        '{"task": "A"}, {"owner": "x"}, "junk", {"task": "B"}]}\n```\n'
    )
    items = extract_action_items_json(doc)
    assert [i["task"] for i in items] == ["A", "B"]
    assert items[0]["id"] == "ai-1"
    assert items[1]["kind"] == "other"


def test_strip_action_items_json() -> None:
    stripped = strip_action_items_json(_SUMMARY_WITH_JSON)
    assert "## Action Items JSON" not in stripped
    assert "```json" not in stripped
    assert "## Action Items" in stripped  # human table untouched
    # idempotent on already-stripped docs
    assert strip_action_items_json(stripped) == stripped


# --- load_action_items --------------------------------------------------------
def _make_meeting(tmp_path: Path, *, with_json: bool = True) -> Path:
    folder = tmp_path / "meeting"
    folder.mkdir()
    (folder / "meta.json").write_text(
        json.dumps({"title": "Sync", "started_at": "2026-08-09T10:00:00"}),
        encoding="utf-8",
    )
    (folder / "summary.md").write_text(
        "# Meeting Summary\n\n## Action Items\n"
        "| Owner | Task | Due |\n| --- | --- | --- |\n| A | Do thing | |\n",
        encoding="utf-8",
    )
    if with_json:
        items = extract_action_items_json(_SUMMARY_WITH_JSON)
        (folder / "action-items.json").write_text(
            json.dumps({"items": items}), encoding="utf-8"
        )
    (folder / "transcript.json").write_text(
        json.dumps(
            {
                "segments": [
                    {"speaker": "Me", "start": 745.0, "end": 752.0,
                     "text": "we need live context for the dossier"},
                    {"speaker": "SPEAKER_1", "start": 2000.0, "end": 2004.0,
                     "text": "unrelated later talk"},
                ]
            }
        ),
        encoding="utf-8",
    )
    return folder


def test_load_action_items_prefers_json(tmp_path: Path) -> None:
    folder = _make_meeting(tmp_path)
    items = load_action_items(folder)
    assert items[0]["project"] == "linkedin-enrich"


def test_load_action_items_table_fallback(tmp_path: Path) -> None:
    folder = _make_meeting(tmp_path, with_json=False)
    items = load_action_items(folder)
    assert items == [
        {
            "id": "ai-1", "owner": "A", "task": "Do thing", "due": "",
            "kind": "other", "project": None,
            "evidence": {"segments": [], "frames": []},
            "state_relation": "",
        }
    ]


# --- target resolution --------------------------------------------------------
def _store(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    ccr = root / ".ccr" / "branches" / "main"
    ccr.mkdir(parents=True)
    (root / ".ccr" / "main.md").write_text(
        "# Project Context\n\n## Current Focus\nX\n\n## Recent Milestones\n"
        "- [2026-08-01 10:00] (main) did X\n",
        encoding="utf-8",
    )
    (ccr / "commits.md").write_text("", encoding="utf-8")
    return root


def test_resolve_target_store_by_tree_name(tmp_path: Path) -> None:
    store = _store(tmp_path, "enrich-frontend")
    cfg = Config(
        meetings_dir=tmp_path / "m",
        gladia_api_key="k",
        register_trees={"linkedin-enrich": {"stores": [str(store)]}},
    )
    assert resolve_target_store(cfg, "linkedin-enrich") == store
    assert resolve_target_store(cfg, "LinkedIn Enrich") == store
    assert resolve_target_store(cfg, "unknown-project") is None
    assert resolve_target_store(cfg, None) is None


# --- spec generation ----------------------------------------------------------
_SPEC_DOC = f"""{SPEC_MARKER}: Build the dossier prototype

## Goal
Do it.

## Current state
C150 exists.

## Approach
1. Step one.

## Files & areas likely touched
- somewhere

## Acceptance criteria
- works

## Open questions
- none
"""


class FakeRunner:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, prompt, options):
        self.calls.append((prompt, options))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _cfg(tmp_path: Path, **kw) -> Config:
    return Config(meetings_dir=tmp_path / "meetings", gladia_api_key="k", **kw)


def test_generate_spec_writes_doc_and_meta(tmp_path: Path) -> None:
    folder = _make_meeting(tmp_path)
    runner = FakeRunner(["preamble\n" + _SPEC_DOC + "\n" + _SPEC_DOC])
    cfg = _cfg(tmp_path)

    path = generate_spec(folder, "ai-1", cfg, session_runner=runner, sleep=lambda s: None)

    text = path.read_text(encoding="utf-8")
    assert text.startswith(SPEC_MARKER)
    assert text.count(SPEC_MARKER) == 1  # deduped repeated document
    meta = json.loads((folder / "specs" / "ai-1.json").read_text(encoding="utf-8"))
    assert meta["task"] == "Build the dossier prototype"
    assert spec_status(folder, "ai-1")["status"] == "done"
    assert not (folder / "specs" / "ai-1.running").exists()

    # prompt grounded in evidence: quote + transcript excerpt around 12:30
    prompt = runner.calls[0][0]
    assert "we need live context" in prompt
    assert "unrelated later talk" not in prompt  # outside the excerpt window
    assert "Build the dossier prototype" in prompt


def test_generate_spec_failure_leaves_err(tmp_path: Path) -> None:
    folder = _make_meeting(tmp_path)
    runner = FakeRunner(["no marker here at all"])
    cfg = _cfg(tmp_path)

    with pytest.raises(AnalysisError):
        generate_spec(folder, "ai-1", cfg, session_runner=runner, sleep=lambda s: None)
    status = spec_status(folder, "ai-1")
    assert status["status"] == "error"
    assert "Build Spec" in status["error"]
    assert not (folder / "specs" / "ai-1.running").exists()


def test_generate_spec_unknown_item(tmp_path: Path) -> None:
    folder = _make_meeting(tmp_path)
    with pytest.raises(AnalysisError, match="no action item"):
        generate_spec(
            folder, "ai-99", _cfg(tmp_path),
            session_runner=FakeRunner([]), sleep=lambda s: None,
        )


def test_spec_status_none(tmp_path: Path) -> None:
    folder = _make_meeting(tmp_path)
    assert spec_status(folder, "ai-1") == {"status": "none"}
