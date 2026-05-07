param(
    [switch]$SkipToolInstall,
    [switch]$SkipPythonInstall,
    [switch]$NoStart,
    [switch]$IncludeVenvForBundle,
    [switch]$EnableLlm,
    [string]$LlmModelPath = $env:KERN_LLAMA_SERVER_MODEL_PATH,
    [string]$LlamaServerBinary = $env:KERN_LLAMA_SERVER_BINARY,
    [string]$LlmModelAlias = $(if ($env:KERN_LLM_MODEL) { $env:KERN_LLM_MODEL } else { "kern-gemma4" }),
    [int]$LlmPort = 8080,
    [string]$LlamaGpuLayers = $(if ($env:KERN_LLAMA_GPU_LAYERS) { $env:KERN_LLAMA_GPU_LAYERS } else { "auto" }),
    [switch]$AllowCpuLlm,
    [switch]$SkipLlamaBuild
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Test-Command {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Add-CargoToPath {
    $cargoBin = Join-Path $env:USERPROFILE ".cargo\bin"
    if ((Test-Path $cargoBin) -and ($env:Path -notlike "*$cargoBin*")) {
        $env:Path = "$cargoBin;$env:Path"
    }
}

function Invoke-Checked {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory = (Get-Location).Path
    )
    Push-Location $WorkingDirectory
    try {
        & $FilePath @Arguments | ForEach-Object { Write-Host $_ }
        if ($LASTEXITCODE -ne 0) {
            throw "$FilePath exited with code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}

function Add-KernCommandPath {
    param([string]$RepoRoot)

    $shimRoot = Join-Path $env:USERPROFILE "bin"
    New-Item -ItemType Directory -Force -Path $shimRoot | Out-Null
    $kernShim = Join-Path $shimRoot "kern.cmd"
    $startShim = Join-Path $shimRoot "start-kern.cmd"
    $installShim = Join-Path $shimRoot "install-kern.cmd"
    $kernCommand = "@echo off`r`ncall `"$RepoRoot\kern.cmd`" %*`r`n"
    $startCommand = "@echo off`r`ncall `"$RepoRoot\start-kern.cmd`" %*`r`n"
    $installCommand = "@echo off`r`ncall `"$RepoRoot\install-kern.cmd`" %*`r`n"
    Set-Content -Path $kernShim -Value $kernCommand -Encoding ASCII
    Set-Content -Path $startShim -Value $startCommand -Encoding ASCII
    Set-Content -Path $installShim -Value $installCommand -Encoding ASCII

    $currentUserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $entries = @()
    if ($currentUserPath) {
        $entries = $currentUserPath -split ";" | Where-Object { $_.Trim() }
    }
    $alreadyPresent = $entries | Where-Object {
        $entry = $_
        try {
            (Resolve-Path $entry -ErrorAction Stop).Path -ieq $shimRoot
        }
        catch {
            $entry.TrimEnd("\") -ieq $shimRoot.TrimEnd("\")
        }
    }
    if (-not $alreadyPresent) {
        $updated = if ($currentUserPath) { "$currentUserPath;$shimRoot" } else { $shimRoot }
        [Environment]::SetEnvironmentVariable("Path", $updated, "User")
        $env:Path = "$shimRoot;$env:Path"
    }
    Write-Host "KERN commands installed. New Windows Terminal windows can run: kern" -ForegroundColor Green
}

function Install-WithWinget {
    param(
        [string]$PackageId,
        [string[]]$ExtraArgs = @()
    )
    if (-not (Test-Command "winget")) {
        throw "winget is required to install $PackageId automatically. Install it or rerun with -SkipToolInstall after installing prerequisites."
    }
    $args = @(
        "install",
        "--id", $PackageId,
        "-e",
        "--accept-package-agreements",
        "--accept-source-agreements"
    ) + $ExtraArgs
    Invoke-Checked "winget" $args
}

function Initialize-MsvcEnvironment {
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (-not (Test-Path $vswhere)) {
        return $false
    }

    $installationPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if (-not $installationPath) {
        return $false
    }

    $vcvars = Join-Path $installationPath "VC\Auxiliary\Build\vcvars64.bat"
    if (-not (Test-Path $vcvars)) {
        return $false
    }

    $envDump = cmd /c "`"$vcvars`" >nul && set"
    foreach ($line in $envDump) {
        $idx = $line.IndexOf("=")
        if ($idx -gt 0) {
            [Environment]::SetEnvironmentVariable($line.Substring(0, $idx), $line.Substring($idx + 1), "Process")
        }
    }
    return $true
}

function Find-FreeTcpPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Parse("127.0.0.1"), 0)
    try {
        $listener.Start()
        return $listener.LocalEndpoint.Port
    }
    finally {
        $listener.Stop()
    }
}

function Test-HttpReady {
    param(
        [string]$Url,
        [int]$TimeoutSec = 3
    )
    try {
        $response = Invoke-WebRequest -Uri $Url -TimeoutSec $TimeoutSec -UseBasicParsing
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Get-LocalGpuSummary {
    try {
        $controllers = Get-CimInstance Win32_VideoController -ErrorAction Stop |
            Where-Object { $_.Name -and $_.Name -notmatch "Microsoft Basic Display|Remote Display" } |
            Select-Object -ExpandProperty Name
        return @($controllers)
    }
    catch {
        return @()
    }
}

function Resolve-GgufModel {
    param(
        [string]$RepoRoot,
        [string]$RequestedPath
    )

    $candidates = @()
    if ($RequestedPath) {
        $candidates += $RequestedPath
    }
    if ($env:KERN_MODEL_DIR) {
        $candidates += $env:KERN_MODEL_DIR
    }
    $candidates += @(
        (Join-Path $RepoRoot "models"),
        (Join-Path $env:USERPROFILE "Models"),
        (Join-Path $env:USERPROFILE ".cache\kern\models")
    )

    foreach ($candidate in $candidates) {
        if (-not $candidate -or -not (Test-Path $candidate)) {
            continue
        }
        $item = Get-Item $candidate
        if ($item.PSIsContainer) {
            $model = Get-ChildItem -LiteralPath $item.FullName -File -Filter "*.gguf" -ErrorAction SilentlyContinue |
                Sort-Object Length -Descending |
                Select-Object -First 1
            if ($model) {
                return $model.FullName
            }
        }
        elseif ($item.Extension -ieq ".gguf") {
            return $item.FullName
        }
    }

    $modelHelp = @"
No GGUF model was found.

Put a GGUF model in one of these locations, then rerun this command:
  - $RepoRoot\models
  - $env:USERPROFILE\Models
  - $env:USERPROFILE\.cache\kern\models

Or pass the model explicitly:
  .\scripts\start-kern.ps1 -EnableLlm -LlmModelPath "C:\path\to\model.gguf"

KERN uses llama.cpp's OpenAI-compatible local server. Gemma-family GGUF files are supported when your llama.cpp build is current enough.
"@
    throw $modelHelp.Trim()
}

function Ensure-VulkanSdk {
    if ($env:VULKAN_SDK -and (Test-Path $env:VULKAN_SDK)) {
        return
    }
    $sdkRoot = Get-ChildItem "C:\VulkanSDK" -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        Select-Object -First 1
    if ($sdkRoot) {
        $env:VULKAN_SDK = $sdkRoot.FullName
        $env:PATH = "$($sdkRoot.FullName)\Bin;$env:PATH"
        return
    }
    if ($SkipToolInstall) {
        throw "Vulkan SDK is required for the default GPU llama.cpp build. Install Khronos Vulkan SDK or rerun without -SkipToolInstall."
    }
    Write-Step "Installing Vulkan SDK for GPU inference"
    Install-WithWinget "KhronosGroup.VulkanSDK"
    $sdkRoot = Get-ChildItem "C:\VulkanSDK" -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        Select-Object -First 1
    if (-not $sdkRoot) {
        throw "Vulkan SDK installation finished but C:\VulkanSDK was not found. Open a new PowerShell session and rerun."
    }
    $env:VULKAN_SDK = $sdkRoot.FullName
    $env:PATH = "$($sdkRoot.FullName)\Bin;$env:PATH"
}

function Ensure-Git {
    if (Test-Command "git") {
        return
    }
    if ($SkipToolInstall) {
        throw "Git is required to fetch llama.cpp. Install Git or rerun without -SkipToolInstall."
    }
    Write-Step "Installing Git"
    Install-WithWinget "Git.Git"
    if (-not (Test-Command "git")) {
        throw "Git was installed but is not available in this PowerShell session. Open a new session and rerun."
    }
}

function Ensure-RustAndTauri {
    Add-CargoToPath

    if (-not (Test-Command "rustup")) {
        Write-Step "Installing Rust"
        Install-WithWinget "Rustlang.Rustup"
    }

    Add-CargoToPath

    Invoke-Checked "rustup" @("default", "stable")

    if (-not (Initialize-MsvcEnvironment)) {
        Write-Step "Installing Visual Studio C++ Build Tools"
        Install-WithWinget "Microsoft.VisualStudio.2022.BuildTools" @(
            "--override",
            "--quiet --wait --norestart --add Microsoft.VisualStudio.Workload.VCTools --add Microsoft.VisualStudio.Component.VC.CMake.Project --includeRecommended"
        )
        if (-not (Initialize-MsvcEnvironment)) {
            throw "Visual Studio C++ build tools were installed, but vcvars64.bat could not be loaded. Open a new PowerShell session and rerun this script."
        }
    }

    $tauriInstalled = $false
    try {
        $tauriVersion = & cargo tauri --version 2>$null
        $tauriInstalled = $LASTEXITCODE -eq 0 -and $tauriVersion
    }
    catch {
        $tauriInstalled = $false
    }
    if (-not $tauriInstalled) {
        Write-Step "Installing Tauri CLI"
        Invoke-Checked "cargo" @("install", "tauri-cli", "--version", "^2")
    }
}

function Resolve-LlamaServerBinary {
    param(
        [string]$RepoRoot,
        [string]$RequestedPath
    )

    if ($RequestedPath) {
        if (-not (Test-Path $RequestedPath)) {
            throw "KERN_LLAMA_SERVER_BINARY does not exist: $RequestedPath"
        }
        return (Resolve-Path $RequestedPath).Path
    }

    $gpuCandidates = @(
        (Join-Path $env:USERPROFILE "Desktop\tools\llama.cpp-fresh\build-kern-vulkan\bin\llama-server.exe"),
        (Join-Path $RepoRoot "tools\llama.cpp\build-kern-vulkan\bin\llama-server.exe"),
        (Join-Path $RepoRoot "tools\llama.cpp\build\bin\llama-server.exe")
    )
    foreach ($candidate in $gpuCandidates) {
        if (Test-Path $candidate) {
            return (Resolve-Path $candidate).Path
        }
    }

    $command = Get-Command "llama-server" -ErrorAction SilentlyContinue
    if ($command -and $command.Source -match "(?i)vulkan|cuda|hip|rocm") {
        return $command.Source
    }

    if (-not $SkipLlamaBuild -and -not $AllowCpuLlm) {
        Ensure-Git
        Ensure-VulkanSdk
        Write-Step "Building fresh GPU llama.cpp server"
        Invoke-Checked "powershell.exe" @(
            "-ExecutionPolicy", "Bypass",
            "-File", (Join-Path $RepoRoot "scripts\build-llama-cpp.ps1"),
            "-Vulkan"
        ) $RepoRoot
        foreach ($candidate in $gpuCandidates) {
            if (Test-Path $candidate) {
                return (Resolve-Path $candidate).Path
            }
        }
    }

    if (-not $AllowCpuLlm) {
        throw "No GPU-enabled llama-server was found. Rerun without -SkipLlamaBuild, build with scripts\build-llama-cpp.ps1 -Vulkan, or set KERN_LLAMA_SERVER_BINARY."
    }

    $cpuCandidates = @(
        (Join-Path $env:USERPROFILE "Desktop\tools\llama.cpp-fresh\build-kern-cpu\bin\llama-server.exe"),
        (Join-Path $RepoRoot "tools\llama.cpp\build-kern-cpu\bin\llama-server.exe")
    )
    foreach ($candidate in $cpuCandidates) {
        if (Test-Path $candidate) {
            return (Resolve-Path $candidate).Path
        }
    }
    if ($command) {
        return $command.Source
    }

    throw "Could not find llama-server.exe."
}

function Ensure-PythonEnvironment {
    param([string]$RepoRoot)

    $venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        Write-Step "Creating Python virtual environment"
        Invoke-Checked "python" @("-m", "venv", ".venv") $RepoRoot
    }

    Write-Step "Installing KERN Python dependencies"
    Invoke-Checked $venvPython @("-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel") $RepoRoot
    Invoke-Checked $venvPython @("-m", "pip", "install", "-e", ".[dev,documents,scheduler,system_control]") $RepoRoot

    return $venvPython
}

function Start-LlamaServerIfRequested {
    param(
        [string]$RepoRoot,
        [string]$ModelPath,
        [string]$BinaryPath,
        [string]$Alias,
        [int]$Port
    )

    if (-not $EnableLlm) {
        $env:KERN_LLM_ENABLED = "false"
        return
    }

    $ModelPath = Resolve-GgufModel -RepoRoot $RepoRoot -RequestedPath $ModelPath
    $BinaryPath = Resolve-LlamaServerBinary -RepoRoot $RepoRoot -RequestedPath $BinaryPath

    $gpuSummary = Get-LocalGpuSummary
    if ($gpuSummary.Count -gt 0) {
        Write-Host "Detected GPU: $($gpuSummary -join ', ')" -ForegroundColor DarkCyan
    }

    $modelsUrl = "http://127.0.0.1:$Port/v1/models"
    $serverReady = Test-HttpReady $modelsUrl
    if (-not $serverReady) {
        $portBusy = Test-NetConnection -ComputerName "127.0.0.1" -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue
        if ($portBusy) {
            $newPort = Find-FreeTcpPort
            Write-Host "Port $Port is busy and not serving llama.cpp; using LLM port $newPort instead." -ForegroundColor Yellow
            $Port = $newPort
            $modelsUrl = "http://127.0.0.1:$Port/v1/models"
        }
    }

    if (-not $serverReady) {
        Write-Step "Starting local llama-server"
        $llamaScript = Join-Path $RepoRoot "scripts\run-llama-server.ps1"
        $llmLogRoot = if ($env:KERN_DESKTOP_LOG_ROOT) { $env:KERN_DESKTOP_LOG_ROOT } else { Join-Path $RepoRoot ".kern-desktop\logs" }
        New-Item -ItemType Directory -Force -Path $llmLogRoot | Out-Null
        $llamaOut = Join-Path $llmLogRoot "llama-server.out.log"
        $llamaErr = Join-Path $llmLogRoot "llama-server.err.log"
        $args = @(
            "-ExecutionPolicy", "Bypass",
            "-File", $llamaScript,
            "-ModelPath", $ModelPath,
            "-Alias", $Alias,
            "-Port", [string]$Port,
            "-GpuLayers", $LlamaGpuLayers
        )
        if ($BinaryPath) {
            $args += @("-BinaryPath", $BinaryPath)
        }
        if ($AllowCpuLlm) {
            $args += @("-AllowCpuFallback")
        }
        Start-Process -FilePath "powershell.exe" -ArgumentList $args -WindowStyle Hidden -RedirectStandardOutput $llamaOut -RedirectStandardError $llamaErr | Out-Null

        $deadline = (Get-Date).AddSeconds(180)
        do {
            Start-Sleep -Seconds 1
            try {
                $response = Invoke-WebRequest -Uri $modelsUrl -TimeoutSec 3 -UseBasicParsing
                $serverReady = $response.StatusCode -eq 200
            }
            catch {
                $serverReady = $false
            }
        } while (-not $serverReady -and (Get-Date) -lt $deadline)

        if (-not $serverReady) {
            throw "llama-server did not become ready at $modelsUrl within 180 seconds. See $llamaErr"
        }
    }

    $env:KERN_LLM_ENABLED = "true"
    $env:KERN_LLM_LOCAL_ONLY = "true"
    $env:KERN_LLAMA_SERVER_URL = "http://127.0.0.1:$Port"
    $env:KERN_LLAMA_SERVER_MODEL_PATH = (Resolve-Path $ModelPath).Path
    $env:KERN_LLM_MODEL = $Alias
    $env:KERN_LLAMA_GPU_LAYERS = $LlamaGpuLayers
    if ($BinaryPath) {
        $env:KERN_LLAMA_SERVER_BINARY = (Resolve-Path $BinaryPath).Path
    }
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir "..")).Path

Write-Step "Preparing KERN from $repoRoot"
Add-KernCommandPath $repoRoot

if (-not $SkipToolInstall) {
    Ensure-RustAndTauri
}
else {
    Add-CargoToPath
    if (-not (Initialize-MsvcEnvironment)) {
        Write-Host "MSVC environment was not loaded. If Tauri fails to link, rerun without -SkipToolInstall." -ForegroundColor Yellow
    }
}

if (-not $SkipPythonInstall) {
    $python = Ensure-PythonEnvironment $repoRoot
}
else {
    $python = Join-Path $repoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) {
        $python = "python"
    }
}

Start-LlamaServerIfRequested -RepoRoot $repoRoot -ModelPath $LlmModelPath -BinaryPath $LlamaServerBinary -Alias $LlmModelAlias -Port $LlmPort

Write-Step "Preparing desktop runtime payload"
$packageArgs = @("-ExecutionPolicy", "Bypass", "-File", (Join-Path $repoRoot "scripts\package-tauri-runtime.ps1"))
if ($IncludeVenvForBundle) {
    $packageArgs += "-IncludeVenv"
}
Invoke-Checked "powershell.exe" $packageArgs $repoRoot

if ($NoStart) {
    Write-Host "KERN is installed and ready. Start it with: kern" -ForegroundColor Green
    exit 0
}

Write-Step "Starting KERN Tauri desktop"
$env:KERN_DESKTOP_RUNTIME_ROOT = $repoRoot
$env:KERN_DESKTOP_PYTHON = $python
$env:KERN_DESKTOP_MODE = "true"
$env:KERN_PRODUCT_POSTURE = "production"
$env:KERN_DISABLE_AUTH_FOR_LOOPBACK = "true"

Invoke-Checked "powershell.exe" @(
    "-ExecutionPolicy", "Bypass",
    "-File", (Join-Path $repoRoot "scripts\start-kern-desktop.ps1"),
    "-RuntimeRoot", $repoRoot,
    "-Python", $python
) $repoRoot
