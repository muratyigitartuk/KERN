param(
    [switch]$RemoveData,
    [string]$TaskName = "KERN Local Runtime",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Stop-KernRuntimeProcesses {
    $repoPath = (Resolve-Path $root).Path
    $pythonProcesses = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction SilentlyContinue
    foreach ($process in $pythonProcesses) {
        $commandLine = [string]$process.CommandLine
        if ($commandLine -like "*app.main:app*" -or $commandLine -like "*scripts\\run-kern.ps1*" -or $commandLine -like "*$repoPath*") {
            try {
                Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
            } catch {
                Write-Warning "Could not stop process $($process.ProcessId): $($_.Exception.Message)"
            }
        }
    }
}

Write-Host "Stopping KERN runtime processes if they are active..."
Stop-KernRuntimeProcesses

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Removing scheduled task '$TaskName'..."
}

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    try {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop | Out-Null
    }
    catch {
        Write-Warning "Could not unregister scheduled task '$TaskName': $($_.Exception.Message)"
    }
}

$venvPath = Join-Path $root ".venv"
if (Test-Path $venvPath) {
    Write-Host "Removing local runtime environment .venv..."
    Remove-Item -LiteralPath $venvPath -Recurse -Force
}

$logsPath = Join-Path $root ".kern\\logs"
if (-not $RemoveData -and (Test-Path $logsPath)) {
    Write-Host "Removing runtime log files from .kern\\logs..."
    Remove-Item -LiteralPath $logsPath -Recurse -Force
}

if ($RemoveData) {
    $dataPath = Join-Path $root ".kern"
    if (Test-Path $dataPath) {
        Write-Host "Removing local KERN data under .kern..."
        Remove-Item -LiteralPath $dataPath -Recurse -Force
    }
    $tokensPath = Join-Path $root ".tokens"
    if (Test-Path $tokensPath) {
        Write-Host "Removing cached token material under .tokens..."
        Remove-Item -LiteralPath $tokensPath -Recurse -Force
    }
} else {
    Write-Host "Preserving local KERN profile data under .kern."
}

Write-Host "KERN uninstall complete."
if ($RemoveData) {
    Write-Host "Runtime artifacts and local profile data were removed."
} else {
    Write-Host "Runtime artifacts were removed. Local profile data and backups remain under .kern."
}
