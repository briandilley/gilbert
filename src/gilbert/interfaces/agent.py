"""Agent interface — Goal, Run entity dataclasses and AgentProvider protocol."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable


class GoalStatus(StrEnum):
    """Goal lifecycle states."""

    ENABLED = "enabled"
    """Active and runnable."""

    DISABLED = "disabled"
    """Paused; no new runs may be started."""

    COMPLETED = "completed"
    """Terminal: agent declared the goal done via complete_goal tool."""


class RunStatus(StrEnum):
    """Run lifecycle states."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Goal:
    """A persistent autonomous-agent task description."""

    id: str
    owner_user_id: str
    name: str
    instruction: str
    profile_id: str
    status: GoalStatus
    created_at: datetime
    updated_at: datetime
    last_run_at: datetime | None = None
    last_run_status: RunStatus | None = None
    run_count: int = 0
    completed_at: datetime | None = None
    completed_reason: str | None = None


@dataclass
class Run:
    """One execution of a goal."""

    id: str
    goal_id: str
    triggered_by: str
    started_at: datetime
    status: RunStatus
    conversation_id: str = ""
    ended_at: datetime | None = None
    final_message_text: str | None = None
    rounds_used: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    error: str | None = None
    complete_goal_called: bool = False
    complete_reason: str | None = None


@runtime_checkable
class AgentProvider(Protocol):
    """Capability protocol for the autonomous-agent service."""

    async def create_goal(
        self,
        *,
        owner_user_id: str,
        name: str,
        instruction: str,
        profile_id: str,
    ) -> Goal: ...

    async def get_goal(self, goal_id: str) -> Goal | None: ...

    async def list_goals(
        self,
        *,
        owner_user_id: str | None = None,
    ) -> list[Goal]: ...

    async def run_goal_now(self, goal_id: str) -> Run: ...

    async def declare_goal_complete(
        self,
        goal_id: str,
        run_id: str,
        reason: str,
    ) -> bool: ...
