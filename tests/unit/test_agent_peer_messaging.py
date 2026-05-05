"""Phase 2 — Peer messaging tools (agent_list, agent_send_message, agent_delegate)."""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from gilbert.core.services.agent import (
    _AGENT_INBOX_SIGNALS_COLLECTION,
    _AGENT_RUNS_COLLECTION,
)
from gilbert.interfaces.storage import Filter, FilterOp, Query

# ── agent_list ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_list_returns_peers_owner_scoped(started_agent_service):
    """agent_list returns peers under the same owner, excludes self,
    excludes other owners' agents."""
    svc = started_agent_service
    a1 = await svc.create_agent(owner_user_id="usr_a", name="a1", role_label="Researcher")
    a2 = await svc.create_agent(owner_user_id="usr_a", name="a2", role_label="Writer")
    a3 = await svc.create_agent(owner_user_id="usr_a", name="a3", role_label="Editor")
    await svc.create_agent(owner_user_id="usr_b", name="b1", role_label="Other")

    raw = await svc._exec_agent_list({"_agent_id": a1.id})
    out = json.loads(raw)
    names = {p["name"] for p in out}
    assert names == {"a2", "a3"}
    # No B-owner leak.
    assert "b1" not in names
    # No self.
    assert "a1" not in names
    # Schema sanity.
    by_name = {p["name"]: p for p in out}
    assert by_name["a2"]["role_label"] == "Writer"
    assert by_name["a2"]["status"] == "enabled"
    assert "conversation_id" in by_name["a2"]

    # _agent_id presence is required.
    assert "error" in await svc._exec_agent_list({})
    # Bogus _agent_id returns an error too.
    assert "error" in await svc._exec_agent_list({"_agent_id": "ag_missing"})

    # Cross-owner sanity: B's agents only see their own bucket (which
    # is empty — no peers exist for usr_b).
    raw_b = await svc._exec_agent_list({
        "_agent_id": (await svc.list_agents(owner_user_id="usr_b"))[0].id
    })
    assert json.loads(raw_b) == []
    # Touch unused fixture so flake8 doesn't complain.
    assert a2.id and a3.id


# ── agent_send_message ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_send_message_signals_target(started_agent_service):
    """A1 → A2 produces an InboxSignal row owned by A2 with sender_id=A1.id."""
    svc = started_agent_service
    a1 = await svc.create_agent(owner_user_id="usr_a", name="a1")
    a2 = await svc.create_agent(owner_user_id="usr_a", name="a2")

    # Hold A2 busy so the signal queues rather than firing a run.
    svc._running_agents.add(a2.id)
    try:
        result = await svc._exec_agent_send_message({
            "_agent_id": a1.id,
            "target_name": "a2",
            "body": "hello",
        })
    finally:
        svc._running_agents.discard(a2.id)
    assert result == "sent to a2"

    rows = await svc._storage.query(
        Query(
            collection=_AGENT_INBOX_SIGNALS_COLLECTION,
            filters=[Filter(field="agent_id", op=FilterOp.EQ, value=a2.id)],
        )
    )
    assert len(rows) == 1
    assert rows[0]["sender_id"] == a1.id
    assert rows[0]["body"] == "hello"
    assert rows[0]["sender_name"] == "a1"
    assert rows[0]["sender_kind"] == "agent"


@pytest.mark.asyncio
async def test_agent_send_message_blocks_cross_owner(started_agent_service):
    """A1 (usr_a) → B1 (usr_b) is rejected; no signal is created."""
    svc = started_agent_service
    a1 = await svc.create_agent(owner_user_id="usr_a", name="a1")
    b1 = await svc.create_agent(owner_user_id="usr_b", name="b1")

    result = await svc._exec_agent_send_message({
        "_agent_id": a1.id,
        "target_name": "b1",
        "body": "hi from across the fence",
    })
    assert result.startswith("error:")
    assert "no peer named" in result.lower()

    rows = await svc._storage.query(
        Query(
            collection=_AGENT_INBOX_SIGNALS_COLLECTION,
            filters=[Filter(field="agent_id", op=FilterOp.EQ, value=b1.id)],
        )
    )
    assert rows == []


