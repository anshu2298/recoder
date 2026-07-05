"""Allow `python[w] -m recoder <command>` (used by the console-less shortcut)."""

from recoder.cli import app

if __name__ == "__main__":
    app()
