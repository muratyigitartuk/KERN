#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Install KERN AI Workspace as a Windows Service.
.DESCRIPTION
    Uses pywin32 service framework or NSSM as fallback.
#>

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir

Write-Host "=== KERN AI Workspace Service Installer ===" -ForegroundColor Cyan

# Try pywin32 service install first
$PythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $PythonExe) {
    Write-Error "Python not found in PATH. Please install Python 3.10+ first."
    exit 1
}

$ServiceScript = Join-Path $ScriptDir "kern-service.py"
if (Test-Path $ServiceScript) {
    Write-Host "Installing service via pywin32..."
    try {
        & $PythonExe $ServiceScript install
        & $PythonExe $ServiceScript update --startup=delayed
        Write-Host "Service installed successfully." -ForegroundColor Green
        Write-Host "Start with: python $ServiceScript start" -ForegroundColor Yellow
        exit 0
    } catch {
        Write-Warning "pywin32 service install failed: $_"
        Write-Host "Trying NSSM fallback..." -ForegroundColor Yellow
    }
}

# NSSM fallback
$NssmPath = Get-Command nssm -ErrorAction SilentlyContinue
if (-not $NssmPath) {
    Write-Host "NSSM not found. Download from https://nssm.cc/ and add to PATH." -ForegroundColor Red
    Write-Host "Or install pywin32: pip install pywin32" -ForegroundColor Red
    exit 1
}

$ServiceName = "KERNWorkspace"
nssm install $ServiceName "`"$PythonExe`"" "-m uvicorn app.main:app --host 127.0.0.1 --port 8000"
nssm set $ServiceName AppDirectory "`"$ProjectDir`""
nssm set $ServiceName AppStdout "`"$(Join-Path $ProjectDir '.kern\kern-service.log')`""
nssm set $ServiceName AppStderr "`"$(Join-Path $ProjectDir '.kern\kern-service-error.log')`""
nssm set $ServiceName AppRestartDelay 5000
nssm set $ServiceName AppExit Default Restart
nssm set $ServiceName Start SERVICE_DELAYED_AUTO_START
nssm set $ServiceName Description "Privacy-first local AI workspace for enterprise use."

Write-Host "Service '$ServiceName' installed via NSSM." -ForegroundColor Green
Write-Host "Start with: nssm start $ServiceName" -ForegroundColor Yellow
