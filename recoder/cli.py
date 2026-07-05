from __future__ import annotations

from pathlib import Path

import typer

app = typer.Typer(
    name="recoder",
    help="Local, personal, context-aware meeting recorder.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command()
def doctor(
    full: bool = typer.Option(
        False,
        "--full",
        help="Also run the unattended Claude SDK + CCR probe (Spike C).",
    ),
) -> None:
    """Verify this machine can run recoder end-to-end."""
    from recoder.doctor import run_doctor

    raise typer.Exit(code=run_doctor(full=full))


@app.command()
def record(
    title: str = typer.Option(None, "--title", "-t", help="Meeting title (prompted if omitted)."),
    context: str = typer.Option(None, "--context", "-c", help="One-line context note for the analysis."),
    process_after: bool = typer.Option(
        True, "--process/--no-process", help="Run the post-meeting pipeline automatically on stop."
    ),
) -> None:
    """Record a meeting (audio + screen snapshots). Ctrl+C to stop."""
    import time

    from recoder.capture.audio import AudioRecorder
    from recoder.capture.snapshots import SnapshotCapturer
    from recoder.config import load_config
    from recoder.store import MeetingState, MeetingStore

    config = load_config()
    if title is None:
        title = typer.prompt("Meeting title", default="meeting")
    if context is None:
        context = typer.prompt("Context note (optional)", default="", show_default=False)

    store = MeetingStore(config)
    meeting = store.create_meeting(title, context or None)
    typer.echo(f"Meeting folder: {meeting.folder}")

    audio = AudioRecorder(meeting.audio_mic, meeting.audio_system, meeting.timing_index)
    snaps = SnapshotCapturer(meeting.frames_dir, config)
    audio.start()
    snaps.start()
    started = time.monotonic()
    typer.echo("Recording. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(5)
            mins, secs = divmod(int(time.monotonic() - started), 60)
            typer.echo(f"  [{mins:02d}:{secs:02d}] frames saved: {snaps.saved_count}", err=False)
    except KeyboardInterrupt:
        typer.echo("Stopping...")
    finally:
        snap_result = snaps.stop()
        audio_result = audio.stop()
    meeting.advance(MeetingState.recorded)
    typer.echo(
        f"Recorded {audio_result.duration_s:.0f}s audio, "
        f"{snap_result.frames_saved} frames ({snap_result.frames_skipped_dup} dups skipped)."
    )
    if process_after:
        typer.echo("Running post-meeting pipeline...")
        _run_pipeline(str(meeting.folder))
    else:
        typer.echo(f"Process later with: recoder process {meeting.folder}")


def _run_pipeline(folder: str) -> None:
    from recoder.config import load_config
    from recoder.pipeline.runner import run_pipeline

    meeting = run_pipeline(Path(folder), load_config())
    typer.echo(f"Pipeline finished; state: {meeting.state.value}")
    if meeting.summary_md.exists():
        typer.echo(f"Summary: {meeting.summary_md}")


@app.command()
def process(folder: str = typer.Argument(...)) -> None:
    """Run (or resume) the post-meeting pipeline on a recorded folder."""
    _run_pipeline(folder)


@app.command()
def replay(folder: str = typer.Argument(...)) -> None:
    """Re-run the full pipeline on an existing meeting folder (acceptance test)."""
    from recoder.config import load_config
    from recoder.store import MeetingState, MeetingStore

    meeting = MeetingStore(load_config()).load(Path(folder))
    meeting.update_meta(state=MeetingState.recorded.value)
    _run_pipeline(folder)


@app.command(name="memory-clean")
def memory_clean(
    apply: bool = typer.Option(
        False, "--apply", help="Write the cleaned registry (default: dry run)."
    ),
) -> None:
    """Prune junk/stale entries from the global CCR registry.

    Junk = node_modules, __pycache__, .claude, site-packages, meeting folders,
    drive roots, or empty names. Also drops empty stores (0 commits) unused for
    over 30 days. Dry run by default; ``--apply`` backs up projects.json first.
    """
    from datetime import datetime, timezone

    from recoder.analysis import routing
    from recoder.config import load_config

    config = load_config()
    registry_path = config.ccr_registry_path
    raw = routing.read_raw_registry(registry_path)
    if not raw:
        typer.echo(f"Registry empty or unreadable: {registry_path}")
        return

    now = datetime.now(timezone.utc)
    prunable: list[dict] = []
    kept: list[dict] = []
    for d in raw:
        entry = routing.entry_from_dict(d)
        if routing.is_prunable(entry, now=now):
            prunable.append(d)
        else:
            kept.append(d)

    typer.echo(f"Registry: {registry_path}")
    typer.echo(f"Total entries: {len(raw)}  keep: {len(kept)}  prune: {len(prunable)}")
    typer.echo("")
    if prunable:
        typer.echo(f"{'commits':>7}  {'last_used':<20}  path")
        for d in prunable:
            e = routing.entry_from_dict(d)
            last = e.last_used.date().isoformat() if e.last_used else "unknown"
            typer.echo(f"{e.commit_count:>7}  {last:<20}  {e.path}")
    else:
        typer.echo("Nothing to prune.")
        return

    if not apply:
        typer.echo("")
        typer.echo("DRY RUN -- re-run with --apply to write the cleaned registry.")
        return

    backup = routing.backup_registry(registry_path)
    routing.write_registry(registry_path, kept)
    typer.echo("")
    typer.echo(f"Backed up to: {backup}")
    typer.echo(f"Wrote cleaned registry with {len(kept)} entries.")


@app.command()
def consolidate(
    source: str = typer.Argument(..., help="Worktree project dir to consolidate FROM."),
    target: str = typer.Argument(..., help="Parent project dir to consolidate INTO."),
    archive: bool = typer.Option(
        False,
        "--archive",
        help="After syncing, archive the source store + deregister it "
        "(default: source stays alive and registered).",
    ),
    archive_dir: str = typer.Option(
        None, "--archive-dir", help="Override the archive base directory."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt before archiving."
    ),
) -> None:
    """Incrementally sync a worktree's NEW CCR commits into its parent store.

    Distills only source commits newer than the per-source watermark into 1-5
    milestone commits on the target and advances the watermark. The source store
    stays alive and registered. ``--archive`` additionally archives + deregisters
    the source after syncing.
    """
    from recoder.analysis.consolidate import ConsolidationError, consolidate as _run
    from recoder.config import load_config

    config = load_config()
    src = Path(source)
    tgt = Path(target)
    mode = "archive" if archive else "incremental"

    if archive and not yes:
        typer.confirm(
            f"--archive will ARCHIVE {src / '.ccr'} and deregister {src.name} "
            "after syncing. Continue?",
            abort=True,
        )

    try:
        result = _run(
            src,
            tgt,
            config,
            mode=mode,
            archive_dir=Path(archive_dir) if archive_dir else None,
        )
    except ConsolidationError as exc:
        typer.echo(f"Consolidation failed: {exc}", err=True)
        raise typer.Exit(code=1)

    if result.no_new:
        since = result.highest_source_commit or "start"
        typer.echo(f"{src.name} -> {tgt.name}: no new commits since {since}.")
        return

    typer.echo(
        f"Synced {src.name} -> {tgt.name}: {len(result.commit_ids)} commit(s) "
        f"[{', '.join(result.commit_ids)}]; watermark now "
        f"{result.highest_source_commit}."
    )
    if archive:
        typer.echo(f"Archived source store to: {result.archived_to}")
        typer.echo(
            "Registry entry removed."
            if result.registry_updated
            else "Registry entry not found (nothing to remove)."
        )


@app.command(name="consolidate-group")
def consolidate_group(
    name: str = typer.Argument(..., help="Name of the [consolidation_groups.*] table."),
    archive: bool = typer.Option(
        False,
        "--archive",
        help="Archive + deregister each source after syncing.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt before archiving."
    ),
) -> None:
    """Sync every source in a named group against the group's target.

    Sources are processed sequentially and independently; individual failures are
    reported and skipped past. Exit code equals the number of failed sources.
    """
    from recoder.analysis.consolidate import consolidate_group as _run_group
    from recoder.config import load_config

    config = load_config()
    groups = config.consolidation_groups or {}
    if name not in groups:
        available = ", ".join(sorted(groups)) or "(none configured)"
        typer.echo(
            f"Unknown consolidation group: {name!r}. Available: {available}",
            err=True,
        )
        raise typer.Exit(code=1)

    group = groups[name]
    mode = "archive" if archive else "incremental"
    if archive and not yes:
        typer.confirm(
            f"--archive will archive + deregister every source in group "
            f"{name!r} after syncing. Continue?",
            abort=True,
        )

    typer.echo(f"Consolidating group {name!r} -> {group.get('target')}")
    outcomes = _run_group(name, config, mode=mode)

    failures = 0
    for outcome in outcomes:
        label = Path(outcome.source).name
        if outcome.error is not None:
            failures += 1
            typer.echo(f"  {label}: ERROR {outcome.error}", err=True)
        elif outcome.result is not None and outcome.result.no_new:
            since = outcome.result.highest_source_commit or "start"
            typer.echo(f"  {label}: NO NEW (since {since})")
        elif outcome.result is not None:
            typer.echo(
                f"  {label}: {len(outcome.result.commit_ids)} commit(s) -> "
                f"watermark {outcome.result.highest_source_commit}"
            )

    ok = len(outcomes) - failures
    typer.echo(f"Done: {ok} ok, {failures} failed.")
    raise typer.Exit(code=failures)


@app.command(name="app")
def desktop_app() -> None:
    """Launch Recoder as a native desktop window (server runs in-process)."""
    from recoder.config import load_config
    from recoder.web.desktop import run_desktop

    raise typer.Exit(code=run_desktop(load_config()))


@app.command()
def ui(
    no_window: bool = typer.Option(
        False, "--no-window", help="Serve only; do not open a browser window."
    ),
) -> None:
    """Launch the web UI (server + a chromeless app window)."""
    import os
    import socket
    import subprocess
    import threading
    import time
    import webbrowser

    import uvicorn

    from recoder.config import load_config
    from recoder.web.app import create_app

    config = load_config()
    host = "127.0.0.1"
    port = config.port
    url = f"http://{host}:{port}"

    def _wait_for_server(timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
                if sock.connect_ex((host, port)) == 0:
                    return True
            time.sleep(0.2)
        return False

    def _chrome_candidates() -> list[str]:
        local = os.environ.get("LOCALAPPDATA", "")
        prog = os.environ.get("ProgramFiles", r"C:\Program Files")
        prog86 = os.environ.get(
            "ProgramFiles(x86)", r"C:\Program Files (x86)"
        )
        paths = [
            os.path.join(prog, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(prog86, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(
                local, "Google", "Chrome", "Application", "chrome.exe"
            ),
            os.path.join(prog, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(
                prog86, "Microsoft", "Edge", "Application", "msedge.exe"
            ),
        ]
        return [p for p in paths if p and os.path.exists(p)]

    def _launch_window() -> None:
        if no_window:
            return
        if not _wait_for_server():
            typer.echo("Server did not come up in time; open " + url, err=True)
            return
        local = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
        profile = os.path.join(local, "recoder", "chrome-profile")
        for exe in _chrome_candidates():
            try:
                subprocess.Popen(
                    [
                        exe,
                        f"--app={url}",
                        f"--user-data-dir={profile}",
                    ],
                    close_fds=True,
                )
                return
            except OSError:
                continue
        # Final fallback: whatever the OS considers the default browser.
        webbrowser.open(url)

    threading.Thread(target=_launch_window, daemon=True).start()

    typer.echo(f"Recoder UI on {url}  (Ctrl+C to stop)")
    uvicorn.run(create_app(config), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    app()
