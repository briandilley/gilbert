# Agent Messaging — Phase 1B: UI Surfaces Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the `/agents` SPA on top of the new `AgentService` (Phase 1A backend), per `docs/superpowers/specs/2026-05-04-agent-messaging-design.md` Section 7 — **UI Surfaces** (agent-only scope; goals/war-rooms ship in Phase 4). The user can list, create, edit, run, and delete agents; pick tools via a grouped checkbox tree; chat with each agent in its personal conversation; browse memories with state/kind/tag filters; manage commitments; and inspect run history. Plugin extension slots (`agent.detail.toolbar`, `agent.detail.settings.tabs`) are scaffolded so plugins can contribute UI without core knowing.

**Architecture:** Two halves.

1. **Backend additions** to `AgentService`: a small set of WS RPCs the SPA needs (`agents.runs.list`, `agents.commitments.{list,create,complete}`, `agents.memories.{list,set_state}`, `agents.tools.{list_available,list_groups}`), event publishing on agent CRUD + run lifecycle so the SPA can react in real time, and an HTTP multipart route for avatar image upload. RBAC inherits the existing `agents.` prefix entry in `interfaces/acl.py` (already at user level) — no new ACL entries needed beyond verifying the new sub-namespaces match.
2. **Frontend rewrite** of `frontend/src/components/agent/` and `frontend/src/types/agent.ts`. New components per spec layout: `AgentAvatar`, `AgentCard`, `AgentsListPage`, `AgentEditForm`, `ToolPicker`, `AgentDetailPage` (tabs: Chat | Settings | Memory | Commitments | Runs), `MemoryBrowser`, `CommitmentsList`, `RunsTable`. Old `AgentChatPage.tsx` and `AgentsPage.tsx` are deleted — they reference the now-removed `agent.goal.*` RPCs and don't model the new entity. The agent's personal conversation is rendered inside the Chat tab using existing chat infrastructure (`ChatTurnList`, `ChatComposer`).

**Tech Stack:** Python 3.12+, `uv run` for backend tests; TypeScript + React + Vite (`npm run --workspace frontend`). The frontend has no unit-test runner today, so frontend verification is `npm run --workspace frontend tsc -b` and a manual smoke test plus the AppShell routes loading without console errors.

