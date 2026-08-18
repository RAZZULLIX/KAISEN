# Model compatibility

KAISEN is not tied to any single model family.  This guide documents what
works with what, and how to point KAISEN at other models.

## How templating works

| Server type | Who applies the chat template | Notes |
|---|---|---|
| `"openai"` (any OpenAI-compatible `/v1/chat/completions`) | **server-side** | Nothing to configure — the server knows its model. This is the recommended path for hosted APIs (OpenAI, Together, Groq, Fireworks, local llama.cpp with `--jinja`, vLLM, ollama, etc.). |
| `"llama"` (raw `/completion`) | **client-side** | KAISEN renders the model's native format from the `chat_template` option. |
| `"remote"` (arbitrary JSON template) | **client-side** | Same renderer applies if you use the chat methods. |

For raw `/completion` servers, set `chat_template` on the server entry:

```json
{
  "id": "my-qwen",
  "type": "llama",
  "url": "http://127.0.0.1:8502/completion",
  "model": "Qwen2.5-Coder-7B",
  "chat_template": "auto",
  "params": { "temperature": 0.2 }
}
```

`chat_template` values: `auto` (default — inferred from the model name),
`gptoss`, `chatml`, `qwen`, `llama3`, `llama2`, `gemma`, `mistral`,
`deepseek`, `none`.

- `auto` guesses from the model name and falls back to **ChatML** for unknown
  names (the most common open format).  Existing gpt-oss-20b setups keep the
  legacy `<|start|>role<|message|>…` format unchanged.

## Field-tested models

| Family | Example models | Template | Notes |
|---|---|---|---|
| GPT-OSS | gpt-oss-20b | `gptoss` | The original target.  Raw `/completion` works with the legacy format. |
| Qwen | Qwen2.5-Coder-7B/14B/32B, Qwen3 | `chatml` (alias `qwen`) | ChatML variant; strong coding models. |
| Gemma | gemma-2, gemma-3 | `gemma` | `<start_of_turn>` turns; system folded into first user turn. |
| Llama 3 | Llama 3/3.1/3.2 | `llama3` | `<|start_header_id|>` headers, BOS once. |
| Llama 2 | Llama 2 | `llama2` | `[INST]…[/INST]`, system folded. |
| Mistral / Mixtral | mistral-7b, mixtral-8x7b, Devstral | `mistral` (v0.2) / `chatml` (v0.3+) | v0.3+ uses ChatML. |
| DeepSeek | DeepSeek-V3, DeepSeek-R1 | `deepseek` | `<｜begin▁of▁sentence｜>` native template. |
| OpenAI-compatible hosted APIs | gpt-4o, claude (via proxy), groq, together | `openai` type | Server-side templating; no client work. |

## Practical notes

- **Check both the config and the server.**  A model that "can't emit a long
  file" is often a `params.n_predict` / `llm.max_tokens` cap in config.json,
  not the model.  Match the framework cap to the server's real `--ctx-size`.
- **Local llama.cpp**: run it with `--jinja` to get correct templates on
  `/v1/chat/completions` for every model; then use `"type": "openai"` with
  `"base_url": "http://127.0.0.1:8502/v1"` and KAISEN needs no template code.
- **Small models** (≤7B) struggle with large C files and cross-architecture
  intrinsics — keep baselines small and kernel-scoped.  Bigger models
  (20B+) do better on whole-file rewrites.
- **Arm vs x86**: a model trained mostly on x86 assembly will happily emit
  `immintrin.h` even on an arm64 host.  The feedback loop feeds the compiler
  error back, but a 7B model may keep trying the same broken idea — consider
  gating on target-appropriate intrinsics in the harness or using a larger
  model for SIMD-heavy targets.
