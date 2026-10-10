# ADR-005 — AI provider boundary, structured-output policy and retry semantics

- **Status:** Accepted
- **Date:** 2026-10-09

## Context

CourseFlow's intelligence layer makes many model calls per course: extraction, verification, mapping, dependency and conflict reasoning. Inference runs on NVIDIA Nemotron models served by Nebius Token Factory, which exposes an OpenAI-compatible HTTP API. Four forces shape how these calls are made:

- **Vendor and model churn.** Model IDs, prices and capabilities change over time (see [model catalog](../experiments/model-catalog.md)). Extraction code must not change when a model or provider changes.
- **Untrustworthy outputs.** Live probes showed that strict JSON-schema mode enforces the *shape* of an answer but not its *meaning*. With strict mode on, models still returned `"value": ""` and `"value": "..."`. A vLLM-style `guided_json` option was accepted with HTTP 200 and silently ignored. When reasoning used up the token budget, the truncated `content` contained partial thinking prose. The catalog's `supported_features` field does not list structured outputs for any Nemotron model, yet the probes showed it works.
- **Unreliable networks.** Calls fail transiently (timeouts, connection resets, 429, 5xx) and permanently (401, 404, 422). Retrying a permanent failure wastes time and money; never retrying makes the pipeline brittle. Token Factory's error bodies are not OpenAI-shaped (`{"detail": ...}`), so the class of a failure must come from the HTTP status, not the body.
- **Auditability.** Every consequential output must be traceable to the model, prompt version, cost and outcome that produced it, including failures. See the [data contract](../architecture/data-contract.md).

## Decision

**1. A provider boundary.** Business code depends only on the `AIProvider` protocol (`backend/app/ai/provider.py`) and vendor-neutral types: `ChatMessage`, `TokenUsage`, `StructuredResult[T]`, `ModelInfo` and `AIProviderError`.

- Callers ask for a **role** (`fast` / `strong`), never a model ID. Roles are resolved from `MODEL_FAST` / `MODEL_STRONG` configuration.
- Only `backend/app/ai/nebius.py` imports the vendor SDK.
- Embeddings get a separate interface when retrieval needs them.
- Metadata and cost hooks: `list_models()` returns context length and per-token prices from the live catalog. `estimate_cost_usd()` returns `None` rather than a guess when tokens or prices are unknown.

**2. SDK.** Use the official `openai` Python SDK, pointed at the Token Factory base URL. It gives a typed request/response model and the HTTP status → exception mapping we classify on. No hand-written HTTP client.

**3. Structured-output policy.** `chat_structured(messages, schema, role=...)` returns a validated instance of a Pydantic model or raises:

1. Request native `response_format: json_schema` with `strict: true`, generated from the Pydantic model.
2. **Always** validate locally with Pydantic. Schemas carry semantic validators, not only types (for example "must contain letters or digits"). Validation errors are summarised without input values, so document text never leaks into logs or the database.
3. On invalid output, make a **bounded repair** turn: the model sees its previous answer and the validation errors (default: 1 repair, configurable 0–3).
4. Only `finish_reason == "stop"` is accepted as an answer. `length` is classified as `truncated`, a refusal or `content_filter` stop as `refused`, and anything else as `unexpected`. None of these is parsed or repaired.
5. Evidence grounding (does the excerpt exist in the source?) is a separate, later check. Schema validity is not grounding.

**4. Retry semantics.** The SDK's built-in retries are **disabled** (`max_retries=0`). Our own `RetryPolicy` replaces them:

- **Transient, retried:** timeout (including HTTP 408), connection error, 429, 5xx.
- **Permanent, fail fast:** 400, 401/403, 404, 422, 501/505, truncation, refusal, invalid output after repair, anything unexpected.
- **Bounded:** `AI_MAX_ATTEMPTS` (default 3) per request, plus a per-request timeout (`AI_REQUEST_TIMEOUT_SECONDS`).
- **Backoff:** capped exponential, `min(max, base · 2ⁿ⁻¹)`, with full jitter. A numeric `Retry-After` / `retry-after-ms` header overrides the backoff. If the server asks for longer than the cap, the call fails instead of retrying early, which would almost certainly fail again.
- **Visible:** every attempt is logged as `ai.attempt_failed` / `ai.attempt_succeeded`, with category, sanitised detail, HTTP status, provider request ID, latency and the chosen delay. Unclassified errors also log their traceback. Sleep and randomness are injected, so tests are deterministic.

**5. `ModelCall` record.** Each logical call writes **one** `model_calls` row with:

- provider, model, role, prompt version, output schema;
- status and error category (a CHECK constraint keeps them consistent);
- error detail, attempts, repair rounds and token counts (prompt, completion, reasoning);
- latency (monotonic clock), the provider request ID, a summary of the *validated* output (marked if truncated), and start/finish timestamps. `finished_at` is derived from the monotonic latency, so a wall-clock step can't break the `finished_at >= started_at` CHECK.