**Out of scope for Phase 1B:**
- `/goals` route, kanban, war rooms, deliverables panel, dependencies panel — Phase 4 / Phase 5 (those entities don't exist yet).
- Peer messaging UI (`agent_send_message`, `agent_delegate`, peer-DM badges) — Phase 2.
- Mid-stream interrupt (urgent inbox indicator beyond a simple "you have N inbox signals" count) — Phase 3.
- Dream tab — Phase 7.
- Cross-user agent visibility — Phase 6.
- Frontend unit tests (no runner exists). Smoke tests + `tsc` are the guardrail.

**Out of scope rationale (the chat tab):** the agent's "personal conv" is just an existing chat conversation. We reuse the existing `ChatPage`-style rendering rather than duplicating it. The Chat tab embeds a thin wrapper that wires the conv id and disables features that don't apply to an agent context (e.g., the AI-profile picker, since the agent's `profile_id` is fixed).

---

## File Structure

**Create — backend:**
- `src/gilbert/web/routes/agent_avatar.py` — HTTP multipart route `POST /api/agents/{agent_id}/avatar`. Accepts an image, persists into the agent's owner workspace (or a service-owned bucket), updates `Agent.avatar_kind="image"` + `Agent.avatar_value="workspace_file:<id>"`. Returns the updated agent dict.
- `tests/unit/test_agents_ws_rpcs.py` — coverage for the new WS RPCs (`agents.runs.list`, `agents.commitments.*`, `agents.memories.*`, `agents.tools.*`).

**Create — frontend:**
- `frontend/src/api/agents.ts` — typed wrappers for all `agents.*` WS frames + the avatar upload HTTP call.
- `frontend/src/components/agent/AgentAvatar.tsx`
- `frontend/src/components/agent/AgentCard.tsx`
- `frontend/src/components/agent/AgentsListPage.tsx`
- `frontend/src/components/agent/AgentEditForm.tsx`
- `frontend/src/components/agent/ToolPicker.tsx`
- `frontend/src/components/agent/AgentDetailPage.tsx` — REPLACES the existing file at the same path.
- `frontend/src/components/agent/MemoryBrowser.tsx`
- `frontend/src/components/agent/CommitmentsList.tsx`
- `frontend/src/components/agent/RunsTable.tsx`

**Modify — backend:**
- `src/gilbert/core/services/agent.py` — add the new WS handlers, add event publishing on create/update/delete + run started/completed, expose `tool_groups` cache.
- `src/gilbert/web/app.py` (or wherever existing HTTP routes mount) — register the new avatar route. Confirm the mount point by grepping for an existing chat-uploads route.
- `src/gilbert/interfaces/acl.py` — verify `agents.` prefix covers the new sub-namespaces (it does); add an HTTP-route ACL entry if the codebase uses one for HTTP routes.

**Modify — frontend:**
- `frontend/src/types/agent.ts` — REWRITE in place to match the new `Agent`, `AgentMemory`, `Commitment`, `Run`, `AgentTrigger` dataclasses.
- `frontend/src/hooks/useWsApi.ts` — remove every `agent.*` (legacy) RPC; do not add `agents.*` here. The new SPA layer should use `frontend/src/api/agents.ts` directly via React Query, matching the `account.ts` / `auth.ts` pattern in `frontend/src/api/`.
- `frontend/src/App.tsx` — register `/agents` (list), `/agents/new` (create form), `/agents/:agentId` (detail). Remove the legacy `/agents` (`AgentChatPage`) and `/agents/list` route entries.

**Delete:**
- `frontend/src/components/agent/AgentChatPage.tsx` — references removed `agent.goal.*` RPCs. The new Chat tab inside `AgentDetailPage` replaces it.
- `frontend/src/components/agent/AgentsPage.tsx` — references the goal model. Replaced by `AgentsListPage`.

**Out-of-pocket changes that may surface during implementation:**
- The legacy `AgentChatPage` is imported by `App.tsx` and possibly `AppShell` nav. Both must be updated when the file is deleted.
- The frontend types file currently exports `Goal`, `GoalCreatePayload`, `GoalStatus`, `TriggerConfig`, `AgentRun` (the old run shape, with `goal_id`). Deleting these will break any plugin frontend that imports them. Grep `std-plugins/*/frontend/` for `from "@/types/agent"` and update or stub those imports.
- Plugin frontends register agent-action handlers via `getAgentActionHandler` (see `frontend/src/lib/agent-actions.ts`) — these are tied to the old `pending_actions` flow on `AgentRun`. The new `Run.pending_actions` shape is identical (per `interfaces/agent.py`), so handlers should keep working; just confirm.
- If any std-plugin contributes a `chat.sidebar.bottom`-style slot that depends on the `Goal` type, port to the new shape or skip if it's a goals-flow integration.

---

## Tasks

### Task 1: Backend — event publishing on agent + run lifecycle

**Files:**
- Modify: `src/gilbert/core/services/agent.py`
- Modify: `tests/unit/test_agent_service.py` — assert events fire.

The SPA needs real-time refresh on `agent.created`, `agent.updated`, `agent.deleted`, `agent.run.started`, `agent.run.completed`. The current Phase 1A code has the `_event_bus` bound but never publishes. Per the "Emit events for state changes" feedback memory, services *should* publish on every state change.

- [ ] **Step 1: Find existing event-publish patterns to copy.**

Run: `grep -rn "self._event_bus.publish\|await self._event_bus.publish" src/gilbert/core/services/ | head -20`

Expected: `notifications.py` and `screens.py` show the canonical idiom — `await bus.publish(Event(event_type=..., data={...}, source="agent"))`.

- [ ] **Step 2: Add helper `_publish(event_type, data)` and use it.**

Add a small private helper at the bottom of `AgentService`:

```python
async def _publish(self, event_type: str, data: dict[str, Any]) -> None:
    if self._event_bus is None:
        return
    from gilbert.interfaces.events import Event
    await self._event_bus.publish(Event(event_type=event_type, data=data, source="agent"))
```

Then:
- After `create_agent` returns the new `Agent`: publish `agent.created` with `{"agent_id": a.id, "owner_user_id": a.owner_user_id}`.
- After `update_agent` returns the updated `Agent`: publish `agent.updated` with `{"agent_id": a.id}`.
- After `delete_agent` returns truthy: publish `agent.deleted` with `{"agent_id": agent_id}`.
- At the start of `_run_agent_internal` (right after persisting the initial `RUNNING` row): publish `agent.run.started` with `{"agent_id": a.id, "run_id": run.id, "triggered_by": triggered_by}`.
- At the end of `_run_agent_internal` (right before `return run`): publish `agent.run.completed` with `{"agent_id": a.id, "run_id": run.id, "status": run.status.value, "cost_usd": run.cost_usd}`.

- [ ] **Step 3: Tests.**

Add to `tests/unit/test_agent_service.py`:

- `test_create_agent_publishes_event` — subscribe to the bus, create, assert the event fires.
- `test_update_agent_publishes_event`.
- `test_delete_agent_publishes_event`.
- `test_run_agent_now_publishes_started_and_completed` — subscribe, mock the AI backend (already done in existing run tests), assert both events fire and carry the run_id.

The bus is an in-process pub/sub (see `core/services/event_bus.py`). Use a list-collecting subscriber:

```python
events: list[Event] = []
unsub = bus.subscribe("agent.run.started", lambda e: events.append(e))
```

- [ ] **Step 4: Run the affected tests.**

`uv run pytest tests/unit/test_agent_service.py -k "publishes" -x`

---

### Task 2: Backend — `agents.runs.list` WS RPC

**Files:**
- Modify: `src/gilbert/core/services/agent.py`
- Modify: `tests/unit/test_agents_ws_rpcs.py` (new file from this task on)

The Runs tab needs a paged list of runs for an agent.

- [ ] **Step 1: Add the handler.**

In `AgentService.get_ws_handlers()`, add `"agents.runs.list": self._ws_runs_list`.

```python
async def _ws_runs_list(self, conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    agent_id = str(params.get("agent_id", ""))
    limit = int(params.get("limit", 50))
    await self._load_agent_for_caller(
        agent_id, caller_user_id=self._caller_user_id(conn),
        admin=self._is_admin(conn),
    )
    runs = await self.list_runs(agent_id=agent_id, limit=limit)
    return {"runs": [_run_to_dict(r) for r in runs]}
```

(`_run_to_dict` already exists for storage serialization. Confirm it produces a SPA-friendly dict; if it embeds `_id` instead of `id`, copy/rename in a wrapper.)

- [ ] **Step 2: Tests.**

`tests/unit/test_agents_ws_rpcs.py` (new file). Use a `FakeConn` with `user_id` + `user_level` attributes (look at `test_agent_service.py` for an existing fake). Assert:
- Owner can list their own runs.
- Non-owner caller raises `PermissionError`.
- `limit` is honored.

`uv run pytest tests/unit/test_agents_ws_rpcs.py -x`

---

### Task 3: Backend — `agents.commitments.*` WS RPCs

**Files:**
- Modify: `src/gilbert/core/services/agent.py`
- Modify: `tests/unit/test_agents_ws_rpcs.py`

The Commitments tab needs list/create/complete. Reuses existing service methods.

- [ ] **Step 1: Add handlers.**

```python
"agents.commitments.list":     self._ws_commitments_list,
"agents.commitments.create":   self._ws_commitments_create,
"agents.commitments.complete": self._ws_commitments_complete,
```

- `_ws_commitments_list(agent_id, include_completed?)` → calls `self.list_commitments(...)`, returns `{"commitments": [_commitment_to_dict(c) for c in cs]}`.
- `_ws_commitments_create(agent_id, content, due_at | due_in_seconds)` → resolves to a datetime and calls `self.create_commitment(...)`, returns `{"commitment": _commitment_to_dict(c)}`.
- `_ws_commitments_complete(commitment_id, note?)` → calls `self.complete_commitment(...)`. Authorization: load the commitment, confirm `agent.owner_user_id == caller_user_id` (admin allowed). Returns `{"commitment": _commitment_to_dict(c)}`.

For all three, route through `_load_agent_for_caller` first.

- [ ] **Step 2: Tests.**

In `tests/unit/test_agents_ws_rpcs.py`: list/create/complete round-trip; cross-owner attempt raises; bad input raises `ValueError`.

---

### Task 4: Backend — `agents.memories.*` WS RPCs

**Files:**
- Modify: `src/gilbert/core/services/agent.py`
- Modify: `tests/unit/test_agents_ws_rpcs.py`

The Memory tab needs filterable list + the ability to flip between SHORT_TERM and LONG_TERM.

- [ ] **Step 1: Add handlers.**

```python
"agents.memories.list":      self._ws_memories_list,
"agents.memories.set_state": self._ws_memories_set_state,
```

- `_ws_memories_list(agent_id, state?, kind?, tags?, q?, limit?)` — extends `search_memory` with optional `kind` filter and `tags` (any-match). If `q` is empty, returns recency-sorted recent memories. Returns `{"memories": [_memory_to_dict(m) for m in ms]}`.
- `_ws_memories_set_state(memory_id, state)` — load the memory, confirm `agent.owner_user_id == caller_user_id`, then `promote_memory(memory_id=..., score=row.score, state=MemoryState(state))`.

If `search_memory` doesn't already support `kind` / `tags` filters, extend it inline (small change — filter on the loaded rows before sorting).

- [ ] **Step 2: Tests.**

Round-trip filters; cross-owner blocked.

---

### Task 5: Backend — `agents.tools.list_available` and `agents.tools.list_groups`

**Files:**
- Modify: `src/gilbert/core/services/agent.py`
- Modify: `tests/unit/test_agents_ws_rpcs.py`

The ToolPicker needs the registered-tool inventory plus the curated UI groups.

- [ ] **Step 1: `agents.tools.list_available`.**

This must reflect the **same** RBAC the agent's run would see. Reuse `AIToolDiscoveryProvider.discover_tools(user_ctx=...)` already implemented by `AIService` (capability `ai_chat`). Bind the discovery capability in `start()` (it's the same object as `self._ai`):

```python
ai_svc = resolver.require_capability("ai_chat")
if not isinstance(ai_svc, AIToolDiscoveryProvider):
    raise RuntimeError("ai_chat capability does not implement AIToolDiscoveryProvider")
self._tool_discovery = ai_svc
```

(Or check `isinstance(self._ai, AIToolDiscoveryProvider)` opportunistically. The existing AIProvider check already runs.)

Handler:

```python
async def _ws_tools_list_available(self, conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    from gilbert.interfaces.auth import UserContext
    caller = self._caller_user_id(conn)
    user_ctx = UserContext.from_user_id(caller)
    discovered = self._tool_discovery.discover_tools(user_ctx=user_ctx)
    # discovered is dict[str, ToolDefinition] or similar — confirm shape
    return {"tools": [
        {"name": t.name, "description": t.description, "provider": getattr(t, "provider", "")}
        for t in discovered.values()
    ]}
```

Confirm the shape of `discover_tools` — look at how `mcp_server_http.py` consumes it, replicate the same access pattern.

- [ ] **Step 2: `agents.tools.list_groups`.**

Returns the cached `tool_groups` config (already in `self._defaults["tool_groups"]`):

```python
async def _ws_tools_list_groups(self, conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"groups": dict(self._defaults.get("tool_groups", {}))}
```

- [ ] **Step 3: Tests.**

In `tests/unit/test_agents_ws_rpcs.py`, mock the `discover_tools` return; assert the SPA shape.

---

### Task 6: Backend — avatar upload HTTP route

**Files:**
- Create: `src/gilbert/web/routes/agent_avatar.py` (or co-locate per existing pattern — confirm by grepping for `chat-uploads` / `notification-attachments`).
- Modify: wherever HTTP routes are mounted (`src/gilbert/web/app.py` or the FastAPI startup in app composition).
- Modify: `tests/unit/test_agent_avatar_route.py` — minimal route test if the codebase has an HTTP test fixture; otherwise smoke-test only.

- [ ] **Step 1: Pattern-match an existing upload route.**

Run: `grep -rn "UploadFile\|multipart" src/gilbert/web/routes/ src/gilbert/web/ 2>/dev/null | head -10`

Find the existing chat upload route (or notification attachment route). Mirror it: accept `UploadFile`, write to the workspace via `WorkspaceProvider` capability, register a `workspace_file` row, return its id.

- [ ] **Step 2: Implement the handler.**

```
POST /api/agents/{agent_id}/avatar
  multipart: file=<image>
  → 200 {"agent": {...}}
```

Resolve the agent via `AgentService._load_agent_for_caller(agent_id, caller_user_id=request.state.user.id, admin=is_admin(request))`. Then write the file, capture the returned `file_id`, set `agent.avatar_kind = "image"`, `agent.avatar_value = f"workspace_file:{file_id}"`. Reuse `update_agent` for the patch so the `agent.updated` event fires.

- [ ] **Step 3: ACL & RBAC.**

If HTTP route ACLs are declared (look at how other routes are gated — e.g., the existing chat upload route), add an entry mirroring `agents.` (user level).

- [ ] **Step 4: Architecture compliance.**

Per the [Web auth allowlist] feedback memory: routes must be added to the auth allowlist when needed and respect tunnel allowlists. Confirm by reading `core/auth.py` allowlist before mounting.

Per the [No private data in tracked files] memory: the avatar bytes go to the workspace (gitignored), not into a tracked path.

---

### Task 7: Frontend — types and API client

**Files:**
- REWRITE: `frontend/src/types/agent.ts`
- Create: `frontend/src/api/agents.ts`
- Modify: `frontend/src/hooks/useWsApi.ts` — remove every `agent.*` (legacy) call. Do **not** add `agents.*` calls there; the new module owns them.

- [ ] **Step 1: Rewrite `frontend/src/types/agent.ts`.**

Match the Python dataclasses (`src/gilbert/interfaces/agent.py`):

```typescript
export type AgentStatus = "enabled" | "disabled";
export type MemoryState = "short_term" | "long_term";
export type RunStatus = "running" | "completed" | "failed" | "timed_out";

export interface Agent {
  id: string;
  owner_user_id: string;
  name: string;
  role_label: string;
  persona: string;
  system_prompt: string;
  procedural_rules: string;
  profile_id: string;
  conversation_id: string;
  status: AgentStatus;
  avatar_kind: "emoji" | "icon" | "image";
  avatar_value: string;
  lifetime_cost_usd: number;
  cost_cap_usd: number | null;
  tools_allowed: string[] | null;
  heartbeat_enabled: boolean;
  heartbeat_interval_s: number;
  heartbeat_checklist: string;
  dream_enabled: boolean;
  dream_quiet_hours: string;
  dream_probability: number;
  dream_max_per_night: number;
  created_at: string;
  updated_at: string;
}

export interface AgentMemory { /* mirror dataclass */ }
export interface Commitment   { /* mirror dataclass */ }
export interface AgentRun     { /* mirror Run; note `agent_id`, no `goal_id` */ }
export interface AgentTrigger { /* mirror dataclass */ }

export interface ToolDescriptor { name: string; description: string; provider: string; }
export type ToolGroupMap = Record<string, string[]>;
```

The old `Goal`, `GoalCreatePayload`, `GoalStatus`, `GoalUpdatePayload`, `TriggerConfig` interfaces are **deleted**. (TriggerConfig may be re-introduced when triggers reach the UI; for Phase 1B the heartbeat is the only trigger and lives directly on `Agent`.)

- [ ] **Step 2: Build `frontend/src/api/agents.ts`.**

Export typed hooks following the existing `frontend/src/api/account.ts` pattern (it uses the WS client directly):

```typescript
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useWebSocket } from "@/hooks/useWebSocket";
import type { Agent, AgentMemory, Commitment, AgentRun, ToolDescriptor, ToolGroupMap } from "@/types/agent";

export function useAgents() { /* agents.list */ }
export function useAgent(agentId: string) { /* agents.get */ }
export function useCreateAgent() { /* agents.create */ }
export function useUpdateAgent() { /* agents.update */ }
export function useDeleteAgent() { /* agents.delete */ }
export function useSetAgentStatus() { /* agents.set_status */ }
export function useRunAgentNow() { /* agents.run_now */ }
export function useAgentDefaults() { /* agents.get_defaults */ }
export function useAgentRuns(agentId: string) { /* agents.runs.list */ }
export function useAgentCommitments(agentId: string, includeCompleted: boolean) { /* agents.commitments.list */ }
export function useCreateCommitment() { /* agents.commitments.create */ }
export function useCompleteCommitment() { /* agents.commitments.complete */ }
export function useAgentMemories(agentId: string, filters: MemoryFilters) { /* agents.memories.list */ }
export function useSetMemoryState() { /* agents.memories.set_state */ }
export function useAvailableTools() { /* agents.tools.list_available */ }
export function useToolGroups() { /* agents.tools.list_groups */ }
export async function uploadAgentAvatar(agentId: string, file: File): Promise<Agent> {
  // POST /api/agents/{agent_id}/avatar — multipart fetch
}
```

Each hook invalidates the appropriate query keys after mutation (e.g., `["agent", agentId]`, `["agents", "list"]`).

- [ ] **Step 3: Strip legacy from `useWsApi.ts`.**

Remove every method that builds a frame whose `type` starts with `agent.` (the legacy namespace). Search-and-delete:

```bash
grep -n "type: \"agent\\." frontend/src/hooks/useWsApi.ts
```

After deletion, run `npm run --workspace frontend tsc -b` and chase the imports. Anything calling the old `api.listGoals` etc. will fail compile — that's the expected demolition signal; those files are scheduled for deletion in Task 14.

- [ ] **Step 4: Verify.**

`npm run --workspace frontend tsc -b` — expect compile errors **only** in the legacy `AgentChatPage.tsx` / `AgentsPage.tsx`, which are deleted in Task 14. (Those files may be temporarily commented out or stubbed if `tsc` blocks downstream tasks.)

---

### Task 8: Frontend — `AgentAvatar` component

**Files:**
- Create: `frontend/src/components/agent/AgentAvatar.tsx`

Single component, renders by `avatar_kind`. Sizes: `xs` (16px), `sm` (24px), `md` (40px), `lg` (96px).

```tsx
type Size = "xs" | "sm" | "md" | "lg";
const SIZE_PX: Record<Size, number> = { xs: 16, sm: 24, md: 40, lg: 96 };

export function AgentAvatar({ agent, size = "md" }: { agent: Agent; size?: Size }) {
  const px = SIZE_PX[size];
  if (agent.avatar_kind === "emoji") return <span style={{ fontSize: px * 0.8 }}>{agent.avatar_value || "🤖"}</span>;
  if (agent.avatar_kind === "icon")  return <LucideIcon name={agent.avatar_value} size={px} />;
  if (agent.avatar_kind === "image") {
    const fileId = agent.avatar_value.replace(/^workspace_file:/, "");
    return <img src={`/api/workspace/files/${fileId}`} width={px} height={px} className="rounded-full object-cover" />;
  }
  return <span style={{ fontSize: px * 0.8 }}>🤖</span>;
}
```

Look up the existing lucide-icon-by-name component (`LucideIcon` or similar — grep for `dynamic icon`); if absent, use a `?` fallback.

- [ ] **Step 1: Create the component.**
- [ ] **Step 2: Confirm tsc.** `npm run --workspace frontend tsc -b`

---

### Task 9: Frontend — `ToolPicker` component

**Files:**
- Create: `frontend/src/components/agent/ToolPicker.tsx`

Grouped checkbox tree backed by `useAvailableTools()` + `useToolGroups()`.

Behavior per spec:
- Core tools (the always-included set) are shown checked-and-disabled. The list of core names is hard-coded in the SPA mirroring `_CORE_AGENT_TOOLS` in `agent.py` (Phase 1: complete_run, request_user_input, notify_user, commitment_create, commitment_complete, commitment_list, agent_memory_save, agent_memory_search, agent_memory_review_and_promote).
- "All tools allowed" toggle at the top: when on, returns `null`; when off, the underlying value is `string[]`.
- Groups expand by default; tools not in any group land in an "Other" group.

Props:

```tsx
{
  value: string[] | null,
  onChange: (next: string[] | null) => void,
}
```

- [ ] **Step 1: Render the checkbox tree.**
- [ ] **Step 2: Honor "all allowed" toggle.**
- [ ] **Step 3: tsc.**

---

### Task 10: Frontend — `AgentEditForm` component (Identity / Persona / Heartbeat / Dreaming / Profile&Cost / Tools)

**Files:**
- Create: `frontend/src/components/agent/AgentEditForm.tsx`

Multi-section form. Pre-fills from `useAgentDefaults()` for create mode; from the loaded `Agent` for edit mode.

Sections (collapsible, all open by default):

1. **Identity** — `name` (slug-validated), `role_label`, avatar picker (emoji input + "Upload image" button calling `uploadAgentAvatar`).
2. **Persona** — three multi-line `<Textarea>` editors for `persona`, `system_prompt`, `procedural_rules`. Show "↗ open in Settings" link to the corresponding `agent.persona_default` etc. config field if the operator wants to edit the default. (Defaults are already exposed as `ai_prompt`-flagged ConfigParams per Phase 1A.)
3. **Heartbeat** — `heartbeat_enabled` (switch), `heartbeat_interval_s` (number with sec/min/hr unit toggle), `heartbeat_checklist` (multi-line).
4. **Dreaming** — `dream_enabled` (switch; disabled with tooltip "Phase 7 — coming soon" since dream-mode runs aren't implemented yet, but the field exists so we can save the desired-state). `dream_quiet_hours` (e.g., `22:00-06:00`), `dream_probability` (0..1 number), `dream_max_per_night`.
5. **Profile & cost** — `profile_id` (select from `useAiProfiles()` — already in `useWsApi.ts`), `cost_cap_usd` (nullable number).
6. **Tools** — `<ToolPicker>` for `tools_allowed`.

Submit calls `useCreateAgent()` (new) or `useUpdateAgent()` (existing). On success, navigate to `/agents/{id}` (create) or stay on `/agents/{id}?tab=settings` (edit) and toast.

- [ ] **Step 1: Build the form skeleton with controlled inputs.**
- [ ] **Step 2: Pre-fill from defaults for create; from agent for edit.**
- [ ] **Step 3: Wire save / cancel.**
- [ ] **Step 4: tsc.**

---

### Task 11: Frontend — `AgentsListPage`, `AgentCard`

**Files:**
- Create: `frontend/src/components/agent/AgentCard.tsx`
- Create: `frontend/src/components/agent/AgentsListPage.tsx`

`AgentCard`: `<AgentAvatar size="sm">`, name, role_label, status pill, last activity (most recent run's `started_at`), cost-to-date vs cap (`$X.XX / $Y.YY`), count of active assignments (in Phase 1B always **0** — no goals yet; render the cell but show `—`).

`AgentsListPage`: header with "New agent" button → navigates to `/agents/new`, then a responsive grid of `AgentCard` linked to `/agents/{id}`.

Subscribe to `agent.created` / `agent.updated` / `agent.deleted` / `agent.run.completed` events via `useEventBus` to invalidate the query.

- [ ] **Step 1: Build `AgentCard`.**
- [ ] **Step 2: Build `AgentsListPage` with grid + event subscriptions.**
- [ ] **Step 3: tsc.**

---

### Task 12: Frontend — `MemoryBrowser`, `CommitmentsList`, `RunsTable`

**Files:**
- Create: `frontend/src/components/agent/MemoryBrowser.tsx`
- Create: `frontend/src/components/agent/CommitmentsList.tsx`
- Create: `frontend/src/components/agent/RunsTable.tsx`

`MemoryBrowser`: filter bar (state pill toggles `short_term | long_term | all`, kind dropdown, tags multi-select, full-text input). Table of `AgentMemory` rows with content, state, kind, tags, created_at. Per-row "Promote" / "Demote" buttons → `useSetMemoryState()`. Bound to `useAgentMemories(agentId, filters)`.

`CommitmentsList`: two sections — Active (default) and Recently Completed (collapsed). Each row: content, due_at (with relative time), "Complete" button (with optional note prompt). Quick-add form at the top: content text + due picker (`due_in_seconds` shorthand: 1h, 1d, custom). Bound to `useAgentCommitments(agentId, includeCompleted)` and `useCreateCommitment` / `useCompleteCommitment`.

`RunsTable`: rows of `AgentRun` with `status` badge, `triggered_by`, `started_at` (relative), duration, cost, rounds, tokens. Click to expand → shows `final_message_text` + `error` if present. Filter by `triggered_by` (`manual | time | event | heartbeat`). Subscribed to `agent.run.started` + `agent.run.completed` for live refresh.

- [ ] **Step 1: `MemoryBrowser`.**
- [ ] **Step 2: `CommitmentsList`.**
- [ ] **Step 3: `RunsTable`.**
- [ ] **Step 4: tsc.**

---

### Task 13: Frontend — `AgentDetailPage` with tabs

**Files:**
- REWRITE: `frontend/src/components/agent/AgentDetailPage.tsx`

Replace the existing file. Layout per spec:

- Header bar: back link, `<AgentAvatar size="md">`, name, role_label, status pill, `Run now` button, `Disable / Enable` toggle, `Delete` icon. Plus `<PluginPanelSlot slot="agent.detail.toolbar" />` for plugin-contributed actions.
- Tabs: `Chat | Settings | Memory | Commitments | Runs` plus `<PluginPanelSlot slot="agent.detail.settings.tabs" />` appended at the end.
- Tab content:
  - **Chat** — wraps the existing chat conversation rendering. Open `agent.conversation_id` (lazy-create on first run if empty by hitting `useRunAgentNow` once with `user_message="hello"`, OR show "No conversation yet — click Run now" placeholder). Reuse the existing chat composer + turn list from `frontend/src/components/chat/`. The composer's submit posts a user-role message into the agent's personal conv via the existing chat WS API (it does *not* directly call `agents.run_now` — typing into the agent's conv is the user's prerogative; the loop wakes when an `InboxSignal` is dispatched; for Phase 1B the simplest behavior is: pressing send posts the message and triggers a `run_now` with `user_message=<text>`).
  - **Settings** — embeds `AgentEditForm` in edit mode.
  - **Memory** — `MemoryBrowser`.
  - **Commitments** — `CommitmentsList`.
  - **Runs** — `RunsTable`.
- Subscribe to `agent.updated` / `agent.deleted` / `agent.run.*` events. Invalidate the loaded agent on `agent.updated`.

- [ ] **Step 1: Build the page skeleton + tabs.**
- [ ] **Step 2: Wire each tab to its component.**
- [ ] **Step 3: Plugin extension slots.**
- [ ] **Step 4: tsc.**

---

### Task 14: Frontend — wire routes, delete legacy, smoke test

**Files:**
- Modify: `frontend/src/App.tsx`
- Delete: `frontend/src/components/agent/AgentChatPage.tsx`
- Delete: `frontend/src/components/agent/AgentsPage.tsx`
- Modify: `frontend/src/components/layout/AppShell.tsx` — confirm the nav entry points at `/agents` (the new list page); update label/icon if needed.

- [ ] **Step 1: Update routes.**

```tsx
<Route path="/agents" element={<AgentsListPage />} />
<Route path="/agents/new" element={<AgentEditForm mode="create" />} />
<Route path="/agents/:agentId" element={<AgentDetailPage />} />
```

Remove `/agents/list` (legacy) and the `:goalId` param route.

- [ ] **Step 2: Delete the legacy components.**

```bash
rm frontend/src/components/agent/AgentChatPage.tsx
rm frontend/src/components/agent/AgentsPage.tsx
```

- [ ] **Step 3: Hunt for stragglers.**

```bash
grep -rn "AgentChatPage\|AgentsPage\|listGoals\|createGoal\|getGoal\|listAgentRuns\|runGoalNow\b" frontend/src/ std-plugins/*/frontend/src/ 2>/dev/null
```

Anything still referencing the old names is either:
- A nav/import in core SPA → update to the new component.
- A plugin frontend → patch the plugin (or stub the import if it's part of an unused integration). If touching a std-plugin, this falls under "commit and push" scope per the [`commit and push` scope] feedback memory.

- [ ] **Step 4: tsc + smoke test.**

```bash
npm run --workspace frontend tsc -b
./gilbert.sh start --foreground
```

In a browser:
1. Navigate to `/agents` — empty state visible, "New agent" button works.
2. Create an agent (set persona + checklist, leave heartbeat default 30 min).
3. Land on `/agents/<id>` — Settings tab shows the saved values.
4. Click "Run now" — Chat tab eventually shows assistant turn (mock-friendly: in dev a stub AI profile may be configured).
5. Verify Memory tab: agent saves a memory via `agent_memory_save` tool during the run; refresh shows it under "Active short-term".
6. Commitments tab: quick-add a commitment; mark complete.
7. Runs tab: see the run we just kicked off, click to expand.
8. Disable the agent → the heartbeat scheduler row goes away (verify in `/scheduler` UI).
9. Delete the agent → list page is empty again.

If the AI backend isn't configured, the chat steps will fail; the SPA must still render every tab without console errors.

---

### Task 15: Tests — backend WS RPCs + integration

**Files:**
- Modify: `tests/unit/test_agents_ws_rpcs.py`
- Modify: `tests/integration/test_agents_e2e.py` (new) — only if integration test pattern exists for WS frames; otherwise skip.

- [ ] **Step 1: Run the full backend suite.**

```bash
uv run pytest tests/unit/test_agent_service.py tests/unit/test_agents_ws_rpcs.py tests/unit/test_agent_memory.py tests/unit/test_commitments.py -x
```

All green. New tests cover:
- Owner-scoped access for every new RPC.
- Non-owner caller raises `PermissionError`.
- Event emission on agent CRUD + run lifecycle (Task 1's tests).

- [ ] **Step 2: Type-check.**

```bash
uv run mypy src/gilbert/core/services/agent.py src/gilbert/web/routes/agent_avatar.py
```

- [ ] **Step 3: Lint.**

```bash
uv run ruff check src/gilbert/core/services/agent.py src/gilbert/web/routes/agent_avatar.py tests/unit/test_agents_ws_rpcs.py
```

- [ ] **Step 4: Architecture audit hooks.**

Per CLAUDE.md, before commit:
- The new HTTP route in `web/routes/agent_avatar.py` must not embed business logic — it should call `AgentService.update_agent` for the patch.
- The new `agents.tools.list_available` handler must use `AIToolDiscoveryProvider` (capability protocol), not isinstance-check the concrete `AIService`.
- All new ConfigParams (none expected in Phase 1B; `tool_groups` already exists) follow the `ai_prompt=True` rule for any non-trivial AI prompt — N/A here since this phase ships UI only.
- Update `.claude/memory/memory-agent-service.md` if any class shape changed (likely: list of WS handlers grew, mention briefly).

---

### Task 16: Memory + spec hygiene

**Files:**
- Modify: `.claude/memory/memory-agent-service.md`
- Modify: `docs/superpowers/specs/2026-05-04-agent-messaging-design.md` (only if Phase 1B's implementation revealed a deviation that the spec should record; otherwise leave it alone — the spec is the design, not the build log).

- [ ] **Step 1: Update the agent-service memory.**

Append a short note under the existing structure: "Phase 1B added WS handlers `agents.runs.list`, `agents.commitments.{list,create,complete}`, `agents.memories.{list,set_state}`, `agents.tools.{list_available,list_groups}`, plus event publication on agent CRUD + run lifecycle. HTTP avatar upload route lives at `web/routes/agent_avatar.py`."

- [ ] **Step 2: Confirm `MEMORIES.md` index entry.**

The Phase 1A commit `39a3322 docs(memory): AgentService memory file + index update` already added the memory file to the index. No new file in 1B — just an update to the existing one.

---

### Task 17: Final verification — full test suite + ruff + tsc

- [ ] **Step 1.** `uv run pytest -x`
- [ ] **Step 2.** `uv run ruff check src/ tests/`
- [ ] **Step 3.** `uv run mypy src/`
- [ ] **Step 4.** `npm run --workspace frontend tsc -b`
- [ ] **Step 5.** Manual smoke test per Task 14 Step 4.
- [ ] **Step 6.** Architecture-violation checklist scan (per CLAUDE.md "Architecture Violation Checklist" section, refer to `.claude/memory/memory-architecture-checklist.md`):
  - Layer imports clean (no `web/` → `integrations/`, no plugin → `core/`).
  - No concrete-class isinstance checks; AI tool discovery uses `AIToolDiscoveryProvider`.
  - Slash commands declared on every tool that exposes one (Phase 1B doesn't add new tools — confirm).
  - README freshness — touch the root README + `std-plugins/README.md` if either references the deleted `AutonomousAgentService` or the removed `agent.*` RPCs (Phase 1A may have cleaned this; verify).

---

## Test Strategy

| Category | Coverage |
|---|---|
| **Backend unit (new)** | `tests/unit/test_agents_ws_rpcs.py` — every new WS RPC: owner-only, payload shape, edge cases. Plus `test_agent_service.py` additions for event emission. |
| **Backend mypy / ruff** | All new handlers + the avatar route. |
| **Frontend tsc** | Strict `tsc -b` — the codebase has no Vitest/Jest runner, so the type-checker is the unit-test substitute. |
| **Manual smoke** | The 9-step browser walk in Task 14 Step 4 (create → settings → run → memory → commitments → runs → disable → delete). |
| **Architecture audit** | Run the checklist from `.claude/memory/memory-architecture-checklist.md` before commit. |
| **Multi-user isolation** | Backend tests cover the owner-only RPC paths. Frontend doesn't expose a cross-owner surface in Phase 1B (no admin-list-as-other-user UI yet). |

---

## Open Questions / Future

- **Avatar upload pipeline.** The plan picks "store in workspace, reference via `workspace_file:<id>`" since the existing chat upload route does the same. If that pipeline doesn't exist yet (`grep -rn "UploadFile" src/gilbert/web/`), Task 6 may need to introduce a small dedicated avatar bucket. Defer to whoever implements Task 6.
- **Default avatar randomization.** The `default_avatar_value` config currently defaults to `🤖`. Operators may want a per-agent randomized emoji. Out of scope; recorded for future polish.
- **Tab-deep-linking.** `AgentDetailPage` should accept `?tab=memory` etc. so the SPA can link directly. The base task says "support deep-linking via search param"; if time pressure surfaces, this is a follow-up.
- **Performance** — the Memory and Runs tabs currently fetch all rows up to limit. If an agent accumulates thousands of memories/runs, paginate. Track in a future card; not Phase 1B.
- **`agent.inbox.received` event** — listed in spec Section 7's real-time table but tied to peer messaging (Phase 2). Phase 1B does NOT subscribe; the UI only reacts to `agent.run.*` and `agent.created/updated/deleted`. Phase 2 will add the subscription.

---

## Related

- Spec: `docs/superpowers/specs/2026-05-04-agent-messaging-design.md` (Section 7 — UI Surfaces)
- Phase 1A plan: `docs/superpowers/plans/2026-05-04-agent-messaging-phase-1a-backend.md`
- `.claude/memory/memory-agent-service.md`
- `.claude/memory/memory-capability-protocols.md`
- `.claude/memory/memory-architecture-checklist.md`
- Existing chat rendering reused for the Chat tab: `frontend/src/components/chat/`
- Plugin extension slot infrastructure: `frontend/src/components/PluginPanelSlot.tsx`
