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
