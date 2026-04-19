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
from datetime import UTC, datetime
from typing import Any

import markdown

from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import ConfigParam
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

    def __init__(self) -> None:
        self._enabled: bool = False
        self._allowed_emails: list[str] = []
        self._allowed_domains: list[str] = []
        self._required_subject: str = ""

        self._inbox: Any = None  # InboxService
        self._ai: Any = None  # AIService
        self._user_svc: Any = None  # UserService (optional)
        self._storage: Any = None  # StorageBackend
        self._knowledge: Any = None  # KnowledgeService (optional)
        self._event_bus: Any = None  # EventBus
        self._unsubscribe: Any = None
        self._ai_profile: str = "standard"

        # Per-request pending attachments, protected by a lock so
        # concurrent _process_message calls don't mix attachments.
        self._pending_attachments: list[EmailAttachment] = []
        self._process_lock = asyncio.Lock()

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="inbox_ai_chat",
            capabilities=frozenset({"email_ai_chat", "ai_tools"}),
            requires=frozenset({"email", "ai_chat", "entity_storage"}),
            optional=frozenset({"event_bus", "users", "knowledge", "configuration"}),
            ai_calls=frozenset({"inbox_ai_chat"}),
            toggleable=True,
            toggle_description="Email-to-AI chat",
        )

    async def start(self, resolver: ServiceResolver) -> None:
        # Check enabled and load config
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                section = config_svc.get_section(self.config_namespace)
                if not section.get("enabled", False):
                    logger.info("Inbox AI chat service disabled")
                    return
                emails = section.get("allowed_emails", [])
                self._allowed_emails = [e.lower() for e in emails]
                domains = section.get("allowed_domains", [])
                self._allowed_domains = [d.lower().lstrip("@") for d in domains]
                subj = section.get("required_subject", "")
                self._required_subject = subj.lower().strip() if subj else ""

        self._enabled = True

        self._inbox = resolver.require_capability("email")
        self._ai = resolver.require_capability("ai_chat")

        from gilbert.interfaces.events import EventBusProvider
        from gilbert.interfaces.storage import StorageProvider

        storage_svc = resolver.require_capability("entity_storage")
        if isinstance(storage_svc, StorageProvider):
            self._storage = storage_svc.backend

        self._user_svc = resolver.get_capability("users")
        self._knowledge = resolver.get_capability("knowledge")

        event_bus_svc = resolver.get_capability("event_bus")
        if isinstance(event_bus_svc, EventBusProvider):
            self._event_bus = event_bus_svc.bus
            self._unsubscribe = self._event_bus.subscribe(
                "inbox.message.received",
                self._on_message_received,
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

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "inbox_ai_chat"

    @property
    def config_category(self) -> str:
        return "Communication"

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="allowed_emails",
                type=ToolParameterType.ARRAY,
                description="Email addresses allowed to chat with AI.",
                default=[],
            ),
            ConfigParam(
                key="allowed_domains",
                type=ToolParameterType.ARRAY,
                description="Email domains allowed to chat with AI.",
                default=[],
            ),
            ConfigParam(
                key="required_subject",
                type=ToolParameterType.STRING,
                description="Required subject line prefix (empty = no filter).",
                default="",
            ),
            ConfigParam(
                key="ai_profile",
                type=ToolParameterType.STRING,
                description="AI profile for email-to-AI chat.",
                default="standard",
                choices_from="ai_profiles",
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        emails = config.get("allowed_emails", [])
        self._allowed_emails = [e.lower() for e in emails]
        domains = config.get("allowed_domains", [])
        self._allowed_domains = [d.lower().lstrip("@") for d in domains]
        subj = config.get("required_subject", "")
        self._required_subject = subj.lower().strip() if subj else ""
        self._ai_profile = config.get("ai_profile", self._ai_profile)

    # ── Event handler ──────────────────────────────────────────

    async def _on_message_received(self, event: Event) -> None:
        """Handle a new inbox message."""
        data = event.data

        # Skip outbound messages
        if not data.get("is_inbound", True):
            return

        mailbox_id = data.get("mailbox_id", "")
        if not mailbox_id:
            return

        sender_email = data.get("sender_email", "")

        # When an email was forwarded through a Google Groups alias
        # (e.g., vendors@current-la.com), Gmail sets X-Original-Sender
        # to the true external sender.  Use that for the allowlist check
        # so forwarded mail from external senders is correctly rejected.
        original_sender = data.get("original_sender", "")
        check_email = original_sender if original_sender else sender_email

        if not self._is_allowed(check_email):
            return

        # Subject filter: require a specific subject (or a reply to it)
        if self._required_subject:
            subject = data.get("subject", "").lower().strip()
            # Strip "re:" / "fwd:" prefixes for reply matching
            bare_subject = re.sub(r"^(re|fwd|fw)\s*:\s*", "", subject).strip()
            if bare_subject != self._required_subject:
                return

        message_id = data.get("message_id", "")
        thread_id = data.get("thread_id", "")

        # Skip if we already replied to this message
        if await self._already_replied(message_id):
            logger.debug("Skipping already-replied message: %s", message_id)
            return

        try:
            await self._process_message(mailbox_id, message_id, thread_id, sender_email)
        except Exception:
            logger.exception(
                "Failed to process email AI chat: message=%s sender=%s mailbox=%s",
                message_id,
                sender_email,
                mailbox_id,
            )

    async def _process_message(
        self,
        mailbox_id: str,
        message_id: str,
        thread_id: str,
        sender_email: str,
    ) -> None:
        """Process a single inbound message: AI chat + reply.

        Uses a lock so that ``_pending_attachments`` is never shared
        between concurrent message processing tasks.
        """
        async with self._process_lock:
            await self._process_message_locked(
                mailbox_id,
                message_id,
                thread_id,
                sender_email,
            )

    async def _process_message_locked(
        self,
        mailbox_id: str,
        message_id: str,
        thread_id: str,
        sender_email: str,
    ) -> None:
        """Inner processing — must be called under ``_process_lock``."""
        # Propagate SYSTEM identity so InboxProvider's read methods
        # (which pull the current user from the async context) don't
        # apply per-user visibility filters to this background handler.
        from gilbert.core.context import set_current_user

        set_current_user(UserContext.SYSTEM)

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
        response_text, conv_id, _ui, _tu, *_ = await self._ai.chat(
            user_message=context_prefix + body,
            conversation_id=conversation_id,
            user_ctx=user_ctx,
            ai_profile=self._ai_profile,
        )

        # Collect any attachments the AI queued via the email_attach tool
        attachments = list(self._pending_attachments)
        self._pending_attachments = []

        # Store thread → conversation mapping
        await self._set_conversation_id(thread_id, conv_id, sender_email)

        # Convert response to HTML and reply. The AI chat service acts
        # on behalf of the system — use UserContext.SYSTEM as the actor
        # so the reply bypasses per-user mailbox access checks (the
        # external sender is not typically a Gilbert user).
        body_html = markdown_to_html(response_text)
        await self._inbox.reply_to_message(
            mailbox_id=mailbox_id,
            message_id=message_id,
            body_html=body_html,
            user_ctx=UserContext.SYSTEM,
            body_text=response_text,
            attachments=attachments or None,
        )

        # Mark this message as replied so we never re-process it
        await self._mark_replied(message_id, sender_email)

        att_msg = f" with {len(attachments)} attachment(s)" if attachments else ""
        logger.info(
            "Email AI chat: replied to %s%s (thread=%s, conv=%s)",
            sender_email,
            att_msg,
            thread_id,
            conv_id,
        )

    # ── Reply dedup ────────────────────────────────────────────

    _REPLIED_COLLECTION = "inbox_ai_chat_replied"

    async def _already_replied(self, message_id: str) -> bool:
        """Check if we already replied to this message."""
        if self._storage is None:
            return False
        return bool(await self._storage.exists(self._REPLIED_COLLECTION, message_id))

    async def _mark_replied(self, message_id: str, sender_email: str) -> None:
        """Record that we replied to a message."""
        if self._storage is None:
            return
        await self._storage.put(
            self._REPLIED_COLLECTION,
            message_id,
            {
                "message_id": message_id,
                "sender_email": sender_email,
                "replied_at": datetime.now(UTC).isoformat(),
            },
        )

    # ── Allowlist ──────────────────────────────────────────────

    def _is_allowed(self, sender_email: str) -> bool:
        """Check if a sender is allowed to chat."""
        email_lower = sender_email.lower()

        if email_lower in self._allowed_emails:
            return True

        domain = email_lower.rsplit("@", 1)[-1] if "@" in email_lower else ""
        return domain in self._allowed_domains

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

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        if not self._enabled:
            return []
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
            backend, path = self._knowledge.resolve_backend(document_id)
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
            cid = record.get("conversation_id")
            return str(cid) if cid is not None else None
        return None

    async def _set_conversation_id(
        self,
        thread_id: str,
        conversation_id: str,
        sender_email: str,
    ) -> None:
        """Store the thread → conversation mapping."""
        await self._storage.put(
            _THREAD_COLLECTION,
            thread_id,
            {
                "thread_id": thread_id,
                "conversation_id": conversation_id,
                "sender_email": sender_email,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )


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
