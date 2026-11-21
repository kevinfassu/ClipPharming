# Starts the worker and the dashboard in the background and opens the dashboard.
param([switch]$NoBrowser)
$root = Split-Path $PSScriptRoot -Parent
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Error "Run scripts\install.ps1 first."; exit 1 }

# Don't start twice (e.g. manual start after the logon task already ran).
$running = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -match "clipforge (worker|dashboard)" }
if ($running) {
    Write-Host "ClipForge is already running."
} else {
    Start-Process -FilePath $py -ArgumentList "-m", "clipforge", "worker" -WorkingDirectory $root -WindowStyle Minimized
    Start-Process -FilePath $py -ArgumentList "-m", "clipforge", "dashboard" -WorkingDirectory $root -WindowStyle Minimized
    Start-Sleep -Seconds 3
}
if (-not $NoBrowser) { Start-Process "http://127.0.0.1:8765" }
