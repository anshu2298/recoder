from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from recoder.pipeline.merge import Segment, merge_channels, write_transcript
from recoder.pipeline.transcribe import RawSegment


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _write_flac(path: Path, samplerate: int = 16000, seconds: float = 3.0) -> Path:
    data = np.zeros(int(samplerate * seconds), dtype="float32")
    sf.write(str(path), data, samplerate, format="FLAC")
    return path


def _write_timing(path: Path, entries: list[dict]) -> Path:
    path.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )
    return path


@pytest.fixture
def flacs(tmp_path: Path) -> tuple[Path, Path]:
    mic = _write_flac(tmp_path / "audio-mic.flac")
    system = _write_flac(tmp_path / "audio-system.flac")
    return mic, system


# --------------------------------------------------------------------------
# Wall alignment across channels with different starts
# --------------------------------------------------------------------------


def test_wall_alignment_different_start_times(tmp_path: Path, flacs) -> None:
    mic, system = flacs
    # mic file-time t -> wall 100+t ; system file-time t -> wall 102+t
    timing = _write_timing(
        tmp_path / "timing.jsonl",
        [
            {"ch": "mic", "event": "start", "wall": 100.0},
            {"ch": "mic", "frames_written": 16000, "wall": 101.0},
            {"ch": "mic", "frames_written": 32000, "wall": 102.0},
            {"ch": "system", "event": "start", "wall": 102.0},
            {"ch": "system", "frames_written": 16000, "wall": 103.0},
            {"ch": "system", "frames_written": 32000, "wall": 104.0},
        ],
    )
    mic_segs = [RawSegment(None, 0.5, 1.0, "hi from me", "en")]
    sys_segs = [RawSegment(0, 0.5, 1.0, "hi from them", "en")]

    merged = merge_channels(mic_segs, sys_segs, timing, mic, system)

    # meeting t=0 at the earlier (mic) start = wall 100.
    assert merged[0].speaker == "Me"
    assert merged[0].start == pytest.approx(0.5)
    assert merged[1].speaker == "SPEAKER_1"
    assert merged[1].start == pytest.approx(2.5)
    # sorted by start
    assert merged[0].start < merged[1].start


def test_gap_in_index_handled(tmp_path: Path, flacs) -> None:
    mic, system = flacs
    timing = _write_timing(
        tmp_path / "timing.jsonl",
        [
            {"ch": "mic", "event": "start", "wall": 100.0},
            {"ch": "mic", "frames_written": 16000, "wall": 101.0},
            {"ch": "mic", "event": "gap", "wall": 101.5},
            # 1s of file-time spanned 4s of wall due to the gap
            {"ch": "mic", "frames_written": 32000, "wall": 105.0},
            {"ch": "system", "event": "start", "wall": 100.0},
            {"ch": "system", "frames_written": 16000, "wall": 101.0},
            {"ch": "system", "frames_written": 32000, "wall": 102.0},
        ],
    )
    mic_segs = [RawSegment(None, 1.5, 1.6, "after gap", "en")]
    merged = merge_channels(mic_segs, [], timing, mic, system)

    # file-time 1.5 interpolates on the steep post-gap slope: 101 + 4*0.5 = 103.
    assert merged[0].start == pytest.approx(3.0)


def test_speaker_mapping_stability(tmp_path: Path, flacs) -> None:
    mic, system = flacs
    timing = _write_timing(
        tmp_path / "timing.jsonl",
        [
            {"ch": "system", "event": "start", "wall": 0.0},
            {"ch": "system", "frames_written": 16000, "wall": 1.0},
            {"ch": "system", "frames_written": 48000, "wall": 3.0},
        ],
    )
    # speakers appear over time in order 5, 2, 5, 9
    sys_segs = [
        RawSegment(5, 0.5, 0.9, "a", "en"),
        RawSegment(2, 1.0, 1.4, "b", "en"),
        RawSegment(5, 1.5, 1.9, "c", "en"),
        RawSegment(9, 2.0, 2.4, "d", "en"),
    ]
    merged = merge_channels([], sys_segs, timing, mic, system)

    labels = {seg.text: seg.speaker for seg in merged}
    assert labels["a"] == "SPEAKER_1"
    assert labels["b"] == "SPEAKER_2"
    assert labels["c"] == "SPEAKER_1"  # same raw speaker as "a"
    assert labels["d"] == "SPEAKER_3"


def test_sparse_index_fallback(tmp_path: Path, flacs) -> None:
    mic, system = flacs
    # Only start events -> <2 frame points per channel -> start-offset fallback.
    timing = _write_timing(
        tmp_path / "timing.jsonl",
        [
            {"ch": "mic", "event": "start", "wall": 48.0},
            {"ch": "system", "event": "start", "wall": 50.0},
        ],
    )
    mic_segs = [RawSegment(None, 0.0, 1.0, "me", "en")]
    sys_segs = [RawSegment(0, 1.0, 2.0, "them", "en")]
    merged = merge_channels(mic_segs, sys_segs, timing, mic, system)

    # meeting t=0 at mic start (wall 48). mic 0.0 -> 0.0; system 1.0 -> 51-48=3.0
    by_speaker = {s.speaker: s for s in merged}
    assert by_speaker["Me"].start == pytest.approx(0.0)
    assert by_speaker["SPEAKER_1"].start == pytest.approx(3.0)


def test_missing_timing_file_degrades_gracefully(tmp_path: Path, flacs) -> None:
    mic, system = flacs
    missing = tmp_path / "nope.jsonl"
    mic_segs = [RawSegment(None, 0.0, 1.0, "me", "en")]
    merged = merge_channels(mic_segs, [], missing, mic, system)
    assert merged[0].speaker == "Me"
    assert merged[0].start == pytest.approx(0.0)


# --------------------------------------------------------------------------
# Output rendering
# --------------------------------------------------------------------------


def test_write_transcript_json_schema_and_markdown(tmp_path: Path) -> None:
    segments = [
        Segment("Me", 5.0, 7.0, "hello there", "en"),
        Segment("SPEAKER_1", 75.0, 78.0, "namaste", "hi"),
    ]
    json_path = tmp_path / "transcript.json"
    md_path = tmp_path / "transcript.md"

    write_transcript(segments, json_path, md_path, source="gladia")

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert set(payload.keys()) == {"segments", "source", "generated_at"}
    assert payload["source"] == "gladia"
    assert payload["generated_at"]
    assert payload["segments"][0] == {
        "speaker": "Me",
        "start": 5.0,
        "end": 7.0,
        "text": "hello there",
        "language": "en",
    }

    md = md_path.read_text(encoding="utf-8").splitlines()
    assert md[0] == "[00:05] Me: hello there"
    assert md[1] == "[01:15] SPEAKER_1: namaste"
