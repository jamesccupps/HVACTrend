<#
.SYNOPSIS
  Run HVACTrend in the background, from boot, as two Windows scheduled tasks.

.DESCRIPTION
  "HVACTrend Server"   - the web pages and the collector. Starts at boot as SYSTEM and is
                         restarted within a minute if it ever stops.
  "HVACTrend Watchdog" - every 5 minutes: checks that data is still arriving, evaluates the
                         alarm rules and sends the phone notifications. Kept separate on
                         purpose, so a stopped server cannot also silence the alert saying so.

  Run as administrator, after install.ps1 and the setup wizard. Run it again after moving
  the folder. Remove both with scripts\windows\uninstall-service.ps1.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\windows\install-service.ps1
#>
#Requires -RunAsAdministrator
param([string]$Name = "HVACTrend")
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { throw "No .venv in $Root - run install.ps1 first." }
if (-not (Test-Path (Join-Path $Root "config\config.yaml"))) {
    throw "No config\config.yaml - run the setup wizard (scripts\configure.py) first."
}

$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

# The server: at boot, restarted on failure, no time limit.
$serverAction = New-ScheduledTaskAction -Execute $Py -Argument "scripts\serve.py" -WorkingDirectory $Root
$serverSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "$Name Server" -Action $serverAction `
    -Trigger (New-ScheduledTaskTrigger -AtStartup) -Settings $serverSettings `
    -Principal $principal -Description "HVACTrend web pages and BACnet collector (read-only)" `
    -Force | Out-Null

# The watchdog: ONE trigger, every 5 minutes, for ten years; a run never overlaps another.
$dogAction = New-ScheduledTaskAction -Execute $Py -Argument "scripts\watchdog.py --quiet" -WorkingDirectory $Root
$dogTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
$dogSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "$Name Watchdog" -Action $dogAction -Trigger $dogTrigger `
    -Settings $dogSettings -Principal $principal `
    -Description "HVACTrend watchdog: data-stopped alerts, alarm rules, phone notifications" `
    -Force | Out-Null

Start-ScheduledTask -TaskName "$Name Server"
Write-Host "Installed and started:" -ForegroundColor Green
Write-Host "  $Name Server    (at boot; restarts on failure)"
Write-Host "  $Name Watchdog  (every 5 minutes)"
Write-Host ""
Write-Host "Open http://localhost:8000 in a minute. Logs: $Root\logs\hvactrend.log"
