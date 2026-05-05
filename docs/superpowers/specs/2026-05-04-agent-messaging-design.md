# Agent Messaging & Multi-Agent Workflow Design

## Summary

Replace the current `AutonomousAgentService`'s "Goal-as-identity" model with a richer multi-agent system. **Agents** become first-class durable identities (persona, system prompt, procedural rules, heartbeat, dreaming, memory, commitments, avatar, tool allowlist). **Goals** become shared work items that one or more agents can be assigned to, with explicit roles (DRIVER / COLLABORATOR / REVIEWER), handoff support, deliverable production, and inter-goal dependencies that drive automatic wake-up. Agents can message each other directly, delegate work synchronously, and post in goal "war rooms." Delivery semantics start as **queue mode** (drained between AI rounds, the proven `_pending_user_messages` pattern) and gain **mid-stream interrupt** in a follow-up phase.

Heavily inspired by OpenClaw (rich autonomous agent identity with heartbeat, dreaming, memory promotion, commitments) and CrewAI (multi-agent goal coordination with explicit roles), ported into Gilbert's entity-storage model — no markdown files on disk, every concept is an entity row queryable via `StorageProvider`.

## Goals

- Agents are addressable, persistent identities with rich autonomous behavior — they don't just exist while a goal is running.
- Multiple agents can collaborate on a single goal, both sequentially (handoff) and concurrently (parallel collaborators).
- Inter-goal dependencies declare "agent B's work is unblocked when agent A's deliverable is READY." When a deliverable flips to READY, dependent agents wake automatically.
- Per-agent tool gating: a research agent doesn't need `lights.set`; a household agent doesn't need `mcp.*`. Allowlists prune the surface.
- The whole system uses Gilbert's existing infrastructure (entity storage, conversations + workspaces, capability protocols, scheduler, event bus) without introducing new persistence layers.
- Cross-user isolation is the default — same-owner only in Phase 1–5. Cross-user is opt-in and deferred to Phase 6.

## Non-Goals

- **Migrating existing data.** The user has authorized nuking the old `AutonomousAgentService` data. Phase 1 deletes the old service and its rows; we cherry-pick design ideas without preserving any specific row.
- **File-based agent state (SOUL.md / MEMORY.md / etc.).** Markdown files on disk are not used. All agent state is entity rows.
- **Mid-stream interrupt in Phase 1–2.** Queue mode ships first; interrupt comes in Phase 3.
- **Cross-user agent collaboration in Phase 1–5.** Phase 6 sketched only.
- **Dreaming / memory promotion in Phase 1.** Heartbeat is core; dreaming is opt-in and slated for Phase 7.
- **Project workspaces beyond what conversations already provide.** War-room conversations + the existing `workspace_files` registry cover all sharing needs.

## Concept Mapping (Gilbert ↔ OpenClaw)

We adopt OpenClaw's "agent is durable workspace-bound identity with rich autonomous behaviors" wholesale, ported to entity storage. We *add* multi-agent goal coordination (assignments, deliverables, dependencies), inspired by CrewAI/AutoGen, that OpenClaw does not have.

| OpenClaw | Gilbert | Purpose |
|---|---|---|
| `SOUL.md` (file) | `Agent.persona` (field) | Long-form character / values / behavioral boundaries; durable identity prompt prepended to every run. |
| `IDENTITY.md` (file) | `Agent.{name, role_label, id}` (fields) | Public-facing metadata for routing and addressing. |
| `AGENTS.md` (file) | `Agent.procedural_rules` (field) | Workflow rulebook: "what do you do and how" — operating procedures, file/memory rules. |
| `USER.md` (file) | existing per-user `user_memory` collection (read via tools) | Operator context — preferences, constraints. Already exists. |
| `TOOLS.md` (file) | existing `ToolDefinition.description` (auto-discovered) | Tool inventory + usage notes. Built from each tool's declaration; no per-agent file. |
| `HEARTBEAT.md` (file) | `Agent.heartbeat_checklist` (field) + heartbeat `AgentTrigger` row | Stable per-tick checklist for the heartbeat run. The trigger schedules the run; the field supplies content. |
| `MEMORY.md` (file) | `AgentMemory` rows where `state=LONG_TERM` | Durable facts, preferences, decisions. Retrieved into prompt context. |
| `DREAMS.md` (file) | `AgentMemory` rows where `kind="dream"` | Dream-diary entries from quiet-hour runs. |
| `memory/YYYY-MM-DD.md` (file) | `AgentMemory` rows where `kind="daily"`, date-tagged | Daily running notes. Recent days loaded into context automatically. |
| **COMMITMENTS** (concept) | `Commitment` entity | Opt-in short-lived follow-up reminders, surfaced in heartbeats when due. |
| **HEARTBEAT** (behavior) | heartbeat `AgentTrigger` (`type="heartbeat"`) | Periodic main-session turn at configurable interval (default 30 min). Real `Run` records, not background tasks. |
| **DREAMING** (behavior) | gated dream-mode runs (heartbeat with `dream` mode swap) | Quiet-hours freeform thinking. Time-window + nightly-cap + probability gate. Output: `kind="dream"` memories. |
| **Memory promotion sweep** (behavior) | `agent_memory_review_and_promote` tool, called during dream runs | Reviews recent `SHORT_TERM` memories, scores them, promotes qualified ones to `LONG_TERM`. |
| Workspace directory (`~/.openclaw/workspace/<agent>/`) | entity storage scoped by `agent_id` (queries filter by agent_id) | All agent state queryable via the entity store. No filesystem layout. |
| Sessions (per-channel routing) | `Agent.conversation_id` (personal conv) + `Goal.war_room_conversation_id` (war room) | Conversation topology — direct agent contact lands in personal conv; goal coordination in war room. |
| Multi-agent routing (per-channel isolation) | `Agent.owner_user_id` + `Goal.owner_user_id` (per-user RBAC) | Cross-user is opt-in for now. |
| **(no analogue)** | `Goal` entity | First-class shared work item with status, war room, cost cap. CrewAI-style. |
| **(no analogue)** | `GoalAssignment(role)` — DRIVER / COLLABORATOR / REVIEWER | Multi-agent assignment with explicit roles. Dynamic add/remove. Handoff log. |
| **(no analogue)** | `Deliverable` + `GoalDependency` | Inter-goal dependency DAG with wake-up on `READY`. |
| **(no analogue)** | `agent_delegate(name, instruction, max_wait_s)` | Synchronous delegation with cycle detection + timeout. |

