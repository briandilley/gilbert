"""Tests for UserMemoryService — auto-capturing user memories from chat
transcripts.

Strategy: stub the AIService so each test controls exactly what the
synthesis call returns, and use a real SQLite storage so the cap /
ownership / watermark logic exercises the actual query/filter code path.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from gilbert.core.events import InMemoryEventBus
from gilbert.core.services.user_memory import UserMemoryService
from gilbert.interfaces.ai import AIResponse, Message, MessageRole, StopReason
from gilbert.interfaces.events import Event
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import (
    StorageBackend,
)

# ── Stubs ─────────────────────────────────────────────────────────


class StubAI:
    """AIService stand-in. Each call dequeues the next canned response;
    if the queue's empty it raises so a missing stub is loud, not silent."""

    def __init__(self) -> None:
        self.responses: list[str] = []  # JSON strings to return as content
        self.calls: list[tuple[str, str, str]] = []  # (system_prompt, user_content, profile)

    def queue(self, json_text: str) -> None:
        self.responses.append(json_text)

    async def complete_one_shot(
        self,
        *,
        messages: list[Message],
        system_prompt: str = "",
        profile_name: str | None = None,
        max_tokens: int | None = None,
        tools_override: list[Any] | None = None,
    ) -> AIResponse:
        if not self.responses:
            raise AssertionError(
                "StubAI: ran out of canned responses — the test under-stubbed."
            )
        text = self.responses.pop(0)
        user_content = messages[0].content if messages else ""
        self.calls.append((system_prompt, user_content, profile_name or ""))
        return AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content=text),
            model="test-model",
            stop_reason=StopReason.END_TURN,
            usage=None,
        )


class StubStorageService(Service):
    def __init__(self, backend: StorageBackend) -> None:
        self.backend = backend
        self.raw_backend = backend

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="storage", capabilities=frozenset({"entity_storage"})
        )

    def create_namespaced(self, namespace: str) -> Any:
        from gilbert.interfaces.storage import NamespacedStorageBackend

        return NamespacedStorageBackend(self.backend, namespace)


class StubAIService(Service):
    def __init__(self, ai: StubAI) -> None:
        self._ai = ai

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(name="ai", capabilities=frozenset({"ai_chat"}))

    async def complete_one_shot(self, **kwargs: Any) -> AIResponse:
        return await self._ai.complete_one_shot(**kwargs)


class StubUserBackend:
    """Just enough UserBackend for opt-out tests."""

    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}

    async def get_user(self, user_id: str) -> dict[str, Any] | None:
        u = self.users.get(user_id)
        return dict(u) if u else None

    async def update_user(self, user_id: str, patch: dict[str, Any]) -> None:
        if user_id not in self.users:
            return
        self.users[user_id].update(patch)


class StubUserService(Service):
    def __init__(self, backend: StubUserBackend) -> None:
        self.backend = backend

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(name="users", capabilities=frozenset({"users"}))


class StubResolver(ServiceResolver):
    def __init__(self, services: dict[str, Service]) -> None:
        self._by_cap = services

    def get_capability(self, capability: str) -> Service | None:
        return self._by_cap.get(capability)

    def require_capability(self, capability: str) -> Service:
        svc = self._by_cap.get(capability)
        if svc is None:
            raise LookupError(f"Missing: {capability}")
        return svc

    def get_all(self, capability: str) -> list[Service]:
        svc = self._by_cap.get(capability)
        return [svc] if svc else []


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
async def memory_service(
    sqlite_storage: StorageBackend,
) -> tuple[UserMemoryService, StubAI, StubUserBackend]:
    ai = StubAI()
    user_backend = StubUserBackend()
    svc = UserMemoryService()
    resolver = StubResolver(
        {
            "ai_chat": StubAIService(ai),
            "entity_storage": StubStorageService(sqlite_storage),
            "users": StubUserService(user_backend),
        }
    )
    await svc.start(resolver)
    return svc, ai, user_backend


def _seed_user(backend: StubUserBackend, user_id: str) -> None:
    backend.users[user_id] = {
        "_id": user_id,
        "email": f"{user_id}@example.com",
        "metadata": {},
    }


