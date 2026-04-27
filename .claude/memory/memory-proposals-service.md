# Proposals Service

## Summary
Autonomous self-improvement reflector. Gathers observations from four sources (event bus, in-chat AI tool, conversation harvest, pre-delete extraction) into a persistent observations collection, periodically asks the AI to propose new plugins / services / config changes, and persists structured records into the `proposals` collection for admin triage.

## Details

### Core files
- `src/gilbert/core/services/proposals.py` — `ProposalsService`
- `src/gilbert/interfaces/proposals.py` — `ProposalsProvider` capability protocol + status / kind / source-type constants
- Registered in `core/app.py` alongside other optional services
- ACL in `interfaces/acl.py`: `proposals.` (RPC) admin-only, `proposal.` (events) admin-only, `chat.conversation.archiving` (event) admin-only

### Capability declarations
- `capabilities = {"proposals", "ws_handlers", "ai_tools"}`
- `optional = {"entity_storage", "event_bus", "scheduler", "ai_chat", "configuration"}` — degrades gracefully when any are missing
- `ai_calls = {"record_observation"}`
- `events = {"proposal.created", "proposal.status_changed"}`
- `toggleable = True`

### Observations: four sources, one collection

All observations land in `proposal_observations` (`OBSERVATIONS_COLLECTION`). Each row:
```
{ _id, source_type, summary, details, occurred_at, created_at, consumed_in_cycle }
```

`source_type` is one of:

| Source | Origin | AI cost |
|---|---|---|
| `event` | Event bus subscription (`subscribe_pattern("*")`). Filters out `_NOISY_EVENT_TYPES` (chat.stream.*) and `proposal.*` (self-emissions). Buffered in memory, flushed on threshold or periodic timer. | None |
| `ai_tool` | Gilbert calls `record_observation(summary, category?, context?)` mid-conversation when he notices a capability gap / frustration / etc. Admin-only tool. | One round, in the user's chat (cost is theirs) |
| `conversation_active` / `conversation_abandoned` | Scheduled harvest job walks `ai_conversations`, classifies each by `updated_at` vs `abandonment_threshold_seconds`, asks AI to extract observation candidates per conversation. Skips conversations where the most recent observation already covers the current `message_count`. | One AI call per processed conversation, capped per cycle |
| `conversation_deleted` | AIService publishes `chat.conversation.archiving` event before deleting (carries the conversation snapshot). ProposalsService subscribes and runs one extraction pass. Deletion does not wait. | One AI call per deletion |

Buffered events: `_observation_buffer` flushes on size threshold (`observation_flush_threshold`, default 50) or via the periodic `proposals.observation_flush` scheduler job (`observation_flush_interval_seconds`, default 30s). Final flush on `stop()`. The lock (`_buffer_lock`) is created in `start()`, so observations recorded before the service starts are dropped.

### Lifecycle: observation → reflection → triage

1. **Observation** — passive event subscription + AI tool + scheduled harvest + pre-delete extraction. Three scheduler jobs registered as system jobs: `proposals.reflection`, `proposals.conversation_harvest`, `proposals.observation_flush`.
2. **Reflection** — runs on schedule (default 6h) or via manual trigger (`proposals.trigger_reflection` WS frame, `trigger_reflection` config action). Each cycle:
   - Drains the observation buffer and prunes oldest rows beyond `observation_cap_total`.
   - Counts `consumed_in_cycle == ""` observations + buffered ones; skips when below `min_observations_per_cycle` (manual bypasses).
   - Skips when pending proposals already at `max_pending_proposals` (manual still respects this).
   - Loads up to `_DEFAULT_REFLECTION_OBSERVATION_LIMIT` (400) unconsumed observations sorted by `occurred_at desc`.
   - Builds the prompt with observations grouped by `source_type` (events deduped by `event_type`; non-event sources listed individually with `category` / `conversation_id` extras).
   - Calls AI with `_REFLECTION_SYSTEM_PROMPT`, parses JSON, validates each proposal via `_build_record`, persists.
   - Marks the observations it cited as `consumed_in_cycle = <id>` so the next cycle focuses on what's new.
3. **Triage** — admins use `/proposals` to view, add notes, change status, or delete. State changes publish `proposal.status_changed`.

### Proposal record shape (unchanged)
Stored in `proposals` (`PROPOSALS_COLLECTION`). Identity: `_id`/`id`, `title`, `summary`, `kind`, `target`, `status`. Provenance: `motivation`, `evidence`, `ai_profile_used`, `reflection_cycle_id`, timestamps. Spec: `spec` dict + `implementation_prompt` (self-contained for a fresh Claude session) + `impact` + `risks` + `acceptance_criteria` + `open_questions`. Triage: `admin_notes`.

