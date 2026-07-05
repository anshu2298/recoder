from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from recoder.analysis import routing
from recoder.analysis.routing import (
    ProjectEntry,
    is_junk,
    is_prunable,
    load_registry,
    route_projects,
)

NOW = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _days_ago(n: int) -> datetime:
    return NOW - timedelta(days=n)


def _write_registry(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    return path


def _entry(name: str, path: str, commits: int = 5, days: int = 1) -> ProjectEntry:
    return ProjectEntry(
        name=name, path=path, commit_count=commits, last_used=_days_ago(days)
    )


# --- registry parsing ---------------------------------------------------------
def test_load_registry_parses_fixture(tmp_path: Path) -> None:
    rows = [
        {
            "path": r"G:\current_working\sherpa",
            "name": "sherpa",
            "last_used": "2026-07-03T07:52:00+00:00",
            "commit_count": 26,
        }
    ]
    path = _write_registry(tmp_path, rows)

    entries = load_registry(path)

    assert len(entries) == 1
    e = entries[0]
    assert e.name == "sherpa"
    assert e.path == r"G:\current_working\sherpa"
    assert e.commit_count == 26
    assert e.last_used == datetime(2026, 7, 3, 7, 52, 0, tzinfo=timezone.utc)


def test_load_registry_tolerates_missing_and_bad_fields(tmp_path: Path) -> None:
    rows = [
        {"path": "G:\\x", "name": "x"},  # no commit_count / last_used
        {"path": "G:\\y", "name": "y", "commit_count": "not-a-number"},
        {"path": "G:\\z", "name": "z", "last_used": "garbage-timestamp"},
    ]
    path = _write_registry(tmp_path, rows)

    entries = load_registry(path)

    assert [e.commit_count for e in entries] == [0, 0, 0]
    assert entries[0].last_used is None
    assert entries[2].last_used is None


def test_corrupt_registry_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "projects.json"
    path.write_text("{ this is not valid json ]", encoding="utf-8")

    assert load_registry(path) == []
    assert routing.read_raw_registry(path) == []


def test_missing_registry_returns_empty(tmp_path: Path) -> None:
    assert load_registry(tmp_path / "nope.json") == []


# --- junk filtering -----------------------------------------------------------
def test_is_junk_filters_node_modules_and_pycache_and_sitepackages() -> None:
    assert is_junk(_entry("sdk", r"G:\proj\node_modules\@anthropic-ai\sdk"))
    assert is_junk(_entry("__pycache__", r"G:\proj\tools\__pycache__"))
    assert is_junk(_entry("pkg", r"C:\py\Lib\site-packages\pkg"))
    assert is_junk(_entry("skill", r"G:\proj\.claude\skills\artifact"))


def test_is_junk_filters_meeting_folders() -> None:
    assert is_junk(
        _entry(
            "2026-07-05-1447-weekly-sync",
            r"G:\recoder\meetings\2026-07-05-1447-weekly-sync",
        )
    )
    # The bare meetings dir itself.
    assert is_junk(_entry("meetings", r"G:\recoder\meetings"))


def test_is_junk_filters_empty_name_and_drive_roots() -> None:
    assert is_junk(_entry("", r"G:\something"))
    assert is_junk(_entry("   ", r"G:\something"))
    assert is_junk(_entry("", "G:\\"))
    assert is_junk(_entry("", "C:\\"))
    assert is_junk(_entry("root", "G:/"))


def test_is_junk_passes_real_project() -> None:
    assert not is_junk(_entry("sherpa", r"G:\current_working\sherpa"))


# --- recency window -----------------------------------------------------------
def test_recency_window_includes_recent_excludes_old() -> None:
    entries = [
        _entry("recent", r"G:\a\recent", commits=3, days=2),
        _entry("stale", r"G:\a\stale", commits=3, days=40),
    ]
    routed = route_projects(entries, "unrelated title", "", now=NOW, recency_days=7)

    paths = {r.path for r in routed}
    assert r"G:\a\recent" in paths
    assert r"G:\a\stale" not in paths
    assert "used 2d ago" in next(r.reason for r in routed if r.name == "recent")


def test_zero_commit_entries_never_mounted() -> None:
    entries = [_entry("empty", r"G:\a\empty", commits=0, days=1)]
    assert route_projects(entries, "empty", "", now=NOW) == []


# --- keyword matching ---------------------------------------------------------
def test_keyword_match_multi_word_title() -> None:
    entries = [
        # keyword only (old), matches "billing"
        _entry("billing-service", r"G:\old\billing-service", commits=4, days=90),
        # unrelated + old -> excluded
        _entry("weather", r"G:\old\weather", commits=4, days=90),
    ]
    routed = route_projects(
        entries, "Weekly billing sync with Rahul", "", now=NOW, recency_days=7
    )

    names = {r.name for r in routed}
    assert "billing-service" in names
    assert "weather" not in names
    assert "matched 'billing'" in next(r.reason for r in routed).lower()


def test_keyword_ignores_short_words() -> None:
    # "the", "of" (<4 chars) must not trigger a match.
    entries = [_entry("the-of-app", r"G:\old\the-of-app", commits=4, days=90)]
    routed = route_projects(entries, "the of a in", "", now=NOW)
    assert routed == []


def test_keyword_matches_final_path_component() -> None:
    entries = [
        ProjectEntry(
            name="frontend",
            path=r"G:\current_working\retryqueue\frontend",
            commit_count=10,
            last_used=_days_ago(90),
        )
    ]
    # "frontend" is the final component; a title word must match it.
    routed = route_projects(entries, "frontend polish", "", now=NOW)
    assert len(routed) == 1


# --- ordering + cap -----------------------------------------------------------
def test_preference_order_keyword_recent_then_recent_then_keyword() -> None:
    entries = [
        # keyword only, old
        _entry("sherpa-old", r"G:\a\sherpa-old", commits=4, days=100),
        # recent only, newest
        _entry("newest", r"G:\a\newest", commits=4, days=1),
        # keyword + recent
        _entry("sherpa-live", r"G:\a\sherpa-live", commits=4, days=3),
    ]
    routed = route_projects(
        entries, "sherpa planning", "", now=NOW, recency_days=7, max_mounts=4
    )

    order = [r.name for r in routed]
    assert order.index("sherpa-live") < order.index("newest")
    assert order.index("newest") < order.index("sherpa-old")


def test_recent_group_sorted_newest_first() -> None:
    entries = [
        _entry("older", r"G:\a\older", commits=4, days=5),
        _entry("newer", r"G:\a\newer", commits=4, days=1),
    ]
    routed = route_projects(entries, "no keywords here", "", now=NOW, recency_days=7)
    assert [r.name for r in routed] == ["newer", "older"]


def test_max_mounts_cap() -> None:
    entries = [
        _entry(f"p{i}", rf"G:\a\p{i}", commits=4, days=i + 1) for i in range(6)
    ]
    routed = route_projects(entries, "no keywords", "", now=NOW, max_mounts=2)
    assert len(routed) == 2


def test_dedupe_by_path() -> None:
    entries = [
        _entry("dup", r"G:\a\dup", commits=4, days=2),
        _entry("dup", r"G:\a\dup\\", commits=4, days=2),  # same path, trailing slash
    ]
    routed = route_projects(entries, "no keywords", "", now=NOW)
    assert len(routed) == 1


# --- recoder self-exclusion ---------------------------------------------------
def test_recoder_self_exclusion() -> None:
    entries = [
        _entry("recoder", r"G:\recoder", commits=10, days=1),
        _entry("sherpa", r"G:\current_working\sherpa", commits=10, days=1),
    ]
    routed = route_projects(
        entries,
        "sherpa sync",
        "",
        now=NOW,
        exclude_paths=(r"G:\recoder",),
    )
    paths = {routing.norm_path(r.path) for r in routed}
    assert routing.norm_path(r"G:\recoder") not in paths
    assert routing.norm_path(r"G:\current_working\sherpa") in paths


# --- prune predicate (memory-clean) -------------------------------------------
def test_is_prunable_junk_and_stale_empty() -> None:
    assert is_prunable(_entry("x", r"G:\a\node_modules\x"), now=NOW)
    # empty store unused > 30 days
    assert is_prunable(_entry("dead", r"G:\a\dead", commits=0, days=40), now=NOW)
    # empty but recent -> keep
    assert not is_prunable(_entry("fresh", r"G:\a\fresh", commits=0, days=5), now=NOW)
    # has commits -> keep even if old
    assert not is_prunable(_entry("live", r"G:\a\live", commits=9, days=90), now=NOW)