@pytest.mark.asyncio
async def test_agent_send_message_self_rejected(started_agent_service):
    """A1 → A1 returns an error and does not create a signal."""
    svc = started_agent_service
    a1 = await svc.create_agent(owner_user_id="usr_a", name="a1")

    result = await svc._exec_agent_send_message({
        "_agent_id": a1.id,
        "target_name": "a1",
        "body": "talking to myself",
    })
    assert result.startswith("error:")
    assert "yourself" in result

    rows = await svc._storage.query(
        Query(
            collection=_AGENT_INBOX_SIGNALS_COLLECTION,
            filters=[Filter(field="agent_id", op=FilterOp.EQ, value=a1.id)],
        )
    )
    assert rows == []


@pytest.mark.asyncio
async def test_agent_send_message_idle_peer_fires_run(started_agent_service):
    """Sending to an idle peer schedules a run for the target agent."""
    svc = started_agent_service
    a1 = await svc.create_agent(owner_user_id="usr_a", name="a1")
    a2 = await svc.create_agent(owner_user_id="usr_a", name="a2")

    # Pre-condition: A2 is not running.
    assert a2.id not in svc._running_agents

    result = await svc._exec_agent_send_message({
        "_agent_id": a1.id,
        "target_name": "a2",
        "body": "wake up",
    })
    assert result == "sent to a2"

    # _signal_agent spawned an asyncio task for A2's run; wait for it
    # to finish so sqlite isn't torn down mid-write.
    deadline = asyncio.get_running_loop().time() + 2.0
    rows: list[dict] = []
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
        rows = await svc._storage.query(
            Query(
                collection=_AGENT_RUNS_COLLECTION,
                filters=[Filter(field="agent_id", op=FilterOp.EQ, value=a2.id)],
            )
        )
        if rows and any(r.get("status") == "completed" for r in rows):
            break
    else:
        pytest.fail("expected a completed run row for a2 after signal-fired wake-up")
    assert any(r.get("triggered_by") == "inbox" for r in rows)
    assert a2.id not in svc._running_agents


# ── agent_delegate ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_delegate_round_trip(started_agent_service):
    """A1 delegates to A2; A2's final assistant message returns to A1."""
    svc = started_agent_service
    a1 = await svc.create_agent(owner_user_id="usr_a", name="a1")
    a2 = await svc.create_agent(owner_user_id="usr_a", name="a2")

    svc._ai.response_text = "hello back"

    result = await asyncio.wait_for(
        svc._exec_agent_delegate({
            "_agent_id": a1.id,
            "target_name": "a2",
            "instruction": "say hi",
            "max_wait_s": 5,
        }),
        timeout=5.0,
    )
    assert result == "hello back"

    # Sanity: a2's run was triggered_by="delegation" and carries the
    # delegation_id on its run row.
    runs = await svc._storage.query(
        Query(
            collection=_AGENT_RUNS_COLLECTION,
            filters=[Filter(field="agent_id", op=FilterOp.EQ, value=a2.id)],
        )
    )
    assert any(r.get("triggered_by") == "delegation" for r in runs)
    assert any(r.get("delegation_id") for r in runs)
    # And the pending-delegations dict was cleaned up.
    assert svc._pending_delegations == {}


@pytest.mark.asyncio
async def test_agent_delegate_cycle_rejected(started_agent_service):
    """A→B→A is rejected up-front by the chain check.

    Setup: a1 originally delegated to a2. While a2 is processing, it
    tries to delegate back to a1 — i.e., the call carries a chain of
    ``[a1.id]`` and target=a1. After the handler appends the caller
    (a2.id) to chain, target.id ∈ chain becomes ``a1.id ∈ [a1, a2]``
    → cycle.
    """
    svc = started_agent_service
    a1 = await svc.create_agent(owner_user_id="usr_a", name="a1")
    a2 = await svc.create_agent(owner_user_id="usr_a", name="a2")

    result = await svc._exec_agent_delegate({
        "_agent_id": a2.id,
        "target_name": "a1",
        "instruction": "back to you",
        "_delegation_chain": [a1.id],
    })
    assert result.startswith("error:")
    assert "cycle" in result


