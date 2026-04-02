$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Write-KernUpdateState {
    param(
        [string]$LastAttemptAt = "",
        [string]$LastSuccessAt = "",
        [string]$LastBackupAt = "",
        [string]$LastRestoreAttemptAt = "",
        [string]$LastStatus = "idle",
        [string]$LastError = ""
    )

    $stateRoot = Join-Path $root ".kern"
    New-Item -ItemType Directory -Force -Path $stateRoot | Out-Null
    $statePath = Join-Path $stateRoot "update-state.json"
    $existing = @{}
    if (Test-Path $statePath) {
        try {
            $existing = Get-Content $statePath -Raw | ConvertFrom-Json -AsHashtable
        } catch {
            $existing = @{}
        }
    }
    if ($LastAttemptAt) { $existing["last_attempt_at"] = $LastAttemptAt }
    if ($LastSuccessAt) { $existing["last_success_at"] = $LastSuccessAt }
    if ($LastBackupAt) { $existing["last_backup_at"] = $LastBackupAt }
    if ($LastRestoreAttemptAt) { $existing["last_restore_attempt_at"] = $LastRestoreAttemptAt }
    if ($LastStatus) { $existing["last_status"] = $LastStatus }
    $existing["last_error"] = $LastError
    ($existing | ConvertTo-Json -Depth 4) | Set-Content -Encoding UTF8 $statePath
}

if (-not (Test-Path .\.venv\Scripts\Activate.ps1)) {
    throw "KERN is not installed in .venv."
}

# --- Version check: compare current version with latest available ---
$pyprojectPath = Join-Path $root "pyproject.toml"
$currentVersion = "0.0.0"
if (Test-Path $pyprojectPath) {
    $match = Select-String -Path $pyprojectPath -Pattern '^\s*version\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($match) {
        $currentVersion = $match.Matches[0].Groups[1].Value
    }
}
Write-Host "Current KERN version: $currentVersion" -ForegroundColor Cyan

# Check latest version from the running instance (if available)
$latestVersion = $null
try {
    $response = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/version" -TimeoutSec 5 -ErrorAction Stop
    $latestVersion = $response.version
    Write-Host "Running instance version: $latestVersion" -ForegroundColor Cyan
} catch {
    Write-Host "Could not reach running KERN instance for version check. Proceeding with update." -ForegroundColor Yellow
}

if ($latestVersion -and $latestVersion -eq $currentVersion) {
    Write-Host "KERN is already at version $currentVersion." -ForegroundColor Green
    $proceed = Read-Host "Continue with update anyway? (y/N)"
    if ($proceed -ne "y" -and $proceed -ne "Y") {
        Write-Host "Update cancelled."
        exit 0
    }
}
# --- End version check ---

function Get-KernBackupPassword {
    if ($env:KERN_BACKUP_PASSWORD -and $env:KERN_BACKUP_PASSWORD.Trim()) {
        return $env:KERN_BACKUP_PASSWORD
    }

    $secure = Read-Host "Enter the encrypted update backup password" -AsSecureString
    if ($secure.Length -eq 0) {
        throw "An encrypted backup password is required."
    }

    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }
}

function Invoke-KernPythonWithPassword {
    param(
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [Parameter(Mandatory = $true)][string]$Password,
        [switch]$Quiet
    )

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "python"
    foreach ($arg in $ArgumentList) { [void]$psi.ArgumentList.Add($arg) }
    [void]$psi.ArgumentList.Add("--password-stdin")
    $psi.WorkingDirectory = $root
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    $null = $proc.Start()
    $proc.StandardInput.WriteLine($Password)
    $proc.StandardInput.Close()
    $stdout = $proc.StandardOutput.ReadToEnd()
    $stderr = $proc.StandardError.ReadToEnd()
    $proc.WaitForExit()
    if (-not $Quiet -and $stdout) {
        Write-Host $stdout.TrimEnd()
    }
    if ($proc.ExitCode -ne 0) {
        throw ($stderr + $stdout).Trim()
    }
    return $stdout
}

$backupPassword = Get-KernBackupPassword

. .\.venv\Scripts\Activate.ps1

$attemptTimestamp = (Get-Date).ToUniversalTime().ToString("o")
Write-KernUpdateState -LastAttemptAt $attemptTimestamp -LastStatus "idle" -LastError ""

python scripts\preflight-kern.py --json
if ($LASTEXITCODE -ne 0) {
    throw "Preflight checks failed before update."
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupRoot = Join-Path $root ".kern\upgrade-backups"
New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null

$bundlePath = Join-Path $backupRoot "update-$timestamp.kernbundle"
$kernRoot = Join-Path $root ".kern"
$requirementsSnapshot = Join-Path $backupRoot "update-$timestamp-requirements.txt"

if (Test-Path $kernRoot) {
    try {
        Invoke-KernPythonWithPassword -ArgumentList @("scripts\create-kern-update-bundle.py", "--root", $root, "--output", $bundlePath) -Password $backupPassword | Out-Null
    } catch {
        Write-KernUpdateState -LastStatus "failed" -LastError "Encrypted update bundle creation failed."
        throw "Encrypted update bundle creation failed."
    }
    try {
        Invoke-KernPythonWithPassword -ArgumentList @("scripts\restore-kern.py", $bundlePath, "--validate-only", "--json") -Password $backupPassword -Quiet | Out-Null
    } catch {
        Write-KernUpdateState -LastStatus "failed" -LastError "Encrypted update bundle validation failed."
        throw "Encrypted update bundle validation failed."
    }
    Write-KernUpdateState -LastBackupAt (Get-Date).ToUniversalTime().ToString("o")
} else {
    Write-Host "No .kern directory found; skipping encrypted update bundle."
}

python -m pip freeze | Out-File -Encoding utf8 $requirementsSnapshot

try {
    python -m pip install --upgrade pip
    python -m pip install -e .[documents,scheduler,system_control]
    python -m compileall app tests | Out-Null
    python -c "import app.main; print('import-ok')"
    python scripts\preflight-kern.py --json
    if ($LASTEXITCODE -ne 0) {
        throw "Post-update preflight checks failed."
    }
}
catch {
    Write-Warning "Update failed. Attempting rollback."
    $failureMessage = $_.Exception.Message
    Write-KernUpdateState -LastStatus "failed" -LastError $failureMessage
    if (Test-Path $requirementsSnapshot) {
        python -m pip install -r $requirementsSnapshot | Out-Null
    }
    if (Test-Path $bundlePath) {
        Write-KernUpdateState -LastRestoreAttemptAt (Get-Date).ToUniversalTime().ToString("o")
        try {
            Invoke-KernPythonWithPassword -ArgumentList @("scripts\restore-kern.py", $bundlePath, "--restore-root", $kernRoot, "--replace-root", "--force", "--json") -Password $backupPassword | Out-Null
            Write-KernUpdateState -LastStatus "rollback_performed" -LastError ""
        } catch {
        }
    }
    python scripts\preflight-kern.py --json | Out-Null
    throw
}

Write-KernUpdateState -LastSuccessAt (Get-Date).ToUniversalTime().ToString("o") -LastStatus "succeeded" -LastError ""
Write-Host "KERN updated."
Write-Host "Upgrade verification passed."