def _conv(
    *,
    chat_id: str = "chat1",
    user_id: str = "u1",
    user_turns: int = 4,
    updated_at: str | None = None,
    shared: bool = False,
) -> dict[str, Any]:
    """Fabricate a conversation doc with `user_turns` user/assistant
    pairs, suitable for feeding _maybe_synthesize directly."""
    messages: list[dict[str, Any]] = []
    for i in range(user_turns):
        messages.append({"role": "user", "content": f"user msg {i}"})
        messages.append({"role": "assistant", "content": f"asst reply {i}"})
    return {
        "_id": chat_id,
        "user_id": user_id,
        "messages": messages,
        "updated_at": updated_at or datetime.now(UTC).isoformat(),
        "shared": shared,
    }


# ── Tests ─────────────────────────────────────────────────────────


async def test_synthesis_happy_path_adds_memory(memory_service) -> None:
    svc, ai, _ = memory_service
    ai.queue(
        '{"ops": [{"op": "add", "summary": "Likes terse answers", '
        '"content": "User explicitly asked for concise responses."}]}'
    )
    await svc._maybe_synthesize(_conv(), source="delete")

    memories = await svc.list_user_memories("u1")
    assert len(memories) == 1
    assert memories[0]["summary"] == "Likes terse answers"
    assert memories[0]["source"] == "auto"


async def test_skip_short_chat(memory_service) -> None:
    svc, ai, _ = memory_service
    # Below the default min_user_turns=4
    await svc._maybe_synthesize(
        _conv(user_turns=2), source="delete"
    )
    assert ai.calls == []  # no AI call made
    assert await svc.list_user_memories("u1") == []


async def test_skip_shared_room(memory_service) -> None:
    svc, ai, _ = memory_service
    await svc._maybe_synthesize(
        _conv(shared=True), source="delete"
    )
    assert ai.calls == []


async def test_skip_admin_opt_out(memory_service) -> None:
    svc, ai, _ = memory_service
    svc._opted_out_user_ids = frozenset({"u1"})
    await svc._maybe_synthesize(_conv(), source="delete")
    assert ai.calls == []


async def test_skip_self_opt_out(memory_service) -> None:
    svc, ai, user_backend = memory_service
    _seed_user(user_backend, "u1")
    user_backend.users["u1"]["metadata"] = {"user_memory_opted_out": True}

    await svc._maybe_synthesize(_conv(), source="delete")
    assert ai.calls == []


async def test_multi_pass_dedupe_via_update(memory_service) -> None:
    """A second pass on the same chat with new content should refine an
    existing memory via 'update', not stack a near-duplicate."""
    svc, ai, _ = memory_service
    # First pass: add a memory.
    ai.queue('{"ops": [{"op": "add", "summary": "Prefers concise replies", '
             '"content": "Asked twice for shorter answers."}]}')
    conv1 = _conv(updated_at="2026-04-30T10:00:00+00:00")
    await svc._maybe_synthesize(conv1, source="delete")

    memories = await svc.list_user_memories("u1")
    assert len(memories) == 1
    existing_id = memories[0]["_id"]

    # Bypass the cooldown (two real chats hours apart wouldn't hit it).
    svc._last_synth_at["u1"] = 0.0

    # Second pass: refine the existing memory.
    ai.queue(
        '{"ops": [{"op": "update", "memory_id": "' + existing_id + '", '
        '"summary": "Prefers concise, no-filler replies", '
        '"content": "Repeatedly asked for shorter answers; explicitly '
        'objected to filler phrases."}]}'
    )
    conv2 = _conv(updated_at="2026-04-30T11:00:00+00:00", chat_id="chat2")
    await svc._maybe_synthesize(conv2, source="delete")

    memories = await svc.list_user_memories("u1")
    assert len(memories) == 1, "update should not have created a duplicate"
    assert memories[0]["summary"] == "Prefers concise, no-filler replies"


async def test_watermark_skips_unchanged_chat(memory_service) -> None:
    """If the same chat is processed twice with the same updated_at, the
    second call must be a no-op (the chat hasn't gained new content)."""
    svc, ai, _ = memory_service
    ai.queue('{"ops": []}')
    conv = _conv(updated_at="2026-04-30T10:00:00+00:00")
    await svc._maybe_synthesize(conv, source="delete")
    assert len(ai.calls) == 1

    # Second call with the same updated_at — no AI call should be made.
    svc._last_synth_at["u1"] = 0.0  # bypass cooldown
    await svc._maybe_synthesize(conv, source="delete")
    assert len(ai.calls) == 1


