"""Merge the two per-channel transcripts into one wall-clock timeline.

The mic and system channels are transcribed independently and each carries
timestamps in *its own* file-time (frames / that channel's sample rate). The
two WASAPI sample clocks drift and can gap on sleep/resume, so we cannot just
overlay file-times. Instead the capture layer wrote a JSONL timing index
(``timing.jsonl``) with periodic ``{"ch","frames_written","wall"}`` points plus
``start``/``stop``/``gap`` events.

From that index we build, per channel, a piecewise-linear map
``file_time -> wall_clock``. Each channel's segments are converted to wall
time, then rebased to meeting-relative seconds (t=0 at the earlier channel
start). The mic speaker becomes "Me"; system speakers become SPEAKER_1..n in
order of first appearance. If a channel's index is too sparse (<2 points) we
fall back to a simple start-offset alignment.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import soundfile as sf

from recoder.pipeline.transcribe import RawSegment

__all__ = ["Segment", "merge_channels", "write_transcript"]


@dataclass(frozen=True)
class Segment:
    """A merged, speaker-labelled utterance in meeting-relative seconds."""

    speaker: str
    start: float
    end: float
    text: str
    language: str | None


# --------------------------------------------------------------------------
# Timing index -> per-channel file_time->wall map
# --------------------------------------------------------------------------


@dataclass
class _ChannelMap:
    """Maps one channel's file-time to wall-clock via the timing index."""

    points: list[tuple[float, float]]  # (file_time, wall), sorted, deduped
    start_wall: float | None
    sparse: bool

    def to_wall(self, file_time: float) -> float:
        if self.sparse or len(self.points) < 2:
            base = self.start_wall if self.start_wall is not None else 0.0
            return base + file_time
        return _piecewise(self.points, file_time)


def _read_timing_index(path: Path) -> dict[str, list[dict]]:
    """Group timing.jsonl entries by channel; tolerate blank/broken lines."""
    by_channel: dict[str, list[dict]] = {}
    if not path.exists():
        return by_channel
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            ch = entry.get("ch")
            if not isinstance(ch, str):
                continue
            by_channel.setdefault(ch, []).append(entry)
    return by_channel


def _build_channel_map(entries: list[dict], sample_rate: float) -> _ChannelMap:
    """Turn one channel's index entries into a file_time->wall map."""
    data: list[tuple[float, float]] = []
    start_wall: float | None = None

    for entry in entries:
        if entry.get("event") == "start" and start_wall is None:
            wall = entry.get("wall")
            if isinstance(wall, (int, float)):
                start_wall = float(wall)
        frames = entry.get("frames_written")
        wall = entry.get("wall")
        if isinstance(frames, (int, float)) and isinstance(wall, (int, float)):
            if sample_rate > 0:
                data.append((float(frames) / sample_rate, float(wall)))

    data.sort(key=lambda p: p[0])

    # The real "index" density is the number of periodic frame points; <2 of
    # those means we cannot trust the piecewise slope -> start-offset fallback.
    sparse = len(data) < 2

    if start_wall is None and data:
        # Infer the file-time origin from the first data point.
        start_wall = data[0][1] - data[0][0]

    # Interpolation points get a (0, start_wall) anchor so early segments (which
    # predate the first ~1s frame point) extrapolate against a real origin.
    interp = list(data)
    if start_wall is not None:
        interp.append((0.0, start_wall))
    interp.sort(key=lambda p: p[0])
    interp = _dedup_by_x(interp)

    return _ChannelMap(points=interp, start_wall=start_wall, sparse=sparse)


