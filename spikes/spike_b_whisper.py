"""Spike B — whisperX large-v3 on 4GB VRAM (int8, batch_size=4).

Loads whisperX large-v3 with compute_type="int8" and batch_size=4 on CUDA,
transcribes an audio file, prints the segments, peak VRAM and wall time; then
explicitly frees the transcription model (del + gc + empty_cache), loads the
English alignment model, aligns, and prints VRAM again.

This proves the hard VRAM plan from spec 4.2: models run strictly sequentially
and are unloaded between phases so the 4GB card does not OOM.

Pass criteria:
  - no CUDA OOM at any phase
  - non-empty transcript

Exits 0 on success, nonzero with a clear message on failure.

Usage:
    python spikes/spike_b_whisper.py [audio_file]
    # default audio_file: spikes/out/loopback.flac (from Spike A)
"""

from __future__ import annotations

import gc
import sys
import time
from pathlib import Path

DEFAULT_AUDIO = Path(__file__).resolve().parent / "out" / "loopback.flac"

MODEL = "large-v3"
COMPUTE_TYPE = "int8"
BATCH_SIZE = 4
DEVICE = "cuda"


def _vram_mb(torch) -> float:
    return torch.cuda.max_memory_allocated() / (1024 * 1024)


def main(audio_file: Path) -> int:
    try:
        import torch
        import whisperx
    except ImportError as exc:
        print(f"FAIL: ml extras not installed ({exc}). Run: uv sync --extra ml")
        return 2

    if not torch.cuda.is_available():
        print("FAIL: CUDA not available. Install the CUDA torch build.")
        return 3

    if not audio_file.exists():
        print(f"FAIL: audio file not found: {audio_file}")
        return 4

    torch.cuda.reset_peak_memory_stats()

    print(f"Loading whisperX {MODEL} (compute_type={COMPUTE_TYPE}) on {DEVICE}...")
    t0 = time.perf_counter()
    try:
        model = whisperx.load_model(MODEL, DEVICE, compute_type=COMPUTE_TYPE)
        audio = whisperx.load_audio(str(audio_file))
        result = model.transcribe(audio, batch_size=BATCH_SIZE)
    except torch.cuda.OutOfMemoryError as exc:
        print(f"FAIL: CUDA OOM during transcription: {exc}")
        return 5
    transcribe_s = time.perf_counter() - t0
    peak_transcribe = _vram_mb(torch)

    segments = result.get("segments", [])
    language = result.get("language", "?")
    print(f"Language: {language}  Segments: {len(segments)}")
    for seg in segments:
        print(f"  [{seg.get('start', 0):7.2f}-{seg.get('end', 0):7.2f}] {seg.get('text', '').strip()}")
    print(f"Transcribe wall time: {transcribe_s:.1f}s  peak VRAM: {peak_transcribe:.0f} MB")

    if not segments:
        print("FAIL: empty transcript.")
        return 6

    del model
    gc.collect()
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()

    print("Loading English alignment model...")
    try:
        align_model, metadata = whisperx.load_align_model(
            language_code="en", device=DEVICE
        )
        aligned = whisperx.align(
            segments, align_model, metadata, audio, DEVICE, return_char_alignments=False
        )
    except torch.cuda.OutOfMemoryError as exc:
        print(f"FAIL: CUDA OOM during alignment: {exc}")
        return 7
    peak_align = _vram_mb(torch)
    print(
        f"Aligned {len(aligned.get('segments', []))} segments  peak VRAM: {peak_align:.0f} MB"
    )

    del align_model
    gc.collect()
    torch.cuda.empty_cache()

    print("PASS: transcription + alignment completed without OOM.")
    return 0


if __name__ == "__main__":
    arg = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_AUDIO
    sys.exit(main(arg))
