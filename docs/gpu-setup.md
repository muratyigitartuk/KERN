# GPU Setup

KERN uses llama.cpp through its OpenAI-compatible local server. The default Windows GPU path is a fresh Vulkan build of llama.cpp, because Vulkan works across AMD, NVIDIA, and Intel GPUs when the driver supports it.

## Automatic Path

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-kern.ps1 -EnableLlm
```

If no GPU llama.cpp build is found, the launcher checks or installs Git, Rust/Tauri, Visual Studio C++ Build Tools with CMake, Vulkan SDK, and a fresh Vulkan `llama-server.exe`.

## Manual Build

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build-llama-cpp.ps1 -Vulkan
```

## CPU Fallback

CPU inference is only for diagnostics:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-kern.ps1 -EnableLlm -AllowCpuLlm
```

Do not use CPU fallback for normal desktop release validation.

## Verification

The llama.cpp log should mention Vulkan devices and offloaded layers.
