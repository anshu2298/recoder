# Recoder — Design Spec

**Date:** 2026-07-05
**Status:** Draft for review
**Owner:** Anshul (single user, local-only)

## 1. Problem

Fathom produces decent meeting transcripts, but the transcript is the *only* source of truth. Pasted into Claude afterwards, it loses everything a transcript cannot carry: what was shown on screen, who the participants are, and how the discussion relates to the user's active projects. Summaries come out context-blind.

## 2. Goal

A local, personal meeting recorder that:

1. Records meetings the user attends on this PC (any platform — Zoom, Meet, Teams) without a visible bot.
2. Captures **more than words**: screen snapshots of the meeting window, meeting metadata, and the user's project memory (CCR).
3. After the call, produces a context-aware summary (decisions, action items with owners, project mapping) using the user's **Claude Code subscription** — no API key.
4. Writes the outcome back into CCR memory so future meetings and future coding sessions both remember it.

### Non-goals (v1)

- No live/in-call transcription or analysis (deliberately cut for simplicity).
- No bot joining meetings the user doesn't attend.
- No deployment, no multi-user, no cloud backend.
- No calendar/email integration (a manual one-line context field covers this in v1).

## 3. Constraints

- **Hardware:** RTX 2050 (4GB VRAM), i5-12450H, 16GB RAM, Windows 11.
- During-call footprint must be minimal (Zoom/Meet + Chrome already load the machine). All heavy compute runs post-call when the machine is idle.
- Meetings are mostly English with some Hindi/Hinglish → multilingual Whisper `large-v3` for the final transcript.
- Claude access exclusively via Claude Agent SDK (Python) with the existing `claude` CLI subscription login.

## 4. Architecture

Python 3.11+ monorepo at `G:\recoder`. Four units, each independently testable:

```
recoder/
  capture/      # during-call: audio + screen snapshots + status UI backend
  pipeline/     # post-call: transcribe → diarize → analyze → commit
  analysis/     # Claude Agent SDK integration (prompts, CCR MCP wiring)
  web/          # FastAPI app: record button, context field, meeting archive
  meetings/     # data dir (gitignored) — one folder per meeting
```

### 4.1 Capture service (during call — lightweight only)

- **Audio:** two synchronized streams via WASAPI:
  - `audio-system.flac` — loopback of default output device (everyone else).
  - `audio-mic.flac` — default microphone (the user).
  - Library: `pyaudiowpatch` (WASAPI loopback) + `soundfile` (streaming FLAC write). Audio is flushed to disk continuously; a crash loses at most the last buffer (< 1s).
  - Each chunk written is timestamped (wall-clock) in a sidecar index so the two streams can be aligned later without assuming equal sample clocks (mic/loopback clock drift on long calls).
