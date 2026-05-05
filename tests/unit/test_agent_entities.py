"""Smoke tests for Agent entity dataclasses — round-trip + enum coverage."""

from __future__ import annotations

from datetime import UTC, datetime

from gilbert.interfaces.agent import (  # noqa: F401
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
        tools_allowed=None,
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
