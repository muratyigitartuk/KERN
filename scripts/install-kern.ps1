param(
    [switch]$InternalDeploy,
    [switch]$Managed,
    [switch]$Corporate,
    [switch]$RegisterTask,
    [switch]$IncludeDev,
    [switch]$IncludeHfAdapter
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Set-KernEnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)][string]$Value
    )

    if (-not (Test-Path .env)) {
        Set-Content -Path .env -Value "" -Encoding UTF8
    }

    $content = Get-Content .env -Raw -Encoding UTF8
    $pattern = "(?m)^$([regex]::Escape($Key))=.*$"
    $line = "$Key=$Value"
    if ([regex]::IsMatch($content, $pattern)) {
        $updated = [regex]::Replace($content, $pattern, $line)
        Set-Content -Path .env -Value $updated -Encoding UTF8
    }
    else {
        $prefix = if ($content.Length -gt 0 -and -not $content.EndsWith("`n")) { "`r`n" } else { "" }
        Add-Content -Path .env -Value "$prefix$line" -Encoding UTF8
    }
}

if ($InternalDeploy) {
    $Managed = $true
    $Corporate = $true
}

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python is required."
}

python -m venv .venv
. .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
$extras = @("documents", "scheduler", "system_control")
if ($IncludeDev) {
    $extras += "dev"
}
if ($IncludeHfAdapter) {
    $extras += "hf_adapter"
}
$extrasArg = [string]::Join(",", $extras)
python -m pip install -e ".[${extrasArg}]"

if (-not (Test-Path .env) -and (Test-Path .env.example)) {
    Copy-Item .env.example .env
}

if ($Managed -or $InternalDeploy) {
    New-Item -ItemType Directory -Force -Path ".kern", ".kern\\profiles", ".kern\\backups", ".kern\\documents", ".kern\\attachments", ".kern\\archives", ".kern\\logs", ".kern\\licenses" | Out-Null
    $validationKeyPath = ".kern\\licenses\\validation-public-key.pem"
    if (-not (Test-Path $validationKeyPath)) {
        Set-Content -Path $validationKeyPath -Value "# Replace with the pilot license verification public key." -Encoding ASCII
    }
}

Set-KernEnvValue -Key "KERN_PRODUCT_POSTURE" -Value "production"
Set-KernEnvValue -Key "KERN_DOCUMENT_ROOT" -Value ".kern/documents"
Set-KernEnvValue -Key "KERN_ATTACHMENT_ROOT" -Value ".kern/attachments"
Set-KernEnvValue -Key "KERN_ARCHIVE_ROOT" -Value ".kern/archives"
Set-KernEnvValue -Key "KERN_LLM_LOCAL_ONLY" -Value "true"
if (-not $env:KERN_ADMIN_AUTH_TOKEN) {
    Set-KernEnvValue -Key "KERN_ADMIN_AUTH_TOKEN" -Value "local-admin-$([guid]::NewGuid().ToString('N'))"
}

if ($Corporate -or $InternalDeploy) {
    Set-KernEnvValue -Key "KERN_POLICY_MODE" -Value "corporate"
    Set-KernEnvValue -Key "KERN_AUDIT_ENABLED" -Value "true"
    Set-KernEnvValue -Key "KERN_RETENTION_ENFORCEMENT_ENABLED" -Value "true"
}

if ($InternalDeploy) {
    Set-KernEnvValue -Key "KERN_UPDATE_CHANNEL" -Value "stable"
    Set-KernEnvValue -Key "KERN_NETWORK_MONITOR_ENABLED" -Value "true"
}

python -c "import app.main; print('import-ok')"
python scripts\preflight-kern.py --json
if ($LASTEXITCODE -ne 0) {
    throw "Preflight failed after install."
}

if ($Managed -and $RegisterTask) {
    .\scripts\register-kern-task.ps1
}

Write-Host "KERN installed."
Write-Host "Run: .\\.venv\\Scripts\\Activate.ps1"
Write-Host "Start: python -m uvicorn app.main:app --host 127.0.0.1 --port 8000"
Write-Host "Open: http://127.0.0.1:8000"
Write-Host "First value: confirm the local profile, confirm the recommended local model path, then upload one document and draft a German reply from it."
if ($Managed) {
    Write-Host "Managed mode prepared local roots in .kern\\"
}
if ($Corporate) {
    Write-Host "Corporate policy defaults were written to .env."
}
if ($InternalDeploy) {
    Write-Host "Internal deployment preset applied: production posture, corporate policy, local-only LLM, stable channel."
    Write-Host "Blessed product path: one controlled Windows machine with one local model endpoint for grounded drafting."
}
if (-not $IncludeDev) {
    Write-Host "Installed runtime extras only. Use -IncludeDev if you want test and developer dependencies."
}
if ($IncludeHfAdapter) {
    Write-Host "HF adapter serving extras installed. Use scripts\\run-kern-hf-adapter-server.ps1 for the reference-quality adapter path."
}
