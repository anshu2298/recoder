"""Extract structured data back out of a finished summary.md.

The analysis session emits ``## Action Items`` as a markdown table with
columns Owner | Task | Due, plus ``## Action Items JSON`` — the same items as
a fenced JSON block with evidence refs (segments/frames), a build/coordination
kind, a target project, and a relation to the project's current state. The
JSON section is parsed and persisted to ``action-items.json`` by the analysis
stage and then stripped from the human-facing summary; the table remains the
regex fallback for meetings analyzed before the JSON contract existed.

Pure functions, no I/O; tolerant of missing sections and malformed content.
"""

from __future__ import annotations

import json
import re

__all__ = [
    "extract_section",
    "extract_action_items",
    "extract_action_items_json",
    "strip_action_items_json",
]

_HEADER_RE = re.compile(r"^##\s+", re.MULTILINE)


def extract_section(summary_md: str, header: str) -> str:
    """Return the body of ``## <header>`` up to the next ``##`` heading, or ""."""
    if not summary_md:
        return ""
    match = re.search(
        rf"^##\s+{re.escape(header)}\s*$", summary_md, flags=re.MULTILINE
    )
    if match is None:
        return ""
    body = summary_md[match.end():]
    nxt = _HEADER_RE.search(body)
    if nxt is not None:
        body = body[: nxt.start()]
    return body.strip()


def _split_row(line: str) -> list[str]:
    """Split one ``| a | b | c |`` markdown table row into stripped cells."""
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_separator(cells: list[str]) -> bool:
    return all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c)


def extract_action_items(summary_md: str | None) -> list[dict]:
    """Parse the ``## Action Items`` table into ``[{owner, task, due}, ...]``.

    Skips the header and separator rows and any row without a task. A missing
    summary, missing section, or non-table content all yield [].
    """
    section = extract_section(summary_md or "", "Action Items")
    if not section:
        return []

    items: list[dict] = []
    for line in section.splitlines():
        if "|" not in line:
            continue
        cells = _split_row(line)
        if len(cells) < 2 or _is_separator(cells):
            continue
        owner, task = cells[0], cells[1]
        due = cells[2] if len(cells) > 2 else ""
        if task.lower() == "task" and owner.lower() == "owner":
            continue  # header row
        if not task or task.lower() in {"none", "n/a", "-"}:
            continue
        items.append({"owner": owner, "task": task, "due": due})
    return items


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)
_AI_JSON_HEADER_RE = re.compile(
    r"^##\s+Action Items JSON\s*$", re.MULTILINE
)


def extract_action_items_json(summary_md: str | None) -> list[dict] | None:
    """Parse the ``## Action Items JSON`` fenced block into a list of items.

    Returns ``None`` (not ``[]``) when the section/fence is absent or the JSON
    is invalid, so callers can distinguish "old-format summary" from "the
    meeting genuinely had zero action items". Items are lightly normalized:
    non-dict entries dropped, ids backfilled, evidence dict guaranteed.
    """
    section = extract_section(summary_md or "", "Action Items JSON")
    if not section:
        return None
    fence = _JSON_FENCE_RE.search(section)
    payload = fence.group(1) if fence else section
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return None

    normalized: list[dict] = []
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            continue
        task = str(item.get("task") or "").strip()
        if not task:
            continue
        evidence = item.get("evidence")
        if not isinstance(evidence, dict):
            evidence = {}
        normalized.append(
            {
                "id": str(item.get("id") or f"ai-{i}"),
                "owner": str(item.get("owner") or "").strip(),
                "task": task,
                "due": str(item.get("due") or "").strip(),
                "kind": str(item.get("kind") or "other").strip() or "other",
                "project": (item.get("project") or None),
                "evidence": {
                    "segments": [
                        s for s in evidence.get("segments") or []
                        if isinstance(s, dict)
                    ],
                    "frames": [
                        str(f) for f in evidence.get("frames") or []
                    ],
                },
                "state_relation": str(item.get("state_relation") or "").strip(),
            }
        )
    return normalized


def strip_action_items_json(summary_md: str) -> str:
    """Remove the ``## Action Items JSON`` section from a summary document.

    The JSON is machine payload; once persisted to ``action-items.json`` it
    has no business in the human-facing summary. Removes from the header to
    the next ``##`` heading (or end of document).
    """
    match = _AI_JSON_HEADER_RE.search(summary_md)
    if match is None:
        return summary_md
    tail = summary_md[match.end():]
    nxt = _HEADER_RE.search(tail)
    end = match.end() + (nxt.start() if nxt else len(tail))
    return (summary_md[: match.start()].rstrip() + "\n" + summary_md[end:].lstrip("\n")).strip() + "\n"
