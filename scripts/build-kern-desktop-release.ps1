param(
    [switch]$SkipPythonInstall,
    [switch]$SkipToolInstall
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir "..")).Path
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$tauriReleaseRoot = Join-Path $repoRoot "src-tauri\target\release"

function Stop-StaleDesktopReleaseProcesses {
    $releaseRootPattern = "$tauriReleaseRoot*"
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            ($_.Name -in @("kern-desktop.exe", "python.exe", "uvicorn.exe")) -and
            ($_.ExecutablePath -like $releaseRootPattern -or $_.CommandLine -like "*$tauriReleaseRoot*")
        } |
        ForEach-Object {
            try {
                Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
            }
            catch {
            }
        }
}

function Invoke-Checked {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory = $repoRoot
    )
    Push-Location $WorkingDirectory
    try {
        & $FilePath @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "$FilePath exited with code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}

if (-not $SkipPythonInstall -or -not (Test-Path $venvPython)) {
    $startArgs = @(
        "-ExecutionPolicy", "Bypass",
        "-File", (Join-Path $repoRoot "scripts\start-kern.ps1"),
        "-NoStart"
    )
    if ($SkipToolInstall) {
        $startArgs += "-SkipToolInstall"
    }
    Invoke-Checked "powershell.exe" $startArgs $repoRoot
}

if (-not (Test-Path $venvPython)) {
    throw "Missing .venv Python after setup: $venvPython"
}

Stop-StaleDesktopReleaseProcesses

Invoke-Checked "powershell.exe" @(
    "-ExecutionPolicy", "Bypass",
    "-File", (Join-Path $repoRoot "scripts\package-tauri-runtime.ps1"),
    "-IncludeVenv"
) $repoRoot

Stop-StaleDesktopReleaseProcesses

Invoke-Checked "cargo" @("tauri", "build") (Join-Path $repoRoot "src-tauri")

$installer = Get-ChildItem (Join-Path $repoRoot "src-tauri\target\release\bundle\nsis") -Filter "*setup.exe" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1

if (-not $installer) {
    throw "Tauri build finished but no NSIS setup executable was found."
}

Write-Host "Built KERN desktop installer: $($installer.FullName)" -ForegroundColor Green
