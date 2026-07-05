from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path

from recoder.config import load_config

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

MIN_FREE_GB = 5.0


def _line(status: str, name: str, detail: str = "", remediation: str = "") -> str:
    msg = f"[{status}] {name}"
    if detail:
        msg += f" — {detail}"
    if status == FAIL and remediation:
        msg += f"\n        fix: {remediation}"
    return msg


def _check_python() -> str:
    v = sys.version_info
    if v >= (3, 11):
        return _line(PASS, "Python >= 3.11", f"{v.major}.{v.minor}.{v.micro}")
    return _line(
        FAIL,
        "Python >= 3.11",
        f"found {v.major}.{v.minor}.{v.micro}",
        "Install Python 3.11+ and recreate the venv.",
    )


def _check_loopback() -> str:
    if importlib.util.find_spec("pyaudiowpatch") is None:
        return _line(
            FAIL,
            "WASAPI loopback device",
            "pyaudiowpatch not importable",
            "uv sync",
        )
    import pyaudiowpatch as pyaudio

    try:
        with pyaudio.PyAudio() as pa:
            info = pa.get_default_wasapi_loopback()
        return _line(PASS, "WASAPI loopback device", str(info["name"]))
    except Exception as exc:  # noqa: BLE001
        return _line(
            FAIL,
            "WASAPI loopback device",
            f"none found ({exc})",
            "Ensure a default output device is active (speakers/headphones).",
        )


def _check_mic() -> str:
    if importlib.util.find_spec("pyaudiowpatch") is None:
        return _line(FAIL, "Microphone device", "pyaudiowpatch not importable", "uv sync")
    import pyaudiowpatch as pyaudio

    try:
        with pyaudio.PyAudio() as pa:
            info = pa.get_default_input_device_info()
        return _line(PASS, "Microphone device", str(info["name"]))
    except Exception as exc:  # noqa: BLE001
        return _line(
            FAIL,
            "Microphone device",
            f"none found ({exc})",
            "Connect a microphone and set it as the default input device.",
        )


def _check_torch() -> str:
    if importlib.util.find_spec("torch") is None:
        note = (
            "\n        note: uv pip install torch --index-url "
            "https://download.pytorch.org/whl/cu121 ; then uv sync --extra ml"
        )
        return _line(SKIP, "torch + CUDA", "ml extras not installed") + note
    import torch

    if not torch.cuda.is_available():
        return _line(
            FAIL,
            "torch + CUDA",
            "CUDA not available",
            "Install the CUDA torch build: uv pip install torch "
            "--index-url https://download.pytorch.org/whl/cu121",
        )
    return _line(PASS, "torch + CUDA", torch.cuda.get_device_name(0))


def _check_whisperx() -> str:
    if importlib.util.find_spec("whisperx") is None:
        note = "\n        note: uv sync --extra ml"
        return _line(SKIP, "whisperx importable", "ml extras not installed") + note
    return _line(PASS, "whisperx importable")


def _check_hf_token() -> str:
    if os.environ.get("HF_TOKEN"):
        return _line(PASS, "HuggingFace token (local STT fallback)", "env HF_TOKEN")
    token_file = Path.home() / ".cache" / "huggingface" / "token"
    if token_file.exists() and token_file.read_text(encoding="utf-8").strip():
        return _line(PASS, "HuggingFace token (local STT fallback)", str(token_file))
    return _line(SKIP, "HuggingFace token (local STT fallback)", "not set — only needed for local whisperX")


def _check_gladia(cfg) -> str:
    if not cfg.gladia_api_key:
        return _line(
            FAIL,
            "Gladia API key",
            "not set",
            "Sign up free at app.gladia.io, then set GLADIA_API_KEY or gladia_api_key in recoder.toml.",
        )
    return _line(PASS, "Gladia API key", f"set ({len(cfg.gladia_api_key)} chars)")


def _check_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path:
        return _line(PASS, "ffmpeg on PATH (local STT fallback)", path)
    return _line(
        SKIP,
        "ffmpeg on PATH (local STT fallback)",
        "not found — only needed for local whisperX (winget install Gyan.FFmpeg)",
    )


def _check_claude_cli() -> str:
    path = shutil.which("claude")
    if path:
        return _line(PASS, "claude CLI on PATH", path)
    return _line(
        FAIL,
        "claude CLI on PATH",
        "not found",
        "Install and log in to the Claude CLI (`claude login`).",
    )


def _check_ccr_python(cfg) -> str:
    p = Path(cfg.ccr_mcp_command)
    if p.exists():
        return _line(PASS, "CCR venv python", str(p))
    return _line(
        FAIL,
        "CCR venv python",
        f"missing at {p}",
        "Run scripts/setup.ps1 (installs CCR into ~/.ccr/.venv), or set ccr_mcp_command in recoder.toml.",
    )


def _check_disk(cfg) -> str:
    target = cfg.meetings_dir
    probe = target
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        free_gb = shutil.disk_usage(probe).free / (1024**3)
    except Exception as exc:  # noqa: BLE001
        return _line(FAIL, "Free disk on meetings drive", f"unreadable ({exc})", "Check the meetings_dir path.")
    if free_gb >= MIN_FREE_GB:
        return _line(PASS, "Free disk on meetings drive", f"{free_gb:.1f} GB free")
    return _line(
        FAIL,
        "Free disk on meetings drive",
        f"{free_gb:.1f} GB free (< {MIN_FREE_GB} GB)",
        "Free up disk space on the meetings drive.",
    )


def run_doctor(full: bool = False) -> int:
    cfg = load_config()
    checks = [
        _check_python(),
        _check_loopback(),
        _check_mic(),
        _check_gladia(cfg),
        _check_torch(),
        _check_whisperx(),
        _check_hf_token(),
        _check_ffmpeg(),
        _check_claude_cli(),
        _check_ccr_python(cfg),
        _check_disk(cfg),
    ]

    for line in checks:
        print(line)

    fails = sum(1 for line in checks if line.startswith(f"[{FAIL}]"))

    if full:
        print(_line("....", "Unattended SDK probe (Spike C)", "running..."))
        rc = _run_sdk_probe()
        if rc == 0:
            print(_line(PASS, "Unattended SDK probe (Spike C)"))
        else:
            print(
                _line(
                    FAIL,
                    "Unattended SDK probe (Spike C)",
                    f"exit {rc}",
                    "Run `python spikes/spike_c_sdk.py` directly to see details.",
                )
            )
            fails += 1

    print(f"\n{fails} failure(s).")
    return fails


def _run_sdk_probe() -> int:
    spikes_dir = Path(__file__).resolve().parent.parent / "spikes"
    sys.path.insert(0, str(spikes_dir))
    try:
        import spike_c_sdk

        return spike_c_sdk.main()
    except Exception as exc:  # noqa: BLE001
        print(f"        probe raised: {exc!r}")
        return 1
    finally:
        sys.path.remove(str(spikes_dir))


if __name__ == "__main__":
    sys.exit(run_doctor())