def _dedup_by_x(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Drop duplicate file-times (keep last) to avoid zero-width segments."""
    out: list[tuple[float, float]] = []
    for x, y in points:
        if out and abs(out[-1][0] - x) < 1e-9:
            out[-1] = (x, y)
        else:
            out.append((x, y))
    return out


def _piecewise(points: list[tuple[float, float]], x: float) -> float:
    """Piecewise-linear interpolate/extrapolate wall for file-time ``x``."""
    n = len(points)
    if x <= points[0][0]:
        (x0, y0), (x1, y1) = points[0], points[1]
    elif x >= points[-1][0]:
        (x0, y0), (x1, y1) = points[-2], points[-1]
    else:
        x0, y0 = points[0]
        x1, y1 = points[-1]
        for i in range(n - 1):
            if points[i][0] <= x <= points[i + 1][0]:
                x0, y0 = points[i]
                x1, y1 = points[i + 1]
                break
    if x1 == x0:
        return y0
    slope = (y1 - y0) / (x1 - x0)
    return y0 + slope * (x - x0)


def _sample_rate(flac_path: Path) -> float:
    return float(sf.info(str(flac_path)).samplerate)


# --------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------


def merge_channels(
    mic_segments: list[RawSegment],
    system_segments: list[RawSegment],
    timing_index_path: Path,
    mic_flac: Path,
    system_flac: Path,
    *,
    system_channel: str = "system",
) -> list[Segment]:
    """Align both channels onto one meeting-relative timeline.

    Mic segments are labelled "Me"; system diarized speaker ints become
    SPEAKER_1..n in order of first appearance (by time). ``system_channel``
    names the timing-index channel for ``system_flac`` (e.g. ``system2`` when
    the pipeline chose an alternate loopback capture). Returns segments sorted
    by start time.
    """
    by_channel = _read_timing_index(Path(timing_index_path))

    mic_map = _build_channel_map(
        by_channel.get("mic", []), _sample_rate(Path(mic_flac))
    )
    system_map = _build_channel_map(
        by_channel.get(system_channel, []), _sample_rate(Path(system_flac))
    )

    # Meeting t=0 is the earlier of the two channel starts.
    starts = [w for w in (mic_map.start_wall, system_map.start_wall) if w is not None]
    meeting_start = min(starts) if starts else 0.0

    def rebase(seg: RawSegment, cmap: _ChannelMap) -> tuple[float, float]:
        start = cmap.to_wall(seg.start) - meeting_start
        end = cmap.to_wall(seg.end) - meeting_start
        return start, end

    # Mic -> "Me".
    merged: list[Segment] = []
    for seg in mic_segments:
        start, end = rebase(seg, mic_map)
        merged.append(
            Segment(
                speaker="Me",
                start=start,
                end=end,
                text=seg.text,
                language=seg.language,
            )
        )

    # System -> SPEAKER_n. Rebase first, sort by start, then assign labels in
    # first-appearance order so the numbering is stable and time-ordered.
    system_rebased: list[tuple[float, float, RawSegment]] = []
    for seg in system_segments:
        start, end = rebase(seg, system_map)
        system_rebased.append((start, end, seg))
    system_rebased.sort(key=lambda item: item[0])

    speaker_labels: dict[object, str] = {}
    for start, end, seg in system_rebased:
        raw = seg.speaker
        if raw not in speaker_labels:
            speaker_labels[raw] = f"SPEAKER_{len(speaker_labels) + 1}"
        merged.append(
            Segment(
                speaker=speaker_labels[raw],
                start=start,
                end=end,
                text=seg.text,
                language=seg.language,
            )
        )

    merged.sort(key=lambda s: s.start)
    return merged


# --------------------------------------------------------------------------
# Output rendering
# --------------------------------------------------------------------------


def _now() -> datetime:
    """Indirection so tests can pin the generated_at timestamp."""
    return datetime.now()


def write_transcript(
    segments: list[Segment],
    transcript_json_path: Path,
    transcript_md_path: Path,
    source: str,
) -> None:
    """Write the merged transcript as JSON (canonical) and Markdown (readable)."""
    transcript_json_path = Path(transcript_json_path)
    transcript_md_path = Path(transcript_md_path)
    transcript_json_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "segments": [
            {
                "speaker": s.speaker,
                "start": s.start,
                "end": s.end,
                "text": s.text,
                "language": s.language,
            }
            for s in segments
        ],
        "source": source,
        "generated_at": _now().isoformat(),
    }
    with transcript_json_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)

    lines = [f"[{_mmss(s.start)}] {s.speaker}: {s.text}" for s in segments]
    transcript_md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _mmss(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"
