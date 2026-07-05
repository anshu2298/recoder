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

### B. Consolidation (finished work)

`recoder consolidate <source-worktree> <target-project> [--apply]` — run when a feature ships and its worktree is about to be deleted:

- A Claude session mounts the source store read-only and the target store writable, distills the source's full history into 3–8 milestone commits on the target, each titled `[from <source>]` with provenance and date range.
- `--apply` then **archives** the source's `.ccr` (moved, never deleted — granularity is preserved on disk) and deregisters it. Default is dry-run.

### Division of labor

Active worktrees are never consolidated — routing reads their original, fully granular stores. Consolidation is end-of-life compaction only, so the registry stays small and knowledge survives worktree deletion.
