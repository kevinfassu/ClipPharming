# Makes ClipForge start automatically when you log in, and keeps the PC awake while plugged in.
# Run once:  powershell -ExecutionPolicy Bypass -File scripts\register-startup.ps1
$ErrorActionPreference = "Stop"
$script = Join-Path $PSScriptRoot "start-clipforge.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`" -NoBrowser"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "ClipForge" -Action $action -Trigger $trigger -Settings $settings `
    -Description "ClipForge AI clipping worker + dashboard" -Force | Out-Null
Write-Host "Registered 'ClipForge' to start at logon." -ForegroundColor Green

# Never sleep on AC power (the screen can still turn off).
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
Write-Host "Sleep disabled while plugged in." -ForegroundColor Green
