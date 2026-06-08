# Per-model config is core and layered, not per-backend

Per-`(backend, model)` settings — an `enabled` flag plus generation defaults (`max_tokens`,
`temperature`, context window), seeded from Hugging Face / GGUF metadata when a model is pulled —
are owned by **`AIService` (core)** and exposed through a capability, not stored inside each backend.
Generation params resolve in **layers**: *backend default ← per-model ← profile ← per-call
override*. To carry the resolved values, `AIRequest` (and optionally `AIContextProfile`) gain
**optional** generation fields; a backend applies what it is handed and falls back to its own default
when a field is unset.

## Considered options

- **Per-backend (Ollama-scoped) map** — rejected: the local-model **manager** is a separate plugin
  (std-plugins ADR-0007), so it would have to reach into the `ollama` backend's storage to edit
  per-model settings (cross-plugin storage coupling), and every other backend (Anthropic, Groq, …)
  would have to reinvent per-model settings independently.

## Consequences

- The `AIBackend` contract changes: generation params arrive on `AIRequest` as **optional** fields.
  `None` means "use the backend's own default," so existing backends keep working unchanged until
  they opt in to honoring them.
- The `ollama` backend's current *global* `temperature` / `max_tokens` become the **backend-default
  layer**, and its `enabled_models` array is subsumed by the per-model `enabled` flag (which also
  drives the now-dynamic `available_models()`).
- "Temperature is a use-case knob, not a model property" is preserved by the layering: a per-model
  value is only a *default* that a profile or call can override.
