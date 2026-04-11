"""Inbox service — email polling, persistence, and AI tools.

Polls an EmailBackend on a schedule, persists messages in entity storage,
publishes events on the bus, and exposes tools for the AI to search, read,
reply to, and compose email.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.email import EmailAddress, EmailAttachment, EmailBackend, EmailMessage
from gilbert.interfaces.events import EventBusProvider
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import StorageProvider
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)

logger = logging.getLogger(__name__)

_COLLECTION = "inbox_messages"


class InboxService(Service):
    """Email inbox with polling, persistence, and AI tool access.

    Capabilities: email, ai_tools
    """

    def __init__(self) -> None:
        self._backend: EmailBackend | None = None
        self._backend_name: str = "gmail"
        self._enabled: bool = False
        self._email_address: str = ""
        self._poll_interval: int = 60
        self._max_body_length: int = 50000

        self._storage: Any = None  # StorageBackend
        self._event_bus: Any = None  # EventBus
        self._knowledge: Any = None  # KnowledgeService
        self._unsubscribes: list[Callable[[], None]] = []

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="inbox",
            capabilities=frozenset({"email", "ai_tools", "ws_handlers"}),
            requires=frozenset({"entity_storage", "scheduler"}),
            optional=frozenset({"event_bus", "knowledge", "configuration"}),
            events=frozenset({"inbox.message.received", "inbox.message.replied", "inbox.message.sent"}),
            toggleable=True,
            toggle_description="Email inbox monitoring",
        )

    async def start(self, resolver: ServiceResolver) -> None:
        from gilbert.interfaces.storage import IndexDefinition

        # Load config
        settings: dict[str, Any] = {}
        config_svc = resolver.get_capability("configuration")
        section: dict[str, Any] = {}
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                section = config_svc.get_section(self.config_namespace)
                self._email_address = section.get("email_address", self._email_address)
                self._poll_interval = int(section.get("poll_interval", self._poll_interval))
                self._max_body_length = int(section.get("max_body_length", self._max_body_length))
                settings = section.get("settings", {})

        # Check enabled
        if not section.get("enabled", False):
            logger.info("Inbox service disabled")
            return

        self._enabled = True

        # Create backend from registry
        backend_name = section.get("backend", "gmail")
        self._backend_name = backend_name
        backends = EmailBackend.registered_backends()
        if backend_name not in backends:
            # Import known backends to trigger registration
            try:
                import gilbert.integrations.gmail  # noqa: F401
            except ImportError:
                pass
            backends = EmailBackend.registered_backends()
        backend_cls = backends.get(backend_name)
        if backend_cls is None:
            raise ValueError(f"Unknown email backend: {backend_name}")
        self._backend = backend_cls()

        # Initialize backend with settings (includes credentials)
        # Pass email_address into settings so the backend has it
        if self._email_address and "email_address" not in settings:
            settings["email_address"] = self._email_address
        await self._backend.initialize(settings)

        # Storage
        storage_svc = resolver.require_capability("entity_storage")
        if isinstance(storage_svc, StorageProvider):
            self._storage = storage_svc.backend

        await self._storage.ensure_index(IndexDefinition(
            collection=_COLLECTION, fields=["thread_id"],
        ))
        await self._storage.ensure_index(IndexDefinition(
            collection=_COLLECTION, fields=["sender_email"],
        ))
        await self._storage.ensure_index(IndexDefinition(
            collection=_COLLECTION, fields=["date"],
        ))

        # Knowledge service (optional — for document attachments)
        self._knowledge = resolver.get_capability("knowledge")

        # Event bus (optional)
        event_bus_svc = resolver.get_capability("event_bus")
        if isinstance(event_bus_svc, EventBusProvider):
            self._event_bus = event_bus_svc.bus

        # Schedule polling
        from gilbert.interfaces.scheduler import Schedule, SchedulerProvider

        scheduler_svc = resolver.require_capability("scheduler")
        if isinstance(scheduler_svc, SchedulerProvider):
            scheduler_svc.add_job(
                name="inbox-poll",
                schedule=Schedule.every(self._poll_interval),
                callback=self._poll,
                system=True,
            )

        logger.info(
            "Inbox service started (poll every %ds, email=%s)",
            self._poll_interval,
            self._email_address or "(not set)",
        )

    async def stop(self) -> None:
        for unsub in self._unsubscribes:
            unsub()
        self._unsubscribes.clear()
        if self._backend is not None:
            await self._backend.close()
        logger.info("Inbox service stopped")

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "inbox"

    @property
    def config_category(self) -> str:
        return "Communication"

    def config_params(self) -> list[ConfigParam]:
        from gilbert.interfaces.email import EmailBackend

        # Import known backends so they register before we query the registry
        try:
            import gilbert.integrations.gmail  # noqa: F401
        except ImportError:
            pass

        params = [
            ConfigParam(
                key="poll_interval", type=ToolParameterType.INTEGER,
                description="How often to check for new email (seconds).",
                default=60,
            ),
            ConfigParam(
                key="max_body_length", type=ToolParameterType.INTEGER,
                description="Maximum email body length to store (characters).",
                default=50000,
            ),
            ConfigParam(
                key="backend", type=ToolParameterType.STRING,
                description="Email backend provider.",
                default="gmail", restart_required=True,
                choices=tuple(EmailBackend.registered_backends().keys()) or ("gmail",),
            ),
        ]
        # Use registry class for backend params (not instance)
        backends = EmailBackend.registered_backends()
        backend_cls = backends.get(self._backend_name)
        if backend_cls is not None:
            for bp in backend_cls.backend_config_params():
                params.append(ConfigParam(
                    key=f"settings.{bp.key}", type=bp.type,
                    description=bp.description, default=bp.default,
                    restart_required=bp.restart_required, sensitive=bp.sensitive,
                    choices=bp.choices, multiline=bp.multiline, backend_param=True,
                ))
        return params

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._poll_interval = int(config.get("poll_interval", self._poll_interval))
        self._max_body_length = int(config.get("max_body_length", self._max_body_length))

    # ── Polling ────────────────────────────────────────────────

    async def _poll(self) -> None:
        """List recent messages, walk until we hit one we already have."""
        if self._backend is None:
            return
        try:
            # Only fetch unread messages to avoid re-processing old mail.
            all_ids = await self._backend.list_message_ids(
                query="in:inbox is:unread", max_results=100,
            )
        except Exception:
            logger.exception("Inbox poll: failed to list messages")
            return

        # Walk the list (newest first) — stop at the first known message.
        new_ids: list[str] = []
        for mid in all_ids:
            if await self._storage.exists(_COLLECTION, mid):
                break
            new_ids.append(mid)

        if not new_ids:
            return

        # Fetch full content only for new messages
        new_count = 0
        for mid in new_ids:
            try:
                msg = await self._backend.get_message(mid)
            except Exception:
                logger.warning("Inbox poll: failed to fetch message %s", mid)
                continue
            if msg is None:
                continue

            is_inbound = not self._is_own_message(msg)
            await self._persist_message(msg, is_inbound=is_inbound)

            # Mark as read in the remote provider so we don't re-fetch next poll
            try:
                await self._backend.mark_read(mid)
            except Exception:
                logger.warning("Inbox poll: failed to mark %s as read", mid)

            new_count += 1

            if self._event_bus:
                from gilbert.interfaces.events import Event

                # Use X-Original-Sender if present (forwarded mail) so
                # downstream services see the true external sender, not
                # the forwarding alias.
                original_sender = (msg.headers or {}).get(
                    "x-original-sender", "",
                )

                await self._event_bus.publish(Event(
                    event_type="inbox.message.received",
                    data={
                        "message_id": msg.message_id,
                        "thread_id": msg.thread_id,
                        "subject": msg.subject,
                        "sender_email": msg.sender.email,
                        "sender_name": msg.sender.name,
                        "is_inbound": is_inbound,
                        "original_sender": original_sender,
                    },
                    source="inbox",
                ))

        if new_count:
            logger.info("Inbox poll: %d new message(s)", new_count)

    def _is_own_message(self, msg: EmailMessage) -> bool:
        """Check if a message was sent by us."""
        if not self._email_address:
            return False
        return msg.sender.email.lower() == self._email_address.lower()

    async def _persist_message(
        self, msg: EmailMessage, is_inbound: bool,
    ) -> None:
        """Store a message in entity storage."""
        body_text = msg.body_text
        if len(body_text) > self._max_body_length:
            body_text = body_text[: self._max_body_length] + "\n... [truncated]"

        body_html = msg.body_html
        if len(body_html) > self._max_body_length:
            body_html = body_html[: self._max_body_length]

        await self._storage.put(_COLLECTION, msg.message_id, {
            "message_id": msg.message_id,
            "thread_id": msg.thread_id,
            "subject": msg.subject,
            "sender_email": msg.sender.email,
            "sender_name": msg.sender.name,
            "to": [{"email": a.email, "name": a.name} for a in msg.to],
            "cc": [{"email": a.email, "name": a.name} for a in msg.cc],
            "body_text": body_text,
            "body_html": body_html,
            "date": msg.date.isoformat(),
            "in_reply_to": msg.in_reply_to,
            "is_inbound": is_inbound,
        })

    # ── Public API ─────────────────────────────────────────────

    async def search_messages(
        self,
        sender: str = "",
        subject: str = "",
        limit: int = 20,
        include_body: bool = True,
    ) -> list[dict[str, Any]]:
        """Search persisted messages.

        Set include_body=False for list views — avoids deserializing large
        body fields and returns a snippet instead.
        """
        from gilbert.interfaces.storage import Filter, FilterOp, Query, SortField

        filters: list[Filter] = []
        if sender:
            filters.append(Filter(field="sender_email", op=FilterOp.CONTAINS, value=sender.lower()))
        if subject:
            filters.append(Filter(field="subject", op=FilterOp.CONTAINS, value=subject))

        results = await self._storage.query(Query(
            collection=_COLLECTION,
            filters=filters,
            sort=[SortField(field="date", descending=True)],
            limit=limit,
        ))

        if not include_body:
            for r in results:
                body = r.get("body_text", "")
                r["snippet"] = body[:120] + ("..." if len(body) > 120 else "")
                r.pop("body_text", None)
                r.pop("body_html", None)

        return results

    async def get_message(self, message_id: str) -> dict[str, Any] | None:
        """Get a persisted message by ID."""
        return await self._storage.get(_COLLECTION, message_id)

    async def get_thread(self, thread_id: str) -> list[dict[str, Any]]:
        """Get all messages in a thread, sorted by date ascending."""
        from gilbert.interfaces.storage import Filter, FilterOp, Query, SortField

        return await self._storage.query(Query(
            collection=_COLLECTION,
            filters=[Filter(field="thread_id", op=FilterOp.EQ, value=thread_id)],
            sort=[SortField(field="date", descending=False)],
        ))

    async def get_stats(self) -> dict[str, int]:
        """Get inbox statistics."""
        from gilbert.interfaces.storage import Filter, FilterOp, Query

        total = await self._storage.count(Query(collection=_COLLECTION))
        inbound = await self._storage.count(Query(
            collection=_COLLECTION,
            filters=[Filter(field="is_inbound", op=FilterOp.EQ, value=True)],
        ))
        return {"total": total, "inbound": inbound}

    async def reply_to_message(
        self,
        message_id: str,
        body_html: str,
        body_text: str = "",
        cc: list[EmailAddress] | None = None,
        attachments: list[EmailAttachment] | None = None,
    ) -> str:
        """Reply to an existing message. Returns the sent message's ID."""
        if self._backend is None:
            raise RuntimeError("Inbox service is not enabled")
        record = await self._storage.get(_COLLECTION, message_id)
        if not record:
            raise ValueError(f"Message {message_id} not found")

        to = [EmailAddress(email=record["sender_email"], name=record.get("sender_name", ""))]
        subject = record["subject"]
        thread_id = record.get("thread_id", "")
        in_reply_to = record.get("in_reply_to", "")

        sent_id = await self._backend.send(
            to=to,
            subject=subject,
            body_html=body_html,
            body_text=body_text,
            cc=cc,
            in_reply_to=in_reply_to,
            thread_id=thread_id,
            attachments=attachments,
        )

        # Persist outbound
        now = datetime.now(timezone.utc)
        await self._storage.put(_COLLECTION, sent_id, {
            "message_id": sent_id,
            "thread_id": thread_id,
            "subject": f"Re: {subject}" if not subject.startswith("Re:") else subject,
            "sender_email": self._email_address,
            "sender_name": "",
            "to": [{"email": a.email, "name": a.name} for a in to],
            "cc": [{"email": a.email, "name": a.name} for a in (cc or [])],
            "body_text": body_text or body_html,
            "body_html": body_html,
            "date": now.isoformat(),
            "in_reply_to": in_reply_to,
            "is_inbound": False,
        })

        if self._event_bus:
            from gilbert.interfaces.events import Event

            await self._event_bus.publish(Event(
                event_type="inbox.message.replied",
                data={
                    "message_id": sent_id,
                    "thread_id": thread_id,
                    "in_reply_to_message": message_id,
                },
                source="inbox",
            ))

        return sent_id

    async def send_message(
        self,
        to: list[EmailAddress],
        subject: str,
        body_html: str,
        body_text: str = "",
        cc: list[EmailAddress] | None = None,
        attachments: list[EmailAttachment] | None = None,
    ) -> str:
        """Compose and send a new email. Returns the sent message's ID."""
        if self._backend is None:
            raise RuntimeError("Inbox service is not enabled")
        sent_id = await self._backend.send(
            to=to,
            subject=subject,
            body_html=body_html,
            body_text=body_text,
            cc=cc,
            attachments=attachments,
        )

        now = datetime.now(timezone.utc)
        await self._storage.put(_COLLECTION, sent_id, {
            "message_id": sent_id,
            "thread_id": sent_id,  # new thread
            "subject": subject,
            "sender_email": self._email_address,
            "sender_name": "",
            "to": [{"email": a.email, "name": a.name} for a in to],
            "cc": [{"email": a.email, "name": a.name} for a in (cc or [])],
            "body_text": body_text or body_html,
            "body_html": body_html,
            "date": now.isoformat(),
            "in_reply_to": "",
            "is_inbound": False,
        })

        if self._event_bus:
            from gilbert.interfaces.events import Event

            await self._event_bus.publish(Event(
                event_type="inbox.message.sent",
                data={
                    "message_id": sent_id,
                    "subject": subject,
                    "to": [a.email for a in to],
                },
                source="inbox",
            ))

        return sent_id

    # ── ToolProvider Protocol ──────────────────────────────────

    @property
    def tool_provider_name(self) -> str:
        return "inbox"

    def get_tools(self) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        return [
            ToolDefinition(
                name="inbox_search",
                description=(
                    "Search the email inbox. Returns a list of messages "
                    "matching the given criteria, sorted by date (newest first)."
                ),
                parameters=[
                    ToolParameter(
                        name="sender",
                        type=ToolParameterType.STRING,
                        description="Filter by sender email (partial match).",
                        required=False,
                    ),
                    ToolParameter(
                        name="subject",
                        type=ToolParameterType.STRING,
                        description="Filter by subject (partial match).",
                        required=False,
                    ),
                    ToolParameter(
                        name="limit",
                        type=ToolParameterType.INTEGER,
                        description="Maximum number of results (default 20).",
                        required=False,
                        default=20,
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="inbox_read",
                description="Read the full content of an email message by its ID.",
                parameters=[
                    ToolParameter(
                        name="message_id",
                        type=ToolParameterType.STRING,
                        description="The message ID to read.",
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="inbox_reply",
                description=(
                    "Reply to an email message. The reply is threaded in the "
                    "same conversation. Provide the body as HTML. "
                    "Optionally attach documents from the knowledge store by document ID."
                ),
                parameters=[
                    ToolParameter(
                        name="message_id",
                        type=ToolParameterType.STRING,
                        description="The message ID to reply to.",
                    ),
                    ToolParameter(
                        name="body_html",
                        type=ToolParameterType.STRING,
                        description="HTML body of the reply.",
                    ),
                    ToolParameter(
                        name="body_text",
                        type=ToolParameterType.STRING,
                        description="Plain text version of the reply (optional).",
                        required=False,
                    ),
                    ToolParameter(
                        name="attach_documents",
                        type=ToolParameterType.ARRAY,
                        description=(
                            "List of knowledge store document IDs to attach "
                            "(e.g., ['local:docs/report.pdf']). Optional."
                        ),
                        required=False,
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="inbox_send",
                description=(
                    "Compose and send a new email. "
                    "Optionally attach documents from the knowledge store by document ID."
                ),
                parameters=[
                    ToolParameter(
                        name="to",
                        type=ToolParameterType.ARRAY,
                        description="List of recipient email addresses.",
                    ),
                    ToolParameter(
                        name="subject",
                        type=ToolParameterType.STRING,
                        description="Email subject line.",
                    ),
                    ToolParameter(
                        name="body_html",
                        type=ToolParameterType.STRING,
                        description="HTML body of the email.",
                    ),
                    ToolParameter(
                        name="body_text",
                        type=ToolParameterType.STRING,
                        description="Plain text version (optional).",
                        required=False,
                    ),
                    ToolParameter(
                        name="cc",
                        type=ToolParameterType.ARRAY,
                        description="List of CC email addresses (optional).",
                        required=False,
                    ),
                    ToolParameter(
                        name="attach_documents",
                        type=ToolParameterType.ARRAY,
                        description=(
                            "List of knowledge store document IDs to attach "
                            "(e.g., ['local:docs/report.pdf']). Optional."
                        ),
                        required=False,
                    ),
                ],
                required_role="user",
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        match name:
            case "inbox_search":
                return await self._tool_search(arguments)
            case "inbox_read":
                return await self._tool_read(arguments)
            case "inbox_reply":
                return await self._tool_reply(arguments)
            case "inbox_send":
                return await self._tool_send(arguments)
            case _:
                raise KeyError(f"Unknown tool: {name}")

    async def _tool_search(self, args: dict[str, Any]) -> str:
        results = await self.search_messages(
            sender=args.get("sender", ""),
            subject=args.get("subject", ""),
            limit=args.get("limit", 20),
        )
        if not results:
            return "No messages found."

        lines: list[str] = [f"{len(results)} message(s):"]
        for r in results:
            direction = "\u2192" if r.get("is_inbound") else "\u2190"
            lines.append(
                f"  {direction} {r.get('_id', '')} | "
                f"{r.get('date', '')[:16]} | "
                f"{r.get('sender_email', '')} | "
                f"{r.get('subject', '')}"
            )
        return "\n".join(lines)

    async def _tool_read(self, args: dict[str, Any]) -> str:
        message_id = args.get("message_id", "")
        if not message_id:
            return "message_id is required."

        record = await self.get_message(message_id)
        if not record:
            return f"Message {message_id} not found."

        return json.dumps({
            "message_id": record.get("_id", ""),
            "thread_id": record.get("thread_id", ""),
            "date": record.get("date", ""),
            "subject": record.get("subject", ""),
            "from": f"{record.get('sender_name', '')} <{record.get('sender_email', '')}>",
            "to": record.get("to", []),
            "cc": record.get("cc", []),
            "body": record.get("body_text", ""),
            "is_inbound": record.get("is_inbound", True),
        }, indent=2)

    async def _tool_reply(self, args: dict[str, Any]) -> str:
        message_id = args.get("message_id", "")
        body_html = args.get("body_html", "")
        if not message_id:
            return "message_id is required."
        if not body_html:
            return "body_html is required."

        attachments = await self._resolve_attachments(args.get("attach_documents"))

        try:
            sent_id = await self.reply_to_message(
                message_id=message_id,
                body_html=body_html,
                body_text=args.get("body_text", ""),
                attachments=attachments or None,
            )
            att_msg = f" with {len(attachments)} attachment(s)" if attachments else ""
            return f"Reply sent{att_msg} (message ID: {sent_id})."
        except ValueError as e:
            return str(e)

    async def _tool_send(self, args: dict[str, Any]) -> str:
        to_raw = args.get("to", [])
        subject = args.get("subject", "")
        body_html = args.get("body_html", "")

        if not to_raw:
            return "to is required."
        if not subject:
            return "subject is required."
        if not body_html:
            return "body_html is required."

        to = [EmailAddress(email=addr) for addr in to_raw]
        cc_raw = args.get("cc") or []
        cc = [EmailAddress(email=addr) for addr in cc_raw] if cc_raw else None
        attachments = await self._resolve_attachments(args.get("attach_documents"))

        sent_id = await self.send_message(
            to=to,
            subject=subject,
            body_html=body_html,
            body_text=args.get("body_text", ""),
            cc=cc,
            attachments=attachments or None,
        )
        att_msg = f" with {len(attachments)} attachment(s)" if attachments else ""
        return f"Email sent{att_msg} (message ID: {sent_id})."

    async def _resolve_attachments(
        self, document_ids: list[str] | None,
    ) -> list[EmailAttachment]:
        """Resolve knowledge store document IDs to email attachments."""
        if not document_ids or not self._knowledge:
            return []

        attachments: list[EmailAttachment] = []
        for doc_id in document_ids:
            try:
                # doc_id format: "source_id:path" — split to find backend + path
                parts = doc_id.split(":", 1)
                if len(parts) != 2:
                    logger.warning("Invalid document ID format: %s", doc_id)
                    continue

                source_id_prefix, path = parts[0], parts[1]

                # Find the matching backend
                backend = None
                for sid, b in self._knowledge.backends.items():
                    if sid == doc_id[:len(sid)]:
                        backend = b
                        path = doc_id[len(sid) + 1:]  # skip "source_id:"
                        break

                if backend is None:
                    # Try simple prefix match
                    for sid, b in self._knowledge.backends.items():
                        if sid.endswith(source_id_prefix):
                            backend = b
                            break

                if backend is None:
                    logger.warning("No backend found for document: %s", doc_id)
                    continue

                content = await backend.get_document(path)
                if content is None:
                    logger.warning("Document not found: %s", doc_id)
                    continue

                attachments.append(EmailAttachment(
                    filename=content.meta.name,
                    data=content.data,
                    mime_type=content.meta.mime_type,
                ))
            except Exception:
                logger.warning("Failed to resolve attachment: %s", doc_id, exc_info=True)

        return attachments

    # --- WebSocket RPC handlers ---

    def get_ws_handlers(self) -> dict[str, Any]:
        return {
            "inbox.stats.get": self._ws_stats_get,
            "inbox.message.list": self._ws_message_list,
            "inbox.message.get": self._ws_message_get,
            "inbox.thread.get": self._ws_thread_get,
            "inbox.pending.list": self._ws_pending_list,
            "inbox.pending.cancel": self._ws_pending_cancel,
        }

    async def _ws_stats_get(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        stats = await self.get_stats()
        return {"type": "inbox.stats.get.result", "ref": frame.get("id"), **stats}

    async def _ws_message_list(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        messages = await self.search_messages(
            sender=frame.get("sender", ""), subject=frame.get("subject", ""),
            limit=frame.get("limit", 50), include_body=False,
        )
        summaries = []
        for m in messages:
            snippet = m.get("snippet", "")
            if not snippet:
                body = m.get("body_text", "")
                snippet = body[:120] + ("..." if len(body) > 120 else "")
            summaries.append({
                "message_id": m.get("_id", ""), "thread_id": m.get("thread_id", ""),
                "subject": m.get("subject", ""), "sender_email": m.get("sender_email", ""),
                "sender_name": m.get("sender_name", ""), "date": m.get("date", ""),
                "is_inbound": m.get("is_inbound", True), "snippet": snippet,
            })
        return {"type": "inbox.message.list.result", "ref": frame.get("id"), "messages": summaries, "total": len(summaries)}

    async def _ws_message_get(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        record = await self.get_message(frame.get("message_id", ""))
        if not record:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Message not found", "code": 404}
        return {
            "type": "inbox.message.get.result", "ref": frame.get("id"),
            "message_id": record.get("_id", ""), "thread_id": record.get("thread_id", ""),
            "subject": record.get("subject", ""), "sender_email": record.get("sender_email", ""),
            "sender_name": record.get("sender_name", ""), "date": record.get("date", ""),
            "to": record.get("to", []), "cc": record.get("cc", []),
            "body_text": record.get("body_text", ""), "body_html": record.get("body_html", ""),
            "is_inbound": record.get("is_inbound", True),
        }

    async def _ws_thread_get(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        messages = await self.get_thread(frame.get("thread_id", ""))
        result = []
        for m in messages:
            result.append({
                "message_id": m.get("_id", ""), "thread_id": m.get("thread_id", ""),
                "subject": m.get("subject", ""), "sender_email": m.get("sender_email", ""),
                "sender_name": m.get("sender_name", ""), "date": m.get("date", ""),
                "to": m.get("to", []), "cc": m.get("cc", []),
                "body_text": m.get("body_text", ""), "body_html": m.get("body_html", ""),
                "is_inbound": m.get("is_inbound", True),
            })
        return {"type": "inbox.thread.get.result", "ref": frame.get("id"), "messages": result}

    async def _ws_pending_list(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:

        gilbert = conn.manager.gilbert
        if gilbert is None:
            return {"type": "inbox.pending.list.result", "ref": frame.get("id"), "pending": []}

        storage_svc = gilbert.service_manager.get_by_capability("entity_storage")
        if storage_svc is None or not isinstance(storage_svc, StorageProvider):
            return {"type": "inbox.pending.list.result", "ref": frame.get("id"), "pending": []}
        raw_storage = storage_svc.raw_backend

        from gilbert.interfaces.storage import Filter, FilterOp, Query, SortField
        from datetime import datetime, timedelta, timezone as tz
        cutoff = (datetime.now(tz.utc) - timedelta(days=1)).isoformat()
        _PENDING_COLLECTIONS = ["gilbert.plugin.current-sales-assistant.pending_replies"]

        pending: list[dict[str, Any]] = []
        for collection in _PENDING_COLLECTIONS:
            try:
                pending_results = await raw_storage.query(Query(
                    collection=collection,
                    filters=[Filter(field="status", op=FilterOp.EQ, value="pending")],
                    sort=[SortField(field="send_at", descending=False)],
                ))
                failed_results = await raw_storage.query(Query(
                    collection=collection,
                    filters=[Filter(field="status", op=FilterOp.EQ, value="failed"), Filter(field="send_at", op=FilterOp.GTE, value=cutoff)],
                    sort=[SortField(field="send_at", descending=False)],
                ))
                for r in pending_results + failed_results:
                    pending.append({
                        "id": r.get("_id", ""), "collection": collection,
                        "customer_email": r.get("customer_email", ""), "subject": r.get("subject", ""),
                        "status": r.get("status", ""), "send_at": r.get("send_at", ""),
                    })
            except Exception:
                pass
        return {"type": "inbox.pending.list.result", "ref": frame.get("id"), "pending": pending}

    async def _ws_pending_cancel(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        reply_id = frame.get("reply_id", "")
        if not reply_id:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "reply_id required", "code": 400}

        gilbert = conn.manager.gilbert
        if gilbert is None:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Storage not available", "code": 503}

        storage_svc = gilbert.service_manager.get_by_capability("entity_storage")
        if storage_svc is None or not isinstance(storage_svc, StorageProvider):
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Storage not available", "code": 503}
        raw_storage = storage_svc.raw_backend

        _PENDING_COLLECTIONS = ["gilbert.plugin.current-sales-assistant.pending_replies"]
        for collection in _PENDING_COLLECTIONS:
            try:
                existing = await raw_storage.get(collection, reply_id)
                if existing and existing.get("status") in ("pending", "failed"):
                    existing["status"] = "cancelled"
                    await raw_storage.put(collection, reply_id, existing)
                    return {"type": "inbox.pending.cancel.result", "ref": frame.get("id"), "status": "cancelled"}
            except Exception:
                pass
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Pending reply not found", "code": 404}
