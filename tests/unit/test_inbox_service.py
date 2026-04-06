"""Tests for InboxService — email polling, persistence, and AI tools."""

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from gilbert.core.services.inbox import InboxService
from gilbert.interfaces.email import EmailAddress, EmailBackend, EmailMessage


# ── Fakes ──────────────────────────────────────────────────────


class FakeEmailBackend(EmailBackend):
    """In-memory email backend for testing."""

    def __init__(self) -> None:
        self.messages: list[EmailMessage] = []
        self.sent: list[dict[str, Any]] = []
        self.read_marks: list[str] = []
        self._next_send_id = "sent_001"

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def list_message_ids(self, query: str = "", max_results: int = 50) -> list[str]:
        return [m.message_id for m in self.messages[:max_results]]

    async def get_message(self, message_id: str) -> EmailMessage | None:
        for m in self.messages:
            if m.message_id == message_id:
                return m
        return None

    async def send(
        self,
        to: list[EmailAddress],
        subject: str,
        body_html: str,
        body_text: str = "",
        cc: list[EmailAddress] | None = None,
        in_reply_to: str = "",
        thread_id: str = "",
        attachments: Any = None,
    ) -> str:
        self.sent.append({
            "to": to, "subject": subject, "body_html": body_html,
            "body_text": body_text, "cc": cc,
            "in_reply_to": in_reply_to, "thread_id": thread_id,
            "attachments": attachments,
        })
        return self._next_send_id

    async def mark_read(self, message_id: str) -> None:
        self.read_marks.append(message_id)


