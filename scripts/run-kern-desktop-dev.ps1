param(
    [string]$RuntimeRoot = "",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir "..")
$tauriRoot = Join-Path $repoRoot "src-tauri"

if (-not (Test-Path $tauriRoot)) {
    throw "Missing src-tauri desktop project."
}

if ($RuntimeRoot) {
    $env:KERN_DESKTOP_RUNTIME_ROOT = (Resolve-Path $RuntimeRoot).Path
}
else {
    $env:KERN_DESKTOP_RUNTIME_ROOT = $repoRoot.Path
}

if ($Python) {
    $env:KERN_DESKTOP_PYTHON = (Resolve-Path $Python).Path
}
elseif (Test-Path (Join-Path $repoRoot ".venv\Scripts\python.exe")) {
    $env:KERN_DESKTOP_PYTHON = (Resolve-Path (Join-Path $repoRoot ".venv\Scripts\python.exe")).Path
}

$env:KERN_DESKTOP_MODE = "true"
$env:KERN_PRODUCT_POSTURE = "production"

Push-Location $tauriRoot
try {
    cargo run
}
finally {
    Pop-Location
}
