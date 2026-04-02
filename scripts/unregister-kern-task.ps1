param(
    [string]$TaskName = "KERN Local Runtime"
)

$ErrorActionPreference = "Stop"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Unregistered scheduled task '$TaskName'."
}
else {
    Write-Host "Scheduled task '$TaskName' is not registered."
}
