# ClipForge one-time setup for Windows 11.
# Run from the repo folder:  powershell -ExecutionPolicy Bypass -File scripts\install.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

function Need($cmd) { -not (Get-Command $cmd -ErrorAction SilentlyContinue) }

$reopen = $false
if (Need "python") {
    Write-Host "Installing Python 3.12..." -ForegroundColor Cyan
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    $reopen = $true
}
if (Need "ffmpeg") {
    Write-Host "Installing FFmpeg (full build, includes Intel Quick Sync + libass)..." -ForegroundColor Cyan
    winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
    $reopen = $true
}
if ($reopen) {
    Write-Host "Installed new tools. Close this window, open a NEW PowerShell, and run this script again." -ForegroundColor Yellow
    exit 0
}

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..." -ForegroundColor Cyan
    python -m venv .venv
}
$py = Join-Path $root ".venv\Scripts\python.exe"
& $py -m pip install --upgrade pip
& $py -m pip install -r requirements.txt

& $py -m clipforge init
Write-Host ""
Write-Host "Pre-downloading the Whisper model (one time, ~500 MB)..." -ForegroundColor Cyan
& $py -c "from faster_whisper import WhisperModel; WhisperModel('small.en', device='cpu', compute_type='int8')"
Write-Host ""
& $py -m clipforge check
Write-Host ""
Write-Host "Done. Start everything with:  powershell -ExecutionPolicy Bypass -File scripts\start-clipforge.ps1" -ForegroundColor Green