@pytest.mark.asyncio
async def test_agent_delegate_depth_cap(started_agent_service):
    """Chain length 4 + caller pushes past the cap of 5."""
    svc = started_agent_service
    a1 = await svc.create_agent(owner_user_id="usr_a", name="a1")
    # a2 must exist for _load_peer_by_name resolution; we don't need
    # the handle directly since the depth check fires before any signal
    # is sent.
    await svc.create_agent(owner_user_id="usr_a", name="a2")

    # 4 distinct prior delegators in the chain; appending the caller
    # makes len(chain) == 5 ≥ cap, which should reject.
    fake_chain = ["ag_p1", "ag_p2", "ag_p3", "ag_p4"]
    result = await svc._exec_agent_delegate({
        "_agent_id": a1.id,
        "target_name": "a2",
        "instruction": "do the thing",
        "_delegation_chain": fake_chain,
    })
    assert result.startswith("error:")
    assert "depth cap" in result


@pytest.mark.asyncio
async def test_agent_delegate_timeout(started_agent_service):
    """max_wait_s=1 with a slow target → timeout error string."""
    svc = started_agent_service
    a1 = await svc.create_agent(owner_user_id="usr_a", name="a1")
    a2 = await svc.create_agent(owner_user_id="usr_a", name="a2")

    # Make A2's chat hang for longer than max_wait_s.
    svc._ai.chat_delay_s = 5.0
    svc._ai.response_text = "ignored"

    result = await asyncio.wait_for(
        svc._exec_agent_delegate({
            "_agent_id": a1.id,
            "target_name": "a2",
            "instruction": "slow work",
            "max_wait_s": 1,
        }),
        timeout=3.0,
    )
    assert result.startswith("error:")
    assert "timed out" in result.lower()
    # The Future was abandoned; the dict entry is cleaned by the finally.
    assert svc._pending_delegations == {}

    # Wait for A2's spawned run to finish so sqlite isn't torn down
    # mid-write.
    deadline = asyncio.get_running_loop().time() + 8.0
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
        runs = await svc._storage.query(
            Query(
                collection=_AGENT_RUNS_COLLECTION,
                filters=[Filter(field="agent_id", op=FilterOp.EQ, value=a2.id)],
            )
        )
        if runs and any(r.get("status") in {"completed", "failed"} for r in runs):
            break
    # Sanity: a2 idle now.
    assert a2.id not in svc._running_agents


@pytest.mark.asyncio
async def test_agent_delegate_target_failure(started_agent_service):
    """Target run raises → caller receives an error string."""
    svc = started_agent_service
    a1 = await svc.create_agent(owner_user_id="usr_a", name="a1")
    await svc.create_agent(owner_user_id="usr_a", name="a2")

    # Force a2's chat to raise — the run goes to FAILED, the future's
    # set_exception path fires, and _exec_agent_delegate's exception
    # arm returns an error string (does NOT propagate).
    svc._ai.raise_on_chat = RuntimeError("kaboom")

    result = await asyncio.wait_for(
        svc._exec_agent_delegate({
            "_agent_id": a1.id,
            "target_name": "a2",
            "instruction": "will fail",
            "max_wait_s": 5,
        }),
        timeout=5.0,
    )
    assert result.startswith("error:")
    assert "target run" in result
    assert svc._pending_delegations == {}


@pytest.mark.asyncio
async def test_agent_delegate_rejects_self(started_agent_service):
    """Delegating to self is rejected before the future is created."""
    svc = started_agent_service
    a1 = await svc.create_agent(owner_user_id="usr_a", name="a1")

    result = await svc._exec_agent_delegate({
        "_agent_id": a1.id,
        "target_name": "a1",
        "instruction": "do it yourself",
    })
    assert result.startswith("error:")
    assert "yourself" in result
    assert svc._pending_delegations == {}


# ── Phase 3 — priority + urgent_pending + mid-round interrupt wiring ─


