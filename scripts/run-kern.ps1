param(
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path .\.venv\Scripts\python.exe)) {
    throw "KERN is not installed in .venv."
}

$python = (Resolve-Path .\.venv\Scripts\python.exe).Path
& $python -m uvicorn app.main:app --host $BindHost --port $Port
