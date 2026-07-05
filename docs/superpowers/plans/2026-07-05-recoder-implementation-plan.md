# Recoder — Implementation Plan

**Spec:** `docs/superpowers/specs/2026-07-05-recoder-design.md`
**Ordering principle:** riskiest assumptions first. The three things that could sink the design — WASAPI loopback capture on this machine, whisperX on 4GB VRAM, and unattended Claude SDK + CCR — are each proven by a small spike before anything is built on top of them.

## Phase 0 — Scaffold + risk spikes

**Goal:** project skeleton exists; every risky external dependency is proven working on this exact machine.

| # | Task | Acceptance |
|---|------|------------|
| 0.1 | `pyproject.toml` (uv-managed venv, Python 3.11), package layout `recoder/{capture,pipeline,analysis,web}`, `config.py` (paths, ports, model settings as one dataclass), `recoder` CLI entry point (typer) | `uv run recoder --help` works |
| 0.2 | **Spike A — loopback:** minimal script records 10s of system audio (play a YouTube video) + 10s of mic via `pyaudiowpatch`, writes two FLACs via `soundfile` | Both files play back correctly; loopback captured the video audio |
| 0.3 | **Spike B — GPU:** install torch (CUDA), faster-whisper/whisperX; transcribe a 60s clip with `large-v3` int8 `batch_size=4`; measure VRAM peak and wall time | No OOM; VRAM peak logged; transcript sane |
| 0.4 | **Spike C — unattended SDK:** `claude-agent-sdk` session with CCR in `mcp_servers`, `permission_mode="bypassPermissions"`, allowlist `[Read, Glob, mcp__ccr__gcc_search, mcp__ccr__gcc_context, mcp__ccr__gcc_commit]`; completes a `gcc_search` + reads a test image, no prompts, subscription auth | Runs headless to completion; response references image content |
| 0.5 | `recoder doctor` v1 wrapping spikes A–C as checks + ffmpeg/HF-token/disk checks | `recoder doctor` green on this machine |

**Exit gate:** all three spikes pass. If any fails, redesign that seam before continuing (this is the cheap moment to do it).

## Phase 1 — Capture layer

**Goal:** reliable during-call recording with zero heavy deps imported.

| # | Task | Acceptance |
|---|------|------------|
| 1.1 | `MeetingStore`: create meeting folder (`meetings/YYYY-MM-DD-HHMM-slug/`), `meta.json` state machine (`recorded → transcribed → diarized → analyzed → committed → done`) with atomic writes; unit tests for transitions + resume logic | pytest green |
| 1.2 | `AudioRecorder`: two threads (loopback + mic) → streaming FLAC, wall-clock timing index (JSONL sidecar: chunk offset ↔ timestamp), device re-open on error/resume with gap marker, clean stop | 30-min recording: files valid, index monotonic, kill -9 mid-recording loses <1s |
| 1.3 | `SnapshotCapturer`: 20s timer, `pygetwindow` title match (Zoom/Meet/Teams patterns, config-extensible) → `mss` region grab → resize ≤1568px → phash dedup (Hamming ≤4) → JPEG q80 with timestamp filenames | Unit test on fixture frames: dupes dropped, distinct kept |
| 1.4 | `recoder record` CLI: starts audio + snapshots, Ctrl+C stops cleanly, prompts for title/context or takes flags; sets state `recorded` | Real 5-min test call recorded end-to-end |
| 1.5 | Fixture pack: 2-voice sample WAV pair + sample frames checked into `tests/fixtures/` (generated from a scripted fake meeting) | Used by later phases |

**Exit gate:** a real meeting recorded (can be a YouTube video + talking) produces a complete, well-formed meeting folder.

## Phase 2 — Transcription + diarization

**Goal:** meeting folder in state `recorded` → diarized `transcript.json`/`transcript.md`.

