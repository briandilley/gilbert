"""InboxSignal + _signal_agent dispatch."""

from __future__ import annotations

import pytest  # noqa: F401

from gilbert.core.services.agent import _AGENT_INBOX_SIGNALS_COLLECTION


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
