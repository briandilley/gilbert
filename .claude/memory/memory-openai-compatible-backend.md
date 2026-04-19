# OpenAI-Compatible AI Backend

## Summary
Vendor-neutral AI backend covering the long tail of OpenAI-Chat-Completions endpoints that don't have a dedicated Gilbert plugin: self-hosted vLLM, LM Studio, llama.cpp, corporate gateways, managed providers still waiting on a first-party plugin. Lives at `std-plugins/openai-compatible/`; `backend_name = "openai_compatible"` (underscored because backend names are config keys: `backends.openai_compatible.*`).

## Details

### Why a separate plugin — and how it relates to the provider-specific ones

Upstream chose per-provider plugins for the big names: `groq`, `ollama`, `openrouter`, `xai`, `deepseek`, `mistral`, `qwen`, `bedrock`. Each ships curated model catalogs and provider-specific defaults. This plugin complements them — it's for endpoints that speak OpenAI-Chat-Completions but don't rate a dedicated plugin yet.

Not folded into `openai` because the `openai` plugin carries OpenAI-specific assumptions that are wrong for generic endpoints:

- Hardcoded model catalog (GPT-4o, o1, o3 …).
- `max_completion_tokens` rename instead of `max_tokens`.
- o-series reasoning-model special-casing (strip temperature on `o1`/`o3`).
- `OpenAI-Organization` header support.

This plugin strips all of that: free-form model string, plain `max_tokens`, no reasoning-model branch, no org header. One `backend_name` = one plugin, per the backend pattern.

### Config surface
- `enabled` *(bool, default true)* — matches the house pattern added upstream for all AI backends; AIService skips initialize when false.
- `base_url` *(required, no default)* — init raises `ValueError` if blank. The plugin's whole point is that the user picks the endpoint.
- `api_key` *(sensitive, optional)* — when blank, no `Authorization` header is sent. Local proxies work out of the box.
- `model` — free-form string, no `choices` tuple. `_build_request_body` raises `AIBackendError` if empty and no per-request model is set.
- `max_tokens` (default 4096 — conservative for local models), `temperature` (0.7, always sent).
- `request_headers` *(multiline)* — `key: value` lines, merged into every request. Unique to this plugin; lets proxies with bespoke auth (`x-api-key`, workspace headers) work without modifying the plugin.
- `supports_tools` *(bool)* — when false, requests carrying tools raise `AIBackendError` with a clear message instead of silently 4xx'ing.
- `supports_streaming` *(bool)* — when false, `capabilities().streaming` reports false and `generate_stream` falls back to `generate()` + single `MESSAGE_COMPLETE`. Needed for endpoints that choke on `stream: true`.

### Model discovery
`available_models()` starts empty — there is deliberately no shipped catalog. The `refresh_models` backend action hits `GET /models`, parses `data[].id`, and populates an in-memory `_discovered_models` list; the UI picks it up via `available_models()` without a restart. On 404, the action reports that the endpoint doesn't implement `/models` and tells the user to type the model ID manually. Cache is in-memory only — no disk persistence — because backends don't receive a data_dir from AIService; users re-run the action after a restart if they want fresh data.

### Streaming / request shape
Everything else — request/response building, SSE tool-call delta aggregation, attachment handling — is the same Chat Completions shape as `openai`, `ollama`, `openrouter`, `groq`, etc. Image attachments ride as `image_url` data-URL parts, PDFs become text stubs, text attachments inline as `## <name>\n\n<body>`. The SSE parser uses the same `tool_builders` keyed-by-index accumulator pattern to reassemble `tool_calls[i].function.arguments` chunks.

### Header parsing (`_parse_header_lines`)
Accepts a multi-line blob; splits on first `:`; ignores blank lines and lines starting with `#`; silently drops lines that don't contain a colon. Exposed at module level (not as a method) so tests can cover it directly without instantiating the backend.

## Related
- `std-plugins/openai-compatible/openai_compatible_ai.py` — the backend.
- `std-plugins/ollama/ollama_ai.py` — the closest sibling; also OpenAI-compat, also optional api_key, but ships a curated Ollama-pull model catalog.
- `std-plugins/openai/openai_ai.py` — first-party OpenAI, with OpenAI-specific catalog, `OpenAI-Organization` header, and o-series special casing.
- [Backend Pattern](memory-backend-pattern.md) — one `backend_name` per plugin; registry discovery.
- [AI Service](memory-ai-service.md) — how the agentic loop drives `generate_stream`.