`workflow_run_id` is **nullable**, because smoke tests and evaluations call models outside any workflow. Per-attempt detail lives in the logs, not in extra rows. The row is written in its **own transaction**, so a failure record survives a rollback of the caller's work. If writing the record itself fails, that is logged and the original model error is still raised. Secrets and prompts are never stored. Error details carry the exception type, HTTP status and, for validation-style bodies, only `loc` and `msg`: raw provider bodies can echo the request, so they are neither stored nor chained into tracebacks. The model recorded is the one the server reports having used.

**Every outcome is recorded.** Any exception other than `AIProviderError` (a provider contract violation, or output of the wrong type) is recorded as `unexpected` and re-raised. Token counts are summed strictly: if any response did not report usage, the total is unknown (`NULL`), never a partial sum. Attempts that failed in flight may still have been billed, so recorded tokens are a lower bound. Cancellation is not recorded.

## Alternatives considered

| Alternative | Why not (for this version) |
|---|---|
| **Call the OpenAI SDK directly from business code** | Every extraction module would depend on one vendor's types, model IDs and error classes. Swapping a model or provider, or mocking in tests, would touch every call site. |
| **LangChain / LiteLLM as the provider layer** | Both add a large abstraction over what is one OpenAI-compatible endpoint today. They would hide exactly the behaviour that must be explicit: what is retried, how outputs are validated, what is recorded. Revisit if CourseFlow ever needs several providers at once. |
| **Hand-written `httpx` client** | Fewer dependencies, but we would reimplement request/response typing and status → exception mapping that the official SDK already maintains. |
| **`tenacity` for retries** | Capable, but a dependency for roughly 30 lines of policy. Our policy needs domain-specific classification (status, Retry-After, truncation versus invalid output) and per-attempt structured logging, and tenacity would need custom hooks for both. A small `RetryPolicy` is easier to test and explain. |
| **Keep the SDK's built-in retries** | Retries would happen invisibly inside the SDK: attempts would not be counted in `ModelCall`, not logged per attempt, and not controlled by our classification. |
| **JSON mode (`json_object`) or prompt-only JSON** | Probes showed JSON mode guarantees syntax only: with an adversarial prompt, models returned off-schema keys. Strict `json_schema` is strictly better where supported. |
| **Trust strict mode, skip local validation** | Probes produced schema-valid but empty or placeholder values. The API's guarantee is about shape, not meaning. |
| **Tool/function calling as the structured channel** | Also worked in the probes, but it is semantically "the model wants to act". `json_schema` states the intent (return data) directly. Tool calling stays available for real tools. |
| **One row per HTTP attempt (`ModelAttempt` table)** | More precise, but multiplies rows and joins for little benefit at this stage. Attempts are counted on the call row and detailed in the logs. |
| **Name the record `AIRun`** | Collides with `WorkflowRun` (a run is a whole workflow). A *model call* is one step inside it, or standalone. |

## Consequences

**Positive**

- Changing a model is a configuration change, and changing a provider is one new module. Extraction code and its tests do not change.
- Every failure has a category that drives behaviour: retry, fail fast or repair. The category is persisted, so retry rate, schema-violation rate and failure modes can be measured.
- Tests use the real SDK against a mocked transport, so error mapping is tested as it really behaves, with no network.
- Costs are traceable per call from reported tokens and catalog prices, never estimated from guesses.

**Negative / risks**

- **More code to own** than calling the SDK directly: the retry loop, classification and repair turn. *Mitigation:* small modules, mutation-checked tests for retry versus fail-fast, truncation and the repair bound.
- **Repair turns cost tokens** and can make latency worse. *Mitigation:* bounded (default 1), and the attempts and tokens of both turns are recorded. Measure repair rate on the golden dataset.
- **Strict schema support is provider-specific** and undocumented in the catalog. *Mitigation:* local validation does not depend on it. Re-probe when models change.
- **Retrying timeouts can double-charge** for a request the server completed. *Mitigation:* attempts are bounded and recorded; acceptable for side-effect-free inference.
- **Reasoning tokens share `max_tokens`**, so too low a budget turns into `truncated` failures. *Mitigation:* a generous default (`AI_MAX_OUTPUT_TOKENS=4096`) and a distinct error category.
- **An SDK major version can change transports or types** (the current SDK uses `httpx2`). *Mitigation:* the dependency is pinned in `uv.lock`, and only one module imports it.

## Revisit when

- A second provider or a self-hosted endpoint is needed at the same time as Token Factory (consider a routing layer then).
- The measured repair rate on the golden dataset is high enough that repairs dominate cost or latency, or almost zero, so the bound could drop to 0.
- Per-attempt analysis is needed beyond what logs give, for example retry-rate dashboards over long periods (consider a `model_call_attempts` table).
- Token Factory publishes official structured-output guarantees or OpenAI-shaped error bodies (simplify classification).
- Streaming responses are needed for user-facing latency.
