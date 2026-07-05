"""Spike C — unattended Claude Agent SDK + CCR MCP wiring.

Proves the most likely silent failure mode of the pipeline (spec 4.2): a
programmatic SDK session with the CCR MCP server wired into mcp_servers and
permission_mode="bypassPermissions" must complete a gcc_search tool call and a
Read of an image WITHOUT hanging on a tool-approval prompt. Subscription auth is
ambient via the installed `claude` CLI — no API key is set.

Pass criteria:
  - the run completes within the 180s timeout (no permission hang)
  - the final assembled text mentions both a search result and a color

Exits 0 on success, nonzero with a clear message on failure.

Usage:
    python spikes/spike_c_sdk.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from recoder.config import load_config  # noqa: E402

_cfg = load_config()
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
FIXTURE_IMG = FIXTURE_DIR / "test_image.jpg"
CWD = str(Path(__file__).resolve().parent.parent)
TIMEOUT_S = 180

CCR_COMMAND = _cfg.ccr_mcp_command
CCR_ARGS = list(_cfg.ccr_mcp_args)

PROMPT = (
    "Call gcc_search with query 'recoder design' and summarize the top result in "
    "one sentence. Then Read the file spikes/fixtures/test_image.jpg and state the "
    "dominant color."
)

_COLOR_WORDS = (
    "red",
    "green",
    "blue",
    "yellow",
    "orange",
    "purple",
    "pink",
    "black",
    "white",
    "gray",
    "grey",
    "cyan",
    "magenta",
    "brown",
    "color",
    "colour",
)


def make_fixture() -> None:
    from PIL import Image

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (200, 200), (255, 0, 0)).save(FIXTURE_IMG, "JPEG")


def _extract_text(message: object) -> str:
    parts: list[str] = []
    content = getattr(message, "content", None)
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
    result = getattr(message, "result", None)
    if isinstance(result, str):
        parts.append(result)
    return "\n".join(parts)


async def _run() -> str:
    from claude_agent_sdk import ClaudeAgentOptions, query

    options = ClaudeAgentOptions(
        mcp_servers={
            "ccr": {
                "type": "stdio",
                "command": CCR_COMMAND,
                "args": CCR_ARGS,
            }
        },
        permission_mode="bypassPermissions",
        allowed_tools=[
            "Read",
            "Glob",
            "mcp__ccr__gcc_search",
            "mcp__ccr__gcc_context",
        ],
        cwd=CWD,
    )

    collected: list[str] = []
    async for message in query(prompt=PROMPT, options=options):
        text = _extract_text(message)
        if text.strip():
            collected.append(text)
    return "\n".join(collected)


async def _main() -> int:
    if not FIXTURE_IMG.exists():
        make_fixture()
        print(f"Created fixture {FIXTURE_IMG}")

    try:
        from claude_agent_sdk import ClaudeAgentOptions, query  # noqa: F401
    except ImportError as exc:
        print(f"FAIL: claude-agent-sdk not installed ({exc}).")
        return 2

    try:
        transcript = await asyncio.wait_for(_run(), timeout=TIMEOUT_S)
    except asyncio.TimeoutError:
        print(
            f"FAIL: SDK session did not complete within {TIMEOUT_S}s "
            "(likely a permission-approval hang)."
        )
        return 3
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: SDK session error: {exc!r}")
        return 4

    lower = transcript.lower()
    mentions_search = ("recoder" in lower) or ("design" in lower) or ("search" in lower)
    mentions_color = any(word in lower for word in _COLOR_WORDS)

    print("---- session output ----")
    print(transcript.strip() or "(no text collected)")
    print("------------------------")

    if not mentions_search:
        print("FAIL: final text does not reference a search result.")
        return 5
    if not mentions_color:
        print("FAIL: final text does not mention a color.")
        return 6

    print("PASS: unattended SDK+CCR session completed, referenced search + color.")
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    sys.exit(main())
