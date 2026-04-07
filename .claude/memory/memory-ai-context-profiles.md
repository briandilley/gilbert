# AI Context Profiles

## Summary
Named profiles that control which tools are available for each AI interaction. Replaces the old `tool_filter` + `chat_enabled` system with a configurable, runtime-manageable approach.

## Details

### Profile Structure
- `AIContextProfile` dataclass in `src/gilbert/core/services/ai.py`
- Fields: `name`, `description`, `tool_mode` (all/include/exclude), `tools` (list), `tool_roles` (dict of per-tool role overrides)
- Stored in `ai_profiles` entity collection, seeded from config + built-in defaults on startup

### Built-in Profiles
- `default` — all tools, no filtering (fallback for unassigned calls)
- `human_chat` — excludes internal tools (e.g., `sales_lead`)
- `text_only` — no tools (pure text generation)
- `sales_agent` — include only `sales_lead`

### Call Assignments
- Stored in `ai_profile_assignments` collection: `{call_name → profile_name}`
- Services declare named AI calls in `ServiceInfo.ai_calls` frozenset
- Callers pass `ai_call="name"` to `ai.chat()`; resolved to profile via assignment
- Unassigned calls use `default` profile

### Current Callers
| Caller | ai_call | Profile |
|--------|---------|---------|
| Web chat | `human_chat` | human_chat |
| Slack | `human_chat` | human_chat |
| GreetingService | `greeting` | text_only |
| RoastService | `roast` | default |
| InboxAIChatService | `inbox_ai_chat` | default |
| SalesAssistant (initial) | `sales_initial_email` | sales_agent |
| SalesAssistant (reply) | `sales_reply` | sales_agent |

### Tool Resolution Flow

When `ai.chat(ai_call="name")` is called:

1. **Resolve profile** — look up `ai_call` in assignments → get profile name → load `AIContextProfile`. If no assignment exists, uses `default` profile. If `ai_call=None`, no profile filtering occurs.
2. **Discover all tools** — iterate every service with `ai_tools` capability, call `get_tools()` on each `ToolProvider`, collect into one dict.
3. **Apply profile filter** — based on `tool_mode`:
   - `all` → keep everything
   - `include` → keep only tools named in `profile.tools`
   - `exclude` → remove tools named in `profile.tools`
4. **Apply RBAC** — for each remaining tool, check user's effective role level against the tool's `required_role` (or the profile's `tool_roles` override if one exists). Remove tools the user can't access.
5. **Result** — the filtered set is what the AI sees and can call.

Profiles control *which* tools are available; RBAC controls *who* can use them. Both always apply.

### Per-Profile Role Overrides
`tool_roles` dict allows overriding a tool's `required_role` within a profile. E.g., make `search_music` available to "everyone" in one profile but "user" in another.

### Management
- AI tools: `list_ai_profiles`, `set_ai_profile`, `delete_ai_profile`, `assign_ai_profile`, `clear_ai_assignment`
- Web UI: `/roles/profiles` — profile CRUD + call assignment management
- Config: `ai.profiles` in gilbert.yaml for defaults seeded to storage on first run

### What It Replaced
- `ToolDefinition.chat_enabled` field (removed)
- `apply_chat_visibility` parameter on `ai.chat()` (removed)
- `tool_filter` parameter on `ai.chat()` (removed)
- `AccessControlService` chat visibility overrides (removed)

## Related
- `src/gilbert/core/services/ai.py` — AIContextProfile, profile loading/management, _discover_tools
- `src/gilbert/interfaces/service.py` — ServiceInfo.ai_calls field
- `src/gilbert/web/routes/roles.py` — /roles/profiles web UI
- `src/gilbert/web/templates/ai_profiles.html` — profile management template
- `gilbert.yaml` — ai.profiles config section
