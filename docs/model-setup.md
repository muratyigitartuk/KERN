# Model Setup

KERN expects a GGUF model served by llama.cpp.

## Recommended Layout

```text
models\
  your-model.gguf
```

The launcher selects the largest `.gguf` file in the model directory.

## Explicit Model Path

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-kern.ps1 -EnableLlm -LlmModelPath "C:\path\to\model.gguf"
```

## Gemma-Family Models

Use a recent llama.cpp build for newer Gemma-family GGUF files. The KERN launcher can build a fresh Vulkan llama.cpp server automatically when `-EnableLlm` is used and no GPU build exists.

## Model Hygiene

Do not commit models. The repository ignores `models\`, and `.gguf` files are blocked by publish-hygiene validation.
