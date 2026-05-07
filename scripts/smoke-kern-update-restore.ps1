param(
    [string]$InstallRoot,
    [string]$OutputRoot = "output\\package-smoke\\restore-smoke",
    [string]$Password = "",
    [switch]$Json
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not $Password) {
    $Password = [guid]::NewGuid().ToString("N")
}

function Resolve-SmokeInstallRoot {
    param([string]$RequestedRoot)
    if ($RequestedRoot) {
        return (Resolve-Path $RequestedRoot).Path
    }

    $candidates = Get-ChildItem "output\\package-smoke\\kern-runtime-smoke-*" -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending
    foreach ($candidate in $candidates) {
        if (
            (Test-Path (Join-Path $candidate.FullName "scripts\\create-kern-update-bundle.py")) -and
            (Test-Path (Join-Path $candidate.FullName "scripts\\restore-kern.py")) -and
            (Test-Path (Join-Path $candidate.FullName ".kern"))
        ) {
            return $candidate.FullName
        }
    }

    $bootstrapRaw = powershell -ExecutionPolicy Bypass -File ".\scripts\smoke-kern-runtime-package.ps1" -Json
    $bootstrap = $bootstrapRaw | ConvertFrom-Json
    return [string]$bootstrap.extracted_to
}

function Resolve-SmokePythonExe {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)
    $venvPython = Join-Path $InstallRoot ".venv\\Scripts\\python.exe"
    if (Test-Path $venvPython) {
        return (Resolve-Path $venvPython).Path
    }
    $command = Get-Command "python" -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    throw "Smoke restore requires either package .venv Python or python on PATH."
}

function Invoke-PythonJsonQuiet {
    param(
        [Parameter(Mandatory = $true)][string]$PythonExe,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [string]$FailureMessage = "Python child process failed.",
        [string]$StdinText = ""
    )
    $pythonPath = if ([System.IO.Path]::IsPathRooted($PythonExe)) { $PythonExe } else { Join-Path $WorkingDirectory $PythonExe }
    $resolvedPythonExe = (Resolve-Path -Path $pythonPath).Path
    Push-Location $WorkingDirectory
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        if ($StdinText) {
            $output = $StdinText | & $resolvedPythonExe @ArgumentList 2>&1
        }
        else {
            $output = & $resolvedPythonExe @ArgumentList 2>&1
        }
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
        Pop-Location
    }
    $text = ($output | Out-String).Trim()
    if ($exitCode -ne 0) {
        throw (("$FailureMessage`n$text").Trim())
    }
    return $text
}

$resolvedInstallRoot = Resolve-SmokeInstallRoot -RequestedRoot $InstallRoot
$pythonExe = Resolve-SmokePythonExe -InstallRoot $resolvedInstallRoot

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$bundlePath = Join-Path $resolvedInstallRoot ".kern\\upgrade-backups\\restore-smoke-$timestamp.kernbundle"
$reportRoot = if ([System.IO.Path]::IsPathRooted($OutputRoot)) { $OutputRoot } else { Join-Path $root $OutputRoot }
$restoreRoot = Join-Path $reportRoot "restored-$timestamp"
$reportPath = Join-Path $reportRoot "restore-smoke-$timestamp.json"
$sentinelRelative = "profiles\\default\\documents\\restore-smoke.txt"
$sentinelSource = Join-Path $resolvedInstallRoot ".kern\\$sentinelRelative"
$sentinelRestored = Join-Path $restoreRoot $sentinelRelative

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $bundlePath), (Split-Path -Parent $sentinelSource), $reportRoot | Out-Null
Set-Content -Path $sentinelSource -Value "restore smoke $(Get-Date -Format o)" -Encoding UTF8

Push-Location $resolvedInstallRoot
try {
    if ($Json) {
        Invoke-PythonJsonQuiet -PythonExe $pythonExe -ArgumentList @(".\scripts\create-kern-update-bundle.py", "--root", ".", "--output", $bundlePath, "--password-stdin", "--json") -WorkingDirectory $resolvedInstallRoot -FailureMessage "Update bundle creation failed." -StdinText $Password | Out-Null
        $validationRaw = Invoke-PythonJsonQuiet -PythonExe $pythonExe -ArgumentList @(".\scripts\restore-kern.py", $bundlePath, "--password-stdin", "--validate-only", "--json") -WorkingDirectory $resolvedInstallRoot -FailureMessage "Update bundle validation failed." -StdinText $Password
        $restoreRaw = Invoke-PythonJsonQuiet -PythonExe $pythonExe -ArgumentList @(".\scripts\restore-kern.py", $bundlePath, "--password-stdin", "--restore-root", $restoreRoot, "--force", "--json") -WorkingDirectory $resolvedInstallRoot -FailureMessage "Update bundle restore failed." -StdinText $Password
    }
    else {
        Invoke-PythonJsonQuiet -PythonExe $pythonExe -ArgumentList @(".\scripts\create-kern-update-bundle.py", "--root", ".", "--output", $bundlePath, "--password-stdin", "--json") -WorkingDirectory $resolvedInstallRoot -FailureMessage "Update bundle creation failed." -StdinText $Password | Out-Null
        $validationRaw = Invoke-PythonJsonQuiet -PythonExe $pythonExe -ArgumentList @(".\scripts\restore-kern.py", $bundlePath, "--password-stdin", "--validate-only", "--json") -WorkingDirectory $resolvedInstallRoot -FailureMessage "Update bundle validation failed." -StdinText $Password
        $restoreRaw = Invoke-PythonJsonQuiet -PythonExe $pythonExe -ArgumentList @(".\scripts\restore-kern.py", $bundlePath, "--password-stdin", "--restore-root", $restoreRoot, "--force", "--json") -WorkingDirectory $resolvedInstallRoot -FailureMessage "Update bundle restore failed." -StdinText $Password
    }
}
finally {
    Pop-Location
}

$validation = $validationRaw | ConvertFrom-Json
$restore = $restoreRaw | ConvertFrom-Json
$sentinelPresent = Test-Path $sentinelRestored

if (-not $sentinelPresent) {
    throw "Restore smoke failed: restored sentinel file missing at $sentinelRestored"
}

$report = [ordered]@{
    install_root = $resolvedInstallRoot
    python = $pythonExe
    bundle_path = $bundlePath
    restore_root = $restoreRoot
    sentinel_source = $sentinelSource
    sentinel_restored = $sentinelRestored
    sentinel_present = $sentinelPresent
    validation = $validation
    restore = $restore
    created_at = (Get-Date).ToString("o")
}

$report | ConvertTo-Json -Depth 8 | Set-Content -Path $reportPath -Encoding UTF8

if ($Json) {
    $report | ConvertTo-Json -Depth 8
}
else {
    Write-Host "Restore smoke passed. Report: $reportPath"
}
