# Agent Messaging — Phase 4: Multi-Agent Goals Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** First-class `Goal` entity with one or more agent assignees (DRIVER / COLLABORATOR / REVIEWER). Each goal owns a war-room conversation. Seven new agent tools (`goal_create / assign / unassign / handoff / post / status / summary`) plus the matching WS RPCs and a Kanban+war-room SPA. **Acceptance:** create a goal with two assignees → both see the war room, posting visible to both, handoff transfers DRIVER, status change (including → COMPLETE) is DRIVER-only.

**Architecture:** Entities + management methods live on `AgentService` (already the multi-agent orchestrator). New collections: `goals`, `goal_assignments`. War-room conversations reuse the existing `ai_conversations` collection — they're created with `metadata={"goal_id": ..., "kind": "war_room"}` so consumers can query for them. Agents see active assignments via a new system-prompt block (`ACTIVE ASSIGNMENTS:` listing each goal + role + recent war-room snippet). `goal_post` writes a USER-role row to the war-room conv with `metadata.sender = {kind, id, name}` and signals each `mention` (Phase 2 inbox machinery). DRIVER-only operations enforced via assignment lookup.

**Out of scope:**
- Deliverables / dependencies — Phase 5.
- Cross-user — Phase 6.
- Goal-level cost cap enforcement (rows have the field; no automatic disable).
- Editing the `name`/`description` of an existing goal beyond `set_goal_status`.

---

## File Structure

**Modify:**
- `src/gilbert/interfaces/agent.py` — add `Goal`, `GoalAssignment`, `GoalStatus`, `AssignmentRole` dataclasses + enums. Extend `AgentProvider` protocol with goal-management methods.
- `src/gilbert/core/services/agent.py` — collections, CRUD, tools, WS RPCs, system-prompt assignment block.
- `src/gilbert/interfaces/acl.py` — add `"goals.": 100`.
- `frontend/src/types/agent.ts` — Goal/GoalAssignment/AssignmentRole/GoalStatus types.
- `frontend/src/api/agents.ts` (or new `frontend/src/api/goals.ts`) — React Query hooks.
- `frontend/src/App.tsx` — register `/goals`, `/goals/:goalId` routes.
- `frontend/src/components/agent/AgentDetailPage.tsx` — goal-context badges if any (probably not).
- `.claude/memory/memory-agent-service.md` — append "Phase 4 — Goals" subsection.

**Create:**
- `tests/unit/test_goals.py` — entity model tests (round-trip).
- `tests/unit/test_goal_assignments.py` — assign / unassign / handoff / RBAC.
- `tests/unit/test_war_room_acl.py` — DRIVER-only for `goal_status`; non-assignee can't post.
- `tests/unit/test_goal_tools.py` — agent-facing tools (`goal_create`, etc.).
- `frontend/src/components/goals/GoalsListPage.tsx`
- `frontend/src/components/goals/GoalKanban.tsx`
- `frontend/src/components/goals/GoalCard.tsx`
- `frontend/src/components/goals/WarRoomPage.tsx`
- `frontend/src/components/goals/AssigneesStrip.tsx`

---

## Tasks

### Task 1: Backend entities

Add to `src/gilbert/interfaces/agent.py`:

```python
class GoalStatus(StrEnum):
    NEW = "new"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETE = "complete"
    CANCELLED = "cancelled"

class AssignmentRole(StrEnum):
    DRIVER = "driver"
    COLLABORATOR = "collaborator"
    REVIEWER = "reviewer"

@dataclass
class Goal:
    id: str
    owner_user_id: str
    name: str
    description: str
    status: GoalStatus
    war_room_conversation_id: str
    cost_cap_usd: float | None
    lifetime_cost_usd: float
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

@dataclass
class GoalAssignment:
    id: str
    goal_id: str
    agent_id: str
    role: AssignmentRole
    assigned_at: datetime
    assigned_by: str         # agent_id or "user:<user_id>"
    removed_at: datetime | None
    handoff_note: str
```

Extend the `AgentProvider` protocol with method stubs for the methods listed in Task 2.

Tests in `tests/unit/test_goals.py`:
- Round-trip Goal and GoalAssignment dataclasses.
- Enum values match spec.

Commit: `goals: entity model + protocol additions`

---

### Task 2: Backend goal CRUD + assignments

Add to `AgentService`:

Collections: `_GOALS_COLLECTION = "goals"`, `_GOAL_ASSIGNMENTS_COLLECTION = "goal_assignments"`.

Serializers `_goal_to_dict / _goal_from_dict / _goal_assignment_to_dict / _goal_assignment_from_dict` mirroring the existing pattern.

Methods (with full signatures):

