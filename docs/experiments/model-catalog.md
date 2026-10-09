# Model Catalog and Capability Matrix — Nebius Token Factory

- **Snapshot date:** 2026-10-09
- **Endpoint:** `https://api.tokenfactory.nebius.com/v1/` (OpenAI-compatible)
- **Source:** `GET /v1/models?verbose=true` with a project API key, plus live probe calls through the official `openai` Python SDK (3.27.0, SDK retries disabled).

This is a point-in-time record, not a contract. Model IDs, prices and limits change. Re-run the listing before changing `MODEL_FAST` / `MODEL_STRONG`, and update this file with a new snapshot date. Latency figures are single-machine observations from one developer workstation (small sample sizes) and are **not benchmarks**.

---

## 1. Catalog snapshot

Prices are USD per 1M tokens as reported by the API. `features` is the API's own `supported_features` field (see §3.1: it under-reports).

### NVIDIA Nemotron models

| Model ID | Context | Input $/1M | Output $/1M | Features (reported) | RPM | Region |
|---|---:|---:|---:|---|---:|---|
| `nvidia/Nemotron-3_5-Lightning` | 1,048,576 | 0.06 | 0.24 | tools, reasoning | 600 | eu-north1 |
| `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | 262,144 | 0.06 | 0.24 | tools, reasoning | 100 | eu-north1 |
| `nvidia/nemotron-3-super-120b-a12b` | 262,144 | 0.30 | 0.90 | tools, reasoning | 300 | us-central1 |
| `nvidia/Nemotron-3-Ultra-550b-a55b` | 1,048,576 | 1.00 | 3.00 | tools, reasoning | 300 | us-central1 |

### Embedding models

| Model ID | Context | Input $/1M | RPM | Region |
|---|---:|---:|---:|---|
| `Qwen/Qwen3-Embedding-8B` | 40,960 | 0.01 | 10,000 | eu-north1 |

### Other text models listed (not evaluated)

`moonshotai/Kimi-K3`, `moonshotai/Kimi-K2.6`, `moonshotai/Kimi-K2.7-Code`, `Qwen/Qwen3-235B-A22B-Instruct-2507`, `Qwen/Qwen3-30B-A3B-Instruct-2507`, `Qwen/Qwen3.5-397B-A17B`, `Qwen/Qwen3.8-27B`, `google/gemma-3-27b-it`, `openai/gpt-oss-120b`, `NousResearch/Hermes-4-405B`, `zai-org/GLM-5.1`, `zai-org/GLM-5.2`, `zai-org/GLM-5.3`, `zai-org/GLM-5.3-Flash`, `openbmb/MiniCPM-V-4_5`, `deepseek-ai/DeepSeek-V4-Flash-0731`, `deepseek-ai/DeepSeek-V4.1-Flash`, `deepseek-ai/DeepSeek-V4-Pro`, `deepseek-ai/DeepSeek-V4-Pro-0813`, `MiniMaxAI/MiniMax-M3`.

CourseFlow's reasoning models are NVIDIA Nemotron. Other providers' models are listed only for completeness.

---

## 2. Capability matrix

Probed models: **`nvidia/Nemotron-3_5-Lightning`** (fast candidate) and **`nvidia/nemotron-3-super-120b-a12b`** (strong candidate). Test input: a three-sentence synthetic coursework brief. Schema: `CourseFact {kind: enum[deadline, weight, requirement], value: str, evidence_text: str}`, `additionalProperties: false`.

| Capability | How it was requested | Lightning | Super | Notes |
|---|---|---|---|---|
| Plain chat | no format constraint | ✅ | ✅ | Super prefixes content with `\n\n`; JSON parsers tolerate it. |
| Reasoning trace | default | separate field | separate field | Returned in `message.reasoning_content` (and `reasoning`), **not** in `content`. Counted in `usage.completion_tokens_details.reasoning_tokens`. |
| JSON via prompt only | schema text in prompt | ✅ valid | ✅ valid | Not enforced; correct only because the prompt was benign. |
| `response_format: json_object` | JSON mode | ✅ valid JSON | ✅ valid JSON | Guarantees JSON syntax **only**. With an adversarial prompt asking for other keys, both returned off-schema JSON. |
| `response_format: json_schema, strict: true` | native structured output | ✅ enforced | ✅ enforced | With the adversarial prompt, both still returned the schema's keys. Shape is enforced; **meaning is not** (see §3.2). |
| `extra_body.guided_json` | vLLM-style guided decoding | ❌ ignored | ❌ ignored | Accepted with HTTP 200 but **silently not applied**: both returned off-schema keys. Do not use. |
| Tool / function calling | `tools` + forced `tool_choice` | ✅ | ✅ | Arguments were valid JSON matching the parameters schema. |
| Long context | ~39k-token prompt with one "needle" sentence | ✅ found | ✅ found | Both answered the needle correctly. Far below the advertised limits; larger contexts not tested. |
| Output truncation | `max_tokens=5` | `finish_reason=length` | `finish_reason=length` | Reasoning tokens count against `max_tokens`. Lightning returned partial *thinking prose* in `content`, which is not an answer. |
| Thinking disabled | `extra_body.chat_template_kwargs.enable_thinking=false` | ✅ 0 reasoning tokens | ✅ 0 reasoning tokens | Correct output, 0.56 s / 0.74 s. Model-specific, not part of the OpenAI API. |
| `reasoning_effort: "low"` | OpenAI parameter | ⚠️ accepted | ✅ fewer tokens | Lightning still used ~1,000 reasoning tokens and returned a wrong fact. Not a reliable control. |

### Failure responses

Same for both models:

| Condition | HTTP | SDK exception | Body shape |
|---|---:|---|---|
| Unknown model ID | 404 | `NotFoundError` | `{"detail": "The model ... does not exist."}` |
| Invalid API key | 401 | `AuthenticationError` | `{"detail": "Couldn't authenticate. ..."}` |
| Invalid parameter (`temperature=99`) | 422 | `UnprocessableEntityError` | FastAPI-style `{"detail": [{"loc": ..., "msg": ...}]}` |
| Client timeout (0.05 s) | — | `APITimeoutError` | No response. |

Error bodies use `detail`, **not** OpenAI's `{"error": {...}}` shape, so `APIError.code`/`type` are `None`. Classification must use the HTTP status and exception class. 429 and 5xx were not observed in this session; their handling is covered by mocked tests only.

### Repeat latency (strict `json_schema`, temperature 0, n = 5, thinking on)

| Model | Median latency | Max latency | Median completion tokens | Schema-valid | Semantically correct |
|---|---:|---:|---:|---:|---:|
| Lightning | 3.67 s | 4.31 s | 1,036 (≈ 95% reasoning) | 5/5 | 5/5 |
| Super | 1.55 s | 1.98 s | 254 (≈ 72% reasoning) | 5/5 | 4/5 |

Approximate cost per call from these token counts at the listed prices: about **$0.00025** for both. Lightning's lower per-token price is cancelled out by its roughly 4× longer reasoning. These are tiny samples on one toy prompt; the fast/strong comparison must be repeated on the golden dataset.

---

## 3. Findings that shape the design

1. **The catalog under-reports capabilities.** No Nemotron model lists `structured_outputs` or `json_mode` in `supported_features`, yet strict `json_schema` was enforced live. Capability decisions come from probes like these, not from metadata.
2. **Schema-valid is not correct.** Under strict mode, Super once returned `{"kind": "requirement", "value": ""}` at temperature 0, and with an adversarial prompt returned `"value": "..."`. Every output is still validated locally with Pydantic, including semantic validators such as "value must contain an alphanumeric character", and gets a bounded repair attempt. Evidence grounding is checked separately against the source text.
3. **Silent no-ops exist.** `guided_json` returns HTTP 200 and is ignored. A request option that isn't verified by a probe should be treated as having no effect.
4. **`finish_reason=length` means "do not parse".** Because reasoning shares the token budget, truncated `content` can be thinking prose. The provider classifies this as `truncated` and does not try to repair it.
5. **Reasoning dominates latency and cost.** Disabling thinking cut latency by about 3–6×. Whether routine extraction keeps its quality without thinking is an evaluation question for the golden dataset, not something to assume.
6. **Errors are classified by status and exception class**, because error bodies are not OpenAI-shaped.

## 4. Configuration chosen

| Role | Setting | Model | Why |
|---|---|---|---|
| Fast | `MODEL_FAST` | `nvidia/Nemotron-3_5-Lightning` | Lowest per-token price, highest Nemotron rate limit (600 RPM), 1M context. |
| Strong | `MODEL_STRONG` | `nvidia/nemotron-3-super-120b-a12b` | Faster and more token-efficient reasoning in these probes; 262k context. |
| Embeddings | `EMBEDDING_MODEL` | `Qwen/Qwen3-Embedding-8B` | The only embedding model in the catalog. Not used yet. |

These are configuration values, not code: swapping a model means changing one environment variable. Revisit after the first golden-dataset evaluation, which should include `NVIDIA-Nemotron-3-Nano-30B-A3B` and `Nemotron-3-Ultra-550b-a55b` (not probed here).
