"""Unit tests for recoder.capture.snapshots.

All tests are screen-free: the window-finder and grabber are injected, and
test images are synthesised in-memory with PIL/numpy (no fixture files).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from recoder.capture import snapshots
from recoder.capture.snapshots import (
    Region,
    SnapshotCapturer,
    WindowMatch,
    find_meeting_window,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_config(**overrides):
    base = dict(
        snapshot_interval_s=20,
        phash_hamming_threshold=4,
        jpeg_quality=80,
        max_frame_width=1568,
        window_title_patterns=["Zoom", "Meet", "Teams"],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# Rich low-frequency sinusoidal patterns give well-conditioned perceptual
# hashes (unlike flat/gradient synthetics, whose DCT coefficients cluster near
# the median and flip wildly under tiny perturbations). These recipes yield
# stable, verified phash distances used by the dedup tests below.
_W = _H = 256
_YY, _XX = np.mgrid[0:_H, 0:_W]


def _to_img(arr) -> Image.Image:
    return Image.fromarray(
        np.clip(arr, 0, 255).astype(np.uint8), mode="L"
    ).convert("RGB")


def img_base() -> Image.Image:
    return _to_img(
        128 + 40 * np.sin(_XX / 30) + 40 * np.cos(_YY / 25)
        + 30 * np.sin((_XX + _YY) / 40)
    )


def img_distinct() -> Image.Image:
    """Perceptually far from img_base (verified phash distance ~32)."""
    return _to_img(128 + 40 * np.sin(_XX / 12) + 40 * np.cos(_YY / 9))


def img_distinct2() -> Image.Image:
    """Perceptually far from both img_base and img_distinct."""
    return _to_img(128 + 50 * np.sin(_XX / 8) + 30 * np.cos((_XX - _YY) / 15))


def img_near_base(seed=2) -> Image.Image:
    """Faint-noise near-copy of img_base (verified phash distance ~4)."""
    arr = (
        128 + 40 * np.sin(_XX / 30) + 40 * np.cos(_YY / 25)
        + 30 * np.sin((_XX + _YY) / 40)
    )
    arr = arr + np.random.default_rng(seed).normal(0, 1.5, (_H, _W))
    return _to_img(arr)


def sequence_grabber(images):
    """Grabber that returns the next image per tick, ignoring the region."""
    it = iter(images)

    def grab(region):
        return next(it)

    return grab


def finder_from(matches):
    """Window-finder that returns the next WindowMatch|None per tick."""
    it = iter(matches)

    def find(patterns):
        return next(it)

    return find


def always_match(title="Zoom Meeting"):
    def find(patterns):
        return WindowMatch(Region(0, 0, 256, 256), title)

    return find


def always_none(patterns):
    return None


# ---------------------------------------------------------------------------
# dedup behaviour
# ---------------------------------------------------------------------------


def test_identical_images_deduped(tmp_path):
    base = img_base()
    cap = SnapshotCapturer(
        tmp_path,
        make_config(),
        window_finder=always_match(),
        grabber=sequence_grabber([base, base.copy()]),
    )
    cap._tick()
    cap._tick()
    assert cap.frames_saved == 1
    assert cap.frames_skipped_dup == 1


def test_distinct_images_kept(tmp_path):
    imgs = [img_base(), img_distinct()]
    # sanity: the two patterns are genuinely far apart perceptually
    import imagehash

    dist = imagehash.phash(imgs[0]) - imagehash.phash(imgs[1])
    assert dist > 4

    cap = SnapshotCapturer(
        tmp_path,
        make_config(),
        window_finder=always_match(),
        grabber=sequence_grabber(imgs),
    )
    cap._tick()
    cap._tick()
    assert cap.frames_saved == 2
    assert cap.frames_skipped_dup == 0


def test_near_identical_noise_deduped(tmp_path):
    base = img_base()
    noisy = img_near_base()
    # sanity: within the dedup threshold but not literally identical
    import imagehash

    dist = imagehash.phash(base) - imagehash.phash(noisy)
    assert 0 < dist <= 4

    cap = SnapshotCapturer(
        tmp_path,
        make_config(),
        window_finder=always_match(),
        grabber=sequence_grabber([base, noisy]),
    )
    cap._tick()
    cap._tick()
    assert cap.frames_saved == 1
    assert cap.frames_skipped_dup == 1


# ---------------------------------------------------------------------------
# downscale
# ---------------------------------------------------------------------------


def test_downscale_respects_max_width_and_aspect(tmp_path):
    cap = SnapshotCapturer(tmp_path, make_config(max_frame_width=50))
    big = Image.new("RGB", (200, 100))
    out = cap._downscale(big)
    assert out.width == 50
    assert out.height == 25  # aspect preserved (200:100 -> 50:25)

    # already-small images are untouched (no upscaling)
    small = Image.new("RGB", (40, 20))
    assert cap._downscale(small).size == (40, 20)


# ---------------------------------------------------------------------------
# filenames + index
# ---------------------------------------------------------------------------


def test_filename_sequencing(tmp_path):
    imgs = [img_base(), img_distinct(), img_distinct2()]
    cap = SnapshotCapturer(
        tmp_path,
        make_config(),
        window_finder=always_match(),
        grabber=sequence_grabber(imgs),
    )
    for _ in imgs:
        cap._tick()

    files = sorted(p.name for p in tmp_path.glob("*.jpg"))
    assert len(files) == 3
    assert files[0].startswith("000000_")
    assert files[1].startswith("000001_")
    assert files[2].startswith("000002_")
    assert all(name.endswith(".jpg") for name in files)


def test_index_jsonl_correctness_with_fallback(tmp_path):
    # tick 1: matched window (title set, not fallback)
    # tick 2: no window -> fallback fullscreen (title null)
    matches = [WindowMatch(Region(0, 0, 256, 256), "Zoom Meeting"), None]
    imgs = [img_base(), img_distinct()]
    cap = SnapshotCapturer(
        tmp_path,
        make_config(),
        window_finder=finder_from(matches),
        grabber=sequence_grabber(imgs),
    )
    cap._tick()
    cap._tick()

    lines = (tmp_path / "index.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    e0 = json.loads(lines[0])
    e1 = json.loads(lines[1])

    assert e0["window_title"] == "Zoom Meeting"
    assert e0["fallback_fullscreen"] is False
    assert e0["file"].startswith("000000_")
    assert isinstance(e0["wall"], float)

    assert e1["window_title"] is None
    assert e1["fallback_fullscreen"] is True
    assert e1["file"].startswith("000001_")


# ---------------------------------------------------------------------------
# error handling + result math
# ---------------------------------------------------------------------------


def test_tick_exception_swallowed_and_counted(tmp_path):
    def boom(region):
        raise RuntimeError("grab failed")

    cap = SnapshotCapturer(
        tmp_path,
        make_config(),
        window_finder=always_match(),
        grabber=boom,
    )
    cap._tick()  # must not raise
    assert cap.errors == 1
    assert cap.frames_saved == 0
    assert cap.ticks == 1
    assert not list(tmp_path.glob("*.jpg"))


def test_stop_result_math(tmp_path):
    # save, dup, error, save  -> ticks=4 = saved(2)+skipped(1)+errors(1)
    base = img_base()
    other = img_distinct()

    calls = {"n": 0}
    plan = [base, base.copy(), "boom", other]

    def grab(region):
        item = plan[calls["n"]]
        calls["n"] += 1
        if item == "boom":
            raise RuntimeError("boom")
        return item

    cap = SnapshotCapturer(
        tmp_path,
        make_config(),
        window_finder=always_match(),
        grabber=grab,
    )
    for _ in plan:
        cap._tick()

    result = cap.stop()
    assert result.frames_saved == 2
    assert result.frames_skipped_dup == 1
    assert result.errors == 1
    assert result.ticks == 4
    assert result.ticks == (
        result.frames_saved + result.frames_skipped_dup + result.errors
    )
    assert cap.saved_count == 2


# ---------------------------------------------------------------------------
# window location
# ---------------------------------------------------------------------------


def test_find_meeting_window_matching_and_clamping(monkeypatch):
    class FakeWin:
        def __init__(self, title, left, top, width, height,
                     minimized=False, visible=True):
            self.title = title
            self.left = left
            self.top = top
            self.width = width
            self.height = height
            self.isMinimized = minimized
            self.visible = visible

    windows = [
        FakeWin("Desktop", 0, 0, 100, 100),
        FakeWin("Zoom Meeting", 10, 20, 5000, 5000),  # oversized -> clamped
        FakeWin("Some Teams chat", 0, 0, 300, 300, minimized=True),
    ]

    fake_pgw = SimpleNamespace(getAllWindows=lambda: windows)
    monkeypatch.setitem(__import__("sys").modules, "pygetwindow", fake_pgw)
    monkeypatch.setattr(
        snapshots, "_virtual_screen_bounds",
        lambda: Region(0, 0, 1920, 1080),
    )

    region = find_meeting_window(["zoom", "teams"])
    assert region is not None
    # matched the Zoom window (case-insensitive), clamped to the screen
    assert region.left == 10 and region.top == 20
    assert region.left + region.width == 1920
    assert region.top + region.height == 1080

    # no match -> None
    assert find_meeting_window(["nonexistent"]) is None
