"""Inbox interfaces — mailbox model, authorization helpers, outbox drafts.

Shared by the core ``InboxService``, the web layer, and plugins that
queue or send mail. Imports only from other ``interfaces`` modules —
never from ``core/``, ``integrations/``, ``web/``, or ``storage/``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.email import EmailAddress, EmailAttachment

# ── Mailbox model ────────────────────────────────────────────────────

@dataclass
class Mailbox:
    """A configured inbox — one email account, owned by a user.

    Mailboxes are stored in the ``inbox_mailboxes`` collection. The
    owner is set at creation time and never changes automatically;
    sharing is granted separately via ``shared_with_users`` and
    ``shared_with_roles``.
    """

    id: str
    name: str
    email_address: str
    backend_name: str
    backend_config: dict[str, object] = field(default_factory=dict)
    owner_user_id: str = ""
    shared_with_users: list[str] = field(default_factory=list)
    shared_with_roles: list[str] = field(default_factory=list)
    poll_enabled: bool = True
    poll_interval_sec: int = 60
    created_at: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "email_address": self.email_address,
            "backend_name": self.backend_name,
            "backend_config": self.backend_config,
            "owner_user_id": self.owner_user_id,
            "shared_with_users": list(self.shared_with_users),
            "shared_with_roles": list(self.shared_with_roles),
            "poll_enabled": self.poll_enabled,
            "poll_interval_sec": self.poll_interval_sec,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Mailbox:
        return cls(
            id=str(data.get("id") or data.get("_id") or ""),
            name=str(data.get("name", "")),
            email_address=str(data.get("email_address", "")),
            backend_name=str(data.get("backend_name", "")),
            backend_config=dict(data.get("backend_config") or {}),  # type: ignore[arg-type]
            owner_user_id=str(data.get("owner_user_id", "")),
            shared_with_users=list(data.get("shared_with_users") or []),  # type: ignore[arg-type]
            shared_with_roles=list(data.get("shared_with_roles") or []),  # type: ignore[arg-type]
            poll_enabled=bool(data.get("poll_enabled", True)),
            poll_interval_sec=int(data.get("poll_interval_sec", 60) or 60),  # type: ignore[arg-type]
            created_at=str(data.get("created_at", "")),
        )


class MailboxAccess(StrEnum):
    """How a user came to have access to a mailbox — used for UI grouping."""

    ADMIN = "admin"
    OWNER = "owner"
    SHARED_USER = "shared_user"
    SHARED_ROLE = "shared_role"


# ── Authorization helpers ────────────────────────────────────────────
#
# One rule, applied everywhere. The caller is responsible for resolving
# the admin bit (via AccessControlProvider) and passing it in — these
# helpers intentionally don't import any concrete service so they stay
# in the interfaces layer.

def can_access_mailbox(
    user_ctx: UserContext, mailbox: Mailbox, *, is_admin: bool = False,
) -> bool:
    """Can this user read/send/reply/queue through this mailbox?

    Admin, owner, any user in ``shared_with_users``, or any user with
    a role in ``shared_with_roles`` has full access. Full access means
    read + send-as + outbox management — but **not** mailbox settings
    or share edits (those are gated by ``can_admin_mailbox``).
    """
    if is_admin:
        return True
    if user_ctx.user_id == mailbox.owner_user_id:
        return True
    if user_ctx.user_id in mailbox.shared_with_users:
        return True
    return bool(user_ctx.roles & set(mailbox.shared_with_roles))


def can_admin_mailbox(
    user_ctx: UserContext, mailbox: Mailbox, *, is_admin: bool = False,
) -> bool:
    """Can this user edit mailbox settings, change shares, or delete it?

    Only the owner or a system admin. Shared users — even with full
    send/read access — cannot change configuration or reassign sharing.
    """
    if is_admin:
        return True
    return user_ctx.user_id == mailbox.owner_user_id


def determine_access(
    user_ctx: UserContext, mailbox: Mailbox, *, is_admin: bool = False,
) -> MailboxAccess | None:
    """Return how the user has access to this mailbox, or None if none.

    Precedence: owner > admin > shared_user > shared_role. Owner beats
    admin because owner is the more stable/durable relationship — an
    admin who's also the owner should see "owner" in the UI.
    """
    if user_ctx.user_id == mailbox.owner_user_id:
        return MailboxAccess.OWNER
    if is_admin:
        return MailboxAccess.ADMIN
    if user_ctx.user_id in mailbox.shared_with_users:
        return MailboxAccess.SHARED_USER
    if user_ctx.roles & set(mailbox.shared_with_roles):
        return MailboxAccess.SHARED_ROLE
    return None


# ── Outbox ────────────────────────────────────────────────────────────

class OutboxStatus(StrEnum):
    """Lifecycle of a queued outbound message."""

    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class OutboxDraft:
    """An outbound message queued in the outbox.

    Either a standalone compose (``in_reply_to``/``thread_id`` empty)
    or a threaded reply. Attachments are resolved to knowledge-store
    document IDs at queue time and loaded into raw bytes by the
    outbox tick just before sending.
    """

    to: list[EmailAddress]
    subject: str
    body_html: str
    body_text: str = ""
    cc: list[EmailAddress] | None = None
    in_reply_to: str = ""
    thread_id: str = ""
    attach_documents: list[str] = field(default_factory=list)
    reply_to: EmailAddress | None = None
    from_name: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "to": [{"email": a.email, "name": a.name} for a in self.to],
            "subject": self.subject,
            "body_html": self.body_html,
            "body_text": self.body_text,
            "cc": (
                [{"email": a.email, "name": a.name} for a in self.cc]
                if self.cc is not None else None
            ),
            "in_reply_to": self.in_reply_to,
            "thread_id": self.thread_id,
            "attach_documents": list(self.attach_documents),
            "reply_to": (
                {"email": self.reply_to.email, "name": self.reply_to.name}
                if self.reply_to is not None else None
            ),
            "from_name": self.from_name,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> OutboxDraft:
        def _addr(d: object) -> EmailAddress:
            if not isinstance(d, dict):
                return EmailAddress(email="")
            return EmailAddress(
                email=str(d.get("email", "")),
                name=str(d.get("name", "")),
            )

        raw_to = data.get("to") or []
        raw_cc = data.get("cc")
        raw_reply_to = data.get("reply_to")
        return cls(
            to=[_addr(a) for a in (raw_to if isinstance(raw_to, list) else [])],
            subject=str(data.get("subject", "")),
            body_html=str(data.get("body_html", "")),
            body_text=str(data.get("body_text", "")),
            cc=(
                [_addr(a) for a in raw_cc]
                if isinstance(raw_cc, list) else None
            ),
            in_reply_to=str(data.get("in_reply_to", "")),
            thread_id=str(data.get("thread_id", "")),
            attach_documents=list(data.get("attach_documents") or []),  # type: ignore[arg-type]
            reply_to=_addr(raw_reply_to) if isinstance(raw_reply_to, dict) else None,
            from_name=str(data.get("from_name", "")),
        )


@dataclass
class OutboxEntry:
    """Snapshot of a queued message — what list_outbox/WS RPCs return."""

    id: str
    mailbox_id: str
    status: OutboxStatus
    send_at: str
    draft: OutboxDraft
    created_by_user_id: str
    created_at: str
    sent_at: str | None = None
    error: str | None = None
    retry_count: int = 0


# ── Provider protocol (what plugins consume) ─────────────────────────

@runtime_checkable
class InboxProvider(Protocol):
    """Protocol for reading messages and queuing outbound mail.

    Plugins and other services resolve this via
    ``resolver.get_capability("inbox")`` and ``isinstance``-check
    against this protocol — they never import the concrete
    ``InboxService``. The mailbox is always explicit — there is no
    "default" mailbox.

    Read methods (``get_message``, ``get_thread``, ``search_messages``)
    rely on the current user from ``gilbert.core.context.get_current_user``
    for visibility filtering. Mutating methods take ``user_ctx``
    explicitly so the actor is unambiguous at the call site.
    """

    # ---- Outbox / send ----

    async def schedule_send(
        self,
        mailbox_id: str,
        draft: OutboxDraft,
        user_ctx: UserContext,
        send_at: datetime | None = None,
    ) -> str:
        """Queue a draft for sending. Returns the outbox entry id."""
        ...

    async def cancel_outbox(self, outbox_id: str, user_ctx: UserContext) -> bool:
        """Cancel a pending or failed outbox entry. Returns True on success."""
        ...

    async def list_outbox(
        self,
        mailbox_id: str | None = None,
        status: OutboxStatus | None = None,
    ) -> list[OutboxEntry]:
        """List outbox entries visible to the current user."""
        ...

    # ---- Reads ----

    async def get_message(self, message_id: str) -> dict[str, object] | None:
        """Get one persisted message. Returns None if missing or not accessible."""
        ...

    async def get_thread(
        self, thread_id: str, mailbox_id: str | None = None,
    ) -> list[dict[str, object]]:
        """Get all messages in a thread, sorted by date ascending."""
        ...

    async def search_messages(
        self,
        mailbox_id: str | None = None,
        sender: str = "",
        subject: str = "",
        limit: int = 20,
        include_body: bool = True,
    ) -> list[dict[str, object]]:
        """Search persisted messages in a mailbox (or across all accessible)."""
        ...

    # ---- Mailboxes ----

    async def get_mailbox(self, mailbox_id: str) -> Mailbox | None:
        """Look up a mailbox by id."""
        ...

    async def list_accessible_mailboxes(
        self, user_ctx: UserContext,
    ) -> list[Mailbox]:
        """Return every mailbox the user can access."""
        ...


@runtime_checkable
class CachedMailboxLister(Protocol):
    """Protocol for anything that can report the currently-cached mailboxes.

    Used by ``ConfigurationService._resolve_dynamic_choices`` to
    populate ``inbox_mailboxes`` dropdowns on settings pages without
    duck-typing the service instance. The cache is refreshed at boot
    and on every mailbox CRUD operation so this property is cheap and
    synchronous.
    """

    @property
    def cached_mailboxes(self) -> list[Mailbox]:
        """Return the last-known mailbox list from the service cache."""
        ...


# ── Attachment helpers (re-exported for convenience) ─────────────────
# Plugins that build drafts need to construct EmailAttachment; rather
# than make them import from interfaces.email directly, re-export.

__all__ = [
    "Mailbox",
    "MailboxAccess",
    "OutboxStatus",
    "OutboxDraft",
    "OutboxEntry",
    "InboxProvider",
    "CachedMailboxLister",
    "can_access_mailbox",
    "can_admin_mailbox",
    "determine_access",
    "EmailAddress",
    "EmailAttachment",
]
