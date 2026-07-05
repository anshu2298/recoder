"""Spike A — WASAPI loopback + mic dual capture.

Records 10s of system audio (WASAPI loopback of the default output device) and
10s of the default microphone simultaneously via pyaudiowpatch, writing
spikes/out/loopback.flac and spikes/out/mic.flac via soundfile, and prints the
RMS level of each stream.

Pass criteria (the CALLER must have audio playing on the default output device
for the whole 10s):
  - both FLAC files are written
  - loopback RMS is above the near-silence floor (>= 1e-4)

Exits 0 on success, nonzero with a clear message on failure.

Usage:
    python spikes/spike_a_loopback.py
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import numpy as np
import pyaudiowpatch as pyaudio
import soundfile as sf

DURATION_S = 10.0
CHUNK = 1024
SILENCE_FLOOR = 1e-4
OUT_DIR = Path(__file__).resolve().parent / "out"


def _rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))


def _record_stream(
    pa: pyaudio.PyAudio,
    device_index: int,
    rate: int,
    channels: int,
    out: list[np.ndarray],
) -> None:
    stream = pa.open(
        format=pyaudio.paInt16,
        channels=channels,
        rate=rate,
        frames_per_buffer=CHUNK,
        input=True,
        input_device_index=device_index,
    )
    frames_needed = int(rate * DURATION_S)
    captured = 0
    try:
        while captured < frames_needed:
            data = stream.read(CHUNK, exception_on_overflow=False)
            block = np.frombuffer(data, dtype=np.int16)
            out.append(block)
            captured += CHUNK
    finally:
        stream.stop_stream()
        stream.close()


def _to_mono_float(blocks: list[np.ndarray], channels: int) -> np.ndarray:
    if not blocks:
        return np.zeros(0, dtype=np.float32)
    raw = np.concatenate(blocks).astype(np.float32) / 32768.0
    if channels > 1:
        usable = (raw.size // channels) * channels
        raw = raw[:usable].reshape(-1, channels).mean(axis=1)
    return raw


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with pyaudio.PyAudio() as pa:
        try:
            loopback = pa.get_default_wasapi_loopback()
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL: no WASAPI loopback device available: {exc}")
            return 2

        try:
            mic_info = pa.get_default_input_device_info()
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL: no default input (mic) device available: {exc}")
            return 3

        lb_index = int(loopback["index"])
        lb_rate = int(loopback["defaultSampleRate"])
        lb_channels = int(loopback["maxInputChannels"])

        mic_index = int(mic_info["index"])
        mic_rate = int(mic_info["defaultSampleRate"])
        mic_channels = min(int(mic_info["maxInputChannels"]), 2)

        print(f"Loopback: {loopback['name']} @ {lb_rate}Hz x{lb_channels}")
        print(f"Mic:      {mic_info['name']} @ {mic_rate}Hz x{mic_channels}")

        lb_blocks: list[np.ndarray] = []
        mic_blocks: list[np.ndarray] = []
        errors: list[BaseException] = []

        def guarded(fn, *args) -> None:
            try:
                fn(*args)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t_lb = threading.Thread(
            target=guarded,
            args=(_record_stream, pa, lb_index, lb_rate, lb_channels, lb_blocks),
        )
        t_mic = threading.Thread(
            target=guarded,
            args=(_record_stream, pa, mic_index, mic_rate, mic_channels, mic_blocks),
        )

        t_lb.start()
        t_mic.start()
        t_lb.join()
        t_mic.join()

        if errors:
            print(f"FAIL: capture error: {errors[0]!r}")
            return 4

    lb_mono = _to_mono_float(lb_blocks, lb_channels)
    mic_mono = _to_mono_float(mic_blocks, mic_channels)

    lb_path = OUT_DIR / "loopback.flac"
    mic_path = OUT_DIR / "mic.flac"
    sf.write(lb_path, lb_mono, lb_rate, format="FLAC")
    sf.write(mic_path, mic_mono, mic_rate, format="FLAC")

    lb_rms = _rms(lb_mono)
    mic_rms = _rms(mic_mono)
    print(f"Wrote {lb_path} ({lb_mono.size} samples, RMS={lb_rms:.6f})")
    print(f"Wrote {mic_path} ({mic_mono.size} samples, RMS={mic_rms:.6f})")

    if lb_rms < SILENCE_FLOOR:
        print(
            "FAIL: loopback RMS is near-silence "
            f"({lb_rms:.6f} < {SILENCE_FLOOR}). "
            "Ensure audio is playing on the default output device during capture."
        )
        return 5

    print("PASS: loopback captured audio; both FLAC files written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
