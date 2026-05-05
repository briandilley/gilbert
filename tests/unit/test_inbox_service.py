"""Tests for multi-mailbox InboxService — mailbox CRUD, authorization, outbox, polling.

These tests bypass the real ``start()`` boot path (which schedules a
one-shot job to spin up runtimes). They construct an ``InboxService``,
attach fakes for storage / scheduler / event bus / access control,
register one or more mailboxes directly, and then call the public API.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gilbert.core.context import set_current_user
from gilbert.core.services.inbox import InboxService, _MailboxRuntime
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.email import EmailAddress, EmailBackend, EmailMessage
from gilbert.interfaces.inbox import (
    InboxProvider,
    Mailbox,
    MailboxAccess,
    OutboxDraft,
    OutboxStatus,
    can_access_mailbox,
    can_admin_mailbox,
    determine_access,
)
from gilbert.interfaces.storage import FilterOp

# ── Fakes ─────────────────────────────────────────────────────────


class FakeEmailBackend(EmailBackend):
    """In-memory email backend for testing."""

    backend_name = "fake"

    def __init__(self) -> None:
        self.messages: list[EmailMessage] = []
        self.sent: list[dict[str, Any]] = []
        self.read_marks: list[str] = []
        self.initialized_with: dict[str, Any] | None = None
        self.closed = False
        self._next_send_id = "sent_001"

    async def initialize(self, config: dict | None = None) -> None:
        self.initialized_with = dict(config or {})

    async def close(self) -> None:
        self.closed = True

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
        reply_to: EmailAddress | None = None,
        from_name: str = "",
    ) -> str:
        sent_id = self._next_send_id
        self.sent.append(
            {
                "id": sent_id,
                "to": to,
                "subject": subject,
                "body_html": body_html,
                "body_text": body_text,
                "cc": cc,
                "in_reply_to": in_reply_to,
                "thread_id": thread_id,
                "attachments": attachments,
                "reply_to": reply_to,
                "from_name": from_name,
            }
        )
        return sent_id

    async def mark_read(self, message_id: str) -> None:
        self.read_marks.append(message_id)


class FakeStorageBackend:
    """In-memory storage backend supporting EQ / IN / CONTAINS / LTE / GTE / EXISTS."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict[str, Any]]] = {}

    async def get(self, collection: str, key: str) -> dict[str, Any] | None:
        record = self._data.get(collection, {}).get(key)
        if record is None:
            return None
        return {**record, "_id": key}

    async def put(self, collection: str, key: str, data: dict[str, Any]) -> None:
        clean = {k: v for k, v in data.items() if k != "_id"}
        self._data.setdefault(collection, {})[key] = clean

    async def delete(self, collection: str, key: str) -> None:
        self._data.get(collection, {}).pop(key, None)

    async def exists(self, collection: str, key: str) -> bool:
        return key in self._data.get(collection, {})

    def _match(self, record: dict[str, Any], filters: list[Any]) -> bool:
        for f in filters:
            val = record.get(f.field)
            if f.op == FilterOp.EQ and val != f.value:
                return False
            if f.op == FilterOp.NEQ and val == f.value:
                return False
            if f.op == FilterOp.IN:
                if val not in f.value:
                    return False
            if f.op == FilterOp.CONTAINS:
                if val is None or str(f.value).lower() not in str(val).lower():
                    return False
            if f.op == FilterOp.LTE:
                if val is None or str(val) > str(f.value):
                    return False
            if f.op == FilterOp.GTE:
                if val is None or str(val) < str(f.value):
                    return False
            if f.op == FilterOp.EXISTS and (val is None) == bool(f.value):
                return False
        return True

    async def count(self, query: Any) -> int:
        coll = query.collection
        count = 0
        for key, data in self._data.get(coll, {}).items():
            record = {**data, "_id": key}
            if self._match(record, query.filters or []):
                count += 1
        return count

    async def query(self, query: Any) -> list[dict[str, Any]]:
        coll = query.collection
        results = []
        for key, data in self._data.get(coll, {}).items():
            record = {**data, "_id": key}
            if self._match(record, query.filters or []):
                results.append(record)
        if query.sort:
            for s in reversed(query.sort):
                results.sort(key=lambda r: r.get(s.field, ""), reverse=s.descending)
        if query.limit:
            results = results[: query.limit]
        return results

    async def ensure_index(self, index_def: Any) -> None:
        pass


