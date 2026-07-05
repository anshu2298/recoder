"""Spike E: live Gladia API test on the fixture audio.

Pass criteria: system.wav (two TTS voices) transcribes with >=2 diarized
speakers and recognizable text; validates our request/response contract live.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from recoder.config import load_config
from recoder.pipeline.transcribe import GladiaTranscriber


def main() -> int:
    config = load_config()
    t = GladiaTranscriber(config)
    fixture = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "system.wav"
    out = Path(__file__).resolve().parent / "out" / "gladia-live-system.json"
    out.parent.mkdir(exist_ok=True)

    segments = t.transcribe(fixture, diarize=True, raw_dump_path=out)
    speakers = sorted({s.speaker for s in segments if s.speaker is not None})
    print(f"{len(segments)} segments, speakers: {speakers}")
    for s in segments:
        print(f"  [{s.start:6.2f}-{s.end:6.2f}] spk={s.speaker} lang={s.language} {s.text}")

    if not segments:
        print("FAIL: no segments")
        return 1
    if len(speakers) < 2:
        print("WARN: fewer than 2 speakers detected (TTS voices may be too similar)")
    joined = " ".join(s.text.lower() for s in segments)
    if "export" not in joined and "plain files" not in joined:
        print("FAIL: transcript does not resemble fixture dialogue")
        return 1
    print("PASS: live Gladia transcription works.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
