# Recoder

A local, personal meeting recorder for Windows: captures both sides of your call
audio plus screen snapshots, then produces a context-aware summary using your
Claude Code subscription and your CCR project memory — all on this machine.

## Setup

1. Install the base project (uv-managed venv, Python 3.11):

   ```sh
   uv sync
   ```

2. Install the CUDA build of PyTorch, then the ML extras (whisperX):

   ```sh
   uv pip install torch --index-url https://download.pytorch.org/whl/cu121
   uv sync --extra ml
   ```

3. HuggingFace token for pyannote diarization (free, one-time):
   - Create a token: https://huggingface.co/settings/tokens
   - Accept the model licenses:
     - https://huggingface.co/pyannote/speaker-diarization-3.1
     - https://huggingface.co/pyannote/segmentation-3.0
   - Set it: `setx HF_TOKEN <token>` (or `huggingface-cli login`).

4. Install ffmpeg and put it on PATH:

   ```sh
   winget install Gyan.FFmpeg
   ```

5. Log in to the Claude CLI (subscription auth, no API key):

   ```sh
   claude login
   ```

## Verify

```sh
uv run recoder doctor          # environment checks
uv run recoder doctor --full   # also runs the unattended Claude SDK + CCR probe
```

`doctor` prints PASS/FAIL/SKIP per check with remediation text; its exit code is
the number of failures.
