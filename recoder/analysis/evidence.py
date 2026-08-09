"""Contact sheets + transcript↔frame evidence linking (pre-analyze, local).

Two deterministic artifacts are derived from a diarized meeting before the
Claude analysis session runs — no model calls, pure Pillow/JSON:

  * ``frames/sheets/sheet-NN.jpg`` — 4x4 contact-sheet montages of every
    captured frame, each tile stamped with its ``[MM:SS]`` offset and frame
    number. The analyst reads a handful of sheets to *see* the whole meeting,
    then opens only the full-res frames worth opening. (Borrowed from the
    "contact sheets → frames → stills" hierarchy of video-analysis skills;
    without sheets, frame selection is blind guessing over a filename table.)
  * ``evidence.json`` — every frame joined to the speech around it: the
    frame's wall clock minus the mic-start wall clock (``timing.jsonl``)
    gives its offset on the transcript timeline; segments within a window
    around that offset become the frame's "nearby speech" snippet.

Both are idempotent (existing artifacts for the same frame count are kept) so
re-running analysis is free.
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

__all__ = ["ensure_evidence", "build_evidence", "build_contact_sheets"]

GRID_COLS = 4
GRID_ROWS = 4
TILE_W = 370  # 4 tiles + gutters stays within Claude's ~1568px sweet spot
LABEL_H = 22
GUTTER = 6
SHEET_QUALITY = 78
SPEECH_WINDOW_S = 25.0  # speech within +/- this many seconds of the frame
SPEECH_MAX_CHARS = 220


def _fmt_mmss(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    return f"{total // 60:02d}:{total % 60:02d}"


def _mic_start_wall(meeting_folder: Path) -> float | None:
    """Wall clock of the mic channel's start event — the transcript's t=0."""
    timing = meeting_folder / "timing.jsonl"
    try:
        lines = timing.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("ch") == "mic" and entry.get("event") == "start":
            try:
                return float(entry["wall"])
            except (KeyError, TypeError, ValueError):
                return None
    return None


def _load_jsonl(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _load_segments(meeting_folder: Path) -> list[dict]:
    try:
        data = json.loads(
            (meeting_folder / "transcript.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return []
    segments = data.get("segments") if isinstance(data, dict) else data
    return segments if isinstance(segments, list) else []


def _speech_near(segments: list[dict], offset_s: float) -> str:
    """Speaker-labeled snippet of the speech within the window of ``offset_s``."""
    parts: list[str] = []
    for seg in segments:
        try:
            start = float(seg.get("start", 0.0))
            end = float(seg.get("end", start))
        except (TypeError, ValueError):
            continue
        if end < offset_s - SPEECH_WINDOW_S or start > offset_s + SPEECH_WINDOW_S:
            continue
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        parts.append(f"{seg.get('speaker') or '?'}: {text}")
    snippet = " ".join(parts)
    if len(snippet) > SPEECH_MAX_CHARS:
        snippet = snippet[: SPEECH_MAX_CHARS - 1].rstrip() + "…"
    return snippet


def build_evidence(meeting_folder: Path) -> list[dict]:
    """Join every frame to its timeline offset and nearby speech.

    Writes ``evidence.json`` and returns the entries. Frames whose offset
    cannot be established (no timing file) get ``offset_s: null`` and no
    speech link — the sheet still renders with a sequence label.
    """
    meeting_folder = Path(meeting_folder)
    inventory = _load_jsonl(meeting_folder / "frames" / "index.jsonl")
    t0 = _mic_start_wall(meeting_folder)
    segments = _load_segments(meeting_folder)

    entries: list[dict] = []
    for i, frame in enumerate(inventory):
        entry: dict = {
            "file": str(frame.get("file") or ""),
            "seq": i,
            "offset_s": None,
            "mmss": "",
            "speech": "",
            "source": frame.get("source") or "window",
            "fallback_fullscreen": bool(frame.get("fallback_fullscreen")),
        }
        wall = frame.get("wall")
        if t0 is not None and isinstance(wall, (int, float)):
            offset = max(0.0, float(wall) - t0)
            entry["offset_s"] = round(offset, 1)
            entry["mmss"] = _fmt_mmss(offset)
            entry["speech"] = _speech_near(segments, offset)
        entries.append(entry)

    (meeting_folder / "evidence.json").write_text(
        json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return entries


def _label_font() -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=13)
    except TypeError:  # older Pillow: no size kwarg
        return ImageFont.load_default()


def build_contact_sheets(
    meeting_folder: Path, entries: list[dict]
) -> list[dict]:
    """Montage frames into 4x4 sheets under ``frames/sheets/``.

    Each tile is the frame scaled to ``TILE_W`` with a label bar underneath:
    ``[MM:SS] #seq`` (plus ``FALLBACK``/source markers where relevant).
    Returns a sheet index (also written to ``frames/sheets/sheets.json``):
    ``[{file, frames: [...], range: "MM:SS-MM:SS"}, ...]``.
    """
    meeting_folder = Path(meeting_folder)
    frames_dir = meeting_folder / "frames"
    sheets_dir = frames_dir / "sheets"
    sheets_dir.mkdir(parents=True, exist_ok=True)
    font = _label_font()

    per_sheet = GRID_COLS * GRID_ROWS
    sheet_index: list[dict] = []
    for sheet_no, start in enumerate(range(0, len(entries), per_sheet), 1):
        batch = entries[start : start + per_sheet]

        # Scale every tile, tracking the tallest to size rows uniformly.
        tiles: list[tuple[dict, Image.Image | None]] = []
        max_h = 1
        for entry in batch:
            path = frames_dir / entry["file"]
            try:
                with Image.open(path) as img:
                    scale = TILE_W / img.width
                    tile = img.convert("RGB").resize(
                        (TILE_W, max(1, int(img.height * scale)))
                    )
            except OSError:
                tile = None
            if tile is not None:
                max_h = max(max_h, tile.height)
            tiles.append((entry, tile))

        cell_h = max_h + LABEL_H
        rows = (len(batch) + GRID_COLS - 1) // GRID_COLS
        sheet = Image.new(
            "RGB",
            (
                GRID_COLS * TILE_W + (GRID_COLS + 1) * GUTTER,
                rows * cell_h + (rows + 1) * GUTTER,
            ),
            (18, 18, 22),
        )
        draw = ImageDraw.Draw(sheet)
        for i, (entry, tile) in enumerate(tiles):
            col, row = i % GRID_COLS, i // GRID_COLS
            x = GUTTER + col * (TILE_W + GUTTER)
            y = GUTTER + row * (cell_h + GUTTER)
            if tile is not None:
                sheet.paste(tile, (x, y))
            label = f"[{entry['mmss'] or '--:--'}] #{entry['seq']:03d}"
            if entry.get("fallback_fullscreen"):
                label += "  FALLBACK"
            src = str(entry.get("source") or "")
            if src.startswith("monitor"):
                label += f"  {src}"
            draw.text(
                (x + 3, y + max_h + 4), label, fill=(230, 230, 235), font=font
            )

        sheet_file = f"sheet-{sheet_no:02d}.jpg"
        sheet.save(
            sheets_dir / sheet_file, format="JPEG", quality=SHEET_QUALITY
        )
        stamps = [e["mmss"] for e in batch if e["mmss"]]
        sheet_index.append(
            {
                "file": sheet_file,
                "frames": [e["file"] for e in batch],
                "range": f"{stamps[0]}-{stamps[-1]}" if stamps else "",
            }
        )

    (sheets_dir / "sheets.json").write_text(
        json.dumps(sheet_index, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return sheet_index


def ensure_evidence(meeting_folder: Path) -> tuple[list[dict], list[dict]]:
    """Build (or reuse) evidence + contact sheets for a meeting.

    Idempotent: if ``evidence.json`` and ``frames/sheets/sheets.json`` already
    cover the current frame count, the cached artifacts are returned. Never
    raises — an empty result simply means no frames.
    """
    meeting_folder = Path(meeting_folder)
    inventory = _load_jsonl(meeting_folder / "frames" / "index.jsonl")
    evidence_path = meeting_folder / "evidence.json"
    sheets_path = meeting_folder / "frames" / "sheets" / "sheets.json"

    try:
        if evidence_path.exists() and sheets_path.exists():
            entries = json.loads(evidence_path.read_text(encoding="utf-8"))
            sheets = json.loads(sheets_path.read_text(encoding="utf-8"))
            if isinstance(entries, list) and len(entries) == len(inventory):
                return entries, sheets if isinstance(sheets, list) else []
    except (OSError, json.JSONDecodeError):
        pass

    try:
        entries = build_evidence(meeting_folder)
        sheets = build_contact_sheets(meeting_folder, entries)
        return entries, sheets
    except Exception:  # noqa: BLE001 - evidence must never sink analysis
        return [], []
