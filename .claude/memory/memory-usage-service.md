# Usage Service

## Summary
Records per-round AI token usage into an `ai_token_usage` entity collection,
computes USD cost from a per-backend-per-model pricing table (defaults
hardcoded, overridable via config), and exposes filter/group-by/aggregate
queries for reporting UIs.

## Details

### Architecture Layers

- **`interfaces/usage.py`** — Pure interfaces:
  - `UsageRecord` (frozen dataclass) — one row describing a single AI round's
    consumption: timestamp, user_id, user_name, conversation_id, profile,
    backend, model, input/output/cache_creation/cache_read tokens,
    cost_usd, tool_names, stop_reason, round_num, invocation_source.
  - `ModelPricing` — per-model USD/MTok rates (input, output,
    cache_creation, cache_read).
  - `UsageQuery` — filter + group_by spec.
  - `UsageAggregate` — one row returned by `query_usage`; represents either
    a single round (ungrouped) or a summed group.
  - `UsageRecorder` Protocol (`@runtime_checkable`) — `record_round(...)`
    method, called by `AIService` after every AI round. Safe-by-design —
    implementations must never raise into the AI loop.
  - `UsagePricingProvider` Protocol — `compute_cost(...)` for previewing
    cost without persisting a record.
  - `UsageProvider` Protocol — `query_usage(...)` and
    `list_models_with_usage()` for reporting UIs.
- **`core/services/usage.py`** — `UsageService(Service)`. Implements all three
  runtime-checkable protocols. Requires `entity_storage`, optional
  `configuration`.

### Collection shape (`ai_token_usage`)

```
{
  "_id": "<uuid>",
  "timestamp": "2026-04-19T14:23:11.004+00:00",   # ISO, indexed
  "date": "2026-04-19",                             # denormalized for daily grouping
  "user_id": "...", "user_name": "...",             # indexed user_id
  "conversation_id": "...",                         # indexed
  "profile": "standard",
  "backend": "anthropic",
  "model": "claude-opus-4-20250514",
  "input_tokens": 1234,                             # fresh input (excludes cache hits)
  "output_tokens": 567,
  "cache_creation_tokens": 200,                     # Anthropic-only
  "cache_read_tokens": 500,                         # Anthropic + OpenAI
  "cost_usd": 0.0412,
  "tool_names": ["web_search", "fetch_url"],       # multi-tool rounds list all
  "stop_reason": "tool_use",
  "round_num": 2,
  "invocation_source": "chat"                       # chat | slash | one_shot | ai_call:<name>
}
```

Indexes declared at `start()`: `timestamp`, `user_id`, `conversation_id`.

### Capture Point

`AIService._record_round_usage` in `core/services/ai.py` runs immediately
after `_log_api_call` inside the agentic loop. It:

1. Folds the round's TokenUsage into `turn_usage_totals` (input, output,
   cache_*, cost, rounds).
2. Resolves `UsageRecorder` via `resolver.get_capability("usage_recording")`
   and calls `record_round()`.
3. Returns a per-round usage dict that the loop attaches to the next
   `turn_rounds` entry (so each round card in the chat UI shows its own
   tokens + cost).
4. Never raises — storage failures / missing recorder are logged and
   swallowed so usage accounting never breaks the AI loop.

Also hooked into `complete_one_shot` (MCP sampling path) with
`invocation_source="one_shot"` and `user_ctx=SYSTEM`.

### Pricing

- Defaults in `_DEFAULT_PRICING` dict keyed `{backend: {model: ModelPricing}}`.
  Seeded with current public rates for Anthropic (Opus/Sonnet/Haiku 4.x) and
  OpenAI (gpt-4o, gpt-4o-mini, gpt-4.1, o1, o3-mini).
- `config_params()` generates **one numeric form field per rate** from
  `_DEFAULT_PRICING` — users tweak rates in the Settings UI as plain number
  inputs, not JSON. Keys are dotted:
  `pricing.<backend>.<sanitized_model>.<field>` where `<sanitized_model>`
  has `-` and `.` collapsed to `_` (avoids collisions with the config path
  separator for model IDs like `gpt-4.1` and `claude-opus-4-20250514`).
  `cache_creation_per_mtok` / `cache_read_per_mtok` fields only render when
  their default is non-zero, so OpenAI models don't show a useless
  cache-write field.
