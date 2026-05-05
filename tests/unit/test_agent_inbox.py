"""InboxSignal + _signal_agent dispatch + drain-into-run wiring."""

from __future__ import annotations

import pytest  # noqa: F401

from gilbert.core.services.agent import _AGENT_INBOX_SIGNALS_COLLECTION
from gilbert.interfaces.agent import InboxSignal
from gilbert.interfaces.ai import MessageRole


@pytest.mark.asyncio
async def test_signal_persists_inbox_row(started_agent_service):
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")
    # Mark agent as busy to force enqueue path.
    svc._running_agents.add(a.id)
    sig = await svc._signal_agent(
        agent_id=a.id,
        signal_kind="inbox",
        body="hello",
        sender_kind="user", sender_id="usr_1", sender_name="brian",
        source_conv_id="conv_1", source_message_id="msg_1",
        metadata={},
    )
    assert sig.id
    assert sig.processed_at is None
    assert any(s.id == sig.id for s in svc._inboxes.get(a.id, []))


@pytest.mark.asyncio
async def test_drain_marks_signals_processed(started_agent_service):
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")
    svc._running_agents.add(a.id)
    sig = await svc._signal_agent(
        agent_id=a.id, signal_kind="inbox", body="hi",
        sender_kind="user", sender_id="usr_1", sender_name="brian",
        source_conv_id="c", source_message_id="m", metadata={},
    )
    drained = await svc._drain_inbox(a.id)
    drained_ids = [s.id for s in drained]
    assert drained_ids == [sig.id]
    assert svc._inboxes.get(a.id, []) == []
    row = await svc._storage.get(_AGENT_INBOX_SIGNALS_COLLECTION, sig.id)
    assert row["processed_at"] is not None


def _make_signal(agent_id: str, body: str, sender_name: str) -> InboxSignal:
    """Build an InboxSignal already in 'pending' state (processed_at=None)."""
    from datetime import UTC, datetime
    return InboxSignal(
        id=f"sig_test_{sender_name}",
        agent_id=agent_id,
        signal_kind="inbox",
        body=body,
        sender_kind="agent",
        sender_id="ag_other",
        sender_name=sender_name,
        source_conv_id="",
        source_message_id="",
        delegation_id="",
        metadata={},
        priority="normal",
        created_at=datetime.now(UTC),
        processed_at=None,
    )


@pytest.mark.asyncio
async def test_run_drains_inbox_at_round_zero(started_agent_service):
    """Pre-staged signals are formatted and appended to user_message."""
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")

    # Pre-stage two signals (persist + cache, like _signal_agent does).
    sigs = [
        _make_signal(a.id, "hello", "alpha"),
        _make_signal(a.id, "world", "beta"),
    ]
    from gilbert.core.services.agent import _signal_to_dict
    for sig in sigs:
        await svc._storage.put(_AGENT_INBOX_SIGNALS_COLLECTION, sig.id, _signal_to_dict(sig))
        svc._inboxes.setdefault(a.id, []).append(sig)

    await svc.run_agent_now(a.id, user_message="please process")

    sent = svc._ai.last_call_kwargs.get("user_message", "")
    assert "please process" in sent
    assert "INBOX:" in sent
    assert "[from alpha]: hello" in sent
    assert "[from beta]: world" in sent

    # Drained signals must be marked processed.
    for sig in sigs:
        row = await svc._storage.get(_AGENT_INBOX_SIGNALS_COLLECTION, sig.id)
        assert row["processed_at"] is not None
    # Cache cleared after drain.
    assert svc._inboxes.get(a.id, []) == []


@pytest.mark.asyncio
async def test_run_drains_inbox_between_rounds(started_agent_service):
    """The fake's between_rounds_callback drains and returns USER messages."""
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")

    # Configure the fake to invoke between_rounds_callback once.
    svc._ai.invoke_between_rounds = True

    # Pre-stage one signal that will be drained mid-run via the callback.
    sig = _make_signal(a.id, "midflight", "gamma")
    from gilbert.core.services.agent import _signal_to_dict
    await svc._storage.put(_AGENT_INBOX_SIGNALS_COLLECTION, sig.id, _signal_to_dict(sig))
    svc._inboxes.setdefault(a.id, []).append(sig)

    # Run; the run will:
    #   1) drain at round 0 (pulls our pre-staged sig and appends to user_msg)
    #   2) call chat → fake invokes the callback once, drain returns []
    # So we want to test BOTH cases. Reset and test the between-rounds path
    # by NOT pre-staging at round 0 — instead, stage right before the
    # callback fires.
    # But the existing run already drained. Stage a NEW signal AFTER the
    # service has booted but before chat runs the callback. The fake's
    # callback fires synchronously from chat(), so the easiest setup:
    # delete the old signal we added (it was drained at round 0), and
    # add a fresh one before triggering the run.
    svc._inboxes.clear()
    await svc._storage.delete(_AGENT_INBOX_SIGNALS_COLLECTION, sig.id)
    svc._ai.last_between_rounds_result = None

    # New signal, staged before run (will be drained at round 0).
    sig0 = _make_signal(a.id, "round-zero", "zeta")
    await svc._storage.put(_AGENT_INBOX_SIGNALS_COLLECTION, sig0.id, _signal_to_dict(sig0))
    svc._inboxes.setdefault(a.id, []).append(sig0)

    # And another signal staged via _signal_agent BEFORE chat runs;
    # since the agent is in _running_agents during the run, _signal_agent
    # enqueues without spawning. To stage between_rounds, we monkeypatch
    # the fake to stage a signal mid-chat then invoke the callback.
    original_chat = svc._ai.chat

    async def _chat_with_late_stage(*args, **kwargs):
        # Round 0 drained sig0; now stage a second one before the callback.
        late = _make_signal(a.id, "between-rounds", "delta")
        await svc._storage.put(_AGENT_INBOX_SIGNALS_COLLECTION, late.id, _signal_to_dict(late))
        svc._inboxes.setdefault(a.id, []).append(late)
        return await original_chat(*args, **kwargs)

    svc._ai.chat = _chat_with_late_stage
    await svc.run_agent_now(a.id, user_message="go")

    # Round 0 inbox block is in user_message.
    sent = svc._ai.last_call_kwargs.get("user_message", "")
    assert "[from zeta]: round-zero" in sent

    # Between-rounds callback returned a Message for the late-staged signal.
    injected = svc._ai.last_between_rounds_result
    assert injected is not None
    assert len(injected) == 1
    assert injected[0].role is MessageRole.USER
    assert injected[0].content == "[from delta]: between-rounds"


@pytest.mark.asyncio
async def test_inbox_rehydrated_on_start(started_agent_service):
    """Drop a pending InboxSignal directly into storage, restart the
    service, verify in-memory cache picked it up."""
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")
    storage = svc._storage
    await storage.put(_AGENT_INBOX_SIGNALS_COLLECTION, "sig_pre", {
        "_id": "sig_pre",
        "agent_id": a.id,
        "signal_kind": "inbox",
        "body": "hi",
        "sender_kind": "user",
        "sender_id": "usr_1",
        "sender_name": "brian",
        "source_conv_id": "c",
        "source_message_id": "m",
        "delegation_id": "",
        "metadata": {},
        "priority": "normal",
        "created_at": "2026-05-04T10:00:00+00:00",
        "processed_at": None,
    })
    # Stop and restart the service against the same storage.
    await svc.stop()
    # The fixture's resolver reference is stable; re-call start.
    if svc._resolver is not None:
        await svc.start(svc._resolver)
    assert any(s.id == "sig_pre" for s in svc._inboxes.get(a.id, []))
