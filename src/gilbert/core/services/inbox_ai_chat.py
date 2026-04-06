"""Inbox AI chat service — email-to-AI conversations.

Subscribes to inbox.message.received events. When an allowed sender
emails Gilbert, runs the message through the AI service and replies
with the response. Email threads map to AI conversations for continuity.

Also acts as a ToolProvider, exposing an ``email_attach`` tool so the AI
can attach knowledge-store documents to its email reply instead of
sending a separate message.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

import markdown

from gilbert.config import InboxAIChatConfig
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.email import EmailAttachment
from gilbert.interfaces.events import Event
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)

logger = logging.getLogger(__name__)

_THREAD_COLLECTION = "inbox_ai_chat_threads"


class InboxAIChatService(Service):
    """Bridges email and AI — conversations over email.

    Capabilities: email_ai_chat, ai_tools

    Also implements the ToolProvider protocol so the AI can queue
    file attachments for the reply via the ``email_attach`` tool.
    """

    def __init__(self, config: InboxAIChatConfig) -> None:
        self._allowed_emails = [e.lower() for e in config.allowed_emails]
        self._allowed_domains = [d.lower().lstrip("@") for d in config.allowed_domains]

        self._inbox: Any = None  # InboxService
        self._ai: Any = None  # AIService
        self._user_svc: Any = None  # UserService (optional)
        self._storage: Any = None  # StorageBackend
        self._knowledge: Any = None  # KnowledgeService (optional)
        self._event_bus: Any = None  # EventBus
        self._unsubscribe: Any = None

        # Per-request pending attachments, protected by a lock so
        # concurrent _process_message calls don't mix attachments.
        self._pending_attachments: list[EmailAttachment] = []
        self._process_lock = asyncio.Lock()

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="inbox_ai_chat",
            capabilities=frozenset({"email_ai_chat", "ai_tools"}),
            requires=frozenset({"email", "ai_chat", "entity_storage"}),
            optional=frozenset({"event_bus", "users", "knowledge"}),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        self._inbox = resolver.require_capability("email")
        self._ai = resolver.require_capability("ai_chat")

        storage_svc = resolver.require_capability("entity_storage")
        self._storage = getattr(storage_svc, "backend", storage_svc)

        self._user_svc = resolver.get_capability("users")
        self._knowledge = resolver.get_capability("knowledge")

        event_bus_svc = resolver.get_capability("event_bus")
        if event_bus_svc:
            self._event_bus = getattr(event_bus_svc, "bus", event_bus_svc)
            self._unsubscribe = self._event_bus.subscribe(
                "inbox.message.received", self._on_message_received,
            )

        logger.info(
            "Inbox AI chat started (allowed_emails=%d, allowed_domains=%d)",
            len(self._allowed_emails),
            len(self._allowed_domains),
        )

    async def stop(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
        logger.info("Inbox AI chat stopped")

    # ── Event handler ──────────────────────────────────────────

    async def _on_message_received(self, event: Event) -> None:
        """Handle a new inbox message."""
        data = event.data

        # Skip outbound messages
        if not data.get("is_inbound", True):
            return

        sender_email = data.get("sender_email", "")
        if not self._is_allowed(sender_email):
            return

        message_id = data.get("message_id", "")
        thread_id = data.get("thread_id", "")

        try:
            await self._process_message(message_id, thread_id, sender_email)
        except Exception:
            logger.exception(
                "Failed to process email AI chat: message=%s sender=%s",
                message_id, sender_email,
            )

    async def _process_message(
        self, message_id: str, thread_id: str, sender_email: str,
    ) -> None:
        """Process a single inbound message: AI chat + reply.

        Uses a lock so that ``_pending_attachments`` is never shared
        between concurrent message processing tasks.
        """
        async with self._process_lock:
            await self._process_message_locked(message_id, thread_id, sender_email)

    async def _process_message_locked(
        self, message_id: str, thread_id: str, sender_email: str,
    ) -> None:
        """Inner processing — must be called under ``_process_lock``."""
        # Get full message
        record = await self._inbox.get_message(message_id)
        if not record:
            logger.warning("Message %s not found in inbox", message_id)
            return

        # Strip quoted reply text
        body = strip_quoted_text(record.get("body_text", ""))
        if not body.strip():
            logger.debug("Skipping empty body after quote stripping: %s", message_id)
            return

        # Resolve sender to UserContext
        user_ctx = await self._resolve_user(sender_email, record.get("sender_name", ""))

        # Look up existing conversation for this thread
        conversation_id = await self._get_conversation_id(thread_id)

        # Clear pending attachments before the AI runs
        self._pending_attachments = []

        # Inject email context so the AI knows how to handle attachments.
        # This tells it to use email_attach instead of inbox_send/inbox_reply.
        context_prefix = (
            "[EMAIL CONTEXT: You are replying to an email. Your text response "
            "will be sent as a reply in the existing email thread automatically. "
            "Do NOT use inbox_send or inbox_reply tools — your response IS the "
            "reply. If you need to attach files or documents, use the "
            "email_attach tool to queue them for this reply. "
            "You may attach multiple documents by calling email_attach "
            "multiple times.]\n\n"
        )

        # Run through AI
        response_text, conv_id = await self._ai.chat(
            user_message=context_prefix + body,
            conversation_id=conversation_id,
            user_ctx=user_ctx,
        )

        # Collect any attachments the AI queued via the email_attach tool
        attachments = list(self._pending_attachments)
        self._pending_attachments = []

        # Store thread → conversation mapping
        await self._set_conversation_id(thread_id, conv_id, sender_email)

        # Convert response to HTML and reply
        body_html = markdown_to_html(response_text)
        await self._inbox.reply_to_message(
            message_id=message_id,
            body_html=body_html,
            body_text=response_text,
            attachments=attachments or None,
        )

        att_msg = f" with {len(attachments)} attachment(s)" if attachments else ""
        logger.info(
            "Email AI chat: replied to %s%s (thread=%s, conv=%s)",
            sender_email, att_msg, thread_id, conv_id,
        )

    # ── Allowlist ──────────────────────────────────────────────

    def _is_allowed(self, sender_email: str) -> bool:
        """Check if a sender is allowed to chat."""
        email_lower = sender_email.lower()

        if email_lower in self._allowed_emails:
            return True

        domain = email_lower.rsplit("@", 1)[-1] if "@" in email_lower else ""
        if domain in self._allowed_domains:
            return True

        return False

    # ── User resolution ────────────────────────────────────────

    async def _resolve_user(self, email: str, display_name: str) -> UserContext:
        """Resolve sender email to a UserContext."""
        if self._user_svc is not None:
            try:
                user = await self._user_svc.get_user_by_email(email)
                if user is not None:
                    return UserContext(
                        user_id=user.get("_id", email),
                        email=user.get("email", email),
                        display_name=user.get("display_name", display_name),
                        roles=frozenset(user.get("roles", ["user"])),
                        provider="email",
                    )
            except Exception:
                logger.debug("User lookup failed for %s", email)

        # Fallback: create a basic user context
        return UserContext(
            user_id=email,
            email=email,
            display_name=display_name or email.split("@")[0],
            roles=frozenset({"user"}),
            provider="email",
        )

    # ── ToolProvider protocol ─────────────────────────────────

    @property
    def tool_provider_name(self) -> str:
        return "inbox_ai_chat"

    def get_tools(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="email_attach",
                description=(
                    "Attach a document from the knowledge store to the current "
                    "email reply. Call this once per document you want to attach. "
                    "The attachment will be included when your reply is sent. "
                    "Do NOT use inbox_send or inbox_reply to send attachments — "
                    "use this tool instead."
                ),
                parameters=[
                    ToolParameter(
                        name="document_id",
                        type=ToolParameterType.STRING,
                        description=(
                            "Knowledge store document ID (source_id:path), "
                            "e.g. 'local:docs/report.pdf' or 'gdrive-work:Quarterly Report.pdf'."
                        ),
                    ),
                ],
                required_role="user",
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        match name:
            case "email_attach":
                return await self._tool_email_attach(arguments)
            case _:
                raise KeyError(f"Unknown tool: {name}")

    async def _tool_email_attach(self, arguments: dict[str, Any]) -> str:
        """Resolve a document and queue it as a pending attachment."""
        document_id = arguments.get("document_id", "")
        if not document_id:
            return "Error: document_id is required."

        if self._knowledge is None:
            return "Error: knowledge service is not available — cannot resolve documents."

        try:
            backend, path = self._knowledge._resolve_backend(document_id)
        except KeyError:
            return f"Error: no backend found for document '{document_id}'."

        content = await backend.get_document(path)
        if content is None:
            return f"Error: document not found: {document_id}"

        attachment = EmailAttachment(
            filename=content.meta.name,
            data=content.data,
            mime_type=content.meta.mime_type or "application/octet-stream",
        )
        self._pending_attachments.append(attachment)

        size_kb = len(content.data) / 1024
        return (
            f"Queued attachment: {content.meta.name} "
            f"({size_kb:.1f} KB, {attachment.mime_type}). "
            f"It will be included in your email reply."
        )

    # ── Thread → conversation mapping ──────────────────────────

    async def _get_conversation_id(self, thread_id: str) -> str | None:
        """Look up the AI conversation ID for an email thread."""
        record = await self._storage.get(_THREAD_COLLECTION, thread_id)
        if record:
            return record.get("conversation_id")
        return None

    async def _set_conversation_id(
        self, thread_id: str, conversation_id: str, sender_email: str,
    ) -> None:
        """Store the thread → conversation mapping."""
        await self._storage.put(_THREAD_COLLECTION, thread_id, {
            "thread_id": thread_id,
            "conversation_id": conversation_id,
            "sender_email": sender_email,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })


# ── Utilities ──────────────────────────────────────────────────


def strip_quoted_text(body: str) -> str:
    """Strip quoted reply text from an email body.

    Handles:
    - Gmail: "On <date> <name> wrote:" blocks
    - Outlook: "-----Original Message-----" separator
    - Apple Mail / generic: lines starting with ">"
    """
    # Gmail-style: "On Mon, Jan 1, 2026 at 12:00 PM Name <email> wrote:"
    match = re.search(r"\nOn .+wrote:\s*\n", body)
    if match:
        return body[: match.start()].rstrip()

    # Outlook-style separator
    match = re.search(r"\n-{3,}\s*Original Message\s*-{3,}", body, re.IGNORECASE)
    if match:
        return body[: match.start()].rstrip()

    # Generic "From:" header block (common in forwarded/replied emails)
    match = re.search(r"\nFrom:\s+.+\n", body)
    if match:
        return body[: match.start()].rstrip()

    # Strip trailing ">" quoted lines
    lines = body.split("\n")
    result_lines: list[str] = []
    found_content = False
    for line in reversed(lines):
        if not found_content and (line.startswith(">") or not line.strip()):
            continue
        found_content = True
        result_lines.append(line)

    return "\n".join(reversed(result_lines)).rstrip()


def markdown_to_html(text: str) -> str:
    """Convert markdown text to email-safe HTML."""
    html = markdown.markdown(text, extensions=["tables", "fenced_code"])

    # Wrap in a styled container for email clients
    return (
        '<div style="font-family: -apple-system, BlinkMacSystemFont, '
        "'Segoe UI', Roboto, sans-serif; font-size: 14px; "
        'line-height: 1.5; color: #333;">'
        f"{html}"
        "</div>"
    )