| # | Task | Acceptance |
|---|------|------------|
| 2.1 | `Transcriber` protocol + `WhisperXTranscriber`: per-channel transcription, int8/batch 4, explicit model load→free sequencing (transcribe → free → align → free → diarize) | Fixture transcribes without OOM alongside a VRAM assertion in the test |
| 2.2 | English-only alignment routing: per-segment language from faster-whisper metadata + Devanagari-script heuristic; Hindi/mixed segments keep segment timestamps | Unit test with mixed-language fixture segments |
| 2.3 | Pyannote diarization on system channel; speaker turns cached to disk | Fixture yields ≥2 speakers |
| 2.4 | Merge: mic segments ("Me") + diarized system segments, ordered by wall-clock via timing index; word-overlap for English, midpoint-overlap for segment-level; render `transcript.md` | Golden-file test on fixtures |
| 2.5 | Pipeline runner: `recoder process <folder>` executes stages per state machine, checkpoints after each, `pipeline.log`, resumable after crash | Kill mid-diarization → rerun skips transcription, completes |

**Exit gate:** Phase 1's real recording produces a readable, correctly speaker-labeled transcript.

## Phase 3 — Claude analysis + CCR write-back

**Goal:** transcript + frames + memory → `summary.md`, committed to CCR.

| # | Task | Acceptance |
|---|------|------------|
| 3.1 | `AnalysisSession`: SDK config from Spike C productionized (cwd = meeting folder, mcp_servers, permission mode, allowlist, model from config); retry ×3 with backoff → error state | Unit-testable config builder; doctor probe reuses it |
| 3.2 | Prompt assembly: transcript, metadata, frame inventory (filename + timestamp table), fixed `summary.md` section contract (TL;DR / topics with screen references / decisions / action items with owners / open questions / project mapping / speaker-name guess table); instruction that frames may contain unrelated desktop content | Snapshot test of assembled prompt |
| 3.3 | Summary validation: required sections present, else one retry with correction message | Malformed-response test |
| 3.4 | CCR write-back: `gcc_commit` of distilled summary from within the session (it has the tool) with fallback to direct call from Python if the model skipped it; state → `committed` | Verified in CCR memory after run |
| 3.5 | `recoder replay <folder>`: full pipeline on an existing folder (the acceptance test forever after) | Real recording → good summary referencing screen content and CCR context |

**Exit gate:** `summary.md` for the Phase 1 real recording is visibly smarter than a bare transcript summary (cites what was on screen; maps to a CCR project).

## Phase 4 — Web UI

**Goal:** no more CLI needed for daily use.

| # | Task | Acceptance |
|---|------|------------|
| 4.1 | FastAPI app (`localhost:8377`), single-page UI, no build step (vanilla JS + htmx-style polling; no node toolchain) | Page loads |
| 4.2 | Record screen: start/stop, title (pre-filled from detected meeting window) + context note, elapsed, disk indicator, snapshot count | Full meeting driven from UI |
| 4.3 | Processing screen: stage progress from `meta.json` polling; errors show a resume button | Kill pipeline → resume from UI works |
| 4.4 | Archive: meeting list, transcript/summary/frames viewer, reprocess action (rerun analysis with corrected context/speaker names) | Reprocess produces updated summary |
| 4.5 | Launch ergonomics: `recoder ui` starts server + opens `chrome --app=`; desktop shortcut; optional Startup-folder entry | Icon-click to recording in <10s |

**Exit gate:** one real meeting handled end-to-end without touching a terminal.

## Phase 5 — Hardening + docs

| # | Task | Acceptance |
|---|------|------------|
| 5.1 | `doctor` final: all checks incl. unattended-SDK probe; clear remediation text per failure | Fresh-eyes run passes |
| 5.2 | Disk-space guard at record start (<5GB warn); optional retention config (default keep-forever) | Unit tests |
| 5.3 | Sleep/lid-close resilience test on real hardware; gap markers verified in transcript output | Documented behavior |
| 5.4 | README: setup (CUDA torch, HF token/license, claude login), daily usage, troubleshooting | Someone-else-could-install quality |

## Explicitly not in this plan (per spec §8)

Live transcript/panel, calendar integration, hosted STT implementation (interface seam only), bot-join, Fathom import.

## Dependencies (pinned at Phase 0)

`pyaudiowpatch`, `soundfile`, `mss`, `pygetwindow`, `imagehash`, `Pillow`, `whisperx` (brings faster-whisper + pyannote), `torch` (CUDA build), `claude-agent-sdk`, `fastapi`, `uvicorn`, `typer`, `pytest`.
