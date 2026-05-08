param(
    [string]$PackagePath,
    [switch]$Json
)

$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.IO.Compression.FileSystem

if (-not $PackagePath) {
    $latest = Get-ChildItem "output\\packages\\kern-internal-runtime-*.zip" -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        Select-Object -First 1
    if (-not $latest) {
        throw "No runtime package found under output\\packages."
    }
    $PackagePath = $latest.FullName
}

$resolved = (Resolve-Path $PackagePath).Path
if (-not (Test-Path $resolved)) {
    throw "Package not found: $PackagePath"
}

$requiredEntries = @(
    "install-kern.cmd",
    "kern.cmd",
    "start-kern.cmd",
    "Install KERN.cmd",
    "Start KERN.cmd",
    "README.md",
    "LICENSE",
    "NOTICE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    ".env.example",
    "pyproject.toml",
    "package-manifest.json",
    "docs/internal-deployment.md",
    "docs/quickstart.md",
    "docs/gpu-setup.md",
    "docs/model-setup.md",
    "docs/architecture.md",
    "docs/operator-runbook.md",
    "docs/update-rollback-guide.md",
    "docs/sample-workspace-guide.md",
    "docs/backup-guide.md",
    "docs/restore-guide.md",
    "docs/uninstall-data-deletion.md",
    "docs/release-checklist.md",
    "docs/troubleshooting-guide.md",
    "docs/windows-deployment.md",
    "docs/validation-pack.md",
    "docs/validation-walkthrough.md",
    "src-tauri/Cargo.toml",
    "src-tauri/Cargo.lock",
    "src-tauri/build.rs",
    "src-tauri/tauri.conf.json",
    "src-tauri/src/main.rs",
    "src-tauri/icons/icon.ico",
    "scripts/install-and-start-kern-tauri.ps1",
    "scripts/install-kern.ps1",
    "scripts/run-kern.ps1",
    "scripts/start-kern.ps1",
    "scripts/start-kern-desktop.ps1",
    "scripts/update-kern.ps1",
    "scripts/uninstall-kern.ps1",
    "scripts/smoke-kern-runtime-package.ps1",
    "scripts/smoke-kern-update-restore.ps1",
    "scripts/smoke-kern-uninstall.ps1",
    "scripts/run-kern-release-gate.ps1",
    "scripts/build-kern-desktop-release.ps1",
    "scripts/create-kern-update-bundle.py",
    "scripts/restore-kern.py",
    "scripts/preflight-kern.py",
    "scripts/validate-kern-package.ps1",
    "scripts/validate-kern-ui.ps1",
    "scripts/validate-kern-ui.py",
    "scripts/register-kern-task.ps1",
    "scripts/status-kern-task.ps1",
    "scripts/unregister-kern-task.ps1",
    "scripts/install-kern-service.ps1",
    "scripts/kern-service.py",
    "app/main.py",
    "tests/fixtures/validation/acme_invoice.txt",
    "tests/fixtures/validation/acme_offer.txt",
    "tests/fixtures/validation/acme_finance.csv",
    "tests/fixtures/validation/retention_policy.md"
)

$zip = [System.IO.Compression.ZipFile]::OpenRead($resolved)
try {
    $entries = $zip.Entries | ForEach-Object { $_.FullName -replace '\\', '/' }
    $missing = @()
    foreach ($entry in $requiredEntries) {
        $normalized = $entry -replace '\\', '/'
        if ($entries -notcontains $normalized) {
            $missing += $entry
        }
    }

    $manifestEntry = $zip.Entries | Where-Object { (($_.FullName -replace '\\', '/')) -eq "package-manifest.json" } | Select-Object -First 1
    if (-not $manifestEntry) {
        throw "package-manifest.json missing from package."
    }
    $reader = New-Object System.IO.StreamReader($manifestEntry.Open())
    try {
        $manifest = $reader.ReadToEnd() | ConvertFrom-Json
    }
    finally {
        $reader.Dispose()
    }

    $manifestErrors = @()
    foreach ($field in @("package_type", "created_at", "source_branch", "source_commit", "app_version", "deployment_profile", "included_paths")) {
        if (-not ($manifest.PSObject.Properties.Name -contains $field)) {
            $manifestErrors += "Missing manifest field: $field"
        }
    }
    if (($manifest.PSObject.Properties.Name -contains "package_type") -and $manifest.package_type -ne "kern_internal_runtime") {
        $manifestErrors += "Unexpected package_type: $($manifest.package_type)"
    }
    if (($manifest.PSObject.Properties.Name -contains "deployment_profile") -and $manifest.deployment_profile -ne "internal_managed") {
        $manifestErrors += "Unexpected deployment_profile: $($manifest.deployment_profile)"
    }

    $checksumPath = "$resolved.sha256"
    $checksumVerified = $false
    $checksumError = $null
    if (Test-Path $checksumPath) {
        $line = (Get-Content $checksumPath -Raw).Trim()
        if ($line -match '^([0-9a-fA-F]{64})\s+(.+)$') {
            $expected = $matches[1].ToLowerInvariant()
            $actual = (Get-FileHash -Path $resolved -Algorithm SHA256).Hash.ToLowerInvariant()
            $checksumVerified = $expected -eq $actual
            if (-not $checksumVerified) {
                $checksumError = "Checksum mismatch."
            }
        }
        else {
            $checksumError = "Checksum file format invalid."
        }
    }

    if (-not (Test-Path $checksumPath)) {
        $checksumError = "Checksum file missing."
    }

    $payload = [ordered]@{
        package = $resolved
        valid = ($missing.Count -eq 0 -and $manifestErrors.Count -eq 0 -and (-not $checksumError) -and $checksumVerified)
        missing_entries = $missing
        manifest_errors = $manifestErrors
        checksum_present = (Test-Path $checksumPath)
        checksum_verified = $checksumVerified
        checksum_error = $checksumError
        manifest = $manifest
    }

    if ($Json) {
        $payload | ConvertTo-Json -Depth 6
    }
    else {
        if ($payload.valid) {
            Write-Host "Package validation passed: $resolved" -ForegroundColor Green
        }
        else {
            Write-Host "Package validation failed: $resolved" -ForegroundColor Red
        }
        if ($missing.Count -gt 0) {
            Write-Host "Missing entries:" -ForegroundColor Yellow
            $missing | ForEach-Object { Write-Host " - $_" }
        }
        if ($manifestErrors.Count -gt 0) {
            Write-Host "Manifest errors:" -ForegroundColor Yellow
            $manifestErrors | ForEach-Object { Write-Host " - $_" }
        }
        if ($checksumError) {
            Write-Host "Checksum: $checksumError" -ForegroundColor Yellow
        }
        elseif ($checksumVerified) {
            Write-Host "Checksum verified." -ForegroundColor Green
        }
    }

    if (-not $payload.valid) {
        exit 1
    }
}
finally {
    $zip.Dispose()
}
