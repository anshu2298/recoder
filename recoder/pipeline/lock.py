"""Per-meeting pipeline lock (crash-safe, pid-verified).

The pipeline for one meeting folder must never run twice concurrently — the
app's startup sweep, the manual Retry button and the detached `recoder
process` child could otherwise race. The lock is a ``pipeline.lock`` JSON file
in the meeting folder holding the owner's pid and process create-time.

Liveness is *verified against the OS*, not assumed from the file's existence:
a lock whose pid is gone (or whose pid was recycled by another process, which
the create-time check catches) is stale and silently reclaimable. A crash
therefore never wedges a meeting — the next `run_pipeline` walks right over
the dead lock.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

__all__ = ["LockHeld", "acquire", "release", "is_live", "LOCK_NAME"]

LOCK_NAME = "pipeline.lock"

# Tolerance when comparing the recorded create-time with the live process's
# (float rounding across psutil calls).
_CREATE_TIME_SLACK_S = 2.0


class LockHeld(RuntimeError):
    """Another live process is already running this meeting's pipeline."""


def _lock_path(folder: Path | str) -> Path:
    return Path(folder) / LOCK_NAME


def _proc_create_time(pid: int) -> float | None:
    """Create-time of a running process, or None if it does not exist."""
    try:
        import psutil

        return float(psutil.Process(pid).create_time())
    except Exception:  # noqa: BLE001 - NoSuchProcess/AccessDenied/etc -> gone
        return None


def is_live(folder: Path | str) -> bool:
    """True iff the folder's lock file names a process that is still alive."""
    try:
        data = json.loads(_lock_path(folder).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    pid = data.get("pid")
    if not isinstance(pid, int):
        return False
    live_ct = _proc_create_time(pid)
    if live_ct is None:
        return False  # owner died -> stale
    recorded_ct = data.get("create_time")
    if (
        isinstance(recorded_ct, (int, float))
        and abs(live_ct - float(recorded_ct)) > _CREATE_TIME_SLACK_S
    ):
        return False  # pid recycled by an unrelated process -> stale
    return True


def acquire(folder: Path | str) -> Path:
    """Claim the folder's pipeline lock for this process.

    Raises :class:`LockHeld` if a *live* owner exists; stale locks are
    overwritten. Returns the lock path.
    """
    path = _lock_path(folder)
    if is_live(folder):
        raise LockHeld(
            f"pipeline for {Path(folder).name} is already running "
            f"(see {LOCK_NAME})"
        )
    payload = {
        "pid": os.getpid(),
        "create_time": _proc_create_time(os.getpid()),
        "started": time.time(),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def release(folder: Path | str) -> None:
    """Remove the folder's lock file (missing file is fine)."""
    try:
        _lock_path(folder).unlink()
    except OSError:
        pass
