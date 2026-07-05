"""Native desktop window for the Recoder UI.

Runs the FastAPI server in-process (daemon thread) and hosts the existing
web UI in a pywebview window (Edge WebView2). Closing the window exits the
app — unless a recording is in progress, in which case the close is blocked
until the user presses Stop.
"""

from __future__ import annotations

import os
import socket
import threading
import time

from recoder.config import Config
from recoder.web.recording import RecordingManager


def _suppress_child_consoles() -> None:
    """Stop console child processes from opening terminal windows.

    The desktop app runs under pythonw (no console). When the pipeline later
    spawns console programs — the Claude CLI during analysis, CCR MCP servers —
    Windows would give each one a visible console window. OR-ing
    CREATE_NO_WINDOW into every Popen from this process (asyncio subprocesses
    included — they route through subprocess.Popen on Windows) keeps them
    headless. Desktop mode only; the terminal CLI keeps normal behavior.
    """
    if os.name != "nt":
        return
    import subprocess

    flag = subprocess.CREATE_NO_WINDOW
    original = subprocess.Popen.__init__

    def patched(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | flag
        return original(self, *args, **kwargs)

    if getattr(subprocess.Popen.__init__, "_recoder_no_window", False):
        return  # already patched
    patched._recoder_no_window = True  # type: ignore[attr-defined]
    subprocess.Popen.__init__ = patched  # type: ignore[method-assign]


def _wait_for_port(host: str, port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.2)
    return False


def allow_close(manager: RecordingManager) -> bool:
    """Window may close only when no recording is running."""
    return not manager.status().get("recording", False)


def run_desktop(config: Config) -> int:
    import uvicorn
    import webview

    _suppress_child_consoles()

    from recoder.web.app import create_app

    host = "127.0.0.1"
    manager = RecordingManager(config)
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(config, manager=manager),
            host=host,
            port=config.port,
            log_level="warning",
        )
    )
    threading.Thread(target=server.run, daemon=True).start()
    if not _wait_for_port(host, config.port):
        print("Server failed to start; aborting.")
        return 1

    window = webview.create_window(
        "Recoder",
        f"http://{host}:{config.port}",
        width=1100,
        height=760,
        min_size=(720, 520),
    )

    def on_closing() -> bool:
        if allow_close(manager):
            return True
        # Recording in progress: keep the window (and the recording) alive.
        window.evaluate_js(
            "alert('Recording in progress — press Stop before closing.')"
        )
        return False

    window.events.closing += on_closing
    webview.start()
    return 0