- `on_config_changed` walks `config["pricing"][backend][sanitized_model]`,
  reverse-maps the sanitized model key back to the real ID, and rebuilds
  `self._overrides`. Adding a new model is a one-line edit to
  `_DEFAULT_PRICING`; the form field, default, and override plumbing all
  follow automatically.
- `compute_cost()` returns 0.0 for unknown models — lets the system degrade
  gracefully when a new model ships before its pricing is added.

### Cache-token semantics (normalization)

`TokenUsage.input_tokens` is **fresh input only** — excludes any cache hits.
Backends normalize differently:

- **Anthropic** reports disjoint counts already
  (`input_tokens` + `cache_creation_input_tokens` + `cache_read_input_tokens`),
  so populate as-is.
- **OpenAI** reports `prompt_tokens` that *includes* `cached_tokens`, so the
  backend subtracts before populating `input_tokens`.

This keeps `compute_cost` simple — multiply each field by its rate.

### Grouping

`UsageQuery.group_by` accepts any subset of:
`user_id`, `user_name`, `backend`, `model`, `profile`, `conversation_id`,
`tool_name`, `date`, `invocation_source`.

When grouping by `tool_name`, a round with N tools contributes N separate
entries (one per tool), each crediting the full token/cost counts for that
round. Splitting cost fractionally across tools would be misleading — tokens
are billed per round, not per tool.

### WS handlers

- `usage.query` → `{rows: UsageAggregate[]}` — admin-gated via default ACL
  (`usage.*` → level 0 in `interfaces/acl.py`).
- `usage.models` → `{models: [{backend, model}, ...]}` — distinct models
  seen in the usage collection (legacy entry point; reporting UIs prefer
  `usage.dimensions` now).
- `usage.dimensions` → `{users, backends, models, profiles, tools,
  invocation_sources}` — one RPC that returns every distinct value in the
  usage collection across every dimension, so the reporting page can
  render its full filter strip from a single call.

### Chat UI integration

- `ChatTurn.turn_usage` + `ChatRound.usage` flow through the WS result
  frame and are rendered by `TurnBubble.tsx`: per-round tokens + cost
  chip in the round header, per-turn total chip next to the Gilbert
  name. `ChatPage.tsx` also shows a running conversation total under
  the chat title by summing `turn_usage` across all loaded turns.
- Frontend helpers in `frontend/src/lib/usage.ts` (`formatTokens`,
  `formatCost`, `summarizeUsage`) keep the token/cost formatting
  consistent between the chat chips and the reporting page.

### Reporting page (`/usage`)

`frontend/src/components/usage/UsagePage.tsx` — admin-only (driven by
`usage.*` ACL + `requires_capability: "usage_reporting"` on the nav
entry). Layout:

- KPI strip: Cost, Input, Output, Avg $/round.
- Filters row: date range (Today/7d/30d/All chips + date inputs), plus
  User / Backend / Model / Profile / Tool / Source dropdowns populated
  from `usage.dimensions`.
- Dimension pickers: Group by + Metric (cost / input / output / total
  tokens).
- Chart: `recharts` — area chart when grouped by date, horizontal bar
  chart (top 20) otherwise. Colors come from a small palette picked for
  distinguishability.
- Table: full per-group breakdown with rounds / input / output / cache
  read / cost columns, sorted by cost desc.

The `invocation_source` filter is applied client-side after the fetch
because the server's `query_usage` doesn't accept it as a filter field
yet. If that filter becomes heavy, add it to `UsageQuery` on the server
side.

### Wiring

Registered in `core/app.py` immediately before `AIService()` so the
`usage_recording` capability is resolvable on the first AI turn.

### ChatTurnResult integration

`ChatTurnResult.turn_usage: dict | None` carries aggregate totals for the
whole turn (sum over every round including the final end_turn round).
Shape: `{input_tokens, output_tokens, cache_creation_tokens,
cache_read_tokens, cost_usd, rounds}`. Each `turn_rounds` entry also gets
its own per-round `usage` sub-dict with the same keys.

## Related
- [AI Service](memory-ai-service.md) — capture point + ChatTurnResult
- [Storage Backend](memory-storage-backend.md) — entity storage
- `src/gilbert/interfaces/usage.py`
- `src/gilbert/core/services/usage.py`
- `tests/unit/test_usage_service.py`
