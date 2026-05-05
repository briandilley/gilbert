"""Smoke tests for Agent entity dataclasses — round-trip + enum coverage."""

from __future__ import annotations

from datetime import UTC, datetime

from gilbert.interfaces.agent import (
    Agent,
    AgentMemory,
    AgentProvider,
    AgentStatus,
    AgentTrigger,
    Commitment,
    InboxSignal,
    MemoryState,
    Run,
    RunStatus,
)


def test_agent_dataclass_round_trip() -> None:
    a = Agent(
        id="ag_1",
        owner_user_id="usr_1",
        name="research-bot",
        display_name="Ballsagna Bot",
        role_label="Research Bot",
        persona="curious and methodical",
        system_prompt="follow up on every lead",
        procedural_rules="always cite sources",
        profile_id="standard",
        conversation_id="",
        status=AgentStatus.ENABLED,
        avatar_kind="emoji",
        avatar_value="🔬",
        lifetime_cost_usd=0.0,
        cost_cap_usd=None,
        tools_include=None,
        tools_exclude=None,
        heartbeat_enabled=True,
        heartbeat_interval_s=1800,
        heartbeat_checklist="check the news",
        dream_enabled=False,
        dream_quiet_hours="22:00-06:00",
        dream_probability=0.1,
        dream_max_per_night=3,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    assert a.id == "ag_1"
    assert a.status is AgentStatus.ENABLED


def test_memory_state_enum_values() -> None:
    assert MemoryState.SHORT_TERM.value == "short_term"
    assert MemoryState.LONG_TERM.value == "long_term"


def test_run_status_terminal_states() -> None:
    terminals = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.TIMED_OUT}
    for status in RunStatus:
        if status is RunStatus.RUNNING:
            assert status not in terminals
        else:
            assert status in terminals


def test_agent_provider_is_runtime_checkable() -> None:
    """A test fake satisfies AgentProvider when it implements the methods."""

    class FakeAgentService:
        async def create_agent(self, **kwargs):
            return None

        async def get_agent(self, agent_id):
            return None

        async def list_agents(self, **kwargs):
            return []

        async def run_agent_now(self, agent_id, **kwargs):
            return None

        async def load_agent_for_caller(self, agent_id, **kwargs):
            return None

        async def set_agent_avatar(self, agent_id, **kwargs):
            return None

        async def create_goal(self, **kwargs):
            return None

        async def get_goal(self, goal_id):
            return None

        async def list_goals(self, **kwargs):
            return []

        async def update_goal_status(self, goal_id, status):
            return None

        async def delete_goal(self, goal_id):
            return True

        async def list_assignments(self, **kwargs):
            return []

        async def assign_agent_to_goal(self, **kwargs):
            return None

        async def unassign_agent_from_goal(self, **kwargs):
            return None

        async def handoff_goal(self, **kwargs):
            return None

        async def create_deliverable(self, **kwargs):
            return None

        async def get_deliverable(self, deliverable_id):
            return None

        async def list_deliverables(self, **kwargs):
            return []

        async def finalize_deliverable(self, deliverable_id):
            return None

        async def supersede_deliverable(self, deliverable_id, **kwargs):
            return None

        async def add_goal_dependency(self, **kwargs):
            return None

        async def remove_goal_dependency(self, dependency_id):
            return None

        async def list_goal_dependencies(self, **kwargs):
            return []

    assert isinstance(FakeAgentService(), AgentProvider)


def test_inbox_signal_dataclass_round_trip() -> None:
    s = InboxSignal(
        id="sig_1",
        agent_id="ag_1",
        signal_kind="inbox",
        body="hello",
        sender_kind="user",
        sender_id="usr_1",
        sender_name="brian",
        source_conv_id="conv_1",
        source_message_id="msg_1",
        delegation_id="",
        metadata={},
        priority="normal",
        created_at=datetime.now(UTC),
        processed_at=None,
    )
    assert s.processed_at is None


def test_agent_memory_dataclass_round_trip() -> None:
    m = AgentMemory(
        id="mem_1",
        agent_id="ag_1",
        content="the sky is blue",
        state=MemoryState.SHORT_TERM,
        kind="fact",
        tags=frozenset({"test"}),
        score=0.5,
        created_at=datetime.now(UTC),
        last_used_at=None,
    )
    assert m.state is MemoryState.SHORT_TERM
    assert "test" in m.tags


def test_agent_trigger_dataclass_round_trip() -> None:
    t = AgentTrigger(
        id="t1",
        agent_id="ag_1",
        trigger_type="heartbeat",
        trigger_config={"interval_s": 1800},
        enabled=True,
    )
    assert t.enabled is True
    assert t.trigger_config["interval_s"] == 1800


def test_commitment_dataclass_round_trip() -> None:
    c = Commitment(
        id="c1",
        agent_id="ag_1",
        content="check sonarr",
        due_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        completed_at=None,
        completion_note="",
    )
    assert c.completed_at is None
    assert c.completion_note == ""


def test_run_dataclass_round_trip_default_factory() -> None:
    run = Run(
        id="run_1",
        agent_id="ag_1",
        triggered_by="heartbeat",
        trigger_context={},
        started_at=datetime.now(UTC),
        status=RunStatus.RUNNING,
        conversation_id="conv_1",
        delegation_id="",
        ended_at=None,
        final_message_text=None,
        rounds_used=0,
        tokens_in=0,
        tokens_out=0,
        cost_usd=0.0,
        error=None,
        awaiting_user_input=False,
        pending_question=None,
    )
    assert run.pending_actions == []
    assert run.status is RunStatus.RUNNING
