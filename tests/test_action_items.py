from __future__ import annotations

from recoder.analysis.action_items import extract_action_items, extract_section

SUMMARY = """# Meeting Summary

## TL;DR
Ship it.

## Discussion
Talked a lot.

## Decisions
Ship billing v2 on Friday.

## Action Items
| Owner | Task | Due |
| --- | --- | --- |
| Rahul | Fix invoice bug | Friday |
| Me | Update the migration doc | |

## Open Questions
None.

## Project Mapping
billing -> sherpa

## Speakers
| Speaker | Name | Evidence |
| --- | --- | --- |
| SPEAKER_1 | Rahul | addressed by name |
"""


def test_extract_section_body() -> None:
    body = extract_section(SUMMARY, "Decisions")
    assert body == "Ship billing v2 on Friday."


def test_extract_section_missing_returns_empty() -> None:
    assert extract_section(SUMMARY, "Budget") == ""
    assert extract_section("", "Decisions") == ""


def test_extract_action_items_happy_path() -> None:
    items = extract_action_items(SUMMARY)
    assert items == [
        {"owner": "Rahul", "task": "Fix invoice bug", "due": "Friday"},
        {"owner": "Me", "task": "Update the migration doc", "due": ""},
    ]


def test_extract_action_items_none_summary() -> None:
    assert extract_action_items(None) == []
    assert extract_action_items("") == []


def test_extract_action_items_no_section() -> None:
    assert extract_action_items("# Meeting Summary\n\n## TL;DR\nhi\n") == []


def test_extract_action_items_prose_instead_of_table() -> None:
    md = "## Action Items\nNobody committed to anything.\n\n## Open Questions\n"
    assert extract_action_items(md) == []


def test_extract_action_items_skips_placeholder_rows() -> None:
    md = (
        "## Action Items\n"
        "| Owner | Task | Due |\n"
        "| --- | --- | --- |\n"
        "| | none | |\n"
        "| Me | Real task | |\n"
    )
    items = extract_action_items(md)
    assert items == [{"owner": "Me", "task": "Real task", "due": ""}]


def test_extract_action_items_two_column_row_tolerated() -> None:
    md = "## Action Items\n| Owner | Task |\n| --- | --- |\n| Me | Do thing |\n"
    assert extract_action_items(md) == [{"owner": "Me", "task": "Do thing", "due": ""}]