class FakeStorageService:
    def __init__(self) -> None:
        self.backend = FakeStorageBackend()
        self.raw_backend = self.backend

    def create_namespaced(self, namespace: str) -> Any:
        return self.backend

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

    def remove_job(self, name: str, requester_id: str = "") -> None:
        self.jobs.pop(name, None)

    def enable_job(self, name: str) -> None:
        pass

    def disable_job(self, name: str) -> None:
        pass

    def list_jobs(self, include_system: bool = True) -> list[Any]:
        return list(self.jobs.values())

    def get_job(self, name: str) -> Any:
        return self.jobs.get(name)

    async def run_now(self, name: str) -> None:
        pass


class FakeAclService:
    """Treats user_id 'admin' as admin, everyone else as non-admin."""

    def get_role_level(self, role_name: str) -> int:
        return 0 if role_name == "admin" else 100

    def get_effective_level(self, user_ctx: UserContext) -> int:
        return 0 if "admin" in user_ctx.roles else 100

    def resolve_rpc_level(self, frame_type: str) -> int:
        return 100

    def check_collection_read(self, user_ctx: UserContext, collection: str) -> bool:
        return True

    def check_collection_write(self, user_ctx: UserContext, collection: str) -> bool:
        return True

    def service_info(self) -> Any:
        from gilbert.interfaces.service import ServiceInfo

        return ServiceInfo(name="access_control", capabilities=frozenset({"access_control"}))


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


# ── Helpers ───────────────────────────────────────────────────────


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
        to=[EmailAddress(email="owner@example.com")],
        cc=[],
        body_text=body_text,
        date=datetime(2026, 4, 5, 12, 0, tzinfo=UTC),
    )


def _make_mailbox(
    mailbox_id: str = "mbx_a",
    name: str = "Work",
    email_address: str = "owner@example.com",
    owner_user_id: str = "owner",
    shared_with_users: list[str] | None = None,
    shared_with_roles: list[str] | None = None,
    backend_name: str = "fake",
) -> Mailbox:
    return Mailbox(
        id=mailbox_id,
        name=name,
        email_address=email_address,
        backend_name=backend_name,
        backend_config={},
        owner_user_id=owner_user_id,
        shared_with_users=shared_with_users or [],
        shared_with_roles=shared_with_roles or [],
        poll_enabled=True,
        poll_interval_sec=60,
        created_at="2026-04-05T00:00:00+00:00",
    )


def _owner() -> UserContext:
    return UserContext(
        user_id="owner",
        email="owner@example.com",
        display_name="Owner",
        roles=frozenset({"user"}),
    )


def _shared_user() -> UserContext:
    return UserContext(
        user_id="alice",
        email="alice@example.com",
        display_name="Alice",
        roles=frozenset({"user"}),
    )


def _role_user() -> UserContext:
    return UserContext(
        user_id="bob",
        email="bob@example.com",
        display_name="Bob",
        roles=frozenset({"user", "sales"}),
    )


def _unrelated_user() -> UserContext:
    return UserContext(
        user_id="carol",
        email="carol@example.com",
        display_name="Carol",
        roles=frozenset({"user"}),
    )


def _admin_user() -> UserContext:
    return UserContext(
        user_id="admin",
        email="admin@example.com",
        display_name="Admin",
        roles=frozenset({"admin"}),
    )


@pytest.fixture
def storage_svc() -> FakeStorageService:
    return FakeStorageService()


@pytest.fixture
def event_bus_svc() -> FakeEventBusService:
    return FakeEventBusService()


@pytest.fixture
def scheduler_svc() -> FakeSchedulerService:
    return FakeSchedulerService()


@pytest.fixture
def resolver(
    storage_svc: FakeStorageService,
    event_bus_svc: FakeEventBusService,
    scheduler_svc: FakeSchedulerService,
) -> FakeResolver:
    r = FakeResolver()
    r.caps["entity_storage"] = storage_svc
    r.caps["event_bus"] = event_bus_svc
    r.caps["scheduler"] = scheduler_svc
    r.caps["access_control"] = FakeAclService()
    return r


@pytest.fixture
def inbox_service(
    resolver: FakeResolver,
    storage_svc: FakeStorageService,
    event_bus_svc: FakeEventBusService,
    scheduler_svc: FakeSchedulerService,
) -> InboxService:
    svc = InboxService()
    svc._enabled = True
    svc._storage = storage_svc.backend
    svc._event_bus = event_bus_svc.bus
    svc._scheduler = scheduler_svc
    svc._access_control = resolver.caps["access_control"]
    return svc


async def _attach_runtime(
    svc: InboxService,
    mailbox: Mailbox,
    backend: FakeEmailBackend,
) -> None:
    """Persist a mailbox and wire up a backend runtime without hitting the real
    backend registry / scheduler code path."""
    assert svc._storage is not None
    await svc._storage.put(
        "inbox_mailboxes",
        mailbox.id,
        mailbox.to_dict(),
    )
    svc._runtimes[mailbox.id] = _MailboxRuntime(
        mailbox=mailbox,
        backend=backend,
        poll_job_name=f"inbox-poll-{mailbox.id}",
    )


