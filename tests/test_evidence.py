"""Tests for contact sheets + transcript↔frame evidence linking."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from recoder.analysis.evidence import (
    build_contact_sheets,
    build_evidence,
    ensure_evidence,
)
from recoder.analysis.prompts import build_analysis_prompt, render_frame_table

T0 = 1_000_000.0


def _make_meeting(tmp_path: Path, n_frames: int = 20) -> Path:
    folder = tmp_path / "meeting"
    frames = folder / "frames"
    frames.mkdir(parents=True)

    (folder / "timing.jsonl").write_text(
        json.dumps({"ch": "mic", "event": "start", "wall": T0}) + "\n"
        + json.dumps({"ch": "system", "event": "start", "wall": T0}) + "\n",
        encoding="utf-8",
    )

    index_lines = []
    for i in range(n_frames):
        name = f"{i:06d}_x.jpg"
        Image.new("RGB", (640, 360), (i * 10 % 255, 80, 120)).save(
            frames / name, format="JPEG"
        )
        index_lines.append(
            json.dumps(
                {
                    "file": name,
                    "wall": T0 + 20.0 * i,  # one frame every 20s
                    "window_title": "Meet",
                    "fallback_fullscreen": i == 0,
                    "source": "window",
                    "presenting": False,
                }
            )
        )
    (frames / "index.jsonl").write_text(
        "\n".join(index_lines) + "\n", encoding="utf-8"
    )

    (folder / "transcript.json").write_text(
        json.dumps(
            {
                "segments": [
                    {"speaker": "Me", "start": 18.0, "end": 21.0, "text": "look at this slide"},
                    {"speaker": "SPEAKER_1", "start": 55.0, "end": 59.0, "text": "the billing chart"},
                ],
                "source": "test",
            }
        ),
        encoding="utf-8",
    )
    return folder


def test_build_evidence_offsets_and_speech(tmp_path: Path) -> None:
    folder = _make_meeting(tmp_path, n_frames=4)
    entries = build_evidence(folder)
    assert len(entries) == 4
    # frame 1 is at wall T0+20 -> offset 20s -> "00:20"; segment at 18-21s links
    assert entries[1]["offset_s"] == 20.0
    assert entries[1]["mmss"] == "00:20"
    assert "look at this slide" in entries[1]["speech"]
    assert entries[1]["speech"].startswith("Me:")
    # frame 3 at offset 60s links the 55-59s segment
    assert "billing chart" in entries[3]["speech"]
    # frame 0's fallback flag carried through
    assert entries[0]["fallback_fullscreen"] is True
    assert (folder / "evidence.json").exists()


def test_build_evidence_without_timing_is_calm(tmp_path: Path) -> None:
    folder = _make_meeting(tmp_path, n_frames=2)
    (folder / "timing.jsonl").unlink()
    entries = build_evidence(folder)
    assert len(entries) == 2
    assert entries[0]["offset_s"] is None
    assert entries[0]["speech"] == ""


def test_contact_sheets_grid_and_index(tmp_path: Path) -> None:
    folder = _make_meeting(tmp_path, n_frames=20)
    entries = build_evidence(folder)
    sheets = build_contact_sheets(folder, entries)

    # 20 frames at 16/sheet -> 2 sheets
    assert [s["file"] for s in sheets] == ["sheet-01.jpg", "sheet-02.jpg"]
    assert len(sheets[0]["frames"]) == 16
    assert len(sheets[1]["frames"]) == 4
    assert sheets[0]["range"].startswith("00:00-")

    sheet1 = Image.open(folder / "frames" / "sheets" / "sheet-01.jpg")
    assert sheet1.width > 4 * 300  # four columns wide
    assert (folder / "frames" / "sheets" / "sheets.json").exists()


def test_ensure_evidence_idempotent(tmp_path: Path) -> None:
    folder = _make_meeting(tmp_path, n_frames=3)
    entries1, sheets1 = ensure_evidence(folder)
    stamp = (folder / "frames" / "sheets" / "sheet-01.jpg").stat().st_mtime_ns
    entries2, sheets2 = ensure_evidence(folder)
    assert entries2 == entries1
    assert sheets2 == sheets1
    # sheet not regenerated
    assert (
        folder / "frames" / "sheets" / "sheet-01.jpg"
    ).stat().st_mtime_ns == stamp


def test_ensure_evidence_rebuilds_on_new_frames(tmp_path: Path) -> None:
    folder = _make_meeting(tmp_path, n_frames=3)
    ensure_evidence(folder)
    # append one more frame to the index
    frames = folder / "frames"
    Image.new("RGB", (640, 360), (5, 5, 5)).save(frames / "extra.jpg")
    with (frames / "index.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"file": "extra.jpg", "wall": T0 + 500}) + "\n")
    entries, _ = ensure_evidence(folder)
    assert len(entries) == 4


def test_ensure_evidence_no_frames(tmp_path: Path) -> None:
    folder = tmp_path / "empty"
    folder.mkdir()
    entries, sheets = ensure_evidence(folder)
    assert entries == []
    assert sheets == []


def test_prompt_includes_sheets_and_speech(tmp_path: Path) -> None:
    folder = _make_meeting(tmp_path, n_frames=4)
    entries, sheets = ensure_evidence(folder)
    inventory = [
        json.loads(line)
        for line in (folder / "frames" / "index.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    prompt = build_analysis_prompt(
        {"title": "T"},
        "[00:00] Me: hi",
        inventory,
        60.0,
        evidence=entries,
        sheets=sheets,
    )
    assert "READ THESE FIRST" in prompt
    assert "frames/sheets/sheet-01.jpg" in prompt
    assert "look at this slide" in prompt  # nearby speech in the frame table
    assert "| 00:20 |" in prompt


def test_frame_table_without_evidence_still_renders() -> None:
    table = render_frame_table([{"file": "a.jpg", "window_title": "Meet"}])
    assert "a.jpg" in table
    assert "Nearby speech" in table
