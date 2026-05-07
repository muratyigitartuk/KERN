param(
    [string]$RuntimeRoot = "",
    [string]$Python = "",
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$launcher = Join-Path $scriptDir "start-kern-desktop.ps1"

$args = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", $launcher
)
if ($RuntimeRoot) { $args += @("-RuntimeRoot", $RuntimeRoot) }
if ($Python) { $args += @("-Python", $Python) }
if ($CheckOnly) { $args += "-CheckOnly" }

& powershell.exe @args
exit $LASTEXITCODE
