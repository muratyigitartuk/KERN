param(
    [string]$OutputRoot = "desktop-runtime",
    [switch]$IncludeVenv
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir "..")
$outputPath = Join-Path $repoRoot $OutputRoot

$include = @(
    "app",
    "scripts\run-kern.ps1",
    "scripts\start-kern.ps1",
    "scripts\run-hf-adapter-server.py",
    "scripts\run-kern-hf-adapter-server.ps1",
    "scripts\run-llama-server.ps1",
    "scripts\build-llama-cpp.ps1",
    "scripts\restore-kern.py",
    "scripts\preflight-kern.py",
    "scripts\validate-publish-hygiene.py",
    "prompts",
    "docs\quickstart.md",
    "docs\gpu-setup.md",
    "docs\model-setup.md",
    "docs\architecture.md",
    "docs\troubleshooting-guide.md",
    "LICENSE",
    "NOTICE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "pyproject.toml",
    "README.md"
)

if ((Test-Path $outputPath) -and -not $IncludeVenv) {
    Remove-Item -LiteralPath $outputPath -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $outputPath | Out-Null

foreach ($item in $include) {
    $source = Join-Path $repoRoot $item
    if (-not (Test-Path $source)) {
        throw "Missing desktop runtime source: $item"
    }
    $target = Join-Path $outputPath $item
    $targetParent = Split-Path -Parent $target
    if (-not (Test-Path $targetParent)) {
        New-Item -ItemType Directory -Force -Path $targetParent | Out-Null
    }
    if (Test-Path $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
    if ((Get-Item $source) -is [System.IO.DirectoryInfo]) {
        Copy-Item -LiteralPath $source -Destination $target -Recurse -Force
    }
    else {
        Copy-Item -LiteralPath $source -Destination $target -Force
    }
}

if ($IncludeVenv) {
    $venvPath = Join-Path $repoRoot ".venv"
    if (-not (Test-Path $venvPath)) {
        throw "Cannot include virtual environment because .venv does not exist."
    }
    $targetVenv = Join-Path $outputPath ".venv"
    New-Item -ItemType Directory -Force -Path $targetVenv | Out-Null
    robocopy $venvPath $targetVenv /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -gt 7) {
        throw "Failed to mirror .venv into desktop runtime. robocopy exit code: $LASTEXITCODE"
    }
}

$generated = @(
    "__pycache__",
    ".pytest_cache",
    "kern.egg-info"
)
foreach ($name in $generated) {
    Get-ChildItem -Path $outputPath -Directory -Recurse -Force -Filter $name -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notlike "$(Join-Path $outputPath '.venv')*" } |
        Remove-Item -Recurse -Force
}

Get-ChildItem -Path $outputPath -Recurse -File -Include "*.pyc", "*.pyo", "*.log", "*.db", "*.key", ".env" -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -notlike "$(Join-Path $outputPath '.venv')*" } |
    Remove-Item -Force

$manifest = @{
    package_type = "kern_tauri_desktop_runtime"
    created_at = (Get-Date).ToString("o")
    include_venv = [bool]$IncludeVenv
    included_paths = $include
} | ConvertTo-Json -Depth 4

Set-Content -Path (Join-Path $outputPath "desktop-runtime-manifest.json") -Value $manifest -Encoding UTF8

Write-Host "Prepared Tauri desktop runtime at $outputPath"
