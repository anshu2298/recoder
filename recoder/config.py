from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_OVERRIDE_FILE = _REPO_ROOT / "recoder.toml"

_CCR_PYTHON = r"C:\Users\anshu\.ccr\.venv\Scripts\python.exe"


@dataclass(frozen=True)
class Config:
    meetings_dir: Path = Path(r"G:\recoder\meetings")
    port: int = 8377

    whisper_model: str = "large-v3"
    compute_type: str = "int8"
    batch_size: int = 4

    snapshot_interval_s: int = 20
    phash_hamming_threshold: int = 4
    jpeg_quality: int = 80
    max_frame_width: int = 1568

    window_title_patterns: list[str] = field(
        default_factory=lambda: [
            "Zoom",
            "Meet",
            "Teams",
            "Google Meet",
            "Microsoft Teams",
        ]
    )

    ccr_mcp_command: str = _CCR_PYTHON
    ccr_mcp_args: list[str] = field(
        default_factory=lambda: ["-m", "ccr.mcp_server", "--project", r"G:\recoder"]
    )

    # --- Gladia hosted STT (default engine, spec §4.2 step 1) ----------------
    # API key resolves from env GLADIA_API_KEY, with a recoder.toml override.
    gladia_api_key: str | None = None
    gladia_base_url: str = "https://api.gladia.io"
    gladia_poll_interval_s: float = 3.0
    gladia_timeout_s: int = 900


_SCALAR_KEYS = {
    "meetings_dir",
    "port",
    "whisper_model",
    "compute_type",
    "batch_size",
    "snapshot_interval_s",
    "phash_hamming_threshold",
    "jpeg_quality",
    "max_frame_width",
    "window_title_patterns",
    "ccr_mcp_command",
    "ccr_mcp_args",
    "gladia_api_key",
    "gladia_base_url",
    "gladia_poll_interval_s",
    "gladia_timeout_s",
}


def load_config(override_file: Path | None = None) -> Config:
    cfg = Config()

    # Environment sources (lowest precedence after dataclass defaults).
    env_key = os.environ.get("GLADIA_API_KEY")
    if env_key:
        cfg = replace(cfg, gladia_api_key=env_key)

    path = override_file if override_file is not None else _OVERRIDE_FILE
    if not path.exists():
        return cfg

    with path.open("rb") as fh:
        data = tomllib.load(fh)

    updates: dict[str, object] = {}
    for key, value in data.items():
        if key not in _SCALAR_KEYS:
            continue
        if key == "meetings_dir":
            updates[key] = Path(value)
        else:
            updates[key] = value

    return replace(cfg, **updates)
