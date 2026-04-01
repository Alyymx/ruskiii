#Requires -Version 5.1
<#
  Registers a daily Windows Scheduled Task that runs run_daily.bat.
  Run from PowerShell (user session is enough for a task that runs as you):

    cd scripts\russian-agent
    .\Register-ScheduledTask.ps1

  Optional: -Time "09:30" -TaskName "RussianDailyTutor"
#>
param(
    [string] $TaskName = "RussianDailyTutor",
    [string] $Time = "08:00"
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$batPath = Join-Path $scriptDir "run_daily.bat"

if (-not (Test-Path $batPath)) {
    throw "Missing run_daily.bat at $batPath"
}

$parts = $Time -split ":"
$hour = [int]$parts[0]
$minute = if ($parts.Count -gt 1) { [int]$parts[1] } else { 0 }
$triggerAt = [DateTime]::Today.AddHours($hour).AddMinutes($minute)

$action = New-ScheduledTaskAction -Execute $batPath -WorkingDirectory $scriptDir
$trigger = New-ScheduledTaskTrigger -Daily -At $triggerAt
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Daily Russian word: Oxlo API + Anki export (run_daily.bat)" `
    -Force

Write-Host "Registered task '$TaskName' daily at $Time. Edit in Task Scheduler if needed."