# ── Service metadata ──────────────────────────────────────────────


class TestServiceInfo:
    def test_implements_inbox_provider(self) -> None:
        svc = InboxService()
        assert isinstance(svc, InboxProvider)

    def test_service_info(self) -> None:
        info = InboxService().service_info()
        assert info.name == "inbox"
        assert "email" in info.capabilities
        assert "inbox" in info.capabilities
        assert "ai_tools" in info.capabilities
        assert "ws_handlers" in info.capabilities
        assert {"entity_storage", "scheduler"} <= info.requires
        assert "inbox.outbox.sent" in info.events
        assert "inbox.mailbox.shares.changed" in info.events
        assert info.toggleable is True

    def test_tool_names(self) -> None:
        svc = InboxService()
        svc._enabled = True
        names = {t.name for t in svc.get_tools()}
        assert names == {
            "inbox_mailboxes",
            "inbox_search",
            "inbox_read",
            "inbox_reply",
            "inbox_send",
        }

    def test_tools_empty_when_disabled(self) -> None:
        svc = InboxService()
        svc._enabled = False
        assert svc.get_tools() == []


# ── Authorization primitives ──────────────────────────────────────


class TestAuthorizationHelpers:
    def test_owner_has_access(self) -> None:
        m = _make_mailbox()
        assert can_access_mailbox(_owner(), m) is True
        assert can_admin_mailbox(_owner(), m) is True

    def test_shared_user_has_access_but_no_admin(self) -> None:
        m = _make_mailbox(shared_with_users=["alice"])
        assert can_access_mailbox(_shared_user(), m) is True
        assert can_admin_mailbox(_shared_user(), m) is False

    def test_shared_role_has_access_but_no_admin(self) -> None:
        m = _make_mailbox(shared_with_roles=["sales"])
        assert can_access_mailbox(_role_user(), m) is True
        assert can_admin_mailbox(_role_user(), m) is False

    def test_unrelated_user_denied(self) -> None:
        m = _make_mailbox()
        assert can_access_mailbox(_unrelated_user(), m) is False
        assert can_admin_mailbox(_unrelated_user(), m) is False

    def test_admin_has_full_access(self) -> None:
        m = _make_mailbox()
        assert can_access_mailbox(_admin_user(), m, is_admin=True) is True
        assert can_admin_mailbox(_admin_user(), m, is_admin=True) is True

    def test_determine_access_precedence(self) -> None:
        m = _make_mailbox(shared_with_users=["alice"], shared_with_roles=["sales"])
        # Owner tag wins over everything
        assert determine_access(_owner(), m, is_admin=True) == MailboxAccess.OWNER
        # Admin tag when not owner and no share
        assert determine_access(_admin_user(), m, is_admin=True) == MailboxAccess.ADMIN
        # Share-user tag
        assert determine_access(_shared_user(), m) == MailboxAccess.SHARED_USER
        # Share-role tag
        assert determine_access(_role_user(), m) == MailboxAccess.SHARED_ROLE
        # None
        assert determine_access(_unrelated_user(), m) is None


# ── Mailbox CRUD ──────────────────────────────────────────────────


