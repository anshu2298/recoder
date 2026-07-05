from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_OVERRIDE_FILE = _REPO_ROOT / "recoder.toml"

# CCR (github.com/qbit-glitch/ccr) lives in a dedicated venv under the user's
# home dir; scripts/setup.ps1 creates it there. Everything below is derived
# from the machine, never hardcoded, so a fresh clone works for any teammate.
_CCR_HOME = Path.home() / ".ccr"
_CCR_PYTHON = str(
    _CCR_HOME
    / ".venv"
    / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
)


@dataclass(frozen=True)
class Config:
    meetings_dir: Path = _REPO_ROOT / "meetings"
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

    # While a screen-share is detected (see presenting_indicator_patterns),
    # additionally snapshot every physical monitor other than the one holding
    # the meeting window — that is where the content being presented lives.
    capture_monitors_when_presenting: bool = True
    # Window titles that indicate an active screen-share. Chrome/Edge show an
    # "<site> is sharing your screen" pill; Zoom/Teams spawn share toolbars.
    presenting_indicator_patterns: list[str] = field(
        default_factory=lambda: [
            "is sharing your screen",
            "is sharing a window",
            "is sharing a tab",
            "share toolbar",
            "share statusbar",
            "sharing toolbar",
            "screen sharing meeting controls",
            "stop sharing",
        ]
    )

    ccr_mcp_command: str = _CCR_PYTHON
    ccr_mcp_args: list[str] = field(
        default_factory=lambda: ["-m", "ccr.mcp_server", "--project", str(_REPO_ROOT)]
    )

    # --- CCR project-memory routing (Piece A) --------------------------------
    # Global CCR registry that maps every project path to its store metadata.
    ccr_registry_path: Path = _CCR_HOME / "projects.json"
    # A routed project counts as "recent" if used within this many days.
    routing_recency_days: int = 7
    # Never mount more than this many foreign stores into an analysis session.
    routing_max_mounts: int = 4

    # --- Worktree memory consolidation (Piece B) -----------------------------
    # Base dir under which a consolidated source store is archived (never
    # deleted): <consolidation_archive_dir>/<source-name>-<YYYYMMDD>.
    consolidation_archive_dir: Path = _REPO_ROOT / "archives" / "ccr"
    # Per-source incremental watermark state (last consolidated source commit
    # id, timestamp, run count) keyed by normalized source path.
    consolidation_state_path: Path = _REPO_ROOT / "consolidation-state.json"
    # Named source->target groups for `recoder consolidate-group <name>`. Shape:
    #   {"<group>": {"target": str, "sources": [str, ...]}}. Loaded verbatim from
    #   the [consolidation_groups.<name>] tables in recoder.toml.
    consolidation_groups: dict = field(default_factory=dict)

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
    "capture_monitors_when_presenting",
    "presenting_indicator_patterns",
    "ccr_mcp_command",
    "ccr_mcp_args",
    "ccr_registry_path",
    "routing_recency_days",
    "routing_max_mounts",
    "consolidation_archive_dir",
    "consolidation_state_path",
    "consolidation_groups",
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
        if key in (
            "meetings_dir",
            "ccr_registry_path",
            "consolidation_archive_dir",
            "consolidation_state_path",
        ):
            updates[key] = Path(value)
        elif key == "consolidation_groups":
            # A TOML table of tables; keep it a plain dict (tolerate absence).
            updates[key] = dict(value) if isinstance(value, dict) else {}
        else:
            updates[key] = value

    return replace(cfg, **updates)