## Entity Model

All entities live in entity-storage collections. Inbox messages are NOT a separate collection — they're user-role messages in existing chat conversations with sender metadata.

```python
@dataclass
class Agent:
    id: str
    owner_user_id: str
    name: str                       # slug-friendly; unique within owner; addressable identity
    role_label: str                 # free-form descriptor ("research-bot", "QA reviewer")
    persona: str                    # the "soul" — long-form identity prompt
    system_prompt: str              # role-specific instructions layered on persona
    procedural_rules: str           # workflow rulebook (AGENTS.md analogue)
    profile_id: str                 # AI profile (model + sampling params)
    conversation_id: str            # personal conversation, lazy-created on first run
    status: AgentStatus             # ENABLED / DISABLED
    avatar_kind: str                # "emoji" | "icon" | "image"
    avatar_value: str               # emoji char, lucide icon name, or workspace_file:<id>
    lifetime_cost_usd: float
    cost_cap_usd: float | None      # auto-DISABLED when exceeded
    tools_allowed: list[str] | None # None = all tools (default); list = strict allowlist (plus core)
    heartbeat_enabled: bool         # default True
    heartbeat_interval_s: int       # default 1800 (30 min)
    heartbeat_checklist: str        # HEARTBEAT.md analogue
    dream_enabled: bool             # default False (opt in)
    dream_quiet_hours: str          # e.g. "22:00-06:00" in agent owner's TZ
    dream_probability: float        # 0..1, roll per quiet-hour heartbeat
    dream_max_per_night: int        # nightly cap
    created_at: datetime
    updated_at: datetime


class AgentStatus(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"


@dataclass
class AgentMemory:
    """Per-agent learned facts. Separate from per-user user_memory."""
    id: str
    agent_id: str
    content: str
    state: MemoryState              # SHORT_TERM / LONG_TERM
    kind: str                       # "fact" / "preference" / "decision" / "daily" / "dream"
    tags: frozenset[str]
    score: float                    # promotion-engine scoring; defaults 0.0
    created_at: datetime
    last_used_at: datetime | None


class MemoryState(StrEnum):
    SHORT_TERM = "short_term"
    LONG_TERM = "long_term"


@dataclass
class AgentTrigger:
    """Triggers move from Goal to Agent. The agent wakes up when the
    trigger fires and processes whatever is pending."""
    id: str
    agent_id: str
    trigger_type: str               # "time" | "event" | "heartbeat"
    trigger_config: dict[str, Any]  # heartbeat: {interval_s}; time/event: same shape as today
    enabled: bool


@dataclass
class Commitment:
    id: str
    agent_id: str
    content: str
    due_at: datetime
    created_at: datetime
    completed_at: datetime | None
    completion_note: str


@dataclass
class Goal:
    id: str
    owner_user_id: str
    name: str
    description: str
    status: GoalStatus              # NEW / IN_PROGRESS / BLOCKED / COMPLETE / CANCELLED
    war_room_conversation_id: str   # always populated; created with the goal
    cost_cap_usd: float | None
    lifetime_cost_usd: float
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class GoalStatus(StrEnum):
    NEW = "new"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


@dataclass
class GoalAssignment:
    id: str
    goal_id: str
    agent_id: str
    role: AssignmentRole            # DRIVER / COLLABORATOR / REVIEWER
    assigned_at: datetime
    assigned_by: str                # agent_id or "user:<user_id>"
    removed_at: datetime | None     # active assignments have None
    handoff_note: str               # context the prior driver leaves on handoff


class AssignmentRole(StrEnum):
    DRIVER = "driver"
    COLLABORATOR = "collaborator"
    REVIEWER = "reviewer"


@dataclass
class Deliverable:
    id: str
    goal_id: str
    name: str                       # logical name dependents reference ("spec", "design-doc")
    kind: str                       # free-form ("spec", "code", "report", "image")
    state: DeliverableState         # DRAFT / READY / OBSOLETE
    produced_by_agent_id: str
    content_ref: str                # "workspace_file:<file_id>" or inline text
    created_at: datetime
    finalized_at: datetime | None


class DeliverableState(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    OBSOLETE = "obsolete"


@dataclass
class GoalDependency:
    id: str
    dependent_goal_id: str          # this goal waits
    source_goal_id: str             # for a deliverable from this goal
    required_deliverable_name: str  # matched by name on source goal
    satisfied_at: datetime | None   # populated when source produces matching READY


@dataclass
class InboxSignal:
    """Durable wake-up tracking. Message *content* lives in chat conversations
    (existing infrastructure); this row tracks 'signal X is pending for
    agent Y, hasn't been processed yet.' The in-memory inbox dict is just
    a cache of unprocessed rows — the truth is in storage so signals
    survive process restart."""
    id: str
    agent_id: str
    signal_kind: str                # "inbox" | "deliverable_ready" |
                                    # "goal_assigned" | "delegation"
    body: str                       # human-readable summary line
    sender_kind: str                # "agent" | "user" | "system"
    sender_id: str
    sender_name: str
    source_conv_id: str             # conv where the message content lives
    source_message_id: str          # chat-row id, if applicable
    delegation_id: str              # populated for delegations
    metadata: dict[str, Any]        # signal-specific extra (deliverable_id, …)
    priority: str                   # "urgent" | "normal"
    created_at: datetime
    processed_at: datetime | None   # populated when the loop drains this signal


@dataclass
class Run:
    id: str
    agent_id: str                   # was goal_id
    triggered_by: str               # "manual" | "time" | "event" | "heartbeat" | "dream"
                                    # | "inbox" | "deliverable_ready" | "goal_assigned"
    trigger_context: dict[str, Any] # signal-specific metadata
    started_at: datetime
    status: RunStatus               # RUNNING / COMPLETED / FAILED / TIMED_OUT
    conversation_id: str            # the agent's personal conv
    delegation_id: str              # populated if this run is handling a delegation
    ended_at: datetime | None
    final_message_text: str | None
    rounds_used: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    error: str | None
    awaiting_user_input: bool
    pending_question: str | None
    pending_actions: list[dict[str, Any]]


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
```

