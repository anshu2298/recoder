from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

import recoder.analysis.session as session_mod
from recoder.config import Config
from recoder.pipeline.runner import PipelineError, run_pipeline
from recoder.pipeline.transcribe import RawSegment
from recoder.store import MeetingState, MeetingStore


# --------------------------------------------------------------------------
# Helpers / doubles
# --------------------------------------------------------------------------


def _write_flac(
    path: Path,
    samplerate: int = 16000,
    seconds: float = 3.0,
    amplitude: float = 0.0,
) -> None:
    n = int(samplerate * seconds)
    if amplitude > 0.0:
        t = np.arange(n, dtype="float32") / samplerate
        data = (amplitude * np.sin(2 * np.pi * 440.0 * t)).astype("float32")
    else:
        data = np.zeros(n, dtype="float32")
    sf.write(str(path), data, samplerate, format="FLAC")


def _make_recorded_meeting(cfg: Config):
    store = MeetingStore(cfg)
    m = store.create_meeting("Test Meeting", "context")
    m.advance(MeetingState.recorded)
    _write_flac(m.audio_mic)
    _write_flac(m.audio_system)
    m.timing_index.write_text(
        "\n".join(
            json.dumps(e)
            for e in [
                {"ch": "mic", "event": "start", "wall": 100.0},
                {"ch": "mic", "frames_written": 16000, "wall": 101.0},
                {"ch": "mic", "frames_written": 32000, "wall": 102.0},
                {"ch": "system", "event": "start", "wall": 100.0},
                {"ch": "system", "frames_written": 16000, "wall": 101.0},
                {"ch": "system", "frames_written": 32000, "wall": 102.0},
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return m


class FakeTranscriber:
    def __init__(self, *, fail_system_first: bool = False) -> None:
        self.calls: list[tuple[str, bool]] = []
        self.fail_system_first = fail_system_first
        self._system_failed = False

    def transcribe(self, audio_path, *, diarize, raw_dump_path=None):
        self.calls.append((Path(audio_path).name, diarize))
        if raw_dump_path is not None:
            Path(raw_dump_path).write_text("{}", encoding="utf-8")
        if diarize:
            if self.fail_system_first and not self._system_failed:
                self._system_failed = True
                raise RuntimeError("gladia system boom")
            return [RawSegment(0, 0.5, 1.0, "them", "en")]
        return [RawSegment(None, 0.0, 1.0, "me", "en")]

    @property
    def mic_calls(self) -> int:
        return sum(1 for _, diarize in self.calls if not diarize)

    @property
    def system_calls(self) -> int:
        return sum(1 for _, diarize in self.calls if diarize)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(meetings_dir=tmp_path / "meetings", gladia_api_key="k")


@pytest.fixture
def stub_analysis(monkeypatch: pytest.MonkeyPatch) -> dict:
    calls = {"analyze": 0, "commit": 0}

    def analyze(folder, config, **kwargs):
        calls["analyze"] += 1
        (Path(folder) / "summary.md").write_text("summary", encoding="utf-8")

    def commit_to_ccr(folder, config, **kwargs):
        calls["commit"] += 1

    monkeypatch.setattr(session_mod, "analyze", analyze)
    monkeypatch.setattr(session_mod, "commit_to_ccr", commit_to_ccr)
    return calls


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_diarize_picks_loudest_system_capture(cfg: Config, stub_analysis: dict) -> None:
    # Primary system file is silence (wrong device); system-2 carries the call.
    m = _make_recorded_meeting(cfg)
    loud = m.audio_system.with_name("audio-system-2.flac")
    _write_flac(loud, amplitude=0.4)
    m.timing_index.write_text(
        m.timing_index.read_text(encoding="utf-8")
        + "\n".join(
            json.dumps(e)
            for e in [
                {"ch": "system2", "event": "start", "wall": 100.0},
                {"ch": "system2", "frames_written": 16000, "wall": 101.0},
                {"ch": "system2", "frames_written": 32000, "wall": 102.0},
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    tr = FakeTranscriber()

    result = run_pipeline(m.folder, cfg, transcriber=tr)

    assert result.state == MeetingState.committed
    # The diarized call went to the loud alternate capture, not the silent primary.
    diarized_files = [name for name, diarize in tr.calls if diarize]
    assert diarized_files == ["audio-system-2.flac"]
    log = m.pipeline_log.read_text(encoding="utf-8")
    assert "-> audio-system-2.flac" in log


def test_diarize_single_system_file_skips_energy_scan(cfg: Config, stub_analysis: dict) -> None:
    m = _make_recorded_meeting(cfg)
    tr = FakeTranscriber()

    run_pipeline(m.folder, cfg, transcriber=tr)

    diarized_files = [name for name, diarize in tr.calls if diarize]
    assert diarized_files == ["audio-system.flac"]
    assert "candidates rms" not in m.pipeline_log.read_text(encoding="utf-8")


def test_full_happy_path(cfg: Config, stub_analysis: dict) -> None:
    m = _make_recorded_meeting(cfg)
    tr = FakeTranscriber()

    result = run_pipeline(m.folder, cfg, transcriber=tr)

    assert result.state == MeetingState.committed
    assert stub_analysis == {"analyze": 1, "commit": 1}

    # transcript + raw dumps + sidecar written
    assert m.transcript_json.exists()
    assert m.transcript_md.exists()
    assert (m.folder / "gladia-mic.json").exists()
    assert (m.folder / "gladia-system.json").exists()
    assert (m.folder / "segments-mic.json").exists()

    payload = json.loads(m.transcript_json.read_text(encoding="utf-8"))
    speakers = {s["speaker"] for s in payload["segments"]}
    assert speakers == {"Me", "SPEAKER_1"}

    # each channel transcribed exactly once
    assert tr.mic_calls == 1
    assert tr.system_calls == 1

    # stage checkpoints recorded
    stages = m.read_meta()["stages"]
    assert {"transcribe", "diarize", "analyze", "commit"} <= set(stages)

    # pipeline.log written
    log = m.pipeline_log.read_text(encoding="utf-8")
    assert "stage transcribe: done" in log
    assert "pipeline complete" in log


def test_transcribe_stage_advances_state_incrementally(
    cfg: Config, stub_analysis: dict
) -> None:
    m = _make_recorded_meeting(cfg)
    run_pipeline(m.folder, cfg, transcriber=FakeTranscriber())
    # sidecar cached the mic segments for resume safety
    cached = json.loads((m.folder / "segments-mic.json").read_text(encoding="utf-8"))
    assert cached[0]["speaker"] is None
    assert cached[0]["text"] == "me"


# --------------------------------------------------------------------------
# Crash + resume
# --------------------------------------------------------------------------


def test_crash_in_diarize_then_resume(cfg: Config, stub_analysis: dict) -> None:
    m = _make_recorded_meeting(cfg)
    tr = FakeTranscriber(fail_system_first=True)

    with pytest.raises(PipelineError, match="diarize"):
        run_pipeline(m.folder, cfg, transcriber=tr)

    # parked in error, remembering the failed stage + predecessor
    assert m.state == MeetingState.error
    meta = m.read_meta()
    assert meta["error"]["stage"] == "diarize"
    assert meta["prev_state"] == MeetingState.transcribed.value
    # mic already transcribed + cached
    assert tr.mic_calls == 1
    assert tr.system_calls == 1  # the failed attempt
    assert (m.folder / "segments-mic.json").exists()

    # resume: transcribe is skipped (mic not re-transcribed), diarize retried
    result = run_pipeline(m.folder, cfg, transcriber=tr)
    assert result.state == MeetingState.committed
    assert tr.mic_calls == 1  # NOT re-called -> cached sidecar reused
    assert tr.system_calls == 2  # failed once, then succeeded
    assert m.transcript_json.exists()


# --------------------------------------------------------------------------
# Missing analysis module
# --------------------------------------------------------------------------


def test_analyze_import_error_becomes_pipeline_error(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = _make_recorded_meeting(cfg)
    # fast-forward to the diarized state so the next stage is analyze
    m.advance(MeetingState.transcribed)
    m.advance(MeetingState.diarized)

    # make the lazy import fail
    monkeypatch.setitem(sys.modules, "recoder.analysis.session", None)

    with pytest.raises(PipelineError, match="analysis stage unavailable"):
        run_pipeline(m.folder, cfg, transcriber=FakeTranscriber())

    assert m.state == MeetingState.error
    assert m.read_meta()["error"]["stage"] == "analyze"


def test_commit_import_error_becomes_pipeline_error(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = _make_recorded_meeting(cfg)
    m.advance(MeetingState.transcribed)
    m.advance(MeetingState.diarized)
    m.advance(MeetingState.analyzed)

    monkeypatch.setitem(sys.modules, "recoder.analysis.session", None)

    with pytest.raises(PipelineError, match="commit stage unavailable"):
        run_pipeline(m.folder, cfg, transcriber=FakeTranscriber())

    assert m.state == MeetingState.error
    assert m.read_meta()["error"]["stage"] == "commit"


def test_no_pending_stage_is_noop(cfg: Config, stub_analysis: dict) -> None:
    m = _make_recorded_meeting(cfg)
    run_pipeline(m.folder, cfg, transcriber=FakeTranscriber())
    assert m.state == MeetingState.committed
    # rerunning a completed meeting does nothing further
    result = run_pipeline(m.folder, cfg, transcriber=FakeTranscriber())
    assert result.state == MeetingState.committed
