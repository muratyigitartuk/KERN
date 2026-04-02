param(
    [string]$InstallRoot,
    [string]$OutputRoot = "output\\package-smoke\\uninstall-smoke",
    [switch]$Json
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

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

function Invoke-PowerShellQuiet {
    param(
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [string]$FailureMessage = "PowerShell child process failed."
    )
    $stdoutPath = Join-Path $env:TEMP "kern-uninstall-stdout-$([guid]::NewGuid().ToString('N')).log"
    $stderrPath = Join-Path $env:TEMP "kern-uninstall-stderr-$([guid]::NewGuid().ToString('N')).log"
    try {
        $proc = Start-Process -FilePath "powershell" -ArgumentList $ArgumentList -WorkingDirectory $WorkingDirectory -Wait -PassThru -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
        if ($proc.ExitCode -ne 0) {
            $stdout = if (Test-Path $stdoutPath) { Get-Content $stdoutPath -Raw } else { "" }
            $stderr = if (Test-Path $stderrPath) { Get-Content $stderrPath -Raw } else { "" }
            throw "$FailureMessage`n$stderr$stdout".Trim()
        }
    }
    finally {
        Remove-Item $stdoutPath, $stderrPath -ErrorAction SilentlyContinue
    }
}

$resolvedInstallRoot = Resolve-SmokeInstallRoot -RequestedRoot $InstallRoot
$reportRoot = if ([System.IO.Path]::IsPathRooted($OutputRoot)) { $OutputRoot } else { Join-Path $root $OutputRoot }
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$reportPath = Join-Path $reportRoot "uninstall-smoke-$timestamp.json"
$fullDeleteRoot = Join-Path $reportRoot "full-delete-$timestamp"

New-Item -ItemType Directory -Force -Path $reportRoot | Out-Null

$sentinelRelative = ".kern\\profiles\\default\\documents\\uninstall-smoke.txt"
$sentinelPath = Join-Path $resolvedInstallRoot $sentinelRelative
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $sentinelPath) | Out-Null
Set-Content -Path $sentinelPath -Value "uninstall smoke $(Get-Date -Format o)" -Encoding UTF8

if (Test-Path $fullDeleteRoot) {
    Remove-Item -Recurse -Force $fullDeleteRoot
}
Copy-Item -Recurse -Force $resolvedInstallRoot $fullDeleteRoot

Push-Location $resolvedInstallRoot
try {
    if ($Json) {
        Invoke-PowerShellQuiet -ArgumentList @("-ExecutionPolicy", "Bypass", "-File", ".\\scripts\\uninstall-kern.ps1") -WorkingDirectory $resolvedInstallRoot -FailureMessage "Default uninstall failed."
    }
    else {
        powershell -ExecutionPolicy Bypass -File ".\\scripts\\uninstall-kern.ps1"
    }
}
finally {
    Pop-Location
}

$defaultPreserved = Test-Path (Join-Path $resolvedInstallRoot ".kern")
$defaultSentinelPresent = Test-Path (Join-Path $resolvedInstallRoot $sentinelRelative)
$defaultVenvRemoved = -not (Test-Path (Join-Path $resolvedInstallRoot ".venv"))

if (-not $defaultPreserved -or -not $defaultSentinelPresent -or -not $defaultVenvRemoved) {
    throw "Default uninstall did not preserve .kern data and remove runtime artifacts as expected."
}

Push-Location $fullDeleteRoot
try {
    if ($Json) {
        Invoke-PowerShellQuiet -ArgumentList @("-ExecutionPolicy", "Bypass", "-File", ".\\scripts\\uninstall-kern.ps1", "-RemoveData") -WorkingDirectory $fullDeleteRoot -FailureMessage "RemoveData uninstall failed."
    }
    else {
        powershell -ExecutionPolicy Bypass -File ".\\scripts\\uninstall-kern.ps1" -RemoveData
    }
}
finally {
    Pop-Location
}

$fullDeleteRemovedData = -not (Test-Path (Join-Path $fullDeleteRoot ".kern"))
$fullDeleteRemovedRuntime = -not (Test-Path (Join-Path $fullDeleteRoot ".venv"))

if (-not $fullDeleteRemovedData -or -not $fullDeleteRemovedRuntime) {
    throw "RemoveData uninstall did not remove both runtime artifacts and .kern data."
}

$report = [ordered]@{
    install_root = $resolvedInstallRoot
    full_delete_root = $fullDeleteRoot
    default_uninstall = [ordered]@{
        preserved_data = $defaultPreserved
        preserved_sentinel = $defaultSentinelPresent
        removed_runtime = $defaultVenvRemoved
    }
    remove_data_uninstall = [ordered]@{
        removed_data = $fullDeleteRemovedData
        removed_runtime = $fullDeleteRemovedRuntime
    }
    created_at = (Get-Date).ToString("o")
}

$report | ConvertTo-Json -Depth 6 | Set-Content -Path $reportPath -Encoding UTF8

if ($Json) {
    $report | ConvertTo-Json -Depth 6
}
else {
    Write-Host "Uninstall smoke passed. Report: $reportPath"
}