class TestMailboxCrud:
    @pytest.mark.asyncio
    async def test_create_sets_owner_and_emits_event(
        self,
        inbox_service: InboxService,
        event_bus_svc: FakeEventBusService,
    ) -> None:
        mb = _make_mailbox(mailbox_id="", owner_user_id="")
        created = await inbox_service.create_mailbox(mb, _owner())
        assert created.owner_user_id == "owner"
        assert created.id  # got assigned
        assert any(e.event_type == "inbox.mailbox.created" for e in event_bus_svc.bus.published)

    @pytest.mark.asyncio
    async def test_update_by_owner_allowed(
        self,
        inbox_service: InboxService,
    ) -> None:
        mb = _make_mailbox()
        await _attach_runtime(inbox_service, mb, FakeEmailBackend())
        updated = await inbox_service.update_mailbox(
            mb.id,
            {"name": "New Name"},
            _owner(),
        )
        assert updated.name == "New Name"

    @pytest.mark.asyncio
    async def test_update_by_shared_user_forbidden(
        self,
        inbox_service: InboxService,
    ) -> None:
        from gilbert.core.services.inbox import InboxPermissionError

        mb = _make_mailbox(shared_with_users=["alice"])
        await _attach_runtime(inbox_service, mb, FakeEmailBackend())
        with pytest.raises(InboxPermissionError):
            await inbox_service.update_mailbox(
                mb.id,
                {"name": "Hacked"},
                _shared_user(),
            )

    @pytest.mark.asyncio
    async def test_share_user_roundtrip(
        self,
        inbox_service: InboxService,
        event_bus_svc: FakeEventBusService,
    ) -> None:
        mb = _make_mailbox()
        await _attach_runtime(inbox_service, mb, FakeEmailBackend())

        await inbox_service.share_user(mb.id, "alice", _owner())
        refreshed = await inbox_service.get_mailbox(mb.id)
        assert refreshed is not None
        assert "alice" in refreshed.shared_with_users
        assert any(
            e.event_type == "inbox.mailbox.shares.changed" for e in event_bus_svc.bus.published
        )

        await inbox_service.unshare_user(mb.id, "alice", _owner())
        refreshed = await inbox_service.get_mailbox(mb.id)
        assert refreshed is not None
        assert "alice" not in refreshed.shared_with_users

    @pytest.mark.asyncio
    async def test_share_role_roundtrip(
        self,
        inbox_service: InboxService,
    ) -> None:
        mb = _make_mailbox()
        await _attach_runtime(inbox_service, mb, FakeEmailBackend())

        await inbox_service.share_role(mb.id, "sales", _owner())
        refreshed = await inbox_service.get_mailbox(mb.id)
        assert refreshed is not None
        assert "sales" in refreshed.shared_with_roles

    @pytest.mark.asyncio
    async def test_delete_refuses_with_pending_outbox(
        self,
        inbox_service: InboxService,
    ) -> None:
        mb = _make_mailbox()
        await _attach_runtime(inbox_service, mb, FakeEmailBackend())

        # Queue a draft so the mailbox has non-terminal outbox rows.
        draft = OutboxDraft(
            to=[EmailAddress(email="alice@example.com")],
            subject="hi",
            body_html="<p>hi</p>",
        )
        await inbox_service.schedule_send(mb.id, draft, _owner())

        with pytest.raises(ValueError, match="pending/failed outbox entries"):
            await inbox_service.delete_mailbox(mb.id, _owner())

    @pytest.mark.asyncio
    async def test_delete_cascades_messages(
        self,
        inbox_service: InboxService,
        storage_svc: FakeStorageService,
    ) -> None:
        mb = _make_mailbox()
        await _attach_runtime(inbox_service, mb, FakeEmailBackend())
        await storage_svc.backend.put(
            "inbox_messages",
            "m1",
            {
                "mailbox_id": mb.id,
                "subject": "x",
                "body_text": "y",
                "date": "2026-04-05T00:00:00+00:00",
                "is_inbound": True,
                "thread_id": "t1",
                "sender_email": "a@b.c",
            },
        )
        await inbox_service.delete_mailbox(mb.id, _owner())
        assert await storage_svc.backend.get("inbox_messages", "m1") is None
        assert await storage_svc.backend.get("inbox_mailboxes", mb.id) is None


# ── Polling ───────────────────────────────────────────────────────


