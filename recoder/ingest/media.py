"""Frame extraction from a Fathom recording's HLS stream (spec §4.6).

For a meeting the user did not attend, the notetaker's recording is the ONLY
visual record that exists — so when someone shared a screen, those frames are
worth real effort. When nobody did, they are worth nothing: Fathom renders the
call's gallery view, and a meeting with no screen-share yields nothing but
webcam tiles.

The two cases cost wildly different amounts (a 32-minute recording is ~330 MB
and takes minutes to stream), so extraction is decided, not assumed:

  1. **Survey** — the HLS playlist is a flat list of independently addressable
     ~1 MB ``.ts`` chunks, so a handful spread across the recording can be
     fetched *in parallel* and decoded standalone. Ten chunks of a half-hour
     meeting costs about 8 seconds and 1 MB.
  2. **Triage** — those ten frames are montaged into one survey sheet for
     :mod:`recoder.ingest.triage` to look at.
  3. **Extract** — only if the survey found screen content. Otherwise the
     survey frames themselves are kept as a visual reference and the full
     stream is never downloaded.

Measured on real recordings: pixel statistics do NOT separate the two cases
(a dark-themed dashboard and a bright webcam wall land on the same side of
every threshold tried), which is why triage looks at the sheet instead of
computing a score. Perceptual dedupe is left exactly where it is — during a
screen-share consecutive frames are near-identical and collapse correctly,
whereas webcam gallery frames defeat it entirely.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin

__all__ = [
    "MediaError",
    "ffmpeg_available",
    "parse_playlist",
    "survey_chunks",
    "build_survey_sheet",
    "extract_frames",
    "write_frame_index",
]

# One frame per chunk is plenty for a survey: chunks are ~6s of video.
SURVEY_CHUNKS = 10
SURVEY_WORKERS = 10
SURVEY_TIMEOUT_S = 120
EXTRACT_TIMEOUT_S = 60 * 60  # a long meeting streams for many minutes
_CHUNK_TIMEOUT_S = 60

SHEET_COLS = 5
SHEET_TILE_W = 420
SHEET_LABEL_H = 18
SHEET_QUALITY = 85


class MediaError(RuntimeError):
    """The recording's video could not be read."""


