"""Spike F: FULL live pipeline — Gladia + merge + Claude + CCR, no mocks.

Builds a meeting folder from fixture WAVs (converted to the production FLAC
layout with a synthetic timing index and the spike-D frames), state=recorded,
then runs the production run_pipeline end to end.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from recoder.config import load_config
from recoder.pipeline.runner import run_pipeline
from recoder.store import MeetingStore
from spike_d_live_e2e import FRAMES, make_frame

ROOT = Path(__file__).resolve().parent.parent


def wav_to_flac(src: Path, dst: Path) -> tuple[int, float]:
    data, rate = sf.read(src)
    sf.write(dst, data, rate, format="FLAC")
    return rate, len(data) / rate


def write_timing_index(path: Path, entries: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


def main() -> int:
    config = load_config()
    store = MeetingStore(config)
    meeting = store.create_meeting(
        "Full live test - export planning",
        "planning sync about the billing export feature and recoder storage choices",
    )

    w0 = time.time()
    index: list[dict] = []
    for ch, src in (("mic", "mic.wav"), ("system", "system.wav")):
        dst = meeting.audio_mic if ch == "mic" else meeting.audio_system
        rate, dur = wav_to_flac(ROOT / "tests" / "fixtures" / src, dst)
        index.append({"ch": ch, "event": "start", "wall": w0})
        t = 5.0
        while t < dur:
            index.append({"ch": ch, "frames_written": int(rate * t), "wall": w0 + t})
            t += 5.0
        index.append({"ch": ch, "frames_written": int(rate * dur), "wall": w0 + dur})
        index.append({"ch": ch, "event": "stop", "wall": w0 + dur})
    write_timing_index(meeting.timing_index, index)

    frame_index = []
    for fname, lines in FRAMES:
        make_frame(meeting.frames_dir / fname, lines)
        frame_index.append({"file": fname, "wall": w0 + 10, "window_title": "Zoom Meeting", "fallback_fullscreen": False})
    (meeting.frames_dir / "index.jsonl").write_text(
        "\n".join(json.dumps(e) for e in frame_index) + "\n", encoding="utf-8"
    )

    meeting.update_meta(state="recorded")
    print(f"Meeting folder: {meeting.folder}")
    print("Running FULL live pipeline (Gladia x2 + merge + Claude + CCR)...")

    t0 = time.monotonic()
    result = run_pipeline(meeting.folder, config)
    took = time.monotonic() - t0

    transcript = json.loads(meeting.transcript_json.read_text(encoding="utf-8"))
    speakers = sorted({s["speaker"] for s in transcript["segments"]})
    summary = meeting.summary_md.read_text(encoding="utf-8") if meeting.summary_md.exists() else ""
    print(f"\nstate={result.state.value} took={took:.0f}s segments={len(transcript['segments'])} speakers={speakers} summary_chars={len(summary)}")

    ok = (
        result.state.value == "committed"
        and "Me" in speakers
        and any(s.startswith("SPEAKER_") for s in speakers)
        and "## Action Items" in summary
    )
    print("PASS: full live pipeline works." if ok else "FAIL: see above.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
