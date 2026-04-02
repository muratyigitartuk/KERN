# KERN HF Adapter Serving

This is KERN's **reference-quality model serving path** when you want the deployed runtime to stay closest to the fine-tuned behavior validated in the HF/PEFT stack.

## When to use this

Use HF adapter serving when:

- you want the closest match to the remote fine-tune and live validation results
- you are evaluating the actual tuned model quality
- you do not want GGUF merge/quantization drift to hide whether the model itself is good

Do **not** confuse this with the lighter `llama-server` GGUF path:

- `HF adapter serving` = better fidelity
- `merged GGUF` = better deployment simplicity later

## Current product decision

For now:

- **reference path:** HF adapter serving
- **later optimization:** merged tuned model -> GGUF -> quantized local artifact

## Install

Install KERN with the optional HF adapter stack:

```powershell
.\scripts\install-kern.ps1 -InternalDeploy -IncludeHfAdapter
```

Or on an existing install:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[hf_adapter]"
```

## Required environment values

KERN itself still talks to an OpenAI-compatible local endpoint through `KERN_LLAMA_SERVER_URL`. The HF adapter server simply becomes the local endpoint on that port.

Recommended values:

```powershell
KERN_LLM_ENABLED=true
KERN_LLM_LOCAL_ONLY=true
KERN_LLAMA_SERVER_URL=http://127.0.0.1:8080
KERN_LLM_MODEL=KERN-qwen
KERN_HF_ADAPTER_MODEL=Qwen/Qwen2.5-14B-Instruct
KERN_HF_ADAPTER_PATH=C:\path\to\qwen25-14b-kern-lora-v3-repair2
KERN_HF_ADAPTER_ALIAS=KERN-qwen
```

Optional:

```powershell
KERN_HF_ADAPTER_TRUST_REMOTE_CODE=true
KERN_HF_ADAPTER_LOAD_IN_4BIT=true
KERN_HF_ADAPTER_DEVICE_MAP=auto
```

`KERN_HF_ADAPTER_DEVICE_MAP` is passed through to Transformers `device_map`.

Useful values:

- `auto`: default, tries GPU plus CPU/disk offload
- `cpu`: force a no-offload CPU load for debugging
- `cuda`: force the whole model onto GPU 0
- `none`: do not pass a device map

## Start the server

```powershell
.\scripts\run-kern-hf-adapter-server.ps1
```

Then verify:

```powershell
Invoke-WebRequest http://127.0.0.1:8080/health -UseBasicParsing
Invoke-WebRequest http://127.0.0.1:8080/v1/models -UseBasicParsing
```

The model alias should be `KERN-qwen` or whatever alias you configured.

## Known Windows failure on this project

On `2026-03-28`, the local Windows test host hit a real failure with:

- `Qwen/Qwen2.5-14B-Instruct`
- local PEFT adapter load
- `device_map=auto`
- PyTorch/PEFT offloading parts of the model to CPU and disk

Observed crash:

```text
KeyError: 'base_model.model.model.model.layers.18.mlp.down_proj'
```

That failure happened after the full base model finished downloading and while `PeftModel.from_pretrained(...)` was attaching the adapter.

Practical meaning:

- `8000 /health -> llm: ok` is not enough to prove the HF adapter path is active
- if `8080 /v1/models` is unreachable, the HF adapter server is not the backend you are testing
- on Windows, `device_map=auto` is not yet a validated reference path for this model on this project

If you need one more local debug attempt, try:

```powershell
$env:KERN_HF_ADAPTER_DEVICE_MAP = "cpu"
.\scripts\run-kern-hf-adapter-server.ps1
```

This avoids the broken offload path, but it is slow and may still require more free system RAM than the machine has available.

## Then start KERN

```powershell
Start-ScheduledTask -TaskName "KERN Local Runtime"
Start-Sleep -Seconds 5
Invoke-WebRequest http://127.0.0.1:8000/health -UseBasicParsing
```

Pass condition:

- `/health` shows `llm: ok`

## Important boundary

This path is the **quality reference**, not yet the simplest operator deployment:

- it has a heavier runtime than merged GGUF serving
- Windows-native GPU compatibility depends on the exact local PyTorch / backend stack
- for a future KERN Box, Linux remains the cleaner long-term host for this path

## Evidence-backed serving ranking

This ranking is the current KERN decision order, based on repo results plus primary-source serving guidance.

### 1. Linux HF adapter serving

Use this as the current **reference-quality truth**.

Why it ranks first:

- the adapter was trained and validated in the HF/PEFT stack
- PEFT checkpoints require the original base model at load time, so this path is the most faithful to what was actually trained
- Qwen explicitly recommends `vLLM` for Qwen deployment, and the Qwen2.5 model card also points to vLLM for deployment, especially when you care about long-context serving

Practical meaning for KERN:

- if you need the closest reproduction of the tuned model, use a Linux host with an HF-native serving stack first
- this is the best place to judge whether the model is good or bad

### 2. Merged tuned model -> GGUF -> quantized local artifact

Use this as the current **best deployment candidate** for a local appliance-style KERN product.

Why it ranks second:

- PEFT docs explicitly describe `merge_and_unload()` as the straightforward way to store the whole PEFT model
- a merged model is larger than the adapter but should infer a bit faster
- this avoids runtime adapter attachment and removes the need to ship a separate base model plus adapter pair
- it fits the KERN Box story better than a heavyweight HF adapter runtime

Risk:

- once merged, you lose PEFT-specific flexibility
- PEFT notes that not every setting supports merging cleanly, especially around some quantized paths
- quantization drift can still move behavior away from the HF reference, so merged artifacts must be judged against the HF reference, not against intuition

### 3. vLLM LoRA serving on Linux

Use this as a strong **adapter-serving production candidate** if we want to stay adapter-native rather than merge immediately.

Why it matters:

- Qwen recommends trying vLLM for deployment
- vLLM can serve LoRA adapters through its OpenAI-compatible server
- vLLM can expose base and LoRA adapters as separate model IDs and process LoRA requests under one server-wide configuration

Important boundary:

- dynamic LoRA loading exists, but vLLM warns that runtime LoRA updating has security risks and should not be used in production unless the environment is isolated and fully trusted

Practical meaning for KERN:

- static vLLM LoRA serving on Linux is credible
- dynamic adapter hot-loading is not the default production posture for KERN

### 4. Windows HF adapter serving

Use this as **experimental only** for now.

Why it ranks low:

- repo evidence already shows a concrete local failure on Windows with `device_map=auto`
- Accelerate documents that `device_map=\"auto\"` fills GPU first, then CPU, then disk, and that this is an inference offload path rather than a clean single-device load
- that offload behavior is exactly the environment in which the local PEFT load failed on this machine