class TestPolling:
    @pytest.mark.asyncio
    async def test_poll_persists_with_mailbox_id(
        self,
        inbox_service: InboxService,
        storage_svc: FakeStorageService,
    ) -> None:
        backend = FakeEmailBackend()
        backend.messages = [_make_message(message_id="m1", sender_email="alice@example.com")]
        mb = _make_mailbox()
        await _attach_runtime(inbox_service, mb, backend)

        runtime = inbox_service._runtimes[mb.id]
        await inbox_service._poll_runtime(runtime)

        row = await storage_svc.backend.get("inbox_messages", "m1")
        assert row is not None
        assert row["mailbox_id"] == mb.id
        assert row["is_inbound"] is True

    @pytest.mark.asyncio
    async def test_poll_detects_own_messages_per_mailbox(
        self,
        inbox_service: InboxService,
        storage_svc: FakeStorageService,
    ) -> None:
        backend = FakeEmailBackend()
        backend.messages = [
            _make_message(message_id="m_self", sender_email="owner@example.com"),
        ]
        mb = _make_mailbox()  # email_address owner@example.com
        await _attach_runtime(inbox_service, mb, backend)

        await inbox_service._poll_runtime(inbox_service._runtimes[mb.id])
        row = await storage_svc.backend.get("inbox_messages", "m_self")
        assert row is not None
        assert row["is_inbound"] is False

    @pytest.mark.asyncio
    async def test_poll_publishes_event_with_mailbox_id(
        self,
        inbox_service: InboxService,
        event_bus_svc: FakeEventBusService,
    ) -> None:
        backend = FakeEmailBackend()
        backend.messages = [_make_message(message_id="m1")]
        mb = _make_mailbox()
        await _attach_runtime(inbox_service, mb, backend)

        await inbox_service._poll_runtime(inbox_service._runtimes[mb.id])
        received = [
            e for e in event_bus_svc.bus.published if e.event_type == "inbox.message.received"
        ]
        assert len(received) == 1
        assert received[0].data["mailbox_id"] == mb.id

    @pytest.mark.asyncio
    async def test_poll_isolation_between_mailboxes(
        self,
        inbox_service: InboxService,
        storage_svc: FakeStorageService,
    ) -> None:
        backend_a = FakeEmailBackend()
        backend_a.messages = [_make_message(message_id="a1", thread_id="tx")]
        backend_b = FakeEmailBackend()
        backend_b.messages = [_make_message(message_id="b1", thread_id="tx")]  # same thread_id
        mb_a = _make_mailbox(mailbox_id="mbx_a", email_address="a@example.com")
        mb_b = _make_mailbox(mailbox_id="mbx_b", email_address="b@example.com")
        await _attach_runtime(inbox_service, mb_a, backend_a)
        await _attach_runtime(inbox_service, mb_b, backend_b)

        await inbox_service._poll_runtime(inbox_service._runtimes[mb_a.id])
        await inbox_service._poll_runtime(inbox_service._runtimes[mb_b.id])

        set_current_user(_admin_user())
        # Even though both messages share thread_id, each is tagged to its
        # own mailbox — querying a mailbox's thread must not pull the other.
        thread_a = await inbox_service.get_thread("tx", mailbox_id="mbx_a")
        thread_b = await inbox_service.get_thread("tx", mailbox_id="mbx_b")

        # An admin bypasses mailbox membership, but the query itself is
        # still scoped to a single mailbox_id — no leakage.
        assert {m["_id"] for m in thread_a} == {"a1"}
        assert {m["_id"] for m in thread_b} == {"b1"}


# ── Message visibility ────────────────────────────────────────────


class TestMessageVisibility:
    @pytest.mark.asyncio
    async def test_search_scoped_to_accessible_mailboxes(
        self,
        inbox_service: InboxService,
        storage_svc: FakeStorageService,
    ) -> None:
        mb_owned = _make_mailbox(mailbox_id="mbx_owned")
        mb_other = _make_mailbox(mailbox_id="mbx_other", owner_user_id="someone_else")
        await _attach_runtime(inbox_service, mb_owned, FakeEmailBackend())
        await _attach_runtime(inbox_service, mb_other, FakeEmailBackend())

        await storage_svc.backend.put(
            "inbox_messages",
            "m_owned",
            {
                "mailbox_id": "mbx_owned",
                "subject": "A",
                "body_text": "x",
                "sender_email": "a@b.c",
                "date": "2026-04-05T00:00:00+00:00",
                "is_inbound": True,
                "thread_id": "t1",
            },
        )
        await storage_svc.backend.put(
            "inbox_messages",
            "m_other",
            {
                "mailbox_id": "mbx_other",
                "subject": "B",
                "body_text": "y",
                "sender_email": "c@d.e",
                "date": "2026-04-05T01:00:00+00:00",
                "is_inbound": True,
                "thread_id": "t2",
            },
        )

        set_current_user(_owner())
        results = await inbox_service.search_messages()
        ids = {r["_id"] for r in results}
        assert ids == {"m_owned"}  # m_other belongs to a mailbox owner can't see

    @pytest.mark.asyncio
    async def test_search_forbidden_mailbox_raises(
        self,
        inbox_service: InboxService,
        storage_svc: FakeStorageService,
    ) -> None:
        from gilbert.core.services.inbox import InboxPermissionError

        mb = _make_mailbox(owner_user_id="someone_else")
        await _attach_runtime(inbox_service, mb, FakeEmailBackend())

        set_current_user(_unrelated_user())
        with pytest.raises(InboxPermissionError):
            await inbox_service.search_messages(mailbox_id=mb.id)

    @pytest.mark.asyncio
    async def test_shared_user_can_read(
        self,
        inbox_service: InboxService,
        storage_svc: FakeStorageService,
    ) -> None:
        mb = _make_mailbox(shared_with_users=["alice"])
        await _attach_runtime(inbox_service, mb, FakeEmailBackend())
        await storage_svc.backend.put(
            "inbox_messages",
            "m1",
            {
                "mailbox_id": mb.id,
                "subject": "x",
                "body_text": "y",
                "sender_email": "a@b.c",
                "date": "2026-04-05T00:00:00+00:00",
                "is_inbound": True,
                "thread_id": "t1",
            },
        )
        set_current_user(_shared_user())
        results = await inbox_service.search_messages(mailbox_id=mb.id)
        assert len(results) == 1


