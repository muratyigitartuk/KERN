param(
    [string]$PackagePath,
    [string]$OutputRoot = "output\\package-smoke",
    [switch]$KeepExtracted,
    [switch]$Json
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Get-FreeTcpPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = ($listener.LocalEndpoint).Port
    $listener.Stop()
    return $port
}

function Wait-HttpReady {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [int]$TimeoutSeconds = 45
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-RestMethod -Uri $Url -Method Get -TimeoutSec 5
            if (
                $response.status -in @("ok", "warning", "degraded", "error", "ready", "not_ready", "live") -or
                $response.summary -or
                $response.checks
            ) {
                return $response
            }
        }
        catch {
            Start-Sleep -Seconds 1
        }
    }
    throw "KERN runtime did not become reachable at $Url within ${TimeoutSeconds}s."
}

function Invoke-PowerShellQuiet {
    param(
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [string]$FailureMessage = "PowerShell child process failed."
    )
    $stdoutPath = Join-Path $env:TEMP "kern-ps-stdout-$([guid]::NewGuid().ToString('N')).log"
    $stderrPath = Join-Path $env:TEMP "kern-ps-stderr-$([guid]::NewGuid().ToString('N')).log"
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

function New-LicenseFixtures {
    param(
        [Parameter(Mandatory = $true)][string]$PythonExe,
        [Parameter(Mandatory = $true)][string]$FixtureRoot
    )
    $script = @'
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def sign_payload(key: Ed25519PrivateKey, payload: dict[str, object]) -> dict[str, object]:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    signature = base64.b64encode(key.sign(body)).decode("ascii")
    return {"payload": payload, "signature": signature}


root = Path(__import__("sys").argv[1])
root.mkdir(parents=True, exist_ok=True)
private_key = Ed25519PrivateKey.generate()
public_key = base64.b64encode(
    private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
).decode("ascii")
now = datetime.now(timezone.utc)

valid_payload = {
    "plan": "Pilot",
    "activation_mode": "offline_license_file",
    "issued_at": now.isoformat(),
    "expires_at": (now + timedelta(days=14)).isoformat(),
    "status": "active",
    "sample_access": True,
    "features": ["grounded_drafting", "support_bundle"],
}
expired_payload = {
    "plan": "Pilot",
    "activation_mode": "offline_license_file",
    "issued_at": (now - timedelta(days=30)).isoformat(),
    "expires_at": (now - timedelta(days=2)).isoformat(),
    "status": "active",
    "sample_access": True,
    "features": ["grounded_drafting", "support_bundle"],
}
invalid_payload = sign_payload(private_key, valid_payload)
invalid_payload["signature"] = base64.b64encode(b"invalid-signature").decode("ascii")

valid_path = root / "valid-license.json"
expired_path = root / "expired-license.json"
invalid_path = root / "invalid-license.json"
valid_path.write_text(json.dumps(sign_payload(private_key, valid_payload), indent=2), encoding="utf-8")
expired_path.write_text(json.dumps(sign_payload(private_key, expired_payload), indent=2), encoding="utf-8")
invalid_path.write_text(json.dumps(invalid_payload, indent=2), encoding="utf-8")

print(json.dumps({
    "directory": str(root),
    "public_key": public_key,
    "valid_license": str(valid_path),
    "expired_license": str(expired_path),
    "invalid_license": str(invalid_path),
}, indent=2))
'@
    $tempScript = Join-Path $env:TEMP "kern-license-fixtures-$([guid]::NewGuid().ToString('N')).py"
    Set-Content -Path $tempScript -Value $script -Encoding UTF8
    try {
        $output = & $PythonExe $tempScript $FixtureRoot
        if ($LASTEXITCODE -ne 0) {
            throw "Could not generate package smoke license fixtures."
        }
        return ($output | ConvertFrom-Json)
    }
    finally {
        Remove-Item $tempScript -ErrorAction SilentlyContinue
    }
}

if (-not $PackagePath) {
    if ($Json) {
        Invoke-PowerShellQuiet -ArgumentList @("-ExecutionPolicy", "Bypass", "-File", ".\scripts\package-kern-runtime.ps1") -WorkingDirectory $root -FailureMessage "Runtime package build failed."
    }
    else {
        powershell -ExecutionPolicy Bypass -File ".\scripts\package-kern-runtime.ps1" | Out-Null
    }
    $latest = Get-ChildItem "output\\packages\\kern-internal-runtime-*.zip" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $latest) {
        throw "Could not build a runtime package under output\\packages."
    }
    $PackagePath = $latest.FullName
}

