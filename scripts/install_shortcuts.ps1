# Install Recoder shortcuts: Start Menu (searchable, pinnable) + Desktop.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_shortcuts.ps1
#
# Both shortcuts launch the app through pythonw.exe (no console window).
# To put Recoder on your taskbar: open Start, search "Recoder",
# right-click it -> Pin to taskbar. Do NOT pin recoder.exe from the venv -
# that is the terminal CLI, not the app.

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Pythonw = Join-Path $RepoRoot ".venv\Scripts\pythonw.exe"
$Icon = Join-Path $RepoRoot "assets\recoder.ico"

if (-not (Test-Path $Pythonw)) {
    Write-Host "Missing $Pythonw - run 'uv sync' in $RepoRoot first." -ForegroundColor Red
    exit 1
}

$targets = @(
    (Join-Path ([Environment]::GetFolderPath("Programs")) "Recoder.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "Recoder.lnk")
)

$shell = New-Object -ComObject WScript.Shell
foreach ($lnkPath in $targets) {
    $lnk = $shell.CreateShortcut($lnkPath)
    $lnk.TargetPath = $Pythonw
    $lnk.Arguments = "-m recoder app"
    $lnk.WorkingDirectory = $RepoRoot
    $lnk.Description = "Recoder - context-aware meeting recorder"
    if (Test-Path $Icon) { $lnk.IconLocation = "$Icon,0" }
    $lnk.Save()
    Write-Host "Installed: $lnkPath"
}

Write-Host "`nTo pin: Start menu -> search 'Recoder' -> right-click -> Pin to taskbar." -ForegroundColor Green
