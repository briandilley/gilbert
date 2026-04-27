# Proposals Service

## Summary
Autonomous self-improvement reflector. Subscribes to the event bus, summarizes activity into a ring buffer, periodically asks the AI to propose new plugins / services / config changes, and persists structured records into the `proposals` entity collection for admin triage.

## Details

### Core file
- `src/gilbert/core/services/proposals.py` — `ProposalsService`
- `src/gilbert/interfaces/proposals.py` — `ProposalsProvider` capability protocol + status / kind constants
- Registered in `core/app.py` alongside other optional services
- ACL: `proposals.` prefix → admin-only (level 0) in `interfaces/acl.py`; event prefix `proposal.` also admin-only

### Capability declarations
- `capabilities = {"proposals", "ws_handlers"}`
- `optional = {"entity_storage", "event_bus", "scheduler", "ai_chat", "configuration"}` — degrades gracefully when any are missing
- `events = {"proposal.created", "proposal.status_changed"}`
- `toggleable = True`

### Lifecycle: observation → reflection → triage
1. **Observation** (passive, zero AI cost) — `_on_event` is subscribed to the event-bus patterns in `_DEFAULT_OBSERVATION_PATTERNS` (failed tool calls, errors, doorbell/presence/inbox/etc). Each event is summarized to a single-line `_Observation` and pushed into a bounded `deque`.
2. **Reflection** — runs only on the scheduler (default 6h via `Schedule.every(...)`) or on a manual trigger via `proposals.trigger_reflection` WS frame / `trigger_reflection` config action. The reflector:
   - Gates on `min_observations_per_cycle` (skipped if buffer hasn't grown enough since the last cycle; bypassed by manual trigger).
   - Gates on `max_pending_proposals` (skipped — even when manual — when the unreviewed backlog is full).
   - Builds a context = grouped event counts + active service inventory + recent proposals (for dedup).
   - Calls `AISamplingProvider.complete_one_shot(tools_override=[])` with `_REFLECTION_SYSTEM_PROMPT` instructing the model to return `{"proposals": []}` when there's nothing worth proposing.
   - Parses the JSON response (tolerant of fenced blocks / surrounding prose), validates each entry via `_build_record` (must have title + spec + implementation_prompt), persists, publishes `proposal.created`.
3. **Triage** — admins use the `/proposals` page to view, add notes, change status (`proposed → approved/rejected/implemented/archived`), or delete. State changes publish `proposal.status_changed`.

### Proposal record shape
Stored in entity collection `proposals` (also `PROPOSALS_COLLECTION` constant). Key fields:
- Identity: `_id`/`id`, `title`, `summary`, `kind`, `target`, `status`
- Provenance: `motivation`, `evidence` (event_type/summary/occurred_at/count), `ai_profile_used`, `reflection_cycle_id`, `created_at`, `updated_at`
- Spec: `spec` (free-form dict — overview, interfaces, data_model, config_params, ws_handlers, ai_tools, events, dependencies, files_to_create/modify, tests), `implementation_prompt` (self-contained — paste into a fresh Claude session and it implements without seeing this conversation), `impact`, `risks`, `acceptance_criteria`, `open_questions`
- Triage: `admin_notes` (list of `{author_id, note, added_at}`)

### WS RPC handlers (all admin-only)
- `proposals.list` — `{status?, kind?, limit?}` → `{proposals, available_statuses, available_kinds}`
- `proposals.get` — `{proposal_id}` → `{proposal}`
- `proposals.update_status` — `{proposal_id, status}`
- `proposals.add_note` — `{proposal_id, note}`
- `proposals.delete` — `{proposal_id}`
- `proposals.trigger_reflection` — `{}` → `{created}`

### Configuration (namespace `proposals`, category `Intelligence`)
- `enabled` (bool, default true)
- `reflection_interval_seconds` (int, default 21600 = 6h, restart_required)
- `max_proposals_per_cycle` (int, default 3)
- `observation_buffer_size` (int, default 500)
- `min_observations_per_cycle` (int, default 25) — skip the AI call when signal is too sparse
- `max_pending_proposals` (int, default 10) — backlog cap
- `ai_profile` (str, choices_from `ai_profiles`, default `advanced` — the reflector benefits from a stronger model since it's writing full implementation specs)
- `observation_event_patterns` (array, default `["*"]` — observe everything; narrow only if you need to focus the reflector, restart_required)

### Frontend
- `frontend/src/components/proposals/ProposalsPage.tsx` — list + collapsible detail with markdown-rendered implementation prompt and copy-to-clipboard
- Route `/proposals` (admin-only via dashboard nav `requires_capability: "proposals"`)
- API stubs in `frontend/src/hooks/useWsApi.ts`; types in `frontend/src/types/proposals.ts`

### Key design decisions
- **Core service, not a plugin** — needs always-on observation and proposes things *about* plugins, so it shouldn't be one itself.
- **Reflection is the only AI cost path** — `_on_event` is synchronous and cheap; the AI is invoked at most once per cycle, gated by both signal-floor and backlog-ceiling so low-use installations and busy backlogs both stop spending tokens automatically.
- **The AI is explicitly allowed to return zero proposals** — the system prompt's rule #1 is the load-bearing instruction here. Validated by `TestParseProposalsResponse` cases.
- **Validation discards malformed records** rather than persisting partial garbage. `_build_record` raises `ValueError` for missing title/spec/implementation_prompt; the runner logs and continues.
- **Capability snapshot uses `ServiceEnumerator` runtime check** on the resolver (the `ServiceManager` implements it). Falls back to `(unavailable)` when a different resolver is wired (tests).

### Phase-2 follow-up (not yet implemented)
- **Safe-mode boot** — once we start auto-loading AI-authored plugin code, the `gilbert.sh` supervisor must support a "skip runtime-installed plugins" exit code so a broken plugin can't brick startup. Today's proposals are inert text records, so this isn't blocking — but it's a hard prerequisite before approving an "auto-implement" pathway.

## Related
- `interfaces/proposals.py`, `core/services/proposals.py`, `interfaces/acl.py`
- `core/app.py` (registration), `core/services/web_api.py` (nav entry)
- [Service System](memory-service-system.md), [Capability Protocols](memory-capability-protocols.md), [Backend Pattern](memory-backend-pattern.md)
- [Configuration Service](memory-configuration-service.md), [Scheduler Service](memory-scheduler-service.md), [Event System](memory-event-system.md)
