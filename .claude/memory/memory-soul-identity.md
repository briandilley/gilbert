# Soul & Identity (AIService Internal)

## Summary
Layered persona system that gives Gilbert a stable character without locking out per-user customization. **Soul** = values and principles (who Gilbert is). **Identity** = persona, voice, and style (how Gilbert behaves). Both are managed inside `AIService` via `_SoulHelper` and `_IdentityHelper` in `src/gilbert/core/services/ai.py`.

## Details

### Layering

**Soul — 2 layers:**
1. **Admin soul** — single text owned by the operator. Sourced from ConfigParam `persona.soul` (multiline). Always present at the top of the system prompt. Default: `DEFAULT_SOUL`.
2. **Per-user soul override** — stored in entity collection `soul_user`, keyed by `user_id`. **Only honored when** ConfigParam `persona.allow_user_soul_override` is true. When honored, replaces the admin soul for that user.

**Identity — 3 layers:**
1. **Immutable** — admin-managed via ConfigParam `persona.identity_immutable`. Always present in the system prompt; never overridable by any user. Houses safety/integrity rules (don't impersonate other AIs, don't leak config, etc.). Default: `DEFAULT_IDENTITY_IMMUTABLE`.
2. **Default** — admin-managed via ConfigParam `persona.identity_default`. The default persona/voice. Replaced (not merged) by the per-user override when present. Default: `DEFAULT_IDENTITY_DEFAULT`.
3. **Per-user override** — stored in entity collection `identity_user`, keyed by `user_id`. Optional. When set, replaces the default layer for that user. The immutable layer is still appended on top, so safety rules can never be dropped via user customization.

### Composition order in the system prompt
Built by `AIService._build_system_prompt`:
```
<datetime>
<custom system_prompt if set>
soul_user[user_id] if allowed & set, else persona.soul     ← from _SoulHelper.get_for_user
persona.identity_immutable                                 ← from _IdentityHelper (always)
identity_user[user_id] if set, else persona.identity_default
_PARALLEL_TOOL_USE_HINT                                    ← hardcoded operational nudge
<user identity block, memory summaries, skills, workspace…>
```
The parallel-tool-use hint stays hardcoded (not in soul/identity content) so user customization can't drop it — it's a runtime affordance, not a personality choice.

### Helpers

`_SoulHelper(storage)`:
- `set_admin_text(text)` / `set_allow_user_override(bool)` — pushed in by `_apply_persona_config` from the ConfigParam values.
- `get_for_user(user_id) -> str` — returns the effective soul text.
- `set_user_override(user_id, text)` / `clear_user_override(user_id)` / `get_user_override(user_id)` — manage entries in `soul_user` collection.
- Empty/null override falls back to admin text.

`_IdentityHelper(storage)`:
- `set_immutable_text(text)` / `set_default_text(text)` — pushed in by `_apply_persona_config`.
- `get_for_user(user_id) -> tuple[str, str]` — returns `(immutable_text, effective_default_text)` so the caller appends both.
- `set_user_override(user_id, text)` / `clear_user_override(user_id)` / `get_user_override(user_id)` — manage entries in `identity_user` collection.

Both helpers accept `None`, `"system"`, and `"guest"` as `user_id` and never honor overrides for those.

### ConfigParams (under `ai.persona.*`)
All `restart_required=True` — a live reload would require an apply-and-rebuild path that doesn't exist yet.
- `persona.soul` — admin soul text (multiline). Seeded with `DEFAULT_SOUL`.
- `persona.allow_user_soul_override` — boolean, default false. Gates both the per-user override behavior AND the visibility of soul tools (see below).
- `persona.identity_immutable` — admin immutable identity (multiline). Seeded with `DEFAULT_IDENTITY_IMMUTABLE`.
- `persona.identity_default` — admin default identity (multiline). Seeded with `DEFAULT_IDENTITY_DEFAULT`.

`AIService._apply_persona_config(section)` is called from `start()` and pushes these into the helpers. Empty values fall back to the seed defaults — operators can't accidentally blank Gilbert out.

### AI tools (exposed via AIService)

Identity tools — always available to any authenticated user:
- `get_identity` — returns `{immutable, effective, is_user_override}`.
- `update_my_identity(text)` — sets the caller's `identity_user` override.
- `reset_my_identity` — clears the caller's override.

Soul tools — **only registered** when `persona.allow_user_soul_override` is true. Same pattern the memory tool uses (registration gated on a config flag):
- `get_soul` — returns `{effective, is_user_override, override_allowed}`.
- `update_my_soul(text)` — sets the caller's `soul_user` override.
- `reset_my_soul` — clears it.

Admin-side editing of soul, identity_immutable, and identity_default happens through the standard `/settings` UI (auto-generated from ConfigParams). There are deliberately no admin-mutating tools — the admin layers move through settings, not chat.

### Capability advertisement
`AIService.service_info()` advertises capabilities `soul` and `identity` (replacing the older `persona`). No concrete consumers depend on these yet; they're declared so a future skill / plugin could discover the helpers via the resolver if needed.

### Migration from the old `_PersonaHelper`
The previous `_PersonaHelper` stored a single text in collection `persona` (entity id `active`) with a `customized` flag. That collection is no longer read or written. Existing entries are inert — they don't get migrated automatically. Operators reset their persona by editing the new ConfigParams in `/settings`.

## Related
- `src/gilbert/core/services/ai.py` — `_SoulHelper`, `_IdentityHelper`, `_apply_persona_config`, `_build_system_prompt`, tool definitions and handlers.
- [AI Service](memory-ai-service.md) — surrounding orchestrator.
- [Memory Scopes](memory-memory-scopes.md) — sibling layered system for facts (vs persona).
- [Configuration Service](memory-configuration-service.md) — how ConfigParams reach the UI.
- `tests/unit/test_soul_identity_service.py` — coverage for layering, gating, and tool surface.
