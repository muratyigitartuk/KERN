param(
    [string]$RuntimeRoot = "",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = if ($RuntimeRoot) { (Resolve-Path $RuntimeRoot).Path } else { (Resolve-Path (Join-Path $scriptDir "..")).Path }
$desktopExe = Join-Path $repoRoot "src-tauri\target\debug\kern-desktop.exe"

if (-not (Test-Path $desktopExe)) {
    Write-Host "KERN desktop runtime is not built yet. Building it once..." -ForegroundColor Cyan
    Push-Location (Join-Path $repoRoot "src-tauri")
    try {
        cargo build
        if ($LASTEXITCODE -ne 0) {
            throw "KERN desktop build failed."
        }
    }
    finally {
        Pop-Location
    }
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

Write-Host "Starting KERN..." -ForegroundColor Cyan
& $desktopExe
exit $LASTEXITCODE
