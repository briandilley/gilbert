"""Tests for InboxAIChatService — email-to-AI conversations."""

from typing import Any

import pytest

from gilbert.config import InboxAIChatConfig
from gilbert.core.services.inbox_ai_chat import (
    InboxAIChatService,
    markdown_to_html,
    strip_quoted_text,
)
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.email import EmailAttachment
from gilbert.interfaces.events import Event


# ── Fakes ──────────────────────────────────────────────────────


class FakeInboxService:
    """Fake InboxService for testing."""

    def __init__(self) -> None:
        self.messages: dict[str, dict[str, Any]] = {}
        self.replies: list[dict[str, Any]] = []

    def service_info(self) -> Any:
        from gilbert.interfaces.service import ServiceInfo
        return ServiceInfo(name="inbox", capabilities=frozenset({"email"}))

    async def get_message(self, message_id: str) -> dict[str, Any] | None:
        return self.messages.get(message_id)

    async def reply_to_message(
        self,
        message_id: str,
        body_html: str,
        body_text: str = "",
        attachments: list[EmailAttachment] | None = None,
    ) -> str:
        self.replies.append({
            "message_id": message_id,
            "body_html": body_html,
            "body_text": body_text,
            "attachments": attachments,
        })
        return f"reply_{message_id}"


class FakeAIService:
    """Fake AIService for testing."""

    def __init__(self) -> None:
        self.chats: list[dict[str, Any]] = []
        self.response_text = "Hello! How can I help?"
        self._conv_counter = 0

    def service_info(self) -> Any:
        from gilbert.interfaces.service import ServiceInfo
        return ServiceInfo(name="ai", capabilities=frozenset({"ai"}))

    async def chat(
        self,
        user_message: str,
        conversation_id: str | None = None,
        user_ctx: UserContext | None = None,
    ) -> tuple[str, str]:
        conv_id = conversation_id or f"conv_{self._conv_counter}"
        self._conv_counter += 1
        self.chats.append({
            "message": user_message,
            "conversation_id": conv_id,
            "user_ctx": user_ctx,
        })
        return self.response_text, conv_id


class FakeStorageBackend:
    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict[str, Any]]] = {}

    async def get(self, collection: str, key: str) -> dict[str, Any] | None:
        record = self._data.get(collection, {}).get(key)
        if record is not None:
            return {**record, "_id": key}
        return None

    async def put(self, collection: str, key: str, data: dict[str, Any]) -> None:
        self._data.setdefault(collection, {})[key] = data


class FakeStorageService:
    def __init__(self) -> None:
        self.backend = FakeStorageBackend()

    def service_info(self) -> Any:
        from gilbert.interfaces.service import ServiceInfo
        return ServiceInfo(name="storage", capabilities=frozenset({"entity_storage"}))


class FakeUserService:
    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}

    def service_info(self) -> Any:
        from gilbert.interfaces.service import ServiceInfo
        return ServiceInfo(name="users", capabilities=frozenset({"users"}))

    async def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        return self.users.get(email.lower())


class FakeEventBus:
    def __init__(self) -> None:
        self.handlers: dict[str, list[Any]] = {}

    def subscribe(self, event_type: str, handler: Any) -> Any:
        self.handlers.setdefault(event_type, []).append(handler)
        return lambda: self.handlers[event_type].remove(handler)

    async def publish(self, event: Any) -> None:
        for handler in self.handlers.get(event.event_type, []):
            await handler(event)


class FakeEventBusService:
    def __init__(self) -> None:
        self.bus = FakeEventBus()

    def service_info(self) -> Any:
        from gilbert.interfaces.service import ServiceInfo
        return ServiceInfo(name="event_bus", capabilities=frozenset({"event_bus"}))


class FakeDocumentContent:
    """Fake DocumentContent for testing."""

    def __init__(self, name: str, data: bytes, mime_type: str = "application/pdf") -> None:
        self.meta = type("Meta", (), {
            "name": name,
            "mime_type": mime_type,
        })()
        self.data = data


class FakeDocumentBackend:
    """Fake DocumentBackend for testing."""

    def __init__(self) -> None:
        self.documents: dict[str, FakeDocumentContent] = {}

    @property
    def source_id(self) -> str:
        return "local"

    @property
    def display_name(self) -> str:
        return "Local Documents"

    @property
    def read_only(self) -> bool:
        return True

    async def get_document(self, path: str) -> FakeDocumentContent | None:
        return self.documents.get(path)


