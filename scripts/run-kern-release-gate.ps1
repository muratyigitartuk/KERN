param(
    [string]$PackagePath,
    [string]$OutputRoot = "output\\releases",
    [switch]$Json
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = "python"
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$releaseRoot = if ([System.IO.Path]::IsPathRooted($OutputRoot)) { $OutputRoot } else { Join-Path $root $OutputRoot }
$releaseDir = Join-Path $releaseRoot "release-gate-$timestamp"
New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null

function Invoke-PowerShellJsonQuiet {
    param(
        [Parameter(Mandatory = $true)][string]$ScriptPath,
        [string[]]$Arguments = @(),
        [string]$FailureMessage = "PowerShell command failed."
    )
    $stdoutPath = Join-Path $env:TEMP "kern-release-stdout-$([guid]::NewGuid().ToString('N')).log"
    $stderrPath = Join-Path $env:TEMP "kern-release-stderr-$([guid]::NewGuid().ToString('N')).log"
    try {
        $argumentList = @("-ExecutionPolicy", "Bypass", "-File", $ScriptPath) + $Arguments + @("-Json")
        $proc = Start-Process -FilePath "powershell" -ArgumentList $argumentList -WorkingDirectory $root -Wait -PassThru -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
        $stdout = if (Test-Path $stdoutPath) { Get-Content $stdoutPath -Raw } else { "" }
        $stderr = if (Test-Path $stderrPath) { Get-Content $stderrPath -Raw } else { "" }
        if ($proc.ExitCode -ne 0) {
            throw "$FailureMessage`n$stderr$stdout".Trim()
        }
        return ($stdout | ConvertFrom-Json)
    }
    finally {
        Remove-Item $stdoutPath, $stderrPath -ErrorAction SilentlyContinue
    }
}

if (-not $PackagePath) {
    powershell -ExecutionPolicy Bypass -File ".\scripts\package-kern-runtime.ps1" | Out-Null
    $latest = Get-ChildItem "output\\packages\\kern-internal-runtime-*.zip" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $latest) {
        throw "Runtime package build did not produce a package."
    }
    $PackagePath = $latest.FullName
}

$resolvedPackage = (Resolve-Path $PackagePath).Path
$hygieneRaw = & $python ".\scripts\validate-publish-hygiene.py" --package $resolvedPackage --json
if ($LASTEXITCODE -ne 0) {
    throw "Publishing hygiene validation failed.`n$hygieneRaw"
}
$hygieneValidation = $hygieneRaw | ConvertFrom-Json
$packageValidation = Invoke-PowerShellJsonQuiet -ScriptPath ".\scripts\validate-kern-package.ps1" -Arguments @("-PackagePath", $resolvedPackage) -FailureMessage "Package validation failed."

$previousValidationPackage = $env:KERN_VALIDATION_PACKAGE_PATH
$env:KERN_VALIDATION_PACKAGE_PATH = $resolvedPackage
try {
    $validationRaw = & $python ".\scripts\validate-kern-ui.py" --launch-local
    if ($LASTEXITCODE -ne 0) {
        throw "Validation pack failed."
    }
    $validationResult = $validationRaw | ConvertFrom-Json
}
finally {
    if ($null -eq $previousValidationPackage) {
        Remove-Item Env:\KERN_VALIDATION_PACKAGE_PATH -ErrorAction SilentlyContinue
    }
    else {
        $env:KERN_VALIDATION_PACKAGE_PATH = $previousValidationPackage
    }
}

$validationOutputDir = [string]$validationResult.output_dir
$summaryJsonPath = Join-Path $validationOutputDir "summary.json"
$summaryMdPath = Join-Path $validationOutputDir "summary.md"
if (-not (Test-Path $summaryJsonPath)) {
    throw "Validation summary.json was not created."
}
$summary = Get-Content $summaryJsonPath -Raw | ConvertFrom-Json
$releaseReady = [bool]$summary.release_gate.release_ready

$packageName = [System.IO.Path]::GetFileName($resolvedPackage)
$checksumPath = "$resolvedPackage.sha256"
Copy-Item -Force $resolvedPackage (Join-Path $releaseDir $packageName)
if (Test-Path $checksumPath) {
    Copy-Item -Force $checksumPath (Join-Path $releaseDir ([System.IO.Path]::GetFileName($checksumPath)))
}
Copy-Item -Force $summaryJsonPath (Join-Path $releaseDir "summary.json")
Copy-Item -Force $summaryMdPath (Join-Path $releaseDir "summary.md")

$report = [ordered]@{
    created_at = (Get-Date).ToString("o")
    package = $resolvedPackage
    checksum = if (Test-Path $checksumPath) { $checksumPath } else { $null }
    package_validation = $packageValidation
    publishing_hygiene = $hygieneValidation
    validation_output_dir = $validationOutputDir
    release_ready = $releaseReady
    release_gate = $summary.release_gate
    preserved_artifacts = [ordered]@{
        release_dir = $releaseDir
        package = Join-Path $releaseDir $packageName
        checksum = if (Test-Path $checksumPath) { Join-Path $releaseDir ([System.IO.Path]::GetFileName($checksumPath)) } else { $null }
        summary_json = Join-Path $releaseDir "summary.json"
        summary_md = Join-Path $releaseDir "summary.md"
    }
}

$reportPath = Join-Path $releaseDir "release-gate.json"
$report | ConvertTo-Json -Depth 8 | Set-Content -Path $reportPath -Encoding UTF8

if ($Json) {
    $report | ConvertTo-Json -Depth 8
}
else {
    Write-Host "Release gate complete. Release-ready: $releaseReady"
    Write-Host "Release artifacts: $releaseDir"
}