$resolvedPackage = (Resolve-Path $PackagePath).Path
$packageValidation = powershell -ExecutionPolicy Bypass -File ".\scripts\validate-kern-package.ps1" -PackagePath $resolvedPackage -Json | ConvertFrom-Json
if (-not $packageValidation.valid) {
    throw "Runtime package validation failed before smoke install."
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$smokeRoot = if ([System.IO.Path]::IsPathRooted($OutputRoot)) { $OutputRoot } else { Join-Path $root $OutputRoot }
$extractRoot = Join-Path $smokeRoot "kern-runtime-smoke-$timestamp"
$reportPath = Join-Path $extractRoot "smoke-report.json"
$runtimeLog = Join-Path $extractRoot "runtime-smoke.log"
$runtimeErrLog = Join-Path $extractRoot "runtime-smoke.err.log"

New-Item -ItemType Directory -Force -Path $smokeRoot | Out-Null
if (Test-Path $extractRoot) {
    Remove-Item -Recurse -Force $extractRoot
}
New-Item -ItemType Directory -Force -Path $extractRoot | Out-Null

Expand-Archive -Path $resolvedPackage -DestinationPath $extractRoot -Force

$installResult = $null
$preflightJson = $null
$runtimeProcess = $null
$health = $null
$readiness = $null
$licenseFixtures = $null
$validationRuns = @{}

Push-Location $extractRoot
try {
    $previousLlmEnabled = $env:KERN_LLM_ENABLED
    $previousLlamaUrl = $env:KERN_LLAMA_SERVER_URL
    $previousLlamaModel = $env:KERN_LLAMA_SERVER_MODEL_PATH
    $env:KERN_LLM_ENABLED = "false"
    Remove-Item Env:\KERN_LLAMA_SERVER_URL -ErrorAction SilentlyContinue
    Remove-Item Env:\KERN_LLAMA_SERVER_MODEL_PATH -ErrorAction SilentlyContinue
    if ($Json) {
        Invoke-PowerShellQuiet -ArgumentList @("-ExecutionPolicy", "Bypass", "-File", ".\scripts\install-kern.ps1", "-InternalDeploy") -WorkingDirectory $extractRoot -FailureMessage "Packaged install failed."
    }
    else {
        powershell -ExecutionPolicy Bypass -File ".\scripts\install-kern.ps1" -InternalDeploy
    }
    $installResult = "ok"

    $preflightRaw = python ".\scripts\preflight-kern.py" --json
    if ($LASTEXITCODE -ne 0) {
        throw "Preflight failed inside extracted package."
    }
    $preflightJson = $preflightRaw | ConvertFrom-Json

    $pythonExe = (Resolve-Path ".\.venv\Scripts\python.exe").Path
    $licenseFixtures = New-LicenseFixtures -PythonExe $pythonExe -FixtureRoot (Join-Path $extractRoot ".kern\validation-license-fixtures")
    Add-Content -Path ".env" -Value "`r`nKERN_LICENSE_PUBLIC_KEY=$($licenseFixtures.public_key)" -Encoding UTF8

    $port = Get-FreeTcpPort
    $runtimeProcess = Start-Process -FilePath $pythonExe -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$port") -WorkingDirectory $extractRoot -PassThru -RedirectStandardOutput $runtimeLog -RedirectStandardError $runtimeErrLog
    $baseUrl = "http://127.0.0.1:$port"
    $env:KERN_VALIDATION_LICENSE_DIR = [string]$licenseFixtures.directory

    $health = Wait-HttpReady -Url "$baseUrl/health/live"
    $readiness = Wait-HttpReady -Url "$baseUrl/health/ready"

    foreach ($lane in @("shell_smoke", "license_sample_flow", "sample_to_real_transition")) {
        $laneOutputDir = Join-Path $extractRoot "output\package-validation\$lane"
        $laneRaw = & $pythonExe ".\scripts\validate-kern-ui.py" --base-url $baseUrl --lane $lane --output-dir $laneOutputDir
        if ($LASTEXITCODE -ne 0) {
            throw "Validation lane '$lane' failed during package smoke."
        }
        $validationRuns[$lane] = ($laneRaw | ConvertFrom-Json)
    }
}
finally {
    if ($null -eq $previousLlmEnabled) { Remove-Item Env:\KERN_LLM_ENABLED -ErrorAction SilentlyContinue } else { $env:KERN_LLM_ENABLED = $previousLlmEnabled }
    if ($null -eq $previousLlamaUrl) { Remove-Item Env:\KERN_LLAMA_SERVER_URL -ErrorAction SilentlyContinue } else { $env:KERN_LLAMA_SERVER_URL = $previousLlamaUrl }
    if ($null -eq $previousLlamaModel) { Remove-Item Env:\KERN_LLAMA_SERVER_MODEL_PATH -ErrorAction SilentlyContinue } else { $env:KERN_LLAMA_SERVER_MODEL_PATH = $previousLlamaModel }
    if ($runtimeProcess -and -not $runtimeProcess.HasExited) {
        Stop-Process -Id $runtimeProcess.Id -Force -ErrorAction SilentlyContinue
    }
    Remove-Item Env:\KERN_VALIDATION_LICENSE_DIR -ErrorAction SilentlyContinue
    Pop-Location
}

$report = [ordered]@{
    package = $resolvedPackage
    extracted_to = $extractRoot
    install_result = $installResult
    preflight = $preflightJson
    health = $health
    readiness = $readiness
    package_validation = $packageValidation
    license_fixtures = $licenseFixtures
    validation_runs = $validationRuns
    runtime_log = $runtimeLog
    runtime_error_log = $runtimeErrLog
    created_at = (Get-Date).ToString("o")
}

$report | ConvertTo-Json -Depth 10 | Set-Content -Path $reportPath -Encoding UTF8

if ($Json) {
    $report | ConvertTo-Json -Depth 10
}
else {
    Write-Host "Package smoke passed. Report: $reportPath"
    if (-not $KeepExtracted) {
        Write-Host "Extracted runtime preserved at $extractRoot"
    }
}