async def test_cooldown_blocks_back_to_back(memory_service) -> None:
    svc, ai, _ = memory_service
    ai.queue('{"ops": []}')
    await svc._maybe_synthesize(
        _conv(updated_at="2026-04-30T10:00:00+00:00"),
        source="delete",
    )
    # Second chat for same user, immediately — cooldown should block.
    await svc._maybe_synthesize(
        _conv(
            chat_id="chat2",
            updated_at="2026-04-30T10:01:00+00:00",
        ),
        source="delete",
    )
    assert len(ai.calls) == 1


async def test_cross_user_isolation(memory_service) -> None:
    """A synthesis for u1 must not affect u2's memories — and a delete
    op aimed at u2's memory_id must be rejected."""
    svc, ai, _ = memory_service
    # Seed a memory for u2 directly (manual), then have u1's synthesis
    # try to delete it.
    await svc._storage.put(  # type: ignore[union-attr]
        "user_memories",
        "memory_other",
        {
            "memory_id": "memory_other",
            "user_id": "u2",
            "summary": "u2's secret",
            "content": "private",
            "source": "user",
            "access_count": 0,
            "created_at": "2026-04-01T00:00:00+00:00",
            "updated_at": "2026-04-01T00:00:00+00:00",
        },
    )
    ai.queue('{"ops": [{"op": "delete", "memory_id": "memory_other"}]}')
    await svc._maybe_synthesize(_conv(user_id="u1"), source="delete")

    # u2's memory must still be there.
    record = await svc._storage.get(  # type: ignore[union-attr]
        "user_memories", "memory_other"
    )
    assert record is not None
    assert record["summary"] == "u2's secret"


async def test_user_source_protected_from_auto_delete(memory_service) -> None:
    """The synthesis call MUST NOT delete a memory whose source='user'
    (manually saved by the user/admin), even if it tries to."""
    svc, ai, _ = memory_service
    await svc._storage.put(  # type: ignore[union-attr]
        "user_memories",
        "memory_user",
        {
            "memory_id": "memory_user",
            "user_id": "u1",
            "summary": "Allergic to peanuts",
            "content": "Severe peanut allergy.",
            "source": "user",
            "access_count": 0,
            "created_at": "2026-04-01T00:00:00+00:00",
            "updated_at": "2026-04-01T00:00:00+00:00",
        },
    )
    ai.queue('{"ops": [{"op": "delete", "memory_id": "memory_user"}]}')
    await svc._maybe_synthesize(_conv(), source="delete")

    record = await svc._storage.get(  # type: ignore[union-attr]
        "user_memories", "memory_user"
    )
    assert record is not None, "user-source memory must survive auto delete attempt"


async def test_cap_trims_oldest_auto_entries(memory_service) -> None:
    svc, _, _ = memory_service
    svc._max_memories_per_user = 3
    # Seed 5 auto memories with ascending updated_at so we can predict
    # which ones the cap drops.
    for i in range(5):
        await svc._storage.put(  # type: ignore[union-attr]
            "user_memories",
            f"memory_{i}",
            {
                "memory_id": f"memory_{i}",
                "user_id": "u1",
                "summary": f"auto fact {i}",
                "content": f"content {i}",
                "source": "auto",
                "access_count": 0,
                "created_at": f"2026-04-{i + 1:02d}T00:00:00+00:00",
                "updated_at": f"2026-04-{i + 1:02d}T00:00:00+00:00",
            },
        )
    await svc._enforce_cap("u1")
    memories = await svc.list_user_memories("u1")
    assert len(memories) == 3
    # The two oldest (memory_0 and memory_1) should be the casualties.
    surviving_ids = {m["_id"] for m in memories}
    assert surviving_ids == {"memory_2", "memory_3", "memory_4"}


async def test_op_parser_handles_code_fences(memory_service) -> None:
    """The synthesis prompt forbids markdown fences but the parser must
    still survive them since LLMs sometimes ignore that rule."""
    svc, ai, _ = memory_service
    ai.queue('```json\n{"ops": [{"op": "add", "summary": "X", "content": "Y"}]}\n```')
    await svc._maybe_synthesize(_conv(), source="delete")
    memories = await svc.list_user_memories("u1")
    assert len(memories) == 1
    assert memories[0]["summary"] == "X"


