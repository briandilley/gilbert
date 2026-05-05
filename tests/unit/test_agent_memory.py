"""Unit tests for AgentService.save_memory / search_memory / promote_memory (Task 7).

Covers:
- save_memory persists and returns a well-formed AgentMemory.
- search_memory returns matching entries (case-insensitive substring).
- promote_memory updates state and score.
- Memories are isolated per agent.
"""

from __future__ import annotations

from typing import Any

import pytest

from gilbert.interfaces.agent import MemoryState


async def test_save_memory_persists(started_agent_service: Any) -> None:
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="memo-bot")
    mem = await svc.save_memory(
        agent_id=a.id,
        content="user prefers TypeScript",
        kind="preference",
        tags={"lang"},
    )
    assert mem.id
    assert mem.state is MemoryState.SHORT_TERM  # default
    assert mem.kind == "preference"
    assert "lang" in mem.tags


async def test_search_memory_returns_matches(started_agent_service: Any) -> None:
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")
    await svc.save_memory(agent_id=a.id, content="user likes hot tea")
    await svc.save_memory(agent_id=a.id, content="user dislikes cilantro")

    out = await svc.search_memory(agent_id=a.id, query="tea")
    assert any("tea" in m.content for m in out)


async def test_promote_memory_changes_state(started_agent_service: Any) -> None:
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")
    mem = await svc.save_memory(agent_id=a.id, content="durable fact")
    promoted = await svc.promote_memory(memory_id=mem.id, score=0.95)
    assert promoted.state is MemoryState.LONG_TERM
    assert promoted.score == pytest.approx(0.95)


async def test_memory_isolated_per_agent(started_agent_service: Any) -> None:
    svc = started_agent_service
    a1 = await svc.create_agent(owner_user_id="usr_1", name="a1")
    a2 = await svc.create_agent(owner_user_id="usr_1", name="a2")
    await svc.save_memory(agent_id=a1.id, content="agent 1 fact")
    a2_mems = await svc.search_memory(agent_id=a2.id, query="agent")
    assert a2_mems == []  # agent 2 doesn't see agent 1's memories