```python
async def create_goal(
    self,
    *,
    owner_user_id: str,
    name: str,
    description: str = "",
    cost_cap_usd: float | None = None,
    assign_to: list[tuple[str, AssignmentRole]] | None = None,  # (agent_name, role)
    assigned_by: str = "user:?",
) -> Goal: ...

async def get_goal(self, goal_id: str) -> Goal | None: ...

async def list_goals(self, *, owner_user_id: str | None = None) -> list[Goal]: ...

async def update_goal_status(
    self,
    goal_id: str,
    status: GoalStatus,
) -> Goal: ...

async def list_assignments(
    self,
    *,
    goal_id: str | None = None,
    agent_id: str | None = None,
    active_only: bool = True,
) -> list[GoalAssignment]: ...

async def assign_agent_to_goal(
    self,
    *,
    goal_id: str,
    agent_id: str,
    role: AssignmentRole,
    assigned_by: str,
    handoff_note: str = "",
) -> GoalAssignment: ...

async def unassign_agent_from_goal(
    self,
    *,
    goal_id: str,
    agent_id: str,
) -> GoalAssignment: ...

async def handoff_goal(
    self,
    *,
    goal_id: str,
    from_agent_id: str,
    to_agent_id: str,
    new_role_for_from: AssignmentRole = AssignmentRole.COLLABORATOR,
    note: str = "",
) -> tuple[GoalAssignment, GoalAssignment]: ...
```

Behavior:

- `create_goal` creates the goal row + a war-room conversation (write to `ai_conversations` collection with `{"title": name, "user_id": owner_user_id, "messages": [], "metadata": {"goal_id": goal_id, "kind": "war_room"}, "created_at": ..., "updated_at": ...}`). Stamps `war_room_conversation_id` onto the goal. Then creates an assignment for each `(agent_name, role)`. The first assignee defaults to DRIVER if none was specified.
- `assign_agent_to_goal` rejects if there's already an active assignment for the same agent on the same goal (idempotency: return existing if same role).
- `handoff_goal` is a transaction: marks the from-driver's assignment as COLLABORATOR (not removed); creates a new DRIVER assignment for the to-agent. Both rows' `handoff_note` gets the supplied note. Asserts from-agent is currently DRIVER.
- All goal-mutation methods require the caller (passed via `assigned_by`) to be either the owner-user or a DRIVER on the goal — enforce in the WS layer (see Task 4).
- Publishes events: `goal.created`, `goal.updated`, `goal.deleted` (deletion may come later — not in Phase 4 scope), `goal.assignment.changed`, `goal.status.changed`.

Tests in `tests/unit/test_goals.py`:
- `test_create_goal_creates_war_room` — assert conv row exists with the metadata.
- `test_create_goal_with_assignees` — first → DRIVER, rest → spec'd roles.
- `test_assign_agent_idempotent` — same agent + same role → returns existing.
- `test_handoff_swaps_driver` — A=DRIVER, B=COLLABORATOR; handoff(A→B); A=COLLABORATOR, B=DRIVER.
- `test_unassign_marks_removed_at` — assignment row gets `removed_at` populated, not deleted.
- `test_status_event_published` — subscribe to `goal.status.changed`; flip status; assert event fires.

Commit: `goals: AgentService CRUD + assignments + war-room conv`

---

### Task 3: Backend tools

Add seven new tools — all under `slash_namespace="agents"`:

- `goal_create(name, description?, assign_to?, cost_cap_usd?)` — caller must be the owner; creates goal + assignments. Resolves `assign_to` strings (peer names) via `_load_peer_by_name`.
- `goal_assign(goal_id, agent_name, role)` — DRIVER-only. Resolves agent_name via `_load_peer_by_name`.
- `goal_unassign(goal_id, agent_name)` — DRIVER-only or self-unassign.
- `goal_handoff(goal_id, target_name, role?, note?)` — current DRIVER only. `role` defaults `driver`; the from-driver becomes COLLABORATOR.
- `goal_post(goal_id, body, mention?)` — assignee-only. Writes user-role message to war-room conv (append to `messages` array on `ai_conversations` row, with `metadata.sender = {kind: "agent", id, name}`). For each name in `mention[]`, fire a `_signal_agent` of kind `"inbox"` with body `[mentioned in war room <goal_name>]: {short body}`.
- `goal_status(goal_id, new_status)` — DRIVER-only. Calls `update_goal_status`.
- `goal_summary(goal_id)` — assignee-only. Returns JSON: `{name, description, status, assignees: [{agent_name, role}], recent_posts: [{author_name, body, ts}], lifetime_cost_usd, is_dependency_blocked: false}` (`is_dependency_blocked` is always `false` until Phase 5).

`goal_post` joins `_CORE_AGENT_TOOLS`. The other six don't (operators may want to pin them via `tools_allowed`).

For each tool: definition + handler (`_exec_*`). Reuse `_load_peer_by_name` for name resolution. Reject cross-owner reach with `PermissionError` → error string.

