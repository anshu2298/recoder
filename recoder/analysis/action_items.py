"""Extract structured data back out of a finished summary.md.

The analysis session emits ``## Action Items`` as a markdown table with
columns Owner | Task | Due (see prompts.REQUIRED_SECTIONS). The web UI wants
those rows as JSON, so this module parses them back out. Pure functions, no
I/O; tolerant of a missing section or a malformed table (returns []).
"""

from __future__ import annotations

import re

__all__ = ["extract_section", "extract_action_items"]

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