class FakeStorageBackend:
    """In-memory storage backend for testing."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict[str, Any]]] = {}
        self._indexes: list[Any] = []

    async def get(self, collection: str, key: str) -> dict[str, Any] | None:
        record = self._data.get(collection, {}).get(key)
        if record is not None:
            return {**record, "_id": key}
        return None

    async def put(self, collection: str, key: str, data: dict[str, Any]) -> None:
        self._data.setdefault(collection, {})[key] = data

    async def delete(self, collection: str, key: str) -> None:
        self._data.get(collection, {}).pop(key, None)

    async def exists(self, collection: str, key: str) -> bool:
        return key in self._data.get(collection, {})

    async def count(self, query: Any) -> int:
        collection = query.collection
        results = []
        for key, data in self._data.get(collection, {}).items():
            record = {**data, "_id": key}
            match = True
            for f in (query.filters or []):
                val = record.get(f.field)
                from gilbert.interfaces.storage import FilterOp
                if f.op == FilterOp.EQ and val != f.value:
                    match = False
            if match:
                results.append(record)
        return len(results)

    async def query(self, query: Any) -> list[dict[str, Any]]:
        collection = query.collection
        results = []
        for key, data in self._data.get(collection, {}).items():
            record = {**data, "_id": key}
            match = True
            for f in (query.filters or []):
                val = record.get(f.field)
                from gilbert.interfaces.storage import FilterOp
                if f.op == FilterOp.EQ and val != f.value:
                    match = False
                elif f.op == FilterOp.CONTAINS and (val is None or str(f.value).lower() not in str(val).lower()):
                    match = False
            if match:
                results.append(record)
        # Sort by date descending if requested
        if query.sort:
            for s in reversed(query.sort):
                results.sort(key=lambda r: r.get(s.field, ""), reverse=s.descending)
        if query.limit:
            results = results[:query.limit]
        return results

    async def ensure_index(self, index_def: Any) -> None:
        self._indexes.append(index_def)


class FakeStorageService:
    def __init__(self) -> None:
        self.backend = FakeStorageBackend()

    def service_info(self) -> Any:
        from gilbert.interfaces.service import ServiceInfo
        return ServiceInfo(name="storage", capabilities=frozenset({"entity_storage"}))


class FakeEventBus:
    def __init__(self) -> None:
        self.published: list[Any] = []

    async def publish(self, event: Any) -> None:
        self.published.append(event)

    def subscribe(self, event_type: str, handler: Any) -> Any:
        return lambda: None


class FakeEventBusService:
    def __init__(self) -> None:
        self.bus = FakeEventBus()

    def service_info(self) -> Any:
        from gilbert.interfaces.service import ServiceInfo
        return ServiceInfo(name="event_bus", capabilities=frozenset({"event_bus"}))


class FakeSchedulerService:
    def __init__(self) -> None:
        self.jobs: dict[str, Any] = {}

    def service_info(self) -> Any:
        from gilbert.interfaces.service import ServiceInfo
        return ServiceInfo(name="scheduler", capabilities=frozenset({"scheduler"}))

    def add_job(self, **kwargs: Any) -> Any:
        self.jobs[kwargs["name"]] = kwargs


class FakeResolver:
    def __init__(self) -> None:
        self.caps: dict[str, Any] = {}

    def get_capability(self, cap: str) -> Any:
        return self.caps.get(cap)

    def require_capability(self, cap: str) -> Any:
        svc = self.caps.get(cap)
        if svc is None:
            raise LookupError(cap)
        return svc

    def get_all(self, cap: str) -> list[Any]:
        svc = self.caps.get(cap)
        return [svc] if svc else []


# ── Helpers ────────────────────────────────────────────────────


def _make_message(
    message_id: str = "msg_001",
    thread_id: str = "thread_001",
    subject: str = "Test Subject",
    sender_email: str = "alice@example.com",
    sender_name: str = "Alice",
    body_text: str = "Hello there",
) -> EmailMessage:
    return EmailMessage(
        message_id=message_id,
        thread_id=thread_id,
        subject=subject,
        sender=EmailAddress(email=sender_email, name=sender_name),
        to=[EmailAddress(email="gilbert@example.com")],
        cc=[],
        body_text=body_text,
        date=datetime(2026, 4, 5, 12, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def backend() -> FakeEmailBackend:
    return FakeEmailBackend()


@pytest.fixture
def event_bus_svc() -> FakeEventBusService:
    return FakeEventBusService()


@pytest.fixture
def resolver(backend: FakeEmailBackend, event_bus_svc: FakeEventBusService) -> FakeResolver:
    r = FakeResolver()
    r.caps["entity_storage"] = FakeStorageService()
    r.caps["event_bus"] = event_bus_svc
    r.caps["scheduler"] = FakeSchedulerService()
    return r


@pytest.fixture
async def inbox_service(
    backend: FakeEmailBackend, resolver: FakeResolver,
) -> InboxService:
    svc = InboxService(
        backend=backend,
        email_address="gilbert@example.com",
        poll_interval=60,
    )
    await svc.start(resolver)
    return svc


# ── Tests ──────────────────────────────────────────────────────


class TestServiceInfo:
    def test_service_info(self) -> None:
        svc = InboxService(backend=FakeEmailBackend())
        info = svc.service_info()
        assert info.name == "inbox"
        assert "email" in info.capabilities
        assert "ai_tools" in info.capabilities
        assert "entity_storage" in info.requires
        assert "scheduler" in info.requires

    def test_tool_definitions(self) -> None:
        svc = InboxService(backend=FakeEmailBackend())
        tools = svc.get_tools()
        names = {t.name for t in tools}
        assert names == {"inbox_search", "inbox_read", "inbox_reply", "inbox_send"}
        for t in tools:
            assert t.required_role == "user"


class TestPolling:
    @pytest.mark.asyncio
    async def test_poll_persists_new_messages(
        self, inbox_service: InboxService, backend: FakeEmailBackend,
    ) -> None:
        backend.messages = [_make_message()]
        await inbox_service._poll()

        record = await inbox_service.get_message("msg_001")
        assert record is not None
        assert record["subject"] == "Test Subject"
        assert record["sender_email"] == "alice@example.com"
        assert record["is_inbound"] is True

    @pytest.mark.asyncio
    async def test_poll_skips_already_persisted(
        self, inbox_service: InboxService, backend: FakeEmailBackend,
        event_bus_svc: FakeEventBusService,
    ) -> None:
        backend.messages = [_make_message()]
        await inbox_service._poll()
        await inbox_service._poll()  # second poll, same message

        # Only one event published
        events = event_bus_svc.bus.published
        received = [e for e in events if e.event_type == "inbox.message.received"]
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_poll_detects_own_messages(
        self, inbox_service: InboxService, backend: FakeEmailBackend,
    ) -> None:
        backend.messages = [_make_message(
            message_id="msg_own",
            sender_email="gilbert@example.com",
            sender_name="Gilbert",
        )]
        await inbox_service._poll()

        record = await inbox_service.get_message("msg_own")
        assert record is not None
        assert record["is_inbound"] is False

    @pytest.mark.asyncio
    async def test_poll_publishes_events(
        self, inbox_service: InboxService, backend: FakeEmailBackend,
        event_bus_svc: FakeEventBusService,
    ) -> None:
        backend.messages = [_make_message()]
        await inbox_service._poll()

        events = event_bus_svc.bus.published
        assert len(events) == 1
        assert events[0].event_type == "inbox.message.received"
        assert events[0].data["message_id"] == "msg_001"
        assert events[0].data["sender_email"] == "alice@example.com"
        assert events[0].data["is_inbound"] is True

    @pytest.mark.asyncio
    async def test_poll_picks_up_all_messages(
        self, inbox_service: InboxService, backend: FakeEmailBackend,
    ) -> None:
        """All messages from the backend should be persisted."""
        backend.messages = [
            _make_message(message_id="msg_a"),
            _make_message(message_id="msg_b"),
        ]
        await inbox_service._poll()

        assert await inbox_service.get_message("msg_a") is not None
        assert await inbox_service.get_message("msg_b") is not None

    @pytest.mark.asyncio
    async def test_poll_marks_read_in_backend(
        self, inbox_service: InboxService, backend: FakeEmailBackend,
    ) -> None:
        """Syncing a new message should mark it read in the remote backend."""
        backend.messages = [_make_message()]
        await inbox_service._poll()
        assert "msg_001" in backend.read_marks


class TestTools:
    @pytest.mark.asyncio
    async def test_search_empty(self, inbox_service: InboxService) -> None:
        result = await inbox_service.execute_tool("inbox_search", {})
        assert "No messages" in result

    @pytest.mark.asyncio
    async def test_search_after_poll(
        self, inbox_service: InboxService, backend: FakeEmailBackend,
    ) -> None:
        backend.messages = [
            _make_message(message_id="msg_a", subject="Invoice"),
            _make_message(message_id="msg_b", subject="Meeting"),
        ]
        await inbox_service._poll()

        result = await inbox_service.execute_tool("inbox_search", {})
        assert "2 message(s)" in result
        assert "msg_a" in result
        assert "msg_b" in result

    @pytest.mark.asyncio
    async def test_search_by_sender(
        self, inbox_service: InboxService, backend: FakeEmailBackend,
    ) -> None:
        backend.messages = [
            _make_message(message_id="m1", sender_email="alice@example.com"),
            _make_message(message_id="m2", sender_email="bob@example.com"),
        ]
        await inbox_service._poll()

        result = await inbox_service.execute_tool("inbox_search", {"sender": "bob"})
        assert "m2" in result
        assert "m1" not in result

    @pytest.mark.asyncio
    async def test_read_message(
        self, inbox_service: InboxService, backend: FakeEmailBackend,
    ) -> None:
        backend.messages = [_make_message(body_text="Important content")]
        await inbox_service._poll()

        result = await inbox_service.execute_tool("inbox_read", {"message_id": "msg_001"})
        data = json.loads(result)
        assert data["subject"] == "Test Subject"
        assert "Important content" in data["body"]

    @pytest.mark.asyncio
    async def test_read_not_found(self, inbox_service: InboxService) -> None:
        result = await inbox_service.execute_tool("inbox_read", {"message_id": "nope"})
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_reply(
        self, inbox_service: InboxService, backend: FakeEmailBackend,
    ) -> None:
        backend.messages = [_make_message()]
        await inbox_service._poll()

        result = await inbox_service.execute_tool("inbox_reply", {
            "message_id": "msg_001",
            "body_html": "<p>Thanks!</p>",
        })
        assert "sent" in result.lower()
        assert len(backend.sent) == 1
        assert backend.sent[0]["to"][0].email == "alice@example.com"
        assert backend.sent[0]["thread_id"] == "thread_001"

    @pytest.mark.asyncio
    async def test_reply_persists_outbound(
        self, inbox_service: InboxService, backend: FakeEmailBackend,
    ) -> None:
        backend.messages = [_make_message()]
        await inbox_service._poll()

        await inbox_service.execute_tool("inbox_reply", {
            "message_id": "msg_001",
            "body_html": "<p>Reply</p>",
        })

        record = await inbox_service.get_message("sent_001")
        assert record is not None
        assert record["is_inbound"] is False
        assert record["sender_email"] == "gilbert@example.com"

    @pytest.mark.asyncio
    async def test_send_new(
        self, inbox_service: InboxService, backend: FakeEmailBackend,
    ) -> None:
        result = await inbox_service.execute_tool("inbox_send", {
            "to": ["bob@example.com"],
            "subject": "Hello Bob",
            "body_html": "<p>Hi</p>",
        })
        assert "sent" in result.lower()
        assert len(backend.sent) == 1
        assert backend.sent[0]["subject"] == "Hello Bob"

    @pytest.mark.asyncio
    async def test_send_validates_required_fields(
        self, inbox_service: InboxService,
    ) -> None:
        result = await inbox_service.execute_tool("inbox_send", {
            "subject": "No recipient",
            "body_html": "<p>Hi</p>",
        })
        assert "to is required" in result.lower()

    @pytest.mark.asyncio
    async def test_unknown_tool(self, inbox_service: InboxService) -> None:
        with pytest.raises(KeyError):
            await inbox_service.execute_tool("inbox_nope", {})


class TestPublicAPI:
    @pytest.mark.asyncio
    async def test_send_publishes_event(
        self, inbox_service: InboxService, backend: FakeEmailBackend,
        event_bus_svc: FakeEventBusService,
    ) -> None:
        await inbox_service.send_message(
            to=[EmailAddress(email="bob@example.com")],
            subject="Hello",
            body_html="<p>Hi</p>",
        )
        sent_events = [
            e for e in event_bus_svc.bus.published
            if e.event_type == "inbox.message.sent"
        ]
        assert len(sent_events) == 1
        assert sent_events[0].data["subject"] == "Hello"

    @pytest.mark.asyncio
    async def test_reply_publishes_event(
        self, inbox_service: InboxService, backend: FakeEmailBackend,
        event_bus_svc: FakeEventBusService,
    ) -> None:
        backend.messages = [_make_message()]
        await inbox_service._poll()
        event_bus_svc.bus.published.clear()

        await inbox_service.reply_to_message(
            message_id="msg_001",
            body_html="<p>Reply</p>",
        )
        replied_events = [
            e for e in event_bus_svc.bus.published
            if e.event_type == "inbox.message.replied"
        ]
        assert len(replied_events) == 1
        assert replied_events[0].data["in_reply_to_message"] == "msg_001"

    @pytest.mark.asyncio
    async def test_truncates_long_body(
        self, inbox_service: InboxService, backend: FakeEmailBackend,
    ) -> None:
        inbox_service._max_body_length = 100
        long_body = "x" * 200
        backend.messages = [_make_message(body_text=long_body)]
        await inbox_service._poll()

        record = await inbox_service.get_message("msg_001")
        assert record is not None
        assert len(record["body_text"]) < 200
        assert "[truncated]" in record["body_text"]

    @pytest.mark.asyncio
    async def test_get_thread(
        self, inbox_service: InboxService, backend: FakeEmailBackend,
    ) -> None:
        backend.messages = [
            _make_message(message_id="m1", thread_id="t1"),
            _make_message(message_id="m2", thread_id="t1"),
            _make_message(message_id="m3", thread_id="t2"),
        ]
        await inbox_service._poll()

        thread = await inbox_service.get_thread("t1")
        assert len(thread) == 2
        assert all(m["thread_id"] == "t1" for m in thread)

    @pytest.mark.asyncio
    async def test_get_stats(
        self, inbox_service: InboxService, backend: FakeEmailBackend,
    ) -> None:
        backend.messages = [
            _make_message(message_id="m1"),
            _make_message(message_id="m2", sender_email="gilbert@example.com"),
        ]
        await inbox_service._poll()

        stats = await inbox_service.get_stats()
        assert stats["total"] == 2
        assert stats["inbound"] == 1
