from __future__ import annotations

from recoder.web.desktop import allow_close


class _FakeManager:
    def __init__(self, recording: bool) -> None:
        self._recording = recording

    def status(self) -> dict:
        return {"recording": self._recording}


def test_allow_close_when_idle() -> None:
    assert allow_close(_FakeManager(recording=False)) is True


def test_block_close_while_recording() -> None:
    assert allow_close(_FakeManager(recording=True)) is False


def test_desktop_module_imports_without_gui() -> None:
    import recoder.web.desktop as desktop

    assert callable(desktop.run_desktop)


def test_suppress_child_consoles_children_still_work() -> None:
    import subprocess
    import sys

    from recoder.web.desktop import _suppress_child_consoles

    _suppress_child_consoles()
    _suppress_child_consoles()  # idempotent — no double wrapping
    proc = subprocess.Popen(
        [sys.executable, "-c", "print('ok')"],
        stdout=subprocess.PIPE,
        text=True,
    )
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 0
    assert out.strip() == "ok"
    assert getattr(subprocess.Popen.__init__, "_recoder_no_window", False)