@pytest.mark.asyncio
async def test_agent_send_message_urgent_sets_pending_flag(started_agent_service):
    """priority='urgent' on send_message flips _urgent_pending[target]."""
    svc = started_agent_service
    a1 = await svc.create_agent(owner_user_id="usr_a", name="a1")
    a2 = await svc.create_agent(owner_user_id="usr_a", name="a2")

    # Hold A2 busy so _signal_agent does NOT fire a fresh run that would
    # immediately drain the inbox (and clear the flag).
    svc._running_agents.add(a2.id)
    try:
        result = await svc._exec_agent_send_message({
            "_agent_id": a1.id,
            "target_name": "a2",
            "body": "drop everything",
            "priority": "urgent",
        })
    finally:
        # Don't release until after the assertion — the asyncio task that
        # would fire on idle would race the test otherwise.
        assert result == "sent to a2"
        assert svc._urgent_pending.get(a2.id) is True
        svc._running_agents.discard(a2.id)


@pytest.mark.asyncio
async def test_agent_send_message_normal_does_not_set_pending(started_agent_service):
    """Default (normal) priority leaves _urgent_pending alone."""
    svc = started_agent_service
    a1 = await svc.create_agent(owner_user_id="usr_a", name="a1")
    a2 = await svc.create_agent(owner_user_id="usr_a", name="a2")

    svc._running_agents.add(a2.id)
    try:
        result = await svc._exec_agent_send_message({
            "_agent_id": a1.id,
            "target_name": "a2",
            "body": "no rush",
            # priority omitted → default "normal"
        })
    finally:
        assert result == "sent to a2"
        # Flag should be absent or False — both are equivalent here.
        assert not svc._urgent_pending.get(a2.id, False)
        svc._running_agents.discard(a2.id)


@pytest.mark.asyncio
async def test_agent_delegate_defaults_to_urgent(started_agent_service):
    """No priority arg → delegate signals urgent on the target."""
    svc = started_agent_service
    a1 = await svc.create_agent(owner_user_id="usr_a", name="a1")
    a2 = await svc.create_agent(owner_user_id="usr_a", name="a2")

    # Keep A2 busy so the run doesn't fire and drain the flag.
    svc._running_agents.add(a2.id)
    delegate_task = asyncio.create_task(
        svc._exec_agent_delegate({
            "_agent_id": a1.id,
            "target_name": "a2",
            "instruction": "handle this",
            "max_wait_s": 1,
        })
    )
    # Yield once so the delegate handler runs up through _signal_agent.
    # The task will then block on the future until timeout.
    for _ in range(20):
        await asyncio.sleep(0.01)
        if svc._urgent_pending.get(a2.id):
            break
    try:
        assert svc._urgent_pending.get(a2.id) is True
    finally:
        svc._running_agents.discard(a2.id)
        # Let the delegate timeout finish so the asyncio task exits cleanly.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(delegate_task, timeout=3.0)


@pytest.mark.asyncio
async def test_agent_delegate_explicit_normal_does_not_set_pending(started_agent_service):
    """priority='normal' on delegate → no urgent flag."""
    svc = started_agent_service
    a1 = await svc.create_agent(owner_user_id="usr_a", name="a1")
    a2 = await svc.create_agent(owner_user_id="usr_a", name="a2")

    svc._running_agents.add(a2.id)
    delegate_task = asyncio.create_task(
        svc._exec_agent_delegate({
            "_agent_id": a1.id,
            "target_name": "a2",
            "instruction": "handle whenever",
            "max_wait_s": 1,
            "priority": "normal",
        })
    )
    # Yield enough for _signal_agent to have completed.
    for _ in range(20):
        await asyncio.sleep(0.01)
        # Inbox row exists once _signal_agent has persisted.
        sigs = svc._inboxes.get(a2.id, [])
        if sigs:
            break

    try:
        assert svc._inboxes.get(a2.id), "delegation signal should be queued"
        # Flag must remain unset for "normal" priority.
        assert not svc._urgent_pending.get(a2.id, False)
    finally:
        svc._running_agents.discard(a2.id)
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(delegate_task, timeout=3.0)


