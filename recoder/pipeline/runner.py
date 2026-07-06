"""Post-call pipeline runner (spec §4.2, §5).

Drives a single meeting folder through the resumable state machine:
``recorded -> transcribed -> diarized -> analyzed -> committed``. Each stage is
checkpointed in ``meta.json`` after it completes, so a crash never redoes
finished (API-billed) work. Human-readable progress is appended to
``pipeline.log``.

Stage mapping (spec §4.2 step 1):
  * ``transcribe`` — mic channel via Gladia, no diarization; raw response saved
    to ``gladia-mic.json`` and the parsed segments cached to
    ``segments-mic.json`` so a crash before diarize does not re-spend hours.
  * ``diarize``    — system channel via Gladia with diarization; raw response
    saved to ``gladia-system.json``; then both channels are merged and the
    ``transcript.json`` / ``transcript.md`` are written. Cached mic segments are
    reloaded rather than re-transcribed when present.
  * ``analyze`` / ``commit`` — delegate to the (separately built)
    :mod:`recoder.analysis.session` module via lazy imports; a missing module
    surfaces as a clear :class:`PipelineError`.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from recoder.pipeline.merge import merge_channels, write_transcript
from recoder.pipeline.transcribe import (
    GladiaTranscriber,
    RawSegment,
    Transcriber,
)
from recoder.store import (
    STAGE_ANALYZE,
    STAGE_COMMIT,
    STAGE_DIARIZE,
    STAGE_TRANSCRIBE,
    Meeting,
    MeetingState,
    MeetingStore,
)

__all__ = ["PipelineError", "run_pipeline"]

_TRANSCRIPT_SOURCE = "gladia"


class PipelineError(RuntimeError):
    """A pipeline stage failed; the meeting is parked in the error state."""


# Which state each stage produces on success.
_STAGE_RESULT_STATE = {
    STAGE_TRANSCRIBE: MeetingState.transcribed,
    STAGE_DIARIZE: MeetingState.diarized,
    STAGE_ANALYZE: MeetingState.analyzed,
    STAGE_COMMIT: MeetingState.committed,
}


def run_pipeline(
    meeting_folder: Path,
    config,
    *,
    transcriber: Transcriber | None = None,
) -> Meeting:
    """Run all pending pipeline stages for ``meeting_folder`` to completion.

    Rerunning on an errored meeting resumes: the error state is rewound to its
    predecessor and the failed stage runs again (finished stages are skipped).
    Returns the :class:`Meeting`.
    """
    store = MeetingStore(config)
    meeting = store.load(meeting_folder)

    # Resume: rewind error -> the state it failed from so next_pending_stage
    # points back at the failed stage.
    if meeting.state == MeetingState.error:
        prev = meeting.read_meta().get("prev_state")
        if prev is None:
            raise PipelineError(
                f"meeting {meeting.folder.name} is in error with no recorded "
                "predecessor state; cannot resume"
            )
        _log(meeting, f"resuming from error at stage-before-state {prev}")
        meeting.advance(prev)

    while True:
        stage = store.next_pending_stage(meeting)
        if stage is None:
            break

        _log(meeting, f"stage {stage}: start")
        t0 = time.monotonic()
        try:
            result_state = _run_stage(stage, meeting, config, transcriber)
        except Exception as exc:  # noqa: BLE001 - park + re-raise as PipelineError
            message = str(exc)
            _log(meeting, f"stage {stage}: FAILED - {message}")
            meeting.set_error(stage, message)
            if isinstance(exc, PipelineError):
                raise
            raise PipelineError(f"stage {stage} failed: {message}") from exc

        duration = time.monotonic() - t0
        meeting.record_stage(stage, duration)
        meeting.advance(result_state)
        _log(
            meeting,
            f"stage {stage}: done in {duration:.1f}s -> {result_state.value}",
        )

    _log(meeting, f"pipeline complete at state {meeting.state.value}")
    return meeting


# --------------------------------------------------------------------------
# Stage dispatch
# --------------------------------------------------------------------------


def _run_stage(
    stage: str,
    meeting: Meeting,
    config,
    transcriber: Transcriber | None,
) -> MeetingState:
    if stage == STAGE_TRANSCRIBE:
        return _stage_transcribe(meeting, config, transcriber)
    if stage == STAGE_DIARIZE:
        return _stage_diarize(meeting, config, transcriber)
    if stage == STAGE_ANALYZE:
        return _stage_analyze(meeting, config)
    if stage == STAGE_COMMIT:
        return _stage_commit(meeting, config)
    raise PipelineError(f"unknown pipeline stage: {stage!r}")


def _stage_transcribe(
    meeting: Meeting, config, transcriber: Transcriber | None
) -> MeetingState:
    tr = _get_transcriber(transcriber, config)
    segments = tr.transcribe(
        meeting.audio_mic,
        diarize=False,
        raw_dump_path=meeting.folder / "gladia-mic.json",
    )
    _dump_segments(segments, _mic_sidecar(meeting))
    _log(meeting, f"transcribe: mic -> {len(segments)} segments")
    return MeetingState.transcribed


def _stage_diarize(
    meeting: Meeting, config, transcriber: Transcriber | None
) -> MeetingState:
    tr = _get_transcriber(transcriber, config)
    system_flac, system_channel = _pick_system_audio(meeting)
    system_segments = tr.transcribe(
        system_flac,
        diarize=True,
        raw_dump_path=meeting.folder / "gladia-system.json",
    )
    _log(meeting, f"diarize: system ({system_channel}) -> {len(system_segments)} segments")

    sidecar = _mic_sidecar(meeting)
    if sidecar.exists():
        mic_segments = _load_segments(sidecar)
        _log(meeting, f"diarize: reused {len(mic_segments)} cached mic segments")
    else:
        # Should not happen on a normal run (transcribe wrote it), but stay
        # resilient: re-transcribe rather than crash.
        mic_segments = tr.transcribe(
            meeting.audio_mic,
            diarize=False,
            raw_dump_path=meeting.folder / "gladia-mic.json",
        )
        _dump_segments(mic_segments, sidecar)
        _log(meeting, "diarize: mic sidecar missing, re-transcribed mic")

    merged = merge_channels(
        mic_segments,
        system_segments,
        meeting.timing_index,
        meeting.audio_mic,
        system_flac,
        system_channel=system_channel,
    )
    write_transcript(
        merged,
        meeting.transcript_json,
        meeting.transcript_md,
        source=_TRANSCRIPT_SOURCE,
    )
    _log(meeting, f"diarize: merged transcript -> {len(merged)} segments")
    return MeetingState.diarized


def _stage_analyze(meeting: Meeting, config) -> MeetingState:
    try:
        from recoder.analysis.session import analyze
    except ImportError as exc:
        raise PipelineError(
            f"analysis stage unavailable: cannot import "
            f"recoder.analysis.session.analyze ({exc})"
        ) from exc
    analyze(meeting.folder, config)
    return MeetingState.analyzed


def _stage_commit(meeting: Meeting, config) -> MeetingState:
    try:
        from recoder.analysis.session import commit_to_ccr
    except ImportError as exc:
        raise PipelineError(
            f"commit stage unavailable: cannot import "
            f"recoder.analysis.session.commit_to_ccr ({exc})"
        ) from exc
    commit_to_ccr(meeting.folder, config)
    return MeetingState.committed


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _get_transcriber(transcriber: Transcriber | None, config) -> Transcriber:
    if transcriber is not None:
        return transcriber
    return GladiaTranscriber(config)


def _pick_system_audio(meeting: Meeting) -> tuple[Path, str]:
    """Choose the loudest system capture: ``(flac_path, timing_channel)``.

    Multiple loopback devices may have been recorded (``audio-system.flac``
    plus ``audio-system-N.flac``); the call's audio lives on whichever device
    the meeting app actually played to. Highest RMS energy wins. With a single
    candidate (or on any error) the primary file is used unchanged.
    """
    primary = meeting.audio_system
    candidates: list[tuple[Path, str]] = [(primary, "system")]
    pattern = f"{primary.stem}-*{primary.suffix}"
    for path in sorted(primary.parent.glob(pattern)):
        suffix_n = path.stem[len(primary.stem) + 1 :]
        if suffix_n.isdigit():
            candidates.append((path, f"system{suffix_n}"))

    if len(candidates) == 1:
        return candidates[0]

    best = candidates[0]
    best_rms = -1.0
    report: list[str] = []
    for path, channel in candidates:
        try:
            rms = _rms_energy(path)
        except Exception as exc:  # noqa: BLE001 - unreadable candidate -> skip
            _log(meeting, f"diarize: {path.name} unreadable ({exc}); skipped")
            continue
        report.append(f"{path.name}={rms:.5f}")
        if rms > best_rms:
            best = (path, channel)
            best_rms = rms
    _log(meeting, f"diarize: system candidates rms {', '.join(report)} -> {best[0].name}")
    return best


def _rms_energy(path: Path) -> float:
    """RMS of a FLAC file, streamed in blocks (never loads it whole)."""
    import numpy as np
    import soundfile as sf

    acc_sq = 0.0
    acc_n = 0
    with sf.SoundFile(str(path)) as fh:
        while True:
            block = fh.read(1_048_576, dtype="float32", always_2d=True)
            if len(block) == 0:
                break
            flat = np.asarray(block).reshape(-1)
            acc_sq += float(np.dot(flat, flat))
            acc_n += flat.size
    if acc_n == 0:
        return 0.0
    return float(np.sqrt(acc_sq / acc_n))


def _mic_sidecar(meeting: Meeting) -> Path:
    return meeting.folder / "segments-mic.json"


def _dump_segments(segments: list[RawSegment], path: Path) -> None:
    payload = [
        {
            "speaker": s.speaker,
            "start": s.start,
            "end": s.end,
            "text": s.text,
            "language": s.language,
        }
        for s in segments
    ]
    with Path(path).open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)


def _load_segments(path: Path) -> list[RawSegment]:
    with Path(path).open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return [
        RawSegment(
            speaker=item.get("speaker"),
            start=float(item.get("start", 0.0)),
            end=float(item.get("end", 0.0)),
            text=item.get("text") or "",
            language=item.get("language"),
        )
        for item in data
    ]


def _log(meeting: Meeting, message: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}\n"
    with meeting.pipeline_log.open("a", encoding="utf-8") as fh:
        fh.write(line)