class FakeKnowledgeService:
    """Fake KnowledgeService for testing."""

    def __init__(self) -> None:
        self._backends: dict[str, FakeDocumentBackend] = {}

    def service_info(self) -> Any:
        from gilbert.interfaces.service import ServiceInfo
        return ServiceInfo(name="knowledge", capabilities=frozenset({"knowledge"}))

    @property
    def backends(self) -> dict[str, FakeDocumentBackend]:
        return dict(self._backends)

    def _resolve_backend(self, document_id: str) -> tuple[FakeDocumentBackend, str]:
        for sid, backend in self._backends.items():
            prefix = sid + ":"
            if document_id.startswith(prefix):
                return backend, document_id[len(prefix):]
        raise KeyError(f"No backend found for document: {document_id}")


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


# ── Fixtures ───────────────────────────────────────────────────


def _make_config(**kwargs: Any) -> InboxAIChatConfig:
    defaults = {"enabled": True, "allowed_domains": ["example.com"]}
    defaults.update(kwargs)
    return InboxAIChatConfig(**defaults)


@pytest.fixture
def inbox_svc() -> FakeInboxService:
    svc = FakeInboxService()
    svc.messages["msg_001"] = {
        "_id": "msg_001",
        "message_id": "msg_001",
        "thread_id": "thread_001",
        "subject": "Hey Gilbert",
        "sender_email": "alice@example.com",
        "sender_name": "Alice",
        "body_text": "What's the weather like?",
        "is_inbound": True,
    }
    return svc


@pytest.fixture
def ai_svc() -> FakeAIService:
    return FakeAIService()


@pytest.fixture
def event_bus_svc() -> FakeEventBusService:
    return FakeEventBusService()


@pytest.fixture
def user_svc() -> FakeUserService:
    return FakeUserService()