async def test_op_parser_handles_garbage(memory_service) -> None:
    """A non-JSON response from the AI must be a no-op, not a crash."""
    svc, ai, _ = memory_service
    ai.queue("I'm sorry, I can't do that.")
    await svc._maybe_synthesize(_conv(), source="delete")
    assert await svc.list_user_memories("u1") == []


async def test_single_flight_per_user(memory_service) -> None:
    """Two concurrent synthesises for the same user must not race on
    the watermark / memory list."""
    svc, ai, _ = memory_service
    ai.queue('{"ops": [{"op": "add", "summary": "fact A", "content": "..."}]}')
    ai.queue('{"ops": []}')

    # Same chat doc, two concurrent calls. The second one to acquire the
    # lock should observe the watermark set by the first and skip — so
    # only ONE AI call happens, and only ONE memory is created.
    conv = _conv(updated_at="2026-04-30T10:00:00+00:00")
    await asyncio.gather(
        svc._maybe_synthesize(conv, source="delete"),
        svc._maybe_synthesize(conv, source="delete"),
    )

    memories = await svc.list_user_memories("u1")
    assert len(memories) == 1
    # The AI was only called once (the second waiter saw the watermark).
    assert len(ai.calls) == 1


async def test_idle_sweep_picks_old_chats(memory_service, sqlite_storage) -> None:
    """The scheduler-driven sweep must find chats older than
    idle_after_hours and synthesize them."""
    svc, ai, _ = memory_service
    svc._idle_after_hours = 1
    # Seed two chats: one too fresh, one stale.
    fresh_at = datetime.now(UTC).isoformat()
    stale_at = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    await sqlite_storage.put(
        "ai_conversations",
        "fresh",
        {"user_id": "u1", "messages": _conv()["messages"], "updated_at": fresh_at},
    )
    await sqlite_storage.put(
        "ai_conversations",
        "stale",
        {"user_id": "u1", "messages": _conv()["messages"], "updated_at": stale_at},
    )
    ai.queue('{"ops": [{"op": "add", "summary": "swept", "content": "swept"}]}')
    await svc._run_idle_sweep()
    # Exactly one synthesis call — for the stale chat.
    assert len(ai.calls) == 1
    memories = await svc.list_user_memories("u1")
    assert len(memories) == 1


async def test_self_opt_out_toggle_persists(memory_service) -> None:
    svc, _, user_backend = memory_service
    _seed_user(user_backend, "u1")
    assert await svc.get_self_opt_out("u1") is False

    await svc.set_self_opt_out("u1", True)
    assert await svc.get_self_opt_out("u1") is True
    assert (
        user_backend.users["u1"]["metadata"]["user_memory_opted_out"] is True
    )

    await svc.set_self_opt_out("u1", False)
    assert await svc.get_self_opt_out("u1") is False
    assert (
        "user_memory_opted_out"
        not in user_backend.users["u1"]["metadata"]
    )


async def test_clear_user_memories(memory_service) -> None:
    svc, _, _ = memory_service
    for i in range(3):
        await svc._storage.put(  # type: ignore[union-attr]
            "user_memories",
            f"memory_{i}",
            {
                "memory_id": f"memory_{i}",
                "user_id": "u1",
                "summary": f"fact {i}",
                "content": "...",
                "source": "auto",
                "access_count": 0,
                "created_at": "2026-04-01T00:00:00+00:00",
                "updated_at": "2026-04-01T00:00:00+00:00",
            },
        )
    count = await svc.clear_user_memories("u1")
    assert count == 3
    assert await svc.list_user_memories("u1") == []


async def test_ai_profile_passed_through(memory_service) -> None:
    """The configured ai_profile must be the value passed to
    complete_one_shot — that's how the admin tunes cost."""
    svc, ai, _ = memory_service
    svc._ai_profile = "light"
    ai.queue('{"ops": []}')
    await svc._maybe_synthesize(_conv(), source="delete")
    assert ai.calls[0][2] == "light"


async def test_synthesis_prompt_passed_through(memory_service) -> None:
    """The configured synthesis_prompt must be sent as the system prompt
    — admins can edit it via Settings."""
    svc, ai, _ = memory_service
    svc._synthesis_prompt = "CUSTOM PROMPT MARKER"
    ai.queue('{"ops": []}')
    await svc._maybe_synthesize(_conv(), source="delete")
    assert ai.calls[0][0] == "CUSTOM PROMPT MARKER"


# ── Non-blocking event handler ────────────────────────────────────


