# Recoder

A local, personal meeting recorder for Windows that produces **context-aware**
summaries. It captures both sides of a call plus screen snapshots, transcribes
with speaker labels, then has Claude analyze the meeting **with your project
memory mounted** — so the summary knows what you were building last week, not
just what was said in the last hour.

Built to replace Fathom. Fathom's transcripts are fine, but they are
context-free: paste one into an AI and it can only summarize the words. Recoder
treats the transcript as one input among several — screen frames, your meeting
context note, and the accumulated memory of your active code projects — and
runs the intelligence on your **Claude Code subscription** (no API key, no
per-token cost).

Everything runs and stays on your machine, with two exceptions you control:
audio is sent to [Gladia](https://gladia.io) for transcription, and analysis
runs through your Claude subscription.

---

## How it works

```
                ┌─────────────────────────────────────────────────┐
                │                RECORD (live)                    │
                │  mic.wav  ← microphone      (always "Me")       │
                │  system.wav ← WASAPI loopback (everyone else)   │
                │  frames/  ← screen snapshots of meeting window  │
                │             (perceptual-hash deduped, ~q80 JPEG)│
                │  timing-index.jsonl ← wall-clock alignment      │
                └───────────────────────┬─────────────────────────┘
                                        │ stop
                                        ▼
       ┌───────────────┐   Gladia API (async, diarization, en/hi)
       │  TRANSCRIBE   │   mic channel → segments labeled "Me"
       │  + DIARIZE    │   system channel → SPEAKER_1, SPEAKER_2…
       └───────┬───────┘   merged on the wall-clock index
               ▼
       ┌───────────────┐   Claude Agent SDK session (subscription auth)
       │    ANALYZE    │   inputs: transcript + screen frames + context note
       │               │   + CCR memory of THIS project
       │               │   + CCR memory of your ACTIVE projects (auto-routed)
       └───────┬───────┘   output: summary.md (8 structured sections)
               ▼
       ┌───────────────┐
       │    COMMIT     │   summary distilled back into CCR memory, so the
       └───────────────┘   NEXT meeting remembers this one
```

Each meeting is a folder that moves through a resumable state machine
(`recording → recorded → transcribed → diarized → analyzed → committed →
done`). Any stage can fail and be resumed with `recoder process` — completed
stages are never redone.

### Why two audio channels?

The microphone and the system loopback are recorded as **separate files** and
never mixed. That makes speaker attribution partly deterministic: everything on
the mic channel is you ("Me"); Gladia's diarization only has to distinguish the
remote speakers on the system channel. A wall-clock timing index aligns the two
channels when merging.

### The memory layer (what makes summaries smart)

Recoder uses [CCR](https://github.com/qbit-glitch/ccr) (Continuous Context
Retention) — a per-project, git-like memory store with an MCP server. Two
mechanisms connect meetings to your work:

1. **Routing (automatic, per meeting).** Before analysis, Recoder reads CCR's
   global project registry and mounts, read-only, the memory stores of projects
   you used in the last 7 days or whose names match the meeting title/context
   (capped at 4). The analysis prompt tells Claude to search them before
   summarizing. A call about "the billing bug" gets summarized by a model that
   has read your week of billing work — no configuration needed.

2. **Consolidation (on demand).** If you work in git worktrees, each worktree
   accumulates its own CCR store. `recoder consolidate` incrementally distills
   a worktree's *new* commits (tracked by a per-source watermark) into the
   parent project's store as `[from <source>]` milestone commits. Sources are
   never deleted — archiving a finished worktree's store is a separate,
   explicit `--archive` flag.

Meeting summaries are themselves committed back to the Recoder store, so
meetings remember previous meetings.

---

## Requirements

- **Windows 10/11** (audio capture uses WASAPI loopback — Windows-only).
- **Python 3.11+** and [uv](https://docs.astral.sh/uv/).
- **Claude Code CLI**, logged in with a subscription (`claude login`). No
  Anthropic API key needed.
- **Gladia API key** — free at [app.gladia.io](https://app.gladia.io)
  (10 h/month free, includes diarization; $0.61/h after).
- ~5 GB free disk on the drive holding `meetings/`.

## Setup

### One-shot (recommended)

```powershell
git clone <this-repo> recoder
cd recoder
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

The script installs project dependencies, sets up CCR in `~\.ccr\.venv`,
creates `recoder.toml` from the example, and runs `recoder doctor`. Then edit
`recoder.toml` and paste your Gladia key.

### Manual

```powershell
uv sync                                      # project deps
python -m venv $HOME\.ccr\.venv              # CCR memory engine
& $HOME\.ccr\.venv\Scripts\python.exe -m pip install ccr-memory
copy recoder.toml.example recoder.toml       # then paste your Gladia key
claude login                                 # if not already
uv run recoder doctor                        # verify — 0 failures expected
```

`recoder doctor` prints PASS/FAIL/SKIP per check with a fix for every FAIL;
its exit code is the number of failures. `recoder doctor --full` additionally
runs a live unattended Claude SDK + CCR probe.

### Desktop shortcut (optional)

Point a shortcut at `<repo>\.venv\Scripts\pythonw.exe -m recoder app` with the
repo as the working directory — a native window, no console, no server to
start by hand.

---

## Usage

### The app

```powershell
uv run recoder app
```

Opens a native window (pywebview + in-process server): start/stop recording
with a title and context note, watch pipeline progress, browse past meetings,
read summaries, view captured frames, and retry failed stages. The window
refuses to close while a recording is live.

`uv run recoder ui` is the fallback that serves the same interface to a
browser window instead.

### Terminal recording

```powershell
uv run recoder record --title "Sprint planning" --context "focus: billing revamp"
# ... meeting happens ... Ctrl+C stops recording and processing starts
```

The one-line `--context` note is worth writing: it seeds both the analysis and
the project-memory routing.

### Processing and reprocessing

```powershell
uv run recoder process <meeting-folder>   # run/resume the pipeline
uv run recoder replay  <meeting-folder>   # print the stored summary
```

`process` picks up wherever the meeting stopped — after a crash, an expired
Gladia upload, or a mid-pipeline reboot, it reruns only the missing stages.

### Memory maintenance

```powershell
uv run recoder memory-clean            # preview registry junk (dry-run)
uv run recoder memory-clean --apply    # prune it (backs up projects.json first)
```

Prunes CCR's global registry: build artifacts that self-registered
(node_modules, __pycache__), empty stores unused for 30+ days, and ghosts
whose project directory no longer exists.

```powershell
uv run recoder consolidate <source-worktree> <target-project>
uv run recoder consolidate-group <name>          # groups from recoder.toml
```

Distills new worktree memory into the parent store (see *The memory layer*
above). Incremental: each run only processes commits newer than the last
watermark, recorded in `consolidation-state.json`. Add `--archive` only when
retiring a worktree for good — it moves (never deletes) the source store and
deregisters it.

---

## Configuration

Copy `recoder.toml.example` → `recoder.toml` (gitignored). The only required
value is `gladia_api_key` (or the `GLADIA_API_KEY` env var). Everything else
has portable defaults; the example file documents the common overrides:
meeting storage location, UI port, snapshot cadence, window-title patterns to
capture, routing recency/caps, and consolidation groups.

## Anatomy of a meeting folder

```
meetings/2026-07-05-1447-sprint-planning/
├── meta.json            # state machine + title/context (atomic writes)
├── mic.wav              # your voice
├── system.wav           # everyone else (loopback)
├── timing-index.jsonl   # wall-clock alignment map
├── frames/              # deduped screen snapshots + index.jsonl
├── gladia-mic.json      # raw STT responses (kept for debugging/reruns)
├── gladia-system.json
├── transcript.json      # merged, speaker-labeled segments
├── transcript.md        # human-readable [MM:SS] Speaker: text
└── summary.md           # the context-aware summary (8 sections)
```

Nothing in `meetings/` is ever uploaded anywhere except the two WAV files sent
to Gladia for transcription. The folder is the source of truth; delete a
meeting by deleting its folder.

## Privacy & cost model

| Data | Where it goes |
|---|---|
| Audio (mic + system WAV) | Uploaded to Gladia for STT, per their retention policy |
| Screen frames | Local only; read by Claude during analysis via your subscription |
| Transcript, summary, memory | Local only (`meetings/`, `.ccr/` stores) |
| Intelligence | Claude Code **subscription** auth — no API key, no metered tokens |

Recurring cost: Gladia beyond 10 h/month ($0.61/h). That's it.

## Development

```powershell
uv run pytest                 # 144 tests, no hardware/network needed
uv run pytest tests/test_routing.py -v
```

- `recoder/capture/` — audio + snapshot capture (hardware injectable for tests)
- `recoder/pipeline/` — Gladia transcription, channel merge, resumable runner
- `recoder/analysis/` — Claude session, prompts, routing, consolidation
- `recoder/web/` — FastAPI API, SPA, pywebview desktop shell
- `spikes/` — live proof scripts (real audio/APIs; not run by pytest)
- `docs/superpowers/specs/` — design docs; `plans/` — the phased build plan

An optional fully-local STT fallback (whisperX, `uv sync --extra ml`) exists
but is not the supported path; it additionally needs a CUDA torch build, an
HF token, ffmpeg, and currently `transformers<5`.

## Known limitations

- Windows-only (WASAPI loopback capture).
- Snapshots grab the meeting window's screen region — a window covering it
  will be captured instead (noted to the analyst in the prompt).
- Post-meeting processing only; there is deliberately no live assistant.
- Single user, single machine. No accounts, no server, no deployment.
