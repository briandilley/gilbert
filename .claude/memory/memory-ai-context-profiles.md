# AI Context Profiles

## Summary
Named bundles of (tool allowlist + backend + model) that every AI call resolves through. The system is tier-shaped now: `light` / `standard` / `advanced` profiles ship built-in, and each AI-driving service exposes an `ai_profile` config so admins can route different use cases to different tiers without changing code.

## Details

### Profile Structure
- `AIContextProfile` dataclass in `src/gilbert/interfaces/ai.py`
- Fields:
  - `name`, `description`
  - `tool_mode` (`all` / `include` / `exclude`)
  - `tools` (list[str]) — tool names for include/exclude
  - `tool_roles` (dict[str, str]) — per-tool role overrides
  - `backend` (str) — pin a specific AI backend (`""` = first available)
  - `model` (str) — pin a model on that backend (`""` = backend default)
- Stored in the `ai_profiles` entity collection, seeded from built-in defaults on first start

### Built-in Profiles (undeletable)
- `light` — light tier (fast, cost-effective model), all tools
- `standard` — standard tier (balanced model), all tools
- `advanced` — advanced tier (most capable model), all tools

The tiers themselves are just profile names; admins map each tier to a real backend/model under **Settings → AI → Profiles**. Custom profiles can be added on top.

### Built-in Call Assignments
`_BUILTIN_ASSIGNMENTS` in `core/services/ai.py` seeds these on first start:

| ai_call | Profile |
|---|---|
| `human_chat` | standard |
| `greeting` | light |
| `roast` | standard |
| `scheduled_action` | standard |
| `inbox_ai_chat` | standard |
| `guess_song_validate` | light |
| `mcp_sampling` | standard |
| `mcp_server_client` | standard |

The assignment table still exists for back-compat, but **new code should not pass `ai_call`** — instead, services declare an `ai_profile` config param and pass that profile name directly to `ai.chat(..., ai_profile=...)`.

### Per-Service `ai_profile` Config
Every service that calls AI now exposes an `ai_profile` config:
- `ai` itself: `chat_profile` (web + Slack chat) and `default_profile` (fallback)
- `greeting.ai_profile` (default `light`)
- `roast.ai_profile` (default `standard`)
- `scheduler.ai_profile` (default `standard`)
- `inbox_ai_chat.ai_profile` (default `standard`)
- `slack.ai_profile` (default `standard`) — in the slack std-plugin
- `guess_game` passes `ai_profile="light"` to its validator chat call
- MCP server records carry `sampling_profile` (default `standard`) and per-client `ai_profile` (default `standard`)

All of these use `choices_from="ai_profiles"` so the dropdown is dynamic — adding a custom profile makes it selectable everywhere automatically.

### Tool Resolution Flow
When `ai.chat(..., ai_profile="name")` (or older `ai_call="name"`) is called:

1. **Resolve profile** — direct lookup by `ai_profile`, or via `_assignments` for `ai_call`. Falls back to `default_profile` (default `standard`).
2. **Resolve backend & model** — `_resolve_backend_and_model(profile)` picks `profile.backend` (or first available), then `profile.model` (or that backend's default).
3. **Discover all tools** — iterate every `ToolProvider` capability, collect tools.
4. **Apply profile filter** based on `tool_mode`:
   - `all` → keep everything
   - `include` → keep only tools in `profile.tools`
   - `exclude` → remove tools in `profile.tools`
5. **Apply RBAC** — check user's effective role against each tool's `required_role` (or the profile's `tool_roles` override).
6. **Result** — the filtered set is what the AI sees on this turn, sent to the resolved backend/model.

Profiles control *which* tools are available; RBAC controls *who* can use them. Both always apply.

### Per-call Override (Chat UI)
The web chat UI lets a user override the model for a single conversation via the model picker. The override is sent as `model` + `backend` on the `chat.message.send` frame, persisted as `model_preference` conversation state, and echoed back as `model` on the response so the bubble can show what answered.

### Backend Visibility Rule
Only `AIService`, the AI profiles editor, and the chat UI know about backend names or model IDs. Service callers only ever reference profile names (via `ai_profile` config or by name passed to `chat`). This keeps the rest of the codebase decoupled from "which provider is configured."

### Capability Protocols
Three Protocols in `interfaces/ai.py` let other services consume AI features without importing the concrete `AIService`:
- `AIProvider` — the main `chat()` entry point
- `AISamplingProvider` — `has_profile()` + `complete_one_shot()` (used by MCP sampling)
- `AIToolDiscoveryProvider` — `discover_tools()` (used by MCP server endpoint)
- `AIModelProvider` — `get_enabled_models()` (used by ConfigurationService for dynamic choices)
- `SharedConversationProvider` — `list_shared_conversations()` (used by WS handshake)

### Management
- AI tools: `list_ai_profiles`, `set_ai_profile`, `delete_ai_profile`, `assign_ai_profile`, `clear_ai_assignment`
- Web UI: `/security/profiles` — profile CRUD with backend/model dropdowns, plus call assignment management
- Config: `ai.default_profile` and `ai.chat_profile` set the headline routing decisions

## Related
- `src/gilbert/interfaces/ai.py` — `AIContextProfile` dataclass, `AISamplingProvider`, `AIToolDiscoveryProvider`, `AIModelProvider`, `SharedConversationProvider`
- `src/gilbert/core/services/ai.py` — built-in profiles, assignments, `_resolve_backend_and_model`, `discover_tools`
- `frontend/src/components/roles/AIProfiles.tsx` — `/security/profiles` editor with backend/model dropdowns
- [AI Service](memory-ai-service.md) — service-level details
- [Backend Pattern](memory-backend-pattern.md) — how AI backends register and how the multi-backend dict works