class SlowAIService(Service):
    """AIService stand-in whose synthesis call only finishes when a
    test-controlled event is set. Used to prove the archiving handler
    doesn't await the AI call inside the publisher's call frame."""

    def __init__(self) -> None:
        self.released = asyncio.Event()
        self.started = asyncio.Event()
        self.finished = asyncio.Event()

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(name="ai", capabilities=frozenset({"ai_chat"}))

    async def complete_one_shot(self, **kwargs: Any) -> AIResponse:
        self.started.set()
        await self.released.wait()
        self.finished.set()
        return AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content='{"ops": []}'),
            model="test-model",
            stop_reason=StopReason.END_TURN,
            usage=None,
        )


class StubEventBusService(Service):
    """EventBusProvider that wraps the in-memory bus."""

    def __init__(self, bus: InMemoryEventBus) -> None:
        self._bus = bus

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(name="event_bus", capabilities=frozenset({"event_bus"}))

    @property
    def bus(self) -> InMemoryEventBus:
        return self._bus


async def test_archiving_handler_is_non_blocking(
    sqlite_storage: StorageBackend,
) -> None:
    """The chat.conversation.archiving handler MUST hand synthesis off
    to a background task — ``EventBus.publish`` awaits every handler
    via ``gather``, so a synchronous-await on a multi-second AI call
    would block the chat-delete RPC. Regression check for that.
    """
    bus = InMemoryEventBus()
    slow_ai = SlowAIService()
    user_backend = StubUserBackend()
    svc = UserMemoryService()
    resolver = StubResolver(
        {
            "ai_chat": slow_ai,
            "entity_storage": StubStorageService(sqlite_storage),
            "event_bus": StubEventBusService(bus),
            "users": StubUserService(user_backend),
        }
    )
    await svc.start(resolver)

    conversation = {
        "user_id": "u1",
        "messages": _conv()["messages"],
        "updated_at": datetime.now(UTC).isoformat(),
    }

    # Publish — must return promptly even though the AI call hasn't released.
    publish_done = asyncio.Event()

    async def do_publish() -> None:
        await bus.publish(
            Event(
                event_type="chat.conversation.archiving",
                data={
                    "conversation_id": "chat1",
                    "owner_id": "u1",
                    "conversation": conversation,
                },
            )
        )
        publish_done.set()

    asyncio.create_task(do_publish())

    # The proof is in the ordering: publish must return BEFORE the AI
    # call finishes. We wait for both ``publish_done`` and ``started``
    # (the AI call has begun) and then verify the AI call is still
    # blocked. If the handler had awaited the AI call inline, publish
    # would only complete after ``finished`` — we'd time out here.
    await asyncio.wait_for(publish_done.wait(), timeout=1.0)
    await asyncio.wait_for(slow_ai.started.wait(), timeout=1.0)
    assert not slow_ai.finished.is_set(), (
        "publish returned before synthesis blocked, but synthesis "
        "completed too — the handler isn't using the background path"
    )

    # Release the AI and let the background task finish; ``stop`` will
    # drain it.
    slow_ai.released.set()
    await asyncio.wait_for(slow_ai.finished.wait(), timeout=1.0)
    await svc.stop()


async def test_stop_drains_background_tasks(
    sqlite_storage: StorageBackend,
) -> None:
    """``stop()`` must wait for in-flight background synthesises before
    returning, so a quick shutdown doesn't strand half-applied ops."""
    bus = InMemoryEventBus()
    slow_ai = SlowAIService()
    svc = UserMemoryService()
    resolver = StubResolver(
        {
            "ai_chat": slow_ai,
            "entity_storage": StubStorageService(sqlite_storage),
            "event_bus": StubEventBusService(bus),
            "users": StubUserService(StubUserBackend()),
        }
    )
    await svc.start(resolver)

    # Kick off a synthesis via a direct background spawn — same code
    # path the event handler uses.
    svc._spawn_background(
        svc._safe_synthesize(
            {
                "_id": "chat1",
                "user_id": "u1",
                "messages": _conv()["messages"],
                "updated_at": datetime.now(UTC).isoformat(),
            },
            source="delete",
        ),
        label="test.synth",
    )
    await asyncio.wait_for(slow_ai.started.wait(), timeout=1.0)
    assert len(svc._background_tasks) == 1

    # Release immediately so stop's drain wins on the happy path.
    slow_ai.released.set()
    await svc.stop()
    assert svc._background_tasks == set()
