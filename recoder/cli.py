from __future__ import annotations

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
def record() -> None:
    """Record a meeting (audio + screen snapshots)."""
    raise NotImplementedError("Phase 1")


@app.command()
def process(folder: str = typer.Argument(...)) -> None:
    """Run the post-meeting pipeline on a recorded folder."""
    raise NotImplementedError("Phase 2")


@app.command()
def replay(folder: str = typer.Argument(...)) -> None:
    """Push an existing meeting folder through the full pipeline."""
    raise NotImplementedError("Phase 3")


@app.command()
def ui() -> None:
    """Launch the web UI."""
    raise NotImplementedError("Phase 4")


if __name__ == "__main__":
    app()
