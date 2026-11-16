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

  The tasks run as SYSTEM from this folder, so it refuses a folder that every signed-in
  user can change (one made at the root of C: is). -LockDown fixes that first: SYSTEM and
  Administrators get full control, the account running this gets modify, and nobody else
  gets anything, which also keeps config\secrets.env private. See docs/security.md.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\windows\install-service.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\windows\install-service.ps1 -LockDown
#>
#Requires -RunAsAdministrator
param([string]$Name = "HVACTrend", [switch]$LockDown)
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { throw "No .venv in $Root - run install.ps1 first." }
if (-not (Test-Path (Join-Path $Root "config\config.yaml"))) {
    throw "No config\config.yaml - run the setup wizard (scripts\configure.py) first."
}

# Who else can change this folder (2026-11-15 review, finding 1). Both tasks run as SYSTEM
# from it, so anyone who can replace python.exe, a script or a package here can run code as
# SYSTEM within five minutes; anyone who can read it can read config\secrets.env.
$BroadSids = @{ "S-1-1-0" = "Everyone"; "S-1-5-11" = "Authenticated Users";
                "S-1-5-32-545" = "Users"; "S-1-5-4" = "INTERACTIVE" }
# write, append, write attributes, delete, change permissions, take ownership, GENERIC_ALL/WRITE
$WriteBits = 0x500D0156

function Get-BroadAccess([string]$Path, [switch]$Write) {
    $found = @()
    foreach ($ace in (Get-Acl -LiteralPath $Path).Access) {
        if ($ace.AccessControlType -ne "Allow") { continue }
        try { $sid = $ace.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value }
        catch { continue }
        if (-not $BroadSids.ContainsKey($sid)) { continue }
        if ($Write -and -not ([int64]$ace.FileSystemRights -band $WriteBits)) { continue }
        $found += $BroadSids[$sid]
    }
    $found | Select-Object -Unique
}

function Set-PrivateFolder([string]$Path) {
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & icacls $Path /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" `
        "*${me}:(OI)(CI)M" /remove:g "*S-1-1-0" "*S-1-5-11" "*S-1-5-32-545" "*S-1-5-4" /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls could not set the permissions of $Path (exit $LASTEXITCODE)" }
    # Everything inside goes back to inheriting from the folder, dropping its own grants.
    & icacls (Join-Path $Path "*") /reset /T /C /Q | Out-Null
}

if ($LockDown) {
    Set-PrivateFolder $Root
    Write-Host "Permissions of $Root set: SYSTEM and Administrators full, you modify, nobody else."
}
$open = @(Get-BroadAccess $Root -Write)
if ($open.Count) {
    throw ("$Root can be changed by: $($open -join ', '). The tasks run as SYSTEM from this " +
           "folder, so any of those accounts could run code as SYSTEM. Run this again with " +
           "-LockDown, or move HVACTrend to a folder only administrators can change.")
}
$readers = @(Get-BroadAccess $Root)
if ($readers.Count) {
    Write-Warning ("Every signed-in user can read $Root, config\secrets.env included " +
                   "($($readers -join ', ')). Run this again with -LockDown to stop that.")
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