### WS RPC handlers (all admin-only)
- `proposals.list` — `{status?, kind?, limit?}` → `{proposals, available_statuses, available_kinds}`
- `proposals.get` — `{proposal_id}` → `{proposal}`
- `proposals.update_status` — `{proposal_id, status}`
- `proposals.add_note` — `{proposal_id, note}`
- `proposals.delete` — `{proposal_id}`
- `proposals.trigger_reflection` — `{}` → `{created}`

### Config actions
- `trigger_reflection` — manual reflection cycle
- `trigger_harvest` — manual conversation-harvest run

### Configuration (namespace `proposals`, category `Intelligence`)
- `enabled` (bool, default true)
- `reflection_interval_seconds` (int, default 21600 = 6h, restart_required)
- `max_proposals_per_cycle` (int, default 3)
- `harvest_interval_seconds` (int, default 21600, restart_required)
- `harvest_max_conversations_per_cycle` (int, default 20)
- `abandonment_threshold_seconds` (int, default 86400 = 24h)
- `observation_cap_total` (int, default 5000) — pruning ceiling, oldest rows dropped at start of each reflection cycle
- `observation_flush_threshold` (int, default 50) — buffer size that triggers immediate flush
- `observation_flush_interval_seconds` (int, default 30, restart_required)
- `min_observations_per_cycle` (int, default 25) — skip the AI call when signal is too sparse
- `max_pending_proposals` (int, default 10) — backlog cap
- `ai_profile` (str, choices_from `ai_profiles`, default `advanced`)
- `observation_event_patterns` (array, default `["*"]`, restart_required) — what the bus subscriber listens to (noisy types are still dropped at the handler)

### `record_observation` AI tool
Exposed by ProposalsService (it's a `ToolProvider`). Admin-only (`required_role="admin"`). Parameters: `summary` (required), `category` (enum: capability_gap / recurring_frustration / knowledge_gap / feature_request / usage_pattern / other), `context` (optional paragraph). Tool description tells Gilbert to use it during chats when he notices something that could become a self-improvement proposal. Each call records one observation.

### `chat.conversation.archiving` event
Published by `AIService._ws_conversation_delete` and the room-destroy path in `_ws_room_leave` BEFORE the storage delete. Payload: `{conversation_id, owner_id, conversation: <full record>}`. ACL-gated to admin-only because the payload includes the message transcript. ProposalsService subscribes; deletion does not wait on extraction.

### Frontend
- `frontend/src/components/proposals/ProposalsPage.tsx` — list + collapsible detail with markdown-rendered implementation prompt and copy-to-clipboard
- Route `/proposals` (admin-only via dashboard nav `requires_capability: "proposals"`)
- API stubs in `frontend/src/hooks/useWsApi.ts`; types in `frontend/src/types/proposals.ts`

### Key design decisions
- **Core service, not a plugin** — needs always-on observation and proposes things *about* plugins, so it shouldn't be one itself.
- **Observations are persistent** — restarts no longer lose recent signal. Pruning by total cap keeps the table bounded.
- **AI cost is gated at three layers** — periodic schedule (no event-triggered AI), `min_observations_per_cycle` (skip when signal sparse), `max_pending_proposals` (skip when backlog full). The AI is also explicitly told it must return `{"proposals": []}` when nothing is worth proposing.
- **Conversation harvest is incremental** — `details.message_count_at_summary` lets us detect "no new content since last summary" and skip the AI call. An empty extraction still records a placeholder so we don't re-process the same content.
- **Pre-delete extraction is fire-and-forget** from the deleter's POV — the archiving event fires before the storage delete and ProposalsService's handler does its own AI call. The deleter doesn't wait.
- **Source-typed prompt grouping** — the reflection prompt groups observations by `source_type` so the AI can weight an in-chat note from Gilbert higher than a raw event count.

### Phase-2 follow-up (not yet implemented)
- **Safe-mode boot** — once we start auto-loading AI-authored plugin code, the `gilbert.sh` supervisor must support a "skip runtime-installed plugins" exit code so a broken plugin can't brick startup. Today's proposals are inert text records, so this isn't blocking — but it's a hard prerequisite before approving an "auto-implement" pathway.

## Related
- `interfaces/proposals.py`, `core/services/proposals.py`, `interfaces/acl.py`
- `core/services/ai.py` (publishes `chat.conversation.archiving`)
- `core/app.py` (registration), `core/services/web_api.py` (nav entry)
- [Service System](memory-service-system.md), [Capability Protocols](memory-capability-protocols.md), [Backend Pattern](memory-backend-pattern.md)
- [Configuration Service](memory-configuration-service.md), [Scheduler Service](memory-scheduler-service.md), [Event System](memory-event-system.md)