# ── Outbox ────────────────────────────────────────────────────────


class TestOutbox:
    @pytest.mark.asyncio
    async def test_schedule_send_persists_pending(
        self,
        inbox_service: InboxService,
    ) -> None:
        mb = _make_mailbox()
        await _attach_runtime(inbox_service, mb, FakeEmailBackend())

        draft = OutboxDraft(
            to=[EmailAddress(email="x@y.z")],
            subject="s",
            body_html="<p>h</p>",
        )
        outbox_id = await inbox_service.schedule_send(mb.id, draft, _owner())

        set_current_user(_owner())
        entries = await inbox_service.list_outbox(mailbox_id=mb.id)
        assert len(entries) == 1
        assert entries[0].id == outbox_id
        assert entries[0].status == OutboxStatus.PENDING
        assert entries[0].created_by_user_id == "owner"

    @pytest.mark.asyncio
    async def test_shared_user_can_cancel_owners_draft(
        self,
        inbox_service: InboxService,
    ) -> None:
        mb = _make_mailbox(shared_with_users=["alice"])
        await _attach_runtime(inbox_service, mb, FakeEmailBackend())

        draft = OutboxDraft(
            to=[EmailAddress(email="x@y.z")],
            subject="s",
            body_html="<p>h</p>",
        )
        outbox_id = await inbox_service.schedule_send(mb.id, draft, _owner())

        # Alice has full access to the mailbox — by design she can cancel
        # drafts that the owner created.
        ok = await inbox_service.cancel_outbox(outbox_id, _shared_user())
        assert ok is True

        set_current_user(_owner())
        entries = await inbox_service.list_outbox(mailbox_id=mb.id)
        assert entries[0].status == OutboxStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_unrelated_user_cannot_cancel(
        self,
        inbox_service: InboxService,
    ) -> None:
        from gilbert.core.services.inbox import InboxPermissionError

        mb = _make_mailbox()
        await _attach_runtime(inbox_service, mb, FakeEmailBackend())

        draft = OutboxDraft(
            to=[EmailAddress(email="x@y.z")],
            subject="s",
            body_html="<p>h</p>",
        )
        outbox_id = await inbox_service.schedule_send(mb.id, draft, _owner())
        with pytest.raises(InboxPermissionError):
            await inbox_service.cancel_outbox(outbox_id, _unrelated_user())

    @pytest.mark.asyncio
    async def test_outbox_tick_sends_and_emits(
        self,
        inbox_service: InboxService,
        event_bus_svc: FakeEventBusService,
    ) -> None:
        backend = FakeEmailBackend()
        mb = _make_mailbox()
        await _attach_runtime(inbox_service, mb, backend)

        draft = OutboxDraft(
            to=[EmailAddress(email="x@y.z")],
            subject="hello",
            body_html="<p>hi</p>",
            body_text="hi",
        )
        await inbox_service.schedule_send(mb.id, draft, _owner())
        await inbox_service._outbox_tick()

        assert len(backend.sent) == 1
        assert backend.sent[0]["subject"] == "hello"
        assert any(
            e.event_type == "inbox.outbox.sent" and e.data["mailbox_id"] == mb.id
            for e in event_bus_svc.bus.published
        )

        set_current_user(_owner())
        entries = await inbox_service.list_outbox(mailbox_id=mb.id)
        assert entries[0].status == OutboxStatus.SENT

    @pytest.mark.asyncio
    async def test_outbox_tick_marks_failure(
        self,
        inbox_service: InboxService,
        event_bus_svc: FakeEventBusService,
    ) -> None:
        class BoomBackend(FakeEmailBackend):
            async def send(self, *args: Any, **kwargs: Any) -> str:
                raise RuntimeError("smtp down")

        backend = BoomBackend()
        mb = _make_mailbox()
        await _attach_runtime(inbox_service, mb, backend)

        draft = OutboxDraft(
            to=[EmailAddress(email="x@y.z")],
            subject="x",
            body_html="<p>x</p>",
        )
        await inbox_service.schedule_send(mb.id, draft, _owner())
        await inbox_service._outbox_tick()

        set_current_user(_owner())
        entries = await inbox_service.list_outbox(mailbox_id=mb.id)
        assert entries[0].status == OutboxStatus.FAILED
        assert "smtp down" in (entries[0].error or "")
        assert any(e.event_type == "inbox.outbox.failed" for e in event_bus_svc.bus.published)


# ── AI tool shape ─────────────────────────────────────────────────


