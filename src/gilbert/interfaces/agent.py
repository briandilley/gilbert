"""Agent interface — entity dataclasses + AgentProvider protocol.

Replaces the old Goal/Run model with a multi-agent design:

- Agent — durable identity (persona, system prompt, procedural rules,
  heartbeat config, tool allowlist, avatar, lifetime cost).
- AgentMemory — per-agent learned facts; SHORT_TERM / LONG_TERM split.
- AgentTrigger — time / event / heartbeat trigger config rows.
- Commitment — opt-in short-lived follow-ups, surfaced in heartbeats.
- InboxSignal — durable wake-up tracking; message content lives in
  conversation rows, signal lifecycle (created → processed) lives here.
- Run — one execution of an agent's loop. Keyed by agent_id.

See docs/superpowers/specs/2026-05-04-agent-messaging-design.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

# ── Enums ────────────────────────────────────────────────────────────


class AgentStatus(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"


class MemoryState(StrEnum):
    SHORT_TERM = "short_term"
    LONG_TERM = "long_term"


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


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


# ── Entities ─────────────────────────────────────────────────────────


@dataclass
class Agent:
    """Durable agent identity. The addressable thing in the multi-agent
    model — peers send to agents by name, goals are assigned to agents.
    """

    id: str
    owner_user_id: str
    name: str                       # slug-friendly; unique within owner
    role_label: str                 # free-form descriptor
    persona: str                    # the "soul" — long-form identity prompt
    system_prompt: str              # role-specific instructions on persona
    procedural_rules: str           # workflow rulebook (AGENTS.md analogue)
    profile_id: str                 # AI profile (model + sampling params)
    conversation_id: str            # personal conv, lazy-created on first run
    status: AgentStatus
    avatar_kind: str                # "emoji" | "icon" | "image"
    avatar_value: str               # emoji char, lucide icon, or workspace_file:<id>
    lifetime_cost_usd: float
    cost_cap_usd: float | None      # auto-DISABLED when exceeded
    tools_allowed: list[str] | None # None = all tools; list = strict allowlist
    heartbeat_enabled: bool
    heartbeat_interval_s: int
    heartbeat_checklist: str
    dream_enabled: bool
    dream_quiet_hours: str
    dream_probability: float
    dream_max_per_night: int
    created_at: datetime
    updated_at: datetime


@dataclass
class AgentMemory:
    """Per-agent learned fact. Distinct from per-user user_memory.

    Recent SHORT_TERM entries are written by the agent during runs.
    LONG_TERM entries are loaded into prompt context (top-K). Promotion
    from SHORT_TERM → LONG_TERM happens during dream-mode runs in
    Phase 7; in Phase 1 the agent can promote/demote manually."""

    id: str
    agent_id: str
    content: str
    state: MemoryState
    kind: str                       # "fact" | "preference" | "decision" |
                                    # "daily" | "dream"
    tags: frozenset[str]
    score: float                    # promotion-engine scoring; defaults 0.0
    created_at: datetime
    last_used_at: datetime | None


@dataclass
class AgentTrigger:
    """Triggers that fire an agent run. Time/event are configurable;
    heartbeat is implicit per-agent (one row per agent when
    heartbeat_enabled=True)."""

    id: str
    agent_id: str
    trigger_type: str               # "time" | "event" | "heartbeat"
    trigger_config: dict[str, Any]  # heartbeat: {interval_s}; time/event:
                                    # {kind, seconds, hour, minute, ...}
    enabled: bool


@dataclass
class Commitment:
    """Self-imposed short-lived follow-up reminder. Surfaced in the
    heartbeat prompt's DUE COMMITMENTS block when due_at <= now and
    completed_at is None."""

    id: str
    agent_id: str
    content: str
    due_at: datetime
    created_at: datetime
    completed_at: datetime | None
    completion_note: str


@dataclass
class InboxSignal:
    """Durable wake-up tracking. Message content lives in chat rows;
    this row tracks 'signal X is pending for agent Y, hasn't been
    processed yet.'"""

    id: str
    agent_id: str
    signal_kind: str                # "inbox" | "deliverable_ready" |
                                    # "goal_assigned" | "delegation"
    body: str                       # human-readable summary
    sender_kind: str                # "agent" | "user" | "system"
    sender_id: str
    sender_name: str
    source_conv_id: str             # conv where the message content lives
    source_message_id: str
    delegation_id: str              # for delegations
    metadata: dict[str, Any]        # signal-specific extra
    priority: str                   # "urgent" | "normal"
    created_at: datetime
    processed_at: datetime | None


@dataclass
class Run:
    """One execution of an agent's loop, keyed by agent_id."""

    id: str
    agent_id: str
    triggered_by: str               # "manual" | "time" | "event" |
                                    # "heartbeat" | "dream" | "inbox" |
                                    # "deliverable_ready" | "goal_assigned"
    trigger_context: dict[str, Any]
    started_at: datetime
    status: RunStatus
    conversation_id: str
    delegation_id: str              # populated if handling a delegation
    ended_at: datetime | None
    final_message_text: str | None
    rounds_used: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    error: str | None
    awaiting_user_input: bool
    pending_question: str | None
    pending_actions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Goal:
    """A multi-agent goal. One war-room conversation per goal; one or
    more agent assignees with role DRIVER / COLLABORATOR / REVIEWER.

    A DRIVER is the only assignee who can change ``status``, hand off,
    or add/remove other assignees. ``lifetime_cost_usd`` is informational
    in Phase 4 (no auto-disable). ``cost_cap_usd`` is stored but not
    enforced until later phases.
    """

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
    """An agent's assignment to a goal at a given role.

    Unassign sets ``removed_at`` rather than deleting the row, so the
    history of who was on a goal is preserved. ``handoff_note`` records
    the note supplied by ``handoff_goal`` on both the from-driver and
    to-driver rows.
    """

    id: str
    goal_id: str
    agent_id: str
    role: AssignmentRole
    assigned_at: datetime
    assigned_by: str          # agent_id or "user:<user_id>"
    removed_at: datetime | None
    handoff_note: str


# ── Protocol ─────────────────────────────────────────────────────────


@runtime_checkable
class AgentProvider(Protocol):
    """Capability protocol for the agent service. Consumers should
    isinstance-check against this rather than the concrete service."""

    async def create_agent(
        self,
        *,
        owner_user_id: str,
        name: str,
        **fields: Any,
    ) -> Agent: ...

    async def get_agent(self, agent_id: str) -> Agent | None: ...

    async def list_agents(
        self,
        *,
        owner_user_id: str | None = None,
    ) -> list[Agent]: ...

    async def run_agent_now(
        self,
        agent_id: str,
        *,
        user_message: str | None = None,
    ) -> Run: ...

    async def load_agent_for_caller(
        self,
        agent_id: str,
        *,
        caller_user_id: str,
        admin: bool = False,
    ) -> Agent: ...

    async def set_agent_avatar(
        self, agent_id: str, *, filename: str
    ) -> Agent: ...

    # ── Goals (Phase 4) ─────────────────────────────────────────────

    async def create_goal(
        self,
        *,
        owner_user_id: str,
        name: str,
        description: str = "",
        cost_cap_usd: float | None = None,
        assign_to: list[tuple[str, AssignmentRole]] | None = None,
        assigned_by: str = "user:?",
    ) -> Goal: ...

    async def get_goal(self, goal_id: str) -> Goal | None: ...

    async def list_goals(
        self,
        *,
        owner_user_id: str | None = None,
    ) -> list[Goal]: ...

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
