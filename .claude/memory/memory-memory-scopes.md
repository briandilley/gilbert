# Memory Scopes (AIService Internal)

## Summary
Persistent facts the AI recalls into the system prompt. Two scopes — per-user (default) and global (visible to everyone, admin-write-only). Implemented as `_MemoryHelper` inside `src/gilbert/core/services/ai.py`.

## Details

### Scopes

**`user`** (default):
- Collection: `user_memories`, indexed by `user_id`.
- Visible only to the owning user. Anyone may manage their own.
- Records: `{memory_id, user_id, summary, content, source, access_count, created_at, updated_at}`.
- `source` is `"user"` (explicit ask) or `"auto"` (AI noticed something worth remembering).

**`global`**:
- Collection: `memory_global` (no user_id field — collection scan is fine; these are short and rare).
- Visible to **every** user in their system prompt.
- Writes (`remember`, `update`, `forget`) require **admin role** — enforced in `AIService._tool_memory_action` via the injected `_user_roles` arg.
- Records: `{memory_id, summary, content, access_count, created_at, updated_at}` (no `user_id`, no `source`).
- IDs are prefixed `gmem_` so they're easy to tell apart from `memory_` user records in logs.

### System prompt injection
`_MemoryHelper.get_summaries_for_user(user_id)` aggregates both scopes and returns one block with two labeled sections:
```
## Global memories (N stored, visible to everyone)
- [gmem_xxx] summary (global)
…

## Memories for this user (M stored)
- [memory_xxx] summary (user)
…
```
The per-user section is omitted for `system`/`guest` callers; the global section is shown to everyone. Empty scopes drop their section header entirely.

### Tool surface
Single `memory` tool with action + scope params:
- Actions: `remember`, `recall`, `update`, `forget`, `list`.
- `scope`: `"user"` (default) or `"global"`.
- `required_role="user"` on the tool itself (visibility) — admin-only writes to global are enforced inside the handler since RBAC is per-tool, not per-arg.
- Slash form: `/memory <action> [scope=user|global] [summary='…'] [content='…']`.

`AIService._tool_memory_action`:
- Resolves caller via injected `_user_id` (falls back to contextvar — direct callers like tests).
- For `scope="user"`, requires authenticated user (rejects `system`/`guest`).
- For `scope="global"` writes (`remember`/`update`/`forget`), requires `"admin"` in `_user_roles`. Reads (`recall`/`list`) are open to any user-level caller.
- Dispatches to `_MemoryHelper.{remember,recall,update,forget,list_memories}` which internally branch on scope.

### Why two scopes (not three)
The earlier sketch included a third "core" scope (settings-only, never tool-writable). It was dropped to keep the surface small — admin-tool-writable global with admin/settings UI both backing it covers every governance use case the operator has expressed. If a stricter "operator-only canon" tier is needed later, add it then.

### Capability advertisement + config
- `AIService.service_info()` advertises `user_memory`.
- ConfigParam `memory_enabled` (boolean, default true, restart_required) gates the entire memory subsystem — when off, the tool isn't registered at all and the system prompt skips memory injection.

## Related
- `src/gilbert/core/services/ai.py` — `_MemoryHelper`, `_tool_memory_action`, memory tool definition.
- [Soul & Identity](memory-soul-identity.md) — sibling layered system for persona (vs facts).
- [AI Service](memory-ai-service.md) — surrounding orchestrator and system prompt builder.
- [Access Control](memory-access-control.md) — role hierarchy that gates global writes.
- `tests/unit/test_memory_service.py` — coverage including scope dispatch and admin RBAC.
