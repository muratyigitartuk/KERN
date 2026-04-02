param(
    [string]$Model = $env:KERN_HF_ADAPTER_MODEL,
    [string]$AdapterPath = $env:KERN_HF_ADAPTER_PATH,
    [string]$Alias = $(if ($env:KERN_HF_ADAPTER_ALIAS) { $env:KERN_HF_ADAPTER_ALIAS } else { $env:KERN_LLM_MODEL }),
    [string]$BindHost = "127.0.0.1",
    [int]$Port = $(if ($env:KERN_HF_ADAPTER_PORT) { [int]$env:KERN_HF_ADAPTER_PORT } else { 8080 }),
    [string]$DeviceMap = $(if ($env:KERN_HF_ADAPTER_DEVICE_MAP) { $env:KERN_HF_ADAPTER_DEVICE_MAP } else { "auto" }),
    [switch]$TrustRemoteCode,
    [switch]$LoadIn4Bit
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path .\.venv\Scripts\python.exe)) {
    throw "KERN is not installed in .venv."
}

if (-not $Model) {
    throw "Set KERN_HF_ADAPTER_MODEL or pass -Model."
}

if (-not $AdapterPath) {
    throw "Set KERN_HF_ADAPTER_PATH or pass -AdapterPath."
}

if (-not (Test-Path $AdapterPath)) {
    throw "HF adapter path does not exist: $AdapterPath"
}

$python = (Resolve-Path .\.venv\Scripts\python.exe).Path
$script = (Resolve-Path .\scripts\run-hf-adapter-server.py).Path

$arguments = @(
    $script,
    "--model", $Model,
    "--adapter", (Resolve-Path $AdapterPath).Path,
    "--host", $BindHost,
    "--port", [string]$Port,
    "--device-map", $DeviceMap
)

if ($Alias) {
    $arguments += @("--alias", $Alias)
}

if ($TrustRemoteCode -or $env:KERN_HF_ADAPTER_TRUST_REMOTE_CODE -eq "true") {
    $arguments += "--trust-remote-code"
}

if ($LoadIn4Bit -or $env:KERN_HF_ADAPTER_LOAD_IN_4BIT -eq "true") {
    $arguments += "--load-in-4bit"
}

& $python @arguments
exit $LASTEXITCODE
