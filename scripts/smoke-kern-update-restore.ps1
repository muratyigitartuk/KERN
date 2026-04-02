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
        if (Test-Path (Join-Path $candidate.FullName ".venv\\Scripts\\python.exe")) {
            return $candidate.FullName
        }
    }

    $bootstrapRaw = powershell -ExecutionPolicy Bypass -File ".\scripts\smoke-kern-runtime-package.ps1" -Json
    $bootstrap = $bootstrapRaw | ConvertFrom-Json
    return [string]$bootstrap.extracted_to
}

function Invoke-PythonJsonQuiet {
    param(
        [Parameter(Mandatory = $true)][string]$PythonExe,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [string]$FailureMessage = "Python child process failed.",
        [string]$StdinText = ""
    )
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $PythonExe
    foreach ($arg in $ArgumentList) { [void]$psi.ArgumentList.Add($arg) }
    $psi.WorkingDirectory = $WorkingDirectory
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    $null = $proc.Start()
    if ($StdinText) {
        $proc.StandardInput.WriteLine($StdinText)
    }
    $proc.StandardInput.Close()
    $stdout = $proc.StandardOutput.ReadToEnd()
    $stderr = $proc.StandardError.ReadToEnd()
    $proc.WaitForExit()
    if ($proc.ExitCode -ne 0) {
        throw "$FailureMessage`n$stderr$stdout".Trim()
    }
    return $stdout
}

$resolvedInstallRoot = Resolve-SmokeInstallRoot -RequestedRoot $InstallRoot
if (-not (Test-Path (Join-Path $resolvedInstallRoot ".venv\\Scripts\\python.exe"))) {
    throw "Smoke restore requires an extracted installed package with .venv present."
}

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
        Invoke-PythonJsonQuiet -PythonExe ".\.venv\Scripts\python.exe" -ArgumentList @(".\scripts\create-kern-update-bundle.py", "--root", ".", "--output", $bundlePath, "--password-stdin", "--json") -WorkingDirectory $resolvedInstallRoot -FailureMessage "Update bundle creation failed." -StdinText $Password | Out-Null
        $validationRaw = Invoke-PythonJsonQuiet -PythonExe ".\.venv\Scripts\python.exe" -ArgumentList @(".\scripts\restore-kern.py", $bundlePath, "--password-stdin", "--validate-only", "--json") -WorkingDirectory $resolvedInstallRoot -FailureMessage "Update bundle validation failed." -StdinText $Password
        $restoreRaw = Invoke-PythonJsonQuiet -PythonExe ".\.venv\Scripts\python.exe" -ArgumentList @(".\scripts\restore-kern.py", $bundlePath, "--password-stdin", "--restore-root", $restoreRoot, "--force", "--json") -WorkingDirectory $resolvedInstallRoot -FailureMessage "Update bundle restore failed." -StdinText $Password
    }
    else {
        Invoke-PythonJsonQuiet -PythonExe ".\.venv\Scripts\python.exe" -ArgumentList @(".\scripts\create-kern-update-bundle.py", "--root", ".", "--output", $bundlePath, "--password-stdin", "--json") -WorkingDirectory $resolvedInstallRoot -FailureMessage "Update bundle creation failed." -StdinText $Password | Out-Null
        $validationRaw = Invoke-PythonJsonQuiet -PythonExe ".\.venv\Scripts\python.exe" -ArgumentList @(".\scripts\restore-kern.py", $bundlePath, "--password-stdin", "--validate-only", "--json") -WorkingDirectory $resolvedInstallRoot -FailureMessage "Update bundle validation failed." -StdinText $Password
        $restoreRaw = Invoke-PythonJsonQuiet -PythonExe ".\.venv\Scripts\python.exe" -ArgumentList @(".\scripts\restore-kern.py", $bundlePath, "--password-stdin", "--restore-root", $restoreRoot, "--force", "--json") -WorkingDirectory $resolvedInstallRoot -FailureMessage "Update bundle restore failed." -StdinText $Password
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
