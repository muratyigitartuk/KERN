param(
    [switch]$SkipConnectivity,
    [switch]$Json
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$env:KERN_DISABLE_DOTENV = "true"
Remove-Item Env:KERN_ADMIN_AUTH_TOKEN -ErrorAction SilentlyContinue
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = "python"
}

function Add-Result {
    param(
        [System.Collections.ArrayList]$Results,
        [string]$Name,
        [bool]$Ok,
        [string]$Detail
    )
    [void]$Results.Add([ordered]@{
        name = $Name
        ok = $Ok
        detail = $Detail
    })
}

$results = [System.Collections.ArrayList]::new()

if ($env:KERN_SERVER_MODE -ne "true") {
    Add-Result $results "server_mode" $false "KERN_SERVER_MODE=true is required for the server release gate."
}
else {
    Add-Result $results "server_mode" $true "server mode requested"
}

$configScript = @"
from app.config import settings
print("server_mode=", settings.server_mode)
"@
$configOutput = ""
try {
    $configOutput = $configScript | & $python -
    if ($LASTEXITCODE -ne 0) {
        throw "configuration validation exited with code $LASTEXITCODE"
    }
    Add-Result $results "configuration" $true ($configOutput -join "`n")
}
catch {
    Add-Result $results "configuration" $false $_.Exception.Message
}

if (-not $SkipConnectivity -and $env:KERN_SERVER_MODE -eq "true") {
    $connectivityScript = @"
from app.config import settings
errors = []
if settings.postgres_dsn:
    try:
        import psycopg
        with psycopg.connect(settings.postgres_dsn, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("select 1")
                cur.fetchone()
    except Exception as exc:
        errors.append(f"postgres: {exc}")
if settings.redis_url:
    try:
        import redis
        client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=5, socket_timeout=5)
        client.ping()
    except Exception as exc:
        errors.append(f"redis: {exc}")
if errors:
    raise SystemExit("; ".join(errors))
print("connectivity ok")
"@
    try {
        $connectivityOutput = $connectivityScript | & $python -
        if ($LASTEXITCODE -ne 0) {
            throw "connectivity validation exited with code $LASTEXITCODE"
        }
        Add-Result $results "connectivity" $true ($connectivityOutput -join "`n")
    }
    catch {
        Add-Result $results "connectivity" $false $_.Exception.Message
    }
}
elseif ($SkipConnectivity) {
    Add-Result $results "connectivity" $true "skipped by operator"
}

$testArgs = @(
    "tests/test_multi_user_threads.py",
    "tests/test_security_regressions.py",
    "tests/test_security_remediation_plan.py",
    "tests/test_auth_routes.py"
)
try {
    $previousSkipValidation = $env:KERN_SKIP_VALIDATION
    $previousServerMode = $env:KERN_SERVER_MODE
    $env:KERN_SKIP_VALIDATION = "1"
    $env:KERN_SERVER_MODE = "false"
    & $python -m pytest @testArgs
    if ($LASTEXITCODE -ne 0) {
        throw "pytest exited with code $LASTEXITCODE"
    }
    Add-Result $results "server_tests" $true "server authorization and security tests passed"
}
catch {
    Add-Result $results "server_tests" $false $_.Exception.Message
}
finally {
    if ($null -eq $previousSkipValidation) {
        Remove-Item Env:KERN_SKIP_VALIDATION -ErrorAction SilentlyContinue
    }
    else {
        $env:KERN_SKIP_VALIDATION = $previousSkipValidation
    }
    if ($null -eq $previousServerMode) {
        Remove-Item Env:KERN_SERVER_MODE -ErrorAction SilentlyContinue
    }
    else {
        $env:KERN_SERVER_MODE = $previousServerMode
    }
}

$ok = -not ($results | Where-Object { -not $_.ok })
$report = [ordered]@{
    ok = $ok
    created_at = (Get-Date).ToString("o")
    results = $results
}

if ($Json) {
    $report | ConvertTo-Json -Depth 6
}
else {
    if ($ok) {
        Write-Host "KERN server release gate passed." -ForegroundColor Green
    }
    else {
        Write-Host "KERN server release gate failed." -ForegroundColor Red
    }
    foreach ($result in $results) {
        $prefix = if ($result.ok) { "PASS" } else { "FAIL" }
        Write-Host "[$prefix] $($result.name): $($result.detail)"
    }
}

if (-not $ok) {
    exit 1
}