def _no_window() -> dict:
    """Keep ffmpeg from flashing a console window on Windows."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _require_ffmpeg() -> None:
    if not ffmpeg_available():
        raise MediaError(
            "ffmpeg is not on PATH — it is required to extract frames from a "
            "Fathom recording. Install it (winget install Gyan.FFmpeg) or "
            "ingest with frames disabled."
        )


def parse_playlist(playlist: str, playlist_url: str) -> list[str]:
    """Absolute URLs of every media chunk in an HLS playlist, in order."""
    chunks: list[str] = []
    for line in playlist.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        chunks.append(urljoin(playlist_url, line))
    return chunks


def fetch_playlist(playlist_url: str, *, http_client=None) -> list[str]:
    """Download and parse the HLS playlist for ``playlist_url``."""
    from recoder.ingest.fathom import _HEADERS  # shared UA

    close = False
    if http_client is None:
        import httpx

        http_client = httpx.Client(timeout=60.0)
        close = True
    try:
        response = http_client.get(
            playlist_url, headers=_HEADERS, follow_redirects=True
        )
        if response.status_code >= 400:
            raise MediaError(
                f"could not read the recording's playlist "
                f"(HTTP {response.status_code})"
            )
        text = response.text
    except MediaError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise MediaError(f"could not read the recording's playlist ({exc})") from exc
    finally:
        if close:
            http_client.close()

    chunks = parse_playlist(text, playlist_url)
    if not chunks:
        raise MediaError("the recording's playlist contained no media chunks")
    return chunks


def _grab_frame(source: str, dest: Path, timeout: float) -> bool:
    """Decode ONE frame from ``source`` into ``dest``. Never raises."""
    cmd = [
        "ffmpeg", "-v", "error",
        "-i", source,
        "-frames:v", "1", "-q:v", "4",
        "-y", str(dest),
    ]
    try:
        subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            **_no_window(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return dest.exists() and dest.stat().st_size > 0


def survey_chunks(
    chunks: list[str],
    dest_dir: Path,
    *,
    count: int = SURVEY_CHUNKS,
    chunk_seconds: float = 6.0,
) -> list[dict]:
    """Decode one frame from each of ``count`` chunks spread across the video.

    Chunks are independent transport-stream segments, so each decodes on its
    own and they can all be fetched at once. Returns
    ``[{file, offset_s, index}, ...]`` for the frames that decoded — a chunk
    that fails is skipped rather than sinking the survey.
    """
    _require_ffmpeg()
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    if not chunks:
        return []

    total = len(chunks)
    picks = sorted({
        min(total - 1, max(0, round((i + 0.5) * total / count)))
        for i in range(min(count, total))
    })

    def _one(index: int) -> dict | None:
        name = f"survey-{index:05d}.jpg"
        if not _grab_frame(chunks[index], dest_dir / name, _CHUNK_TIMEOUT_S):
            return None
        return {
            "file": name,
            "index": index,
            "offset_s": round(index * chunk_seconds, 1),
        }

    with ThreadPoolExecutor(max_workers=min(SURVEY_WORKERS, len(picks))) as pool:
        results = list(pool.map(_one, picks))
    return [r for r in results if r is not None]


def _fmt_mmss(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    return f"{total // 60:02d}:{total % 60:02d}"


def build_survey_sheet(entries: list[dict], dest_dir: Path) -> Path | None:
    """Montage the survey frames into ONE labelled sheet for triage."""
    from PIL import Image, ImageDraw, ImageFont

    dest_dir = Path(dest_dir)
    if not entries:
        return None

    try:
        font = ImageFont.load_default(size=13)
    except TypeError:  # older Pillow
        font = ImageFont.load_default()

    tiles: list[tuple[dict, object]] = []
    tile_h = 1
    for entry in entries:
        try:
            with Image.open(dest_dir / entry["file"]) as img:
                scale = SHEET_TILE_W / img.width
                tile = img.convert("RGB").resize(
                    (SHEET_TILE_W, max(1, int(img.height * scale)))
                )
        except OSError:
            continue
        tile_h = max(tile_h, tile.height)
        tiles.append((entry, tile))
    if not tiles:
        return None

    rows = (len(tiles) + SHEET_COLS - 1) // SHEET_COLS
    cell_h = tile_h + SHEET_LABEL_H
    sheet = Image.new(
        "RGB", (SHEET_COLS * SHEET_TILE_W, rows * cell_h), (18, 18, 22)
    )
    draw = ImageDraw.Draw(sheet)
    for i, (entry, tile) in enumerate(tiles):
        x = (i % SHEET_COLS) * SHEET_TILE_W
        y = (i // SHEET_COLS) * cell_h
        sheet.paste(tile, (x, y))
        draw.text(
            (x + 4, y + tile_h + 2),
            f"{_fmt_mmss(entry['offset_s'])}  ({entry['file']})",
            fill=(232, 232, 238),
            font=font,
        )

    path = dest_dir / "survey-sheet.jpg"
    sheet.save(path, format="JPEG", quality=SHEET_QUALITY)
    return path


def extract_frames(
    playlist_url: str,
    dest_dir: Path,
    *,
    interval_s: int = 20,
    max_width: int = 1568,
    quality: int = 80,
    timeout_s: int = EXTRACT_TIMEOUT_S,
) -> list[Path]:
    """Stream the whole recording, writing one JPEG every ``interval_s``.

    This is the expensive path (the entire video is downloaded), so it only
    runs once triage has found screen content worth having.
    """
    _require_ffmpeg()
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    pattern = dest_dir / "raw-%05d.jpg"
    interval = max(1, int(interval_s))

    cmd = [
        "ffmpeg", "-v", "error",
        "-i", playlist_url,
        "-vf", f"fps=1/{interval},scale='min({max_width},iw)':-2",
        "-q:v", str(max(2, min(31, 31 - quality // 4))),
        "-y", str(pattern),
    ]
    try:
        completed = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            check=False,
            **_no_window(),
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaError(
            f"extracting frames timed out after {timeout_s}s"
        ) from exc
    except OSError as exc:
        raise MediaError(f"could not run ffmpeg ({exc})") from exc

    frames = sorted(dest_dir.glob("raw-*.jpg"))
    if not frames:
        detail = (completed.stderr or b"").decode("utf-8", "replace").strip()
        raise MediaError(
            "no frames could be extracted from the recording"
            + (f": {detail[:200]}" if detail else "")
        )
    return frames


def dedupe_frames(frames: list[Path], threshold: int) -> list[Path]:
    """Drop frames perceptually identical to the previous kept one.

    Uses the same perceptual hash and threshold as live capture. During a
    screen-share this collapses long static stretches to a single frame; it is
    deliberately NOT relied on to filter webcam gallery frames, which it
    cannot do (that is triage's job, before we ever get here).
    """
    import imagehash
    from PIL import Image

    kept: list[Path] = []
    last = None
    for path in frames:
        try:
            with Image.open(path) as img:
                current = imagehash.phash(img)
        except OSError:
            continue
        if last is not None and (current - last) <= threshold:
            path.unlink(missing_ok=True)
            continue
        kept.append(path)
        last = current
    return kept


def write_frame_index(
    frames: list[Path],
    frames_dir: Path,
    *,
    t0: float,
    interval_s: float,
    offsets: list[float] | None = None,
    source: str = "fathom",
) -> list[dict]:
    """Rename frames to the capture convention and write ``index.jsonl``.

    The wall clock is synthesized as ``t0 + offset`` — the recording's start
    plus the frame's position in the video — which is exactly what
    :mod:`recoder.analysis.evidence` needs to place a frame on the transcript
    timeline. That is the whole adapter: no other downstream code knows or
    cares that these frames came from a video rather than a screen grab.
    """
    frames_dir = Path(frames_dir)
    frames_dir.mkdir(parents=True, exist_ok=True)
    index_path = frames_dir / "index.jsonl"

    entries: list[dict] = []
    with index_path.open("w", encoding="utf-8") as fh:
        for seq, path in enumerate(frames):
            if offsets is not None and seq < len(offsets):
                offset = float(offsets[seq])
            else:
                offset = seq * float(interval_s)
            wall = t0 + offset
            stamp = time.strftime("%H%M%S", time.localtime(wall))
            name = f"{seq:06d}_{stamp}.jpg"
            target = frames_dir / name
            if path.resolve() != target.resolve():
                path.replace(target)
            entry = {
                "file": name,
                "wall": wall,
                "window_title": None,
                "fallback_fullscreen": False,
                "source": source,
                "presenting": source == "fathom",
            }
            fh.write(json.dumps(entry) + "\n")
            entries.append(entry)
    return entries
