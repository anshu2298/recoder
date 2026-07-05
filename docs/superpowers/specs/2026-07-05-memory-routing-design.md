# Recoder — Memory Routing & Consolidation (addendum)

**Date:** 2026-07-05 · extends the main design spec §4.2 step 2.

## Problem

CCR memory is keyed to a directory. Two consequences:

1. **Meetings were blind to code-project memory.** The analysis session mounted only the recorder's own store, so a call about a Sherpa feature could not see the week's work in that worktree. Correlation relied entirely on the user's one-line context note.
2. **Worktrees fragment memory.** Each git worktree accumulates an independent store (Sherpa: 7+ stores, ~250 commits scattered). When a worktree finishes and is deleted, its memory is orphaned.

## Design

### A. Meeting → project routing (active work)

Before analysis, the pipeline reads CCR's global registry (`~/.ccr/projects.json`) and auto-mounts as additional read-only MCP servers:

- every non-junk store with commits whose `last_used` falls within the recency window (default 7 days) — "what I'm working on right now", which automatically covers multi-feature/multi-worktree meetings, plus
- stores whose name matches keywords from the meeting title/context note,
- capped (default 4) and always alongside the recorder's own store (the only one with `gcc_commit` rights).

The analysis prompt lists the mounted stores with the reason each was mounted and instructs Claude to search them before summarizing and to cite concrete prior work in the summary. No user configuration required; recency does the routing.

Junk filtering (node_modules, `__pycache__`, meeting folders, empty names, drive roots) plus a `recoder memory-clean` command for the registry itself.

### B. Consolidation (incremental checkpoint sync)

`recoder consolidate <source-worktree> <target-project> [--archive] [--archive-dir ...] [--yes]` — an **incremental checkpoint sync** run any time, not end-of-life archiving. Sources stay alive, registered, and untouched by default; each run distills only the commits NEWER than a per-source watermark.

- A Claude session mounts the source store read-only and the target store writable. It examines only source commits with an id greater than the stored watermark and distills them into 1–5 milestone commits on the target, each titled `[from <source>]` with provenance and date range. Distillation always writes target commits — that is the operation.
- **Watermark state** lives in `consolidation-state.json` (path from `config.consolidation_state_path`, gitignored), keyed by normalized source path: `{ "<norm path>": {"target", "last_commit_id": "C047", "last_consolidated_at", "runs"} }`. Written atomically (tmp + `os.replace`); missing/corrupt reads degrade to `{}`. CCR commit ids are sequential per store (`C<nnn>`), so the watermark is a simple high-water mark.
- **Reply protocol.** The session ends its reply with `HIGHEST_SOURCE_COMMIT: C<nnn>` reporting the highest source commit examined; the caller advances the watermark to it. If there are no newer commits it replies exactly `NO_NEW_COMMITS since <id>` and writes nothing — the watermark is left untouched, no error. A reply that created commits but omitted the marker triggers one corrective turn asking only for the marker; if it is still missing, the run errors **without** advancing the watermark (the next run re-covers the span and target-side dedup absorbs repeats). The watermark is advanced BEFORE any archive step.
- **Archiving is opt-in** via `--archive` (replaces the old `--apply`/`--dry-run` semantics): after the sync + watermark update it **archives** the source's `.ccr` (moved, never deleted — granularity is preserved on disk) and deregisters it. `--yes` skips the archive confirmation. The default mode touches neither the source directory nor the registry.
- **Groups.** `[consolidation_groups.<name>]` tables in `recoder.toml` (`target = ...`, `sources = [...]`) drive `recoder consolidate-group <name> [--archive] [--yes]`, which syncs each source sequentially against the group target, prints a per-source outcome (n commits / NO NEW / error), continues past individual failures, and exits with the number of failures.

### Division of labor

Routing (A) reads active worktrees' original, fully granular stores; consolidation (B) incrementally checkpoints their new work into the parent so the parent store accumulates milestones over time. Sources remain alive until an explicit `--archive` retires a finished worktree, so knowledge survives worktree deletion while the registry stays manageable.
