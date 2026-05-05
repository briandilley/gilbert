"""Commitment — create/complete/list and heartbeat surfacing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


async def test_create_commitment_persists(started_agent_service):
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")
    c = await svc.create_commitment(
        agent_id=a.id,
        content="check inbox",
        due_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    assert c.id
    assert c.content == "check inbox"
    assert c.completed_at is None


async def test_complete_commitment_marks_done(started_agent_service):
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")
    c = await svc.create_commitment(agent_id=a.id, content="foo", due_at=datetime.now(UTC))
    completed = await svc.complete_commitment(c.id, note="handled")
    assert completed.completed_at is not None
    assert completed.completion_note == "handled"


async def test_due_commitments_filters_by_time_and_unfinished(started_agent_service):
    svc = started_agent_service
    a = await svc.create_agent(owner_user_id="usr_1", name="x")
    past = await svc.create_commitment(
        agent_id=a.id, content="past", due_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    _future = await svc.create_commitment(
        agent_id=a.id, content="future", due_at=datetime.now(UTC) + timedelta(hours=1)
    )
    await svc.complete_commitment(past.id, note="done")  # mark past as done

    due = await svc._due_commitments(a.id)
    # past is completed → excluded; future is not yet due → excluded
    assert due == []

    # Add a new past one that's still pending
    pending = await svc.create_commitment(
        agent_id=a.id, content="pending", due_at=datetime.now(UTC) - timedelta(seconds=5)
    )
    due = await svc._due_commitments(a.id)
    assert len(due) == 1
    assert due[0].id == pending.id
