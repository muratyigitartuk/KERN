param(
    [string]$RuntimeRoot = "",
    [string]$Python = "",
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = if ($RuntimeRoot) { (Resolve-Path $RuntimeRoot).Path } else { (Resolve-Path (Join-Path $scriptDir "..")).Path }
$releaseExe = Join-Path $repoRoot "src-tauri\target\release\kern-desktop.exe"
$debugExe = Join-Path $repoRoot "src-tauri\target\debug\kern-desktop.exe"
$desktopExe = if (Test-Path $releaseExe) { $releaseExe } elseif (Test-Path $debugExe) { $debugExe } else { "" }

if (-not $desktopExe) {
    throw "KERN is not installed yet. Run install-kern once, then run kern."
}

$pythonPath = if ($Python) { (Resolve-Path $Python).Path } else { Join-Path $repoRoot ".venv\Scripts\python.exe" }
if (-not (Test-Path $pythonPath)) {
    throw "KERN is not installed yet. Run install-kern first."
}

$env:KERN_DESKTOP_RUNTIME_ROOT = $repoRoot
$env:KERN_DESKTOP_PYTHON = (Resolve-Path $pythonPath).Path
$env:KERN_DESKTOP_MODE = "true"
$env:KERN_PRODUCT_POSTURE = "production"
$env:KERN_DISABLE_AUTH_FOR_LOOPBACK = "true"

if ($CheckOnly) {
    Write-Host "KERN start check passed." -ForegroundColor Green
    exit 0
}

Write-Host "Starting KERN..." -ForegroundColor Cyan
& $desktopExe
exit $LASTEXITCODE