class TestAiTools:
    @pytest.mark.asyncio
    async def test_inbox_mailboxes_lists_accessible(
        self,
        inbox_service: InboxService,
    ) -> None:
        mb_owned = _make_mailbox(mailbox_id="mbx_owned")
        mb_shared = _make_mailbox(
            mailbox_id="mbx_shared",
            owner_user_id="someone",
            shared_with_users=["owner"],
        )
        mb_hidden = _make_mailbox(mailbox_id="mbx_hidden", owner_user_id="someone")
        await _attach_runtime(inbox_service, mb_owned, FakeEmailBackend())
        await _attach_runtime(inbox_service, mb_shared, FakeEmailBackend())
        await _attach_runtime(inbox_service, mb_hidden, FakeEmailBackend())

        set_current_user(_owner())
        result = await inbox_service.execute_tool("inbox_mailboxes", {})
        assert "mbx_owned" in result
        assert "mbx_shared" in result
        assert "mbx_hidden" not in result

    @pytest.mark.asyncio
    async def test_search_requires_mailbox_id(
        self,
        inbox_service: InboxService,
    ) -> None:
        set_current_user(_owner())
        result = await inbox_service.execute_tool("inbox_search", {})
        assert "mailbox_id is required" in result
        assert "/inbox mailboxes" in result

    @pytest.mark.asyncio
    async def test_search_forbidden_mailbox_gives_helpful_error(
        self,
        inbox_service: InboxService,
    ) -> None:
        mb = _make_mailbox(owner_user_id="someone_else")
        await _attach_runtime(inbox_service, mb, FakeEmailBackend())
        set_current_user(_unrelated_user())
        result = await inbox_service.execute_tool(
            "inbox_search",
            {"mailbox_id": mb.id},
        )
        assert "don't have access" in result
        assert "/inbox mailboxes" in result


# ── Workspace attachment resolution ───────────────────────────────


