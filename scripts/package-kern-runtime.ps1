param(
    [string]$OutputRoot = "output\\packages"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$staging = Join-Path $env:TEMP "kern-runtime-package-$timestamp"
$outputDir = Join-Path $root $OutputRoot
$zipPath = Join-Path $outputDir "kern-internal-runtime-$timestamp.zip"
$pyprojectPath = Join-Path $root "pyproject.toml"
$appVersion = "0.0.0"
if (Test-Path $pyprojectPath) {
    $versionMatch = Select-String -Path $pyprojectPath -Pattern '^\s*version\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($versionMatch) {
        $appVersion = $versionMatch.Matches[0].Groups[1].Value
    }
}
$sourceBranch = (& git branch --show-current).Trim()
$sourceCommit = (& git rev-parse HEAD).Trim()

if (Test-Path $staging) {
    Remove-Item -Recurse -Force $staging
}
New-Item -ItemType Directory -Force -Path $staging, $outputDir | Out-Null

$include = @(
    "install-kern.cmd",
    "kern.cmd",
    "start-kern.cmd",
    "Install KERN.cmd",
    "Start KERN.cmd",
    "app",
    "src-tauri\\Cargo.toml",
    "src-tauri\\Cargo.lock",
    "src-tauri\\build.rs",
    "src-tauri\\tauri.conf.json",
    "src-tauri\\src",
    "src-tauri\\icons",
    "src-tauri\\gen",
    "scripts\\install-and-start-kern-tauri.ps1",
    "scripts\\install-kern.ps1",
    "scripts\\run-kern.ps1",
    "scripts\\start-kern.ps1",
    "scripts\\start-kern-desktop.ps1",
    "scripts\\run-kern-hf-adapter-server.ps1",
    "scripts\\run-hf-adapter-server.py",
    "scripts\\run-llama-server.ps1",
    "scripts\\build-llama-cpp.ps1",
    "scripts\\build-kern-desktop-release.ps1",
    "scripts\\update-kern.ps1",
    "scripts\\create-kern-update-bundle.py",
    "scripts\\restore-kern.py",
    "scripts\\preflight-kern.py",
    "scripts\\smoke-kern-runtime-package.ps1",
    "scripts\\smoke-kern-update-restore.ps1",
    "scripts\\smoke-kern-uninstall.ps1",
    "scripts\\run-kern-release-gate.ps1",
    "scripts\\run-kern-server-release-gate.ps1",
    "scripts\\uninstall-kern.ps1",
    "scripts\\validate-kern-package.ps1",
    "scripts\\validate-publish-hygiene.py",
    "scripts\\validate-kern-ui.ps1",
    "scripts\\validate-kern-ui.py",
    "scripts\\register-kern-task.ps1",
    "scripts\\status-kern-task.ps1",
    "scripts\\unregister-kern-task.ps1",
    "scripts\\install-kern-service.ps1",
    "scripts\\kern-service.py",
    "prompts",
    "README.md",
    "LICENSE",
    "NOTICE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    ".env.example",
    "pyproject.toml",
    "docs\\deployment-overview.md",
    "docs\\quickstart.md",
    "docs\\gpu-setup.md",
    "docs\\model-setup.md",
    "docs\\architecture.md",
    "docs\\server-deployment.md",
    "docs\\internal-deployment.md",
    "docs\\hf-adapter-serving.md",
    "docs\\operator-runbook.md",
    "docs\\activation-renewal-guide.md",
    "docs\\update-rollback-guide.md",
    "docs\\sample-workspace-guide.md",
    "docs\\backup-guide.md",
    "docs\\restore-guide.md",
    "docs\\uninstall-data-deletion.md",
    "docs\\troubleshooting-guide.md",
    "docs\\pilot-acceptance-checklist.md",
    "docs\\pilot-troubleshooting-matrix.md",
    "docs\\release-checklist.md",
    "docs\\windows-deployment.md",
    "docs\\security-governance.md",
    "docs\\validation-pack.md",
    "tests\\fixtures\\validation"
)

foreach ($item in $include) {
    $source = Join-Path $root $item
    if (-not (Test-Path $source)) {
        throw "Missing package source: $item"
    }
    $target = Join-Path $staging $item
    $targetParent = Split-Path -Parent $target
    if (-not (Test-Path $targetParent)) {
        New-Item -ItemType Directory -Force -Path $targetParent | Out-Null
    }
    if ((Get-Item $source) -is [System.IO.DirectoryInfo]) {
        Copy-Item -Recurse -Force $source $target
    }
    else {
        Copy-Item -Force $source $target
    }
}

$pythonCacheDirs = Get-ChildItem -Path (Join-Path $staging "app") -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue
foreach ($cacheDir in $pythonCacheDirs) {
    Remove-Item -Recurse -Force $cacheDir.FullName
}

$pythonBytecode = Get-ChildItem -Path (Join-Path $staging "app") -Recurse -Include "*.pyc", "*.pyo" -File -ErrorAction SilentlyContinue
foreach ($compiled in $pythonBytecode) {
    Remove-Item -Force $compiled.FullName
}

$manifest = @{
    package_type = "kern_internal_runtime"
    created_at = (Get-Date).ToString("o")
    source_branch = $sourceBranch
    source_commit = $sourceCommit
    app_version = $appVersion
    deployment_profile = "internal_managed"
    included_paths = $include
} | ConvertTo-Json -Depth 4

Set-Content -Path (Join-Path $staging "package-manifest.json") -Value $manifest -Encoding UTF8

if (Test-Path $zipPath) {
    Remove-Item -Force $zipPath
}
Compress-Archive -Path (Join-Path $staging '*') -DestinationPath $zipPath -CompressionLevel Optimal

$sha256 = (Get-FileHash -Path $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
$checksumPath = "$zipPath.sha256"
$checksumBody = "$sha256  $([System.IO.Path]::GetFileName($zipPath))"
Set-Content -Path $checksumPath -Value $checksumBody -Encoding ASCII

Write-Host "Created runtime package: $zipPath"
Write-Host "Created checksum: $checksumPath"
