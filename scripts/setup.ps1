# Recoder one-shot setup for a fresh Windows machine.
#
#   git clone <this repo> ; cd recoder ; powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
#
# Installs project deps (uv), sets up CCR memory (github.com/qbit-glitch/ccr)
# in a dedicated venv at ~\.ccr\.venv, creates recoder.toml from the example,
# then runs `recoder doctor` so you can see what (if anything) is still missing.
#
# Prerequisites you must install yourself first:
#   * Python 3.11+            https://www.python.org/downloads/
#   * uv                      https://docs.astral.sh/uv/  (winget install astral-sh.uv)
#   * Claude Code CLI, logged in on a subscription: `claude login`
#   * A Gladia API key (free): https://app.gladia.io

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

# --- 1. Preflight ---------------------------------------------------------------
Step "Checking prerequisites"
foreach ($tool in @("python", "uv", "claude")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Write-Host "MISSING: '$tool' is not on PATH. Install it (see header of this script) and re-run." -ForegroundColor Red
        exit 1
    }
}
$pyVersion = (& python -c "import sys; print('%d.%d' % sys.version_info[:2])")
if ([version]$pyVersion -lt [version]"3.11") {
    Write-Host "MISSING: Python 3.11+ required, found $pyVersion." -ForegroundColor Red
    exit 1
}
Write-Host "python $pyVersion, uv, claude — all found."

# --- 2. Project dependencies ------------------------------------------------------
Step "Installing Recoder dependencies (uv sync)"
uv sync
if ($LASTEXITCODE -ne 0) { Write-Host "uv sync failed." -ForegroundColor Red; exit 1 }

# --- 3. CCR memory engine ---------------------------------------------------------
# CCR (Continuous Context Retention) gives Recoder its project memory. It lives
# in its own venv under ~\.ccr so it is shared by all projects on this machine.
$CcrHome = Join-Path $env:USERPROFILE ".ccr"
$CcrPython = Join-Path $CcrHome ".venv\Scripts\python.exe"

if (Test-Path $CcrPython) {
    Step "CCR already installed at $CcrHome — skipping"
} else {
    Step "Installing CCR memory engine into $CcrHome\.venv"
    New-Item -ItemType Directory -Force $CcrHome | Out-Null
    python -m venv (Join-Path $CcrHome ".venv")
    & $CcrPython -m pip install --upgrade pip --quiet
    & $CcrPython -m pip install ccr-memory
    if ($LASTEXITCODE -ne 0) {
        Write-Host "CCR install failed. See https://github.com/qbit-glitch/ccr" -ForegroundColor Red
        exit 1
    }
}

# Verify the MCP server module is importable — this is exactly how Recoder spawns it.
& $CcrPython -c "import ccr.mcp_server" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "CCR venv exists but 'ccr.mcp_server' is not importable — reinstall with:" -ForegroundColor Red
    Write-Host "  $CcrPython -m pip install --force-reinstall ccr-memory"
    exit 1
}
Write-Host "CCR MCP server OK: $CcrPython"

# Optional: wire CCR into your own interactive Claude Code sessions too
# (Recoder does NOT need this — it spawns the MCP server itself — but it means
# your regular coding sessions build the project memory that meetings draw on).
Step "Enabling CCR for interactive Claude Code sessions (ccr install-global)"
& $CcrPython -m pip show ccr-memory | Out-Null
try {
    & (Join-Path $CcrHome ".venv\Scripts\ccr.exe") install-global
} catch {
    Write-Host "ccr install-global failed (non-fatal): Recoder still works; your interactive" -ForegroundColor Yellow
    Write-Host "Claude Code sessions just won't auto-record memory. See the CCR README." -ForegroundColor Yellow
}

# --- 4. Personal config -------------------------------------------------------------
if (Test-Path (Join-Path $RepoRoot "recoder.toml")) {
    Step "recoder.toml already exists — leaving it alone"
} else {
    Step "Creating recoder.toml from recoder.toml.example"
    Copy-Item (Join-Path $RepoRoot "recoder.toml.example") (Join-Path $RepoRoot "recoder.toml")
    Write-Host "EDIT recoder.toml and paste your Gladia API key (free at https://app.gladia.io)." -ForegroundColor Yellow
}

# --- 5. Health check ------------------------------------------------------------------
Step "Running recoder doctor"
uv run recoder doctor

Write-Host "`nSetup complete. Next steps:" -ForegroundColor Green
Write-Host "  1. Put your Gladia key in recoder.toml (if doctor flagged it)."
Write-Host "  2. Start the app:   uv run recoder app"
Write-Host "  3. Or from a terminal: uv run recoder record --title 'My first meeting'"