**Entity-collection mapping:**

```
agents                 → Agent rows
agent_memories         → AgentMemory rows
agent_triggers         → AgentTrigger rows
agent_commitments      → Commitment rows
agent_inbox_signals    → InboxSignal rows
agent_runs             → Run rows
goals                  → Goal rows
goal_assignments       → GoalAssignment rows
goal_deliverables      → Deliverable rows
goal_dependencies      → GoalDependency rows
```

Inbox **message content** is stored as user-role messages in the existing chat-conversation collection, with `metadata.sender = {kind, id, name}` and (for delegations) `metadata.delegation_id`. The new `agent_inbox_signals` collection tracks "is this signal still pending for this agent?" durably — message content lives in the conv, signal lifecycle (created → processed) lives in `InboxSignal`. The in-memory `inboxes: dict[agent_id, list[InboxSignal]]` is a cache of unprocessed rows; on service start the cache is rehydrated by querying `agent_inbox_signals where processed_at is None`.

## Conversation Routing & Messaging

**Three conversation kinds:**

- **Personal conv** (one per Agent) — the agent's mind / inbox / journal. Direct messages from peers, users, or system.
- **War-room conv** (one per Goal) — the goal's coordination space. Always created with the goal, regardless of assignee count. Solo-agent goals have a one-participant war room.
- **(existing chat conv)** — user-initiated chats with the AI service. Untouched.

**Inbox routing rules:**

| Action | Where it lands | Wakeup |
|---|---|---|
| `agent_send_message(target_name, body)` | target's personal conv as user-role msg, with `metadata.sender = {kind: agent, id, name}` | idle → fire loop now; busy → enqueue, drain on next round |
| `goal_post(goal_id, body)` | goal's war-room conv | none — assignees observe on next loop |
| `goal_post(goal_id, body, mention=[name…])` | war room + a `[mentioned in war room <id>]` note in each mentioned assignee's personal conv | mentioned assignees: idle → fire; busy → enqueue |
| `agent_delegate(target_name, instruction, max_wait_s)` | target's personal conv as user-role msg with `metadata.delegation = {from_agent_id, delegation_id}`; calling agent's tool call awaits reply | target idle → fire, busy → enqueue. Caller's tool returns when target's loop ends and produces END_TURN. Cycle check rejects if it would deadlock. |
| `user → agent personal conv` (existing /agents UI) | personal conv as user-role | same as today |
| `user → war-room conv` (new UI) | war-room conv | none — assignees observe on next loop |

**Inbox queue mechanic (queue-first delivery):**

- Persistent state: `agent_inbox_signals` collection. Each `InboxSignal` row tracks one pending wake-up; the row's `processed_at` is `None` until the loop drains it.
- In-memory cache: `inboxes: dict[agent_id, list[InboxSignal]]` is rehydrated on service start by querying `agent_inbox_signals where processed_at is None`. Acts as a fast lookup; the truth is in storage.
- Message *content* lives in the corresponding chat conversation (existing infrastructure), referenced by `InboxSignal.source_message_id`. No content duplication.
- When agent's loop is running and `between_rounds_callback` fires:
  - Read pending signals for this agent (from cache → confirm in storage).
  - Format each as a user-role message ("[from {sender_name}]: {body}") prepended to next round's input.
  - Mark each as processed (`processed_at = now`) in storage; clear from cache.