class TestWorkspaceAttachments:
    """The inbox tools should accept ``workspace:<skill>/<path>`` refs in
    addition to knowledge document IDs, resolve them via SkillService,
    and surface failures explicitly so the AI can't claim a successful
    send when an attachment failed to attach."""

    @pytest.mark.asyncio
    async def test_normalize_short_workspace_ref_expanded(self) -> None:
        """A short ``workspace:<skill>/<path>`` ref is rewritten to the
        full self-contained URI form using injected user/conv ids."""
        out = InboxService._normalize_attach_refs(
            ["workspace:pdf/po-1.pdf"],
            injected_user_id="usr_brian",
            injected_conv_id="conv_xyz",
        )
        assert out == ["workspace:usr_brian/conv_xyz/pdf/po-1.pdf"]

    @pytest.mark.asyncio
    async def test_normalize_full_uri_passthrough(self) -> None:
        """Already-full workspace URIs and knowledge IDs pass through
        the normalizer untouched."""
        out = InboxService._normalize_attach_refs(
            [
                "workspace:usr_a/conv_b/pdf/file.pdf",
                "local_docs:reports/march.pdf",
            ],
            injected_user_id="usr_brian",
            injected_conv_id="conv_xyz",
        )
        assert out == [
            "workspace:usr_a/conv_b/pdf/file.pdf",
            "local_docs:reports/march.pdf",
        ]

    @pytest.mark.asyncio
    async def test_resolve_workspace_attachment_reads_bytes(
        self,
        inbox_service: InboxService,
        tmp_path: Any,
    ) -> None:
        """Pointing a workspace ref at a real file on disk produces an
        EmailAttachment with the right filename, bytes, and mime type."""
        # Build a minimal fake SkillService whose
        # ``_resolve_workspace_file`` returns the staged file.
        staged = tmp_path / "po.pdf"
        staged.write_bytes(b"%PDF-1.4 fake")

        class FakeWorkspace:
            def resolve_file_path(self, user_id, rel_path, conversation_id):
                return staged, None

        inbox_service._get_workspace_service = lambda: FakeWorkspace()

        atts, errs = await inbox_service._resolve_attachments(
            ["workspace:usr_a/conv_b/pdf/po.pdf"]
        )
        assert errs == []
        assert len(atts) == 1
        assert atts[0].filename == "po.pdf"
        assert atts[0].data == b"%PDF-1.4 fake"
        assert atts[0].mime_type == "application/pdf"

    @pytest.mark.asyncio
    async def test_resolve_workspace_attachment_missing_file(
        self,
        inbox_service: InboxService,
    ) -> None:
        """A workspace ref that doesn't resolve produces an error
        string instead of silently dropping."""

        class FakeWorkspace:
            def resolve_file_path(self, user_id, rel_path, conversation_id):
                return None, "File not found: po.pdf"

        inbox_service._get_workspace_service = lambda: FakeWorkspace()

        atts, errs = await inbox_service._resolve_attachments(
            ["workspace:usr_a/conv_b/pdf/po.pdf"]
        )
        assert atts == []
        assert len(errs) == 1
        assert "po.pdf" in errs[0]
        assert "not found" in errs[0].lower()

    @pytest.mark.asyncio
    async def test_resolve_workspace_attachment_invalid_uri(
        self,
        inbox_service: InboxService,
    ) -> None:
        """A workspace URI with too few segments errors out instead of
        crashing."""
        class FakeWorkspace:
            def resolve_file_path(self, user_id, rel_path, conversation_id):
                return None, "should not be called"

        inbox_service._get_workspace_service = lambda: FakeWorkspace()
        atts, errs = await inbox_service._resolve_attachments(
            ["workspace:not_enough_segments"]
        )
        assert atts == []
        assert len(errs) == 1
        assert "invalid" in errs[0].lower()

    @pytest.mark.asyncio
    async def test_workspace_capability_resolved_lazily(
        self,
        inbox_service: InboxService,
        tmp_path: Any,
    ) -> None:
        """WorkspaceService might start AFTER InboxService — the
        topological sort only orders by ``requires``, not ``optional``.
        The workspace capability must be looked up at call time, not at
        start time, so a late-started WorkspaceService is still usable
        for workspace attachment resolution.
        """
        staged = tmp_path / "po.pdf"
        staged.write_bytes(b"%PDF-1.4 fake")

        class FakeWorkspace:
            def resolve_file_path(self, user_id, rel_path, conversation_id):
                return staged, None

            def get_workspace_root(self, user_id, conversation_id):
                return tmp_path

            def get_upload_dir(self, user_id, conversation_id):
                return tmp_path

            def get_output_dir(self, user_id, conversation_id):
                return tmp_path

            def get_scratch_dir(self, user_id, conversation_id):
                return tmp_path

            async def register_file(self, **kwargs):
                return {}

            async def list_files(self, conversation_id, category=None):
                return []

            async def build_workspace_manifest(self, conversation_id):
                return ""

            async def resolve_deliverable_for_dependent(
                self, *, file_id, viewing_agent_id, viewing_goal_id,
            ):
                # Phase 5 — protocol stub; not exercised here.
                return None, "not supported"

        # Simulate the start-order race: at start time, the resolver
        # returns None for "workspace" (not started yet). Later, when
        # the AI fires inbox_send, workspace is ready — the lazy lookup
        # picks it up.
        workspace_ready = [False]
        fake_workspace = FakeWorkspace()

        class LazyResolver:
            def get_capability(self, cap):
                if cap == "workspace" and workspace_ready[0]:
                    return fake_workspace
                return None

            def require_capability(self, cap):
                svc = self.get_capability(cap)
                if svc is None:
                    raise LookupError(cap)
                return svc

            def get_all(self, cap):
                return []

        inbox_service._resolver = LazyResolver()
        # First call: workspace isn't ready yet.
        atts, errs = await inbox_service._resolve_attachments(
            ["workspace:usr_a/conv_b/pdf/po.pdf"]
        )
        assert errs and "workspace service not available" in errs[0]
        # Now WorkspaceService starts.
        workspace_ready[0] = True
        # Same call now succeeds.
        atts, errs = await inbox_service._resolve_attachments(
            ["workspace:usr_a/conv_b/pdf/po.pdf"]
        )
        assert errs == []
        assert len(atts) == 1
        assert atts[0].filename == "po.pdf"

    @pytest.mark.asyncio
    async def test_inbox_send_fails_loudly_on_unresolved_attachment(
        self,
        inbox_service: InboxService,
    ) -> None:
        """If any attachment in inbox_send fails to resolve, the email
        is NOT sent and the tool returns an error so the AI sees it."""
        mb = _make_mailbox(mailbox_id="mbx_send")
        await _attach_runtime(inbox_service, mb, FakeEmailBackend())

        class FakeWorkspace:
            def resolve_file_path(self, user_id, rel_path, conversation_id):
                return None, "File not found: missing.pdf"

        inbox_service._get_workspace_service = lambda: FakeWorkspace()
        set_current_user(_owner())

        result = await inbox_service.execute_tool(
            "inbox_send",
            {
                "mailbox_id": mb.id,
                "to": ["recipient@example.com"],
                "subject": "Here it is",
                "body_html": "<p>Attached.</p>",
                "attach_documents": ["workspace:pdf/missing.pdf"],
                "_user_id": "owner",
                "_conversation_id": "conv_xyz",
            },
        )
        assert "Could not attach" in result
        assert "NOT sent" in result
        # And the backend was never asked to send.
        runtime = inbox_service._runtimes[mb.id]
        assert getattr(runtime.backend, "sent", []) == []
