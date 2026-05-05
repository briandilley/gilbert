"""AgentService — durable agent identity, lifecycle, and run orchestration.

This service owns the Agent, AgentMemory, AgentTrigger, Commitment,
InboxSignal, and Run entity collections. It exposes:

- CRUD for agents and related entities (Task 5 / Task 7 / Task 9).
- Agent run orchestration via ``run_agent_now`` (Task 8).
- Heartbeat re-arming via the scheduler (Task 10).
- Inbox signal dispatch (Task 11).
- WS RPC handlers for the SPA (Task 6).
- AI tool definitions (Task 14).

Task 3 establishes the skeleton: start/stop lifecycle, service_info,
and NotImplementedError stubs for the four AgentProvider methods.
Task 5 implements CRUD + RBAC helper.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

# ContextVar threaded into the tool dispatcher so every ``_exec_*`` handler
# can recover the active agent's id even though ``AIService.execute_tool``
# doesn't pass per-call wrapping. ``_run_agent_internal`` sets it before
# calling ``self._ai.chat`` and resets it afterwards. Tools read it via
# ``_active_agent_id.get("")`` and treat the empty string as "not in a
# run" (e.g. when invoked from a slash command outside an agent run).
_active_agent_id: ContextVar[str] = ContextVar("_active_agent_id", default="")
_active_delegation_chain: ContextVar[list[str]] = ContextVar(
    "_active_delegation_chain", default=[],
)

from gilbert.interfaces.agent import (
    Agent,
    AgentMemory,
    AgentStatus,
    AssignmentRole,
    Commitment,
    Deliverable,
    DeliverableState,
    Goal,
    GoalAssignment,
    GoalDependency,
    GoalStatus,
    InboxSignal,
    MemoryState,
    Run,
    RunStatus,
)
from gilbert.interfaces.ai import AIProvider, AIToolDiscoveryProvider, Message, MessageRole
from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.events import Event, EventBusProvider
from gilbert.interfaces.scheduler import Schedule, SchedulerProvider
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import Filter, FilterOp, Query, StorageBackend, StorageProvider
from gilbert.interfaces.tools import ToolDefinition, ToolParameter, ToolParameterType

logger = logging.getLogger(__name__)

# ── Name validation ──────────────────────────────────────────────────

_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# ── Default ConfigParam values ───────────────────────────────────────

_DEFAULT_PERSONA = "You are an autonomous AI agent."
_DEFAULT_SYSTEM_PROMPT = (
    "Take whatever action is appropriate to advance the goals you have "
    "been assigned. Use your tools deliberately. End your turn briefly "
    "when there is nothing pressing."
)
_DEFAULT_PROCEDURAL_RULES = (
    "When you ask a question or need user input, MUST call "
    "request_user_input first so the user gets a notification.\n\n"
    "When you make a follow-up commitment, call commitment_create.\n\n"
    "When you learn a durable fact about the user or their context, "
    "call agent_memory_save with kind='preference' or kind='fact'."
)
_DEFAULT_HEARTBEAT_CHECKLIST = (
    "1. Are there any due commitments to action?\n"
    "2. Anything inbound in your inbox you haven't seen?\n"
    "3. Any goals assigned to you that are blocked?\n"
    "4. If nothing pressing, end your turn briefly."
)
# ── Collection names ─────────────────────────────────────────────────

_AGENTS_COLLECTION = "agents"
_AGENT_MEMORIES_COLLECTION = "agent_memories"
_AGENT_TRIGGERS_COLLECTION = "agent_triggers"
_AGENT_COMMITMENTS_COLLECTION = "agent_commitments"
_AGENT_INBOX_SIGNALS_COLLECTION = "agent_inbox_signals"
_AGENT_RUNS_COLLECTION = "agent_runs"
_GOALS_COLLECTION = "goals"
_GOAL_ASSIGNMENTS_COLLECTION = "goal_assignments"
_DELIVERABLES_COLLECTION = "goal_deliverables"
_DEPENDENCIES_COLLECTION = "goal_dependencies"

# AI conversation rows live here. The same constant is declared on
# AIService (``core/services/ai.py`` _COLLECTION). We re-declare instead
# of importing because ``core/services/`` modules must not depend on
# each other — the agent service writes the war-room conversation row
# directly rather than going through AIService.
_AI_CONVERSATIONS_COLLECTION = "ai_conversations"

_AI_CALL_NAME = "agent.run"

_CORE_AGENT_TOOLS: frozenset[str] = frozenset({
    # Phase 1A — agent self-management
    "complete_run",
    "request_user_input",
    "notify_user",
    "commitment_create",
    "commitment_complete",
    "commitment_list",
    "agent_memory_save",
    "agent_memory_search",
    "agent_memory_review_and_promote",
    # Phase 2 — peer messaging
    "agent_list",
    "agent_send_message",
    "agent_delegate",
    # Phase 4 — goal participation. ``goal_post`` is core because every
    # assignee needs to be able to post to a war room they're on.
    # The other six goal tools (create / assign / unassign / handoff /
    # status / summary) are NOT core — operators may pin them per-agent
    # via ``tools_include``.
    "goal_post",
})


# ── ToolDefinitions (Task 14) ────────────────────────────────────────

_TOOL_COMPLETE_RUN = ToolDefinition(
    name="complete_run",
    description=(
        "Flag the current agent run as having met its success criteria. "
        "Use this when you've completed the work you were triggered for "
        "and have nothing else to do this turn. Reason is logged onto the "
        "Run entity."
    ),
    parameters=[
        ToolParameter(
            name="reason",
            type=ToolParameterType.STRING,
            description="One-line success reason logged onto the Run.",
            required=True,
        ),
    ],
    slash_command="complete_run",
    slash_help="Mark the current run as successfully complete.",
)

_TOOL_COMMITMENT_CREATE = ToolDefinition(
    name="commitment_create",
    description=(
        "Create a follow-up commitment for yourself. Surfaces in the "
        "next heartbeat whose schedule is at-or-after due_at."
    ),
    parameters=[
        ToolParameter(
            name="content",
            type=ToolParameterType.STRING,
            description="What to follow up on",
            required=True,
        ),
        ToolParameter(
            name="due_in_seconds",
            type=ToolParameterType.NUMBER,
            description="Surface at-or-after this many seconds from now.",
            required=False,
        ),
        ToolParameter(
            name="due_at",
            type=ToolParameterType.STRING,
            description="ISO-8601 absolute time alternative to due_in_seconds.",
            required=False,
        ),
    ],
)

_TOOL_COMMITMENT_COMPLETE = ToolDefinition(
    name="commitment_complete",
    description="Mark a previously-created commitment as complete.",
    parameters=[
        ToolParameter(
            name="commitment_id",
            type=ToolParameterType.STRING,
            description="The commitment id.",
            required=True,
        ),
        ToolParameter(
            name="note",
            type=ToolParameterType.STRING,
            description="Optional completion note.",
            required=False,
        ),
    ],
)

_TOOL_COMMITMENT_LIST = ToolDefinition(
    name="commitment_list",
    description="List your commitments. By default only unfinished ones.",
    parameters=[
        ToolParameter(
            name="include_completed",
            type=ToolParameterType.BOOLEAN,
            description="Include already-completed commitments.",
            required=False,
        ),
    ],
)

_TOOL_AGENT_MEMORY_SAVE = ToolDefinition(
    name="agent_memory_save",
    description=(
        "Save a learned fact to your own memory. SHORT_TERM by default; "
        "use kind='preference' or kind='decision' or kind='fact' as "
        "appropriate. Tags are free-form."
    ),
    parameters=[
        ToolParameter(
            name="content",
            type=ToolParameterType.STRING,
            description="The memory text.",
            required=True,
        ),
        ToolParameter(
            name="kind",
            type=ToolParameterType.STRING,
            description="'fact' | 'preference' | 'decision' | 'daily' | 'dream'.",
            required=False,
        ),
        ToolParameter(
            name="tags",
            type=ToolParameterType.ARRAY,
            description="Free-form tags.",
            required=False,
        ),
    ],
)

_TOOL_AGENT_MEMORY_SEARCH = ToolDefinition(
    name="agent_memory_search",
    description="Search your own memories by substring match. Recency-ordered.",
    parameters=[
        ToolParameter(
            name="query",
            type=ToolParameterType.STRING,
            description="Substring to match. Empty = recent.",
            required=False,
        ),
        ToolParameter(
            name="limit",
            type=ToolParameterType.NUMBER,
            description="Max results (default 20).",
            required=False,
        ),
    ],
)

_TOOL_AGENT_MEMORY_PROMOTE = ToolDefinition(
    name="agent_memory_review_and_promote",
    description=(
        "Review recent SHORT_TERM memories and promote durable ones to "
        "LONG_TERM with a score. Pass an array of {memory_id, score, "
        "decision} triplets (decision='promote'|'demote'|'keep')."
    ),
    parameters=[
        ToolParameter(
            name="reviews",
            type=ToolParameterType.ARRAY,
            description="List of {memory_id, score, decision}.",
            required=True,
        ),
    ],
)

# ── Phase 2 — peer messaging tools ──────────────────────────────────

_TOOL_AGENT_LIST = ToolDefinition(
    name="agent_list",
    description=(
        "List your peer agents (other agents owned by the same user). "
        "Returns name, role_label, status, conversation_id."
    ),
    parameters=[],
    slash_command="agent_list",
    slash_help="List your peer agents.",
)

_TOOL_AGENT_SEND_MESSAGE = ToolDefinition(
    name="agent_send_message",
    description=(
        "Send a fire-and-forget direct message to a peer agent. The peer's "
        "loop wakes (or, if running, picks up the message between rounds). "
        "No reply is awaited — use agent_delegate if you need a response. "
        "Pass priority=\"urgent\" to interrupt a busy peer at the next "
        "tool-call boundary instead of waiting for its current round to end."
    ),
    parameters=[
        ToolParameter(
            name="target_name",
            type=ToolParameterType.STRING,
            description="The peer agent's name.",
            required=True,
        ),
        ToolParameter(
            name="body",
            type=ToolParameterType.STRING,
            description="Message body.",
            required=True,
        ),
        ToolParameter(
            name="priority",
            type=ToolParameterType.STRING,
            description=(
                "\"urgent\" interrupts the recipient between tool calls; "
                "\"normal\" (default) waits for round boundaries."
            ),
            required=False,
        ),
    ],
    slash_command="agent_send_message",
    slash_help="DM another agent.",
)

_TOOL_AGENT_DELEGATE = ToolDefinition(
    name="agent_delegate",
    description=(
        "Send a message to a peer and await its END_TURN reply. The peer "
        "gets a system-prompt note saying it is handling a delegation; its "
        "final assistant message becomes your tool result. Errors on "
        "circular delegations or when the delegation chain depth would "
        "exceed 5. Default timeout is 600 seconds. Delegations default "
        "to priority=\"urgent\" since the caller is awaiting a reply — "
        "set priority=\"normal\" to queue behind the peer's current "
        "round work instead."
    ),
    parameters=[
        ToolParameter(
            name="target_name",
            type=ToolParameterType.STRING,
            description="The peer agent's name.",
            required=True,
        ),
        ToolParameter(
            name="instruction",
            type=ToolParameterType.STRING,
            description="What you want the peer to do.",
            required=True,
        ),
        ToolParameter(
            name="max_wait_s",
            type=ToolParameterType.NUMBER,
            description="Timeout in seconds (default 600).",
            required=False,
        ),
        ToolParameter(
            name="priority",
            type=ToolParameterType.STRING,
            description=(
                "\"urgent\" (default for delegations) interrupts the "
                "target between tool calls; \"normal\" waits for round "
                "boundaries."
            ),
            required=False,
        ),
    ],
    slash_command="agent_delegate",
    slash_help="Delegate work to another agent and await its reply.",
)


# ── Phase 4 — goal tools ─────────────────────────────────────────────

_TOOL_GOAL_CREATE = ToolDefinition(
    name="goal_create",
    description=(
        "Create a new goal you own. Optionally assign one or more peer "
        "agents (by name) at specified roles. The first assignee is "
        "promoted to DRIVER if no DRIVER is in the list. A war-room "
        "conversation is created and bound to the goal."
    ),
    parameters=[
        ToolParameter(
            name="name",
            type=ToolParameterType.STRING,
            description="Goal name (short, human-readable).",
            required=True,
        ),
        ToolParameter(
            name="description",
            type=ToolParameterType.STRING,
            description="Goal description / brief.",
            required=False,
        ),
        ToolParameter(
            name="assign_to",
            type=ToolParameterType.ARRAY,
            description=(
                "List of {agent_name, role} objects to assign. Roles: "
                "'driver' | 'collaborator' | 'reviewer'."
            ),
            required=False,
        ),
        ToolParameter(
            name="cost_cap_usd",
            type=ToolParameterType.NUMBER,
            description="Optional cost cap in USD (informational in Phase 4).",
            required=False,
        ),
    ],
    slash_command="goal_create",
    slash_help="Create a new multi-agent goal.",
)

_TOOL_GOAL_ASSIGN = ToolDefinition(
    name="goal_assign",
    description=(
        "Assign a peer agent to a goal at the given role. Only the "
        "current DRIVER on the goal may call this."
    ),
    parameters=[
        ToolParameter(
            name="goal_id",
            type=ToolParameterType.STRING,
            description="The goal id.",
            required=True,
        ),
        ToolParameter(
            name="agent_name",
            type=ToolParameterType.STRING,
            description="Peer agent name (must be same owner).",
            required=True,
        ),
        ToolParameter(
            name="role",
            type=ToolParameterType.STRING,
            description="'driver' | 'collaborator' | 'reviewer'.",
            required=True,
        ),
    ],
    slash_command="goal_assign",
    slash_help="Assign a peer agent to a goal.",
)

_TOOL_GOAL_UNASSIGN = ToolDefinition(
    name="goal_unassign",
    description=(
        "Remove an agent from a goal. Driver-only, except an agent may "
        "remove itself."
    ),
    parameters=[
        ToolParameter(
            name="goal_id",
            type=ToolParameterType.STRING,
            description="The goal id.",
            required=True,
        ),
        ToolParameter(
            name="agent_name",
            type=ToolParameterType.STRING,
            description="Agent name to unassign.",
            required=True,
        ),
    ],
    slash_command="goal_unassign",
    slash_help="Remove an agent from a goal.",
)

_TOOL_GOAL_HANDOFF = ToolDefinition(
    name="goal_handoff",
    description=(
        "Hand off DRIVER on a goal to a peer agent. Only the current "
        "DRIVER may call this. The from-agent becomes COLLABORATOR."
    ),
    parameters=[
        ToolParameter(
            name="goal_id",
            type=ToolParameterType.STRING,
            description="The goal id.",
            required=True,
        ),
        ToolParameter(
            name="target_name",
            type=ToolParameterType.STRING,
            description="Peer agent to receive DRIVER.",
            required=True,
        ),
        ToolParameter(
            name="role",
            type=ToolParameterType.STRING,
            description="Role to grant the target (default 'driver').",
            required=False,
        ),
        ToolParameter(
            name="note",
            type=ToolParameterType.STRING,
            description="Optional handoff note (stamped on both rows).",
            required=False,
        ),
    ],
    slash_command="goal_handoff",
    slash_help="Hand off a goal's driver role to a peer.",
)

_TOOL_GOAL_POST = ToolDefinition(
    name="goal_post",
    description=(
        "Post a message into a goal's war-room conversation. You must "
        "be an active assignee. ``mention`` is a list of peer-agent "
        "names that should each receive an inbox signal pointing at "
        "this post."
    ),
    parameters=[
        ToolParameter(
            name="goal_id",
            type=ToolParameterType.STRING,
            description="The goal id.",
            required=True,
        ),
        ToolParameter(
            name="body",
            type=ToolParameterType.STRING,
            description="Message body.",
            required=True,
        ),
        ToolParameter(
            name="mention",
            type=ToolParameterType.ARRAY,
            description="List of peer agent names to ping with an inbox signal.",
            required=False,
        ),
    ],
    slash_command="goal_post",
    slash_help="Post into a goal's war room.",
)

_TOOL_GOAL_STATUS = ToolDefinition(
    name="goal_status",
    description=(
        "Set a goal's status. Only the current DRIVER may call this. "
        "Statuses: 'new' | 'in_progress' | 'blocked' | 'complete' | "
        "'cancelled'. Use 'cancelled' to abandon a goal — there is no "
        "goal-deletion path."
    ),
    parameters=[
        ToolParameter(
            name="goal_id",
            type=ToolParameterType.STRING,
            description="The goal id.",
            required=True,
        ),
        ToolParameter(
            name="new_status",
            type=ToolParameterType.STRING,
            description="One of 'new', 'in_progress', 'blocked', 'complete', 'cancelled'.",
            required=True,
        ),
    ],
    slash_command="goal_status",
    slash_help="Update a goal's status.",
)

_TOOL_GOAL_SUMMARY = ToolDefinition(
    name="goal_summary",
    description=(
        "Return a JSON summary of a goal you're assigned to: name, "
        "description, status, assignees (with roles), recent posts "
        "(last 10), lifetime_cost_usd, is_dependency_blocked."
    ),
    parameters=[
        ToolParameter(
            name="goal_id",
            type=ToolParameterType.STRING,
            description="The goal id.",
            required=True,
        ),
    ],
    slash_command="goal_summary",
    slash_help="Summarize a goal you're on.",
)


# ── Phase 5 — deliverable + dependency tools ────────────────────────

_TOOL_DELIVERABLE_CREATE = ToolDefinition(
    name="deliverable_create",
    description=(
        "Create a DRAFT deliverable on a goal you're assigned to. "
        "``name`` is the logical key dependents reference (e.g., 'spec'); "
        "``kind`` is a free-form category ('spec', 'code', 'report', "
        "'image', etc.); ``content_ref`` is an optional pointer to the "
        "underlying content (typically 'workspace_file:<id>')."
    ),
    parameters=[
        ToolParameter(
            name="goal_id",
            type=ToolParameterType.STRING,
            description="The goal the deliverable belongs to.",
            required=True,
        ),
        ToolParameter(
            name="name",
            type=ToolParameterType.STRING,
            description="Logical name (dependents reference this).",
            required=True,
        ),
        ToolParameter(
            name="kind",
            type=ToolParameterType.STRING,
            description="Category: 'spec' | 'code' | 'report' | 'image' | …",
            required=True,
        ),
        ToolParameter(
            name="content_ref",
            type=ToolParameterType.STRING,
            description="Pointer to underlying content (default empty).",
            required=False,
        ),
    ],
    slash_command="deliverable_create",
    slash_help="Create a DRAFT deliverable on a goal.",
)

_TOOL_DELIVERABLE_FINALIZE = ToolDefinition(
    name="deliverable_finalize",
    description=(
        "Flip a deliverable from DRAFT to READY. Only the producer or "
        "the goal's DRIVER may call this. Finalizing a deliverable with "
        "the same ``name`` as a prior READY one supersedes the prior "
        "(marks it OBSOLETE) — only one READY per (goal, name)."
    ),
    parameters=[
        ToolParameter(
            name="deliverable_id",
            type=ToolParameterType.STRING,
            description="The deliverable id.",
            required=True,
        ),
    ],
    slash_command="deliverable_finalize",
    slash_help="Finalize a deliverable to READY.",
)

_TOOL_DELIVERABLE_SUPERSEDE = ToolDefinition(
    name="deliverable_supersede",
    description=(
        "Mark a deliverable OBSOLETE and create a new one (DRAFT, or "
        "READY if ``finalize=True``) with the same ``name`` and "
        "``kind`` on the same goal. Only the producer or the goal's "
        "DRIVER may call this."
    ),
    parameters=[
        ToolParameter(
            name="deliverable_id",
            type=ToolParameterType.STRING,
            description="The deliverable id to obsolete.",
            required=True,
        ),
        ToolParameter(
            name="new_content_ref",
            type=ToolParameterType.STRING,
            description="Content pointer for the replacement deliverable.",
            required=True,
        ),
        ToolParameter(
            name="finalize",
            type=ToolParameterType.BOOLEAN,
            description="If true, the new deliverable is created READY.",
            required=False,
        ),
    ],
    slash_command="deliverable_supersede",
    slash_help="Supersede a deliverable with a new revision.",
)

_TOOL_GOAL_ADD_DEPENDENCY = ToolDefinition(
    name="goal_add_dependency",
    description=(
        "Register that ``goal_id`` depends on ``source_goal_id`` "
        "producing a READY deliverable named "
        "``required_deliverable_name``. DRIVER-only on the dependent "
        "goal. Idempotent on (dependent, source, name). If the source "
        "already has a matching READY deliverable, the new dependency "
        "is created satisfied — and assignees on ``goal_id`` are "
        "signaled immediately."
    ),
    parameters=[
        ToolParameter(
            name="goal_id",
            type=ToolParameterType.STRING,
            description="The dependent goal (the one that gets unblocked).",
            required=True,
        ),
        ToolParameter(
            name="source_goal_id",
            type=ToolParameterType.STRING,
            description="The source goal that must produce the deliverable.",
            required=True,
        ),
        ToolParameter(
            name="required_deliverable_name",
            type=ToolParameterType.STRING,
            description="Name of the deliverable on the source goal.",
            required=True,
        ),
    ],
    slash_command="goal_add_dependency",
    slash_help="Add a goal dependency edge.",
)

_TOOL_GOAL_REMOVE_DEPENDENCY = ToolDefinition(
    name="goal_remove_dependency",
    description=(
        "Remove a goal dependency edge. Only the DRIVER on the dependent "
        "goal may call this."
    ),
    parameters=[
        ToolParameter(
            name="dependency_id",
            type=ToolParameterType.STRING,
            description="The dependency row id to remove.",
            required=True,
        ),
    ],
    slash_command="goal_remove_dependency",
    slash_help="Remove a goal dependency edge.",
)


# Maximum allowed delegation chain depth (caller + targets). The 5th
# delegation in a chain is rejected before the signal fires.
_DELEGATION_DEPTH_CAP = 5


# Allowed values for InboxSignal.priority / the ``priority`` argument
# on ``agent_send_message`` and ``agent_delegate``.
_VALID_PRIORITIES: frozenset[str] = frozenset({"urgent", "normal"})


def _parse_priority(raw: Any, default: str) -> tuple[str | None, str | None]:
    """Validate a ``priority`` argument from an AI tool invocation.

    Returns ``(value, error_message)`` — exactly one is non-None.
    Missing / empty ``raw`` yields ``default``; any other value is
    lowercased + stripped and must be in :data:`_VALID_PRIORITIES`.
    """
    if raw is None:
        return default, None
    text = str(raw).lower().strip()
    if not text:
        return default, None
    if text not in _VALID_PRIORITIES:
        valid = ", ".join(sorted(_VALID_PRIORITIES))
        return None, f"priority must be one of: {valid}"
    return text, None


# ── Module-level helpers ─────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(UTC)


def _agent_to_dict(a: Agent) -> dict[str, Any]:
    """Storage row representation. ``status`` serializes as .value; datetimes as ISO."""
    return {
        "_id": a.id,
        "owner_user_id": a.owner_user_id,
        "name": a.name,
        "display_name": a.display_name,
        "role_label": a.role_label,
        "persona": a.persona,
        "system_prompt": a.system_prompt,
        "procedural_rules": a.procedural_rules,
        "profile_id": a.profile_id,
        "conversation_id": a.conversation_id,
        "status": a.status.value,
        "avatar_kind": a.avatar_kind,
        "avatar_value": a.avatar_value,
        "lifetime_cost_usd": a.lifetime_cost_usd,
        "cost_cap_usd": a.cost_cap_usd,
        "tools_include": a.tools_include,
        "tools_exclude": a.tools_exclude,
        "heartbeat_enabled": a.heartbeat_enabled,
        "heartbeat_interval_s": a.heartbeat_interval_s,
        "heartbeat_checklist": a.heartbeat_checklist,
        "dream_enabled": a.dream_enabled,
        "dream_quiet_hours": a.dream_quiet_hours,
        "dream_probability": a.dream_probability,
        "dream_max_per_night": a.dream_max_per_night,
        "created_at": a.created_at.isoformat(),
        "updated_at": a.updated_at.isoformat(),
    }


def _agent_from_dict(row: dict[str, Any]) -> Agent:
    return Agent(
        id=row["_id"],
        owner_user_id=row["owner_user_id"],
        name=row["name"],
        display_name=row.get("display_name") or row["name"],
        role_label=row.get("role_label", ""),
        persona=row.get("persona", ""),
        system_prompt=row.get("system_prompt", ""),
        procedural_rules=row.get("procedural_rules", ""),
        profile_id=row.get("profile_id", "standard"),
        conversation_id=row.get("conversation_id", ""),
        status=AgentStatus(row.get("status", "enabled")),
        avatar_kind=row.get("avatar_kind", "emoji"),
        avatar_value=row.get("avatar_value", "🤖"),
        lifetime_cost_usd=float(row.get("lifetime_cost_usd", 0.0)),
        cost_cap_usd=row.get("cost_cap_usd"),
        tools_include=row.get("tools_include"),
        tools_exclude=row.get("tools_exclude"),
        heartbeat_enabled=bool(row.get("heartbeat_enabled", True)),
        heartbeat_interval_s=int(row.get("heartbeat_interval_s", 1800)),
        heartbeat_checklist=row.get("heartbeat_checklist", ""),
        dream_enabled=bool(row.get("dream_enabled", False)),
        dream_quiet_hours=row.get("dream_quiet_hours", "22:00-06:00"),
        dream_probability=float(row.get("dream_probability", 0.1)),
        dream_max_per_night=int(row.get("dream_max_per_night", 3)),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _memory_to_dict(m: AgentMemory) -> dict[str, Any]:
    return {
        "_id": m.id,
        "agent_id": m.agent_id,
        "content": m.content,
        "state": m.state.value,
        "kind": m.kind,
        "tags": sorted(m.tags),
        "score": m.score,
        "created_at": m.created_at.isoformat(),
        "last_used_at": m.last_used_at.isoformat() if m.last_used_at else None,
    }


def _memory_from_dict(row: dict[str, Any]) -> AgentMemory:
    return AgentMemory(
        id=row["_id"],
        agent_id=row["agent_id"],
        content=row.get("content", ""),
        state=MemoryState(row.get("state", "short_term")),
        kind=row.get("kind", "fact"),
        tags=frozenset(row.get("tags", [])),
        score=float(row.get("score", 0.0)),
        created_at=datetime.fromisoformat(row["created_at"]),
        last_used_at=(
            datetime.fromisoformat(row["last_used_at"])
            if row.get("last_used_at") else None
        ),
    )


def _run_to_dict(r: Run) -> dict[str, Any]:
    return {
        "_id": r.id,
        "agent_id": r.agent_id,
        "triggered_by": r.triggered_by,
        "trigger_context": r.trigger_context,
        "started_at": r.started_at.isoformat(),
        "status": r.status.value,
        "conversation_id": r.conversation_id,
        "delegation_id": r.delegation_id,
        "ended_at": r.ended_at.isoformat() if r.ended_at else None,
        "final_message_text": r.final_message_text,
        "rounds_used": r.rounds_used,
        "tokens_in": r.tokens_in,
        "tokens_out": r.tokens_out,
        "cost_usd": r.cost_usd,
        "error": r.error,
        "awaiting_user_input": r.awaiting_user_input,
        "pending_question": r.pending_question,
        "pending_actions": list(r.pending_actions),
    }


def _run_from_dict(row: dict[str, Any]) -> Run:
    return Run(
        id=row["_id"],
        agent_id=row["agent_id"],
        triggered_by=row.get("triggered_by", "manual"),
        trigger_context=row.get("trigger_context", {}),
        started_at=datetime.fromisoformat(row["started_at"]),
        status=RunStatus(row.get("status", "running")),
        conversation_id=row.get("conversation_id", ""),
        delegation_id=row.get("delegation_id", ""),
        ended_at=datetime.fromisoformat(row["ended_at"]) if row.get("ended_at") else None,
        final_message_text=row.get("final_message_text"),
        rounds_used=int(row.get("rounds_used", 0)),
        tokens_in=int(row.get("tokens_in", 0)),
        tokens_out=int(row.get("tokens_out", 0)),
        cost_usd=float(row.get("cost_usd", 0.0)),
        error=row.get("error"),
        awaiting_user_input=bool(row.get("awaiting_user_input", False)),
        pending_question=row.get("pending_question"),
        pending_actions=list(row.get("pending_actions", [])),
    )


def _signal_to_dict(s: InboxSignal) -> dict[str, Any]:
    return {
        "_id": s.id,
        "agent_id": s.agent_id,
        "signal_kind": s.signal_kind,
        "body": s.body,
        "sender_kind": s.sender_kind,
        "sender_id": s.sender_id,
        "sender_name": s.sender_name,
        "source_conv_id": s.source_conv_id,
        "source_message_id": s.source_message_id,
        "delegation_id": s.delegation_id,
        "metadata": s.metadata,
        "priority": s.priority,
        "created_at": s.created_at.isoformat(),
        "processed_at": s.processed_at.isoformat() if s.processed_at else None,
    }


def _signal_from_dict(row: dict[str, Any]) -> InboxSignal:
    return InboxSignal(
        id=row["_id"],
        agent_id=row["agent_id"],
        signal_kind=row.get("signal_kind", "inbox"),
        body=row.get("body", ""),
        sender_kind=row.get("sender_kind", "user"),
        sender_id=row.get("sender_id", ""),
        sender_name=row.get("sender_name", ""),
        source_conv_id=row.get("source_conv_id", ""),
        source_message_id=row.get("source_message_id", ""),
        delegation_id=row.get("delegation_id", ""),
        metadata=row.get("metadata", {}),
        priority=row.get("priority", "normal"),
        created_at=datetime.fromisoformat(row["created_at"]),
        processed_at=(
            datetime.fromisoformat(row["processed_at"])
            if row.get("processed_at") else None
        ),
    )


def _commitment_from_dict(row: dict[str, Any]) -> Commitment:
    return Commitment(
        id=row["_id"],
        agent_id=row["agent_id"],
        content=row.get("content", ""),
        due_at=datetime.fromisoformat(row["due_at"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        completed_at=datetime.fromisoformat(row["completed_at"]) if row.get("completed_at") else None,
        completion_note=row.get("completion_note", ""),
    )


def _commitment_to_dict(c: Commitment) -> dict[str, Any]:
    return {
        "_id": c.id,
        "agent_id": c.agent_id,
        "content": c.content,
        "due_at": c.due_at.isoformat(),
        "created_at": c.created_at.isoformat(),
        "completed_at": c.completed_at.isoformat() if c.completed_at else None,
        "completion_note": c.completion_note,
    }


def _goal_to_dict(g: Goal) -> dict[str, Any]:
    return {
        "_id": g.id,
        "owner_user_id": g.owner_user_id,
        "name": g.name,
        "description": g.description,
        "status": g.status.value,
        "war_room_conversation_id": g.war_room_conversation_id,
        "cost_cap_usd": g.cost_cap_usd,
        "lifetime_cost_usd": g.lifetime_cost_usd,
        "created_at": g.created_at.isoformat(),
        "updated_at": g.updated_at.isoformat(),
        "completed_at": g.completed_at.isoformat() if g.completed_at else None,
    }


def _goal_from_dict(row: dict[str, Any]) -> Goal:
    return Goal(
        id=row["_id"],
        owner_user_id=row["owner_user_id"],
        name=row.get("name", ""),
        description=row.get("description", ""),
        status=GoalStatus(row.get("status", "new")),
        war_room_conversation_id=row.get("war_room_conversation_id", ""),
        cost_cap_usd=row.get("cost_cap_usd"),
        lifetime_cost_usd=float(row.get("lifetime_cost_usd", 0.0)),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        completed_at=(
            datetime.fromisoformat(row["completed_at"])
            if row.get("completed_at") else None
        ),
    )


def _goal_assignment_to_dict(ga: GoalAssignment) -> dict[str, Any]:
    return {
        "_id": ga.id,
        "goal_id": ga.goal_id,
        "agent_id": ga.agent_id,
        "role": ga.role.value,
        "assigned_at": ga.assigned_at.isoformat(),
        "assigned_by": ga.assigned_by,
        "removed_at": ga.removed_at.isoformat() if ga.removed_at else None,
        "handoff_note": ga.handoff_note,
    }


def _goal_assignment_from_dict(row: dict[str, Any]) -> GoalAssignment:
    return GoalAssignment(
        id=row["_id"],
        goal_id=row["goal_id"],
        agent_id=row["agent_id"],
        role=AssignmentRole(row.get("role", "collaborator")),
        assigned_at=datetime.fromisoformat(row["assigned_at"]),
        assigned_by=row.get("assigned_by", ""),
        removed_at=(
            datetime.fromisoformat(row["removed_at"])
            if row.get("removed_at") else None
        ),
        handoff_note=row.get("handoff_note", ""),
    )


def _deliverable_to_dict(d: Deliverable) -> dict[str, Any]:
    return {
        "_id": d.id,
        "goal_id": d.goal_id,
        "name": d.name,
        "kind": d.kind,
        "state": d.state.value,
        "produced_by_agent_id": d.produced_by_agent_id,
        "content_ref": d.content_ref,
        "created_at": d.created_at.isoformat(),
        "finalized_at": d.finalized_at.isoformat() if d.finalized_at else None,
    }


def _deliverable_from_dict(row: dict[str, Any]) -> Deliverable:
    return Deliverable(
        id=row["_id"],
        goal_id=row["goal_id"],
        name=row.get("name", ""),
        kind=row.get("kind", ""),
        state=DeliverableState(row.get("state", "draft")),
        produced_by_agent_id=row.get("produced_by_agent_id", ""),
        content_ref=row.get("content_ref", ""),
        created_at=datetime.fromisoformat(row["created_at"]),
        finalized_at=(
            datetime.fromisoformat(row["finalized_at"])
            if row.get("finalized_at") else None
        ),
    )


def _dependency_to_dict(d: GoalDependency) -> dict[str, Any]:
    return {
        "_id": d.id,
        "dependent_goal_id": d.dependent_goal_id,
        "source_goal_id": d.source_goal_id,
        "required_deliverable_name": d.required_deliverable_name,
        "satisfied_at": d.satisfied_at.isoformat() if d.satisfied_at else None,
    }


def _dependency_from_dict(row: dict[str, Any]) -> GoalDependency:
    return GoalDependency(
        id=row["_id"],
        dependent_goal_id=row["dependent_goal_id"],
        source_goal_id=row["source_goal_id"],
        required_deliverable_name=row.get("required_deliverable_name", ""),
        satisfied_at=(
            datetime.fromisoformat(row["satisfied_at"])
            if row.get("satisfied_at") else None
        ),
    )


class AgentService(Service):
    """Manages durable agent identities and run orchestration.

    Capabilities declared:

    - ``agent`` — satisfies ``AgentProvider``.
    - ``ai_tools`` — exposes AI tool definitions (Task 14).
    - ``ws_handlers`` — exposes RPC handlers (Task 6).

    Requires:

    - ``entity_storage`` — persists all agent entity collections.
    - ``event_bus`` — publishes state-change events.
    - ``ai_chat`` — drives agent runs.
    - ``scheduler`` — re-arms heartbeat triggers.
    """

    tool_provider_name = "agent"
    config_namespace = "agent_service"
    config_category = "Intelligence"
    slash_namespace = "agents"

    def __init__(self) -> None:
        # Entity storage backend (bound in start())
        self._storage: StorageBackend | None = None

        # Raw EventBus instance from EventBusProvider (bound in start())
        self._event_bus: Any = None

        # AIProvider capability (bound in start())
        self._ai: AIProvider | None = None

        # AIToolDiscoveryProvider capability (bound in start())
        # — used by agents.tools.list_available to enumerate tools.
        self._tool_discovery: AIToolDiscoveryProvider | None = None

        # ServiceResolver reference for late-bound capability lookups
        self._resolver: ServiceResolver | None = None

        # SchedulerProvider capability (bound in start())
        self._scheduler: SchedulerProvider | None = None

        # Agent IDs that currently have a run in progress
        self._running_agents: set[str] = set()

        # Per-agent inbox queues: agent_id → list of pending InboxSignals
        self._inboxes: dict[str, list[InboxSignal]] = {}

        # Pending delegations: delegation_id → Future awaiting target's
        # final assistant message. Resolved in _run_agent_internal when
        # a delegation-triggered run finishes.
        self._pending_delegations: dict[str, asyncio.Future[str]] = {}

        # Per-agent urgent-signal pending flag. Set by ``_signal_agent``
        # whenever it persists a signal with ``priority="urgent"`` for
        # the agent; consumed (cleared) by ``_drain_inbox``. Drives the
        # ``mid_round_interrupt`` callback passed to ``AIService.chat``
        # so a busy agent breaks out of its tool-call loop at the next
        # safe boundary instead of waiting for the round to end.
        self._urgent_pending: dict[str, bool] = {}

        # Service-level defaults merged into create_agent calls
        self._defaults: dict[str, Any] = {}

        # Toggleable enable flag — set in ``start()`` after consulting the
        # configuration capability. When ``False`` the service skips all
        # capability binding and exposes no WS handlers; the web nav filter
        # also drops the /agents and /goals groups via ``svc.enabled``.
        self._enabled: bool = True

    # ── Configurable ────────────────────────────────────────────────

    def config_params(self) -> list[ConfigParam]:
        """Describe all operator-tunable defaults for new agents."""
        return [
            ConfigParam(
                key="enabled",
                type=ToolParameterType.BOOLEAN,
                description=(
                    "Whether the agent service is enabled. Disabling skips "
                    "service start, exposes no WS handlers, and hides "
                    "/agents and /goals from the nav."
                ),
                default=True,
            ),
            ConfigParam(
                key="default_persona",
                type=ToolParameterType.STRING,
                description="Default persona text injected into new agents' system prompt.",
                default=_DEFAULT_PERSONA,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="default_system_prompt",
                type=ToolParameterType.STRING,
                description="Default system-prompt body for new agents.",
                default=_DEFAULT_SYSTEM_PROMPT,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="default_procedural_rules",
                type=ToolParameterType.STRING,
                description="Default procedural rules injected into new agents' system prompt.",
                default=_DEFAULT_PROCEDURAL_RULES,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="default_heartbeat_interval_s",
                type=ToolParameterType.NUMBER,
                description="Default heartbeat interval in seconds for new agents.",
                default=1800,
            ),
            ConfigParam(
                key="default_heartbeat_checklist",
                type=ToolParameterType.STRING,
                description="Default heartbeat checklist for new agents.",
                default=_DEFAULT_HEARTBEAT_CHECKLIST,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="default_dream_enabled",
                type=ToolParameterType.BOOLEAN,
                description="Whether dreaming is enabled by default for new agents.",
                default=False,
            ),
            ConfigParam(
                key="default_dream_quiet_hours",
                type=ToolParameterType.STRING,
                description="Default dream quiet-hours window for new agents (e.g. '22:00-06:00').",
                default="22:00-06:00",
            ),
            ConfigParam(
                key="default_dream_probability",
                type=ToolParameterType.NUMBER,
                description="Default probability (0–1) that a dream run fires in each heartbeat.",
                default=0.1,
            ),
            ConfigParam(
                key="default_dream_max_per_night",
                type=ToolParameterType.INTEGER,
                description="Default maximum dream runs allowed per night for new agents.",
                default=3,
            ),
            ConfigParam(
                key="default_avatar_kind",
                type=ToolParameterType.STRING,
                description="Default avatar kind for new agents (e.g. 'emoji', 'url').",
                default="emoji",
            ),
            ConfigParam(
                key="default_avatar_value",
                type=ToolParameterType.STRING,
                description="Default avatar value for new agents (emoji character or image URL).",
                default="🤖",
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        """Cache the full config section as ``_defaults``, merged over the
        ConfigParam defaults.

        ``ConfigurationService.get_section`` returns whatever is stored in
        YAML/DB without merging param defaults; if the operator has never
        edited an ``agent_service.*`` value, the section is ``{}``. We
        merge the param defaults here so the SPA's ``agents.get_defaults``
        always returns sensible prompt fields and the create-form
        pre-fills as expected.

        Also caches the ``enabled`` flag onto ``self._enabled`` — note that
        ``start()`` checks the same key separately so a disabled service
        skips capability binding entirely.
        """
        merged: dict[str, Any] = {}
        for param in self.config_params():
            if param.default is not None:
                merged[param.key] = param.default
        merged.update(config)
        self._defaults = merged
        self._enabled = bool(merged.get("enabled", True))

    # ── Service lifecycle ────────────────────────────────────────────

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="agent",
            capabilities=frozenset({"agent", "ai_tools", "ws_handlers"}),
            requires=frozenset({"entity_storage", "event_bus", "ai_chat", "scheduler"}),
            ai_calls=frozenset({_AI_CALL_NAME}),
            # Surfaces the ``enabled`` ConfigParam in the Settings UI's
            # "Services" toggle section alongside Greeting / Knowledge /
            # Vision / etc. — the standard place for service on/off.
            toggleable=True,
        )

    async def start(self, resolver: ServiceResolver) -> None:
        """Bind capabilities and prepare the service for requests."""
        self._resolver = resolver

        # Check enabled flag before binding any capability. When disabled,
        # the web nav filter drops /agents and /goals via ``svc.enabled``,
        # ``get_ws_handlers`` returns ``{}``, and no heartbeats are armed.
        # Also seed ``self._defaults`` from the resolved section so the
        # SPA's ``agents.get_defaults`` can pre-fill the create form on
        # first run — the configuration service only fires
        # ``on_config_changed`` when the user explicitly changes a value,
        # so without this initial pull the form would render with empty
        # prompt fields.
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                section = config_svc.get_section(self.config_namespace)
                await self.on_config_changed(section)
                if not self._enabled:
                    logger.info("AgentService disabled")
                    return

        self._enabled = True

        # Bind entity storage
        storage_svc = resolver.require_capability("entity_storage")
        if not isinstance(storage_svc, StorageProvider):
            raise RuntimeError(
                "entity_storage capability does not implement StorageProvider"
            )
        self._storage = storage_svc.backend

        # Bind event bus
        event_bus_svc = resolver.require_capability("event_bus")
        if not isinstance(event_bus_svc, EventBusProvider):
            raise RuntimeError(
                "event_bus capability does not implement EventBusProvider"
            )
        self._event_bus = event_bus_svc.bus

        # Bind AI chat capability
        ai_svc = resolver.require_capability("ai_chat")
        if not isinstance(ai_svc, AIProvider):
            raise RuntimeError(
                "ai_chat capability does not implement AIProvider"
            )
        self._ai = ai_svc

        # The same service must also expose AIToolDiscoveryProvider so the
        # agents.tools.list_available handler can enumerate tools without
        # importing the concrete AI service. AIService satisfies both, so
        # this is a hard requirement.
        if not isinstance(ai_svc, AIToolDiscoveryProvider):
            raise RuntimeError(
                "ai_chat capability does not implement AIToolDiscoveryProvider"
            )
        self._tool_discovery = ai_svc

        # Bind scheduler capability
        scheduler_svc = resolver.require_capability("scheduler")
        if not isinstance(scheduler_svc, SchedulerProvider):
            raise RuntimeError(
                "scheduler capability does not implement SchedulerProvider"
            )
        self._scheduler = scheduler_svc

        # Task 5: index creation goes here.
        # Task 8: run rehydration goes here.

        # Re-arm heartbeats for every ENABLED agent on service start.
        rows = await self._storage.query(
            Query(collection=_AGENTS_COLLECTION, filters=[])
        )
        for r in rows:
            a = _agent_from_dict(r)
            if a.status is AgentStatus.ENABLED and a.heartbeat_enabled:
                await self._arm_heartbeat(a)

        # Restore unprocessed inbox signals into in-memory cache.
        await self._rehydrate_inboxes()

        logger.info("AgentService started")

    async def stop(self) -> None:
        """Graceful shutdown — disarm all heartbeat scheduler jobs."""
        self._inboxes.clear()
        if self._storage:
            rows = await self._storage.query(Query(collection=_AGENTS_COLLECTION, filters=[]))
            for r in rows:
                await self._disarm_heartbeat(r["_id"])
        logger.info("AgentService stopped")

    # ── AgentProvider — CRUD (Task 5) ───────────────────────────────

    async def create_agent(
        self,
        *,
        owner_user_id: str,
        name: str,
        **fields: Any,
    ) -> Agent:
        """Create and persist a new Agent entity."""
        if self._storage is None:
            raise RuntimeError("not started")
        if not _NAME_PATTERN.match(name):
            raise ValueError(f"name {name!r} must match {_NAME_PATTERN.pattern}")

        # Uniqueness: same-owner, same-name collision rejected.
        existing = await self._storage.query(
            Query(
                collection=_AGENTS_COLLECTION,
                filters=[
                    Filter(field="owner_user_id", op=FilterOp.EQ, value=owner_user_id),
                    Filter(field="name", op=FilterOp.EQ, value=name),
                ],
            )
        )
        if existing:
            raise ValueError(f"name already in use: {name}")

        # tools_include and tools_exclude are mutually exclusive. Either
        # an allowlist OR a denylist — never both.
        tools_include = fields.get("tools_include")
        tools_exclude = fields.get("tools_exclude")
        if tools_include is not None and tools_exclude is not None:
            raise ValueError(
                "tools_include and tools_exclude are mutually exclusive"
            )

        defaults = self._defaults
        now = _now()
        # display_name defaults to the slug when not supplied (so a brand-new
        # agent is at least nameable). The SPA derives a slug from the
        # display_name on the create form, but server-side both fields are
        # independent — we don't re-derive here.
        display_name_raw = fields.get("display_name", "")
        if isinstance(display_name_raw, str):
            display_name = display_name_raw.strip() or name
        else:
            display_name = name

        a = Agent(
            id=f"ag_{uuid.uuid4().hex[:12]}",
            owner_user_id=owner_user_id,
            name=name,
            display_name=display_name,
            role_label=fields.get("role_label", ""),
            persona=fields.get("persona", defaults.get("default_persona", "")),
            system_prompt=fields.get("system_prompt", defaults.get("default_system_prompt", "")),
            procedural_rules=fields.get("procedural_rules", defaults.get("default_procedural_rules", "")),
            profile_id=fields.get("profile_id", "standard"),
            conversation_id="",
            status=AgentStatus.ENABLED,
            avatar_kind=fields.get("avatar_kind", defaults.get("default_avatar_kind", "emoji")),
            avatar_value=fields.get("avatar_value", defaults.get("default_avatar_value", "🤖")),
            lifetime_cost_usd=0.0,
            cost_cap_usd=fields.get("cost_cap_usd"),
            tools_include=tools_include,
            tools_exclude=tools_exclude,
            heartbeat_enabled=fields.get("heartbeat_enabled", True),
            heartbeat_interval_s=fields.get(
                "heartbeat_interval_s",
                int(defaults.get("default_heartbeat_interval_s", 1800)),
            ),
            heartbeat_checklist=fields.get("heartbeat_checklist", defaults.get("default_heartbeat_checklist", "")),
            dream_enabled=fields.get("dream_enabled", bool(defaults.get("default_dream_enabled", False))),
            dream_quiet_hours=fields.get("dream_quiet_hours", defaults.get("default_dream_quiet_hours", "22:00-06:00")),
            dream_probability=fields.get(
                "dream_probability",
                float(defaults.get("default_dream_probability", 0.1)),
            ),
            dream_max_per_night=fields.get(
                "dream_max_per_night",
                int(defaults.get("default_dream_max_per_night", 3)),
            ),
            created_at=now,
            updated_at=now,
        )
        await self._storage.put(_AGENTS_COLLECTION, a.id, _agent_to_dict(a))
        await self._arm_heartbeat(a)
        await self._publish("agent.created", {"agent_id": a.id, "owner_user_id": a.owner_user_id})
        return a

    async def get_agent(self, agent_id: str) -> Agent | None:
        """Fetch an Agent by ID. Returns None if not found."""
        if self._storage is None:
            raise RuntimeError("not started")
        row = await self._storage.get(_AGENTS_COLLECTION, agent_id)
        if row is None:
            return None
        return _agent_from_dict(row)

    async def list_agents(
        self,
        *,
        owner_user_id: str | None = None,
    ) -> list[Agent]:
        """List agents, optionally filtered by owner."""
        if self._storage is None:
            raise RuntimeError("not started")
        filters = (
            []
            if owner_user_id is None
            else [Filter(field="owner_user_id", op=FilterOp.EQ, value=owner_user_id)]
        )
        rows = await self._storage.query(Query(collection=_AGENTS_COLLECTION, filters=filters))
        return [_agent_from_dict(r) for r in rows]

    async def update_agent(self, agent_id: str, patch: dict[str, Any]) -> Agent:
        """Apply a partial update to an agent. Only known fields may be patched."""
        if self._storage is None:
            raise RuntimeError("not started")
        row = await self._storage.get(_AGENTS_COLLECTION, agent_id)
        if row is None:
            raise KeyError(agent_id)
        _allowed_patch_fields = {
            "display_name",
            "role_label", "persona", "system_prompt", "procedural_rules",
            "profile_id", "avatar_kind", "avatar_value", "cost_cap_usd",
            "tools_include", "tools_exclude",
            "heartbeat_enabled", "heartbeat_interval_s",
            "heartbeat_checklist", "dream_enabled", "dream_quiet_hours",
            "dream_probability", "dream_max_per_night", "status",
        }
        for k, v in patch.items():
            if k not in _allowed_patch_fields:
                raise ValueError(f"field not patchable: {k}")
            if k == "status":
                # Coerce to AgentStatus to validate, then store the canonical
                # string value. Accepts either an AgentStatus or its .value.
                row[k] = AgentStatus(v).value if not isinstance(v, AgentStatus) else v.value
            else:
                row[k] = v
        # Enforce the include/exclude mutex on the merged row, not just the
        # patch — patching one to a value when the other is already set
        # would otherwise sneak past.
        if row.get("tools_include") is not None and row.get("tools_exclude") is not None:
            raise ValueError(
                "tools_include and tools_exclude are mutually exclusive"
            )
        row["updated_at"] = _now().isoformat()
        await self._storage.put(_AGENTS_COLLECTION, agent_id, row)
        updated = _agent_from_dict(row)
        # Re-arm (or disarm) heartbeat whenever any agent field changes.
        # A DISABLED agent must never have an armed heartbeat, regardless of
        # heartbeat_enabled. _arm_heartbeat is idempotent and a no-op when
        # heartbeat_enabled=False.
        if updated.status is AgentStatus.ENABLED and updated.heartbeat_enabled:
            await self._arm_heartbeat(updated)
        else:
            await self._disarm_heartbeat(agent_id)
        await self._publish("agent.updated", {"agent_id": updated.id})
        return updated

    async def delete_agent(self, agent_id: str) -> bool:
        """Delete the agent and cascade-delete its memories, triggers,
        commitments, inbox signals, and runs.

        Returns True if the agent existed and was deleted, False if not found.
        Also removes the agent's avatar directory if any avatar bytes
        were ever uploaded for it (best-effort — logs and continues on
        I/O failure so a borked filesystem can't keep an agent row alive).
        """
        if self._storage is None:
            raise RuntimeError("not started")
        row = await self._storage.get(_AGENTS_COLLECTION, agent_id)
        if row is None:
            return False
        await self._disarm_heartbeat(agent_id)
        await self._storage.delete(_AGENTS_COLLECTION, agent_id)
        # Cascade-delete related collections.
        for coll in (
            _AGENT_MEMORIES_COLLECTION,
            _AGENT_TRIGGERS_COLLECTION,
            _AGENT_COMMITMENTS_COLLECTION,
            _AGENT_INBOX_SIGNALS_COLLECTION,
            _AGENT_RUNS_COLLECTION,
        ):
            related = await self._storage.query(
                Query(
                    collection=coll,
                    filters=[Filter(field="agent_id", op=FilterOp.EQ, value=agent_id)],
                )
            )
            for r in related:
                await self._storage.delete(coll, r["_id"])
        # Drop the on-disk avatar bytes if any were uploaded. Done
        # after the storage row is gone so a half-deleted state can't
        # resurrect a stale avatar pointer.
        self._remove_avatar_dir(agent_id)
        await self._publish("agent.deleted", {"agent_id": agent_id})
        return True

    async def set_agent_avatar(
        self, agent_id: str, *, filename: str
    ) -> Agent:
        """Mark *agent_id* as having an image avatar stored at *filename*.

        Routes update_agent so the standard ``agent.updated`` event
        fires for WS subscribers — keeps the avatar upload route a thin
        bytes-handling layer with no entity-shape knowledge.
        """
        return await self.update_agent(
            agent_id,
            {"avatar_kind": "image", "avatar_value": filename},
        )

    def _remove_avatar_dir(self, agent_id: str) -> None:
        """Delete the on-disk avatar directory for *agent_id* if present.

        Avatars live under ``<DATA_DIR>/agent-avatars/<agent_id>/``.
        Best-effort by design: a borked filesystem must not block
        deletion of the entity row, so I/O errors are logged and
        swallowed rather than re-raised.

        Path resolution is duplicated here (the avatar route also
        knows the layout) rather than imported from ``web.routes`` to
        keep ``core/services/`` from depending on ``web/`` — the layer
        rules are unambiguous on that direction.
        """
        import shutil
        from pathlib import Path

        from gilbert.config import DATA_DIR

        # ``Path.name`` strips path components from the supplied id as
        # a defense-in-depth measure even though ids come from storage.
        target = DATA_DIR / "agent-avatars" / Path(agent_id).name
        if not target.exists():
            return
        try:
            shutil.rmtree(target)
        except Exception:
            logger.exception("failed to remove avatar dir for agent %s", agent_id)

    # ── Heartbeat scheduling (Task 10) ──────────────────────────────

    async def _arm_heartbeat(self, a: Agent) -> None:
        """Register a heartbeat scheduler job for this agent.

        Idempotent: removes any existing job first, then adds a fresh one.
        A no-op if the scheduler is not bound or heartbeat is disabled.
        """
        if self._scheduler is None or not a.heartbeat_enabled:
            return
        job_name = f"heartbeat_{a.id}"

        async def _cb() -> None:
            await self._on_heartbeat_fired(a.id)

        try:
            self._scheduler.remove_job(job_name)
        except Exception:
            pass
        self._scheduler.add_job(
            name=job_name,
            schedule=Schedule.every(a.heartbeat_interval_s),
            callback=_cb,
            system=True,
        )

    async def _disarm_heartbeat(self, agent_id: str) -> None:
        """Remove the heartbeat scheduler job for *agent_id*, if any."""
        if self._scheduler is None:
            return
        try:
            self._scheduler.remove_job(f"heartbeat_{agent_id}")
        except Exception:
            pass

    async def _on_heartbeat_fired(self, agent_id: str) -> None:
        """Scheduler callback — fire a heartbeat run if the agent is
        still ENABLED and not already running."""
        a = await self.get_agent(agent_id)
        if a is None or a.status is not AgentStatus.ENABLED:
            await self._disarm_heartbeat(agent_id)
            return
        if agent_id in self._running_agents:
            # In-flight run; skip silently. The heartbeat re-fires next interval.
            return
        try:
            self._running_agents.add(agent_id)
            await self._run_agent_internal(
                a, triggered_by="heartbeat",
                trigger_context={}, user_message=None,
            )
        finally:
            self._running_agents.discard(agent_id)

    # ── InboxSignal dispatch (Task 11) ──────────────────────────────────

    async def _signal_agent(
        self,
        *,
        agent_id: str,
        signal_kind: str,
        body: str,
        sender_kind: str,
        sender_id: str,
        sender_name: str,
        source_conv_id: str = "",
        source_message_id: str = "",
        delegation_id: str = "",
        metadata: dict[str, Any] | None = None,
        priority: str = "normal",
    ) -> InboxSignal:
        """Create, persist, and dispatch an InboxSignal for *agent_id*.

        If the agent is currently idle (not in ``_running_agents``) and
        ENABLED, a new run is spawned via ``asyncio.create_task``; the
        dispatcher returns immediately without waiting for the run to finish.

        If the agent is busy, the signal is enqueued in the in-memory cache
        and persisted; the next round will drain it via ``_drain_inbox``.
        """
        if self._storage is None:
            raise RuntimeError("not started")

        sig = InboxSignal(
            id=f"sig_{uuid.uuid4().hex[:12]}",
            agent_id=agent_id,
            signal_kind=signal_kind,
            body=body,
            sender_kind=sender_kind,
            sender_id=sender_id,
            sender_name=sender_name,
            source_conv_id=source_conv_id,
            source_message_id=source_message_id,
            delegation_id=delegation_id,
            metadata=metadata or {},
            priority=priority,
            created_at=_now(),
            processed_at=None,
        )

        # Persist before touching in-memory state so restart survives a crash here.
        await self._storage.put(
            _AGENT_INBOX_SIGNALS_COLLECTION, sig.id, _signal_to_dict(sig)
        )

        # Mark the agent as having an urgent signal pending. Set AFTER
        # persistence so a crash mid-write doesn't leave a flag with no
        # backing row. Cleared in ``_drain_inbox``.
        if priority == "urgent":
            self._urgent_pending[agent_id] = True

        # Append to in-memory inbox cache.
        self._inboxes.setdefault(agent_id, []).append(sig)

        # If the agent is idle and ENABLED, fire a run immediately.
        if agent_id not in self._running_agents:
            a = await self.get_agent(agent_id)
            if a is not None and a.status is AgentStatus.ENABLED:
                asyncio.create_task(
                    self._run_with_signal(agent_id, signal_kind, sig),
                    name=f"agent-run-{agent_id}",
                )

        return sig

    async def _run_with_signal(
        self,
        agent_id: str,
        signal_kind: str,
        sig: InboxSignal,
    ) -> None:
        """Spawn point for signal-triggered agent runs.

        Re-checks that the agent is still idle (race-safe), re-fetches it
        (could have been disabled between dispatch and now), then runs the
        agent under the ``_running_agents`` guard.

        Forwards the delegation chain (carried in ``sig.metadata['chain']``)
        and ``sig.delegation_id`` into ``trigger_context`` so the run can
        propagate them to nested delegations and resolve the awaiting Future
        on completion.
        """
        if agent_id in self._running_agents:
            # Raced with another trigger — skip; the in-flight run will
            # pick up the signal via _drain_inbox on its next round.
            return
        a = await self.get_agent(agent_id)
        if a is None or a.status is not AgentStatus.ENABLED:
            return
        self._running_agents.add(agent_id)
        try:
            chain_raw = sig.metadata.get("chain", []) if sig.metadata else []
            chain = [str(x) for x in chain_raw] if isinstance(chain_raw, list) else []
            await self._run_agent_internal(
                a,
                triggered_by=signal_kind,
                trigger_context={
                    "signal_id": sig.id,
                    "sender_id": sig.sender_id,
                    "chain": chain,
                    "delegation_id": sig.delegation_id,
                },
                user_message=None,
            )
        finally:
            self._running_agents.discard(agent_id)

    async def _drain_inbox(self, agent_id: str) -> list[InboxSignal]:
        """Pop all pending inbox signals for *agent_id*, mark them processed,
        and return them so the caller can include them in the next round's prompt.
        """
        if self._storage is None:
            return []

        sigs = self._inboxes.pop(agent_id, [])
        now_iso = _now().isoformat()
        for sig in sigs:
            row = await self._storage.get(_AGENT_INBOX_SIGNALS_COLLECTION, sig.id)
            if row is not None:
                row["processed_at"] = now_iso
                await self._storage.put(_AGENT_INBOX_SIGNALS_COLLECTION, sig.id, row)
        # Drain consumed everything, including any urgent signals — clear
        # the pending flag unconditionally so the next round's
        # ``mid_round_interrupt`` only trips on signals that arrive after
        # this drain.
        self._urgent_pending.pop(agent_id, None)
        return sigs

    def _format_inbox_signal(self, sig: InboxSignal) -> str:
        """Convert an InboxSignal into a one-line user-role prose snippet.

        Peer / user signals format as ``[from {sender_name}]: {body}``;
        system signals format as ``[system]: {body}``.
        """
        if sig.sender_kind in ("agent", "user"):
            prefix = f"[from {sig.sender_name}]"
        else:
            prefix = "[system]"
        return f"{prefix}: {sig.body}"

    async def _rehydrate_inboxes(self) -> None:
        """Restore unprocessed InboxSignals from storage into the in-memory cache.

        Called during ``start()`` so signals survive process restarts.

        Decision: ``FilterOp.EQ`` with ``value=None`` generates ``= NULL`` in
        SQL which never matches.  Instead we use ``FilterOp.EXISTS`` with
        ``value=False`` which generates ``IS NULL`` — the correct SQL predicate.
        """
        if self._storage is None:
            return
        rows = await self._storage.query(
            Query(
                collection=_AGENT_INBOX_SIGNALS_COLLECTION,
                filters=[Filter(field="processed_at", op=FilterOp.EXISTS, value=False)],
            )
        )
        count = 0
        for row in rows:
            sig = _signal_from_dict(row)
            self._inboxes.setdefault(sig.agent_id, []).append(sig)
            count += 1
        if count:
            logger.info("Rehydrated %d unprocessed inbox signal(s)", count)

    async def load_agent_for_caller(
        self,
        agent_id: str,
        *,
        caller_user_id: str,
        admin: bool = False,
    ) -> Agent:
        """Fetch an agent and enforce ownership.

        Raises:
            KeyError: agent does not exist.
            PermissionError: agent exists but belongs to another user.
        """
        a = await self.get_agent(agent_id)
        if a is None:
            raise KeyError(agent_id)
        if not admin and a.owner_user_id != caller_user_id:
            raise PermissionError(
                f"agent {agent_id} not accessible to user {caller_user_id}"
            )
        return a

    # ── AgentMemory (Task 7) ────────────────────────────────────────────

    async def save_memory(
        self,
        *,
        agent_id: str,
        content: str,
        kind: str = "fact",
        tags: frozenset[str] | set[str] | None = None,
        state: MemoryState = MemoryState.SHORT_TERM,
    ) -> AgentMemory:
        """Create and persist a new AgentMemory for the given agent."""
        if self._storage is None:
            raise RuntimeError("not started")
        m = AgentMemory(
            id=f"mem_{uuid.uuid4().hex[:12]}",
            agent_id=agent_id,
            content=content,
            state=state,
            kind=kind,
            tags=frozenset(tags or ()),
            score=0.0,
            created_at=_now(),
            last_used_at=None,
        )
        await self._storage.put(_AGENT_MEMORIES_COLLECTION, m.id, _memory_to_dict(m))
        return m

    async def search_memory(
        self,
        *,
        agent_id: str,
        query: str,
        limit: int = 20,
        state: MemoryState | None = None,
        kind: str | None = None,
        tags: frozenset[str] | None = None,
    ) -> list[AgentMemory]:
        """Naive substring search over an agent's memories.

        Filters by ``agent_id`` first (indexed filter), then applies a
        case-insensitive substring match on ``content``. Optional ``state``
        filter restricts to SHORT_TERM or LONG_TERM only. Optional ``kind``
        is an exact-match filter on ``kind``. Optional ``tags`` is an
        any-match filter — a memory matches if any of its tags appears in
        the requested set. Results are sorted by ``created_at`` descending
        and capped at ``limit``.
        """
        if self._storage is None:
            raise RuntimeError("not started")
        rows = await self._storage.query(
            Query(
                collection=_AGENT_MEMORIES_COLLECTION,
                filters=[Filter(field="agent_id", op=FilterOp.EQ, value=agent_id)],
            )
        )
        out: list[AgentMemory] = []
        q = query.lower()
        for r in rows:
            if state is not None and r.get("state") != state.value:
                continue
            if kind is not None and r.get("kind") != kind:
                continue
            if tags is not None:
                row_tags = frozenset(r.get("tags", []))
                if not (tags & row_tags):
                    continue
            content = str(r.get("content", "")).lower()
            if not q or q in content:
                out.append(_memory_from_dict(r))
        # Sort recency descending, then cap.
        out.sort(key=lambda m: m.created_at, reverse=True)
        return out[:limit]

    async def promote_memory(
        self,
        *,
        memory_id: str,
        score: float,
        state: MemoryState = MemoryState.LONG_TERM,
    ) -> AgentMemory:
        """Promote a memory to a new state with an updated relevance score."""
        if self._storage is None:
            raise RuntimeError("not started")
        row = await self._storage.get(_AGENT_MEMORIES_COLLECTION, memory_id)
        if row is None:
            raise KeyError(memory_id)
        row["state"] = state.value
        row["score"] = score
        await self._storage.put(_AGENT_MEMORIES_COLLECTION, memory_id, row)
        return _memory_from_dict(row)

    # ── Run orchestration (Task 8) ───────────────────────────────────

    async def run_agent_now(
        self,
        agent_id: str,
        *,
        user_message: str | None = None,
        triggered_by: str = "manual",
        trigger_context: dict[str, Any] | None = None,
    ) -> Run:
        """Trigger an immediate agent run, awaiting completion.

        Verifies the agent exists and is ENABLED, guards against concurrent
        runs, then delegates to ``_run_agent_internal``.
        """
        if self._storage is None or self._ai is None:
            raise RuntimeError("not started")
        a = await self.get_agent(agent_id)
        if a is None:
            raise ValueError(f"agent not found: {agent_id}")
        if a.status is not AgentStatus.ENABLED:
            raise ValueError(f"agent {agent_id} is {a.status.value}")
        if agent_id in self._running_agents:
            raise ValueError(f"agent {agent_id} has a run in progress")

        self._running_agents.add(agent_id)
        try:
            run = await asyncio.shield(
                self._run_agent_internal(
                    a,
                    triggered_by=triggered_by,
                    trigger_context=trigger_context or {},
                    user_message=user_message,
                )
            )
        finally:
            self._running_agents.discard(agent_id)
        return run

    async def _run_agent_internal(
        self,
        a: Agent,
        *,
        triggered_by: str,
        trigger_context: dict[str, Any],
        user_message: str | None,
    ) -> Run:
        """Inner run loop — invoked under _running_agents guard.

        Builds the system prompt, synthesizes a trigger message if needed,
        calls ``self._ai.chat`` with ai_call='agent.run', and persists the
        Run entity with cost/token totals.
        """
        run = Run(
            id=f"run_{uuid.uuid4().hex[:12]}",
            agent_id=a.id,
            triggered_by=triggered_by,
            trigger_context=dict(trigger_context),
            started_at=_now(),
            status=RunStatus.RUNNING,
            conversation_id=a.conversation_id,
            delegation_id=str(trigger_context.get("delegation_id", "")),
            ended_at=None,
            final_message_text=None,
            rounds_used=0,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            error=None,
            awaiting_user_input=False,
            pending_question=None,
            pending_actions=[],
        )
        await self._storage.put(_AGENT_RUNS_COLLECTION, run.id, _run_to_dict(run))  # type: ignore[union-attr]
        await self._publish(
            "agent.run.started",
            {"agent_id": a.id, "run_id": run.id, "triggered_by": triggered_by},
        )

        try:
            system_prompt = await self._build_system_prompt(a, triggered_by, trigger_context)
            user_msg = user_message or self._synthesize_trigger_message(triggered_by, trigger_context)

            # Drain inbox at round 0: append any pending signals onto
            # the lead user message in a clearly-marked INBOX block.
            drained = await self._drain_inbox(a.id)
            if drained:
                inbox_block = "\n".join(self._format_inbox_signal(s) for s in drained)
                user_msg = f"{user_msg}\n\nINBOX:\n{inbox_block}"

            async def _between_rounds() -> list[Message]:
                """Drain pending signals between rounds and inject them as
                user-role messages so the model sees mid-run peer DMs."""
                sigs = await self._drain_inbox(a.id)
                if not sigs:
                    return []
                return [
                    Message(
                        role=MessageRole.USER,
                        content=self._format_inbox_signal(s),
                    )
                    for s in sigs
                ]

            def _interrupt_check() -> bool:
                """Mid-round boundary check (between tool-call groups).

                Returns True iff an ``urgent`` signal has been queued for
                this agent since the last drain. ``AIService.chat``
                stubs out remaining tool calls in the current round,
                completes the round, and the existing
                ``between_rounds_callback`` flow drains the urgent
                signal into the next round.
                """
                return self._urgent_pending.get(a.id, False)

            from gilbert.interfaces.auth import UserContext
            user_ctx = UserContext.from_user_id(a.owner_user_id) if hasattr(UserContext, "from_user_id") else None

            # Stamp the active-agent context so every tool the AI calls
            # during this run sees ``_agent_id`` injected — see
            # ``execute_tool`` above. Token reset in the ``finally`` is
            # critical: contextvars persist within a Task, so leaving the
            # token set would corrupt subsequent runs in the same task
            # (e.g., when scheduler ticks reuse the same loop).
            agent_token = _active_agent_id.set(a.id)
            chain = list(trigger_context.get("chain") or [])
            chain_token = _active_delegation_chain.set(chain)
            try:
                result = await self._ai.chat(  # type: ignore[union-attr]
                    user_message=user_msg,
                    conversation_id=a.conversation_id or None,
                    user_ctx=user_ctx,
                    system_prompt=system_prompt,
                    ai_call=_AI_CALL_NAME,
                    ai_profile=a.profile_id,
                    between_rounds_callback=_between_rounds,
                    mid_round_interrupt=_interrupt_check,
                )
            finally:
                _active_agent_id.reset(agent_token)
                _active_delegation_chain.reset(chain_token)

            # ChatTurnResult uses `response_text`; map to run.final_message_text.
            run.final_message_text = result.response_text
            run.conversation_id = result.conversation_id
            tu = result.turn_usage or {}
            run.rounds_used = int(tu.get("rounds", 0))
            run.tokens_in = int(tu.get("input_tokens", 0))
            run.tokens_out = int(tu.get("output_tokens", 0))
            run.cost_usd = float(tu.get("cost_usd", 0.0))
            run.status = RunStatus.COMPLETED
            run.ended_at = _now()

            # Capture conv_id back on the agent row if just created, and
            # stamp ``metadata.kind = "agent"`` (+ ``agent_id``) on the
            # conversation row so the SPA's chat sidebar can group agent
            # conversations into a separate section beneath regular
            # chats. AIService creates the row without that hint; we
            # patch it once per agent.
            if a.conversation_id == "" and run.conversation_id:
                fresh = await self._storage.get(_AGENTS_COLLECTION, a.id)  # type: ignore[union-attr]
                if fresh is not None:
                    fresh["conversation_id"] = run.conversation_id
                    await self._storage.put(_AGENTS_COLLECTION, a.id, fresh)  # type: ignore[union-attr]
                conv_row = await self._storage.get(_AI_CONVERSATIONS_COLLECTION, run.conversation_id)  # type: ignore[union-attr]
                if conv_row is not None:
                    metadata = dict(conv_row.get("metadata") or {})
                    metadata["kind"] = "agent"
                    metadata["agent_id"] = a.id
                    conv_row["metadata"] = metadata
                    # Title the conversation after the agent so it shows
                    # something useful in the sidebar (the user-facing
                    # display_name when set, slug otherwise).
                    if not conv_row.get("title"):
                        conv_row["title"] = a.display_name or a.name
                    await self._storage.put(_AI_CONVERSATIONS_COLLECTION, run.conversation_id, conv_row)  # type: ignore[union-attr]

            await self._accumulate_cost(a.id, run.cost_usd)

        except Exception as exc:
            logger.exception("agent run failed: %s", a.id)
            run.status = RunStatus.FAILED
            run.error = repr(exc)
            run.ended_at = _now()

        await self._storage.put(_AGENT_RUNS_COLLECTION, run.id, _run_to_dict(run))  # type: ignore[union-attr]
        await self._publish(
            "agent.run.completed",
            {
                "agent_id": a.id,
                "run_id": run.id,
                "status": run.status.value,
                "cost_usd": run.cost_usd,
            },
        )

        # Resolve any awaiting delegation Future. A run that completes
        # successfully delivers its final assistant message to the
        # delegator; a failed/timed-out run propagates an exception so
        # the caller's _exec_agent_delegate surfaces the error.
        delegation_id = str(trigger_context.get("delegation_id", ""))
        if delegation_id:
            fut = self._pending_delegations.get(delegation_id)
            if fut is not None and not fut.done():
                if run.status is RunStatus.COMPLETED:
                    fut.set_result(run.final_message_text or "")
                else:
                    fut.set_exception(
                        RuntimeError(run.status.value)
                    )
        return run

    def _synthesize_trigger_message(self, triggered_by: str, ctx: dict[str, Any]) -> str:
        """Return a synthetic user message describing why the agent was triggered."""
        if triggered_by == "manual":
            return "Run manually triggered. Take whatever action is appropriate."
        if triggered_by == "heartbeat":
            return "Heartbeat — periodic self-check."
        if triggered_by == "time":
            return "Scheduled trigger fired."
        if triggered_by == "event":
            etype = ctx.get("event_type", "?")
            return f"Event '{etype}' fired. See trigger context for the payload."
        if triggered_by == "inbox":
            return (
                "You have new inbox messages. Read them below and "
                "respond as appropriate."
            )
        if triggered_by == "delegation":
            sender = ctx.get("sender_id", "?")
            return (
                "You are handling a delegation request. Read the instruction "
                "below and end your turn with a clear conclusion — your final "
                f"assistant message becomes the reply to {sender}."
            )
        return f"Trigger: {triggered_by}."

    def _compute_allowed_tool_names(self, a: Agent, *, available: set[str]) -> set[str]:
        """Compute the tool name set for an agent's run.

        Three modes, mutually exclusive:

        - ``tools_include=[…]`` → strict allowlist. Returns
          ``(core ∪ include) ∩ available``. If the owner loses access to
          a listed tool the agent loses it too — propagation by
          intersection.
        - ``tools_exclude=[…]`` → denylist. Returns
          ``core ∪ (available - exclude)``. Core tools are kept even if
          they appear in ``exclude``.
        - both ``None`` → returns the full ``available`` set.

        ``available`` is the OWNER's runtime tool discovery result. Core
        tools come from the agent service's own ``ToolProvider`` (so they
        are implicitly part of ``available`` whenever the agent is
        running).
        """
        core = set(_CORE_AGENT_TOOLS)
        if a.tools_include is not None:
            keep = (core | set(a.tools_include)) & set(available)
            return keep
        if a.tools_exclude is not None:
            keep = core | (set(available) - set(a.tools_exclude))
            return keep
        return set(available)

    async def _build_system_prompt(
        self,
        a: Agent,
        triggered_by: str,
        trigger_context: dict[str, Any],
    ) -> str:
        """Assemble the full system prompt from persona, rules, and context blocks."""
        parts = [a.persona, a.system_prompt, a.procedural_rules]

        if triggered_by == "heartbeat":
            due = await self._due_commitments(a.id)
            checklist = a.heartbeat_checklist or "(no checklist configured)"
            due_block = (
                "\n".join(
                    f"- [{c.id}] {c.content} (due {c.due_at.isoformat()})"
                    for c in due
                )
                or "(none)"
            )
            parts.append(
                f"HEARTBEAT — periodic self-check. Read your checklist below "
                f"and decide if anything needs action right now. If nothing is "
                f"pressing, end your turn briefly.\n\n"
                f"CHECKLIST:\n{checklist}\n\n"
                f"DUE COMMITMENTS:\n{due_block}"
            )
        elif triggered_by == "delegation":
            parts.append(
                "You are handling a delegation from a peer. Read the request "
                "(in the inbox below) and respond. Your final assistant "
                "message is returned as the reply — end your turn cleanly "
                "with a complete answer, no follow-up actions."
            )

        # Long-term memory block (top-K by recency).
        long_term = await self.search_memory(
            agent_id=a.id, query="", limit=20, state=MemoryState.LONG_TERM,
        )
        if long_term:
            mem_block = "\n".join(f"- {m.content}" for m in long_term)
            parts.append(f"LONG-TERM MEMORY:\n{mem_block}")

        # Active goal assignments — agents need to see what they're
        # currently committed to, with a short snippet of recent
        # war-room chatter so they can pick up context without
        # specifically querying.
        assignments = await self.list_assignments(agent_id=a.id, active_only=True)
        if assignments:
            blocks: list[str] = []
            for asgn in assignments:
                goal = await self.get_goal(asgn.goal_id)
                if goal is None:
                    continue
                recent = await self._recent_war_room_posts(asgn.goal_id, limit=10)
                recent_block = (
                    "\n".join(
                        f"  {p['author_name']}: {p['body']}" for p in recent
                    )
                    or "  (no posts yet)"
                )
                blocks.append(
                    f"- Goal '{goal.name}' (id={goal.id}) "
                    f"[role={asgn.role.value}, status={goal.status.value}]\n"
                    f"{recent_block}"
                )
            if blocks:
                parts.append("ACTIVE ASSIGNMENTS:\n" + "\n\n".join(blocks))

        return "\n\n---\n\n".join(p for p in parts if p)

    async def create_commitment(
        self, *, agent_id: str, content: str, due_at: datetime,
    ) -> Commitment:
        """Persist a new commitment and return it."""
        if self._storage is None:
            raise RuntimeError("not started")
        c = Commitment(
            id=f"com_{uuid.uuid4().hex[:12]}",
            agent_id=agent_id,
            content=content,
            due_at=due_at,
            created_at=_now(),
            completed_at=None,
            completion_note="",
        )
        await self._storage.put(_AGENT_COMMITMENTS_COLLECTION, c.id, _commitment_to_dict(c))
        return c

    async def complete_commitment(
        self, commitment_id: str, *, note: str = "",
    ) -> Commitment:
        """Mark a commitment complete with an optional note."""
        if self._storage is None:
            raise RuntimeError("not started")
        row = await self._storage.get(_AGENT_COMMITMENTS_COLLECTION, commitment_id)
        if row is None:
            raise KeyError(commitment_id)
        row["completed_at"] = _now().isoformat()
        row["completion_note"] = note
        await self._storage.put(_AGENT_COMMITMENTS_COLLECTION, commitment_id, row)
        return _commitment_from_dict(row)

    async def list_commitments(
        self, *, agent_id: str, include_completed: bool = False,
    ) -> list[Commitment]:
        """Return commitments for *agent_id*, sorted by due_at ascending.

        By default only unfinished commitments are returned; pass
        ``include_completed=True`` to include completed ones.
        """
        if self._storage is None:
            raise RuntimeError("not started")
        rows = await self._storage.query(
            Query(
                collection=_AGENT_COMMITMENTS_COLLECTION,
                filters=[Filter(field="agent_id", op=FilterOp.EQ, value=agent_id)],
            )
        )
        out: list[Commitment] = []
        for r in rows:
            if not include_completed and r.get("completed_at"):
                continue
            out.append(_commitment_from_dict(r))
        out.sort(key=lambda c: c.due_at)
        return out

    async def _due_commitments(self, agent_id: str) -> list[Commitment]:
        """Return commitments for *agent_id* that are due now and not completed."""
        if self._storage is None:
            return []
        rows = await self._storage.query(
            Query(
                collection=_AGENT_COMMITMENTS_COLLECTION,
                filters=[Filter(field="agent_id", op=FilterOp.EQ, value=agent_id)],
            )
        )
        out: list[Commitment] = []
        for r in rows:
            if r.get("completed_at"):
                continue
            due = datetime.fromisoformat(r["due_at"])
            if due <= _now():
                out.append(_commitment_from_dict(r))
        return out

    async def _accumulate_cost(self, agent_id: str, delta: float) -> None:
        """Add *delta* to the agent's lifetime_cost_usd; auto-DISABLE on cap breach."""
        if delta <= 0 or self._storage is None:
            return
        row = await self._storage.get(_AGENTS_COLLECTION, agent_id)
        if row is None:
            return
        new_total = float(row.get("lifetime_cost_usd", 0.0)) + delta
        row["lifetime_cost_usd"] = new_total
        cap = row.get("cost_cap_usd")
        if cap is not None and new_total >= float(cap):
            row["status"] = AgentStatus.DISABLED.value
            logger.warning(
                "Agent %s auto-DISABLED at cost cap %s (cumulative %s)",
                agent_id, cap, new_total,
            )
        await self._storage.put(_AGENTS_COLLECTION, agent_id, row)

    async def list_runs(self, *, agent_id: str, limit: int = 50) -> list[Run]:
        """Return up to *limit* runs for *agent_id*, sorted most-recent first."""
        if self._storage is None:
            raise RuntimeError("not started")
        rows = await self._storage.query(
            Query(
                collection=_AGENT_RUNS_COLLECTION,
                filters=[Filter(field="agent_id", op=FilterOp.EQ, value=agent_id)],
            )
        )
        rows.sort(key=lambda r: r.get("started_at", ""), reverse=True)
        return [_run_from_dict(r) for r in rows[:limit]]

    # ── Goals (Phase 4) ──────────────────────────────────────────────

    async def create_goal(
        self,
        *,
        owner_user_id: str,
        name: str,
        description: str = "",
        cost_cap_usd: float | None = None,
        assign_to: list[tuple[str, AssignmentRole]] | None = None,
        assigned_by: str = "user:?",
    ) -> Goal:
        """Create a goal + war-room conversation + initial assignments.

        ``assign_to`` is a list of ``(agent_name, role)`` tuples. Agent
        names are resolved owner-scoped: only same-owner agents may be
        assigned. If ``assign_to`` is non-empty and none of the entries
        is DRIVER, the first entry is promoted to DRIVER.
        """
        if self._storage is None:
            raise RuntimeError("not started")

        goal_id = f"goal_{uuid.uuid4().hex[:12]}"
        now = _now()

        # War-room conversation row — written directly to the
        # ai_conversations collection so AIService can pick it up via
        # its existing chat history machinery without any additional
        # plumbing. See _AI_CONVERSATIONS_COLLECTION above for the
        # rationale on the duplicate constant.
        conv_id = uuid.uuid4().hex
        conv_row = {
            "_id": conv_id,
            "title": name,
            "user_id": owner_user_id,
            "messages": [],
            "metadata": {"goal_id": goal_id, "kind": "war_room"},
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        await self._storage.put(_AI_CONVERSATIONS_COLLECTION, conv_id, conv_row)

        g = Goal(
            id=goal_id,
            owner_user_id=owner_user_id,
            name=name,
            description=description,
            status=GoalStatus.NEW,
            war_room_conversation_id=conv_id,
            cost_cap_usd=cost_cap_usd,
            lifetime_cost_usd=0.0,
            created_at=now,
            updated_at=now,
            completed_at=None,
        )
        await self._storage.put(_GOALS_COLLECTION, g.id, _goal_to_dict(g))
        await self._publish(
            "goal.created",
            {"goal_id": g.id, "owner_user_id": owner_user_id, "name": name},
        )

        # Materialize initial assignments. If a DRIVER was specified,
        # honor the user's roles verbatim; else, promote the first
        # entry to DRIVER.
        if assign_to:
            roles = [r for (_, r) in assign_to]
            promote_first = AssignmentRole.DRIVER not in roles
            for idx, (agent_name, role) in enumerate(assign_to):
                # Look up the agent owner-scoped — same logic as
                # _load_peer_by_name but without a caller agent.
                rows = await self._storage.query(
                    Query(
                        collection=_AGENTS_COLLECTION,
                        filters=[
                            Filter(field="owner_user_id", op=FilterOp.EQ, value=owner_user_id),
                            Filter(field="name", op=FilterOp.EQ, value=agent_name),
                        ],
                    )
                )
                if not rows:
                    raise ValueError(f"no agent named {agent_name!r} for owner {owner_user_id}")
                agent_row = rows[0]
                final_role = (
                    AssignmentRole.DRIVER
                    if (idx == 0 and promote_first) else role
                )
                await self.assign_agent_to_goal(
                    goal_id=g.id,
                    agent_id=agent_row["_id"],
                    role=final_role,
                    assigned_by=assigned_by,
                )

        return g

    async def get_goal(self, goal_id: str) -> Goal | None:
        """Fetch a Goal by ID. Returns None if not found."""
        if self._storage is None:
            raise RuntimeError("not started")
        row = await self._storage.get(_GOALS_COLLECTION, goal_id)
        if row is None:
            return None
        return _goal_from_dict(row)

    async def list_goals(
        self,
        *,
        owner_user_id: str | None = None,
    ) -> list[Goal]:
        """List goals, optionally filtered by owner."""
        if self._storage is None:
            raise RuntimeError("not started")
        filters = (
            []
            if owner_user_id is None
            else [Filter(field="owner_user_id", op=FilterOp.EQ, value=owner_user_id)]
        )
        rows = await self._storage.query(
            Query(collection=_GOALS_COLLECTION, filters=filters)
        )
        return [_goal_from_dict(r) for r in rows]

    async def update_goal_status(
        self,
        goal_id: str,
        status: GoalStatus,
    ) -> Goal:
        """Set the goal's status, stamp completed_at on COMPLETE, publish event."""
        if self._storage is None:
            raise RuntimeError("not started")
        row = await self._storage.get(_GOALS_COLLECTION, goal_id)
        if row is None:
            raise KeyError(goal_id)
        row["status"] = status.value
        now = _now()
        row["updated_at"] = now.isoformat()
        if status is GoalStatus.COMPLETE and not row.get("completed_at"):
            row["completed_at"] = now.isoformat()
        await self._storage.put(_GOALS_COLLECTION, goal_id, row)
        g = _goal_from_dict(row)
        await self._publish(
            "goal.status.changed",
            {"goal_id": g.id, "status": g.status.value},
        )
        await self._publish("goal.updated", {"goal_id": g.id})
        return g

    async def list_assignments(
        self,
        *,
        goal_id: str | None = None,
        agent_id: str | None = None,
        active_only: bool = True,
    ) -> list[GoalAssignment]:
        """List assignments, optionally filtered by goal_id, agent_id, or
        active status. Active means ``removed_at IS NULL``."""
        if self._storage is None:
            raise RuntimeError("not started")
        filters = []
        if goal_id is not None:
            filters.append(Filter(field="goal_id", op=FilterOp.EQ, value=goal_id))
        if agent_id is not None:
            filters.append(Filter(field="agent_id", op=FilterOp.EQ, value=agent_id))
        rows = await self._storage.query(
            Query(collection=_GOAL_ASSIGNMENTS_COLLECTION, filters=filters)
        )
        out: list[GoalAssignment] = []
        for r in rows:
            if active_only and r.get("removed_at"):
                continue
            out.append(_goal_assignment_from_dict(r))
        out.sort(key=lambda a: a.assigned_at)
        return out

    async def assign_agent_to_goal(
        self,
        *,
        goal_id: str,
        agent_id: str,
        role: AssignmentRole,
        assigned_by: str,
        handoff_note: str = "",
    ) -> GoalAssignment:
        """Assign an agent to a goal at the given role.

        Idempotent: if the agent already has an active assignment on the
        goal at the same role, returns the existing row. If active at a
        different role, the existing row's role is updated in place.
        """
        if self._storage is None:
            raise RuntimeError("not started")

        existing = await self._storage.query(
            Query(
                collection=_GOAL_ASSIGNMENTS_COLLECTION,
                filters=[
                    Filter(field="goal_id", op=FilterOp.EQ, value=goal_id),
                    Filter(field="agent_id", op=FilterOp.EQ, value=agent_id),
                ],
            )
        )
        active_row = next((r for r in existing if not r.get("removed_at")), None)
        if active_row is not None:
            if active_row.get("role") == role.value:
                return _goal_assignment_from_dict(active_row)
            # Role change: update in place rather than insert a new row.
            active_row["role"] = role.value
            if handoff_note:
                active_row["handoff_note"] = handoff_note
            await self._storage.put(
                _GOAL_ASSIGNMENTS_COLLECTION, active_row["_id"], active_row,
            )
            await self._publish(
                "goal.assignment.changed",
                {"goal_id": goal_id, "agent_id": agent_id, "role": role.value},
            )
            return _goal_assignment_from_dict(active_row)

        ga = GoalAssignment(
            id=f"ga_{uuid.uuid4().hex[:12]}",
            goal_id=goal_id,
            agent_id=agent_id,
            role=role,
            assigned_at=_now(),
            assigned_by=assigned_by,
            removed_at=None,
            handoff_note=handoff_note,
        )
        await self._storage.put(
            _GOAL_ASSIGNMENTS_COLLECTION, ga.id, _goal_assignment_to_dict(ga),
        )
        # Signal the assigned agent so it's aware of the new assignment
        # at the next safe moment. Best-effort: failure to signal is
        # logged but doesn't roll back the assignment.
        try:
            goal = await self.get_goal(goal_id)
            goal_name = goal.name if goal else goal_id
            await self._signal_agent(
                agent_id=agent_id,
                signal_kind="goal_assigned",
                body=f"You have been assigned to goal '{goal_name}' as {role.value}.",
                sender_kind="system",
                sender_id="system",
                sender_name="system",
                metadata={"goal_id": goal_id, "role": role.value},
            )
        except Exception:
            logger.exception("failed to signal agent %s for goal %s", agent_id, goal_id)
        await self._publish(
            "goal.assignment.changed",
            {"goal_id": goal_id, "agent_id": agent_id, "role": role.value},
        )
        return ga

    async def unassign_agent_from_goal(
        self,
        *,
        goal_id: str,
        agent_id: str,
    ) -> GoalAssignment:
        """Mark the active assignment as removed (preserves the row)."""
        if self._storage is None:
            raise RuntimeError("not started")
        rows = await self._storage.query(
            Query(
                collection=_GOAL_ASSIGNMENTS_COLLECTION,
                filters=[
                    Filter(field="goal_id", op=FilterOp.EQ, value=goal_id),
                    Filter(field="agent_id", op=FilterOp.EQ, value=agent_id),
                ],
            )
        )
        active = next((r for r in rows if not r.get("removed_at")), None)
        if active is None:
            raise KeyError(f"no active assignment for agent {agent_id} on goal {goal_id}")
        active["removed_at"] = _now().isoformat()
        await self._storage.put(
            _GOAL_ASSIGNMENTS_COLLECTION, active["_id"], active,
        )
        await self._publish(
            "goal.assignment.changed",
            {"goal_id": goal_id, "agent_id": agent_id, "removed": True},
        )
        return _goal_assignment_from_dict(active)

    async def handoff_goal(
        self,
        *,
        goal_id: str,
        from_agent_id: str,
        to_agent_id: str,
        new_role_for_from: AssignmentRole = AssignmentRole.COLLABORATOR,
        note: str = "",
    ) -> tuple[GoalAssignment, GoalAssignment]:
        """Hand off DRIVER from one agent to another.

        Asserts ``from_agent_id`` is currently DRIVER on ``goal_id``.
        Demotes the from-agent to ``new_role_for_from`` (defaults
        COLLABORATOR), promotes the to-agent to DRIVER. Both rows get
        the ``handoff_note`` stamped. Returns ``(from_assignment,
        to_assignment)``.
        """
        if self._storage is None:
            raise RuntimeError("not started")

        # Find the current DRIVER row for from_agent.
        from_rows = await self._storage.query(
            Query(
                collection=_GOAL_ASSIGNMENTS_COLLECTION,
                filters=[
                    Filter(field="goal_id", op=FilterOp.EQ, value=goal_id),
                    Filter(field="agent_id", op=FilterOp.EQ, value=from_agent_id),
                ],
            )
        )
        from_active = next((r for r in from_rows if not r.get("removed_at")), None)
        if from_active is None:
            raise KeyError(f"agent {from_agent_id} is not assigned to goal {goal_id}")
        if from_active.get("role") != AssignmentRole.DRIVER.value:
            raise ValueError(
                f"agent {from_agent_id} is not DRIVER on goal {goal_id}"
            )

        # Demote from-agent.
        from_active["role"] = new_role_for_from.value
        from_active["handoff_note"] = note
        await self._storage.put(
            _GOAL_ASSIGNMENTS_COLLECTION, from_active["_id"], from_active,
        )

        # Promote (or insert) to-agent as DRIVER.
        to_rows = await self._storage.query(
            Query(
                collection=_GOAL_ASSIGNMENTS_COLLECTION,
                filters=[
                    Filter(field="goal_id", op=FilterOp.EQ, value=goal_id),
                    Filter(field="agent_id", op=FilterOp.EQ, value=to_agent_id),
                ],
            )
        )
        to_active = next((r for r in to_rows if not r.get("removed_at")), None)
        if to_active is None:
            ga = GoalAssignment(
                id=f"ga_{uuid.uuid4().hex[:12]}",
                goal_id=goal_id,
                agent_id=to_agent_id,
                role=AssignmentRole.DRIVER,
                assigned_at=_now(),
                assigned_by=f"agent:{from_agent_id}",
                removed_at=None,
                handoff_note=note,
            )
            await self._storage.put(
                _GOAL_ASSIGNMENTS_COLLECTION, ga.id, _goal_assignment_to_dict(ga),
            )
            to_assignment_row = _goal_assignment_to_dict(ga)
        else:
            to_active["role"] = AssignmentRole.DRIVER.value
            to_active["handoff_note"] = note
            await self._storage.put(
                _GOAL_ASSIGNMENTS_COLLECTION, to_active["_id"], to_active,
            )
            to_assignment_row = to_active

        await self._publish(
            "goal.assignment.changed",
            {
                "goal_id": goal_id,
                "from_agent_id": from_agent_id,
                "to_agent_id": to_agent_id,
                "kind": "handoff",
            },
        )
        return (
            _goal_assignment_from_dict(from_active),
            _goal_assignment_from_dict(to_assignment_row),
        )

    # ── Deliverables + Dependencies (Phase 5) ────────────────────────

    async def create_deliverable(
        self,
        *,
        goal_id: str,
        name: str,
        kind: str,
        produced_by_agent_id: str,
        content_ref: str = "",
        state: DeliverableState | None = None,
    ) -> Deliverable:
        """Persist a new deliverable.

        Defaults to ``DeliverableState.DRAFT``. ``state=READY`` is only
        used by the ``supersede_deliverable(finalize=True)`` path; in
        that case the propagation hook is invoked here.
        """
        if self._storage is None:
            raise RuntimeError("not started")
        eff_state = state if state is not None else DeliverableState.DRAFT
        d = Deliverable(
            id=f"dlv_{uuid.uuid4().hex[:12]}",
            goal_id=goal_id,
            name=name,
            kind=kind,
            state=eff_state,
            produced_by_agent_id=produced_by_agent_id,
            content_ref=content_ref,
            created_at=_now(),
            finalized_at=_now() if eff_state is DeliverableState.READY else None,
        )
        await self._storage.put(
            _DELIVERABLES_COLLECTION, d.id, _deliverable_to_dict(d),
        )
        await self._publish(
            "goal.deliverable.created",
            {"deliverable_id": d.id, "goal_id": d.goal_id, "name": d.name},
        )
        if eff_state is DeliverableState.READY:
            await self._on_deliverable_finalized(d)
        return d

    async def get_deliverable(
        self, deliverable_id: str,
    ) -> Deliverable | None:
        if self._storage is None:
            raise RuntimeError("not started")
        row = await self._storage.get(_DELIVERABLES_COLLECTION, deliverable_id)
        if row is None:
            return None
        return _deliverable_from_dict(row)

    async def list_deliverables(
        self,
        *,
        goal_id: str | None = None,
        state: DeliverableState | None = None,
    ) -> list[Deliverable]:
        if self._storage is None:
            raise RuntimeError("not started")
        filters = []
        if goal_id is not None:
            filters.append(Filter(field="goal_id", op=FilterOp.EQ, value=goal_id))
        if state is not None:
            filters.append(Filter(field="state", op=FilterOp.EQ, value=state.value))
        rows = await self._storage.query(
            Query(collection=_DELIVERABLES_COLLECTION, filters=filters)
        )
        return [_deliverable_from_dict(r) for r in rows]

    async def find_deliverable_by_content_ref(
        self, content_ref: str,
    ) -> Deliverable | None:
        """Locate a deliverable that points at the given ``content_ref``.

        Used by the cross-goal workspace resolver — given a workspace
        file_id, find the deliverable that produced it. Returns the
        most recently created match (if any).
        """
        if self._storage is None or not content_ref:
            return None
        rows = await self._storage.query(
            Query(
                collection=_DELIVERABLES_COLLECTION,
                filters=[
                    Filter(field="content_ref", op=FilterOp.EQ, value=content_ref),
                ],
            )
        )
        if not rows:
            return None
        rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return _deliverable_from_dict(rows[0])

    async def finalize_deliverable(
        self, deliverable_id: str,
    ) -> Deliverable:
        """Flip a DRAFT deliverable to READY.

        Refuses OBSOLETE; supersedes any prior READY deliverable on the
        same goal sharing the same ``name`` (single-READY invariant).
        Sequence: mark prior READY rows OBSOLETE first, then flip the
        target to READY last, so a partial-failure mid-way leaves the
        store coherent (target still DRAFT, prior already OBSOLETE).
        """
        if self._storage is None:
            raise RuntimeError("not started")
        row = await self._storage.get(_DELIVERABLES_COLLECTION, deliverable_id)
        if row is None:
            raise KeyError(deliverable_id)
        d = _deliverable_from_dict(row)
        if d.state is DeliverableState.OBSOLETE:
            raise ValueError(
                f"deliverable {deliverable_id} is OBSOLETE; cannot finalize"
            )
        if d.state is DeliverableState.READY:
            return d  # idempotent

        # Single-READY invariant — supersede prior READY rows on the
        # same (goal, name) BEFORE flipping the target. Excludes the
        # current target so we don't OBSOLETE the row we're about to
        # mark READY.
        prior = await self._storage.query(
            Query(
                collection=_DELIVERABLES_COLLECTION,
                filters=[
                    Filter(field="goal_id", op=FilterOp.EQ, value=d.goal_id),
                    Filter(field="name", op=FilterOp.EQ, value=d.name),
                    Filter(field="state", op=FilterOp.EQ,
                           value=DeliverableState.READY.value),
                ],
            )
        )
        for prior_row in prior:
            if prior_row["_id"] == deliverable_id:
                continue
            prior_row["state"] = DeliverableState.OBSOLETE.value
            await self._storage.put(
                _DELIVERABLES_COLLECTION, prior_row["_id"], prior_row,
            )
            await self._publish(
                "goal.deliverable.obsoleted",
                {"deliverable_id": prior_row["_id"], "goal_id": d.goal_id},
            )

        # Flip target LAST so a mid-failure leaves the store coherent.
        row["state"] = DeliverableState.READY.value
        row["finalized_at"] = _now().isoformat()
        await self._storage.put(_DELIVERABLES_COLLECTION, deliverable_id, row)
        finalized = _deliverable_from_dict(row)
        await self._on_deliverable_finalized(finalized)
        return finalized

    async def supersede_deliverable(
        self,
        deliverable_id: str,
        *,
        new_content_ref: str,
        finalize: bool = False,
    ) -> tuple[Deliverable, Deliverable]:
        """Mark the existing deliverable OBSOLETE; create a new one
        (DRAFT, or READY if ``finalize=True``) with the same name/kind/
        goal as the predecessor.

        Returns ``(obsoleted, new)``.
        """
        if self._storage is None:
            raise RuntimeError("not started")
        row = await self._storage.get(_DELIVERABLES_COLLECTION, deliverable_id)
        if row is None:
            raise KeyError(deliverable_id)
        existing = _deliverable_from_dict(row)
        if existing.state is DeliverableState.OBSOLETE:
            raise ValueError(
                f"deliverable {deliverable_id} is already OBSOLETE"
            )
        # Mark OBSOLETE first.
        row["state"] = DeliverableState.OBSOLETE.value
        await self._storage.put(_DELIVERABLES_COLLECTION, deliverable_id, row)
        await self._publish(
            "goal.deliverable.obsoleted",
            {"deliverable_id": deliverable_id, "goal_id": existing.goal_id},
        )

        new_state = (
            DeliverableState.READY if finalize else DeliverableState.DRAFT
        )
        new_d = await self.create_deliverable(
            goal_id=existing.goal_id,
            name=existing.name,
            kind=existing.kind,
            produced_by_agent_id=existing.produced_by_agent_id,
            content_ref=new_content_ref,
            state=new_state,
        )
        obsoleted = _deliverable_from_dict(
            await self._storage.get(_DELIVERABLES_COLLECTION, deliverable_id)
            or row
        )
        return obsoleted, new_d

    async def add_goal_dependency(
        self,
        *,
        dependent_goal_id: str,
        source_goal_id: str,
        required_deliverable_name: str,
    ) -> GoalDependency:
        """Register a dependency edge.

        Idempotent on (dependent, source, name): if a row exists, return
        it. If a matching READY deliverable already exists on the source
        goal, the new row is created with ``satisfied_at=now()`` and
        the wake-up signal fires immediately (scoped to this dep).
        """
        if self._storage is None:
            raise RuntimeError("not started")
        existing = await self._storage.query(
            Query(
                collection=_DEPENDENCIES_COLLECTION,
                filters=[
                    Filter(
                        field="dependent_goal_id",
                        op=FilterOp.EQ, value=dependent_goal_id,
                    ),
                    Filter(
                        field="source_goal_id",
                        op=FilterOp.EQ, value=source_goal_id,
                    ),
                    Filter(
                        field="required_deliverable_name",
                        op=FilterOp.EQ, value=required_deliverable_name,
                    ),
                ],
            )
        )
        if existing:
            return _dependency_from_dict(existing[0])

        # Look for a matching READY deliverable on the source goal —
        # if one exists, this dependency is created pre-satisfied and
        # we fire the wake-up signal immediately.
        ready = await self._storage.query(
            Query(
                collection=_DELIVERABLES_COLLECTION,
                filters=[
                    Filter(field="goal_id", op=FilterOp.EQ, value=source_goal_id),
                    Filter(field="name", op=FilterOp.EQ,
                           value=required_deliverable_name),
                    Filter(field="state", op=FilterOp.EQ,
                           value=DeliverableState.READY.value),
                ],
            )
        )
        now = _now()
        dep = GoalDependency(
            id=f"dep_{uuid.uuid4().hex[:12]}",
            dependent_goal_id=dependent_goal_id,
            source_goal_id=source_goal_id,
            required_deliverable_name=required_deliverable_name,
            satisfied_at=now if ready else None,
        )
        await self._storage.put(
            _DEPENDENCIES_COLLECTION, dep.id, _dependency_to_dict(dep),
        )
        await self._publish(
            "goal.dependency.added",
            {
                "dependency_id": dep.id,
                "dependent_goal_id": dependent_goal_id,
                "source_goal_id": source_goal_id,
                "required_deliverable_name": required_deliverable_name,
            },
        )
        if ready:
            # Scoped propagation: signal only the dependent assignees on
            # this fresh dep; the deliverable itself isn't being
            # finalized again.
            d = _deliverable_from_dict(ready[0])
            await self._signal_dependent_assignees(
                deliverable=d, dep=dep,
            )
        return dep

    async def remove_goal_dependency(self, dependency_id: str) -> None:
        if self._storage is None:
            raise RuntimeError("not started")
        row = await self._storage.get(_DEPENDENCIES_COLLECTION, dependency_id)
        if row is None:
            raise KeyError(dependency_id)
        await self._storage.delete(_DEPENDENCIES_COLLECTION, dependency_id)
        await self._publish(
            "goal.dependency.removed",
            {"dependency_id": dependency_id},
        )

    async def list_goal_dependencies(
        self,
        *,
        dependent_goal_id: str | None = None,
        source_goal_id: str | None = None,
        satisfied: bool | None = None,
    ) -> list[GoalDependency]:
        if self._storage is None:
            raise RuntimeError("not started")
        filters = []
        if dependent_goal_id is not None:
            filters.append(Filter(
                field="dependent_goal_id",
                op=FilterOp.EQ, value=dependent_goal_id,
            ))
        if source_goal_id is not None:
            filters.append(Filter(
                field="source_goal_id",
                op=FilterOp.EQ, value=source_goal_id,
            ))
        rows = await self._storage.query(
            Query(collection=_DEPENDENCIES_COLLECTION, filters=filters)
        )
        out: list[GoalDependency] = []
        for r in rows:
            row_satisfied = bool(r.get("satisfied_at"))
            if satisfied is True and not row_satisfied:
                continue
            if satisfied is False and row_satisfied:
                continue
            out.append(_dependency_from_dict(r))
        return out

    async def _signal_dependent_assignees(
        self,
        *,
        deliverable: Deliverable,
        dep: GoalDependency,
    ) -> None:
        """Signal every non-REVIEWER active assignee on the dependent
        goal that the deliverable is ready. Best-effort (signal failures
        log but don't roll back)."""
        assignments = await self.list_assignments(
            goal_id=dep.dependent_goal_id, active_only=True,
        )
        for asgn in assignments:
            if asgn.role is AssignmentRole.REVIEWER:
                continue
            try:
                await self._signal_agent(
                    agent_id=asgn.agent_id,
                    signal_kind="deliverable_ready",
                    body=(
                        f"Dependency satisfied: '{deliverable.name}' "
                        f"from goal {dep.source_goal_id}"
                    ),
                    sender_kind="system",
                    sender_id="",
                    sender_name="system",
                    metadata={
                        "deliverable_id": deliverable.id,
                        "source_goal_id": deliverable.goal_id,
                        "dependent_goal_id": dep.dependent_goal_id,
                    },
                )
            except Exception:
                logger.exception(
                    "failed to signal agent %s for deliverable %s",
                    asgn.agent_id, deliverable.id,
                )

    async def _on_deliverable_finalized(self, d: Deliverable) -> None:
        """Propagate a READY deliverable to its dependents.

        Find every unsatisfied ``GoalDependency`` whose
        ``source_goal_id == d.goal_id`` and
        ``required_deliverable_name == d.name``; mark each
        ``satisfied_at`` and signal the dependent goal's non-REVIEWER
        assignees. Publish ``goal.deliverable.finalized``.
        """
        if self._storage is None:
            return
        deps = await self.list_goal_dependencies(
            source_goal_id=d.goal_id, satisfied=False,
        )
        for dep in deps:
            if dep.required_deliverable_name != d.name:
                continue
            row = await self._storage.get(_DEPENDENCIES_COLLECTION, dep.id)
            if row is None:
                continue
            row["satisfied_at"] = _now().isoformat()
            await self._storage.put(_DEPENDENCIES_COLLECTION, dep.id, row)
            satisfied_dep = _dependency_from_dict(row)
            await self._signal_dependent_assignees(
                deliverable=d, dep=satisfied_dep,
            )
        await self._publish(
            "goal.deliverable.finalized",
            {
                "deliverable_id": d.id,
                "goal_id": d.goal_id,
                "name": d.name,
            },
        )

    async def _recent_war_room_posts(
        self, goal_id: str, limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Return the last *limit* user-role posts from the goal's
        war-room conv, oldest-to-newest. Each entry has
        ``{author_name, body, ts}``."""
        if self._storage is None:
            return []
        goal = await self.get_goal(goal_id)
        if goal is None or not goal.war_room_conversation_id:
            return []
        conv_row = await self._storage.get(
            _AI_CONVERSATIONS_COLLECTION, goal.war_room_conversation_id,
        )
        if conv_row is None:
            return []
        msgs = conv_row.get("messages", []) or []
        out: list[dict[str, Any]] = []
        for m in msgs:
            if m.get("role") != "user":
                continue
            sender = m.get("metadata", {}).get("sender", {}) or {}
            out.append({
                "author_id": sender.get("id", ""),
                "author_name": sender.get("name", ""),
                "author_kind": sender.get("kind", ""),
                "body": m.get("content", ""),
                "ts": m.get("ts", ""),
            })
        return out[-limit:]

    # ── ToolProvider (Task 14) ───────────────────────────────────────

    def get_tools(self, user_ctx: Any = None) -> list[ToolDefinition]:
        """Return the core agent tool definitions."""
        return [
            _TOOL_COMPLETE_RUN,
            _TOOL_COMMITMENT_CREATE,
            _TOOL_COMMITMENT_COMPLETE,
            _TOOL_COMMITMENT_LIST,
            _TOOL_AGENT_MEMORY_SAVE,
            _TOOL_AGENT_MEMORY_SEARCH,
            _TOOL_AGENT_MEMORY_PROMOTE,
            _TOOL_AGENT_LIST,
            _TOOL_AGENT_SEND_MESSAGE,
            _TOOL_AGENT_DELEGATE,
            _TOOL_GOAL_CREATE,
            _TOOL_GOAL_ASSIGN,
            _TOOL_GOAL_UNASSIGN,
            _TOOL_GOAL_HANDOFF,
            _TOOL_GOAL_POST,
            _TOOL_GOAL_STATUS,
            _TOOL_GOAL_SUMMARY,
            _TOOL_DELIVERABLE_CREATE,
            _TOOL_DELIVERABLE_FINALIZE,
            _TOOL_DELIVERABLE_SUPERSEDE,
            _TOOL_GOAL_ADD_DEPENDENCY,
            _TOOL_GOAL_REMOVE_DEPENDENCY,
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Dispatch a tool call by name. Raises KeyError for unknown tools.

        Reads the active agent's id from the ``_active_agent_id`` ContextVar
        and injects it as ``_agent_id`` into the tool's arguments dict
        unless the caller already set it. ``_run_agent_internal`` sets
        the contextvar before invoking ``AIService.chat``; without this
        injection every ``_exec_*`` handler would see an empty
        ``_agent_id`` and bail with "requires _agent_id (injected by
        runtime)" — which manifests to the model as "agent functions
        unavailable".
        """
        if "_agent_id" not in arguments:
            ctx_agent_id = _active_agent_id.get("")
            if ctx_agent_id:
                arguments = {**arguments, "_agent_id": ctx_agent_id}
        if "_delegation_chain" not in arguments:
            ctx_chain = _active_delegation_chain.get([])
            if ctx_chain:
                arguments = {**arguments, "_delegation_chain": list(ctx_chain)}

        if name == "complete_run":
            return await self._exec_complete_run(arguments)
        if name == "commitment_create":
            return await self._exec_commitment_create(arguments)
        if name == "commitment_complete":
            return await self._exec_commitment_complete(arguments)
        if name == "commitment_list":
            return await self._exec_commitment_list(arguments)
        if name == "agent_memory_save":
            return await self._exec_memory_save(arguments)
        if name == "agent_memory_search":
            return await self._exec_memory_search(arguments)
        if name == "agent_memory_review_and_promote":
            return await self._exec_memory_promote(arguments)
        if name == "agent_list":
            return await self._exec_agent_list(arguments)
        if name == "agent_send_message":
            return await self._exec_agent_send_message(arguments)
        if name == "agent_delegate":
            return await self._exec_agent_delegate(arguments)
        if name == "goal_create":
            return await self._exec_goal_create(arguments)
        if name == "goal_assign":
            return await self._exec_goal_assign(arguments)
        if name == "goal_unassign":
            return await self._exec_goal_unassign(arguments)
        if name == "goal_handoff":
            return await self._exec_goal_handoff(arguments)
        if name == "goal_post":
            return await self._exec_goal_post(arguments)
        if name == "goal_status":
            return await self._exec_goal_status(arguments)
        if name == "goal_summary":
            return await self._exec_goal_summary(arguments)
        if name == "deliverable_create":
            return await self._exec_deliverable_create(arguments)
        if name == "deliverable_finalize":
            return await self._exec_deliverable_finalize(arguments)
        if name == "deliverable_supersede":
            return await self._exec_deliverable_supersede(arguments)
        if name == "goal_add_dependency":
            return await self._exec_goal_add_dependency(arguments)
        if name == "goal_remove_dependency":
            return await self._exec_goal_remove_dependency(arguments)
        raise KeyError(f"unknown tool: {name}")

    async def _exec_complete_run(self, args: dict[str, Any]) -> str:
        agent_id = str(args.get("_agent_id", ""))
        reason = str(args.get("reason", "")).strip() or "(no reason given)"
        if not agent_id:
            return "error: complete_run requires _agent_id (injected by runtime)"
        rows = await self._storage.query(  # type: ignore[union-attr]
            Query(
                collection=_AGENT_RUNS_COLLECTION,
                filters=[
                    Filter(field="agent_id", op=FilterOp.EQ, value=agent_id),
                    Filter(field="status", op=FilterOp.EQ, value="running"),
                ],
            )
        )
        if not rows:
            return f"no active run for agent {agent_id}"
        row = sorted(rows, key=lambda r: r.get("started_at", ""), reverse=True)[0]
        row["status"] = RunStatus.COMPLETED.value
        row["ended_at"] = _now().isoformat()
        row["final_message_text"] = reason
        await self._storage.put(_AGENT_RUNS_COLLECTION, row["_id"], row)  # type: ignore[union-attr]
        return f"run {row['_id']} marked complete: {reason}"

    async def _exec_commitment_create(self, args: dict[str, Any]) -> str:
        agent_id = str(args.get("_agent_id", ""))
        content = str(args.get("content", "")).strip()
        if not agent_id or not content:
            return "error: commitment_create requires _agent_id and content"
        if args.get("due_at"):
            due_at = datetime.fromisoformat(str(args["due_at"]))
        else:
            seconds = float(args.get("due_in_seconds", 1800))
            due_at = _now() + timedelta(seconds=seconds)
        c = await self.create_commitment(agent_id=agent_id, content=content, due_at=due_at)
        return f"commitment {c.id} created, due {c.due_at.isoformat()}"

    async def _exec_commitment_complete(self, args: dict[str, Any]) -> str:
        cid = str(args.get("commitment_id", ""))
        if not cid:
            return "error: commitment_complete requires commitment_id"
        c = await self.complete_commitment(cid, note=str(args.get("note", "")))
        return f"commitment {c.id} completed"

    async def _exec_commitment_list(self, args: dict[str, Any]) -> str:
        agent_id = str(args.get("_agent_id", ""))
        if not agent_id:
            return "error: commitment_list requires _agent_id"
        include = bool(args.get("include_completed", False))
        cs = await self.list_commitments(agent_id=agent_id, include_completed=include)
        if not cs:
            return "(no commitments)"
        lines = [
            f"- [{c.id}] {c.content} — due {c.due_at.isoformat()}"
            + (f" (completed: {c.completion_note})" if c.completed_at else "")
            for c in cs
        ]
        return "\n".join(lines)

    async def _exec_memory_save(self, args: dict[str, Any]) -> str:
        agent_id = str(args.get("_agent_id", ""))
        content = str(args.get("content", "")).strip()
        if not agent_id or not content:
            return "error: agent_memory_save requires _agent_id and content"
        kind = str(args.get("kind", "fact"))
        tags_raw = args.get("tags") or []
        tags = frozenset(str(t) for t in tags_raw if str(t).strip())
        m = await self.save_memory(agent_id=agent_id, content=content, kind=kind, tags=tags)
        return f"memory {m.id} saved (state={m.state.value}, kind={m.kind})"

    async def _exec_memory_search(self, args: dict[str, Any]) -> str:
        agent_id = str(args.get("_agent_id", ""))
        if not agent_id:
            return "error: agent_memory_search requires _agent_id"
        query = str(args.get("query", "")).strip()
        limit = int(args.get("limit", 20))
        out = await self.search_memory(agent_id=agent_id, query=query, limit=limit)
        if not out:
            return "(no matches)"
        return "\n".join(f"- [{m.id}] ({m.state.value}, {m.kind}) {m.content}" for m in out)

    async def _exec_memory_promote(self, args: dict[str, Any]) -> str:
        reviews = args.get("reviews") or []
        if not isinstance(reviews, list):
            return "error: reviews must be an array"
        applied = 0
        for r in reviews:
            if not isinstance(r, dict):
                continue
            mid = str(r.get("memory_id", ""))
            decision = str(r.get("decision", ""))
            if not mid or decision not in {"promote", "demote", "keep"}:
                continue
            if decision == "promote":
                await self.promote_memory(memory_id=mid, score=float(r.get("score", 0.5)))
                applied += 1
            elif decision == "demote":
                await self.promote_memory(
                    memory_id=mid,
                    score=float(r.get("score", 0.0)),
                    state=MemoryState.SHORT_TERM,
                )
                applied += 1
            # 'keep' is a no-op
        return f"reviewed {len(reviews)} memories, applied {applied}"

    # ── Phase 2: peer messaging tool helpers ─────────────────────────

    async def _load_peer_by_name(
        self,
        *,
        caller_agent_id: str,
        target_name: str,
    ) -> Agent:
        """Resolve a peer agent by name within the caller's owner.

        Raises ``PermissionError`` if no agent matches in the same owner
        namespace (cross-user reach is treated as a permission failure,
        not a missing record, to avoid leaking that the name exists).
        """
        if self._storage is None:
            raise RuntimeError("not started")
        me = await self.get_agent(caller_agent_id)
        if me is None:
            raise PermissionError("caller agent not found")
        rows = await self._storage.query(
            Query(
                collection=_AGENTS_COLLECTION,
                filters=[
                    Filter(field="owner_user_id", op=FilterOp.EQ, value=me.owner_user_id),
                    Filter(field="name", op=FilterOp.EQ, value=target_name),
                ],
            )
        )
        if not rows:
            raise PermissionError(f"no peer named {target_name!r}")
        return _agent_from_dict(rows[0])

    async def _exec_agent_list(self, args: dict[str, Any]) -> str:
        agent_id = args.get("_agent_id")
        if not isinstance(agent_id, str) or not agent_id:
            return "error: agent_list requires _agent_id"
        me = await self.get_agent(agent_id)
        if me is None:
            return "error: caller agent not found"
        peers = await self.list_agents(owner_user_id=me.owner_user_id)
        out = [
            {
                "name": p.name,
                "role_label": p.role_label,
                "status": p.status.value,
                "conversation_id": p.conversation_id,
            }
            for p in peers
            if p.id != me.id  # exclude self
        ]
        return json.dumps(out)

    async def _exec_agent_send_message(self, args: dict[str, Any]) -> str:
        agent_id = args.get("_agent_id")
        target_name = str(args.get("target_name", "")).strip()
        body = str(args.get("body", "")).strip()
        if not isinstance(agent_id, str) or not agent_id:
            return "error: missing _agent_id"
        if not target_name:
            return "error: target_name is required"
        if not body:
            return "error: body is required"

        priority, perr = _parse_priority(args.get("priority"), default="normal")
        if perr is not None:
            return f"error: {perr}"
        assert priority is not None

        me = await self.get_agent(agent_id)
        if me is None:
            return "error: caller agent not found"

        try:
            target = await self._load_peer_by_name(
                caller_agent_id=agent_id, target_name=target_name,
            )
        except PermissionError as exc:
            return f"error: {exc}"

        if target.id == me.id:
            return "error: cannot message yourself"

        await self._signal_agent(
            agent_id=target.id,
            signal_kind="inbox",
            body=body,
            sender_kind="agent",
            sender_id=me.id,
            sender_name=me.name,
            priority=priority,
        )
        return f"sent to {target_name}"

    async def _exec_agent_delegate(self, args: dict[str, Any]) -> str:
        agent_id = args.get("_agent_id")
        target_name = str(args.get("target_name", "")).strip()
        instruction = str(args.get("instruction", "")).strip()
        max_wait_s_raw = args.get("max_wait_s", 600)
        try:
            max_wait_s = max(1, int(max_wait_s_raw))
        except (TypeError, ValueError):
            return "error: max_wait_s must be a number"
        if not isinstance(agent_id, str) or not agent_id:
            return "error: missing _agent_id"
        if not target_name or not instruction:
            return "error: target_name and instruction are required"

        # Delegations default to ``urgent`` priority — the caller is
        # awaiting an END_TURN reply, so a busy target should drop
        # whatever it's doing at the next safe boundary instead of
        # finishing its current round first.
        priority, perr = _parse_priority(args.get("priority"), default="urgent")
        if perr is not None:
            return f"error: {perr}"
        assert priority is not None

        me = await self.get_agent(agent_id)
        if me is None:
            return "error: caller agent not found"

        try:
            target = await self._load_peer_by_name(
                caller_agent_id=agent_id, target_name=target_name,
            )
        except PermissionError as exc:
            return f"error: {exc}"

        if target.id == me.id:
            return "error: cannot delegate to yourself"

        # Cycle + depth check. The chain is the list of agent IDs that
        # have already initiated a delegation in the current call stack;
        # we append the current caller before checking, so a chain that
        # would loop back to a prior delegator (target.id ∈ chain) or
        # would push depth past the cap is rejected before fire.
        chain_raw = args.get("_delegation_chain", [])
        if not isinstance(chain_raw, list):
            chain_raw = []
        chain: list[str] = [str(x) for x in chain_raw]
        chain.append(me.id)
        if target.id in chain:
            return f"error: delegation cycle — {target.name} already in chain"
        if len(chain) >= _DELEGATION_DEPTH_CAP:
            return (
                f"error: delegation depth cap reached "
                f"({_DELEGATION_DEPTH_CAP})"
            )

        delegation_id = f"del_{uuid.uuid4().hex[:12]}"
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending_delegations[delegation_id] = future

        try:
            await self._signal_agent(
                agent_id=target.id,
                signal_kind="delegation",
                body=instruction,
                sender_kind="agent",
                sender_id=me.id,
                sender_name=me.name,
                delegation_id=delegation_id,
                metadata={"chain": chain},
                priority=priority,
            )
            try:
                reply = await asyncio.wait_for(future, timeout=max_wait_s)
            except TimeoutError:
                return (
                    f"error: delegation to {target_name} timed out after "
                    f"{max_wait_s}s"
                )
            except Exception as exc:
                # Target run failed (FAILED / TIMED_OUT etc.) — surface
                # to the caller as an error string rather than letting
                # the exception propagate into the AI tool runtime.
                return f"error: target run {exc}"
            return reply
        finally:
            self._pending_delegations.pop(delegation_id, None)

    # ── Phase 4: goal tool helpers ───────────────────────────────────

    async def _is_active_assignee(
        self, *, goal_id: str, agent_id: str,
    ) -> GoalAssignment | None:
        """Return the active assignment if the agent is on the goal, else None."""
        asgns = await self.list_assignments(goal_id=goal_id, active_only=True)
        for a in asgns:
            if a.agent_id == agent_id:
                return a
        return None

    async def _is_driver(self, *, goal_id: str, agent_id: str) -> bool:
        a = await self._is_active_assignee(goal_id=goal_id, agent_id=agent_id)
        return a is not None and a.role is AssignmentRole.DRIVER

    def _coerce_role(self, raw: Any) -> AssignmentRole | None:
        """Map a string to AssignmentRole. Returns None on bad input."""
        if raw is None:
            return None
        try:
            return AssignmentRole(str(raw).lower().strip())
        except ValueError:
            return None

    def _coerce_goal_status(self, raw: Any) -> GoalStatus | None:
        try:
            return GoalStatus(str(raw).lower().strip())
        except ValueError:
            return None

    async def _exec_goal_create(self, args: dict[str, Any]) -> str:
        agent_id = str(args.get("_agent_id", ""))
        if not agent_id:
            return "error: goal_create requires _agent_id (injected by runtime)"
        me = await self.get_agent(agent_id)
        if me is None:
            return "error: caller agent not found"
        name = str(args.get("name", "")).strip()
        if not name:
            return "error: name is required"
        description = str(args.get("description", ""))
        cost_cap_raw = args.get("cost_cap_usd")
        cost_cap = float(cost_cap_raw) if cost_cap_raw is not None else None

        # Resolve assign_to. Each entry is either {agent_name, role} or
        # a plain string (interpreted as agent_name with role=collaborator).
        assign_raw = args.get("assign_to") or []
        assign_to: list[tuple[str, AssignmentRole]] = []
        if not isinstance(assign_raw, list):
            return "error: assign_to must be an array"
        for entry in assign_raw:
            target_name: str
            role: AssignmentRole
            if isinstance(entry, str):
                target_name = entry.strip()
                role = AssignmentRole.COLLABORATOR
            elif isinstance(entry, dict):
                target_name = str(entry.get("agent_name", "")).strip()
                role = self._coerce_role(entry.get("role")) or AssignmentRole.COLLABORATOR
            else:
                return "error: assign_to entries must be strings or {agent_name, role} objects"
            if not target_name:
                return "error: assign_to entry missing agent_name"
            # Resolve owner-scoped via _load_peer_by_name (raises if cross-owner).
            try:
                await self._load_peer_by_name(
                    caller_agent_id=agent_id, target_name=target_name,
                )
            except PermissionError as exc:
                return f"error: {exc}"
            assign_to.append((target_name, role))

        try:
            g = await self.create_goal(
                owner_user_id=me.owner_user_id,
                name=name,
                description=description,
                cost_cap_usd=cost_cap,
                assign_to=assign_to,
                assigned_by=f"agent:{agent_id}",
            )
        except ValueError as exc:
            return f"error: {exc}"
        return json.dumps({"goal_id": g.id, "war_room_conversation_id": g.war_room_conversation_id})

    async def _exec_goal_assign(self, args: dict[str, Any]) -> str:
        agent_id = str(args.get("_agent_id", ""))
        goal_id = str(args.get("goal_id", "")).strip()
        target_name = str(args.get("agent_name", "")).strip()
        role = self._coerce_role(args.get("role"))
        if not agent_id:
            return "error: goal_assign requires _agent_id"
        if not goal_id or not target_name or role is None:
            return "error: goal_id, agent_name, role required"
        goal = await self.get_goal(goal_id)
        if goal is None:
            return f"error: goal {goal_id} not found"
        me = await self.get_agent(agent_id)
        if me is None or me.owner_user_id != goal.owner_user_id:
            return "error: not authorized for this goal"
        if not await self._is_driver(goal_id=goal_id, agent_id=agent_id):
            return "error: only the goal's DRIVER may assign agents"
        try:
            target = await self._load_peer_by_name(
                caller_agent_id=agent_id, target_name=target_name,
            )
        except PermissionError as exc:
            return f"error: {exc}"
        ga = await self.assign_agent_to_goal(
            goal_id=goal_id,
            agent_id=target.id,
            role=role,
            assigned_by=f"agent:{agent_id}",
        )
        return f"assigned {target_name} as {ga.role.value}"

    async def _exec_goal_unassign(self, args: dict[str, Any]) -> str:
        agent_id = str(args.get("_agent_id", ""))
        goal_id = str(args.get("goal_id", "")).strip()
        target_name = str(args.get("agent_name", "")).strip()
        if not agent_id:
            return "error: goal_unassign requires _agent_id"
        if not goal_id or not target_name:
            return "error: goal_id and agent_name required"
        goal = await self.get_goal(goal_id)
        if goal is None:
            return f"error: goal {goal_id} not found"
        me = await self.get_agent(agent_id)
        if me is None or me.owner_user_id != goal.owner_user_id:
            return "error: not authorized for this goal"
        try:
            target = await self._load_peer_by_name(
                caller_agent_id=agent_id, target_name=target_name,
            )
        except PermissionError as exc:
            return f"error: {exc}"
        # DRIVER may unassign anyone; non-DRIVER may only unassign self.
        is_driver = await self._is_driver(goal_id=goal_id, agent_id=agent_id)
        if not is_driver and target.id != agent_id:
            return "error: only the DRIVER (or yourself) may unassign on this goal"
        try:
            await self.unassign_agent_from_goal(goal_id=goal_id, agent_id=target.id)
        except KeyError as exc:
            return f"error: {exc}"
        return f"unassigned {target_name}"

    async def _exec_goal_handoff(self, args: dict[str, Any]) -> str:
        agent_id = str(args.get("_agent_id", ""))
        goal_id = str(args.get("goal_id", "")).strip()
        target_name = str(args.get("target_name", "")).strip()
        note = str(args.get("note", ""))
        # ``role`` defaults to "driver" per the spec — if specified as
        # something else, that's the role the FROM-agent receives after
        # the handoff (the TO-agent is always promoted to DRIVER).
        role_raw = args.get("role")
        role_for_from = AssignmentRole.COLLABORATOR
        if role_raw is not None:
            coerced = self._coerce_role(role_raw)
            if coerced is None:
                return "error: role must be one of driver/collaborator/reviewer"
            # If caller supplied "driver" (the default), the FROM-agent
            # demotes to COLLABORATOR — we don't allow two DRIVERs.
            if coerced is not AssignmentRole.DRIVER:
                role_for_from = coerced
        if not agent_id:
            return "error: goal_handoff requires _agent_id"
        if not goal_id or not target_name:
            return "error: goal_id and target_name required"
        goal = await self.get_goal(goal_id)
        if goal is None:
            return f"error: goal {goal_id} not found"
        me = await self.get_agent(agent_id)
        if me is None or me.owner_user_id != goal.owner_user_id:
            return "error: not authorized for this goal"
        if not await self._is_driver(goal_id=goal_id, agent_id=agent_id):
            return "error: only the current DRIVER may hand off"
        try:
            target = await self._load_peer_by_name(
                caller_agent_id=agent_id, target_name=target_name,
            )
        except PermissionError as exc:
            return f"error: {exc}"
        if target.id == agent_id:
            return "error: cannot hand off to yourself"
        try:
            await self.handoff_goal(
                goal_id=goal_id,
                from_agent_id=agent_id,
                to_agent_id=target.id,
                new_role_for_from=role_for_from,
                note=note,
            )
        except (KeyError, ValueError) as exc:
            return f"error: {exc}"
        return f"handed off goal {goal_id} to {target_name}"

    async def _exec_goal_post(self, args: dict[str, Any]) -> str:
        agent_id = str(args.get("_agent_id", ""))
        goal_id = str(args.get("goal_id", "")).strip()
        body = str(args.get("body", "")).strip()
        if not agent_id:
            return "error: goal_post requires _agent_id"
        if not goal_id or not body:
            return "error: goal_id and body required"
        goal = await self.get_goal(goal_id)
        if goal is None:
            return f"error: goal {goal_id} not found"
        me = await self.get_agent(agent_id)
        if me is None or me.owner_user_id != goal.owner_user_id:
            return "error: not authorized for this goal"
        # Assignee-only.
        asgn = await self._is_active_assignee(goal_id=goal_id, agent_id=agent_id)
        if asgn is None:
            return "error: only assignees can post to the war room"

        if self._storage is None:
            return "error: not started"
        conv_id = goal.war_room_conversation_id
        conv_row = await self._storage.get(_AI_CONVERSATIONS_COLLECTION, conv_id)
        if conv_row is None:
            return "error: war room conversation missing"
        msgs = list(conv_row.get("messages", []) or [])
        now = _now()
        msg_id = f"msg_{uuid.uuid4().hex[:12]}"
        msg: dict[str, Any] = {
            "id": msg_id,
            "role": "user",
            "content": body,
            "ts": now.isoformat(),
            "metadata": {
                "sender": {"kind": "agent", "id": me.id, "name": me.name},
                "goal_id": goal_id,
            },
        }
        msgs.append(msg)
        conv_row["messages"] = msgs
        conv_row["updated_at"] = now.isoformat()
        await self._storage.put(_AI_CONVERSATIONS_COLLECTION, conv_id, conv_row)

        # Process mentions: each named peer gets an inbox signal.
        mentions_raw = args.get("mention") or []
        mention_count = 0
        if isinstance(mentions_raw, list):
            short_body = body if len(body) <= 200 else body[:200]
            for raw_name in mentions_raw:
                name = str(raw_name).strip()
                if not name:
                    continue
                try:
                    target = await self._load_peer_by_name(
                        caller_agent_id=agent_id, target_name=name,
                    )
                except PermissionError:
                    continue
                if target.id == me.id:
                    continue
                await self._signal_agent(
                    agent_id=target.id,
                    signal_kind="inbox",
                    body=f"[mentioned in war room {goal.name}]: {short_body}",
                    sender_kind="agent",
                    sender_id=me.id,
                    sender_name=me.name,
                    source_conv_id=conv_id,
                    source_message_id=msg_id,
                    metadata={"goal_id": goal_id, "kind": "war_room_mention"},
                )
                mention_count += 1
        return f"posted to war room (mentions={mention_count})"

    async def _exec_goal_status(self, args: dict[str, Any]) -> str:
        agent_id = str(args.get("_agent_id", ""))
        goal_id = str(args.get("goal_id", "")).strip()
        new_status = self._coerce_goal_status(args.get("new_status"))
        if not agent_id:
            return "error: goal_status requires _agent_id"
        if not goal_id or new_status is None:
            return "error: goal_id and new_status required (one of new/in_progress/blocked/complete/cancelled)"
        goal = await self.get_goal(goal_id)
        if goal is None:
            return f"error: goal {goal_id} not found"
        me = await self.get_agent(agent_id)
        if me is None or me.owner_user_id != goal.owner_user_id:
            return "error: not authorized for this goal"
        if not await self._is_driver(goal_id=goal_id, agent_id=agent_id):
            return "error: only the goal's DRIVER may change status"
        await self.update_goal_status(goal_id, new_status)
        return f"goal {goal_id} status set to {new_status.value}"

    async def _exec_goal_summary(self, args: dict[str, Any]) -> str:
        agent_id = str(args.get("_agent_id", ""))
        goal_id = str(args.get("goal_id", "")).strip()
        if not agent_id:
            return "error: goal_summary requires _agent_id"
        if not goal_id:
            return "error: goal_id is required"
        goal = await self.get_goal(goal_id)
        if goal is None:
            return f"error: goal {goal_id} not found"
        me = await self.get_agent(agent_id)
        if me is None or me.owner_user_id != goal.owner_user_id:
            return "error: not authorized for this goal"
        # Assignee-only.
        if await self._is_active_assignee(goal_id=goal_id, agent_id=agent_id) is None:
            return "error: only assignees can read the summary"

        asgns = await self.list_assignments(goal_id=goal_id, active_only=True)
        # Build (agent_id → name) lookup.
        names: dict[str, str] = {}
        for a in asgns:
            ag = await self.get_agent(a.agent_id)
            if ag is not None:
                names[a.agent_id] = ag.name
        recent = await self._recent_war_room_posts(goal_id, limit=10)
        unsat = await self.list_goal_dependencies(
            dependent_goal_id=goal_id, satisfied=False,
        )
        out = {
            "name": goal.name,
            "description": goal.description,
            "status": goal.status.value,
            "assignees": [
                {
                    "agent_id": a.agent_id,
                    "agent_name": names.get(a.agent_id, ""),
                    "role": a.role.value,
                }
                for a in asgns
            ],
            "recent_posts": recent,
            "lifetime_cost_usd": goal.lifetime_cost_usd,
            "is_dependency_blocked": len(unsat) > 0,
        }
        return json.dumps(out)

    # ── Phase 5: deliverable + dependency tool helpers ───────────────

    async def _exec_deliverable_create(self, args: dict[str, Any]) -> str:
        agent_id = str(args.get("_agent_id", ""))
        if not agent_id:
            return "error: deliverable_create requires _agent_id"
        goal_id = str(args.get("goal_id", "")).strip()
        name = str(args.get("name", "")).strip()
        kind = str(args.get("kind", "")).strip()
        content_ref = str(args.get("content_ref", ""))
        if not goal_id or not name or not kind:
            return "error: goal_id, name, kind required"
        goal = await self.get_goal(goal_id)
        if goal is None:
            return f"error: goal {goal_id} not found"
        me = await self.get_agent(agent_id)
        if me is None or me.owner_user_id != goal.owner_user_id:
            return "error: not authorized for this goal"
        if await self._is_active_assignee(
            goal_id=goal_id, agent_id=agent_id,
        ) is None:
            return "error: only assignees may create deliverables on this goal"
        d = await self.create_deliverable(
            goal_id=goal_id,
            name=name,
            kind=kind,
            produced_by_agent_id=agent_id,
            content_ref=content_ref,
        )
        return json.dumps({"deliverable_id": d.id, "state": d.state.value})

    async def _exec_deliverable_finalize(self, args: dict[str, Any]) -> str:
        agent_id = str(args.get("_agent_id", ""))
        deliverable_id = str(args.get("deliverable_id", "")).strip()
        if not agent_id:
            return "error: deliverable_finalize requires _agent_id"
        if not deliverable_id:
            return "error: deliverable_id is required"
        d = await self.get_deliverable(deliverable_id)
        if d is None:
            return f"error: deliverable {deliverable_id} not found"
        goal = await self.get_goal(d.goal_id)
        if goal is None:
            return f"error: goal {d.goal_id} not found"
        me = await self.get_agent(agent_id)
        if me is None or me.owner_user_id != goal.owner_user_id:
            return "error: not authorized for this goal"
        is_producer = d.produced_by_agent_id == agent_id
        is_driver = await self._is_driver(goal_id=d.goal_id, agent_id=agent_id)
        if not (is_producer or is_driver):
            return "error: only the producer or DRIVER may finalize"
        try:
            finalized = await self.finalize_deliverable(deliverable_id)
        except (KeyError, ValueError) as exc:
            return f"error: {exc}"
        return f"deliverable {finalized.id} READY"

    async def _exec_deliverable_supersede(self, args: dict[str, Any]) -> str:
        agent_id = str(args.get("_agent_id", ""))
        deliverable_id = str(args.get("deliverable_id", "")).strip()
        new_content_ref = str(args.get("new_content_ref", "")).strip()
        finalize = bool(args.get("finalize", False))
        if not agent_id:
            return "error: deliverable_supersede requires _agent_id"
        if not deliverable_id or not new_content_ref:
            return "error: deliverable_id and new_content_ref required"
        d = await self.get_deliverable(deliverable_id)
        if d is None:
            return f"error: deliverable {deliverable_id} not found"
        goal = await self.get_goal(d.goal_id)
        if goal is None:
            return f"error: goal {d.goal_id} not found"
        me = await self.get_agent(agent_id)
        if me is None or me.owner_user_id != goal.owner_user_id:
            return "error: not authorized for this goal"
        is_producer = d.produced_by_agent_id == agent_id
        is_driver = await self._is_driver(goal_id=d.goal_id, agent_id=agent_id)
        if not (is_producer or is_driver):
            return "error: only the producer or DRIVER may supersede"
        try:
            obs, new = await self.supersede_deliverable(
                deliverable_id, new_content_ref=new_content_ref,
                finalize=finalize,
            )
        except (KeyError, ValueError) as exc:
            return f"error: {exc}"
        return json.dumps({
            "obsoleted_id": obs.id,
            "new_id": new.id,
            "new_state": new.state.value,
        })

    async def _exec_goal_add_dependency(self, args: dict[str, Any]) -> str:
        agent_id = str(args.get("_agent_id", ""))
        dependent_goal_id = str(args.get("goal_id", "")).strip()
        source_goal_id = str(args.get("source_goal_id", "")).strip()
        required_name = str(
            args.get("required_deliverable_name", "")
        ).strip()
        if not agent_id:
            return "error: goal_add_dependency requires _agent_id"
        if not dependent_goal_id or not source_goal_id or not required_name:
            return (
                "error: goal_id, source_goal_id, "
                "required_deliverable_name required"
            )
        dep_goal = await self.get_goal(dependent_goal_id)
        src_goal = await self.get_goal(source_goal_id)
        if dep_goal is None:
            return f"error: dependent goal {dependent_goal_id} not found"
        if src_goal is None:
            return f"error: source goal {source_goal_id} not found"
        me = await self.get_agent(agent_id)
        if me is None or me.owner_user_id != dep_goal.owner_user_id:
            return "error: not authorized for the dependent goal"
        # Source goal must also be same-owner — no cross-owner reach.
        if src_goal.owner_user_id != dep_goal.owner_user_id:
            return "error: source and dependent goals must share owner"
        if not await self._is_driver(
            goal_id=dependent_goal_id, agent_id=agent_id,
        ):
            return "error: only the dependent goal's DRIVER may add dependencies"
        dep = await self.add_goal_dependency(
            dependent_goal_id=dependent_goal_id,
            source_goal_id=source_goal_id,
            required_deliverable_name=required_name,
        )
        return json.dumps({
            "dependency_id": dep.id,
            "dependent_goal_id": dep.dependent_goal_id,
            "source_goal_id": dep.source_goal_id,
            "satisfied": dep.satisfied_at is not None,
        })

    async def _exec_goal_remove_dependency(self, args: dict[str, Any]) -> str:
        agent_id = str(args.get("_agent_id", ""))
        dependency_id = str(args.get("dependency_id", "")).strip()
        if not agent_id:
            return "error: goal_remove_dependency requires _agent_id"
        if not dependency_id:
            return "error: dependency_id required"
        if self._storage is None:
            return "error: not started"
        row = await self._storage.get(_DEPENDENCIES_COLLECTION, dependency_id)
        if row is None:
            return f"error: dependency {dependency_id} not found"
        dep = _dependency_from_dict(row)
        dep_goal = await self.get_goal(dep.dependent_goal_id)
        if dep_goal is None:
            return f"error: dependent goal {dep.dependent_goal_id} not found"
        me = await self.get_agent(agent_id)
        if me is None or me.owner_user_id != dep_goal.owner_user_id:
            return "error: not authorized for the dependent goal"
        if not await self._is_driver(
            goal_id=dep.dependent_goal_id, agent_id=agent_id,
        ):
            return "error: only the DRIVER may remove dependencies"
        try:
            await self.remove_goal_dependency(dependency_id)
        except KeyError as exc:
            return f"error: {exc}"
        return f"dependency {dependency_id} removed"

    # ── Tool argument injection (Task 15) ────────────────────────────

    def _inject_agent_id(
        self,
        agent_id: str,
        tools_dict: dict[str, Any],
        *,
        delegation_chain: list[str] | None = None,
    ) -> dict[str, Any]:
        """Wrap each tool handler so ``_agent_id`` (and, when
        ``delegation_chain`` is non-empty, ``_delegation_chain``) get
        injected into the call's argument dict.

        Expects tools_dict shape: ``dict[name, tuple[ToolDefinition, callable]]``
        matching agent_loop.run_loop's expected shape.

        The wrapped handler accepts the same arguments dict and mutates it
        to include ``_agent_id`` if absent (caller's value wins if present).
        ``_delegation_chain`` is only injected when the active run is
        delegation-triggered, so non-delegation runs see the same shape
        they always have.
        """
        chain_to_inject = list(delegation_chain) if delegation_chain else None
        wrapped: dict[str, Any] = {}
        for name, entry in tools_dict.items():
            tool_def, handler = entry

            async def _wrapped(
                args: dict[str, Any],
                _h: Any = handler,
                _chain: list[str] | None = chain_to_inject,
            ) -> Any:
                new_args = dict(args)
                new_args.setdefault("_agent_id", agent_id)
                if _chain is not None:
                    new_args.setdefault("_delegation_chain", list(_chain))
                return await _h(new_args)

            wrapped[name] = (tool_def, _wrapped)
        return wrapped

    # ── WsHandlerProvider ────────────────────────────────────────────

    def _wrap_ws_handler(self, handler: Any) -> Any:
        """Inject ``ref`` (and a ``type``) into the response so the SPA's
        rpc client can route it back to the awaiting promise.

        The dispatcher (``dispatch_frame``) sends whatever the handler
        returns straight to the wire, and the SPA's ``useWebSocket``
        client routes by ``frame.ref`` matching the outgoing ``id``.
        Without ``ref``, the response is dropped and the rpc Promise
        hangs forever. Every handler in this service produces a payload
        dict (e.g. ``{"agents": [...]}``); this wrapper adds the ref/type.
        """

        async def _wrapped(conn: Any, frame: dict[str, Any]) -> Any:
            result = await handler(conn, frame)
            if isinstance(result, dict) and "ref" not in result:
                result = {
                    "type": f"{frame.get('type', '')}.result",
                    "ref": frame.get("id"),
                    **result,
                }
            return result

        return _wrapped

    def get_ws_handlers(self) -> dict[str, Any]:
        if not self._enabled:
            return {}
        raw: dict[str, Any] = {
            "agents.create": self._ws_create,
            "agents.get": self._ws_get,
            "agents.list": self._ws_list,
            "agents.update": self._ws_update,
            "agents.delete": self._ws_delete,
            "agents.set_status": self._ws_set_status,
            "agents.run_now": self._ws_run_now,
            "agents.get_defaults": self._ws_get_defaults,
            "agents.runs.list": self._ws_runs_list,
            "agents.commitments.list": self._ws_commitments_list,
            "agents.commitments.create": self._ws_commitments_create,
            "agents.commitments.complete": self._ws_commitments_complete,
            "agents.memories.list": self._ws_memories_list,
            "agents.memories.set_state": self._ws_memories_set_state,
            "agents.tools.list_available": self._ws_tools_list_available,
            # Phase 4 — goals
            "goals.create": self._ws_goals_create,
            "goals.list": self._ws_goals_list,
            "goals.get": self._ws_goals_get,
            "goals.update_status": self._ws_goals_update_status,
            "goals.assignments.list": self._ws_goals_assignments_list,
            "goals.assignments.add": self._ws_goals_assignments_add,
            "goals.assignments.remove": self._ws_goals_assignments_remove,
            "goals.assignments.handoff": self._ws_goals_assignments_handoff,
            "goals.summary": self._ws_goals_summary,
            "goals.posts.list": self._ws_goals_posts_list,
            # Phase 5 — deliverables + dependencies
            "deliverables.list": self._ws_deliverables_list,
            "deliverables.create": self._ws_deliverables_create,
            "deliverables.finalize": self._ws_deliverables_finalize,
            "deliverables.supersede": self._ws_deliverables_supersede,
            "goals.dependencies.list": self._ws_goal_dependencies_list,
            "goals.dependencies.add": self._ws_goal_dependencies_add,
            "goals.dependencies.remove": self._ws_goal_dependencies_remove,
        }
        return {k: self._wrap_ws_handler(h) for k, h in raw.items()}

    def _is_admin(self, conn: Any) -> bool:
        return getattr(conn, "user_level", 999) <= 0

    def _caller_user_id(self, conn: Any) -> str:
        uid = getattr(conn, "user_id", "") or ""
        if not uid:
            raise PermissionError("anonymous caller")
        return uid

    async def _ws_create(self, conn: Any, params: dict[str, Any]) -> dict[str, Any]:
        owner = self._caller_user_id(conn)
        name = str(params.get("name", "")).strip()
        if not name:
            raise ValueError("name is required")
        # Drop unknown fields; create_agent accepts a tight allowlist.
        allowed_fields = {
            "display_name",
            "role_label", "persona", "system_prompt", "procedural_rules",
            "profile_id", "avatar_kind", "avatar_value", "cost_cap_usd",
            "tools_include", "tools_exclude",
            "heartbeat_enabled", "heartbeat_interval_s",
            "heartbeat_checklist", "dream_enabled", "dream_quiet_hours",
            "dream_probability", "dream_max_per_night",
        }
        fields = {k: v for k, v in params.items() if k in allowed_fields}
        a = await self.create_agent(owner_user_id=owner, name=name, **fields)
        return {"agent": _agent_to_dict(a)}

    async def _ws_get(self, conn: Any, params: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(params.get("agent_id", ""))
        a = await self.load_agent_for_caller(
            agent_id, caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        return {"agent": _agent_to_dict(a)}

    async def _ws_list(self, conn: Any, params: dict[str, Any]) -> dict[str, Any]:
        admin = self._is_admin(conn)
        if admin and params.get("owner_user_id") is not None:
            agents = await self.list_agents(owner_user_id=str(params["owner_user_id"]))
        elif admin:
            agents = await self.list_agents()
        else:
            agents = await self.list_agents(owner_user_id=self._caller_user_id(conn))
        return {"agents": [_agent_to_dict(a) for a in agents]}

    async def _ws_update(self, conn: Any, params: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(params.get("agent_id", ""))
        await self.load_agent_for_caller(
            agent_id, caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        patch = params.get("patch") or {}
        if not isinstance(patch, dict):
            raise ValueError("patch must be an object")
        a = await self.update_agent(agent_id, patch)
        return {"agent": _agent_to_dict(a)}

    async def _ws_delete(self, conn: Any, params: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(params.get("agent_id", ""))
        await self.load_agent_for_caller(
            agent_id, caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        ok = await self.delete_agent(agent_id)
        return {"deleted": ok}

    async def _ws_set_status(self, conn: Any, params: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(params.get("agent_id", ""))
        status_raw = str(params.get("status", "")).strip()
        try:
            status = AgentStatus(status_raw)
        except ValueError:
            raise ValueError(f"unknown status: {status_raw}") from None
        await self.load_agent_for_caller(
            agent_id, caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        # Route through update_agent so the agent.updated event fires and
        # heartbeat lifecycle is handled in one place.
        updated = await self.update_agent(agent_id, {"status": status.value})
        return {"agent": _agent_to_dict(updated)}

    async def _ws_run_now(self, conn: Any, params: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(params.get("agent_id", ""))
        user_message = params.get("user_message")
        await self.load_agent_for_caller(
            agent_id, caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        run = await self.run_agent_now(agent_id, user_message=user_message)
        return {"run_id": run.id, "status": run.status.value}

    async def _ws_get_defaults(self, conn: Any, params: dict[str, Any]) -> dict[str, Any]:
        return {"defaults": dict(self._defaults)}

    async def _ws_runs_list(self, conn: Any, params: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(params.get("agent_id", ""))
        limit = max(1, int(params.get("limit", 50)))
        await self.load_agent_for_caller(
            agent_id, caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        runs = await self.list_runs(agent_id=agent_id, limit=limit)
        return {"runs": [_run_to_dict(r) for r in runs]}

    async def _ws_commitments_list(
        self, conn: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        agent_id = str(params.get("agent_id", ""))
        include_completed = bool(params.get("include_completed", False))
        await self.load_agent_for_caller(
            agent_id, caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        cs = await self.list_commitments(
            agent_id=agent_id, include_completed=include_completed,
        )
        return {"commitments": [_commitment_to_dict(c) for c in cs]}

    async def _ws_commitments_create(
        self, conn: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        agent_id = str(params.get("agent_id", ""))
        content = str(params.get("content", "")).strip()
        if not content:
            raise ValueError("content is required")
        await self.load_agent_for_caller(
            agent_id, caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        # Resolve due_at: prefer explicit due_at; fall back to due_in_seconds.
        due_at_raw = params.get("due_at")
        due_in_seconds = params.get("due_in_seconds")
        if due_at_raw:
            due_at = datetime.fromisoformat(str(due_at_raw))
        elif due_in_seconds is not None:
            due_at = _now() + timedelta(seconds=int(due_in_seconds))
        else:
            raise ValueError("due_at or due_in_seconds is required")
        c = await self.create_commitment(
            agent_id=agent_id, content=content, due_at=due_at,
        )
        return {"commitment": _commitment_to_dict(c)}

    async def _ws_commitments_complete(
        self, conn: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        commitment_id = str(params.get("commitment_id", ""))
        note = str(params.get("note", ""))
        if self._storage is None:
            raise RuntimeError("not started")
        row = await self._storage.get(_AGENT_COMMITMENTS_COLLECTION, commitment_id)
        if row is None:
            raise KeyError(commitment_id)
        # Authorize via the owning agent.
        await self.load_agent_for_caller(
            row["agent_id"], caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        c = await self.complete_commitment(commitment_id, note=note)
        return {"commitment": _commitment_to_dict(c)}

    async def _ws_memories_list(
        self, conn: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        agent_id = str(params.get("agent_id", ""))
        await self.load_agent_for_caller(
            agent_id, caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        state_raw = params.get("state")
        state = MemoryState(state_raw) if state_raw else None
        kind = params.get("kind")
        kind_str = str(kind) if kind else None
        tags_raw = params.get("tags")
        tags: frozenset[str] | None = None
        if tags_raw:
            tags = frozenset(str(t) for t in tags_raw if str(t).strip())
        q = str(params.get("q", ""))
        limit = int(params.get("limit", 50))
        memories = await self.search_memory(
            agent_id=agent_id, query=q, limit=limit,
            state=state, kind=kind_str, tags=tags,
        )
        return {"memories": [_memory_to_dict(m) for m in memories]}

    async def _ws_memories_set_state(
        self, conn: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        memory_id = str(params.get("memory_id", ""))
        state_raw = str(params.get("state", ""))
        if self._storage is None:
            raise RuntimeError("not started")
        row = await self._storage.get(_AGENT_MEMORIES_COLLECTION, memory_id)
        if row is None:
            raise KeyError(memory_id)
        await self.load_agent_for_caller(
            row["agent_id"], caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        updated = await self.promote_memory(
            memory_id=memory_id,
            score=float(row.get("score", 0.0)),
            state=MemoryState(state_raw),
        )
        return {"memory": _memory_to_dict(updated)}

    async def _ws_tools_list_available(
        self, conn: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        """Enumerate tools the caller could grant to an agent.

        Delegates to the bound AIToolDiscoveryProvider — same path used
        by the MCP server tools-preview endpoint. Flattens the discovery
        result into a list of ``{name, description, required_role,
        provider}`` objects sorted by name.
        """
        from gilbert.interfaces.auth import UserContext

        if self._tool_discovery is None:
            raise RuntimeError("not started")
        # Ensure caller is authenticated; the tools list is non-sensitive
        # but we keep the same gate as every other handler.
        self._caller_user_id(conn)
        user_ctx = getattr(conn, "user_ctx", None) or UserContext.GUEST
        discovered = self._tool_discovery.discover_tools(user_ctx=user_ctx)
        tools: list[dict[str, Any]] = []
        for name, entry in discovered.items():
            # discover_tools returns dict[str, tuple[ToolProvider, ToolDefinition]].
            if isinstance(entry, tuple) and len(entry) == 2:
                provider, tool_def = entry
                provider_name = getattr(provider, "tool_provider_name", "")
            else:
                tool_def = entry
                provider_name = ""
            tools.append({
                "name": getattr(tool_def, "name", name),
                "description": getattr(tool_def, "description", ""),
                "required_role": getattr(tool_def, "required_role", "user"),
                "provider": provider_name,
            })
        tools.sort(key=lambda t: t["name"])
        return {"tools": tools}

    # ── WS handlers — goals (Phase 4) ───────────────────────────────

    async def _load_goal_for_caller(
        self, goal_id: str, conn: Any,
    ) -> Goal:
        """Fetch a goal and enforce caller ownership.

        Same pattern as ``load_agent_for_caller`` but for goals. Admins
        bypass the owner check.
        """
        goal = await self.get_goal(goal_id)
        if goal is None:
            raise KeyError(goal_id)
        if not self._is_admin(conn) and goal.owner_user_id != self._caller_user_id(conn):
            raise PermissionError(
                f"goal {goal_id} not accessible to user {self._caller_user_id(conn)}"
            )
        return goal

    async def _ws_goals_create(
        self, conn: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        owner = self._caller_user_id(conn)
        name = str(params.get("name", "")).strip()
        if not name:
            raise ValueError("name is required")
        description = str(params.get("description", ""))
        cost_cap_raw = params.get("cost_cap_usd")
        cost_cap = float(cost_cap_raw) if cost_cap_raw is not None else None

        # ``assign_to`` may be a list of {agent_name, role} or strings.
        assign_to: list[tuple[str, AssignmentRole]] = []
        raw = params.get("assign_to") or []
        if not isinstance(raw, list):
            raise ValueError("assign_to must be an array")
        for entry in raw:
            if isinstance(entry, str):
                assign_to.append((entry.strip(), AssignmentRole.COLLABORATOR))
            elif isinstance(entry, dict):
                aname = str(entry.get("agent_name", "")).strip()
                if not aname:
                    raise ValueError("assign_to entry missing agent_name")
                role_raw = entry.get("role", "collaborator")
                try:
                    role = AssignmentRole(str(role_raw).lower().strip())
                except ValueError as exc:
                    raise ValueError(
                        f"invalid role {role_raw!r} in assign_to"
                    ) from exc
                assign_to.append((aname, role))
            else:
                raise ValueError("assign_to entries must be strings or {agent_name, role} objects")

        g = await self.create_goal(
            owner_user_id=owner,
            name=name,
            description=description,
            cost_cap_usd=cost_cap,
            assign_to=assign_to,
            assigned_by=f"user:{owner}",
        )
        return {"goal": _goal_to_dict(g)}

    async def _ws_goals_list(
        self, conn: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        admin = self._is_admin(conn)
        owner_param = params.get("owner_user_id")
        if admin and owner_param is not None:
            goals = await self.list_goals(owner_user_id=str(owner_param))
        elif admin:
            goals = await self.list_goals()
        else:
            goals = await self.list_goals(owner_user_id=self._caller_user_id(conn))
        return {"goals": [_goal_to_dict(g) for g in goals]}

    async def _ws_goals_get(
        self, conn: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        goal_id = str(params.get("goal_id", ""))
        g = await self._load_goal_for_caller(goal_id, conn)
        return {"goal": _goal_to_dict(g)}

    async def _ws_goals_update_status(
        self, conn: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        goal_id = str(params.get("goal_id", ""))
        await self._load_goal_for_caller(goal_id, conn)
        status_raw = str(params.get("status", "")).strip()
        try:
            status = GoalStatus(status_raw)
        except ValueError as exc:
            raise ValueError(f"unknown status: {status_raw}") from exc
        g = await self.update_goal_status(goal_id, status)
        return {"goal": _goal_to_dict(g)}

    async def _ws_goals_assignments_list(
        self, conn: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        goal_id_raw = params.get("goal_id")
        agent_id_raw = params.get("agent_id")
        active_only = bool(params.get("active_only", True))
        if goal_id_raw is not None:
            await self._load_goal_for_caller(str(goal_id_raw), conn)
        elif agent_id_raw is not None:
            # Auth via the agent (not the goal).
            await self.load_agent_for_caller(
                str(agent_id_raw),
                caller_user_id=self._caller_user_id(conn),
                admin=self._is_admin(conn),
            )
        else:
            raise ValueError("goal_id or agent_id required")
        asgns = await self.list_assignments(
            goal_id=str(goal_id_raw) if goal_id_raw else None,
            agent_id=str(agent_id_raw) if agent_id_raw else None,
            active_only=active_only,
        )
        return {"assignments": [_goal_assignment_to_dict(a) for a in asgns]}

    async def _ws_goals_assignments_add(
        self, conn: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        goal_id = str(params.get("goal_id", ""))
        agent_id = str(params.get("agent_id", ""))
        role_raw = str(params.get("role", "")).strip()
        try:
            role = AssignmentRole(role_raw)
        except ValueError as exc:
            raise ValueError(f"unknown role: {role_raw}") from exc
        await self._load_goal_for_caller(goal_id, conn)
        # Confirm the agent is owned by the same user (no cross-owner).
        await self.load_agent_for_caller(
            agent_id,
            caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        ga = await self.assign_agent_to_goal(
            goal_id=goal_id,
            agent_id=agent_id,
            role=role,
            assigned_by=f"user:{self._caller_user_id(conn)}",
        )
        return {"assignment": _goal_assignment_to_dict(ga)}

    async def _ws_goals_assignments_remove(
        self, conn: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        goal_id = str(params.get("goal_id", ""))
        agent_id = str(params.get("agent_id", ""))
        await self._load_goal_for_caller(goal_id, conn)
        ga = await self.unassign_agent_from_goal(goal_id=goal_id, agent_id=agent_id)
        return {"assignment": _goal_assignment_to_dict(ga)}

    async def _ws_goals_assignments_handoff(
        self, conn: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        goal_id = str(params.get("goal_id", ""))
        from_agent_id = str(params.get("from_agent_id", ""))
        to_agent_id = str(params.get("to_agent_id", ""))
        note = str(params.get("note", ""))
        new_role_raw = str(params.get("new_role_for_from", "")).strip()
        if new_role_raw:
            try:
                new_role_for_from = AssignmentRole(new_role_raw)
            except ValueError:
                raise ValueError(f"unknown role: {new_role_raw}") from None
        else:
            new_role_for_from = AssignmentRole.COLLABORATOR
        await self._load_goal_for_caller(goal_id, conn)
        # Both agents must belong to the same owner; load_agent_for_caller
        # enforces that.
        await self.load_agent_for_caller(
            from_agent_id,
            caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        await self.load_agent_for_caller(
            to_agent_id,
            caller_user_id=self._caller_user_id(conn),
            admin=self._is_admin(conn),
        )
        from_a, to_a = await self.handoff_goal(
            goal_id=goal_id,
            from_agent_id=from_agent_id,
            to_agent_id=to_agent_id,
            new_role_for_from=new_role_for_from,
            note=note,
        )
        return {
            "from_assignment": _goal_assignment_to_dict(from_a),
            "to_assignment": _goal_assignment_to_dict(to_a),
        }

    async def _ws_goals_summary(
        self, conn: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        goal_id = str(params.get("goal_id", ""))
        goal = await self._load_goal_for_caller(goal_id, conn)
        asgns = await self.list_assignments(goal_id=goal_id, active_only=True)
        names: dict[str, str] = {}
        for a in asgns:
            ag = await self.get_agent(a.agent_id)
            if ag is not None:
                names[a.agent_id] = ag.name
        recent = await self._recent_war_room_posts(goal_id, limit=10)
        unsat = await self.list_goal_dependencies(
            dependent_goal_id=goal_id, satisfied=False,
        )
        return {
            "goal": _goal_to_dict(goal),
            "assignees": [
                {
                    "agent_id": a.agent_id,
                    "agent_name": names.get(a.agent_id, ""),
                    "role": a.role.value,
                }
                for a in asgns
            ],
            "recent_posts": recent,
            "is_dependency_blocked": len(unsat) > 0,
        }

    async def _ws_goals_posts_list(
        self, conn: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        goal_id = str(params.get("goal_id", ""))
        limit = max(1, int(params.get("limit", 50)))
        await self._load_goal_for_caller(goal_id, conn)
        posts = await self._recent_war_room_posts(goal_id, limit=limit)
        return {"posts": posts}

    # ── WS handlers — deliverables + dependencies (Phase 5) ─────────

    async def _load_deliverable_for_caller(
        self, deliverable_id: str, conn: Any,
    ) -> Deliverable:
        """Fetch a deliverable, enforcing caller ownership of its goal."""
        d = await self.get_deliverable(deliverable_id)
        if d is None:
            raise KeyError(deliverable_id)
        # Ownership flows through the goal; ``_load_goal_for_caller``
        # enforces admin / owner check and raises PermissionError on
        # cross-user reach.
        await self._load_goal_for_caller(d.goal_id, conn)
        return d

    async def _ws_deliverables_list(
        self, conn: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        goal_id_raw = params.get("goal_id")
        state_raw = params.get("state")
        state: DeliverableState | None = None
        if state_raw:
            try:
                state = DeliverableState(str(state_raw).lower().strip())
            except ValueError as exc:
                raise ValueError(f"unknown state: {state_raw}") from exc
        if goal_id_raw is None:
            raise ValueError("goal_id is required")
        await self._load_goal_for_caller(str(goal_id_raw), conn)
        ds = await self.list_deliverables(
            goal_id=str(goal_id_raw), state=state,
        )
        return {"deliverables": [_deliverable_to_dict(d) for d in ds]}

    async def _ws_deliverables_create(
        self, conn: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        goal_id = str(params.get("goal_id", "")).strip()
        name = str(params.get("name", "")).strip()
        kind = str(params.get("kind", "")).strip()
        content_ref = str(params.get("content_ref", ""))
        produced_by_agent_id = str(params.get("produced_by_agent_id", "")).strip()
        if not goal_id or not name or not kind:
            raise ValueError("goal_id, name, kind are required")
        await self._load_goal_for_caller(goal_id, conn)
        # If produced_by_agent_id is provided, confirm same-owner.
        if produced_by_agent_id:
            await self.load_agent_for_caller(
                produced_by_agent_id,
                caller_user_id=self._caller_user_id(conn),
                admin=self._is_admin(conn),
            )
        d = await self.create_deliverable(
            goal_id=goal_id,
            name=name,
            kind=kind,
            produced_by_agent_id=produced_by_agent_id,
            content_ref=content_ref,
        )
        return {"deliverable": _deliverable_to_dict(d)}

    async def _ws_deliverables_finalize(
        self, conn: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        deliverable_id = str(params.get("deliverable_id", "")).strip()
        if not deliverable_id:
            raise ValueError("deliverable_id is required")
        await self._load_deliverable_for_caller(deliverable_id, conn)
        d = await self.finalize_deliverable(deliverable_id)
        return {"deliverable": _deliverable_to_dict(d)}

    async def _ws_deliverables_supersede(
        self, conn: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        deliverable_id = str(params.get("deliverable_id", "")).strip()
        new_content_ref = str(params.get("new_content_ref", "")).strip()
        finalize = bool(params.get("finalize", False))
        if not deliverable_id or not new_content_ref:
            raise ValueError("deliverable_id and new_content_ref required")
        await self._load_deliverable_for_caller(deliverable_id, conn)
        obs, new = await self.supersede_deliverable(
            deliverable_id,
            new_content_ref=new_content_ref,
            finalize=finalize,
        )
        return {
            "obsoleted": _deliverable_to_dict(obs),
            "new": _deliverable_to_dict(new),
        }

    async def _ws_goal_dependencies_list(
        self, conn: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        dep_id_raw = params.get("dependent_goal_id")
        src_id_raw = params.get("source_goal_id")
        sat_raw = params.get("satisfied")
        if dep_id_raw is None and src_id_raw is None:
            raise ValueError(
                "dependent_goal_id or source_goal_id required"
            )
        if dep_id_raw is not None:
            await self._load_goal_for_caller(str(dep_id_raw), conn)
        if src_id_raw is not None:
            await self._load_goal_for_caller(str(src_id_raw), conn)
        satisfied: bool | None = None
        if sat_raw is not None:
            satisfied = bool(sat_raw)
        deps = await self.list_goal_dependencies(
            dependent_goal_id=str(dep_id_raw) if dep_id_raw else None,
            source_goal_id=str(src_id_raw) if src_id_raw else None,
            satisfied=satisfied,
        )
        return {"dependencies": [_dependency_to_dict(d) for d in deps]}

    async def _ws_goal_dependencies_add(
        self, conn: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        dep_id = str(params.get("dependent_goal_id", "")).strip()
        src_id = str(params.get("source_goal_id", "")).strip()
        name = str(params.get("required_deliverable_name", "")).strip()
        if not dep_id or not src_id or not name:
            raise ValueError(
                "dependent_goal_id, source_goal_id, "
                "required_deliverable_name required"
            )
        await self._load_goal_for_caller(dep_id, conn)
        await self._load_goal_for_caller(src_id, conn)
        dep = await self.add_goal_dependency(
            dependent_goal_id=dep_id,
            source_goal_id=src_id,
            required_deliverable_name=name,
        )
        return {"dependency": _dependency_to_dict(dep)}

    async def _ws_goal_dependencies_remove(
        self, conn: Any, params: dict[str, Any],
    ) -> dict[str, Any]:
        dependency_id = str(params.get("dependency_id", "")).strip()
        if not dependency_id:
            raise ValueError("dependency_id is required")
        if self._storage is None:
            raise RuntimeError("not started")
        row = await self._storage.get(_DEPENDENCIES_COLLECTION, dependency_id)
        if row is None:
            raise KeyError(dependency_id)
        # Ownership through the dependent goal.
        await self._load_goal_for_caller(row["dependent_goal_id"], conn)
        await self.remove_goal_dependency(dependency_id)
        return {"removed": True}

    # ── Event publishing helper ─────────────────────────────────────

    async def _publish(self, event_type: str, data: dict[str, Any]) -> None:
        """Publish an event on the bus, no-op if the bus is not bound."""
        if self._event_bus is None:
            return
        await self._event_bus.publish(Event(event_type=event_type, data=data, source="agent"))
