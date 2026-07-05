from __future__ import annotations

from pathlib import Path

from recoder.config import Config, load_config


def test_defaults() -> None:
    cfg = Config()
    assert cfg.port == 8377
    assert cfg.whisper_model == "large-v3"
    assert cfg.compute_type == "int8"
    assert cfg.batch_size == 4
    assert cfg.snapshot_interval_s == 20
    assert cfg.phash_hamming_threshold == 4
    assert cfg.jpeg_quality == 80
    assert cfg.max_frame_width == 1568
    assert "Zoom" in cfg.window_title_patterns
    assert cfg.ccr_mcp_command.endswith("python.exe")
    assert cfg.ccr_mcp_args[0] == "-m"


def test_load_without_override(tmp_path: Path) -> None:
    cfg = load_config(override_file=tmp_path / "missing.toml")
    assert cfg == Config()


def test_toml_override(tmp_path: Path) -> None:
    override = tmp_path / "recoder.toml"
    override.write_text(
        "\n".join(
            [
                "port = 9000",
                'compute_type = "float16"',
                "batch_size = 8",
                'meetings_dir = "D:\\\\data\\\\meetings"',
                'window_title_patterns = ["Webex", "Slack"]',
            ]
        ),
        encoding="utf-8",
    )
    cfg = load_config(override_file=override)
    assert cfg.port == 9000
    assert cfg.compute_type == "float16"
    assert cfg.batch_size == 8
    assert cfg.meetings_dir == Path(r"D:\data\meetings")
    assert cfg.window_title_patterns == ["Webex", "Slack"]
    # untouched fields keep defaults
    assert cfg.whisper_model == "large-v3"


def test_unknown_keys_ignored(tmp_path: Path) -> None:
    override = tmp_path / "recoder.toml"
    override.write_text("bogus = 123\nport = 8080\n", encoding="utf-8")
    cfg = load_config(override_file=override)
    assert cfg.port == 8080
    assert not hasattr(cfg, "bogus")
