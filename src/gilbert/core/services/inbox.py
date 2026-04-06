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

from gilbert.interfaces.email import EmailAddress, EmailAttachment, EmailBackend, EmailMessage
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
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

    def __init__(
        self,
        backend: EmailBackend,
        credential_name: str = "",
        email_address: str = "",
        poll_interval: int = 60,
        max_body_length: int = 50000,
        google_account: str = "",
    ) -> None:
        self._backend = backend
        self._credential_name = credential_name
        self._email_address = email_address
        self._poll_interval = poll_interval
        self._max_body_length = max_body_length
        self._google_account = google_account

        self._storage: Any = None  # StorageBackend
        self._event_bus: Any = None  # EventBus
        self._knowledge: Any = None  # KnowledgeService
        self._unsubscribes: list[Callable[[], None]] = []

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="inbox",
            capabilities=frozenset({"email", "ai_tools"}),
            requires=frozenset({"entity_storage", "scheduler"}),
            optional=frozenset({"event_bus", "google_api", "knowledge"}),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        from gilbert.interfaces.storage import IndexDefinition

        # Storage
        storage_svc = resolver.require_capability("entity_storage")
        self._storage = getattr(storage_svc, "backend", storage_svc)

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
        if event_bus_svc:
            self._event_bus = getattr(event_bus_svc, "bus", event_bus_svc)

        # Google API — build Gmail service if backend needs it
        google_svc = resolver.get_capability("google_api")
        if google_svc and self._google_account:
            try:
                gmail_service = google_svc.build_service(
                    account=self._google_account,
                    service_name="gmail",
                    version="v1",
                    subject=self._email_address or None,
                )
                # GmailBackend expects the service resource
                if hasattr(self._backend, "set_service"):
                    self._backend.set_service(gmail_service)
            except Exception:
                logger.exception("Failed to build Gmail API service")
                raise

        await self._backend.initialize()

        # Schedule polling
        from gilbert.core.services.scheduler import SchedulerService
        from gilbert.interfaces.scheduler import Schedule

        scheduler_svc = resolver.require_capability("scheduler")
        if isinstance(scheduler_svc, SchedulerService):
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
        await self._backend.close()
        logger.info("Inbox service stopped")

    # ── Polling ────────────────────────────────────────────────

    async def _poll(self) -> None:
        """List recent messages, walk until we hit one we already have."""
        try:
            # Fetch up to 500 IDs (paginated internally by the backend).
            # On steady-state only the first few will be new; on a fresh
            # store this ensures we backfill everything.
            all_ids = await self._backend.list_message_ids(max_results=500)
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

                await self._event_bus.publish(Event(
                    event_type="inbox.message.received",
                    data={
                        "message_id": msg.message_id,
                        "thread_id": msg.thread_id,
                        "subject": msg.subject,
                        "sender_email": msg.sender.email,
                        "sender_name": msg.sender.name,
                        "is_inbound": is_inbound,
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
        attachments: list[EmailAttachment] | None = None,
    ) -> str:
        """Reply to an existing message. Returns the sent message's ID."""
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
            "cc": [],
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
