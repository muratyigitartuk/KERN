param(
    [switch]$EnableLlm,
    [string]$LlmModelPath = $env:KERN_LLAMA_SERVER_MODEL_PATH,
    [string]$LlamaServerBinary = $env:KERN_LLAMA_SERVER_BINARY,
    [string]$LlmModelAlias = $(if ($env:KERN_LLM_MODEL) { $env:KERN_LLM_MODEL } else { "kern-gemma4" }),
    [int]$LlmPort = 8080,
    [string]$LlamaGpuLayers = $(if ($env:KERN_LLAMA_GPU_LAYERS) { $env:KERN_LLAMA_GPU_LAYERS } else { "auto" }),
    [switch]$AllowCpuLlm,
    [switch]$SkipToolInstall,
    [switch]$SkipPythonInstall,
    [switch]$SkipLlamaBuild,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$installer = Join-Path $scriptDir "install-and-start-kern-tauri.ps1"

$args = @(
    "-ExecutionPolicy", "Bypass",
    "-File", $installer
)

if ($EnableLlm) { $args += "-EnableLlm" }
if ($LlmModelPath) { $args += @("-LlmModelPath", $LlmModelPath) }
if ($LlamaServerBinary) { $args += @("-LlamaServerBinary", $LlamaServerBinary) }
if ($LlmModelAlias) { $args += @("-LlmModelAlias", $LlmModelAlias) }
if ($LlmPort) { $args += @("-LlmPort", [string]$LlmPort) }
if ($LlamaGpuLayers) { $args += @("-LlamaGpuLayers", $LlamaGpuLayers) }
if ($AllowCpuLlm) { $args += "-AllowCpuLlm" }
if ($SkipToolInstall) { $args += "-SkipToolInstall" }
if ($SkipPythonInstall) { $args += "-SkipPythonInstall" }
if ($SkipLlamaBuild) { $args += "-SkipLlamaBuild" }
if ($NoStart) { $args += "-NoStart" }

& powershell.exe @args
exit $LASTEXITCODE