- When agent's loop is idle and a message arrives:
  - Persist a new `InboxSignal` row.
  - Append to in-memory cache.
  - If the signal has a wake-up tag (peer DM, mention, delegation, deliverable_ready, goal_assigned), spawn the agent's loop with `triggered_by` set accordingly. The loop drains the cache on first round.
  - If no wake-up tag (war-room ambient chatter that doesn't generate a signal at all — assignees observe war rooms by reading the conv on their next natural tick), no signal is persisted in the first place.

**Delegation specifics:**

- `agent_delegate` returns the target's END_TURN final message text. No separate `agent_reply` tool — being delegated-to puts a system-prompt note in the target's run ("You are handling a delegation from {sender}. End your turn with the conclusion."), and the target's last assistant message is the reply.
- Pending delegations: `delegations: dict[delegation_id, asyncio.Future[str]]`. Resolved by the target's run-completion hook.
- Cycle detection: each delegation carries a `chain` list of agent_ids. If target appears in chain, reject before firing. Cap depth at 5 to prevent unbounded delegation trees.
- Timeout: `max_wait_s` defaults to 600. On expiry, future is set to a timeout exception, calling agent's tool returns an error string. The target's loop keeps running independently — delegation timeout doesn't cancel the target.

**War-room observation:**

- Each agent's loop, when it wakes, builds context from: its personal conv (last N messages) + war-room convs of every active assignment (last N messages each, summarized if large). The system prompt includes a "you have N active assignments, here's the recent state of each" preamble.
- An assignee who hasn't been mentioned won't react to chatter until their loop wakes (trigger, DM, mention).

## Loop Model & Tool Surface

**When an agent's loop fires:**

| Trigger | `Run.triggered_by` | Trigger context |
|---|---|---|
| Manual run-now (UI) | `"manual"` | `{user_message?}` |
| Time trigger fires | `"time"` | `{trigger_id}` |
| Event trigger fires | `"event"` | `{event_type, event_data}` |
| Heartbeat trigger fires | `"heartbeat"` | `{}` |
| Dreaming gate passes | `"dream"` | `{category?}` |
| Inbox wake-up tag (DM, mention, delegation) | `"inbox"` | `{message_count, sender_id, delegation_id?}` |
| Deliverable readied that an active assignment depends on | `"deliverable_ready"` | `{deliverable_id, source_goal_id}` |
| New DRIVER assignment created | `"goal_assigned"` | `{goal_id, role, handoff_note}` |

One run at a time per agent (`_running_agents: set[agent_id]`); N agents can run concurrently; inbox drains between rounds via `between_rounds_callback`.

**`_signal_agent` is the single dispatch point** for every wake-up source:
- Agent idle → spawn a `Run` with `triggered_by=signal_kind`, `trigger_context={metadata}`. The next system prompt includes the signal block.
- Agent busy → append `InboxSignal(sender_kind, body, metadata)` to the inbox dict; drained between rounds. Peer DMs format as `"[from {sender}]: {body}"` (user-role); system signals format as `"[system: {body}]"` (still user-role but visually marked).

**System-prompt assembly per run:**

```
{agent.persona}                          ← "soul"
{agent.system_prompt}                    ← role-specific instructions
{agent.procedural_rules}                 ← workflow rulebook
---
USER CONTEXT: {access to owner's user_memory at agent's discretion via tools}
ACTIVE ASSIGNMENTS: {goal_id, name, role, recent state per assignment}
LONG-TERM MEMORY (relevant): {AgentMemory where state=LONG_TERM, top-K by tag/recency}
{trigger-specific block: heartbeat checklist / dream prompt / inbox / etc.}
DATE/TIME: {date_ctx}
INBOX: {drained inbox messages, formatted as "[from {sender}]: {body}"}
```

`SHORT_TERM` memory and dreams are NOT loaded into every run — they're loaded selectively (during dream/promotion runs, or on explicit `agent_memory_search` tool calls).

**Tool surface — full inventory, with phasing flags:**

```
[Phase 1 — Agent foundation]
  complete_run(reason)                               flags the active Run as
                                                     having met its success
                                                     criteria. (Goal-level
                                                     completion is Phase 4.)
  request_user_input(question, actions?)             existing, kept
  notify_user(...)                                   existing, NotificationService
  commitment_create(content, due_in_seconds | due_at)
  commitment_complete(commitment_id, note?)
  commitment_list(include_completed=False)
  agent_memory_save(content, kind?, tags?)
  agent_memory_search(query, limit)
  agent_memory_review_and_promote(reviews=[…])       used by Phase 7 dreaming

[Phase 2 — Peer messaging (queue mode)]
  agent_list()                                       discover peers
  agent_send_message(name, body)                     DM, no reply
  agent_delegate(name, instruction, max_wait_s)      DM + await reply

[Phase 4 — Multi-agent goals]
  goal_create(name, description, assign_to[], deps?)
  goal_assign(goal_id, agent_name, role)
  goal_unassign(goal_id, agent_name)
  goal_handoff(goal_id, target_name, role, note)
  goal_post(goal_id, body, mention?[])
  goal_status(goal_id, new_status)                   includes flipping to COMPLETE
  goal_summary(goal_id)

[Phase 5 — Deliverables + dependencies]
  deliverable_create(goal_id, name, kind, ref)
  deliverable_finalize(id)
  deliverable_supersede(id, new_ref)
  goal_add_dependency(goal_id, source_goal, name)
  goal_remove_dependency(dep_id)
```

**Slash commands & namespace:**

`slash_namespace = "agents"` on the new service. Slash-enabled tools include `agent_list`, `agent_send_message`, `goal_create`, `goal_status`, `goal_summary`, `goal_post`, `deliverable_create`, `deliverable_finalize`. Three or more in `agents.goal.*` collapse under `slash_group = "goal"` so it's `/agents goal create …`. `slash_help` accompanies every `slash_command`.

**Core tools (force-included regardless of `tools_allowed`).**

The core set grows phase by phase as new tools land. In each phase, the listed tools are *always* registered for every agent run, even if `tools_allowed` is a strict allowlist that doesn't include them — they're the agent's self-management surface. Removing them would break the loop contract.

```
[Phase 1 — Agent foundation]
  complete_run, request_user_input, notify_user
  commitment_create, commitment_complete, commitment_list
  agent_memory_save, agent_memory_search,
  agent_memory_review_and_promote

[Phase 2 — Peer messaging additions]
  agent_list, agent_send_message, agent_delegate

[Phase 4 — War-room additions]
  goal_post   (assigned agents must always be able to post in their war rooms)
```

**Optional pool — gated by `tools_allowed`:** everything else (knowledge, browser, inbox, lights, music, MCP, web search, deliverable tools, goal management tools, etc.). When `tools_allowed=None`, all available tools are exposed. When `tools_allowed=[...]`, the run only registers tools whose name is in the list (in addition to core).

Run-time gating in `_run_agent_internal`:

```python
all_tools = await tool_discovery.get_all_tools(user_ctx)   # respects user RBAC
if agent.tools_allowed is not None:
    keep = set(_CORE_AGENT_TOOLS) | set(agent.tools_allowed)
    all_tools = {n: t for n, t in all_tools.items() if n in keep}
return all_tools
```

User-level RBAC applies first; per-agent allowlist is a narrower gate on top.

## Autonomous Behaviors

**Heartbeat (Phase 1, default-on):**

When `Agent.heartbeat_enabled=True`, the SchedulerService schedules `heartbeat_<agent_id>` at `heartbeat_interval_s`. When it fires, the agent runs with `triggered_by="heartbeat"` and a system-prompt prepend:

```
HEARTBEAT — periodic self-check. Read your checklist below and decide
if anything needs action right now. If nothing is pressing, end your
turn briefly.

CHECKLIST:
{agent.heartbeat_checklist}

DUE COMMITMENTS:
{Commitment rows where due_at <= now AND completed_at IS NULL}
```

The heartbeat run uses the same personal conversation. Cost is included in `Agent.lifetime_cost_usd`. Tools available: full agent tool set (subject to `tools_allowed`).

**Dreaming (Phase 7, opt-in):**

When `Agent.dream_enabled=True`, every heartbeat fire passes through a gate:

1. Is local time inside `dream_quiet_hours`?
2. Is `count(today's dreams) < dream_max_per_night`?
3. Roll: `random() < dream_probability`?

If all three pass, the heartbeat is replaced by a dream run with `triggered_by="dream"` and a dream prompt:

```
DREAMING — quiet-hours freeform thinking. No task focus. Pick one of:
future scenario, tangent, strategy, creative thought, reflection,
hypothetical, unexpected connection. Explore briefly, then call
agent_memory_save with kind="dream" and a category tag.
End your turn.
```

Dreams are written via `agent_memory_save(content, kind="dream", tags=[category])` as `state=SHORT_TERM`. A separate **memory promotion sweep** (also dream-time) reviews short-term entries from the past 24h, asks the agent to score each (relevance / durability), and promotes high-scoring ones to `LONG_TERM` via `agent_memory_review_and_promote`.

**Commitments (Phase 1):**

`Commitment` rows are loaded into the heartbeat prompt's `DUE COMMITMENTS` block when `due_at <= now`. The agent decides to act and calls `commitment_complete` when handled. `commitment_create(content, due_in_seconds=N)` is the agent's self-imposed reminder mechanism.

## Agent Management Surface

**Defaults strategy:**

- Service-level defaults live as `ConfigParam` entries in the `agent_service` config namespace (so operators can tweak globally — same pattern as every other configurable service). Defaults include: `default_persona`, `default_system_prompt`, `default_procedural_rules`, `default_heartbeat_interval_s`, `default_heartbeat_checklist`, `default_dream_enabled`, `default_dream_quiet_hours`, `default_dream_probability`, `default_dream_max_per_night`, `default_profile_id`, `default_avatar_kind`, `default_avatar_value`, `default_tools_allowed`, `tool_groups`.
- All persona / prompt / checklist defaults are `ConfigParam(multiline=True, ai_prompt=True)` so they show up in the prompt-author Settings UI. Aligns with the "AI prompts always configurable" rule.
- At create time, omitted fields are filled from the *current* default values (snapshot — editing the default later doesn't retroactively change existing agents).

**WS RPCs (all `agents.*`, user-level RBAC, owner-scoped):**

```
agents.create(name, fields?)          defaults fill the rest; returns Agent
agents.update(agent_id, patch)        partial; any field
agents.delete(agent_id, cascade=true) agent + memories, commitments, triggers,
                                      runs, assignments. Does NOT delete goals.
agents.list(owner_user_id?)           admin sees all; users see their own
agents.get(agent_id)
agents.run_now(agent_id, user_message?)
agents.set_status(agent_id, status)
agents.get_defaults()                 current defaults for create-form prefill
agents.avatar.upload(agent_id, ...)   multipart → workspace_file_id; route writes
                                      avatar_kind="image", avatar_value=...
agents.tools.list_available()         all registered tools w/ metadata
agents.tools.list_groups()            curated UI groups
```

## Deliverables & Dependencies

**Lifecycle:**

```
DRAFT ──finalize()──→ READY ──supersede()──→ OBSOLETE
   │
   └──supersede()──→ OBSOLETE   (replaced before finalize)
```

`deliverable_supersede(id, new_content_ref)` creates a new DRAFT (or READY, optional flag) with the same `name` on the same goal, and marks the old one OBSOLETE in one transaction. Multiple READY deliverables with the same `name` on a goal is forbidden — the supersede transaction enforces it.

**Dependency satisfaction** is computed: a `GoalDependency` is satisfied iff the source goal has a `Deliverable` matching `required_deliverable_name` with `state=READY`. `satisfied_at` is purely an audit timestamp.

**Wake-up propagation:**

```python
async def on_deliverable_finalized(d: Deliverable) -> None:
    deps = await storage.query("goal_dependencies",
        source_goal_id=d.goal_id,
        required_deliverable_name=d.name,
        satisfied_at=None)

    for dep in deps:
        await storage.update("goal_dependencies", dep.id, {"satisfied_at": now})
        assignees = await storage.query("goal_assignments",
            goal_id=dep.dependent_goal_id, removed_at=None)
        for a in assignees:
            if a.role == AssignmentRole.REVIEWER:
                continue
            await self._signal_agent(
                agent_id=a.agent_id,
                signal_kind="deliverable_ready",
                body=f"Dependency satisfied: {d.name} from goal {dep.source_goal_id}",
                metadata={"deliverable_id": d.id, "source_goal_id": d.goal_id},
            )

    await event_bus.publish(Event("deliverable.ready",
        {"deliverable_id": d.id, "goal_id": d.goal_id, "name": d.name}))
```

**Cross-goal file access:**

War-room conversations get standard per-conversation workspaces via `WorkspaceProvider.get_workspace_root(user_id, war_room_conv_id)`. Files produced for a deliverable are registered in the existing `workspace_files` collection; `Deliverable.content_ref = "workspace_file:<file_id>"`.

When dependency H ← G is registered (via `goal_add_dependency`), H's assignees auto-gain read access to G's war-room workspace files **scoped to deliverables matching `name=<required_deliverable_name>` and `state=READY`**. Implementation: a sibling helper on `WorkspaceProvider`:

```python
def resolve_deliverable_for_dependent(
    self,
    file_id: str,
    viewing_agent_id: str,
    viewing_goal_id: str,
) -> tuple[Path | None, str | None]:
    """Returns the path iff the file_id is a Deliverable.content_ref on
    a goal that viewing_goal_id has a registered dependency on, and the
    deliverable is currently READY (not OBSOLETE)."""
```

The agent's prompt for a `deliverable_ready` run includes the file_id and goal_id explicitly: `"Read the spec via read_workspace_file(file_id=X, goal_id=Y)"`. The tool checks the cross-goal grant before reading.

OBSOLETE deliverables revoke access (the resolver returns "not found" for them).

**`BLOCKED` status:**

`Goal.status` is a simple field set by agents/users. `BLOCKED` means "cannot make progress until X" and is the agent's choice. `goal_summary()` returns a derived `is_dependency_blocked: bool` computed from unsatisfied dependencies. UI shows both. When a wake-up fires (deliverable becomes READY), the receiving agent's run system prompt includes `"You may now be unblocked: <satisfied dependency list>"` so it can flip status back to IN_PROGRESS.

## Multi-User & RBAC

**Default permission rules (Phase 1–5: same-owner only).**

Every new RPC under `agents.*`, `goals.*`, and `deliverables.*` registers at user-level (`DEFAULT_RPC_PERMISSIONS["agents."] = 100`, etc.). Handlers enforce ownership before doing anything else. Admins can read across owners; mutations stay owner-scoped except for explicit admin operations.

**Per-tool RBAC matrix:**

| Tool | Allowed when |
|---|---|
| `agent_list` | returns peers with `owner_user_id == caller_agent.owner_user_id` |
| `agent_send_message(target)` | `target.owner == caller.owner` |
| `agent_delegate(target)` | same |
| `goal_create(assign_to=[…])` | every assignee `.owner == caller.owner`; goal inherits caller's `owner_user_id` |
| `goal_assign(goal_id, name)` | `goal.owner == caller.owner` AND `assignee.owner == caller.owner` |
| `goal_unassign` / `goal_handoff` | same |
| `goal_post(goal_id, …)` | caller is an active assignee of `goal_id` |
| `goal_status(goal_id, …)` | caller has role `DRIVER` on `goal_id` |
| `goal_add_dependency` | caller has role `DRIVER` on `dependent_goal_id` |
| `deliverable_create(goal_id, …)` | caller has `DRIVER` or `COLLABORATOR` on `goal_id` |
| `deliverable_finalize` / `deliverable_supersede` | caller is the producer OR caller has `DRIVER` on the goal |
| `request_user_input` | unchanged |
| `commitment_*`, `agent_memory_*` | scoped to the caller's own agent_id by injection — no inter-agent reach |

Tool argument injection: extend the agent service's tool-registration wrapper to inject `_agent_id` (the currently-running agent's id) on every tool call, taken from the active `Run`. Tools never trust arguments for caller identity — always read from injection.

**Conversation access (war rooms reuse existing chat ACL):**

Each war-room conversation has the goal's `owner_user_id` as the conversation owner. Assigned agents act as the goal owner — their own `owner_user_id` matches in Phase 1–5. The agent's run executes under `UserContext.from_user_id(agent.owner_user_id)` with metadata `{"actor_kind": "agent", "agent_id": …}`, so existing chat read/write/streaming gates work without modification.

**Multi-user isolation safety:**

- The new `AgentService` is a singleton across all users. Per-request state lives in `_signal_agent`-keyed maps (`inboxes: dict[agent_id, …]`, `_running_agents: set[agent_id]`, `delegations: dict[delegation_id, Future]`). Keys are agent_ids (owner-scoped); no cross-user leakage.
- Loops spawned for triggered runs use `asyncio.Task(coro, context=copy_context())` so the entry-point's `current_user` ContextVar doesn't bleed across concurrent agent runs.
- All entity queries that fetch an agent / goal / deliverable / etc. by id MUST also filter by `owner_user_id` (or be admin). A helper `_load_agent_for_caller(agent_id, caller_user_id)` raises `PermissionError` if mismatch — every tool / RPC handler routes through it.

**Notification routing:** `request_user_input` and `notify_user` route to the agent's `owner_user_id`, not the running agent. Agents cannot notify users other than their owner.

**Cross-user (Phase 6, sketched only):**

Three additive fields on `Agent`:

- `allow_inbound_from: frozenset[str]` — user_ids whose agents may send messages here
- `allow_outbound_to: frozenset[str]` — user_ids whose agents we may message
- `allow_assign_by: frozenset[str]` — user_ids who may assign this agent to their goals

Plus a `Goal.allow_assignees_from: frozenset[str]` for cross-user collaboration on a single goal. Cross-user message delivery, dependency resolution, and file access all check these allowlists before the same-owner short-circuit. Default everything to empty — strictly opt-in. Detailed design is its own future spec.

## UI Surfaces

**Routes:**

```
/agents               list all owned agents (admin sees all)
/agents/new           create form
/agents/<id>          detail (tabs: Chat | Settings | Memory | Commitments | Runs)
/goals                list goals (kanban by status by default)
/goals/<id>           war room
```

**Pages and panels:**

| Surface | Contents |
|---|---|
| `/agents` list | Agent cards: `<AgentAvatar>`, name, role_label, status pill, last activity, cost-to-date vs cap, count of active assignments. "New agent" button. |
| `/agents/new` and Settings tab | Multi-section form. **Identity:** name, role_label, avatar picker. **Persona:** persona, system_prompt, procedural_rules — three multiline editors with `ai_prompt`-flagged Settings-page component. **Heartbeat:** enabled, interval, checklist. **Dreaming:** enabled, quiet hours, probability, max-per-night. **Profile & cost:** profile_id, cost_cap_usd. **Tools:** `<ToolPicker>` checkbox tree; core tools shown checked-and-disabled; optional pool grouped by `ToolProvider` and curated groups. All sections collapsible; pre-filled from `agents.get_defaults()`. |
| `/agents/<id>` Chat tab | The agent's personal conversation. Existing `ChatPage` rendering reused. Sender attribution badges. Inbox indicator while idle. |
| `/agents/<id>` Memory tab | Browser over `AgentMemory`. Filter by `state`, `kind`, tags, full-text. "Promote" / "Demote" buttons. |
| `/agents/<id>` Commitments tab | Active and recently-completed commitments. Quick-add form. |
| `/agents/<id>` Runs tab | Run history with cost/round/token columns, click to expand prompt + final message. Filter by `triggered_by`. |
| `/goals` list | Kanban: NEW / IN_PROGRESS / BLOCKED / COMPLETE / CANCELLED. Card: name, assignee avatars (overlapped circles), deliverable count, dependency status badge. Drag between columns calls `goal_status`. |
| `/goals/<id>` war room | **Header:** name, status, cost. **Assignees strip:** avatars + role chips, "+" to add. **Main:** war-room conversation. **Right rail:** `<DeliverablesPanel>`, `<DependenciesPanel>`. |

**Shared components:**

- `<AgentAvatar size>` — single component, renders by `avatar_kind`. Sizes: xs (16px chat), sm (24px cards), md (40px detail), lg (96px edit form).
- `<ToolPicker value onChange>` — fetches `agents.tools.list_available()` + `list_groups()`, renders grouped checkbox tree. Returns `string[] | null`.
- `<DeliverablesPanel goalId>` — table of deliverables (name, kind, state, producer avatar, created_at). Expandable to show content/file viewer.
- `<DependenciesPanel goalId>` — two lists: outgoing deps with satisfied checkmarks + click-to-source, plus incoming deps from goals waiting on this one.

**Real-time updates (event-bus subscriptions):**

| Event | Drives |
|---|---|
| `agent.run.started` / `agent.run.completed` | status pills, runs tab refresh |
| `agent.inbox.received` | inbox indicator on agent card; badge on personal-conv tab |
| `goal.assignment.changed` | assignees strip, kanban card avatars |
| `goal.deliverable.created` / `goal.deliverable.finalized` | deliverables + dependencies panels |
| `goal.status.changed` | kanban movement |
| existing `chat.stream.*` | streaming text in personal & war-room convs |

**Plugin extension slots** so plugins can contribute UI without core knowing:

- `agent.detail.settings.tabs` — extra settings tab per plugin
- `agent.detail.toolbar` — top-of-detail action buttons
- `goal.warroom.right_rail` — extra right-rail panels
- `goal.warroom.toolbar` — top-of-war-room actions

**Front-end file layout:**

```
frontend/src/
  components/
    agents/
      AgentsListPage.tsx
      AgentDetailPage.tsx
      AgentEditForm.tsx
      AgentAvatar.tsx
      AgentCard.tsx
      ToolPicker.tsx
      MemoryBrowser.tsx
      CommitmentsList.tsx
    goals/
      GoalsListPage.tsx
      GoalKanban.tsx
      GoalCard.tsx
      WarRoomPage.tsx
      DeliverablesPanel.tsx
      DependenciesPanel.tsx
  api/
    agents.ts
    goals.ts
    deliverables.ts
  types/
    agent.ts
    goal.ts
    deliverable.ts
```

## Shipping Phases

**Phase 1: Agent foundation** — entities, `AgentService`, heartbeat, commitments, memory tools, per-agent tool gating, defaults via `ConfigParam`, full management UI. Old `AutonomousAgentService` and its data deleted in this phase. **Acceptance:** can create an agent with persona/heartbeat/checklist, see it run on a schedule, edit/delete, view memories and runs, gate its tools.

**Phase 2: Peer messaging (queue mode)** — `agent_list`, `agent_send_message`, `agent_delegate`, `_signal_agent` dispatch, sender attribution UI, cycle detection, delegation timeout. **Acceptance:** A→B works (B responds on next loop). A→B→C delegation works. A→B→A rejected by cycle check.

**Phase 3: Mid-stream interrupt** — `AIService.chat()` accepts urgent injection at two safe boundaries: (1) between rounds (existing `between_rounds_callback` point — already exists; the gain is *latency*: an urgent message ends the current round earlier and gets surfaced immediately rather than waiting for the round's natural completion), and (2) between tool calls within a round (new — when a round dispatches multiple tool calls sequentially, an urgent message interrupts after the current tool call resolves but before the next is invoked). Mid-token-streaming cancellation is **out of scope** for Phase 3. `InboxSignal.priority: "urgent" | "normal"` (default `"normal"`); only `urgent` triggers interrupt; `normal` stays queue-mode. **Acceptance:** urgent messages interrupt at the next safe boundary (whichever comes first); non-urgent stays queue-mode; existing chat behavior unchanged when no urgent message arrives.

**Phase 4: Multi-agent goals** — `Goal` and `GoalAssignment` entities are *introduced fresh* in this phase (Phase 1 has no goals at all). War-room conversations, goal management tools (`goal_create / assign / unassign / handoff / post / status / summary`), kanban + war-room UI. `goal_post` joins the core force-include set. **Acceptance:** create goal with two assignees, both see war room, posting visible to both, handoff transfers DRIVER role, status change (including flipping to COMPLETE) is DRIVER-only.

**Phase 5: Deliverables + dependency wake-up** — `Deliverable`, `GoalDependency`, propagation mechanic, cross-goal file access, deliverables/dependencies UI panels. **Acceptance:** A produces `spec` deliverable → B's drivers wake up → B can `read_workspace_file(file_id, goal_id=A)`.

**Phase 6 (deferred): Cross-user** — `allow_*` allowlists, cross-user delivery + dependency resolution + file access.

**Phase 7 (deferred): Dreaming + memory promotion** — dream-mode runs, gating, `agent_memory_review_and_promote` workflow, dream tab UI.

## Test Strategy

| Category | Coverage |
|---|---|
| **Unit (per phase)** | Tool arg validation, RBAC handlers, entity CRUD, lifecycle transitions, gating predicates, prompt assembly, signal dispatch (idle vs busy), cycle detection, timeouts. Mock the AI backend; assert on prompt + tool calls + run results. |
| **Integration (per phase)** | Real SQLite + real `AgentService` + mocked `AIBackend`. End-to-end flows: schedule fires → run completes → memory saved; A→B message round-trip; goal handoff; dependency satisfaction → automatic wakeup of dependents. |
| **Multi-user isolation** | Concurrent runs of two users' agents don't bleed ContextVars; queries strictly filter by `owner_user_id`; cross-user reach raises `PermissionError`; agent owned by X can't appear in Y's `agent_list`. |
| **Streaming (Phase 3)** | Urgent injection mid-stream lands at safe boundary, doesn't corrupt tool-result formatting, doesn't double-resolve delegations. |
| **War-room ACL (Phase 4)** | Non-assignee cannot post or read; assigned agent can read+post; DRIVER-only for `goal_status`. |
| **Cross-goal access (Phase 5)** | Dependency-grant works only for READY same-named deliverables; OBSOLETE blocked; missing dependency edge blocked; superseding revokes old grant. |
| **Architecture rule compliance** | Capability protocols only, no concrete-class isinstance, AI prompts via `ConfigParam(ai_prompt=True)`, slash commands declared, `slash_namespace="agents"`, multi-user isolation per the existing checklist. |

**Test files under `tests/unit/`:** `test_agent_service.py`, `test_agent_messaging.py`, `test_agent_inbox.py`, `test_agent_delegation.py`, `test_agent_memory.py`, `test_commitments.py`, `test_heartbeat.py`, `test_goals.py`, `test_goal_assignments.py`, `test_war_room_acl.py`, `test_deliverables.py`, `test_dependencies.py`, `test_cross_goal_access.py`, `test_tool_gating.py`. Plus updates to `test_slash_command_uniqueness.py` for the new tools.

## Open Questions / Future

- **Phase 6 cross-user design:** the allowlist sketch is a starting point. A real design needs invitation flows (how does user X opt user Y's agent in?), revocation semantics, audit trails, and possibly cross-user `Goal.owner_user_id` semantics (does a multi-owner goal exist? or is the goal still owned by one user with cross-user assignees allowed?).
- **Phase 7 dreaming:** the memory-promotion sweep design is a sketch. A real design needs a scoring rubric (what makes a memory durable?), how the agent self-reviews without becoming sycophantic, and human review surfaces for the long-term memory store.
- **Workspace cleanup on goal deletion:** if a goal is deleted (CANCELLED then purged), what happens to its war-room workspace files referenced by another goal's deliverables? Probably: files persist until all dependents are also gone. Implementation deferred to Phase 5.
- **Agent profile pictures:** the avatar mechanic supports image uploads via the existing chat-uploads route, but image moderation / validation policy is left to the operator.
- **Cost attribution on collaboration:** when two agents on the same goal both run, both runs cost money. Today both `Agent.lifetime_cost_usd` increment. Should the goal's `lifetime_cost_usd` also accumulate (it does, via Phase 4 wiring)? Yes, but a single run might split between agent and goal cost trackers — the implementation should record once per run on both axes and document the convention.

## Related

- `src/gilbert/core/services/agent.py` (current `AutonomousAgentService`, to be replaced)
- `src/gilbert/interfaces/agent.py` (current Goal/Run; will be reshaped)
- `src/gilbert/core/services/workspace.py` (`resolve_file_path` will gain a sibling for cross-goal access)
- `.claude/memory/memory-autonomous-agent-service.md` (existing service to be replaced)
- `.claude/memory/memory-agent-loop.md` (`run_loop` primitive — used by the new `AgentService`)
- `.claude/memory/memory-multi-user-isolation.md` (must comply)
- `.claude/memory/memory-ai-prompts-configurable.md` (every prompt must be `ConfigParam(ai_prompt=True)`)
- `.claude/memory/memory-capability-protocols.md` (consumers must use protocols)
- `.claude/memory/memory-backend-pattern.md` (no concrete instantiation)
- OpenClaw docs: heartbeat, dreaming, memory, AGENTS.md template (concept references; we adopt the model, not the file format)
- CrewAI: agent ≠ task, role-based assignment (concept reference)
- AutoGen: long-lived addressable agents with names (concept reference)
