param(
    [string]$TaskName = "KERN Local Runtime",
    [int]$Port = 8000,
    [switch]$AtStartup
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path .\.venv\Scripts\python.exe)) {
    throw "KERN is not installed in .venv."
}

$launcher = (Resolve-Path .\scripts\run-kern.ps1).Path
$startScript = "powershell.exe -ExecutionPolicy Bypass -File `"$launcher`" -Port $Port"

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $startScript"
$trigger = if ($AtStartup) { New-ScheduledTaskTrigger -AtStartup } else { New-ScheduledTaskTrigger -AtLogOn }
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName' for KERN on port $Port."
Write-Host "Trigger: $(if ($AtStartup) { 'startup' } else { 'logon' })"