- **Screen snapshots:** every 20s, capture the screen region occupied by the meeting window (located via `pygetwindow` title match: Zoom/Meet/Teams patterns; fallback: primary monitor full-screen grab). **Known limitation (accepted):** this is a coordinate-region grab — if the meeting window is covered or minimized, the frame captures whatever is on top instead. Dedup discards repeats and the analysis prompt tells Claude some frames may be unrelated desktop content; true background-window capture (PrintWindow/DWM) is deliberately out of scope for v1.
  - Dedup with perceptual hash (`imagehash.phash`, Hamming distance ≤ 4 → skip). A static screen share costs ~nothing; a demo or slide deck yields one frame per distinct view.
  - Saved as JPEG (quality 80, max 1568px wide — Claude vision's effective ceiling) into `frames/` with timestamps.
- **Meeting metadata:** at record start, the UI offers two optional fields: title (pre-filled from the meeting window title) and a one-line context note ("weekly sync with Rahul about billing"). Saved to `meta.json`.
- CPU/RAM budget during call: < 5% CPU, < 300MB RAM, zero GPU.

### 4.2 Post-meeting pipeline (heavy, runs on stop)

A resumable state machine; state persisted in `meta.json` (`recorded → transcribed → diarized → analyzed → committed → done`). Each stage is idempotent: rerunning the pipeline skips completed stages, so a crash never redoes finished work.

1. **Transcribe + diarize:** the two channels are transcribed **separately** (never mixed — mixing would destroy the channel information that makes "Me" labeling deterministic):
   - `audio-mic.flac` → whisperX `large-v3` → every segment labeled "Me".
   - `audio-system.flac` → whisperX `large-v3`, then pyannote diarization (one-time free HuggingFace token, documented in setup) → SPEAKER_1, SPEAKER_2…
   - The two segment lists are merged into one timeline using the wall-clock timing index from capture. Output: `transcript.json` (segments with speaker, start, end, text) + `transcript.md` (readable).
   - **VRAM plan (hard requirement, 4GB card):** `compute_type="int8"`, `batch_size=4`. Models run strictly sequentially and are explicitly unloaded between phases: whisper transcribe → free → alignment model → free → pyannote. Default whisperX settings (batch 16, co-resident alignment model) will CUDA-OOM on this card.
   - **Hinglish handling (accepted trade-off):** whisperX word-level alignment uses a single-language wav2vec2 model and degrades on code-switched speech. Word-level alignment runs only for segments whisper marks as English; Hindi/mixed segments keep segment-level timestamps, and speaker assignment for those uses segment-midpoint overlap with diarization turns. Slightly coarser speaker boundaries on Hindi speech is acceptable; transcription itself (large-v3) handles code-switching at segment level.
   - **Pluggable STT interface:** `Transcriber` protocol with the local whisperX implementation as default; a hosted implementation (e.g. Groq Whisper) can be added later behind a config flag if local speed ever disappoints. Privacy trade-off documented at that point, not now.
2. **Analyze (Claude):** one Claude Agent SDK session, configured for unattended operation:
   - **Inputs:** the diarized transcript and meeting metadata (title, context note, duration, date) go in the prompt. **Frames are delivered via the filesystem**: the session's working directory is the meeting folder and the prompt lists the `frames/` inventory with timestamps; Claude reads the images it deems relevant with the Read tool (multi-turn, so no per-request image cap applies — no sampling/cap logic needed on our side).
   - **CCR access:** the CCR MCP server is wired explicitly into the SDK `mcp_servers` config (the interactive Claude Code hook that auto-loads CCR does NOT apply to programmatic SDK sessions). Tool names confirmed against the installed CCR: `gcc_search`, `gcc_context`, `gcc_commit`.
   - **Permissions:** `permission_mode="bypassPermissions"` with an `allowed_tools` allowlist (Read, Glob, the three CCR tools). Without this the unattended pipeline hangs on tool-approval prompts — this is the most likely silent failure mode and gets an explicit `doctor` check.
   It produces `summary.md` with fixed sections: TL;DR, discussion by topic (with references to what was on screen), decisions, action items (owner → task → due if stated), open questions, project mapping (which CCR projects this touches), and a speaker-name guess table (SPEAKER_1 = "probably Rahul — addressed by name at 14:32") for the user to confirm.
3. **Commit back:** `gcc_commit` into the recoder project's CCR memory with the meeting summary, so cross-meeting continuity accrues automatically. The raw transcript stays on disk; only the distilled summary enters memory.

Pipeline failures surface in the UI with a "resume" button; every stage logs to `pipeline.log` in the meeting folder.

### 4.3 Web UI

FastAPI + uvicorn on `localhost:8377`, launched as a Chrome `--app` window via a desktop shortcut (no Electron — saves ~300MB RAM; identical UI code if we ever wrap it in pywebview).

Three screens, one HTML page:
- **Record:** big start/stop, title + context fields, elapsed time, disk-write indicator, snapshot count.
- **Processing:** pipeline stage progress for the just-finished meeting.
- **Archive:** list of past meetings → transcript, frames, summary; a "reprocess" action (rerun analysis with a corrected context note or confirmed speaker names).

### 4.4 Storage layout

```
meetings/2026-07-05-1430-weekly-billing-sync/
  audio-mic.flac
  audio-system.flac
  frames/000123_143512.jpg ...
  meta.json          # state machine, metadata, timing index
  transcript.json    # diarized segments
  transcript.md
  summary.md
  pipeline.log
```

Raw audio is retained (it is the true source of truth; re-transcription stays possible). A config knob sets optional auto-cleanup age; default: keep forever.

## 5. Error handling

- **Recording is sacred.** The capture unit has zero imports from pipeline/analysis/web logic. Snapshot or UI failures never interrupt audio writing; audio failures stop the recording with a loud UI error.
- Pipeline stages: idempotent + checkpointed (see 4.2). Claude/SDK failures retry with backoff (3 attempts) then park the meeting in a resumable error state.
- Machine sleep/lid-close during recording: capture threads re-open device streams on resume and log a gap marker into the timing index.
- Disk space checked at record start (warn < 5GB free; a 1h meeting ≈ 600MB audio + frames).

## 6. Setup prerequisites (documented in README)

- Python 3.11+, CUDA-enabled PyTorch, ffmpeg.
- HuggingFace token with pyannote model license accepted (free, one-time).
- `claude` CLI logged in (already true on this machine).
- CCR installed globally (already true).

## 7. Testing

- **Fixtures:** a short two-voice recorded WAV pair + a folder of sample frames, checked into `tests/fixtures/`.
- **Unit:** audio chunk/alignment logic, phash dedup thresholds, state-machine transitions and resume, transcript merge (Me-channel + diarized speakers), summary prompt assembly.
- **Integration:** `--replay <meeting-folder>` mode pushes an existing recording through the full pipeline (including a real Claude call) without holding a meeting. This is also the manual acceptance test.
- **Hardware smoke test:** a `recoder doctor` command verifies WASAPI loopback device, CUDA availability, whisper model load, HF token, and Claude SDK auth.

## 8. Future (explicitly deferred)

- Live transcript / live context panel (the cut v1 scope — capture layer already produces everything it would need).
- Google Calendar integration to auto-fill title/attendees.
- Hosted STT config flag; bot-join for unattended meetings; Fathom history import.
