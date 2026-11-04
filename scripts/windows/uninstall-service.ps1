<#
.SYNOPSIS
  Stop HVACTrend running in the background and remove its two scheduled tasks.
  Your configuration, database and backups are left exactly as they are.
#>
#Requires -RunAsAdministrator
param([string]$Name = "HVACTrend")
foreach ($t in @("$Name Server", "$Name Watchdog")) {
    if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $t -Confirm:$false
        Write-Host "Removed $t"
    } else {
        Write-Host "$t was not installed"
    }
}
