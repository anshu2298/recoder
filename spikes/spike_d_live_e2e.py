"""Spike D: live end-to-end of analyze + commit stages.

Builds a meeting folder from the TTS fixture dialogue (transcription mocked —
Gladia key not yet available), generates fake screen-share frames, then runs
the REAL pipeline: Claude Agent SDK analysis + CCR commit-back.
Pass criteria: summary.md written with all required sections; state=committed.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from recoder.config import load_config
from recoder.pipeline.merge import Segment, write_transcript
from recoder.pipeline.runner import run_pipeline
from recoder.store import MeetingStore

SEGMENTS = [
    ("Me", 0.0, 6.0, "Thanks for joining. Today I want to review the recorder project timeline and the billing dashboard."),
    ("SPEAKER_1", 7.0, 12.5, "Sounds good. On my side, the client asked about exporting summaries to email every week."),
    ("Me", 13.5, 18.0, "I can take the action item to finish the transcription pipeline by Friday."),
    ("Me", 19.0, 24.5, "Let us also decide on the database. I propose we keep everything as plain files for now."),
    ("SPEAKER_2", 25.5, 31.0, "I disagree about plain files. We should at least consider SQLite for the archive index."),
    ("SPEAKER_1", 32.0, 37.5, "Okay, decision made. We start with plain files and revisit SQLite next month."),
    ("SPEAKER_2", 38.5, 43.0, "Fine. My action item is to send the export requirements document by Tuesday."),
]

FRAMES = [
    ("000001_140012.jpg", ["Billing Dashboard", "MRR: $4,200", "Churn: 3.1%", "Weekly export: NOT CONFIGURED"]),
    ("000002_140155.jpg", ["Recoder Roadmap", "[ ] Transcription pipeline - Friday", "[ ] Export requirements doc - Tuesday", "[x] Capture layer"]),
]


def make_frame(path: Path, lines: list[str]) -> None:
    img = Image.new("RGB", (1280, 720), (18, 22, 30))
    d = ImageDraw.Draw(img)
    d.rectangle([40, 40, 1240, 110], fill=(35, 90, 160))
    d.text((60, 62), lines[0], fill=(255, 255, 255))
    for i, line in enumerate(lines[1:]):
        d.text((80, 170 + i * 70), line, fill=(220, 225, 235))
    img.save(path, "JPEG", quality=80)


def main() -> int:
    config = load_config()
    store = MeetingStore(config)
    meeting = store.create_meeting("Weekly sync - recoder and billing", "weekly client sync about the recoder project and the billing dashboard export feature")

    segments = [Segment(speaker=s, start=a, end=b, text=t, language="en") for s, a, b, t in SEGMENTS]
    write_transcript(segments, meeting.transcript_json, meeting.transcript_md, source="fixture-mock")

    index_lines = []
    for fname, lines in FRAMES:
        make_frame(meeting.frames_dir / fname, lines)
        index_lines.append({"file": fname, "wall": time.time(), "window_title": "Zoom Meeting", "fallback_fullscreen": False})
    (meeting.frames_dir / "index.jsonl").write_text(
        "\n".join(json.dumps(e) for e in index_lines) + "\n", encoding="utf-8"
    )

    meeting.update_meta(state="diarized")
    print(f"Meeting folder ready: {meeting.folder}")
    print("Running live analyze + commit stages...")

    t0 = time.monotonic()
    result = run_pipeline(meeting.folder, config)
    took = time.monotonic() - t0

    summary = meeting.summary_md.read_text(encoding="utf-8") if meeting.summary_md.exists() else ""
    required = ["## TL;DR", "## Decisions", "## Action Items", "## Project Mapping", "## Speakers"]
    missing = [s for s in required if s not in summary]
    print(f"\nstate={result.state.value}  took={took:.0f}s  summary_chars={len(summary)}")
    if missing:
        print(f"FAIL: summary missing sections: {missing}")
        return 1
    if result.state.value != "committed":
        print(f"FAIL: expected state committed, got {result.state.value}")
        return 1
    print("PASS: live analyze + commit completed.")
    print("\n--- summary.md (first 2000 chars) ---")
    print(summary[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
