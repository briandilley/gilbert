"""Inbox service — multi-mailbox email polling, persistence, outbox, and AI tools.

Each mailbox is owned by a user and can be shared with individual
users and/or roles. The service runs one ``EmailBackend`` instance +
one scheduler poll job per ``poll_enabled`` mailbox. Messages are
persisted in ``inbox_messages`` (tagged with ``mailbox_id``); queued
outbound drafts live in ``inbox_outbox`` and are flushed by a
shared outbox tick.

Authorization is centralized in ``interfaces/inbox.py`` —
``can_access_mailbox`` gates read/send/reply/outbox, and
``can_admin_mailbox`` gates settings and share edits.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from gilbert.core.context import get_current_user
from gilbert.core.services._backend_actions import (
    all_backend_actions,
    invoke_backend_action,
)
from gilbert.interfaces.auth import AccessControlProvider, UserContext
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.email import EmailAddress, EmailAttachment, EmailBackend, EmailMessage
from gilbert.interfaces.events import EventBusProvider
from gilbert.interfaces.inbox import (
    InboxProvider,
    Mailbox,
    OutboxDraft,
    OutboxEntry,
    OutboxStatus,
    can_access_mailbox,
    can_admin_mailbox,
    determine_access,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import (
    Filter,
    FilterOp,
    IndexDefinition,
    Query,
    SortField,
    StorageProvider,
)
from gilbert.interfaces.workspace import WorkspaceProvider
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)

logger = logging.getLogger(__name__)

_MAILBOXES_COLLECTION = "inbox_mailboxes"
_MESSAGES_COLLECTION = "inbox_messages"
_OUTBOX_COLLECTION = "inbox_outbox"

_OUTBOX_TICK_INTERVAL_SEC = 10


class InboxPermissionError(PermissionError):
    """Raised when a caller lacks access to a mailbox or outbox entry."""


class MailboxNotFoundError(LookupError):
    """Raised when a mailbox_id does not resolve."""


@dataclass
class _MailboxRuntime:
    """In-memory per-mailbox state: current config + live backend."""

    mailbox: Mailbox
    backend: EmailBackend
    poll_job_name: str = ""


class InboxService(Service):
    """Multi-mailbox email inbox with outbox, sharing, and AI tools.

    Capabilities: email, ai_tools, ws_handlers
    Events: inbox.message.{received,replied,sent},
            inbox.outbox.{sent,failed},
            inbox.mailbox.{created,updated,deleted,shares.changed}
    """

    def __init__(self) -> None:
        self._storage: Any = None  # StorageBackend
        self._event_bus: Any = None  # EventBus
        self._knowledge: Any = None  # KnowledgeService (optional)
        self._scheduler: Any = None  # SchedulerProvider
        self._access_control: AccessControlProvider | None = None
        # Resolver is cached so capability lookups can happen lazily at
        # call time. Some optional capabilities (skills, knowledge)
        # might not be started yet when InboxService.start() runs —
        # the topological sort only orders by ``requires``, so cross-
        # dependencies on ``optional`` capabilities aren't guaranteed
        # to be ready at start time. Looking them up at use time
        # sidesteps the ordering problem entirely.
        self._resolver: ServiceResolver | None = None

        self._runtimes: dict[str, _MailboxRuntime] = {}
        self._cached_mailboxes: list[Mailbox] = []
        self._max_body_length: int = 50000
        self._enabled: bool = True
        self._outbox_busy: bool = False

    @property
    def cached_mailboxes(self) -> list[Mailbox]:
        """Sync snapshot of all mailboxes — used by config dynamic choices.

        Maintained by ``_refresh_cache``, called from ``_boot_runtimes``
        and after every mailbox CRUD operation. Returns a copy so the
        caller can iterate freely.
        """
        return list(self._cached_mailboxes)

    # ── Service metadata ─────────────────────────────────────────────

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="inbox",
            capabilities=frozenset({"email", "ai_tools", "ws_handlers", "inbox"}),
            requires=frozenset({"entity_storage", "scheduler"}),
            optional=frozenset(
                {
                    "event_bus",
                    "knowledge",
                    "configuration",
                    "access_control",
                }
            ),
            events=frozenset(
                {
                    "inbox.message.received",
                    "inbox.message.replied",
                    "inbox.message.sent",
                    "inbox.outbox.sent",
                    "inbox.outbox.failed",
                    "inbox.mailbox.created",
                    "inbox.mailbox.updated",
                    "inbox.mailbox.deleted",
                    "inbox.mailbox.shares.changed",
                }
            ),
            ai_calls=frozenset({"inbox_compose", "inbox_reply"}),
            toggleable=True,
            toggle_description="Email inbox polling and outbox",
        )

    # ── Lifecycle ────────────────────────────────────────────────────

    async def start(self, resolver: ServiceResolver) -> None:
        # Storage is required.
        storage_svc = resolver.require_capability("entity_storage")
        if not isinstance(storage_svc, StorageProvider):
            raise TypeError("entity_storage capability does not provide StorageProvider")
        self._storage = storage_svc.backend

        # Ensure indexes on all three collections.
        await self._storage.ensure_index(
            IndexDefinition(
                collection=_MAILBOXES_COLLECTION,
                fields=["owner_user_id"],
            )
        )
        await self._storage.ensure_index(
            IndexDefinition(
                collection=_MESSAGES_COLLECTION,
                fields=["mailbox_id", "thread_id"],
            )
        )
        await self._storage.ensure_index(
            IndexDefinition(
                collection=_MESSAGES_COLLECTION,
                fields=["mailbox_id", "date"],
            )
        )
        await self._storage.ensure_index(
            IndexDefinition(
                collection=_MESSAGES_COLLECTION,
                fields=["mailbox_id", "sender_email"],
            )
        )
        await self._storage.ensure_index(
            IndexDefinition(
                collection=_OUTBOX_COLLECTION,
                fields=["mailbox_id", "status", "send_at"],
            )
        )
        await self._storage.ensure_index(
            IndexDefinition(
                collection=_OUTBOX_COLLECTION,
                fields=["created_by_user_id", "status"],
            )
        )

        # Optional capabilities.
        event_bus_svc = resolver.get_capability("event_bus")
        if isinstance(event_bus_svc, EventBusProvider):
            self._event_bus = event_bus_svc.bus

        self._knowledge = resolver.get_capability("knowledge")
        # ``skills`` is looked up lazily — see ``_get_skills_service``.
        self._resolver = resolver

        acl_svc = resolver.get_capability("access_control")
        if isinstance(acl_svc, AccessControlProvider):
            self._access_control = acl_svc

        # Load global config.
        config_svc = resolver.get_capability("configuration")
        section: dict[str, Any] = {}
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                section = config_svc.get_section(self.config_namespace)
                self._max_body_length = int(
                    section.get(
                        "max_body_length",
                        self._max_body_length,
                    )
                )

        self._enabled = bool(section.get("enabled", True))
        if not self._enabled:
            logger.info("Inbox service disabled via configuration")
            return

        # Scheduler (required) — used for per-mailbox polls and outbox tick.
        from gilbert.interfaces.scheduler import Schedule, SchedulerProvider

        scheduler_svc = resolver.require_capability("scheduler")
        if not isinstance(scheduler_svc, SchedulerProvider):
            raise TypeError("scheduler capability does not provide SchedulerProvider")
        self._scheduler = scheduler_svc

        # Boot runtimes asynchronously via a one-shot scheduler job so
        # network-bound backend initialization doesn't block start().
        self._scheduler.add_job(
            name="inbox-boot",
            schedule=Schedule.once_after(0),
            callback=self._boot_runtimes,
            system=True,
        )

        # Outbox tick runs forever while service is enabled.
        self._scheduler.add_job(
            name="inbox-outbox-tick",
            schedule=Schedule.every(_OUTBOX_TICK_INTERVAL_SEC),
            callback=self._outbox_tick,
            system=True,
        )

        logger.info(
            "Inbox service started (boot deferred, outbox tick every %ds)",
            _OUTBOX_TICK_INTERVAL_SEC,
        )

    async def stop(self) -> None:
        if self._scheduler is not None:
            for runtime in list(self._runtimes.values()):
                if runtime.poll_job_name:
                    with contextlib.suppress(Exception):
                        self._scheduler.remove_job(runtime.poll_job_name)
            with contextlib.suppress(Exception):
                self._scheduler.remove_job("inbox-outbox-tick")
            with contextlib.suppress(Exception):
                self._scheduler.remove_job("inbox-boot")

        for runtime in list(self._runtimes.values()):
            try:
                await runtime.backend.close()
            except Exception:
                logger.exception("Error closing backend for mailbox %s", runtime.mailbox.id)
        self._runtimes.clear()
        logger.info("Inbox service stopped")

    async def _boot_runtimes(self) -> None:
        """One-shot job at startup: spin up a runtime per poll_enabled mailbox."""
        try:
            mailboxes = await self._load_mailboxes()
        except Exception:
            logger.exception("Inbox boot: failed to load mailboxes")
            return

        self._cached_mailboxes = list(mailboxes)

        for mailbox in mailboxes:
            if mailbox.poll_enabled:
                try:
                    await self._start_runtime(mailbox)
                except Exception:
                    logger.exception(
                        "Inbox boot: failed to start runtime for %s",
                        mailbox.id,
                    )

        logger.info("Inbox boot: %d runtime(s) started", len(self._runtimes))

    async def _refresh_cache(self) -> None:
        """Refresh ``_cached_mailboxes`` from storage. Cheap — one query."""
        try:
            self._cached_mailboxes = await self._load_mailboxes()
        except Exception:
            logger.exception("Inbox: failed to refresh mailbox cache")

    async def _start_runtime(self, mailbox: Mailbox) -> None:
        """Instantiate a backend + register a poll job for one mailbox."""
        assert self._scheduler is not None

        backends = EmailBackend.registered_backends()
        backend_cls = backends.get(mailbox.backend_name)
        if backend_cls is None:
            raise ValueError(f"Unknown email backend: {mailbox.backend_name}")

        backend = backend_cls()
        # Pass email_address into backend settings so backends that need
        # it (e.g. gmail) can authenticate as the right account.
        settings = dict(mailbox.backend_config)
        if mailbox.email_address and "email_address" not in settings:
            settings["email_address"] = mailbox.email_address
        await backend.initialize(settings)

        from gilbert.interfaces.scheduler import Schedule

        poll_job_name = f"inbox-poll-{mailbox.id}"
        callback = self._make_poll_callback(mailbox.id)
        self._scheduler.add_job(
            name=poll_job_name,
            schedule=Schedule.every(mailbox.poll_interval_sec),
            callback=callback,
            system=True,
        )

        self._runtimes[mailbox.id] = _MailboxRuntime(
            mailbox=mailbox,
            backend=backend,
            poll_job_name=poll_job_name,
        )
        logger.info(
            "Mailbox runtime started: id=%s backend=%s poll=%ds",
            mailbox.id,
            mailbox.backend_name,
            mailbox.poll_interval_sec,
        )

    async def _stop_runtime(self, mailbox_id: str) -> None:
        """Stop and remove a runtime (leaves the mailbox row intact)."""
        runtime = self._runtimes.pop(mailbox_id, None)
        if runtime is None:
            return
        if self._scheduler is not None and runtime.poll_job_name:
            with contextlib.suppress(Exception):
                self._scheduler.remove_job(runtime.poll_job_name)
        try:
            await runtime.backend.close()
        except Exception:
            logger.exception("Error closing backend for mailbox %s", mailbox_id)
        logger.info("Mailbox runtime stopped: id=%s", mailbox_id)

    async def _restart_runtime(self, mailbox: Mailbox) -> None:
        """Stop and re-start a runtime for a mailbox whose config changed."""
        await self._stop_runtime(mailbox.id)
        if mailbox.poll_enabled:
            await self._start_runtime(mailbox)

    # ── Configurable protocol ────────────────────────────────────────

    @property
    def config_namespace(self) -> str:
        return "inbox"

    @property
    def config_category(self) -> str:
        return "Communication"

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="max_body_length",
                type=ToolParameterType.INTEGER,
                description="Maximum email body length to store (characters).",
                default=50000,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._max_body_length = int(
            config.get(
                "max_body_length",
                self._max_body_length,
            )
        )

    # ── ConfigAction provider (no-op — per-mailbox now) ──────────────

    def config_actions(self) -> list[ConfigAction]:
        # Legacy hook; per-mailbox backend actions are exposed via the
        # mailbox edit UI, not the global service config.
        return all_backend_actions(
            registry=EmailBackend.registered_backends(),
            current_backend=None,
        )

    async def invoke_config_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        return await invoke_backend_action(None, key, payload)

    # ── Internal: authorization helpers ──────────────────────────────

    def _is_admin(self, user_ctx: UserContext) -> bool:
        """Resolve whether the user has admin-level access.

        Uses ``AccessControlProvider`` if available, otherwise falls
        back to checking for ``"admin"`` in the user's roles.
        """
        if user_ctx.user_id == UserContext.SYSTEM.user_id:
            return True
        if self._access_control is not None:
            return self._access_control.get_effective_level(user_ctx) <= 0
        return "admin" in user_ctx.roles

    def _require_access(self, mailbox: Mailbox, user_ctx: UserContext) -> None:
        if not can_access_mailbox(
            user_ctx,
            mailbox,
            is_admin=self._is_admin(user_ctx),
        ):
            raise InboxPermissionError(
                f"User {user_ctx.user_id!r} cannot access mailbox {mailbox.id!r}",
            )

    def _require_admin(self, mailbox: Mailbox, user_ctx: UserContext) -> None:
        if not can_admin_mailbox(
            user_ctx,
            mailbox,
            is_admin=self._is_admin(user_ctx),
        ):
            raise InboxPermissionError(
                f"User {user_ctx.user_id!r} cannot administer mailbox {mailbox.id!r}",
            )

    # ── Mailbox CRUD ─────────────────────────────────────────────────

    async def _load_mailboxes(self) -> list[Mailbox]:
        rows = await self._storage.query(Query(collection=_MAILBOXES_COLLECTION))
        return [Mailbox.from_dict(row) for row in rows]

    async def list_mailboxes(self) -> list[Mailbox]:
        """List every mailbox regardless of access. Callers enforce auth."""
        return await self._load_mailboxes()

    async def list_accessible_mailboxes(
        self,
        user_ctx: UserContext,
    ) -> list[Mailbox]:
        """List mailboxes the given user can access."""
        is_admin = self._is_admin(user_ctx)
        all_mailboxes = await self._load_mailboxes()
        return [m for m in all_mailboxes if can_access_mailbox(user_ctx, m, is_admin=is_admin)]

    async def get_mailbox(self, mailbox_id: str) -> Mailbox | None:
        row = await self._storage.get(_MAILBOXES_COLLECTION, mailbox_id)
        if row is None:
            return None
        return Mailbox.from_dict(row)

    async def _require_mailbox(self, mailbox_id: str) -> Mailbox:
        mailbox = await self.get_mailbox(mailbox_id)
        if mailbox is None:
            raise MailboxNotFoundError(f"Mailbox not found: {mailbox_id}")
        return mailbox

    async def create_mailbox(
        self,
        mailbox: Mailbox,
        user_ctx: UserContext,
    ) -> Mailbox:
        """Create a new mailbox. Creator becomes owner."""
        if not mailbox.id:
            mailbox.id = f"mbx_{uuid.uuid4().hex[:12]}"
        mailbox.owner_user_id = user_ctx.user_id
        mailbox.created_at = datetime.now(UTC).isoformat()

        existing = await self._storage.get(_MAILBOXES_COLLECTION, mailbox.id)
        if existing is not None:
            raise ValueError(f"Mailbox id already exists: {mailbox.id}")

        await self._storage.put(
            _MAILBOXES_COLLECTION,
            mailbox.id,
            mailbox.to_dict(),
        )
        await self._refresh_cache()

        if self._enabled and mailbox.poll_enabled:
            try:
                await self._start_runtime(mailbox)
            except Exception:
                logger.exception(
                    "Failed to start runtime for newly created mailbox %s",
                    mailbox.id,
                )

        await self._publish_mailbox_event("inbox.mailbox.created", mailbox)
        return mailbox

    async def update_mailbox(
        self,
        mailbox_id: str,
        updates: dict[str, Any],
        user_ctx: UserContext,
    ) -> Mailbox:
        """Update settings on an existing mailbox. Owner/admin only.

        Cannot change ``owner_user_id`` — that's fixed at creation.
        Share lists are updated via ``share_user``/``share_role`` and
        siblings so the ``shares.changed`` event fires cleanly.
        """
        mailbox = await self._require_mailbox(mailbox_id)
        self._require_admin(mailbox, user_ctx)

        immutable = {"id", "owner_user_id", "created_at"}
        share_fields = {"shared_with_users", "shared_with_roles"}
        needs_restart = False

        for key, value in updates.items():
            if key in immutable:
                continue
            if key in share_fields:
                continue  # use share_* methods
            if not hasattr(mailbox, key):
                continue
            if key in (
                "backend_name",
                "backend_config",
                "poll_enabled",
                "poll_interval_sec",
                "email_address",
            ):
                needs_restart = True
            setattr(mailbox, key, value)

        await self._storage.put(
            _MAILBOXES_COLLECTION,
            mailbox.id,
            mailbox.to_dict(),
        )
        await self._refresh_cache()

        if self._enabled and needs_restart:
            try:
                await self._restart_runtime(mailbox)
            except Exception:
                logger.exception(
                    "Failed to restart runtime after update for mailbox %s",
                    mailbox.id,
                )

        await self._publish_mailbox_event("inbox.mailbox.updated", mailbox)
        return mailbox

    async def delete_mailbox(
        self,
        mailbox_id: str,
        user_ctx: UserContext,
    ) -> None:
        """Delete a mailbox. Refuses if non-terminal outbox entries exist.

        Cascades: deletes all ``inbox_messages`` rows tagged with this
        mailbox_id. Archived outbox rows (sent/cancelled) are also
        deleted so the mailbox leaves no trace.
        """
        mailbox = await self._require_mailbox(mailbox_id)
        self._require_admin(mailbox, user_ctx)

        # Refuse if there are non-terminal outbox entries.
        non_terminal = await self._storage.query(
            Query(
                collection=_OUTBOX_COLLECTION,
                filters=[
                    Filter(field="mailbox_id", op=FilterOp.EQ, value=mailbox_id),
                    Filter(
                        field="status",
                        op=FilterOp.IN,
                        value=[OutboxStatus.PENDING, OutboxStatus.SENDING, OutboxStatus.FAILED],
                    ),
                ],
                limit=1,
            )
        )
        if non_terminal:
            raise ValueError(
                "Cannot delete mailbox with pending/failed outbox entries; "
                "cancel or retry them first",
            )

        await self._stop_runtime(mailbox_id)

        # Cascade: messages and terminal outbox rows.
        msgs = await self._storage.query(
            Query(
                collection=_MESSAGES_COLLECTION,
                filters=[Filter(field="mailbox_id", op=FilterOp.EQ, value=mailbox_id)],
            )
        )
        for m in msgs:
            await self._storage.delete(_MESSAGES_COLLECTION, m["_id"])

        outbox = await self._storage.query(
            Query(
                collection=_OUTBOX_COLLECTION,
                filters=[Filter(field="mailbox_id", op=FilterOp.EQ, value=mailbox_id)],
            )
        )
        for o in outbox:
            await self._storage.delete(_OUTBOX_COLLECTION, o["_id"])

        await self._storage.delete(_MAILBOXES_COLLECTION, mailbox_id)
        await self._refresh_cache()
        await self._publish_mailbox_event("inbox.mailbox.deleted", mailbox)

    async def share_user(
        self,
        mailbox_id: str,
        user_id: str,
        user_ctx: UserContext,
    ) -> Mailbox:
        mailbox = await self._require_mailbox(mailbox_id)
        self._require_admin(mailbox, user_ctx)
        if user_id not in mailbox.shared_with_users:
            mailbox.shared_with_users.append(user_id)
            await self._storage.put(
                _MAILBOXES_COLLECTION,
                mailbox.id,
                mailbox.to_dict(),
            )
            await self._publish_shares_changed(mailbox)
        return mailbox

    async def unshare_user(
        self,
        mailbox_id: str,
        user_id: str,
        user_ctx: UserContext,
    ) -> Mailbox:
        mailbox = await self._require_mailbox(mailbox_id)
        self._require_admin(mailbox, user_ctx)
        if user_id in mailbox.shared_with_users:
            mailbox.shared_with_users.remove(user_id)
            await self._storage.put(
                _MAILBOXES_COLLECTION,
                mailbox.id,
                mailbox.to_dict(),
            )
            await self._publish_shares_changed(mailbox)
        return mailbox

    async def share_role(
        self,
        mailbox_id: str,
        role: str,
        user_ctx: UserContext,
    ) -> Mailbox:
        mailbox = await self._require_mailbox(mailbox_id)
        self._require_admin(mailbox, user_ctx)
        if role not in mailbox.shared_with_roles:
            mailbox.shared_with_roles.append(role)
            await self._storage.put(
                _MAILBOXES_COLLECTION,
                mailbox.id,
                mailbox.to_dict(),
            )
            await self._publish_shares_changed(mailbox)
        return mailbox

    async def unshare_role(
        self,
        mailbox_id: str,
        role: str,
        user_ctx: UserContext,
    ) -> Mailbox:
        mailbox = await self._require_mailbox(mailbox_id)
        self._require_admin(mailbox, user_ctx)
        if role in mailbox.shared_with_roles:
            mailbox.shared_with_roles.remove(role)
            await self._storage.put(
                _MAILBOXES_COLLECTION,
                mailbox.id,
                mailbox.to_dict(),
            )
            await self._publish_shares_changed(mailbox)
        return mailbox

    async def test_mailbox_connection(
        self,
        mailbox_id: str,
        user_ctx: UserContext,
    ) -> dict[str, Any]:
        """Probe a mailbox's backend — returns {'ok': bool, 'error': str}."""
        mailbox = await self._require_mailbox(mailbox_id)
        self._require_admin(mailbox, user_ctx)
        try:
            backends = EmailBackend.registered_backends()
            backend_cls = backends.get(mailbox.backend_name)
            if backend_cls is None:
                return {"ok": False, "error": f"Unknown backend: {mailbox.backend_name}"}
            probe = backend_cls()
            settings = dict(mailbox.backend_config)
            if mailbox.email_address and "email_address" not in settings:
                settings["email_address"] = mailbox.email_address
            await probe.initialize(settings)
            try:
                await probe.list_message_ids(max_results=1)
            finally:
                await probe.close()
            return {"ok": True, "error": ""}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def _publish_mailbox_event(self, event_type: str, mailbox: Mailbox) -> None:
        if self._event_bus is None:
            return
        from gilbert.interfaces.events import Event

        await self._event_bus.publish(
            Event(
                event_type=event_type,
                data={
                    "mailbox_id": mailbox.id,
                    "name": mailbox.name,
                    "owner_user_id": mailbox.owner_user_id,
                },
                source="inbox",
            )
        )

    async def _publish_shares_changed(self, mailbox: Mailbox) -> None:
        if self._event_bus is None:
            return
        from gilbert.interfaces.events import Event

        await self._event_bus.publish(
            Event(
                event_type="inbox.mailbox.shares.changed",
                data={
                    "mailbox_id": mailbox.id,
                    "owner_user_id": mailbox.owner_user_id,
                    "shared_with_users": list(mailbox.shared_with_users),
                    "shared_with_roles": list(mailbox.shared_with_roles),
                },
                source="inbox",
            )
        )

    # ── Polling ──────────────────────────────────────────────────────

    def _make_poll_callback(self, mailbox_id: str) -> Callable[[], Awaitable[None]]:
        async def _run() -> None:
            runtime = self._runtimes.get(mailbox_id)
            if runtime is None:
                return
            await self._poll_runtime(runtime)

        return _run

    async def _poll_runtime(self, runtime: _MailboxRuntime) -> None:
        """Fetch new messages for one mailbox, persist, and publish events."""
        backend = runtime.backend
        mailbox = runtime.mailbox

        try:
            all_ids = await backend.list_message_ids(
                query="in:inbox is:unread",
                max_results=100,
            )
        except Exception:
            logger.exception("Inbox poll (%s): failed to list messages", mailbox.id)
            return

        new_ids: list[str] = []
        for mid in all_ids:
            if await self._storage.exists(_MESSAGES_COLLECTION, mid):
                break
            new_ids.append(mid)

        if not new_ids:
            return

        new_count = 0
        for mid in new_ids:
            try:
                msg = await backend.get_message(mid)
            except Exception:
                logger.warning("Inbox poll (%s): failed to fetch %s", mailbox.id, mid)
                continue
            if msg is None:
                continue

            is_inbound = not self._is_own_message(msg, mailbox)
            await self._persist_message(msg, mailbox, is_inbound=is_inbound)

            try:
                await backend.mark_read(mid)
            except Exception:
                logger.warning("Inbox poll (%s): failed to mark %s as read", mailbox.id, mid)

            new_count += 1

            if self._event_bus is not None:
                from gilbert.interfaces.events import Event

                original_sender = (msg.headers or {}).get("x-original-sender", "")
                await self._event_bus.publish(
                    Event(
                        event_type="inbox.message.received",
                        data={
                            "mailbox_id": mailbox.id,
                            "message_id": msg.message_id,
                            "thread_id": msg.thread_id,
                            "subject": msg.subject,
                            "sender_email": msg.sender.email,
                            "sender_name": msg.sender.name,
                            "is_inbound": is_inbound,
                            "original_sender": original_sender,
                        },
                        source="inbox",
                    )
                )

        if new_count:
            logger.info("Inbox poll (%s): %d new message(s)", mailbox.id, new_count)

    def _is_own_message(self, msg: EmailMessage, mailbox: Mailbox) -> bool:
        if not mailbox.email_address:
            return False
        return msg.sender.email.lower() == mailbox.email_address.lower()

    async def _persist_message(
        self,
        msg: EmailMessage,
        mailbox: Mailbox,
        is_inbound: bool,
    ) -> None:
        body_text = msg.body_text
        if len(body_text) > self._max_body_length:
            body_text = body_text[: self._max_body_length] + "\n... [truncated]"
        body_html = msg.body_html
        if len(body_html) > self._max_body_length:
            body_html = body_html[: self._max_body_length]

        await self._storage.put(
            _MESSAGES_COLLECTION,
            msg.message_id,
            {
                "mailbox_id": mailbox.id,
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
            },
        )

    # ── Messages: search / get / thread / stats ──────────────────────

    async def _accessible_mailbox_ids(self, user_ctx: UserContext) -> list[str]:
        mailboxes = await self.list_accessible_mailboxes(user_ctx)
        return [m.id for m in mailboxes]

    async def search_messages(
        self,
        mailbox_id: str | None = None,
        sender: str = "",
        subject: str = "",
        limit: int = 20,
        include_body: bool = True,
    ) -> list[dict[str, Any]]:
        """Search persisted messages visible to the current user.

        If ``mailbox_id`` is given, only that mailbox (after access
        check). If ``None``, returns messages aggregated across every
        mailbox the current user can access.
        """
        user_ctx = get_current_user()

        filters: list[Filter] = []
        if mailbox_id is not None:
            mailbox = await self._require_mailbox(mailbox_id)
            self._require_access(mailbox, user_ctx)
            filters.append(Filter(field="mailbox_id", op=FilterOp.EQ, value=mailbox_id))
        else:
            ids = await self._accessible_mailbox_ids(user_ctx)
            if not ids:
                return []
            filters.append(Filter(field="mailbox_id", op=FilterOp.IN, value=ids))

        if sender:
            filters.append(Filter(field="sender_email", op=FilterOp.CONTAINS, value=sender.lower()))
        if subject:
            filters.append(Filter(field="subject", op=FilterOp.CONTAINS, value=subject))

        results = await self._storage.query(
            Query(
                collection=_MESSAGES_COLLECTION,
                filters=filters,
                sort=[SortField(field="date", descending=True)],
                limit=limit,
            )
        )

        if not include_body:
            for r in results:
                body = r.get("body_text", "")
                r["snippet"] = body[:120] + ("..." if len(body) > 120 else "")
                r.pop("body_text", None)
                r.pop("body_html", None)
        return list(results)

    async def get_message(self, message_id: str) -> dict[str, Any] | None:
        user_ctx = get_current_user()
        record = await self._storage.get(_MESSAGES_COLLECTION, message_id)
        if record is None:
            return None
        mailbox_id = record.get("mailbox_id")
        if mailbox_id:
            mailbox = await self.get_mailbox(str(mailbox_id))
            if mailbox is not None:
                self._require_access(mailbox, user_ctx)
        return dict(record)

    async def get_thread(
        self,
        thread_id: str,
        mailbox_id: str | None = None,
    ) -> list[dict[str, Any]]:
        user_ctx = get_current_user()

        if mailbox_id is not None:
            mailbox = await self._require_mailbox(mailbox_id)
            self._require_access(mailbox, user_ctx)
            mailbox_ids = [mailbox_id]
        else:
            mailbox_ids = await self._accessible_mailbox_ids(user_ctx)
            if not mailbox_ids:
                return []

        return list(
            await self._storage.query(
                Query(
                    collection=_MESSAGES_COLLECTION,
                    filters=[
                        Filter(field="mailbox_id", op=FilterOp.IN, value=mailbox_ids),
                        Filter(field="thread_id", op=FilterOp.EQ, value=thread_id),
                    ],
                    sort=[SortField(field="date", descending=False)],
                )
            )
        )

    async def get_stats(
        self,
        mailbox_id: str | None = None,
    ) -> dict[str, int]:
        user_ctx = get_current_user()

        if mailbox_id is not None:
            mailbox = await self._require_mailbox(mailbox_id)
            self._require_access(mailbox, user_ctx)
            mailbox_ids = [mailbox_id]
        else:
            mailbox_ids = await self._accessible_mailbox_ids(user_ctx)
            if not mailbox_ids:
                return {"total": 0, "inbound": 0}

        total = await self._storage.count(
            Query(
                collection=_MESSAGES_COLLECTION,
                filters=[Filter(field="mailbox_id", op=FilterOp.IN, value=mailbox_ids)],
            )
        )
        inbound = await self._storage.count(
            Query(
                collection=_MESSAGES_COLLECTION,
                filters=[
                    Filter(field="mailbox_id", op=FilterOp.IN, value=mailbox_ids),
                    Filter(field="is_inbound", op=FilterOp.EQ, value=True),
                ],
            )
        )
        return {"total": total, "inbound": inbound}

    # ── Direct send (bypass outbox) ──────────────────────────────────

    async def send_message(
        self,
        mailbox_id: str,
        to: list[EmailAddress],
        subject: str,
        body_html: str,
        user_ctx: UserContext,
        body_text: str = "",
        cc: list[EmailAddress] | None = None,
        attachments: list[EmailAttachment] | None = None,
        reply_to: EmailAddress | None = None,
        from_name: str = "",
    ) -> str:
        """Compose and send a new email immediately (not queued)."""
        mailbox = await self._require_mailbox(mailbox_id)
        self._require_access(mailbox, user_ctx)
        runtime = self._runtimes.get(mailbox_id)
        if runtime is None:
            raise RuntimeError(f"Mailbox {mailbox_id} runtime is not active")

        sent_id = await runtime.backend.send(
            to=to,
            subject=subject,
            body_html=body_html,
            body_text=body_text,
            cc=cc,
            attachments=attachments,
            reply_to=reply_to,
            from_name=from_name,
        )

        now = datetime.now(UTC)
        await self._storage.put(
            _MESSAGES_COLLECTION,
            sent_id,
            {
                "mailbox_id": mailbox.id,
                "message_id": sent_id,
                "thread_id": sent_id,  # new thread
                "subject": subject,
                "sender_email": mailbox.email_address,
                "sender_name": from_name,
                "to": [{"email": a.email, "name": a.name} for a in to],
                "cc": [{"email": a.email, "name": a.name} for a in (cc or [])],
                "body_text": body_text or body_html,
                "body_html": body_html,
                "date": now.isoformat(),
                "in_reply_to": "",
                "is_inbound": False,
            },
        )

        if self._event_bus is not None:
            from gilbert.interfaces.events import Event

            await self._event_bus.publish(
                Event(
                    event_type="inbox.message.sent",
                    data={
                        "mailbox_id": mailbox.id,
                        "message_id": sent_id,
                        "subject": subject,
                        "to": [a.email for a in to],
                    },
                    source="inbox",
                )
            )

        return sent_id

    async def reply_to_message(
        self,
        mailbox_id: str,
        message_id: str,
        body_html: str,
        user_ctx: UserContext,
        body_text: str = "",
        cc: list[EmailAddress] | None = None,
        attachments: list[EmailAttachment] | None = None,
        reply_to: EmailAddress | None = None,
        from_name: str = "",
    ) -> str:
        """Reply to an existing message. Returns the sent message's ID."""
        mailbox = await self._require_mailbox(mailbox_id)
        self._require_access(mailbox, user_ctx)
        runtime = self._runtimes.get(mailbox_id)
        if runtime is None:
            raise RuntimeError(f"Mailbox {mailbox_id} runtime is not active")

        record = await self._storage.get(_MESSAGES_COLLECTION, message_id)
        if not record:
            raise ValueError(f"Message {message_id} not found")
        if record.get("mailbox_id") != mailbox.id:
            raise ValueError(
                f"Message {message_id} does not belong to mailbox {mailbox.id}",
            )

        to = [EmailAddress(email=record["sender_email"], name=record.get("sender_name", ""))]
        subject = record["subject"]
        thread_id = record.get("thread_id", "")
        in_reply_to = record.get("in_reply_to", "")

        sent_id = await runtime.backend.send(
            to=to,
            subject=subject,
            body_html=body_html,
            body_text=body_text,
            cc=cc,
            in_reply_to=in_reply_to,
            thread_id=thread_id,
            attachments=attachments,
            reply_to=reply_to,
            from_name=from_name,
        )

        now = datetime.now(UTC)
        await self._storage.put(
            _MESSAGES_COLLECTION,
            sent_id,
            {
                "mailbox_id": mailbox.id,
                "message_id": sent_id,
                "thread_id": thread_id,
                "subject": (f"Re: {subject}" if not subject.startswith("Re:") else subject),
                "sender_email": mailbox.email_address,
                "sender_name": from_name,
                "to": [{"email": a.email, "name": a.name} for a in to],
                "cc": [{"email": a.email, "name": a.name} for a in (cc or [])],
                "body_text": body_text or body_html,
                "body_html": body_html,
                "date": now.isoformat(),
                "in_reply_to": in_reply_to,
                "is_inbound": False,
            },
        )

        if self._event_bus is not None:
            from gilbert.interfaces.events import Event

            await self._event_bus.publish(
                Event(
                    event_type="inbox.message.replied",
                    data={
                        "mailbox_id": mailbox.id,
                        "message_id": sent_id,
                        "thread_id": thread_id,
                        "in_reply_to_message": message_id,
                    },
                    source="inbox",
                )
            )

        return sent_id

    # ── Outbox (InboxProvider) ───────────────────────────────────

    async def schedule_send(
        self,
        mailbox_id: str,
        draft: OutboxDraft,
        user_ctx: UserContext,
        send_at: datetime | None = None,
    ) -> str:
        """Queue a draft for sending. Returns the outbox entry id."""
        mailbox = await self._require_mailbox(mailbox_id)
        self._require_access(mailbox, user_ctx)

        now = datetime.now(UTC)
        when = send_at or now
        outbox_id = f"out_{uuid.uuid4().hex[:12]}"
        row = {
            "id": outbox_id,
            "mailbox_id": mailbox_id,
            "status": OutboxStatus.PENDING.value,
            "send_at": when.isoformat(),
            "draft": draft.to_dict(),
            "created_by_user_id": user_ctx.user_id,
            "created_at": now.isoformat(),
            "sent_at": None,
            "error": None,
            "retry_count": 0,
        }
        await self._storage.put(_OUTBOX_COLLECTION, outbox_id, row)
        return outbox_id

    async def cancel_outbox(
        self,
        outbox_id: str,
        user_ctx: UserContext,
    ) -> bool:
        """Cancel a pending/failed outbox entry. Any user with access
        to the mailbox may cancel — not just the draft creator.
        """
        record = await self._storage.get(_OUTBOX_COLLECTION, outbox_id)
        if record is None:
            return False
        mailbox = await self.get_mailbox(str(record.get("mailbox_id", "")))
        if mailbox is None:
            return False
        self._require_access(mailbox, user_ctx)

        status = record.get("status")
        if status not in (OutboxStatus.PENDING.value, OutboxStatus.FAILED.value):
            return False
        record["status"] = OutboxStatus.CANCELLED.value
        await self._storage.put(_OUTBOX_COLLECTION, outbox_id, record)
        return True

    async def list_outbox(
        self,
        mailbox_id: str | None = None,
        status: OutboxStatus | None = None,
    ) -> list[OutboxEntry]:
        """List outbox entries for mailboxes the current user can access."""
        user_ctx = get_current_user()

        if mailbox_id is not None:
            mailbox = await self._require_mailbox(mailbox_id)
            self._require_access(mailbox, user_ctx)
            mailbox_ids = [mailbox_id]
        else:
            mailbox_ids = await self._accessible_mailbox_ids(user_ctx)
            if not mailbox_ids:
                return []

        filters = [Filter(field="mailbox_id", op=FilterOp.IN, value=mailbox_ids)]
        if status is not None:
            filters.append(Filter(field="status", op=FilterOp.EQ, value=status.value))

        rows = await self._storage.query(
            Query(
                collection=_OUTBOX_COLLECTION,
                filters=filters,
                sort=[SortField(field="send_at", descending=False)],
            )
        )
        return [self._row_to_outbox_entry(r) for r in rows]

    def _row_to_outbox_entry(self, row: dict[str, Any]) -> OutboxEntry:
        draft_data = row.get("draft") or {}
        if not isinstance(draft_data, dict):
            draft_data = {}
        return OutboxEntry(
            id=str(row.get("_id") or row.get("id") or ""),
            mailbox_id=str(row.get("mailbox_id", "")),
            status=OutboxStatus(str(row.get("status", OutboxStatus.PENDING.value))),
            send_at=str(row.get("send_at", "")),
            draft=OutboxDraft.from_dict(draft_data),
            created_by_user_id=str(row.get("created_by_user_id", "")),
            created_at=str(row.get("created_at", "")),
            sent_at=(str(row["sent_at"]) if row.get("sent_at") else None),
            error=(str(row["error"]) if row.get("error") else None),
            retry_count=int(row.get("retry_count", 0) or 0),
        )

    async def _outbox_tick(self) -> None:
        """Flush pending outbox entries whose send_at is due."""
        if self._outbox_busy:
            return
        self._outbox_busy = True
        try:
            now = datetime.now(UTC).isoformat()
            due = await self._storage.query(
                Query(
                    collection=_OUTBOX_COLLECTION,
                    filters=[
                        Filter(field="status", op=FilterOp.EQ, value=OutboxStatus.PENDING.value),
                        Filter(field="send_at", op=FilterOp.LTE, value=now),
                    ],
                    sort=[SortField(field="send_at", descending=False)],
                    limit=20,
                )
            )
            for row in due:
                await self._send_outbox_row(row)
        except Exception:
            logger.exception("Inbox outbox tick failed")
        finally:
            self._outbox_busy = False

    async def _send_outbox_row(self, row: dict[str, Any]) -> None:
        outbox_id = str(row.get("_id") or row.get("id") or "")
        mailbox_id = str(row.get("mailbox_id", ""))
        mailbox = await self.get_mailbox(mailbox_id)
        if mailbox is None:
            row["status"] = OutboxStatus.FAILED.value
            row["error"] = f"Mailbox {mailbox_id} not found"
            await self._storage.put(_OUTBOX_COLLECTION, outbox_id, row)
            return

        runtime = self._runtimes.get(mailbox_id)
        if runtime is None:
            # Service-toggleable: leave pending, try next tick.
            return

        # Transition to sending.
        row["status"] = OutboxStatus.SENDING.value
        await self._storage.put(_OUTBOX_COLLECTION, outbox_id, row)

        try:
            draft_data = row.get("draft") or {}
            if not isinstance(draft_data, dict):
                draft_data = {}
            draft = OutboxDraft.from_dict(draft_data)

            attachments, attach_errors = await self._resolve_attachments(
                draft.attach_documents
            )
            if attach_errors:
                # If any attachment failed to resolve at flush time, fail
                # the send loudly. The user already saw success at tool-
                # call time (where we eagerly resolve), so reaching this
                # branch means the file was deleted between then and
                # now — surface that instead of sending a half-empty
                # email.
                raise RuntimeError(
                    "attachment resolution failed: " + "; ".join(attach_errors)
                )

            sent_id = await runtime.backend.send(
                to=draft.to,
                subject=draft.subject,
                body_html=draft.body_html,
                body_text=draft.body_text,
                cc=draft.cc,
                in_reply_to=draft.in_reply_to,
                thread_id=draft.thread_id,
                attachments=attachments or None,
                reply_to=draft.reply_to,
                from_name=draft.from_name,
            )

            now = datetime.now(UTC)
            row["status"] = OutboxStatus.SENT.value
            row["sent_at"] = now.isoformat()
            row["error"] = None
            await self._storage.put(_OUTBOX_COLLECTION, outbox_id, row)

            # Persist sent message in the messages collection too.
            await self._storage.put(
                _MESSAGES_COLLECTION,
                sent_id,
                {
                    "mailbox_id": mailbox.id,
                    "message_id": sent_id,
                    "thread_id": draft.thread_id or sent_id,
                    "subject": draft.subject,
                    "sender_email": mailbox.email_address,
                    "sender_name": draft.from_name,
                    "to": [{"email": a.email, "name": a.name} for a in draft.to],
                    "cc": [{"email": a.email, "name": a.name} for a in (draft.cc or [])],
                    "body_text": draft.body_text or draft.body_html,
                    "body_html": draft.body_html,
                    "date": now.isoformat(),
                    "in_reply_to": draft.in_reply_to,
                    "is_inbound": False,
                },
            )

            if self._event_bus is not None:
                from gilbert.interfaces.events import Event

                await self._event_bus.publish(
                    Event(
                        event_type="inbox.outbox.sent",
                        data={
                            "mailbox_id": mailbox.id,
                            "outbox_id": outbox_id,
                            "message_id": sent_id,
                            "subject": draft.subject,
                        },
                        source="inbox",
                    )
                )
        except Exception as exc:
            logger.exception("Outbox send failed for %s", outbox_id)
            row["status"] = OutboxStatus.FAILED.value
            row["error"] = str(exc)
            row["retry_count"] = int(row.get("retry_count", 0) or 0) + 1
            await self._storage.put(_OUTBOX_COLLECTION, outbox_id, row)

            if self._event_bus is not None:
                from gilbert.interfaces.events import Event

                await self._event_bus.publish(
                    Event(
                        event_type="inbox.outbox.failed",
                        data={
                            "mailbox_id": mailbox.id,
                            "outbox_id": outbox_id,
                            "error": str(exc),
                        },
                        source="inbox",
                    )
                )

    # ── Attachments ──────────────────────────────────────────────────

    async def _resolve_attachments(
        self,
        document_ids: list[str] | None,
    ) -> tuple[list[EmailAttachment], list[str]]:
        """Resolve attachment refs to email attachments.

        Accepts two URI formats:

        - **Knowledge document ref** — ``<source_id>:<path>``, e.g.
          ``local_docs:reports/march.pdf``. Resolved via the active
          ``KnowledgeService`` backend that owns that source_id. Same
          shape as before; existing knowledge attachments still work.

        - **Workspace file ref** — ``workspace:<user_id>/<conv_id>/<skill>/<path>``,
          e.g. ``workspace:usr_28ff/cc2b54.../pdf/po-00006567.pdf``. Resolved
          via ``WorkspaceProvider.resolve_file_path`` (which tries the
          per-conversation workspace first, then falls back to the legacy
          per-(user, skill) path). Self-contained on purpose so the outbox
          processor — which runs decoupled from the original tool call's
          user context — can still resolve the file at flush time.

        Returns a tuple ``(attachments, errors)``. ``errors`` is a list
        of human-readable strings describing why each unresolved ref
        failed; the calling tool surfaces them as a ``ToolResult`` error
        so the AI sees the failure instead of silently shipping an
        attachment-less email.
        """
        if not document_ids:
            return [], []

        attachments: list[EmailAttachment] = []
        errors: list[str] = []
        for doc_id in document_ids:
            try:
                if doc_id.startswith("workspace:"):
                    att, err = await self._resolve_workspace_attachment(doc_id)
                else:
                    att, err = await self._resolve_knowledge_attachment(doc_id)
            except Exception as exc:
                logger.warning(
                    "Failed to resolve attachment %s",
                    doc_id,
                    exc_info=True,
                )
                att, err = None, f"{doc_id}: {exc}"
            if att is not None:
                attachments.append(att)
            elif err is not None:
                errors.append(err)
        return attachments, errors

    async def _resolve_knowledge_attachment(
        self,
        doc_id: str,
    ) -> tuple[EmailAttachment | None, str | None]:
        """Look up a knowledge-store ref and read its bytes."""
        if self._knowledge is None:
            return None, f"{doc_id}: knowledge service not available"
        parts = doc_id.split(":", 1)
        if len(parts) != 2:
            return None, f"{doc_id}: invalid document ID format"
        source_id_prefix, path = parts[0], parts[1]

        backend = None
        for sid, b in self._knowledge.backends.items():
            if sid == doc_id[: len(sid)]:
                backend = b
                path = doc_id[len(sid) + 1 :]
                break
        if backend is None:
            for sid, b in self._knowledge.backends.items():
                if sid.endswith(source_id_prefix):
                    backend = b
                    break
        if backend is None:
            return None, f"{doc_id}: no knowledge backend matches"

        content = await backend.get_document(path)
        if content is None:
            return None, f"{doc_id}: document not found in knowledge store"

        return (
            EmailAttachment(
                filename=content.meta.name,
                data=content.data,
                mime_type=content.meta.mime_type,
            ),
            None,
        )

    def _get_workspace_service(self) -> WorkspaceProvider | None:
        """Resolve the ``workspace`` capability lazily.

        InboxService can start before WorkspaceService since the
        topological sort only orders by ``requires``, not ``optional``.
        Looking up the workspace capability at use time (after all
        services have finished starting) avoids the start-order race.
        """
        if self._resolver is None:
            return None
        svc = self._resolver.get_capability("workspace")
        if isinstance(svc, WorkspaceProvider):
            return svc
        return None

    async def _resolve_workspace_attachment(
        self,
        doc_id: str,
    ) -> tuple[EmailAttachment | None, str | None]:
        """Look up a ``workspace:<user>/<conv>/<skill>/<path>`` ref and
        read the file's bytes off disk via ``SkillService``."""
        workspace = self._get_workspace_service()
        if workspace is None:
            return None, f"{doc_id}: workspace service not available"
        # Strip the scheme.
        body = doc_id[len("workspace:") :]
        # Expect at least 4 path segments: user / conv / skill / path...
        parts = body.split("/", 3)
        if len(parts) < 4:
            return (
                None,
                (
                    f"{doc_id}: invalid workspace ref — expected "
                    "'workspace:<user_id>/<conv_id>/<skill>/<path>'"
                ),
            )
        user_id, conv_id, skill_name, rel_path = parts
        if not user_id or not skill_name or not rel_path:
            return None, f"{doc_id}: workspace ref has empty segments"

        # Resolve via WorkspaceService — handles conv-scoped + legacy
        # fallback + path traversal check. The split rejoins skill/path
        # so new-layout refs (uploads/foo, outputs/bar, scratch/baz)
        # land directly on disk; legacy refs fall through to the
        # per-skill iteration inside ``resolve_file_path``.
        target, err = workspace.resolve_file_path(
            user_id=user_id,
            rel_path=f"{skill_name}/{rel_path}",
            conversation_id=conv_id or None,
        )
        if err is not None or target is None:
            return None, f"{doc_id}: {err or 'not found'}"

        try:
            data = await asyncio.to_thread(target.read_bytes)
        except OSError as exc:
            return None, f"{doc_id}: cannot read file: {exc}"

        # Mime sniff + filename from the path basename.
        import mimetypes

        mime_type, _enc = mimetypes.guess_type(target.name)
        return (
            EmailAttachment(
                filename=target.name,
                data=data,
                mime_type=mime_type or "application/octet-stream",
            ),
            None,
        )

    # ── ToolProvider ─────────────────────────────────────────────────

    @property
    def tool_provider_name(self) -> str:
        return "inbox"

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        return [
            ToolDefinition(
                name="inbox_mailboxes",
                slash_group="inbox",
                slash_command="mailboxes",
                slash_help="List mailboxes you can access: /inbox mailboxes",
                description=(
                    "List every email mailbox the current user can access, "
                    "with each mailbox's id, name, and how access was granted "
                    "(owner / admin / shared). Call this first when the user's "
                    "intent doesn't already name a specific mailbox."
                ),
                parameters=[],
                required_role="user",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="inbox_search",
                slash_group="inbox",
                slash_command="search",
                slash_help=(
                    "Search a mailbox: /inbox search <mailbox_id> "
                    "[sender=...] [subject=...] [limit=20]"
                ),
                description=(
                    "Search persisted messages in a specific mailbox by "
                    "sender and/or subject. Call /inbox mailboxes first if "
                    "you don't know the mailbox_id."
                ),
                parameters=[
                    ToolParameter(
                        name="mailbox_id",
                        type=ToolParameterType.STRING,
                        description="The mailbox to search (from /inbox mailboxes).",
                    ),
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
                parallel_safe=True,
            ),
            ToolDefinition(
                name="inbox_read",
                slash_group="inbox",
                slash_command="read",
                slash_help="Read a message: /inbox read <mailbox_id> <message_id>",
                description=(
                    "Read the full content of a single email message in a specific mailbox."
                ),
                parameters=[
                    ToolParameter(
                        name="mailbox_id",
                        type=ToolParameterType.STRING,
                        description="The mailbox the message belongs to.",
                    ),
                    ToolParameter(
                        name="message_id",
                        type=ToolParameterType.STRING,
                        description="The message ID to read.",
                    ),
                ],
                required_role="user",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="inbox_reply",
                description=(
                    "Reply to an email message in a specific mailbox. The "
                    "reply is threaded in the same conversation. Provide "
                    "the body as HTML. Optionally attach files via "
                    "``attach_documents`` — see that parameter for the "
                    "two supported reference formats."
                ),
                parameters=[
                    ToolParameter(
                        name="mailbox_id",
                        type=ToolParameterType.STRING,
                        description="The mailbox to send from.",
                    ),
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
                            "Optional list of attachment refs. Two formats:\n"
                            "1. **Knowledge store**: ``<source_id>:<path>`` "
                            "(e.g. ``local_docs:reports/march.pdf``).\n"
                            "2. **Skill workspace file**: "
                            "``workspace:<skill>/<path>`` (e.g. "
                            "``workspace:pdf/po-00006567.pdf``). Resolves "
                            "in the current conversation's workspace, so "
                            "you can attach files you generated this turn "
                            "via write_skill_workspace_file + "
                            "run_workspace_script. If any ref fails to "
                            "resolve the email is NOT sent — fix or remove "
                            "the failing ref and retry."
                        ),
                        required=False,
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="inbox_send",
                description=(
                    "Compose and send a new email from a specific mailbox. "
                    "Optionally attach files via ``attach_documents`` — see "
                    "that parameter for the two supported reference formats."
                ),
                parameters=[
                    ToolParameter(
                        name="mailbox_id",
                        type=ToolParameterType.STRING,
                        description="The mailbox to send from.",
                    ),
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
                            "Optional list of attachment refs. Two formats:\n"
                            "1. **Knowledge store**: ``<source_id>:<path>`` "
                            "(e.g. ``local_docs:reports/march.pdf``).\n"
                            "2. **Skill workspace file**: "
                            "``workspace:<skill>/<path>`` (e.g. "
                            "``workspace:pdf/po-00006567.pdf``). Resolves "
                            "in the current conversation's workspace, so "
                            "you can attach files you generated this turn "
                            "via write_skill_workspace_file + "
                            "run_workspace_script. If any ref fails to "
                            "resolve the email is NOT sent — fix or remove "
                            "the failing ref and retry."
                        ),
                        required=False,
                    ),
                ],
                required_role="user",
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        match name:
            case "inbox_mailboxes":
                return await self._tool_mailboxes()
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

    async def _tool_mailboxes(self) -> str:
        user_ctx = get_current_user()
        is_admin = self._is_admin(user_ctx)
        all_mailboxes = await self._load_mailboxes()
        out: list[dict[str, Any]] = []
        for m in all_mailboxes:
            access = determine_access(user_ctx, m, is_admin=is_admin)
            if access is None:
                continue
            out.append(
                {
                    "id": m.id,
                    "name": m.name,
                    "email_address": m.email_address,
                    "access": access.value,
                }
            )
        if not out:
            return "You have no accessible mailboxes."
        return json.dumps(out, indent=2)

    async def _tool_search(self, args: dict[str, Any]) -> str:
        mailbox_id = str(args.get("mailbox_id") or "")
        if not mailbox_id:
            return (
                "mailbox_id is required. Call /inbox mailboxes to list "
                "the mailboxes you can access."
            )
        try:
            results = await self.search_messages(
                mailbox_id=mailbox_id,
                sender=str(args.get("sender", "")),
                subject=str(args.get("subject", "")),
                limit=int(args.get("limit", 20) or 20),
            )
        except MailboxNotFoundError:
            return f"Mailbox not found: {mailbox_id}"
        except InboxPermissionError:
            return (
                f"You don't have access to mailbox {mailbox_id}. "
                "Call /inbox mailboxes to see what you can access."
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
        mailbox_id = str(args.get("mailbox_id") or "")
        message_id = str(args.get("message_id") or "")
        if not mailbox_id:
            return (
                "mailbox_id is required. Call /inbox mailboxes to list "
                "the mailboxes you can access."
            )
        if not message_id:
            return "message_id is required."

        try:
            mailbox = await self._require_mailbox(mailbox_id)
            self._require_access(mailbox, get_current_user())
        except MailboxNotFoundError:
            return f"Mailbox not found: {mailbox_id}"
        except InboxPermissionError:
            return (
                f"You don't have access to mailbox {mailbox_id}. "
                "Call /inbox mailboxes to see what you can access."
            )

        record = await self._storage.get(_MESSAGES_COLLECTION, message_id)
        if not record:
            return f"Message {message_id} not found."
        if record.get("mailbox_id") != mailbox.id:
            return f"Message {message_id} does not belong to mailbox {mailbox.id}."

        return json.dumps(
            {
                "mailbox_id": mailbox.id,
                "message_id": record.get("_id", ""),
                "thread_id": record.get("thread_id", ""),
                "date": record.get("date", ""),
                "subject": record.get("subject", ""),
                "from": f"{record.get('sender_name', '')} <{record.get('sender_email', '')}>",
                "to": record.get("to", []),
                "cc": record.get("cc", []),
                "body": record.get("body_text", ""),
                "is_inbound": record.get("is_inbound", True),
            },
            indent=2,
        )

    @staticmethod
    def _normalize_attach_refs(
        refs: Any,
        injected_user_id: str,
        injected_conv_id: str,
    ) -> list[str]:
        """Expand short workspace refs to the self-contained URI form.

        AI-friendly form: ``workspace:<skill>/<path>`` (omits user_id and
        conv_id since the AI has them implicitly via the injected args).
        Outbox-storable form: ``workspace:<user_id>/<conv_id>/<skill>/<path>``
        (self-contained so the outbox processor can resolve at flush
        time without any caller context).

        Knowledge document refs (``<source>:<path>``) and already-full
        workspace URIs pass through untouched.
        """
        if not isinstance(refs, list):
            return []
        out: list[str] = []
        for raw in refs:
            ref = str(raw or "").strip()
            if not ref:
                continue
            if not ref.startswith("workspace:"):
                out.append(ref)
                continue
            body = ref[len("workspace:") :]
            parts = body.split("/", 3)
            # Already 4+ segments → self-contained URI, keep as-is.
            if len(parts) >= 4:
                out.append(ref)
                continue
            # Short form ``workspace:<skill>/<path>`` → expand using
            # the injected user_id + conv_id.
            if len(parts) >= 2 and injected_user_id and injected_conv_id:
                skill_path = body  # everything after "workspace:"
                out.append(
                    f"workspace:{injected_user_id}/{injected_conv_id}/{skill_path}"
                )
                continue
            # Couldn't expand — pass through and let the resolver
            # reject it with a helpful error.
            out.append(ref)
        return out

    async def _tool_reply(self, args: dict[str, Any]) -> str:
        user_ctx = get_current_user()
        mailbox_id = str(args.get("mailbox_id") or "")
        message_id = str(args.get("message_id") or "")
        body_html = str(args.get("body_html") or "")
        if not mailbox_id:
            return (
                "mailbox_id is required. Call /inbox mailboxes to list "
                "the mailboxes you can access."
            )
        if not message_id:
            return "message_id is required."
        if not body_html:
            return "body_html is required."

        attach_refs = self._normalize_attach_refs(
            args.get("attach_documents"),
            str(args.get("_user_id") or ""),
            str(args.get("_conversation_id") or ""),
        )
        attachments, attach_errors = await self._resolve_attachments(attach_refs)
        if attach_errors:
            return (
                "Could not attach: "
                + "; ".join(attach_errors)
                + ". The reply was NOT sent. Fix or remove the failing "
                "attachments and retry."
            )

        try:
            sent_id = await self.reply_to_message(
                mailbox_id=mailbox_id,
                message_id=message_id,
                body_html=body_html,
                user_ctx=user_ctx,
                body_text=str(args.get("body_text", "")),
                attachments=attachments or None,
            )
        except MailboxNotFoundError:
            return f"Mailbox not found: {mailbox_id}"
        except InboxPermissionError:
            return (
                f"You don't have access to mailbox {mailbox_id}. "
                "Call /inbox mailboxes to see what you can access."
            )
        except ValueError as e:
            return str(e)

        att_msg = f" with {len(attachments)} attachment(s)" if attachments else ""
        return f"Reply sent{att_msg} (message ID: {sent_id})."

    async def _tool_send(self, args: dict[str, Any]) -> str:
        user_ctx = get_current_user()
        mailbox_id = str(args.get("mailbox_id") or "")
        to_raw = args.get("to", [])
        subject = str(args.get("subject", ""))
        body_html = str(args.get("body_html", ""))

        if not mailbox_id:
            return (
                "mailbox_id is required. Call /inbox mailboxes to list "
                "the mailboxes you can access."
            )
        if not to_raw:
            return "to is required."
        if not subject:
            return "subject is required."
        if not body_html:
            return "body_html is required."

        to = [EmailAddress(email=addr) for addr in to_raw]
        cc_raw = args.get("cc") or []
        cc = [EmailAddress(email=addr) for addr in cc_raw] if cc_raw else None
        attach_refs = self._normalize_attach_refs(
            args.get("attach_documents"),
            str(args.get("_user_id") or ""),
            str(args.get("_conversation_id") or ""),
        )
        attachments, attach_errors = await self._resolve_attachments(attach_refs)
        if attach_errors:
            return (
                "Could not attach: "
                + "; ".join(attach_errors)
                + ". The email was NOT sent. Fix or remove the failing "
                "attachments and retry."
            )

        try:
            sent_id = await self.send_message(
                mailbox_id=mailbox_id,
                to=to,
                subject=subject,
                body_html=body_html,
                user_ctx=user_ctx,
                body_text=str(args.get("body_text", "")),
                cc=cc,
                attachments=attachments or None,
            )
        except MailboxNotFoundError:
            return f"Mailbox not found: {mailbox_id}"
        except InboxPermissionError:
            return (
                f"You don't have access to mailbox {mailbox_id}. "
                "Call /inbox mailboxes to see what you can access."
            )

        att_msg = f" with {len(attachments)} attachment(s)" if attachments else ""
        return f"Email sent{att_msg} (message ID: {sent_id})."

    # ── WebSocket RPC handlers ───────────────────────────────────────

    def get_ws_handlers(self) -> dict[str, Any]:
        return {
            # Messages / threads / stats
            "inbox.stats.get": self._ws_stats_get,
            "inbox.message.list": self._ws_message_list,
            "inbox.message.get": self._ws_message_get,
            "inbox.thread.get": self._ws_thread_get,
            # Outbox
            "inbox.outbox.list": self._ws_outbox_list,
            "inbox.outbox.cancel": self._ws_outbox_cancel,
            # Mailboxes
            "inbox.mailboxes.list": self._ws_mailboxes_list,
            "inbox.mailboxes.get": self._ws_mailboxes_get,
            "inbox.mailboxes.create": self._ws_mailboxes_create,
            "inbox.mailboxes.update": self._ws_mailboxes_update,
            "inbox.mailboxes.delete": self._ws_mailboxes_delete,
            "inbox.mailboxes.test_connection": self._ws_mailboxes_test,
            "inbox.mailboxes.share_user": self._ws_mailboxes_share_user,
            "inbox.mailboxes.unshare_user": self._ws_mailboxes_unshare_user,
            "inbox.mailboxes.share_role": self._ws_mailboxes_share_role,
            "inbox.mailboxes.unshare_role": self._ws_mailboxes_unshare_role,
            "inbox.backends.list": self._ws_backends_list,
        }

    def _err(self, frame: dict[str, Any], msg: str, code: int) -> dict[str, Any]:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": msg, "code": code}

    @staticmethod
    def _result_type(base: str) -> str:
        return f"{base}.result"

    # ---- Messages ----

    async def _ws_stats_get(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        mailbox_id = frame.get("mailbox_id")
        try:
            stats = await self.get_stats(mailbox_id=mailbox_id)
        except InboxPermissionError as e:
            return self._err(frame, str(e), 403)
        except MailboxNotFoundError as e:
            return self._err(frame, str(e), 404)
        return {"type": "inbox.stats.get.result", "ref": frame.get("id"), **stats}

    async def _ws_message_list(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        try:
            messages = await self.search_messages(
                mailbox_id=frame.get("mailbox_id"),
                sender=str(frame.get("sender", "")),
                subject=str(frame.get("subject", "")),
                limit=int(frame.get("limit", 50) or 50),
                include_body=False,
            )
        except InboxPermissionError as e:
            return self._err(frame, str(e), 403)
        except MailboxNotFoundError as e:
            return self._err(frame, str(e), 404)

        summaries: list[dict[str, Any]] = []
        for m in messages:
            snippet = m.get("snippet", "")
            if not snippet:
                body = m.get("body_text", "")
                snippet = body[:120] + ("..." if len(body) > 120 else "")
            summaries.append(
                {
                    "mailbox_id": m.get("mailbox_id", ""),
                    "message_id": m.get("_id", ""),
                    "thread_id": m.get("thread_id", ""),
                    "subject": m.get("subject", ""),
                    "sender_email": m.get("sender_email", ""),
                    "sender_name": m.get("sender_name", ""),
                    "date": m.get("date", ""),
                    "is_inbound": m.get("is_inbound", True),
                    "snippet": snippet,
                }
            )
        return {
            "type": "inbox.message.list.result",
            "ref": frame.get("id"),
            "messages": summaries,
            "total": len(summaries),
        }

    async def _ws_message_get(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        try:
            record = await self.get_message(str(frame.get("message_id", "")))
        except InboxPermissionError as e:
            return self._err(frame, str(e), 403)
        if not record:
            return self._err(frame, "Message not found", 404)

        return {
            "type": "inbox.message.get.result",
            "ref": frame.get("id"),
            "mailbox_id": record.get("mailbox_id", ""),
            "message_id": record.get("_id", ""),
            "thread_id": record.get("thread_id", ""),
            "subject": record.get("subject", ""),
            "sender_email": record.get("sender_email", ""),
            "sender_name": record.get("sender_name", ""),
            "date": record.get("date", ""),
            "to": record.get("to", []),
            "cc": record.get("cc", []),
            "body_text": record.get("body_text", ""),
            "body_html": record.get("body_html", ""),
            "is_inbound": record.get("is_inbound", True),
        }

    async def _ws_thread_get(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        try:
            messages = await self.get_thread(
                thread_id=str(frame.get("thread_id", "")),
                mailbox_id=frame.get("mailbox_id"),
            )
        except InboxPermissionError as e:
            return self._err(frame, str(e), 403)
        except MailboxNotFoundError as e:
            return self._err(frame, str(e), 404)

        result = []
        for m in messages:
            result.append(
                {
                    "mailbox_id": m.get("mailbox_id", ""),
                    "message_id": m.get("_id", ""),
                    "thread_id": m.get("thread_id", ""),
                    "subject": m.get("subject", ""),
                    "sender_email": m.get("sender_email", ""),
                    "sender_name": m.get("sender_name", ""),
                    "date": m.get("date", ""),
                    "to": m.get("to", []),
                    "cc": m.get("cc", []),
                    "body_text": m.get("body_text", ""),
                    "body_html": m.get("body_html", ""),
                    "is_inbound": m.get("is_inbound", True),
                }
            )
        return {
            "type": "inbox.thread.get.result",
            "ref": frame.get("id"),
            "messages": result,
        }

    # ---- Outbox ----

    async def _ws_outbox_list(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        status_raw = frame.get("status")
        status = OutboxStatus(status_raw) if status_raw else None
        try:
            entries = await self.list_outbox(
                mailbox_id=frame.get("mailbox_id"),
                status=status,
            )
        except InboxPermissionError as e:
            return self._err(frame, str(e), 403)
        except MailboxNotFoundError as e:
            return self._err(frame, str(e), 404)

        out: list[dict[str, Any]] = []
        for entry in entries:
            out.append(
                {
                    "id": entry.id,
                    "mailbox_id": entry.mailbox_id,
                    "status": entry.status.value,
                    "send_at": entry.send_at,
                    "created_by_user_id": entry.created_by_user_id,
                    "created_at": entry.created_at,
                    "sent_at": entry.sent_at,
                    "error": entry.error,
                    "retry_count": entry.retry_count,
                    "subject": entry.draft.subject,
                    "to": [a.email for a in entry.draft.to],
                }
            )
        return {
            "type": "inbox.outbox.list.result",
            "ref": frame.get("id"),
            "entries": out,
        }

    async def _ws_outbox_cancel(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        outbox_id = str(frame.get("outbox_id") or "")
        if not outbox_id:
            return self._err(frame, "outbox_id required", 400)
        try:
            ok = await self.cancel_outbox(outbox_id, conn.user_ctx)
        except InboxPermissionError as e:
            return self._err(frame, str(e), 403)
        if not ok:
            return self._err(frame, "Outbox entry not found or not cancellable", 404)
        return {
            "type": "inbox.outbox.cancel.result",
            "ref": frame.get("id"),
            "status": "cancelled",
        }

    # ---- Mailboxes ----

    def _mailbox_payload(
        self,
        mailbox: Mailbox,
        user_ctx: UserContext,
        is_admin: bool,
    ) -> dict[str, Any]:
        access = determine_access(user_ctx, mailbox, is_admin=is_admin)
        return {
            "id": mailbox.id,
            "name": mailbox.name,
            "email_address": mailbox.email_address,
            "backend_name": mailbox.backend_name,
            "backend_config": mailbox.backend_config,
            "owner_user_id": mailbox.owner_user_id,
            "shared_with_users": list(mailbox.shared_with_users),
            "shared_with_roles": list(mailbox.shared_with_roles),
            "poll_enabled": mailbox.poll_enabled,
            "poll_interval_sec": mailbox.poll_interval_sec,
            "created_at": mailbox.created_at,
            "access": access.value if access is not None else None,
            "can_admin": can_admin_mailbox(user_ctx, mailbox, is_admin=is_admin),
        }

    async def _ws_mailboxes_list(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        user_ctx = conn.user_ctx
        is_admin = self._is_admin(user_ctx)
        all_mailboxes = await self._load_mailboxes()
        out = []
        for m in all_mailboxes:
            if determine_access(user_ctx, m, is_admin=is_admin) is None:
                continue
            out.append(self._mailbox_payload(m, user_ctx, is_admin))
        return {
            "type": "inbox.mailboxes.list.result",
            "ref": frame.get("id"),
            "mailboxes": out,
        }

    async def _ws_mailboxes_get(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        mailbox_id = str(frame.get("mailbox_id") or "")
        mailbox = await self.get_mailbox(mailbox_id)
        if mailbox is None:
            return self._err(frame, "Mailbox not found", 404)
        user_ctx = conn.user_ctx
        is_admin = self._is_admin(user_ctx)
        if determine_access(user_ctx, mailbox, is_admin=is_admin) is None:
            return self._err(frame, "Forbidden", 403)
        return {
            "type": "inbox.mailboxes.get.result",
            "ref": frame.get("id"),
            "mailbox": self._mailbox_payload(mailbox, user_ctx, is_admin),
        }

    async def _ws_mailboxes_create(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        user_ctx = conn.user_ctx
        mailbox = Mailbox(
            id=str(frame.get("id") or ""),
            name=str(frame.get("name") or ""),
            email_address=str(frame.get("email_address") or ""),
            backend_name=str(frame.get("backend_name") or ""),
            backend_config=dict(frame.get("backend_config") or {}),
            poll_enabled=bool(frame.get("poll_enabled", True)),
            poll_interval_sec=int(frame.get("poll_interval_sec", 60) or 60),
        )
        if not mailbox.name or not mailbox.backend_name:
            return self._err(frame, "name and backend_name are required", 400)
        try:
            created = await self.create_mailbox(mailbox, user_ctx)
        except ValueError as e:
            return self._err(frame, str(e), 400)

        is_admin = self._is_admin(user_ctx)
        return {
            "type": "inbox.mailboxes.create.result",
            "ref": frame.get("id"),
            "mailbox": self._mailbox_payload(created, user_ctx, is_admin),
        }

    async def _ws_mailboxes_update(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        mailbox_id = str(frame.get("mailbox_id") or "")
        updates = frame.get("updates") or {}
        if not isinstance(updates, dict):
            return self._err(frame, "updates must be an object", 400)
        try:
            mailbox = await self.update_mailbox(mailbox_id, updates, conn.user_ctx)
        except InboxPermissionError as e:
            return self._err(frame, str(e), 403)
        except MailboxNotFoundError as e:
            return self._err(frame, str(e), 404)

        is_admin = self._is_admin(conn.user_ctx)
        return {
            "type": "inbox.mailboxes.update.result",
            "ref": frame.get("id"),
            "mailbox": self._mailbox_payload(mailbox, conn.user_ctx, is_admin),
        }

    async def _ws_mailboxes_delete(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        mailbox_id = str(frame.get("mailbox_id") or "")
        try:
            await self.delete_mailbox(mailbox_id, conn.user_ctx)
        except InboxPermissionError as e:
            return self._err(frame, str(e), 403)
        except MailboxNotFoundError as e:
            return self._err(frame, str(e), 404)
        except ValueError as e:
            return self._err(frame, str(e), 409)
        return {
            "type": "inbox.mailboxes.delete.result",
            "ref": frame.get("id"),
            "status": "ok",
        }

    async def _ws_mailboxes_test(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        mailbox_id = str(frame.get("mailbox_id") or "")
        try:
            result = await self.test_mailbox_connection(mailbox_id, conn.user_ctx)
        except InboxPermissionError as e:
            return self._err(frame, str(e), 403)
        except MailboxNotFoundError as e:
            return self._err(frame, str(e), 404)
        return {
            "type": "inbox.mailboxes.test_connection.result",
            "ref": frame.get("id"),
            **result,
        }

    async def _ws_mailboxes_share_user(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        try:
            mailbox = await self.share_user(
                mailbox_id=str(frame.get("mailbox_id") or ""),
                user_id=str(frame.get("user_id") or ""),
                user_ctx=conn.user_ctx,
            )
        except InboxPermissionError as e:
            return self._err(frame, str(e), 403)
        except MailboxNotFoundError as e:
            return self._err(frame, str(e), 404)
        is_admin = self._is_admin(conn.user_ctx)
        return {
            "type": "inbox.mailboxes.share_user.result",
            "ref": frame.get("id"),
            "mailbox": self._mailbox_payload(mailbox, conn.user_ctx, is_admin),
        }

    async def _ws_mailboxes_unshare_user(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        try:
            mailbox = await self.unshare_user(
                mailbox_id=str(frame.get("mailbox_id") or ""),
                user_id=str(frame.get("user_id") or ""),
                user_ctx=conn.user_ctx,
            )
        except InboxPermissionError as e:
            return self._err(frame, str(e), 403)
        except MailboxNotFoundError as e:
            return self._err(frame, str(e), 404)
        is_admin = self._is_admin(conn.user_ctx)
        return {
            "type": "inbox.mailboxes.unshare_user.result",
            "ref": frame.get("id"),
            "mailbox": self._mailbox_payload(mailbox, conn.user_ctx, is_admin),
        }

    async def _ws_mailboxes_share_role(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        try:
            mailbox = await self.share_role(
                mailbox_id=str(frame.get("mailbox_id") or ""),
                role=str(frame.get("role") or ""),
                user_ctx=conn.user_ctx,
            )
        except InboxPermissionError as e:
            return self._err(frame, str(e), 403)
        except MailboxNotFoundError as e:
            return self._err(frame, str(e), 404)
        is_admin = self._is_admin(conn.user_ctx)
        return {
            "type": "inbox.mailboxes.share_role.result",
            "ref": frame.get("id"),
            "mailbox": self._mailbox_payload(mailbox, conn.user_ctx, is_admin),
        }

    async def _ws_backends_list(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        """Return available email backends and their config_params.

        The mailbox create/edit UI consumes this to render backend
        selection and the backend-specific credential fields.
        """
        backends = []
        for name, cls in EmailBackend.registered_backends().items():
            params = [
                {
                    "key": p.key,
                    "type": p.type.value if hasattr(p.type, "value") else str(p.type),
                    "description": p.description,
                    "default": p.default,
                    "restart_required": p.restart_required,
                    "sensitive": p.sensitive,
                    "choices": list(p.choices) if p.choices else None,
                    "multiline": p.multiline,
                    "backend_param": True,
                }
                for p in cls.backend_config_params()
            ]
            backends.append({"name": name, "config_params": params})
        return {
            "type": "inbox.backends.list.result",
            "ref": frame.get("id"),
            "backends": backends,
        }

    async def _ws_mailboxes_unshare_role(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        try:
            mailbox = await self.unshare_role(
                mailbox_id=str(frame.get("mailbox_id") or ""),
                role=str(frame.get("role") or ""),
                user_ctx=conn.user_ctx,
            )
        except InboxPermissionError as e:
            return self._err(frame, str(e), 403)
        except MailboxNotFoundError as e:
            return self._err(frame, str(e), 404)
        is_admin = self._is_admin(conn.user_ctx)
        return {
            "type": "inbox.mailboxes.unshare_role.result",
            "ref": frame.get("id"),
            "mailbox": self._mailbox_payload(mailbox, conn.user_ctx, is_admin),
        }


# InboxService is verified to satisfy InboxProvider at runtime via
# isinstance() checks in consuming plugins. The protocol is imported so
# its symbol is re-exported through this module for convenience.
_ = InboxProvider