@pytest.fixture
def knowledge_svc() -> FakeKnowledgeService:
    svc = FakeKnowledgeService()
    backend = FakeDocumentBackend()
    backend.documents["docs/report.pdf"] = FakeDocumentContent(
        name="report.pdf",
        data=b"%PDF-fake-content",
        mime_type="application/pdf",
    )
    backend.documents["docs/spreadsheet.xlsx"] = FakeDocumentContent(
        name="spreadsheet.xlsx",
        data=b"fake-xlsx-content",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    svc._backends["local"] = backend
    return svc


@pytest.fixture
def resolver(
    inbox_svc: FakeInboxService,
    ai_svc: FakeAIService,
    event_bus_svc: FakeEventBusService,
    user_svc: FakeUserService,
    knowledge_svc: FakeKnowledgeService,
) -> FakeResolver:
    r = FakeResolver()
    r.caps["email"] = inbox_svc
    r.caps["ai_chat"] = ai_svc
    r.caps["entity_storage"] = FakeStorageService()
    r.caps["event_bus"] = event_bus_svc
    r.caps["users"] = user_svc
    r.caps["knowledge"] = knowledge_svc
    return r


@pytest.fixture
async def service(resolver: FakeResolver) -> InboxAIChatService:
    svc = InboxAIChatService(_make_config())
    await svc.start(resolver)
    return svc


def _make_event(
    message_id: str = "msg_001",
    thread_id: str = "thread_001",
    sender_email: str = "alice@example.com",
    is_inbound: bool = True,
) -> Event:
    return Event(
        event_type="inbox.message.received",
        data={
            "message_id": message_id,
            "thread_id": thread_id,
            "subject": "Hey Gilbert",
            "sender_email": sender_email,
            "sender_name": "Alice",
            "is_inbound": is_inbound,
        },
        source="inbox",
    )


# ── Tests ──────────────────────────────────────────────────────


class TestServiceInfo:
    def test_service_info(self) -> None:
        svc = InboxAIChatService(_make_config())
        info = svc.service_info()
        assert info.name == "inbox_ai_chat"
        assert "email_ai_chat" in info.capabilities
        assert "ai_tools" in info.capabilities
        assert "email" in info.requires
        assert "ai_chat" in info.requires


class TestAllowlist:
    def test_allowed_by_domain(self) -> None:
        svc = InboxAIChatService(_make_config(allowed_domains=["example.com"]))
        assert svc._is_allowed("alice@example.com")
        assert not svc._is_allowed("alice@other.com")

    def test_allowed_by_email(self) -> None:
        svc = InboxAIChatService(_make_config(
            allowed_emails=["special@other.com"], allowed_domains=[],
        ))
        assert svc._is_allowed("special@other.com")
        assert not svc._is_allowed("alice@other.com")

    def test_case_insensitive(self) -> None:
        svc = InboxAIChatService(_make_config(
            allowed_emails=["Alice@Example.com"], allowed_domains=["CORP.COM"],
        ))
        assert svc._is_allowed("alice@example.com")
        assert svc._is_allowed("bob@corp.com")

    def test_empty_allowlist_blocks_all(self) -> None:
        svc = InboxAIChatService(_make_config(allowed_emails=[], allowed_domains=[]))
        assert not svc._is_allowed("anyone@anywhere.com")


class TestEventHandling:
    @pytest.mark.asyncio
    async def test_processes_allowed_inbound(
        self, service: InboxAIChatService, inbox_svc: FakeInboxService,
        ai_svc: FakeAIService, event_bus_svc: FakeEventBusService,
    ) -> None:
        await event_bus_svc.bus.publish(_make_event())

        assert len(ai_svc.chats) == 1
        # The user message includes an [EMAIL CONTEXT] prefix
        assert "What's the weather like?" in ai_svc.chats[0]["message"]
        assert "[EMAIL CONTEXT" in ai_svc.chats[0]["message"]
        assert len(inbox_svc.replies) == 1
        assert "Hello! How can I help?" in inbox_svc.replies[0]["body_text"]

    @pytest.mark.asyncio
    async def test_skips_outbound(
        self, service: InboxAIChatService, ai_svc: FakeAIService,
        event_bus_svc: FakeEventBusService,
    ) -> None:
        await event_bus_svc.bus.publish(_make_event(is_inbound=False))
        assert len(ai_svc.chats) == 0

    @pytest.mark.asyncio
    async def test_skips_disallowed_sender(
        self, service: InboxAIChatService, ai_svc: FakeAIService,
        inbox_svc: FakeInboxService, event_bus_svc: FakeEventBusService,
    ) -> None:
        inbox_svc.messages["msg_bad"] = {
            "_id": "msg_bad",
            "body_text": "Hello",
            "sender_email": "hacker@evil.com",
            "sender_name": "Hacker",
            "thread_id": "thread_bad",
        }
        await event_bus_svc.bus.publish(_make_event(
            message_id="msg_bad", sender_email="hacker@evil.com",
        ))
        assert len(ai_svc.chats) == 0

    @pytest.mark.asyncio
    async def test_skips_empty_body_after_stripping(
        self, service: InboxAIChatService, ai_svc: FakeAIService,
        inbox_svc: FakeInboxService, event_bus_svc: FakeEventBusService,
    ) -> None:
        inbox_svc.messages["msg_empty"] = {
            "_id": "msg_empty",
            "body_text": "\n> quoted line\n> another\n",
            "sender_email": "alice@example.com",
            "sender_name": "Alice",
            "thread_id": "thread_empty",
        }
        await event_bus_svc.bus.publish(_make_event(message_id="msg_empty"))
        assert len(ai_svc.chats) == 0


class TestConversationContinuity:
    @pytest.mark.asyncio
    async def test_same_thread_continues_conversation(
        self, service: InboxAIChatService, inbox_svc: FakeInboxService,
        ai_svc: FakeAIService, event_bus_svc: FakeEventBusService,
    ) -> None:
        # First message in thread
        await event_bus_svc.bus.publish(_make_event())
        conv_id_1 = ai_svc.chats[0]["conversation_id"]

        # Second message in same thread
        inbox_svc.messages["msg_002"] = {
            "_id": "msg_002",
            "body_text": "Follow up question",
            "sender_email": "alice@example.com",
            "sender_name": "Alice",
            "thread_id": "thread_001",
        }
        await event_bus_svc.bus.publish(_make_event(message_id="msg_002"))

        assert len(ai_svc.chats) == 2
        assert ai_svc.chats[1]["conversation_id"] == conv_id_1

    @pytest.mark.asyncio
    async def test_different_threads_get_different_conversations(
        self, service: InboxAIChatService, inbox_svc: FakeInboxService,
        ai_svc: FakeAIService, event_bus_svc: FakeEventBusService,
    ) -> None:
        await event_bus_svc.bus.publish(_make_event())

        inbox_svc.messages["msg_other"] = {
            "_id": "msg_other",
            "body_text": "Different topic",
            "sender_email": "alice@example.com",
            "sender_name": "Alice",
            "thread_id": "thread_other",
        }
        await event_bus_svc.bus.publish(_make_event(
            message_id="msg_other", thread_id="thread_other",
        ))

        assert len(ai_svc.chats) == 2
        assert ai_svc.chats[0]["conversation_id"] != ai_svc.chats[1]["conversation_id"]


class TestUserResolution:
    @pytest.mark.asyncio
    async def test_resolves_known_user(
        self, service: InboxAIChatService, ai_svc: FakeAIService,
        user_svc: FakeUserService, event_bus_svc: FakeEventBusService,
    ) -> None:
        user_svc.users["alice@example.com"] = {
            "_id": "user_alice",
            "email": "alice@example.com",
            "display_name": "Alice Smith",
            "roles": ["admin"],
        }
        await event_bus_svc.bus.publish(_make_event())

        ctx = ai_svc.chats[0]["user_ctx"]
        assert ctx.user_id == "user_alice"
        assert ctx.display_name == "Alice Smith"
        assert "admin" in ctx.roles

    @pytest.mark.asyncio
    async def test_unknown_user_gets_default_context(
        self, service: InboxAIChatService, ai_svc: FakeAIService,
        event_bus_svc: FakeEventBusService,
    ) -> None:
        await event_bus_svc.bus.publish(_make_event())

        ctx = ai_svc.chats[0]["user_ctx"]
        assert ctx.user_id == "alice@example.com"
        assert ctx.email == "alice@example.com"
        assert "user" in ctx.roles


class TestStripQuotedText:
    def test_gmail_style(self) -> None:
        body = "My reply\n\nOn Mon, Jan 1, 2026 at 12:00 PM Alice <alice@example.com> wrote:\n> Original text"
        assert strip_quoted_text(body) == "My reply"

    def test_outlook_style(self) -> None:
        body = "My reply\n\n-----Original Message-----\nFrom: Alice"
        assert strip_quoted_text(body) == "My reply"

    def test_from_header(self) -> None:
        body = "My reply\n\nFrom: Alice <alice@example.com>\nSent: Monday"
        assert strip_quoted_text(body) == "My reply"

    def test_trailing_quotes(self) -> None:
        body = "My reply\n> quoted\n> more quoted"
        result = strip_quoted_text(body)
        assert "My reply" in result
        assert "> quoted" not in result

    def test_no_quotes(self) -> None:
        body = "Just a plain message"
        assert strip_quoted_text(body) == "Just a plain message"

    def test_empty(self) -> None:
        assert strip_quoted_text("") == ""


class TestMarkdownToHtml:
    def test_basic_conversion(self) -> None:
        html = markdown_to_html("**bold** and *italic*")
        assert "<strong>bold</strong>" in html
        assert "<em>italic</em>" in html

    def test_wraps_in_styled_div(self) -> None:
        html = markdown_to_html("hello")
        assert html.startswith('<div style="')
        assert html.endswith("</div>")


class TestToolProvider:
    def test_tool_provider_name(self) -> None:
        svc = InboxAIChatService(_make_config())
        assert svc.tool_provider_name == "inbox_ai_chat"

    def test_get_tools_returns_email_attach(self) -> None:
        svc = InboxAIChatService(_make_config())
        tools = svc.get_tools()
        assert len(tools) == 1
        assert tools[0].name == "email_attach"


class TestEmailAttachTool:
    @pytest.mark.asyncio
    async def test_attach_resolves_document(
        self, service: InboxAIChatService,
    ) -> None:
        result = await service.execute_tool("email_attach", {
            "document_id": "local:docs/report.pdf",
        })
        assert "report.pdf" in result
        assert "Queued" in result
        assert len(service._pending_attachments) == 1
        att = service._pending_attachments[0]
        assert att.filename == "report.pdf"
        assert att.data == b"%PDF-fake-content"
        assert att.mime_type == "application/pdf"

    @pytest.mark.asyncio
    async def test_attach_multiple_documents(
        self, service: InboxAIChatService,
    ) -> None:
        await service.execute_tool("email_attach", {
            "document_id": "local:docs/report.pdf",
        })
        await service.execute_tool("email_attach", {
            "document_id": "local:docs/spreadsheet.xlsx",
        })
        assert len(service._pending_attachments) == 2
        names = {a.filename for a in service._pending_attachments}
        assert names == {"report.pdf", "spreadsheet.xlsx"}

    @pytest.mark.asyncio
    async def test_attach_missing_document(
        self, service: InboxAIChatService,
    ) -> None:
        result = await service.execute_tool("email_attach", {
            "document_id": "local:docs/nonexistent.pdf",
        })
        assert "not found" in result.lower()
        assert len(service._pending_attachments) == 0

    @pytest.mark.asyncio
    async def test_attach_unknown_backend(
        self, service: InboxAIChatService,
    ) -> None:
        result = await service.execute_tool("email_attach", {
            "document_id": "unknown_source:docs/file.pdf",
        })
        assert "no backend" in result.lower()

    @pytest.mark.asyncio
    async def test_attach_empty_document_id(
        self, service: InboxAIChatService,
    ) -> None:
        result = await service.execute_tool("email_attach", {"document_id": ""})
        assert "required" in result.lower()

    @pytest.mark.asyncio
    async def test_attach_no_knowledge_service(self) -> None:
        """Service without knowledge should return an error."""
        r = FakeResolver()
        r.caps["email"] = FakeInboxService()
        r.caps["ai_chat"] = FakeAIService()
        r.caps["entity_storage"] = FakeStorageService()
        svc = InboxAIChatService(_make_config())
        await svc.start(r)

        result = await svc.execute_tool("email_attach", {
            "document_id": "local:docs/report.pdf",
        })
        assert "not available" in result.lower()


class TestAttachmentInReply:
    @pytest.mark.asyncio
    async def test_reply_includes_no_attachments_by_default(
        self, service: InboxAIChatService, inbox_svc: FakeInboxService,
        event_bus_svc: FakeEventBusService,
    ) -> None:
        await event_bus_svc.bus.publish(_make_event())
        assert len(inbox_svc.replies) == 1
        assert inbox_svc.replies[0]["attachments"] is None

    @pytest.mark.asyncio
    async def test_reply_includes_queued_attachments(
        self, service: InboxAIChatService, inbox_svc: FakeInboxService,
        ai_svc: FakeAIService, event_bus_svc: FakeEventBusService,
    ) -> None:
        # Simulate the AI queueing an attachment during chat by
        # manually calling the tool before the reply is assembled.
        # In real usage, the AI calls email_attach during the agentic loop.
        original_chat = ai_svc.chat

        async def chat_with_attach(
            user_message: str,
            conversation_id: str | None = None,
            user_ctx: UserContext | None = None,
        ) -> tuple[str, str]:
            # Simulate AI calling email_attach tool during chat
            await service.execute_tool("email_attach", {
                "document_id": "local:docs/report.pdf",
            })
            return await original_chat(user_message, conversation_id, user_ctx)

        ai_svc.chat = chat_with_attach

        await event_bus_svc.bus.publish(_make_event())

        assert len(inbox_svc.replies) == 1
        attachments = inbox_svc.replies[0]["attachments"]
        assert attachments is not None
        assert len(attachments) == 1
        assert attachments[0].filename == "report.pdf"

    @pytest.mark.asyncio
    async def test_attachments_cleared_between_messages(
        self, service: InboxAIChatService, inbox_svc: FakeInboxService,
        ai_svc: FakeAIService, event_bus_svc: FakeEventBusService,
    ) -> None:
        # First message: AI attaches a file
        original_chat = ai_svc.chat
        call_count = 0

        async def chat_sometimes_attach(
            user_message: str,
            conversation_id: str | None = None,
            user_ctx: UserContext | None = None,
        ) -> tuple[str, str]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                await service.execute_tool("email_attach", {
                    "document_id": "local:docs/report.pdf",
                })
            return await original_chat(user_message, conversation_id, user_ctx)

        ai_svc.chat = chat_sometimes_attach

        await event_bus_svc.bus.publish(_make_event())
        assert inbox_svc.replies[0]["attachments"] is not None

        # Second message: AI does NOT attach anything
        inbox_svc.messages["msg_002"] = {
            "_id": "msg_002",
            "body_text": "Thanks!",
            "sender_email": "alice@example.com",
            "sender_name": "Alice",
            "thread_id": "thread_001",
        }
        await event_bus_svc.bus.publish(_make_event(message_id="msg_002"))
        assert inbox_svc.replies[1]["attachments"] is None
