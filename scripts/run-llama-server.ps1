param(
    [string]$BinaryPath = $env:KERN_LLAMA_SERVER_BINARY,
    [string]$ModelPath = $env:KERN_LLAMA_SERVER_MODEL_PATH,
    [string]$LoraPath = $env:KERN_LLAMA_SERVER_LORA_PATH,
    [string]$Alias = $env:KERN_LLM_MODEL,
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8080,
    [string]$ContextSize = $(if ($env:KERN_LLM_CONTEXT_WINDOW) { $env:KERN_LLM_CONTEXT_WINDOW } else { "8192" }),
    [string]$GpuLayers = $(if ($env:KERN_LLAMA_GPU_LAYERS) { $env:KERN_LLAMA_GPU_LAYERS } else { "auto" }),
    [switch]$AllowCpuFallback
)

$ErrorActionPreference = "Stop"

function Resolve-LlamaServerBinary {
    param([string]$RequestedPath)

    if ($RequestedPath) {
        if (-not (Test-Path $RequestedPath)) {
            throw "KERN_LLAMA_SERVER_BINARY does not exist: $RequestedPath"
        }
        return (Resolve-Path $RequestedPath).Path
    }

    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    $gpuCandidates = @(
        (Join-Path $env:USERPROFILE "Desktop\tools\llama.cpp-fresh\build-kern-vulkan\bin\llama-server.exe"),
        (Join-Path $repoRoot "tools\llama.cpp\build-kern-vulkan\bin\llama-server.exe"),
        (Join-Path $repoRoot "tools\llama.cpp\build\bin\llama-server.exe")
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

    if (-not $AllowCpuFallback) {
        throw "No GPU-enabled llama-server was found. Build Vulkan with scripts\build-llama-cpp.ps1 -Vulkan, set KERN_LLAMA_SERVER_BINARY to a GPU build, or pass -AllowCpuFallback explicitly."
    }

    $cpuCandidates = @(
        (Join-Path $env:USERPROFILE "Desktop\tools\llama.cpp-fresh\build-kern-cpu\bin\llama-server.exe"),
        (Join-Path $repoRoot "tools\llama.cpp\build-kern-cpu\bin\llama-server.exe")
    )
    foreach ($candidate in $cpuCandidates) {
        if (Test-Path $candidate) {
            return (Resolve-Path $candidate).Path
        }
    }
    if ($command) {
        return $command.Source
    }
    $wingetBinary = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter "llama-server.exe" -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty FullName -First 1
    if ($wingetBinary) {
        return $wingetBinary
    }

    throw "Could not find llama-server.exe. Install llama.cpp or set KERN_LLAMA_SERVER_BINARY."
}

function Resolve-GgufModelPath {
    param([string]$RequestedPath)

    if (-not $RequestedPath) {
        throw "Set KERN_LLAMA_SERVER_MODEL_PATH to a GGUF file or folder."
    }

    if (-not (Test-Path $RequestedPath)) {
        throw "Model path does not exist: $RequestedPath"
    }

    $item = Get-Item $RequestedPath
    if ($item.PSIsContainer) {
        $gguf = Get-ChildItem $item.FullName -File -Filter "*.gguf" | Sort-Object Length -Descending | Select-Object -First 1
        if (-not $gguf) {
            throw "No GGUF file found under: $RequestedPath"
        }
        return $gguf.FullName
    }

    if ($item.Extension -ne ".gguf") {
        throw "Model path must point to a .gguf file or folder containing one: $RequestedPath"
    }

    return $item.FullName
}

function Resolve-OptionalGgufPath {
    param([string]$RequestedPath)

    if (-not $RequestedPath) {
        return $null
    }

    if (-not (Test-Path $RequestedPath)) {
        throw "LoRA path does not exist: $RequestedPath"
    }

    $item = Get-Item $RequestedPath
    if ($item.PSIsContainer) {
        $gguf = Get-ChildItem $item.FullName -File -Filter "*.gguf" | Sort-Object Length -Descending | Select-Object -First 1
        if (-not $gguf) {
            throw "No GGUF LoRA file found under: $RequestedPath"
        }
        return $gguf.FullName
    }

    if ($item.Extension -ne ".gguf") {
        throw "LoRA path must point to a .gguf file or folder containing one: $RequestedPath"
    }

    return $item.FullName
}

$serverBinary = Resolve-LlamaServerBinary $BinaryPath
$resolvedModel = Resolve-GgufModelPath $ModelPath
$resolvedLora = Resolve-OptionalGgufPath $LoraPath

if (-not $AllowCpuFallback -and $serverBinary -match "(?i)build-kern-cpu") {
    throw "Refusing to start CPU llama-server without -AllowCpuFallback: $serverBinary"
}

$arguments = @(
    "--host", $BindHost,
    "--port", [string]$Port,
    "--model", $resolvedModel,
    "--ctx-size", [string]$ContextSize,
    "--gpu-layers", [string]$GpuLayers
)

if ($Alias) {
    $arguments += @("--alias", $Alias)
}

if ($resolvedLora) {
    $arguments += @("--lora", $resolvedLora)
}

& $serverBinary @arguments
exit $LASTEXITCODE
