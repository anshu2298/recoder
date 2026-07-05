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


@app.command()
def ui() -> None:
    """Launch the web UI."""
    raise NotImplementedError("Phase 4")


if __name__ == "__main__":
    app()
