"""Screen snapshot capture for the recoder capture layer (spec §4.1).

Captures the meeting window region every ``snapshot_interval_s`` seconds,
downscales, perceptually deduplicates against the last saved frame, and writes
JPEGs plus an ``index.jsonl`` sidecar.

Design contract (spec §5): a snapshot failure must *never* disturb recording.
Every per-tick error is caught, counted and logged; nothing propagates out of
the capture thread.

The window-finder and the frame grabber are injectable callables so the whole
capturer is exercisable in tests without touching a real screen.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NamedTuple, Optional

import imagehash
from PIL import Image

logger = logging.getLogger(__name__)


class Region(NamedTuple):
    """A screen rectangle in virtual-desktop coordinates."""

    left: int
    top: int
    width: int
    height: int


class WindowMatch(NamedTuple):
    """A located meeting window: its clamped region plus its title."""

    region: Region
    title: str


# ---------------------------------------------------------------------------
# Window location
# ---------------------------------------------------------------------------


def _virtual_screen_bounds() -> Region:
    """Bounds of the whole virtual desktop (all monitors) via mss monitor 0."""

    import mss  # lazy: only needed on a real screen

    with mss.mss() as sct:
        mon = sct.monitors[0]
    return Region(mon["left"], mon["top"], mon["width"], mon["height"])


def _clamp_region(region: Region, bounds: Region) -> Optional[Region]:
    """Clamp ``region`` to ``bounds``; return None if nothing remains visible."""

    b_right = bounds.left + bounds.width
    b_bottom = bounds.top + bounds.height

    left = max(region.left, bounds.left)
    top = max(region.top, bounds.top)
    right = min(region.left + region.width, b_right)
    bottom = min(region.top + region.height, b_bottom)

    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        return None
    return Region(left, top, width, height)


def _match_window(patterns: list[str]) -> Optional[WindowMatch]:
    """First visible, non-minimized window whose title matches any pattern.

    Matching is case-insensitive substring. The returned region is clamped to
    the virtual-desktop bounds; a window entirely off-screen yields None.
    """

    import pygetwindow  # lazy: only needed on a real screen

    lowered = [p.lower() for p in patterns]
    try:
        windows = pygetwindow.getAllWindows()
    except Exception:  # pragma: no cover - platform dependent
        logger.exception("enumerating windows failed")
        return None

    bounds = _virtual_screen_bounds()

    for win in windows:
        title = getattr(win, "title", "") or ""
        if not title:
            continue
        if getattr(win, "isMinimized", False):
            continue
        if not getattr(win, "visible", True):
            continue
        if win.width <= 0 or win.height <= 0:
            continue
        title_l = title.lower()
        if not any(pat in title_l for pat in lowered):
            continue
        raw = Region(int(win.left), int(win.top), int(win.width), int(win.height))
        clamped = _clamp_region(raw, bounds)
        if clamped is None:
            continue
        return WindowMatch(clamped, title)

    return None


def find_meeting_window(patterns: list[str]) -> Optional[Region]:
    """Public API: region of the first matching meeting window, or None.

    See :func:`_match_window` for matching semantics.
    """

    match = _match_window(patterns)
    return match.region if match is not None else None


# ---------------------------------------------------------------------------
# Default frame grabber
# ---------------------------------------------------------------------------


def _default_grabber(region: Optional[Region]) -> Image.Image:
    """Grab ``region`` (or primary monitor when None) via mss into a PIL image."""

    import mss  # lazy: only needed on a real screen

    with mss.mss() as sct:
        if region is None:
            monitor = sct.monitors[1]  # primary physical monitor
        else:
            monitor = {
                "left": region.left,
                "top": region.top,
                "width": region.width,
                "height": region.height,
            }
        raw = sct.grab(monitor)
    return Image.frombytes("RGB", raw.size, raw.rgb)


# ---------------------------------------------------------------------------
# Capturer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SnapshotResult:
    frames_saved: int
    frames_skipped_dup: int
    ticks: int
    errors: int


WindowFinder = Callable[[list[str]], Optional[WindowMatch]]
Grabber = Callable[[Optional[Region]], Image.Image]


class SnapshotCapturer:
    """Periodic screen-snapshot capturer for a single meeting.

    Parameters
    ----------
    frames_dir:
        Directory to write JPEG frames and ``index.jsonl`` into.
    config:
        A :class:`recoder.config.Config` (only the snapshot-related fields are
        read: ``snapshot_interval_s``, ``phash_hamming_threshold``,
        ``jpeg_quality``, ``max_frame_width``, ``window_title_patterns``).
    window_finder / grabber:
        Injectable callables. Defaults hit the real screen; tests supply their
        own so no real display is required.
    """

    def __init__(
        self,
        frames_dir: Path,
        config,
        *,
        window_finder: Optional[WindowFinder] = None,
        grabber: Optional[Grabber] = None,
    ) -> None:
        self._frames_dir = Path(frames_dir)
        self._frames_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._frames_dir / "index.jsonl"

        self._interval = config.snapshot_interval_s
        self._threshold = config.phash_hamming_threshold
        self._jpeg_quality = config.jpeg_quality
        self._max_width = config.max_frame_width
        self._patterns = list(config.window_title_patterns)

        self._window_finder: WindowFinder = window_finder or _match_window
        self._grabber: Grabber = grabber or _default_grabber

        # State (mutated only under _lock or from the single capture thread).
        self._last_hash: Optional[imagehash.ImageHash] = None
        self._seq = 0
        self.frames_saved = 0
        self.frames_skipped_dup = 0
        self.ticks = 0
        self.errors = 0

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- public API ---------------------------------------------------------

    @property
    def saved_count(self) -> int:
        return self.frames_saved

    def start(self) -> None:
        """Begin capturing on a background daemon thread."""

        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="snapshot-capturer", daemon=True
        )
        self._thread.start()

    def stop(self) -> SnapshotResult:
        """Stop capturing, join the thread, and return the tally."""

        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self._interval + 5)
            self._thread = None
        return SnapshotResult(
            frames_saved=self.frames_saved,
            frames_skipped_dup=self.frames_skipped_dup,
            ticks=self.ticks,
            errors=self.errors,
        )

    # -- internals ----------------------------------------------------------

    def _run(self) -> None:
        # Immediate first capture, then every interval until stopped.
        while not self._stop_event.is_set():
            self._tick()
            if self._stop_event.wait(self._interval):
                break

    def _tick(self) -> None:
        """One capture cycle. Never raises (spec §5)."""

        self.ticks += 1
        try:
            match = self._window_finder(self._patterns)
            if match is not None:
                region: Optional[Region] = match.region
                title: Optional[str] = match.title
                fallback = False
            else:
                region = None
                title = None
                fallback = True

            image = self._grabber(region)
            image = self._downscale(image)

            frame_hash = imagehash.phash(image)
            if (
                self._last_hash is not None
                and (frame_hash - self._last_hash) <= self._threshold
            ):
                self.frames_skipped_dup += 1
                return

            self._save(image, title, fallback)
            self._last_hash = frame_hash
            self.frames_saved += 1
        except Exception:
            self.errors += 1
            logger.exception("snapshot tick failed; recording continues")

    def _downscale(self, image: Image.Image) -> Image.Image:
        """Downscale to width <= max_frame_width, preserving aspect ratio."""

        if image.width <= self._max_width:
            return image
        ratio = self._max_width / image.width
        new_height = max(1, round(image.height * ratio))
        return image.resize((self._max_width, new_height), Image.LANCZOS)

    def _save(self, image: Image.Image, title: Optional[str], fallback: bool) -> None:
        wall = time.time()
        stamp = time.strftime("%H%M%S", time.localtime(wall))
        filename = f"{self._seq:06d}_{stamp}.jpg"
        self._seq += 1

        rgb = image if image.mode == "RGB" else image.convert("RGB")
        with self._lock:
            rgb.save(
                self._frames_dir / filename,
                format="JPEG",
                quality=self._jpeg_quality,
            )
            entry = {
                "file": filename,
                "wall": wall,
                "window_title": title,
                "fallback_fullscreen": fallback,
            }
            with self._index_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