@pytest.mark.asyncio
async def test_drain_inbox_clears_urgent_pending(started_agent_service):
    """_drain_inbox unconditionally clears the urgent flag — even if
    no signals were drained, a stale flag could mis-trip the next round."""
    svc = started_agent_service
    a1 = await svc.create_agent(owner_user_id="usr_a", name="a1")

    # Pre-stage: flag set, plus one urgent signal queued so the drain
    # has work to do as well.
    svc._running_agents.add(a1.id)
    try:
        await svc._signal_agent(
            agent_id=a1.id,
            signal_kind="inbox",
            body="urgent ping",
            sender_kind="agent",
            sender_id="ag_other",
            sender_name="other",
            priority="urgent",
        )
        assert svc._urgent_pending.get(a1.id) is True

        sigs = await svc._drain_inbox(a1.id)
        assert len(sigs) == 1
        assert sigs[0].priority == "urgent"
        # And the flag is cleared.
        assert a1.id not in svc._urgent_pending
    finally:
        svc._running_agents.discard(a1.id)


@pytest.mark.asyncio
async def test_invalid_priority_returns_error_string(started_agent_service):
    """Unknown priority value → tool returns an error: ... string and
    no signal is created."""
    svc = started_agent_service
    a1 = await svc.create_agent(owner_user_id="usr_a", name="a1")
    a2 = await svc.create_agent(owner_user_id="usr_a", name="a2")

    result = await svc._exec_agent_send_message({
        "_agent_id": a1.id,
        "target_name": "a2",
        "body": "hi",
        "priority": "bogus",
    })
    assert result.startswith("error:")
    assert "priority" in result.lower()

    # No signal row was persisted.
    rows = await svc._storage.query(
        Query(
            collection=_AGENT_INBOX_SIGNALS_COLLECTION,
            filters=[Filter(field="agent_id", op=FilterOp.EQ, value=a2.id)],
        )
    )
    assert rows == []
    # Same path on delegate.
    result_d = await svc._exec_agent_delegate({
        "_agent_id": a1.id,
        "target_name": "a2",
        "instruction": "do",
        "priority": "WHATEVER",
    })
    assert result_d.startswith("error:")
    assert "priority" in result_d.lower()


# ── Phase 3 — end-to-end mid-round interrupt wiring ──────────────────


@pytest.mark.asyncio
async def test_urgent_interrupts_mid_round(started_agent_service):
    """End-to-end wiring assertion.

    The boundary mechanic itself is exercised in
    ``tests/unit/test_ai_service_interrupt.py``; this test verifies the
    Agent side: _run_agent_internal hands AIService.chat a callback
    that reads ``self._urgent_pending[a.id]`` — so when the flag is
    set ahead of (or during) a run, the interrupt callback returns
    True.
    """
    svc = started_agent_service
    a1 = await svc.create_agent(owner_user_id="usr_a", name="a1")

    # Capture the kwargs of the next chat call so we can extract the
    # callback _run_agent_internal hands down.
    captured: dict[str, object] = {}

    async def fake_chat(*args, **kwargs):
        captured.update(kwargs)
        from gilbert.interfaces.ai import ChatTurnResult
        return ChatTurnResult(
            response_text="ok",
            conversation_id="conv_test",
            ui_blocks=[],
            tool_usage=[],
            attachments=[],
            rounds=[],
            interrupted=False,
            model="",
            turn_usage={"rounds": 1},
        )

    svc._ai.chat = fake_chat  # type: ignore[method-assign]

    # Pre-stage the urgent flag *before* the run starts.
    svc._urgent_pending[a1.id] = True

    await svc.run_agent_now(a1.id, user_message="hello")

    assert "mid_round_interrupt" in captured, (
        "AgentService must pass a mid_round_interrupt callback to AIService.chat"
    )
    cb = captured["mid_round_interrupt"]
    assert callable(cb)

    # Round 0 calls _drain_inbox, which clears the urgent flag — so
    # by the time fake_chat captures the callback, the flag has just
    # been cleared. Re-set it to simulate "an urgent signal arrived
    # mid-round" and assert the callback returns True.
    svc._urgent_pending[a1.id] = True
    assert cb() is True

    # Clear it; callback returns False — backwards-compat path.
    svc._urgent_pending.pop(a1.id, None)
    assert cb() is False