Tests in `tests/unit/test_goal_tools.py`:
- `test_goal_create_via_tool` — tool returns goal_id; war-room conv exists.
- `test_goal_post_writes_to_war_room` — assert message appended; non-assignee blocked.
- `test_goal_post_mentions_signal_targets` — mentioned agent has an InboxSignal row.
- `test_goal_status_driver_only` — non-driver assignee gets error string; driver succeeds.
- `test_goal_handoff_via_tool` — DRIVER A handoff to B; assert both rows updated.

Commit: `goals: agent tools (create / assign / unassign / handoff / post / status / summary)`

---

### Task 4: Backend WS RPCs

Add handlers (all `goals.*`):

```
goals.create(name, description?, assign_to?, cost_cap_usd?)
goals.list(owner_user_id?)
goals.get(goal_id)
goals.update_status(goal_id, status)
goals.assignments.list(goal_id?, agent_id?, active_only?)
goals.assignments.add(goal_id, agent_id, role)
goals.assignments.remove(goal_id, agent_id)
goals.assignments.handoff(goal_id, from_agent_id, to_agent_id, note?)
goals.summary(goal_id)
goals.posts.list(goal_id, limit?)  // returns recent war-room messages
```

Per-user RBAC: caller must own the goal OR be an admin. For `goals.summary` and `goals.posts.list`, also allow callers who own an assigned agent (i.e., a peer's owner can read the war room iff they have an assignee — but Phase 4 is same-owner only, so this collapses to "owner only").

Add `"goals.": 100` to `interfaces/acl.py`.

Tests in `tests/unit/test_goals.py` or sibling — owner-only enforcement, basic round-trips for each handler.

Commit: `goals: WS RPCs (goals.*)`

---

### Task 5: Backend system-prompt active-assignments block

In `_build_system_prompt`, after the LONG_TERM memory block, add:

```python
assignments = await self.list_assignments(agent_id=a.id, active_only=True)
if assignments:
    blocks: list[str] = []
    for asgn in assignments:
        goal = await self.get_goal(asgn.goal_id)
        if goal is None:
            continue
        recent = await self._recent_war_room_posts(asgn.goal_id, limit=10)
        recent_block = "\n".join(
            f"  {p['author_name']}: {p['body']}" for p in recent
        ) or "  (no posts yet)"
        blocks.append(
            f"- Goal '{goal.name}' (id={goal.id}) "
            f"[role={asgn.role.value}, status={goal.status.value}]\n"
            f"{recent_block}"
        )
    parts.append("ACTIVE ASSIGNMENTS:\n" + "\n\n".join(blocks))
```

Implement `_recent_war_room_posts(goal_id, limit=10)` that loads the war-room conv and returns the last `limit` user-role messages, each with `{author_name, body, ts}`.

Tests in `tests/unit/test_goals.py`:
- `test_system_prompt_includes_assignments` — agent A1 with one DRIVER assignment; build prompt; assert `ACTIVE ASSIGNMENTS:` block appears with the goal name + role.

Commit: `goals: active-assignments block in agent system prompt`

---

### Task 6: Frontend types + API

Add to `frontend/src/types/agent.ts`:

```typescript
export type GoalStatus = "new" | "in_progress" | "blocked" | "complete" | "cancelled";
export type AssignmentRole = "driver" | "collaborator" | "reviewer";

export interface Goal {
  _id: string;
  owner_user_id: string;
  name: string;
  description: string;
  status: GoalStatus;
  war_room_conversation_id: string;
  cost_cap_usd: number | null;
  lifetime_cost_usd: number;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface GoalAssignment {
  _id: string;
  goal_id: string;
  agent_id: string;
  role: AssignmentRole;
  assigned_at: string;
  assigned_by: string;
  removed_at: string | null;
  handoff_note: string;
}

export interface WarRoomPost {
  author_id: string;
  author_name: string;
  author_kind: "agent" | "user";
  body: string;
  ts: string;
}

export interface GoalSummary {
  goal: Goal;
  assignees: Array<{ agent_id: string; agent_name: string; role: AssignmentRole }>;
  recent_posts: WarRoomPost[];
  is_dependency_blocked: boolean;
}
```

Add `frontend/src/api/goals.ts` (or extend `agents.ts` with goal hooks):
- `useGoals(ownerUserId?)`, `useGoal(goalId)`, `useGoalSummary(goalId)`, `useGoalAssignments(goalId)`, `useGoalPosts(goalId, limit?)`.
- Mutations: `useCreateGoal`, `useUpdateGoalStatus`, `useAssignAgentToGoal`, `useUnassignAgent`, `useHandoffGoal`.

Each mutation invalidates relevant query keys.

Commit: `goals (frontend): types + API hooks`

---

### Task 7: Frontend Kanban + GoalCard

`frontend/src/components/goals/GoalCard.tsx`:
- Header: name, status pill.
- Body: assignee avatars (overlapped circles) using `<AgentAvatar size="xs">`, deliverable count placeholder (Phase 5: "—" for now), dependency-blocked badge (always false in Phase 4).
- Wraps a `<Link>` to `/goals/<goal._id>`.

`frontend/src/components/goals/GoalKanban.tsx`:
- Five columns (NEW / IN_PROGRESS / BLOCKED / COMPLETE / CANCELLED).
- Each column lists `GoalCard` instances filtered by status.
- Drag between columns calls `useUpdateGoalStatus`.
  - Use `react-dnd` if it's already in `package.json`; else use HTML5 drag events directly. **Don't add new deps.**

`frontend/src/components/goals/GoalsListPage.tsx`:
- Header: page title + "New goal" button → opens a `<Dialog>` with `name`, `description`, multi-select assignee dropdown (read peers from `useAgents()`).
- Below: `<GoalKanban>`.

Subscribe to `goal.created`, `goal.updated`, `goal.status.changed`, `goal.assignment.changed` via `useEventBus` to refresh.

Commit: `goals (frontend): GoalsListPage + Kanban + GoalCard`

---

### Task 8: Frontend WarRoomPage

`frontend/src/components/goals/WarRoomPage.tsx`:
- Header: name, status, lifetime_cost_usd, "Handoff" / "Status" / "Add assignee" buttons.
- AssigneesStrip: avatars + role chips, "+" opens dialog to add a peer agent.
- Main: scrollable list of war-room posts (use `useGoalPosts`); render each as `<author_name>: <body>` with relative time.
- Right rail (placeholder): "Deliverables — Phase 5" / "Dependencies — Phase 5".
- Subscribe to `goal.updated`, `goal.assignment.changed`, and `chat.message.appended` (or whatever event the war-room conv emits when a new post lands — confirm by reading existing chat events).

For Phase 4, don't ship a composer (the user can post via `goal_post` from the agent or via a future Phase 4-bis SPA composer). Note this as "Open question".

Add the `/goals` and `/goals/:goalId` routes to `App.tsx`. Add a "Goals" nav entry to `AppShell` (look at existing nav entries for the pattern).

Commit: `goals (frontend): WarRoomPage + routes + nav`

---

### Task 9: Memory file update

Append "Phase 4 — Multi-agent goals" subsection covering entities, tools, war-room mechanic, prompt-block.

Commit: `docs(memory): Phase 4 — multi-agent goals`

---

### Task 10: Verification

- `uv run pytest -x`
- `uv run ruff check src/gilbert/core/services/agent.py src/gilbert/interfaces/agent.py tests/unit/test_goals.py tests/unit/test_goal_assignments.py tests/unit/test_war_room_acl.py tests/unit/test_goal_tools.py`
- `uv run mypy src/gilbert/core/services/agent.py src/gilbert/interfaces/agent.py`
- `npm run --workspace frontend tsc -b` (or node-direct tsc)
- Architecture audit pass.

---

## Test Strategy

| Category | Coverage |
|---|---|
| Entity round-trip | Goal + GoalAssignment dataclass round-trip via storage. |
| War-room conv | `create_goal` creates the conv with metadata; war_room_conversation_id stamped. |
| Assignments | Idempotent assign; unassign marks removed_at; handoff swaps roles atomically. |
| Tools | `goal_create / assign / handoff / post / status` happy paths + RBAC blocked paths. |
| War-room ACL | Non-assignee `goal_post` blocked; non-DRIVER `goal_status` blocked; assignee can read summary. |
| System prompt | Active assignments block appears with goal name + role + recent posts. |
| WS RPCs | Owner-only enforcement; cross-owner blocked. |
| Events | `goal.created`, `goal.status.changed`, `goal.assignment.changed` all publish. |
| Frontend | tsc clean. Manual smoke via the SPA after backend is up. |

---

## Open Questions / Future

- **War-room composer in SPA.** Phase 4 ships the read-only war room. A "post message" composer in WarRoomPage would let humans participate without going through an agent — a follow-up.
- **Goal deletion / cascade.** `delete_goal` is not in Phase 4 scope; goals are CANCELLED, not deleted. A purge tool comes later.
- **Conv-row write contract.** `goal_post` mutates `ai_conversations.<id>.messages` directly. A more principled approach would call into `AIService` via a capability protocol method `append_message_to_conversation(...)`. For Phase 4 we accept the direct write; Phase 5+ may hoist.
- **Cost rollup from assignee runs.** When an agent runs and incurs cost, that should also accumulate onto the goal it's working on (`Goal.lifetime_cost_usd`). Phase 4 stamps the field but doesn't auto-roll-up (no run→goal linkage exists yet). Add later.