Practical meaning for KERN:

- Windows can still be the app/package validation machine
- it is not currently the trusted HF reference host for this 14B adapter path

### 5. Runtime LoRA on GGUF

Use this as **experimental only**, not as a truth path.

Why it ranks last:

- local repo evidence showed materially weaker behavior than the remote HF/PEFT result
- Qwen's local `llama.cpp` guidance is about quantization and local inference, not about claiming parity with HF adapter behavior
- this path is good for proving a local model can run, but not for proving the tuned model kept its behavior

## Morning recommendation

Treat the paths like this:

- **quality truth now:** Linux HF-native adapter serving
- **best deployable product artifact next:** merged tuned model -> GGUF -> quantized serving
- **do not over-invest further:** Windows HF adapter troubleshooting and runtime GGUF LoRA quality judgment

Local update on `2026-03-28`:

- the merged `Q8_0` GGUF candidate was served successfully on this Windows machine through `llama-server.exe`
- `/v1/models` responded correctly and a tiny `/v1/chat/completions` request returned the expected string
- that does **not** replace HF-native serving as the quality reference
- it does strengthen the merged-model path as the practical local deployment candidate
- full `kern_model_eval_v2` comparison later showed the merged `Q8_0` artifact scoring `43/56 = 0.768` versus the saved HF reference `45/56 = 0.804`
- the entire observed loss was concentrated in the `refusal` category, not in drafting, tool calling, grounded answers, or summarization

## Reference host rule

When KERN needs to answer "is the tuned model actually good?", the reference host should satisfy these conditions:

- adapter-native serving, not merged or converted serving
- same base model family as training
- model endpoint verified directly before evaluation
- no quality judgment based only on app health checks
- no reliance on the Windows box if it is already showing HF offload instability

For now, that points to a Linux HF-native host first.

## Deployment host rule

When KERN needs to answer "what should we actually ship locally?", the deployable host path should satisfy these conditions:

- single model artifact or at least a stable serving bundle
- predictable operator story
- no runtime adapter surprises
- good enough fidelity against the reference host

For now, that points to the merged-model path first, with the explicit rule that merged quality must be checked against the HF-native reference.

## Sources

- Qwen2.5-14B-Instruct model card: [https://huggingface.co/Qwen/Qwen2.5-14B-Instruct](https://huggingface.co/Qwen/Qwen2.5-14B-Instruct)
- Qwen vLLM deployment guide: [https://qwen.readthedocs.io/en/v2.5/deployment/vllm.html](https://qwen.readthedocs.io/en/v2.5/deployment/vllm.html)
- Qwen llama.cpp quantization guide: [https://qwen.readthedocs.io/en/latest/quantization/llama.cpp.html](https://qwen.readthedocs.io/en/latest/quantization/llama.cpp.html)
- vLLM LoRA adapters guide: [https://docs.vllm.ai/en/latest/features/lora/](https://docs.vllm.ai/en/latest/features/lora/)
- Hugging Face Accelerate Big Model Inference: [https://huggingface.co/docs/accelerate/en/usage_guides/big_modeling](https://huggingface.co/docs/accelerate/en/usage_guides/big_modeling)
- Hugging Face PEFT checkpoint format: [https://huggingface.co/docs/peft/developer_guides/checkpoint](https://huggingface.co/docs/peft/developer_guides/checkpoint)

## Deployment rule

If a merged GGUF candidate ever disagrees materially with HF adapter serving, trust the HF adapter result first and treat the merged artifact as the thing that needs more work.
