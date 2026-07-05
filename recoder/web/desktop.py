"""Native desktop window for the Recoder UI.

Runs the FastAPI server in-process (daemon thread) and hosts the existing
web UI in a pywebview window (Edge WebView2). Closing the window exits the
app — unless a recording is in progress, in which case the close is blocked
until the user presses Stop.
"""

from __future__ import annotations

import socket
import threading
import time

from recoder.config import Config
from recoder.web.recording import RecordingManager


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
