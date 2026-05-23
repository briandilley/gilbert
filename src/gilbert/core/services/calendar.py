"""Calendar service — multi-account calendar polling, events, free/busy, and AI tools.

Each account is owned by a user and can be shared with individuals
and/or roles. The service runs one ``CalendarBackend`` instance + one
scheduler poll job per ``poll_enabled`` account. Events are persisted
in ``calendar_events`` (tagged with ``account_id``) and
``calendar_event_announcements`` tracks dedup of
``calendar.event.upcoming`` notifications.

Authorization is centralized in ``interfaces/calendar.py`` —
``can_access_account`` gates reads/writes/free-busy/create_event, and
``can_admin_account`` gates settings and share edits. ``is_admin`` is
derived from the ``UserContext`` inside the helpers; callers never
pass an ad-hoc bool.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gilbert.core.services._backend_actions import (
    all_backend_actions,
    invoke_backend_action_from_payload,
)
from gilbert.core.services._ui_blocks import build_preview_output, confirm_or_execute
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.calendar import (
    AggregatedEvents,
    CalendarAccount,
    CalendarBackend,
    CalendarBackendAuthError,
    CalendarBackendConflictError,
    CalendarBackendError,
    CalendarBackendNotFoundError,
    CalendarEvent,
    CalendarProvider,
    EventCreateRequest,
    EventStatus,
    EventVisibility,
    FreeBusyBlock,
    FreeSlot,
    FreeTimeResult,
    can_access_account,
    can_admin_account,
    determine_access,
)
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.events import EventBusProvider
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import (
    Filter,
    FilterOp,
    IndexDefinition,
    Query,
    SortField,
    StorageProvider,
)
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)
from gilbert.interfaces.ui import ToolOutput

logger = logging.getLogger(__name__)

_ACCOUNTS_COLLECTION = "calendar_accounts"
_EVENTS_COLLECTION = "calendar_events"
_ANNOUNCEMENTS_COLLECTION = "calendar_event_announcements"

_ANNOUNCEMENT_SWEEP_INTERVAL_SEC = 30 * 60  # 30 minutes


class CalendarPermissionError(PermissionError):
    """Raised when a caller lacks access to a calendar account."""


class CalendarAccountNotFoundError(LookupError):
    """Raised when an account id does not resolve."""


@dataclass
class _AccountRuntime:
    """In-memory per-account state: current config + live backend + diff state."""

    account: CalendarAccount
    backend: CalendarBackend
    poll_job_name: str = ""
    last_seen_event_ids: set[str] = field(default_factory=set)
    last_seen_event_snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    recent_mutate_publishes: dict[str, float] = field(default_factory=dict)
    consecutive_failures: int = 0
    seeded_from_cache: bool = False
    #: ``time.monotonic()`` value before which the poll loop must
    #: skip its API call. Set by ``_on_poll_failure`` to honour
    #: ``Retry-After`` on rate-limit responses and to apply
    #: exponential backoff on transient failures.
    next_poll_allowed_at: float = 0.0


class CalendarService(Service):
    """Multi-account calendar service with polling, sharing, and AI tools.

    Capabilities: calendar, ai_tools, ws_handlers
    """

    # Class-level so tests can monkeypatch a deterministic value.
    _process_start_monotonic: float = time.monotonic()

    def __init__(self) -> None:
        self._storage: Any = None
        self._event_bus: Any = None
        self._scheduler: Any = None

        self._runtimes: dict[str, _AccountRuntime] = {}
        self._cached_accounts: list[CalendarAccount] = []

        # Service-level config (defaults match config_params).
        self._enabled: bool = False
        self._default_lookahead_days: int = 14
        self._cache_back_hours: int = 2
        self._upcoming_announce_minutes: int = 15
        self._aggregation_timeout_sec: int = 10
        self._mutate_publish_dedup_sec: int = 60
        self._unhealthy_failure_threshold: int = 3
        self._show_dashboard_card: bool = True

    # ── ToolProvider / capability accessors ──────────────────────────

    @property
    def tool_provider_name(self) -> str:
        return "calendar"

    @property
    def cached_accounts(self) -> list[CalendarAccount]:
        """Atomic snapshot — replaced (never mutated) on each CRUD."""
        return list(self._cached_accounts)

    # ── Service metadata ─────────────────────────────────────────────

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="calendar",
            capabilities=frozenset({"calendar", "ai_tools", "ws_handlers"}),
            requires=frozenset({"entity_storage", "scheduler"}),
            optional=frozenset({"event_bus", "configuration"}),
            events=frozenset(
                {
                    "calendar.event.upcoming",
                    "calendar.account.created",
                    "calendar.account.updated",
                    "calendar.account.deleted",
                    "calendar.account.shares.changed",
                    "calendar.account.health_changed",
                    "calendar.event.created",
                    "calendar.event.updated",
                    "calendar.event.deleted",
                }
            ),
            ai_calls=frozenset(),
            toggleable=True,
            toggle_description="Calendar polling and event tools",
        )

    # ── Lifecycle ────────────────────────────────────────────────────

    async def start(self, resolver: ServiceResolver) -> None:
        storage_svc = resolver.require_capability("entity_storage")
        if not isinstance(storage_svc, StorageProvider):
            raise TypeError("entity_storage capability does not provide StorageProvider")
        self._storage = storage_svc.backend

        await self._storage.ensure_index(
            IndexDefinition(
                collection=_ACCOUNTS_COLLECTION,
                fields=["owner_user_id"],
            )
        )
        await self._storage.ensure_index(
            IndexDefinition(
                collection=_EVENTS_COLLECTION,
                fields=["account_id", "start_utc_iso"],
            )
        )
        await self._storage.ensure_index(
            IndexDefinition(
                collection=_EVENTS_COLLECTION,
                fields=["start_utc_iso"],
            )
        )
        await self._storage.ensure_index(
            IndexDefinition(
                collection=_EVENTS_COLLECTION,
                fields=["end_utc_iso"],
            )
        )
        await self._storage.ensure_index(
            IndexDefinition(
                collection=_ANNOUNCEMENTS_COLLECTION,
                fields=["account_id", "start_iso"],
            )
        )

        event_bus_svc = resolver.get_capability("event_bus")
        if isinstance(event_bus_svc, EventBusProvider):
            self._event_bus = event_bus_svc.bus

        config_svc = resolver.get_capability("configuration")
        section: dict[str, Any] = {}
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                section = config_svc.get_section(self.config_namespace)

        self._apply_config(section)
        if not self._enabled:
            logger.info("Calendar service disabled via configuration")
            return

        from gilbert.interfaces.scheduler import Schedule, SchedulerProvider

        scheduler_svc = resolver.require_capability("scheduler")
        if not isinstance(scheduler_svc, SchedulerProvider):
            raise TypeError("scheduler capability does not provide SchedulerProvider")
        self._scheduler = scheduler_svc

        self._scheduler.add_job(
            name="calendar-boot",
            schedule=Schedule.once_after(0),
            callback=self._boot_runtimes,
            system=True,
        )

        self._scheduler.add_job(
            name="calendar-announcement-sweep",
            schedule=Schedule.every(_ANNOUNCEMENT_SWEEP_INTERVAL_SEC),
            callback=self._sweep_old_records,
            system=True,
        )

        logger.info(
            "Calendar service started (boot deferred, sweep every %ds)",
            _ANNOUNCEMENT_SWEEP_INTERVAL_SEC,
        )

    async def stop(self) -> None:
        if self._scheduler is not None:
            for runtime in list(self._runtimes.values()):
                if runtime.poll_job_name:
                    with contextlib.suppress(Exception):
                        self._scheduler.remove_job(runtime.poll_job_name, force=True)
            with contextlib.suppress(Exception):
                self._scheduler.remove_job("calendar-boot", force=True)
            with contextlib.suppress(Exception):
                self._scheduler.remove_job("calendar-announcement-sweep", force=True)

        if self._runtimes:
            await asyncio.gather(
                *(self._close_runtime_backend(r) for r in self._runtimes.values()),
                return_exceptions=True,
            )
        self._runtimes.clear()
        logger.info("Calendar service stopped")

    async def _close_runtime_backend(self, runtime: _AccountRuntime) -> None:
        try:
            await runtime.backend.close()
        except Exception:
            logger.exception("Error closing backend for account %s", runtime.account.id)

    async def _boot_runtimes(self) -> None:
        try:
            accounts = await self._load_accounts()
        except Exception:
            logger.exception("Calendar boot: failed to load accounts")
            return
        self._cached_accounts = list(accounts)
        for account in accounts:
            if account.poll_enabled:
                try:
                    await self._start_runtime(account)
                except Exception:
                    logger.exception("Calendar boot: failed to start runtime for %s", account.id)
        logger.info("Calendar boot: %d runtime(s) started", len(self._runtimes))

    async def _refresh_cache(self) -> None:
        try:
            self._cached_accounts = await self._load_accounts()
        except Exception:
            logger.exception("Calendar: failed to refresh account cache")

    async def _start_runtime(self, account: CalendarAccount) -> None:
        assert self._scheduler is not None
        backends = CalendarBackend.registered_backends()
        backend_cls = backends.get(account.backend_name)
        if backend_cls is None:
            raise ValueError(f"Unknown calendar backend: {account.backend_name}")
        backend = backend_cls()
        settings = dict(account.backend_config)
        if account.email_address and "email_address" not in settings:
            settings["email_address"] = account.email_address
        await backend.initialize(settings)

        from gilbert.interfaces.scheduler import Schedule

        # Cold-start jitter — prevents N simultaneous backend requests
        # on Gilbert restart from tripping per-user quotas. The
        # scheduler compares ``start_at`` against a naive-local
        # ``datetime.now()``, so we must hand it a naive-local time —
        # tz-aware would TypeError on comparison.
        jitter = random.uniform(0, min(account.poll_interval_sec, 120))
        first_fire = datetime.now() + timedelta(seconds=jitter)
        poll_job_name = f"calendar-poll-{account.id}"
        callback = self._make_poll_callback(account.id)
        self._scheduler.add_job(
            name=poll_job_name,
            schedule=Schedule.every(
                account.poll_interval_sec,
                start_at=first_fire,
            ),
            callback=callback,
            system=True,
        )
        self._runtimes[account.id] = _AccountRuntime(
            account=account,
            backend=backend,
            poll_job_name=poll_job_name,
        )
        logger.info(
            "Calendar runtime started: id=%s backend=%s poll=%ds jitter=%.1fs",
            account.id,
            account.backend_name,
            account.poll_interval_sec,
            jitter,
        )

    async def _stop_runtime(self, account_id: str) -> None:
        runtime = self._runtimes.pop(account_id, None)
        if runtime is None:
            return
        if self._scheduler is not None and runtime.poll_job_name:
            with contextlib.suppress(Exception):
                self._scheduler.remove_job(runtime.poll_job_name, force=True)
        await self._close_runtime_backend(runtime)
        logger.info("Calendar runtime stopped: id=%s", account_id)

    async def _restart_runtime(self, account: CalendarAccount) -> None:
        await self._stop_runtime(account.id)
        if account.poll_enabled:
            await self._start_runtime(account)

    # ── Configurable protocol ────────────────────────────────────────

    @property
    def config_namespace(self) -> str:
        return "calendar"

    @property
    def config_category(self) -> str:
        return "Communication"

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="enabled",
                type=ToolParameterType.BOOLEAN,
                description="Whether calendar polling and event tools are enabled.",
                default=True,
            ),
            ConfigParam(
                key="default_event_lookahead_days",
                type=ToolParameterType.INTEGER,
                description=(
                    "How many days into the future the per-account poll caches. "
                    "Larger values mean more memory + storage, but more "
                    "responsive find_free_time queries that span weeks."
                ),
                default=14,
            ),
            ConfigParam(
                key="cache_back_hours",
                type=ToolParameterType.INTEGER,
                description=(
                    "How many hours into the past the cache retains events. "
                    "Wide enough to answer 'what was my last meeting' for a "
                    "few hours after it ends; narrow enough to keep cache "
                    "size bounded."
                ),
                default=2,
            ),
            ConfigParam(
                key="upcoming_announce_minutes",
                type=ToolParameterType.INTEGER,
                description=(
                    "Default lead time (in minutes) for "
                    "calendar.event.upcoming events. Per-account override "
                    "available on each account row. This is the imminent-"
                    "event notification window — for the morning-brief "
                    "greeting use case (hours into the future), the "
                    "greeting service computes its own lookahead via "
                    "CalendarProvider.list_events; do not conflate the two."
                ),
                default=15,
            ),
            ConfigParam(
                key="aggregation_timeout_sec",
                type=ToolParameterType.INTEGER,
                description=(
                    "Per-runtime timeout (seconds) when fanning out "
                    "aggregate reads (account_id=None) across multiple "
                    "accounts. A slow or hung backend never blocks the "
                    "aggregate result; its failure is surfaced as a warning."
                ),
                default=10,
            ),
            ConfigParam(
                key="mutate_publish_dedup_sec",
                type=ToolParameterType.INTEGER,
                description=(
                    "Window (seconds) during which the poll-loop diff "
                    "suppresses republication of calendar.event.* events "
                    "for ids the mutate path already announced. Prevents "
                    "the same logical mutation firing twice."
                ),
                default=60,
            ),
            ConfigParam(
                key="unhealthy_failure_threshold",
                type=ToolParameterType.INTEGER,
                description=(
                    "Number of consecutive poll failures before the "
                    "account flips to health=unhealthy and "
                    "calendar.account.health_changed fires."
                ),
                default=3,
            ),
            ConfigParam(
                key="show_dashboard_card",
                type=ToolParameterType.BOOLEAN,
                description=(
                    "Whether to render the 'Up next' card on the "
                    "dashboard, showing the user's next event across "
                    "accessible accounts."
                ),
                default=True,
            ),
        ]

    def _apply_config(self, section: dict[str, Any]) -> None:
        self._enabled = bool(section.get("enabled", False))
        self._default_lookahead_days = int(section.get("default_event_lookahead_days", 14) or 14)
        self._cache_back_hours = int(section.get("cache_back_hours", 2) or 2)
        self._upcoming_announce_minutes = int(section.get("upcoming_announce_minutes", 15) or 15)
        self._aggregation_timeout_sec = int(section.get("aggregation_timeout_sec", 10) or 10)
        self._mutate_publish_dedup_sec = int(section.get("mutate_publish_dedup_sec", 60) or 60)
        self._unhealthy_failure_threshold = int(section.get("unhealthy_failure_threshold", 3) or 3)
        self._show_dashboard_card = bool(section.get("show_dashboard_card", True))

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._apply_config(config)

    # ── ConfigAction provider ────────────────────────────────────────

    def config_actions(self) -> list[ConfigAction]:
        return all_backend_actions(
            registry=CalendarBackend.registered_backends(),
            current_backend=None,
        )

    async def invoke_config_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        return await invoke_backend_action_from_payload(
            registry=CalendarBackend.registered_backends(),
            current_backend=None,
            key=key,
            payload=payload,
        )

    # ── Authorization helpers ────────────────────────────────────────

    def _require_access(
        self,
        account: CalendarAccount,
        user_ctx: UserContext,
    ) -> None:
        if not can_access_account(user_ctx, account):
            raise CalendarPermissionError(
                f"User {user_ctx.user_id!r} cannot access calendar account {account.id!r}",
            )

    def _require_admin(
        self,
        account: CalendarAccount,
        user_ctx: UserContext,
    ) -> None:
        if not can_admin_account(user_ctx, account):
            raise CalendarPermissionError(
                f"User {user_ctx.user_id!r} cannot administer calendar account {account.id!r}",
            )

    # ── Account CRUD ─────────────────────────────────────────────────

    async def _load_accounts(self) -> list[CalendarAccount]:
        rows = await self._storage.query(Query(collection=_ACCOUNTS_COLLECTION))
        return [CalendarAccount.from_dict(row) for row in rows]

    async def list_accounts(self) -> list[CalendarAccount]:
        """List every account regardless of access. Callers enforce auth."""
        return await self._load_accounts()

    async def list_accessible_accounts(
        self,
        user_ctx: UserContext,
    ) -> list[CalendarAccount]:
        """Return every account ``user_ctx`` can access via storage truth."""
        accounts = await self._load_accounts()
        return [a for a in accounts if can_access_account(user_ctx, a)]

    async def get_account(
        self,
        account_id: str,
        user_ctx: UserContext,
    ) -> CalendarAccount | None:
        row = await self._storage.get(_ACCOUNTS_COLLECTION, account_id)
        if row is None:
            return None
        account = CalendarAccount.from_dict(row)
        if not can_access_account(user_ctx, account):
            return None
        return account

    async def _require_account(self, account_id: str) -> CalendarAccount:
        row = await self._storage.get(_ACCOUNTS_COLLECTION, account_id)
        if row is None:
            raise CalendarAccountNotFoundError(
                f"Calendar account not found: {account_id}",
            )
        return CalendarAccount.from_dict(row)

    @staticmethod
    def _validate_timezone(tz: str) -> None:
        try:
            ZoneInfo(tz)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Invalid IANA timezone: {tz!r}") from exc

    @staticmethod
    def _validate_working_hours(start_h: int, end_h: int) -> None:
        if not (0 <= start_h <= 23):
            raise ValueError(
                f"working_hours_start_hour must be 0–23 (got {start_h})",
            )
        if not (1 <= end_h <= 24):
            raise ValueError(
                f"working_hours_end_hour must be 1–24 (got {end_h})",
            )
        if start_h >= end_h:
            raise ValueError(
                "working_hours_start_hour must be strictly less than "
                "working_hours_end_hour (cross-midnight ranges are not "
                "supported in v1)"
            )

    async def create_account(
        self,
        account: CalendarAccount,
        user_ctx: UserContext,
    ) -> CalendarAccount:
        """Create a new calendar account. Creator becomes owner."""
        self._validate_timezone(account.timezone)
        self._validate_working_hours(
            account.working_hours_start_hour,
            account.working_hours_end_hour,
        )
        if not account.id:
            account.id = f"cal_{uuid.uuid4().hex[:12]}"
        account.owner_user_id = user_ctx.user_id
        account.created_at = datetime.now(UTC).isoformat()

        existing = await self._storage.get(_ACCOUNTS_COLLECTION, account.id)
        if existing is not None:
            raise ValueError(f"Calendar account id already exists: {account.id}")

        await self._storage.put(
            _ACCOUNTS_COLLECTION,
            account.id,
            account.to_dict(),
        )
        await self._refresh_cache()

        if self._enabled and account.poll_enabled:
            try:
                await self._start_runtime(account)
            except Exception:
                logger.exception(
                    "Failed to start runtime for newly created account %s",
                    account.id,
                )

        await self._publish_account_event("calendar.account.created", account)
        return account

    async def update_account(
        self,
        account_id: str,
        updates: dict[str, Any],
        user_ctx: UserContext,
    ) -> CalendarAccount:
        account = await self._require_account(account_id)
        self._require_admin(account, user_ctx)

        immutable = {"id", "owner_user_id", "created_at"}
        share_fields = {"shared_with_users", "shared_with_roles"}
        runtime_affecting = {
            "backend_name",
            "backend_config",
            "poll_enabled",
            "poll_interval_sec",
            "email_address",
            "calendar_id",
            "timezone",
        }
        needs_restart = False

        new_tz: str | None = None
        new_wh_start: int | None = None
        new_wh_end: int | None = None
        for key, value in updates.items():
            if key in immutable:
                continue
            if key in share_fields:
                continue
            if not hasattr(account, key):
                continue
            if key == "timezone":
                new_tz = str(value)
            if key == "working_hours_start_hour":
                new_wh_start = int(value)
            if key == "working_hours_end_hour":
                new_wh_end = int(value)
            if key in runtime_affecting:
                needs_restart = True
            setattr(account, key, value)

        if new_tz is not None:
            self._validate_timezone(new_tz)
        self._validate_working_hours(
            account.working_hours_start_hour if new_wh_start is None else new_wh_start,
            account.working_hours_end_hour if new_wh_end is None else new_wh_end,
        )

        await self._storage.put(
            _ACCOUNTS_COLLECTION,
            account.id,
            account.to_dict(),
        )
        await self._refresh_cache()

        if self._enabled and needs_restart:
            try:
                await self._restart_runtime(account)
            except Exception:
                logger.exception(
                    "Failed to restart runtime after update for account %s",
                    account.id,
                )

        await self._publish_account_event("calendar.account.updated", account)
        return account

    async def delete_account(
        self,
        account_id: str,
        user_ctx: UserContext,
    ) -> None:
        """Delete an account.

        Cascades: deletes every ``calendar_events`` and
        ``calendar_event_announcements`` row tagged with ``account_id``
        before the account row itself, so a crash mid-delete leaves the
        runtime live (and the next boot drains it cleanly) rather than
        leaving orphan rows.
        """
        account = await self._require_account(account_id)
        self._require_admin(account, user_ctx)

        await self._stop_runtime(account_id)

        events = await self._storage.query(
            Query(
                collection=_EVENTS_COLLECTION,
                filters=[Filter(field="account_id", op=FilterOp.EQ, value=account_id)],
            )
        )
        for row in events:
            await self._storage.delete(_EVENTS_COLLECTION, row["_id"])

        announcements = await self._storage.query(
            Query(
                collection=_ANNOUNCEMENTS_COLLECTION,
                filters=[Filter(field="account_id", op=FilterOp.EQ, value=account_id)],
            )
        )
        for row in announcements:
            await self._storage.delete(_ANNOUNCEMENTS_COLLECTION, row["_id"])

        await self._storage.delete(_ACCOUNTS_COLLECTION, account_id)
        await self._refresh_cache()
        await self._publish_account_event("calendar.account.deleted", account)

    async def share_user(
        self,
        account_id: str,
        user_id: str,
        user_ctx: UserContext,
    ) -> CalendarAccount:
        account = await self._require_account(account_id)
        self._require_admin(account, user_ctx)
        if user_id not in account.shared_with_users:
            account.shared_with_users.append(user_id)
            await self._storage.put(
                _ACCOUNTS_COLLECTION,
                account.id,
                account.to_dict(),
            )
            await self._refresh_cache()
            await self._publish_shares_changed(account)
        return account

    async def unshare_user(
        self,
        account_id: str,
        user_id: str,
        user_ctx: UserContext,
    ) -> CalendarAccount:
        account = await self._require_account(account_id)
        self._require_admin(account, user_ctx)
        if user_id in account.shared_with_users:
            account.shared_with_users.remove(user_id)
            await self._storage.put(
                _ACCOUNTS_COLLECTION,
                account.id,
                account.to_dict(),
            )
            await self._refresh_cache()
            await self._publish_shares_changed(account)
        return account

    async def share_role(
        self,
        account_id: str,
        role: str,
        user_ctx: UserContext,
    ) -> CalendarAccount:
        account = await self._require_account(account_id)
        self._require_admin(account, user_ctx)
        if role not in account.shared_with_roles:
            account.shared_with_roles.append(role)
            await self._storage.put(
                _ACCOUNTS_COLLECTION,
                account.id,
                account.to_dict(),
            )
            await self._refresh_cache()
            await self._publish_shares_changed(account)
        return account

    async def unshare_role(
        self,
        account_id: str,
        role: str,
        user_ctx: UserContext,
    ) -> CalendarAccount:
        account = await self._require_account(account_id)
        self._require_admin(account, user_ctx)
        if role in account.shared_with_roles:
            account.shared_with_roles.remove(role)
            await self._storage.put(
                _ACCOUNTS_COLLECTION,
                account.id,
                account.to_dict(),
            )
            await self._refresh_cache()
            await self._publish_shares_changed(account)
        return account

    async def test_account_connection(
        self,
        account_id: str,
        user_ctx: UserContext,
    ) -> dict[str, Any]:
        """Probe an account's backend with the persisted config.

        Returns ``{ok: bool, error?: str, calendars?: list}``.
        """
        account = await self._require_account(account_id)
        self._require_admin(account, user_ctx)
        try:
            backends = CalendarBackend.registered_backends()
            backend_cls = backends.get(account.backend_name)
            if backend_cls is None:
                return {
                    "ok": False,
                    "error": f"Unknown backend: {account.backend_name}",
                }
            probe = backend_cls()
            settings = dict(account.backend_config)
            if account.email_address and "email_address" not in settings:
                settings["email_address"] = account.email_address
            if account.calendar_id and "calendar_id" not in settings:
                settings["calendar_id"] = account.calendar_id
            await probe.initialize(settings)
            try:
                calendars = await probe.list_calendars()
            finally:
                await probe.close()
            return {"ok": True, "calendars": calendars}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def probe_calendars(
        self,
        account_id: str,
        user_ctx: UserContext,
    ) -> list[dict[str, Any]]:
        """List calendars on the configured account.

        Used by the SPA's account-edit drawer to populate the
        calendar_id dropdown after credentials are saved (with
        ``poll_enabled=False``). Owns the backend lifecycle in a
        ``try/finally`` so a probe failure can't leak the connection.
        """
        account = await self._require_account(account_id)
        self._require_admin(account, user_ctx)
        backends = CalendarBackend.registered_backends()
        backend_cls = backends.get(account.backend_name)
        if backend_cls is None:
            raise ValueError(f"Unknown backend: {account.backend_name}")
        probe = backend_cls()
        settings = dict(account.backend_config)
        if account.email_address and "email_address" not in settings:
            settings["email_address"] = account.email_address
        if account.calendar_id and "calendar_id" not in settings:
            settings["calendar_id"] = account.calendar_id
        await probe.initialize(settings)
        try:
            return await probe.list_calendars()
        finally:
            with contextlib.suppress(Exception):
                await probe.close()

    # ── Event publication helpers ────────────────────────────────────

    async def _publish_event(self, event_type: str, data: dict[str, Any]) -> None:
        if self._event_bus is None:
            return
        from gilbert.interfaces.events import Event

        await self._event_bus.publish(
            Event(event_type=event_type, data=data, source="calendar"),
        )

    async def _publish_account_event(
        self,
        event_type: str,
        account: CalendarAccount,
    ) -> None:
        await self._publish_event(
            event_type,
            {
                "account_id": account.id,
                "name": account.name,
                "owner_user_id": account.owner_user_id,
            },
        )

    async def _publish_shares_changed(self, account: CalendarAccount) -> None:
        await self._publish_event(
            "calendar.account.shares.changed",
            {
                "account_id": account.id,
                "owner_user_id": account.owner_user_id,
                "shared_with_users": list(account.shared_with_users),
                "shared_with_roles": list(account.shared_with_roles),
            },
        )

    async def _publish_health_changed(self, account: CalendarAccount) -> None:
        await self._publish_event(
            "calendar.account.health_changed",
            {
                "account_id": account.id,
                "health": account.health,
                "last_error": account.last_error,
            },
        )

    # ── Polling ──────────────────────────────────────────────────────

    def _make_poll_callback(
        self,
        account_id: str,
    ) -> Callable[[], Awaitable[None]]:
        async def _run() -> None:
            runtime = self._runtimes.get(account_id)
            if runtime is None:
                return
            await self._poll_runtime(runtime)

        return _run

    async def _poll_runtime(self, runtime: _AccountRuntime) -> None:
        account = runtime.account
        backend = runtime.backend
        # Honor a deferred next-poll deadline set by a prior rate-limit
        # or transient failure — skip without touching the backend so we
        # don't hammer the provider during its cooldown window.
        if time.monotonic() < runtime.next_poll_allowed_at:
            logger.debug(
                "Calendar poll (%s): deferred (cooldown active)",
                account.id,
            )
            return
        now = datetime.now(UTC)
        time_min = now - timedelta(hours=self._cache_back_hours)
        time_max = now + timedelta(days=self._default_lookahead_days)

        # Lazy seed last_seen_event_ids from persisted cache so a
        # process restart does not republish every event as created.
        if not runtime.seeded_from_cache:
            try:
                cached = await self._storage.query(
                    Query(
                        collection=_EVENTS_COLLECTION,
                        filters=[
                            Filter(
                                field="account_id",
                                op=FilterOp.EQ,
                                value=account.id,
                            ),
                        ],
                    )
                )
                runtime.last_seen_event_ids = {
                    str(row.get("event_id", "")) for row in cached if row.get("event_id")
                }
                runtime.last_seen_event_snapshots = {
                    str(row.get("event_id", "")): row for row in cached if row.get("event_id")
                }
            except Exception:
                logger.exception(
                    "Calendar poll (%s): failed to seed cache",
                    account.id,
                )
            runtime.seeded_from_cache = True

        try:
            events = await asyncio.wait_for(
                backend.list_events(
                    account.calendar_id,
                    time_min,
                    time_max,
                    single_events=True,
                ),
                timeout=float(self._aggregation_timeout_sec),
            )
        except CalendarBackendAuthError as exc:
            await self._on_poll_failure(runtime, exc, fatal=True)
            return
        except CalendarBackendNotFoundError as exc:
            await self._on_poll_failure(runtime, exc, fatal=True)
            return
        except (TimeoutError, CalendarBackendError) as exc:
            await self._on_poll_failure(runtime, exc, fatal=False)
            return
        except Exception as exc:
            # An unexpected exception class slipped through the typed
            # taxonomy — record it, increment the counter, but don't
            # crash the poll callback (the scheduler would log it as a
            # job failure but we want to keep the runtime alive and
            # surface ``unhealthy`` after the configured threshold).
            logger.exception(
                "Calendar poll (%s): unexpected exception class %s",
                runtime.account.id,
                type(exc).__name__,
            )
            await self._on_poll_failure(runtime, exc, fatal=False)
            return

        # Filter cancelled out of fresh BEFORE the diff, so the diff
        # publishes calendar.event.deleted exactly once for cancellations.
        fresh = [e for e in events if e.status != EventStatus.CANCELLED]
        fresh_ids = {e.event_id for e in fresh}

        # Diff against last_seen, suppressing recently-mutated ids.
        now_mono = time.monotonic()
        # Prune stale dedup entries.
        runtime.recent_mutate_publishes = {
            eid: ts
            for eid, ts in runtime.recent_mutate_publishes.items()
            if (now_mono - ts) < self._mutate_publish_dedup_sec
        }

        previous_snapshots = runtime.last_seen_event_snapshots
        new_ids = fresh_ids - runtime.last_seen_event_ids
        missing_ids = runtime.last_seen_event_ids - fresh_ids

        new_snapshots: dict[str, dict[str, Any]] = {}

        for evt in fresh:
            snapshot = self._event_summary_for_diff(evt)
            new_snapshots[evt.event_id] = snapshot
            # Only write to storage when the diff-relevant fields actually
            # changed. Skipping unchanged rows turns a 100-event / 5-min
            # poll from a 100-write-per-cycle storm into a no-op cycle on
            # quiet calendars.
            previous = previous_snapshots.get(evt.event_id)
            if previous == snapshot:
                continue
            row_id = self._event_row_id(account.id, evt.event_id)
            await self._storage.put(
                _EVENTS_COLLECTION,
                row_id,
                self._event_row_payload(account, evt),
            )

        # Delete missing rows.
        for missing in missing_ids:
            row_id = self._event_row_id(account.id, missing)
            with contextlib.suppress(Exception):
                await self._storage.delete(_EVENTS_COLLECTION, row_id)

        # Publish diffs.
        for evt in fresh:
            eid = evt.event_id
            if eid in runtime.recent_mutate_publishes:
                continue
            if eid in new_ids:
                await self._publish_event_change("calendar.event.created", account, evt)
            else:
                old = previous_snapshots.get(eid)
                cur = new_snapshots[eid]
                if old is not None and old != cur:
                    await self._publish_event_change("calendar.event.updated", account, evt)

        for missing in missing_ids:
            if missing in runtime.recent_mutate_publishes:
                continue
            await self._publish_event(
                "calendar.event.deleted",
                {"account_id": account.id, "event_id": missing},
            )

        runtime.last_seen_event_ids = fresh_ids
        runtime.last_seen_event_snapshots = new_snapshots
        runtime.consecutive_failures = 0
        runtime.next_poll_allowed_at = 0.0

        # Health recovery — flip back to ok if it was unhealthy.
        if account.health != "ok":
            account.health = "ok"
            account.last_error = ""
            account.last_error_at = ""
            await self._storage.put(
                _ACCOUNTS_COLLECTION,
                account.id,
                account.to_dict(),
            )
            await self._refresh_cache()
            await self._publish_health_changed(account)

        # Upcoming announcements.
        await self._emit_upcoming_for_account(account, fresh)

    async def _on_poll_failure(
        self,
        runtime: _AccountRuntime,
        exc: BaseException,
        *,
        fatal: bool,
    ) -> None:
        account = runtime.account
        runtime.consecutive_failures += 1
        logger.warning(
            "Calendar poll (%s) failed: %s (consecutive=%d)",
            account.id,
            exc,
            runtime.consecutive_failures,
        )

        # Defer the next poll attempt to honor backend signals:
        #   - Rate-limit with ``Retry-After`` → sleep at least that long.
        #   - Transient (5xx, timeout) → exponential backoff capped at
        #     the account's poll interval, so a flaky provider can't
        #     keep us hammering at the normal cadence.
        cooldown_sec = 0.0
        from gilbert.interfaces.calendar import (
            CalendarBackendRateLimitError,
            CalendarBackendTransientError,
        )

        if isinstance(exc, CalendarBackendRateLimitError) and exc.retry_after_sec:
            cooldown_sec = float(exc.retry_after_sec)
        elif isinstance(exc, (CalendarBackendTransientError, TimeoutError)):
            # 2, 4, 8, 16, … seconds of backoff capped at the configured
            # poll interval (so we never push the next attempt past the
            # cycle that would have run anyway).
            backoff = 2.0 ** runtime.consecutive_failures
            cooldown_sec = min(backoff, float(account.poll_interval_sec))
        if cooldown_sec > 0.0:
            runtime.next_poll_allowed_at = time.monotonic() + cooldown_sec
            logger.info(
                "Calendar poll (%s): deferring next attempt by %.1fs",
                account.id,
                cooldown_sec,
            )

        if (
            runtime.consecutive_failures >= self._unhealthy_failure_threshold
            and account.health != "unhealthy"
        ):
            account.health = "unhealthy"
            err_msg = str(exc)[:500]
            account.last_error = err_msg
            account.last_error_at = datetime.now(UTC).isoformat()
            try:
                await self._storage.put(
                    _ACCOUNTS_COLLECTION,
                    account.id,
                    account.to_dict(),
                )
                await self._refresh_cache()
            except Exception:
                logger.exception("Failed to persist unhealthy state for %s", account.id)
            await self._publish_health_changed(account)
        # Fatal failures (auth/notfound) flip immediately too — once
        # threshold is reached they're handled above; otherwise leave
        # consecutive_failures bumping until threshold.
        if fatal:
            logger.info("Calendar poll (%s): fatal error class", account.id)

    @staticmethod
    def _event_row_id(account_id: str, event_id: str) -> str:
        return f"{account_id}:{event_id}"

    @staticmethod
    def _event_row_payload(
        account: CalendarAccount,
        evt: CalendarEvent,
    ) -> dict[str, Any]:
        # ``start`` / ``end`` keep the event's original timezone offset so
        # the SPA can render the original wall-clock time. Filter / sort
        # queries always use ``start_utc_iso`` / ``end_utc_iso`` because
        # string-comparing mixed-offset ISO timestamps is not order-
        # preserving (e.g. "2026-05-09T22:00-08:00" sorts before
        # "2026-05-10T05:00+00:00" even though the first instant is
        # later).
        return {
            "account_id": account.id,
            "event_id": evt.event_id,
            "calendar_id": evt.calendar_id,
            "title": evt.title,
            "start": evt.start.isoformat(),
            "end": evt.end.isoformat(),
            "start_utc_iso": evt.start.astimezone(UTC).isoformat(),
            "end_utc_iso": evt.end.astimezone(UTC).isoformat(),
            "all_day": evt.all_day,
            "etag": evt.etag,
            "status": evt.status.value,
            "transparency": evt.transparency,
            "attendees_json": json.dumps(
                [a.to_dict() for a in evt.attendees],
                sort_keys=True,
            ),
            "organizer_email": evt.organizer_email,
            "location": evt.location,
            "description": evt.description,
            "html_link": evt.html_link,
            "recurring_event_id": evt.recurring_event_id,
            "visibility": evt.visibility.value,
        }

    @staticmethod
    def _event_summary_for_diff(evt: CalendarEvent) -> dict[str, Any]:
        """Subset of fields whose change should fire calendar.event.updated.

        Cosmetic fields like etag/html_link are excluded so the diff
        doesn't trigger spurious updates.
        """
        return {
            "title": evt.title,
            "start": evt.start.isoformat(),
            "end": evt.end.isoformat(),
            "location": evt.location,
            "description": evt.description,
            "status": evt.status.value,
            "attendees": [a.to_dict() for a in evt.attendees],
        }

    async def _publish_event_change(
        self,
        event_type: str,
        account: CalendarAccount,
        evt: CalendarEvent,
    ) -> None:
        await self._publish_event(
            event_type,
            {
                "account_id": account.id,
                "event_id": evt.event_id,
                "title": evt.title,
                "start": evt.start.isoformat(),
                "end": evt.end.isoformat(),
            },
        )

    async def _emit_upcoming_for_account(
        self,
        account: CalendarAccount,
        events: list[CalendarEvent],
    ) -> None:
        lookahead = max(1, account.upcoming_event_lookahead_minutes)
        now = datetime.now(UTC)
        cutoff = now + timedelta(minutes=lookahead)
        for evt in events:
            if evt.start < now:
                continue
            if evt.start > cutoff:
                continue
            announcement_id = f"{account.id}:{evt.event_id}"
            existing = await self._storage.get(
                _ANNOUNCEMENTS_COLLECTION,
                announcement_id,
            )
            if existing is not None:
                continue
            await self._storage.put(
                _ANNOUNCEMENTS_COLLECTION,
                announcement_id,
                {
                    "account_id": account.id,
                    "event_id": evt.event_id,
                    "start_iso": evt.start.isoformat(),
                    "announced_at": now.isoformat(),
                },
            )
            await self._publish_event(
                "calendar.event.upcoming",
                {
                    "account_id": account.id,
                    "event_id": evt.event_id,
                    "title": evt.title,
                    "start": evt.start.isoformat(),
                    "location": evt.location,
                    "attendee_emails": [a.email for a in evt.attendees],
                    "organizer_email": evt.organizer_email,
                    "owner_user_id": account.owner_user_id,
                },
            )

    async def _sweep_old_records(self) -> None:
        """Reap rows older than ``cache_back_hours``.

        Entity storage has no TTL primitive so we run an explicit sweep
        every 30 minutes. Anything in ``calendar_event_announcements``
        older than 48h, and anything in ``calendar_events`` older than
        the configured cache window, is deleted.
        """
        try:
            now = datetime.now(UTC)
            announcement_cutoff = (now - timedelta(hours=48)).isoformat()
            event_cutoff = (now - timedelta(hours=max(1, self._cache_back_hours))).isoformat()

            stale_announcements = await self._storage.query(
                Query(
                    collection=_ANNOUNCEMENTS_COLLECTION,
                    filters=[
                        Filter(
                            field="start_iso",
                            op=FilterOp.LT,
                            value=announcement_cutoff,
                        ),
                    ],
                )
            )
            for row in stale_announcements:
                with contextlib.suppress(Exception):
                    await self._storage.delete(_ANNOUNCEMENTS_COLLECTION, row["_id"])

            stale_events = await self._storage.query(
                Query(
                    collection=_EVENTS_COLLECTION,
                    filters=[
                        Filter(field="start_utc_iso", op=FilterOp.LT, value=event_cutoff),
                    ],
                )
            )
            for row in stale_events:
                with contextlib.suppress(Exception):
                    await self._storage.delete(_EVENTS_COLLECTION, row["_id"])
        except Exception:
            logger.exception("Calendar announcement sweep failed")

    # ── Mutation publish dedup helper ────────────────────────────────

    def _record_mutate_publish(self, account_id: str, event_id: str) -> None:
        runtime = self._runtimes.get(account_id)
        if runtime is None:
            return
        runtime.recent_mutate_publishes[event_id] = time.monotonic()

    # ── Reads (CalendarProvider) ─────────────────────────────────────

    @staticmethod
    def _event_row_to_event(row: dict[str, Any]) -> CalendarEvent:
        from gilbert.interfaces.calendar import CalendarAttendee

        attendees: tuple[CalendarAttendee, ...] = ()
        try:
            raw_atts = json.loads(row.get("attendees_json") or "[]")
            if isinstance(raw_atts, list):
                attendees = tuple(CalendarAttendee.from_dict(a) for a in raw_atts)
        except (ValueError, TypeError):
            attendees = ()
        return CalendarEvent(
            event_id=str(row.get("event_id", "")),
            calendar_id=str(row.get("calendar_id", "")),
            account_id=str(row.get("account_id", "")),
            title=str(row.get("title", "")),
            start=datetime.fromisoformat(str(row.get("start") or "1970-01-01T00:00:00+00:00")),
            end=datetime.fromisoformat(str(row.get("end") or "1970-01-01T00:00:00+00:00")),
            etag=str(row.get("etag", "")),
            all_day=bool(row.get("all_day", False)),
            description=str(row.get("description", "")),
            location=str(row.get("location", "")),
            organizer_email=str(row.get("organizer_email", "")),
            attendees=attendees,
            visibility=EventVisibility(str(row.get("visibility", EventVisibility.DEFAULT.value))),
            status=EventStatus(str(row.get("status", EventStatus.CONFIRMED.value))),
            transparency=str(row.get("transparency", "opaque")),
            html_link=str(row.get("html_link", "")),
            recurring_event_id=(
                str(row["recurring_event_id"]) if row.get("recurring_event_id") else None
            ),
        )

    async def _accessible_account_ids(self, user_ctx: UserContext) -> list[str]:
        accs = await self.list_accessible_accounts(user_ctx)
        return [a.id for a in accs]

    async def list_events(
        self,
        account_id: str | None,
        time_min: datetime,
        time_max: datetime,
        user_ctx: UserContext,
        *,
        max_results: int = 250,
    ) -> AggregatedEvents:
        """Aggregate events across one or all accessible accounts.

        Reads from the in-storage cache first; failures are surfaced as
        per-account warnings on the result envelope.
        """
        if time_min.tzinfo is None or time_max.tzinfo is None:
            raise ValueError("time_min and time_max must be timezone-aware")
        if account_id is not None:
            account = await self._require_account(account_id)
            self._require_access(account, user_ctx)
            accounts = [account]
        else:
            accounts = await self.list_accessible_accounts(user_ctx)
        if not accounts:
            return AggregatedEvents(events=[], warnings=[])

        events_acc: list[CalendarEvent] = []
        warnings: list[str] = []
        time_min_iso = time_min.astimezone(UTC).isoformat()
        time_max_iso = time_max.astimezone(UTC).isoformat()

        async def _fetch_for(a: CalendarAccount) -> tuple[str, list[CalendarEvent]]:
            try:
                rows = await self._storage.query(
                    Query(
                        collection=_EVENTS_COLLECTION,
                        filters=[
                            Filter(
                                field="account_id",
                                op=FilterOp.EQ,
                                value=a.id,
                            ),
                            Filter(
                                field="start_utc_iso",
                                op=FilterOp.GTE,
                                value=time_min_iso,
                            ),
                            Filter(
                                field="start_utc_iso",
                                op=FilterOp.LTE,
                                value=time_max_iso,
                            ),
                        ],
                        sort=[SortField(field="start_utc_iso", descending=False)],
                    )
                )
                evs = [self._event_row_to_event(r) for r in rows]
                evs = [e for e in evs if e.status != EventStatus.CANCELLED]
                return a.id, evs
            except Exception as exc:
                logger.exception("Calendar list_events failed for account %s", a.id)
                return a.id, [_AggregateFailure(account_name=a.name, error=str(exc))]  # type: ignore[list-item]

        results = await asyncio.gather(
            *(_fetch_for(a) for a in accounts),
            return_exceptions=False,
        )
        for _aid, evs in results:
            for e in evs:
                if isinstance(e, _AggregateFailure):
                    warnings.append(f"calendar {e.account_name!r} failed: {e.error}")
                else:
                    events_acc.append(e)

        events_acc.sort(key=lambda e: e.start)
        if len(events_acc) > max_results:
            events_acc = events_acc[:max_results]
        return AggregatedEvents(events=events_acc, warnings=warnings)

    async def next_event(
        self,
        account_id: str | None,
        user_ctx: UserContext,
        *,
        after: datetime | None = None,
        within: timedelta | None = None,
    ) -> CalendarEvent | None:
        now = after or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("`after` must be timezone-aware when provided")
        until = now + (within or timedelta(days=30))
        agg = await self.list_events(
            account_id,
            now,
            until,
            user_ctx,
            max_results=50,
        )
        for evt in agg.events:
            if evt.start >= now:
                return evt
        return None

    async def free_busy(
        self,
        account_id: str | None,
        time_min: datetime,
        time_max: datetime,
        user_ctx: UserContext,
    ) -> list[FreeBusyBlock]:
        if time_min.tzinfo is None or time_max.tzinfo is None:
            raise ValueError("time_min and time_max must be timezone-aware")
        if account_id is not None:
            account = await self._require_account(account_id)
            self._require_access(account, user_ctx)
            accounts = [account]
        else:
            accounts = await self.list_accessible_accounts(user_ctx)
        blocks: list[FreeBusyBlock] = []
        for a in accounts:
            runtime = self._runtimes.get(a.id)
            if runtime is None:
                continue
            try:
                got = await asyncio.wait_for(
                    runtime.backend.free_busy([a.calendar_id], time_min, time_max),
                    timeout=float(self._aggregation_timeout_sec),
                )
                blocks.extend(got)
            except Exception:
                logger.exception("Calendar free_busy failed for account %s", a.id)
        return blocks

    async def find_free_time(
        self,
        account_id: str | None,
        time_min: datetime,
        time_max: datetime,
        duration_minutes: int,
        user_ctx: UserContext,
        *,
        respect_working_hours: bool = True,
        max_results: int = 5,
        attendee_emails: list[str] | None = None,
    ) -> FreeTimeResult:
        if not (5 <= duration_minutes <= 480):
            raise ValueError(f"duration_minutes must be between 5 and 480 (got {duration_minutes})")
        if time_min >= time_max:
            raise ValueError("time_min must be strictly before time_max")
        window_minutes = (time_max - time_min).total_seconds() / 60.0
        if duration_minutes > window_minutes:
            raise ValueError("requested duration exceeds search window")
        if time_min.tzinfo is None or time_max.tzinfo is None:
            raise ValueError("time_min and time_max must be timezone-aware")

        warnings: list[str] = []

        if account_id is not None:
            account = await self._require_account(account_id)
            self._require_access(account, user_ctx)
            accounts = [account]
        else:
            accounts = await self.list_accessible_accounts(user_ctx)
        if not accounts:
            return FreeTimeResult(slots=[], warnings=warnings)

        # Working hours intersection (most-restrictive wins).
        wh_start_h = max(a.working_hours_start_hour for a in accounts)
        wh_end_h = min(a.working_hours_end_hour for a in accounts)
        if wh_start_h >= wh_end_h:
            respect_working_hours = False

        # Use the first account's timezone as the canonical clock.
        primary_tz = ZoneInfo(accounts[0].timezone)

        # Collect busy intervals from the cache (status != cancelled,
        # transparency != transparent, tentative counts as busy).
        busy: list[tuple[datetime, datetime]] = []

        async def _busy_for_account(a: CalendarAccount) -> list[tuple[datetime, datetime]]:
            rows = await self._storage.query(
                Query(
                    collection=_EVENTS_COLLECTION,
                    filters=[
                        Filter(field="account_id", op=FilterOp.EQ, value=a.id),
                        Filter(
                            field="start_utc_iso",
                            op=FilterOp.LTE,
                            value=time_max.astimezone(UTC).isoformat(),
                        ),
                        Filter(
                            field="end_utc_iso",
                            op=FilterOp.GTE,
                            value=time_min.astimezone(UTC).isoformat(),
                        ),
                    ],
                    sort=[SortField(field="start_utc_iso", descending=False)],
                )
            )
            local_busy: list[tuple[datetime, datetime]] = []
            for row in rows:
                if row.get("status") == EventStatus.CANCELLED.value:
                    continue
                if row.get("transparency") == "transparent":
                    continue
                try:
                    s = datetime.fromisoformat(str(row["start"]))
                    e = datetime.fromisoformat(str(row["end"]))
                except Exception:
                    continue
                if e <= time_min or s >= time_max:
                    continue
                local_busy.append((max(s, time_min), min(e, time_max)))
            return local_busy

        for a in accounts:
            try:
                busy.extend(await _busy_for_account(a))
            except Exception as exc:
                logger.exception("find_free_time: storage query failed for %s", a.id)
                warnings.append(f"calendar {a.name!r} busy lookup failed: {exc}")

        # Cross-attendee free/busy via backend. We try one account at a
        # time and stop on the first that succeeds (the same email list
        # produces the same answer from any backend with delegation).
        # Per-attempt failures surface as warnings on the result so the
        # AI tool / SPA can tell the user "we couldn't see <foo>'s
        # calendar".
        if attendee_emails:
            attendee_blocks_collected = False
            for a in accounts:
                runtime = self._runtimes.get(a.id)
                if runtime is None:
                    continue
                try:
                    got = await asyncio.wait_for(
                        runtime.backend.free_busy(list(attendee_emails), time_min, time_max),
                        timeout=float(self._aggregation_timeout_sec),
                    )
                    for b in got:
                        busy.append((max(b.start, time_min), min(b.end, time_max)))
                    attendee_blocks_collected = True
                    break  # one account is enough to reach the same emails
                except Exception as exc:
                    logger.exception(
                        "find_free_time cross-attendee free_busy failed for %s",
                        a.id,
                    )
                    warnings.append(
                        f"attendee free/busy probe via {a.name!r} failed: {exc}"
                    )
                    continue
            if not attendee_blocks_collected:
                warnings.append(
                    "could not retrieve free/busy data for one or more attendees — "
                    "their calendars may not be shared with this account"
                )

        # Merge busy intervals.
        busy.sort(key=lambda iv: iv[0])
        merged: list[tuple[datetime, datetime]] = []
        for s, e in busy:
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))

        # Walk free windows.
        slots: list[FreeSlot] = []
        cursor = time_min
        for s, e in merged:
            if cursor < s:
                self._collect_slots(
                    slots,
                    cursor,
                    s,
                    duration_minutes,
                    respect_working_hours,
                    primary_tz,
                    wh_start_h,
                    wh_end_h,
                    max_results,
                )
            cursor = max(cursor, e)
            if len(slots) >= max_results:
                break
        if len(slots) < max_results and cursor < time_max:
            self._collect_slots(
                slots,
                cursor,
                time_max,
                duration_minutes,
                respect_working_hours,
                primary_tz,
                wh_start_h,
                wh_end_h,
                max_results,
            )
        return FreeTimeResult(slots=slots[:max_results], warnings=warnings)

    @staticmethod
    def _collect_slots(
        out: list[FreeSlot],
        start: datetime,
        end: datetime,
        duration_minutes: int,
        respect_working_hours: bool,
        tz: ZoneInfo,
        wh_start_h: int,
        wh_end_h: int,
        max_results: int,
    ) -> None:
        granularity = timedelta(minutes=15)
        duration = timedelta(minutes=duration_minutes)

        def _round_up_to_granularity(dt: datetime) -> datetime:
            secs = dt.minute * 60 + dt.second + dt.microsecond / 1_000_000
            grain = 15 * 60
            remainder = secs % grain
            if remainder == 0:
                return dt.replace(second=0, microsecond=0)
            add = grain - remainder
            return (dt + timedelta(seconds=add)).replace(microsecond=0).replace(second=0)

        def _day_end_of(local: datetime) -> datetime:
            """End-of-working-day boundary for ``local``'s date.

            ``wh_end_h == 24`` is allowed by the validator and means
            "end of day" — Python's ``datetime.replace(hour=24)`` raises,
            so we compute the next-midnight boundary manually for the 24
            case to keep the math correct.
            """
            if wh_end_h == 24:
                base = (local + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                return base
            return local.replace(hour=wh_end_h, minute=0, second=0, microsecond=0)

        cursor = _round_up_to_granularity(start)
        while cursor + duration <= end and len(out) < max_results:
            slot_end = cursor + duration
            slot_start = cursor
            if respect_working_hours:
                local_start = slot_start.astimezone(tz)
                local_end = slot_end.astimezone(tz)
                day_start = local_start.replace(hour=wh_start_h, minute=0, second=0, microsecond=0)
                day_end = _day_end_of(local_start)
                if local_start < day_start:
                    cursor = day_start.astimezone(slot_start.tzinfo or UTC)
                    cursor = _round_up_to_granularity(cursor)
                    continue
                if local_end > day_end:
                    # Jump to the next day's working window.
                    next_day = local_start + timedelta(days=1)
                    next_day_start = next_day.replace(
                        hour=wh_start_h, minute=0, second=0, microsecond=0
                    )
                    cursor = next_day_start.astimezone(slot_start.tzinfo or UTC)
                    cursor = _round_up_to_granularity(cursor)
                    continue
            slot_minutes = int((slot_end - slot_start).total_seconds() // 60)
            out.append(
                FreeSlot(
                    start=slot_start,
                    end=slot_end,
                    slot_duration_minutes=slot_minutes,
                    requested_duration_minutes=duration_minutes,
                )
            )
            cursor = cursor + granularity

    # ── Mutations (CalendarProvider) ─────────────────────────────────

    @staticmethod
    def _localize_naive(dt: datetime, tz_name: str) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=ZoneInfo(tz_name))
        return dt

    @staticmethod
    def _compute_idempotency_key(
        account_id: str,
        request: EventCreateRequest,
    ) -> str:
        attendee_emails = sorted(a.email for a in request.attendees)
        raw = (
            f"{account_id}|{request.title}|{request.start.isoformat()}|"
            f"{request.end.isoformat()}|{','.join(attendee_emails)}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    async def create_event(
        self,
        account_id: str,
        request: EventCreateRequest,
        user_ctx: UserContext,
    ) -> CalendarEvent:
        account = await self._require_account(account_id)
        self._require_access(account, user_ctx)
        runtime = self._runtimes.get(account_id)
        if runtime is None:
            raise RuntimeError(f"Calendar account {account_id} runtime is not active")
        if not request.idempotency_key:
            request.idempotency_key = self._compute_idempotency_key(account_id, request)
        request.start = self._localize_naive(request.start, account.timezone)
        request.end = self._localize_naive(request.end, account.timezone)
        evt = await runtime.backend.create_event(account.calendar_id, request)
        self._record_mutate_publish(account_id, evt.event_id)
        await self._publish_event_change("calendar.event.created", account, evt)
        return evt

    async def update_event(
        self,
        account_id: str,
        event_id: str,
        request: EventCreateRequest,
        user_ctx: UserContext,
        *,
        if_match_etag: str = "",
    ) -> CalendarEvent:
        """Update an event with optimistic concurrency.

        ``if_match_etag`` is REQUIRED — without it, a service-side fresh
        ``get_event`` would always match the server etag, defeating the
        whole point of OCC and silently masking writes that lost the
        race. Callers (the WS layer, the AI tool's preview/confirm
        flow, internal code) must read the event first and pass
        ``current.etag``.
        """
        account = await self._require_account(account_id)
        self._require_access(account, user_ctx)
        runtime = self._runtimes.get(account_id)
        if runtime is None:
            raise RuntimeError(f"Calendar account {account_id} runtime is not active")
        if not if_match_etag:
            raise ValueError(
                "if_match_etag is required for update_event — read the event "
                "first via get_event and pass its etag so optimistic "
                "concurrency can detect concurrent edits"
            )
        request.start = self._localize_naive(request.start, account.timezone)
        request.end = self._localize_naive(request.end, account.timezone)
        try:
            evt = await runtime.backend.update_event(
                account.calendar_id,
                event_id,
                request,
                if_match_etag=if_match_etag,
            )
        except CalendarBackendConflictError:
            raise
        if evt.recurring_event_id is not None:
            logger.info(
                "calendar.update_event on recurring instance (account=%s event=%s series=%s)",
                account.id,
                event_id,
                evt.recurring_event_id,
            )
        self._record_mutate_publish(account_id, evt.event_id)
        await self._publish_event_change("calendar.event.updated", account, evt)
        return evt

    async def delete_event(
        self,
        account_id: str,
        event_id: str,
        user_ctx: UserContext,
        *,
        send_cancellations: bool = False,
    ) -> None:
        account = await self._require_account(account_id)
        self._require_access(account, user_ctx)
        runtime = self._runtimes.get(account_id)
        if runtime is None:
            raise RuntimeError(f"Calendar account {account_id} runtime is not active")
        # Best-effort log on recurring instances for operator audit.
        try:
            current = await runtime.backend.get_event(account.calendar_id, event_id)
            if current is not None and current.recurring_event_id is not None:
                logger.info(
                    "calendar.delete_event on recurring instance (account=%s event=%s series=%s)",
                    account.id,
                    event_id,
                    current.recurring_event_id,
                )
        except Exception:
            pass
        await runtime.backend.delete_event(
            account.calendar_id,
            event_id,
            send_cancellations=send_cancellations,
        )
        self._record_mutate_publish(account_id, event_id)
        await self._publish_event(
            "calendar.event.deleted",
            {"account_id": account_id, "event_id": event_id},
        )

    # ── AI tools ─────────────────────────────────────────────────────

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        return [
            ToolDefinition(
                name="list_calendar_accounts",
                slash_group="calendar",
                slash_command="accounts",
                slash_help="List calendar accounts you can access",
                description=(
                    "List every calendar account the current user can "
                    "access. Returns each account's id, display name, "
                    "email_address, calendar_id, the user's timezone for "
                    "that account, the account's health (`ok` / "
                    "`unhealthy`), and how access was granted (one of: "
                    "`owner`, `admin`, `shared_user`, `shared_role`). Call "
                    "this first when the user's intent doesn't already "
                    "name a specific account, or when the user reports "
                    "'calendar isn't working' so you can check `health`."
                ),
                parameters=[],
                required_role="user",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="get_schedule",
                slash_group="calendar",
                slash_command="schedule",
                slash_help="Show the schedule for a date or range",
                description=(
                    "Return the user's events for a specific day or date "
                    "range across one or all accessible calendar "
                    "accounts. Defaults to today in the account's "
                    "timezone. Pass `date` for a single day (today / "
                    "tomorrow / yesterday or an ISO date), or pass "
                    "`start` and `end` for a range. Times in the response "
                    "are ISO 8601 with the account's timezone offset; "
                    "render in the user's local timezone when "
                    "summarizing. Returns `events` and a `warnings` "
                    "array — `warnings` is non-empty when one or more "
                    "accounts in the aggregate failed; mention warnings "
                    "to the user."
                ),
                parameters=[
                    ToolParameter(
                        name="account_id",
                        type=ToolParameterType.STRING,
                        description="Account id, or null to aggregate.",
                        required=False,
                    ),
                    ToolParameter(
                        name="date",
                        type=ToolParameterType.STRING,
                        description=(
                            "ISO date OR 'today' / 'tomorrow' / 'yesterday'"
                            " — mutually exclusive with start/end."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="start",
                        type=ToolParameterType.STRING,
                        description="ISO datetime range start.",
                        required=False,
                    ),
                    ToolParameter(
                        name="end",
                        type=ToolParameterType.STRING,
                        description="ISO datetime range end.",
                        required=False,
                    ),
                ],
                required_role="user",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="next_event",
                slash_group="calendar",
                slash_command="next",
                slash_help="Show the next event whose start is in the future",
                description=(
                    "Return the next single calendar event whose start "
                    "is in the future, optionally limited to a window "
                    "of N hours (default 72). Returns ANY event "
                    "including all-day events and solo events ('dentist "
                    "appointment', 'focus block') — not only "
                    "multi-attendee meetings. Pass `within_hours=null` "
                    "for unlimited; `0` is **not** a sentinel and will "
                    "return null. Times are ISO 8601 with the account's "
                    "timezone offset."
                ),
                parameters=[
                    ToolParameter(
                        name="account_id",
                        type=ToolParameterType.STRING,
                        description="Account id, or null to aggregate.",
                        required=False,
                    ),
                    ToolParameter(
                        name="within_hours",
                        type=ToolParameterType.INTEGER,
                        description=("Lookahead in hours. Default 72; null = unlimited."),
                        required=False,
                        default=72,
                    ),
                ],
                required_role="user",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="get_event",
                description=(
                    "Fetch one calendar event by id with full detail — "
                    "title, start, end, description, location, attendees "
                    "with their response statuses, and html_link to view "
                    "in the provider's UI. Use this after `get_schedule` "
                    "or `next_event` returns an event id when the user "
                    "asks 'tell me about that meeting' or wants attendee "
                    "details."
                ),
                parameters=[
                    ToolParameter(
                        name="account_id",
                        type=ToolParameterType.STRING,
                        description="Account id of the event.",
                    ),
                    ToolParameter(
                        name="event_id",
                        type=ToolParameterType.STRING,
                        description="Event id.",
                    ),
                ],
                required_role="user",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="find_free_time",
                description=(
                    "Find free intervals of at least `duration_minutes` "
                    "inside a window. Default window is the next 7 days "
                    "during working hours (per account). Pass "
                    "`attendee_emails` to find a time when the user AND "
                    "those attendees are all free — visibility into "
                    "other calendars depends on the other party's "
                    "sharing settings; if a target is unreadable, the "
                    "response includes a warning. 'Free' means: "
                    "confirmed/tentative count as busy (we don't suggest "
                    "free slots over a maybe-meeting); cancelled or "
                    "transparency=transparent count as free; all-day "
                    "events are treated as fully busy across the "
                    "working-hours window. Returns up to `max_results` "
                    "candidate slots, earliest first."
                ),
                parameters=[
                    ToolParameter(
                        name="duration_minutes",
                        type=ToolParameterType.INTEGER,
                        description="Required slot length in minutes (5–480).",
                    ),
                    ToolParameter(
                        name="account_id",
                        type=ToolParameterType.STRING,
                        description="Account id, or null to aggregate.",
                        required=False,
                    ),
                    ToolParameter(
                        name="start",
                        type=ToolParameterType.STRING,
                        description="ISO datetime — defaults to now.",
                        required=False,
                    ),
                    ToolParameter(
                        name="end",
                        type=ToolParameterType.STRING,
                        description="ISO datetime — defaults to start + 7 days.",
                        required=False,
                    ),
                    ToolParameter(
                        name="respect_working_hours",
                        type=ToolParameterType.BOOLEAN,
                        description="Honor each account's working-hours window.",
                        required=False,
                        default=True,
                    ),
                    ToolParameter(
                        name="max_results",
                        type=ToolParameterType.INTEGER,
                        description="Max candidate slots (default 5).",
                        required=False,
                        default=5,
                    ),
                    ToolParameter(
                        name="attendee_emails",
                        type=ToolParameterType.ARRAY,
                        description=(
                            "Optional list of attendee email addresses to intersect with."
                        ),
                        required=False,
                    ),
                ],
                required_role="user",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="create_event",
                description=(
                    "Create a new calendar event on a specific account. "
                    "**High blast radius**: this can send real invite "
                    "emails. Defaults to PREVIEW (`confirm=false`): you "
                    "receive a confirmation form back; only after the "
                    "user clicks Confirm should you re-call with the "
                    "SAME arguments plus `confirm=true`. If the user "
                    "gave you a relative time, call `system_datetime` "
                    "FIRST and compute the ISO string in the account's "
                    "timezone — DO NOT guess today's date. Pass either "
                    "`end` (ISO datetime) OR `duration_minutes` "
                    "(integer) — exactly one is required. If the user "
                    "references attendees by name, resolve to email "
                    "first via `directory_search` — DO NOT hallucinate "
                    "addresses. `send_invites` defaults to false; only "
                    "set true when the user explicitly says 'invite "
                    "them' or similar. Recurring phrases like 'every "
                    "Tuesday' are NOT supported — ask the user to set "
                    "those up manually. On success the response includes "
                    "`html_link`; include it in your reply."
                ),
                parameters=[
                    ToolParameter(
                        name="account_id",
                        type=ToolParameterType.STRING,
                        description=(
                            "Required when more than one account is "
                            "accessible; optional when only one."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="title",
                        type=ToolParameterType.STRING,
                        description="Event title.",
                    ),
                    ToolParameter(
                        name="start",
                        type=ToolParameterType.STRING,
                        description=(
                            "Strict ISO 8601 datetime. Naive values are "
                            "interpreted in the account's timezone."
                        ),
                    ),
                    ToolParameter(
                        name="end",
                        type=ToolParameterType.STRING,
                        description=("ISO datetime. Mutually exclusive with duration_minutes."),
                        required=False,
                    ),
                    ToolParameter(
                        name="duration_minutes",
                        type=ToolParameterType.INTEGER,
                        description=("Length in minutes. Mutually exclusive with end."),
                        required=False,
                    ),
                    ToolParameter(
                        name="description",
                        type=ToolParameterType.STRING,
                        description="Event description.",
                        required=False,
                    ),
                    ToolParameter(
                        name="location",
                        type=ToolParameterType.STRING,
                        description="Event location.",
                        required=False,
                    ),
                    ToolParameter(
                        name="attendees",
                        type=ToolParameterType.ARRAY,
                        description="List of attendee email addresses.",
                        required=False,
                    ),
                    ToolParameter(
                        name="all_day",
                        type=ToolParameterType.BOOLEAN,
                        description="True for an all-day event.",
                        required=False,
                        default=False,
                    ),
                    ToolParameter(
                        name="send_invites",
                        type=ToolParameterType.BOOLEAN,
                        description=("Email attendees on save. Defaults to false."),
                        required=False,
                        default=False,
                    ),
                    ToolParameter(
                        name="confirm",
                        type=ToolParameterType.BOOLEAN,
                        description=("Set true after the user clicks Confirm in the preview form."),
                        required=False,
                        default=False,
                    ),
                ],
                required_role="user",
                parallel_safe=False,
            ),
            ToolDefinition(
                name="update_event",
                description=(
                    "Modify an existing calendar event by id. **High "
                    "blast radius**: same preview/confirm pattern as "
                    "create_event — first call with `confirm=false` "
                    "returns a delta preview; only after the user clicks "
                    "Confirm should you re-call with `confirm=true`. "
                    "Only fields you supply are modified. For events "
                    "with `recurring_event_id` set, this modifies only "
                    "the SINGLE INSTANCE, not the whole series — tell "
                    "the user. If another client edited the event "
                    "between your read and write, you'll get an error "
                    "telling you to re-fetch with `get_event` and "
                    "retry."
                ),
                parameters=[
                    ToolParameter(
                        name="account_id",
                        type=ToolParameterType.STRING,
                        description="Account id of the event.",
                    ),
                    ToolParameter(
                        name="event_id",
                        type=ToolParameterType.STRING,
                        description="Event id.",
                    ),
                    ToolParameter(
                        name="title",
                        type=ToolParameterType.STRING,
                        description="New title.",
                        required=False,
                    ),
                    ToolParameter(
                        name="start",
                        type=ToolParameterType.STRING,
                        description="New ISO start datetime.",
                        required=False,
                    ),
                    ToolParameter(
                        name="end",
                        type=ToolParameterType.STRING,
                        description="New ISO end datetime.",
                        required=False,
                    ),
                    ToolParameter(
                        name="duration_minutes",
                        type=ToolParameterType.INTEGER,
                        description=("New duration. Mutually exclusive with end."),
                        required=False,
                    ),
                    ToolParameter(
                        name="description",
                        type=ToolParameterType.STRING,
                        description="New description.",
                        required=False,
                    ),
                    ToolParameter(
                        name="location",
                        type=ToolParameterType.STRING,
                        description="New location.",
                        required=False,
                    ),
                    ToolParameter(
                        name="attendees",
                        type=ToolParameterType.ARRAY,
                        description="Replacement attendee list (emails).",
                        required=False,
                    ),
                    ToolParameter(
                        name="send_invites",
                        type=ToolParameterType.BOOLEAN,
                        description="Email attendees on save.",
                        required=False,
                        default=False,
                    ),
                    ToolParameter(
                        name="confirm",
                        type=ToolParameterType.BOOLEAN,
                        description="Set true after user confirms.",
                        required=False,
                        default=False,
                    ),
                ],
                required_role="user",
                parallel_safe=False,
            ),
            ToolDefinition(
                name="delete_event",
                description=(
                    "Delete an existing calendar event by id. **High "
                    "blast radius**: same preview/confirm pattern as "
                    "create_event. `send_cancellations` defaults to "
                    "false; set true when the user says 'tell everyone "
                    "it's cancelled'. For recurring instances, this "
                    "deletes only the single instance."
                ),
                parameters=[
                    ToolParameter(
                        name="account_id",
                        type=ToolParameterType.STRING,
                        description="Account id of the event.",
                    ),
                    ToolParameter(
                        name="event_id",
                        type=ToolParameterType.STRING,
                        description="Event id.",
                    ),
                    ToolParameter(
                        name="send_cancellations",
                        type=ToolParameterType.BOOLEAN,
                        description="Email attendees that the event is cancelled.",
                        required=False,
                        default=False,
                    ),
                    ToolParameter(
                        name="confirm",
                        type=ToolParameterType.BOOLEAN,
                        description="Set true after user confirms.",
                        required=False,
                        default=False,
                    ),
                ],
                required_role="user",
                parallel_safe=False,
            ),
        ]

    def _resolve_user_ctx_from_args(
        self,
        arguments: dict[str, Any],
    ) -> UserContext:
        """Construct a ``UserContext`` from injected ``_user_*`` args.

        Calendar tools touch personal data (PII attendees, free/busy
        windows). The AI service injects ``_user_id`` automatically; if
        it's missing the call hasn't been authenticated and we MUST NOT
        silently elevate to ``UserContext.SYSTEM`` — that would let any
        unauthenticated tool invocation see / mutate every user's
        calendar. Raise instead so the failure is loud and visible.
        """
        user_id = str(arguments.get("_user_id") or "")
        if not user_id:
            raise PermissionError(
                "missing user context — calendar tools require an authenticated _user_id"
            )
        roles_raw = arguments.get("_user_roles") or []
        roles: frozenset[str]
        if isinstance(roles_raw, (list, tuple, set, frozenset)):
            roles = frozenset(str(r) for r in roles_raw)
        else:
            roles = frozenset()
        return UserContext(
            user_id=user_id,
            email=str(arguments.get("_user_email") or ""),
            display_name=str(arguments.get("_user_name") or user_id),
            roles=roles,
        )

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str | ToolOutput:
        match name:
            case "list_calendar_accounts":
                return await self._tool_list_accounts(arguments)
            case "get_schedule":
                return await self._tool_get_schedule(arguments)
            case "next_event":
                return await self._tool_next_event(arguments)
            case "get_event":
                return await self._tool_get_event(arguments)
            case "find_free_time":
                return await self._tool_find_free_time(arguments)
            case "create_event":
                return await self._tool_create_event(arguments)
            case "update_event":
                return await self._tool_update_event(arguments)
            case "delete_event":
                return await self._tool_delete_event(arguments)
            case _:
                raise KeyError(f"Unknown tool: {name}")

    async def _tool_list_accounts(self, arguments: dict[str, Any]) -> str:
        user_ctx = self._resolve_user_ctx_from_args(arguments)
        all_accounts = await self._load_accounts()
        out: list[dict[str, Any]] = []
        for a in all_accounts:
            access = determine_access(user_ctx, a)
            if access is None:
                continue
            out.append(
                {
                    "account_id": a.id,
                    "name": a.name,
                    "email_address": a.email_address,
                    "calendar_id": a.calendar_id,
                    "timezone": a.timezone,
                    "access": access.value,
                    "health": a.health,
                    "last_error": a.last_error,
                }
            )
        if not out:
            return "You have no accessible calendar accounts."
        return json.dumps(out, indent=2)

    @staticmethod
    def _parse_tool_datetime(value: str, fallback_tz: str) -> datetime:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(fallback_tz))
        return dt

    @staticmethod
    def _parse_date_arg(value: str, tz_name: str) -> tuple[datetime, datetime]:
        tz = ZoneInfo(tz_name)
        today = datetime.now(tz).date()
        if value == "today":
            d = today
        elif value == "tomorrow":
            d = today + timedelta(days=1)
        elif value == "yesterday":
            d = today - timedelta(days=1)
        else:
            try:
                d = datetime.fromisoformat(value).date()
            except ValueError as exc:
                raise ValueError(
                    f"date must be 'today'/'tomorrow'/'yesterday' or ISO date (got {value!r})"
                ) from exc
        start = datetime(d.year, d.month, d.day, tzinfo=tz)
        end = start + timedelta(days=1)
        return start, end

    async def _resolve_tool_account(
        self,
        user_ctx: UserContext,
        account_id: str | None,
        *,
        require_when_multiple: bool = False,
    ) -> tuple[CalendarAccount | None, str | None]:
        """Returns (account, error_msg). When ``account_id`` is None and
        the user has exactly one accessible account, returns it; with
        multiple accessible accounts and ``require_when_multiple=True``,
        returns an error string for the AI."""
        if account_id:
            try:
                account = await self._require_account(account_id)
            except CalendarAccountNotFoundError:
                return None, f"Calendar account not found: {account_id}"
            try:
                self._require_access(account, user_ctx)
            except CalendarPermissionError:
                return None, (
                    f"You don't have access to calendar account {account_id}. "
                    "Call list_calendar_accounts to see what you can access."
                )
            return account, None
        # No account_id given.
        accessible = await self.list_accessible_accounts(user_ctx)
        if not accessible:
            return None, "You have no accessible calendar accounts."
        if len(accessible) == 1:
            return accessible[0], None
        if require_when_multiple:
            return None, (
                "More than one calendar account is accessible — pass "
                "account_id (call list_calendar_accounts first to see "
                "the options)."
            )
        return None, None

    async def _tool_get_schedule(self, arguments: dict[str, Any]) -> str:
        user_ctx = self._resolve_user_ctx_from_args(arguments)
        account_id = arguments.get("account_id") or None
        account, err = await self._resolve_tool_account(
            user_ctx, account_id, require_when_multiple=False
        )
        if err is not None:
            return err
        # Determine effective tz.
        if account is not None:
            tz_name = account.timezone
        else:
            accessible = await self.list_accessible_accounts(user_ctx)
            tz_name = accessible[0].timezone if accessible else "UTC"

        date = arguments.get("date") or None
        start_arg = arguments.get("start") or None
        end_arg = arguments.get("end") or None
        if date and (start_arg or end_arg):
            return "Pass either `date` OR `start`+`end`, not both."
        try:
            if date:
                time_min, time_max = self._parse_date_arg(str(date), tz_name)
            elif start_arg and end_arg:
                time_min = self._parse_tool_datetime(str(start_arg), tz_name)
                time_max = self._parse_tool_datetime(str(end_arg), tz_name)
            elif start_arg and not end_arg:
                return "`end` is required when `start` is given."
            else:
                time_min, time_max = self._parse_date_arg("today", tz_name)
        except ValueError as exc:
            return str(exc)
        try:
            agg = await self.list_events(
                account.id if account else None,
                time_min,
                time_max,
                user_ctx,
            )
        except CalendarPermissionError as exc:
            return str(exc)
        return json.dumps(
            {
                "events": [
                    {
                        "event_id": e.event_id,
                        "account_id": e.account_id,
                        "title": e.title,
                        "start": e.start.isoformat(),
                        "end": e.end.isoformat(),
                        "location": e.location,
                        "attendees": [a.email for a in e.attendees],
                        "all_day": e.all_day,
                        "status": e.status.value,
                    }
                    for e in agg.events
                ],
                "warnings": agg.warnings,
            },
            indent=2,
        )

    async def _tool_next_event(self, arguments: dict[str, Any]) -> str:
        user_ctx = self._resolve_user_ctx_from_args(arguments)
        account_id = arguments.get("account_id") or None
        account, err = await self._resolve_tool_account(
            user_ctx, account_id, require_when_multiple=False
        )
        if err is not None:
            return err
        within_hours_raw = arguments.get("within_hours", 72)
        within: timedelta | None
        if within_hours_raw is None:
            within = None
        else:
            try:
                hrs = int(within_hours_raw)
            except (TypeError, ValueError):
                hrs = 72
            if hrs <= 0:
                return "null"
            within = timedelta(hours=hrs)
        evt = await self.next_event(account.id if account else None, user_ctx, within=within)
        if evt is None:
            return "null"
        return json.dumps(
            {
                "event_id": evt.event_id,
                "account_id": evt.account_id,
                "title": evt.title,
                "start": evt.start.isoformat(),
                "end": evt.end.isoformat(),
                "location": evt.location,
                "attendees": [a.email for a in evt.attendees],
                "all_day": evt.all_day,
                "status": evt.status.value,
            },
            indent=2,
        )

    async def _tool_get_event(self, arguments: dict[str, Any]) -> str:
        user_ctx = self._resolve_user_ctx_from_args(arguments)
        account_id = str(arguments.get("account_id") or "")
        event_id = str(arguments.get("event_id") or "")
        if not account_id:
            return "account_id is required."
        if not event_id:
            return "event_id is required."
        try:
            account = await self._require_account(account_id)
        except CalendarAccountNotFoundError:
            return f"Calendar account not found: {account_id}"
        try:
            self._require_access(account, user_ctx)
        except CalendarPermissionError as exc:
            return str(exc)
        runtime = self._runtimes.get(account_id)
        if runtime is None:
            row_id = self._event_row_id(account_id, event_id)
            row = await self._storage.get(_EVENTS_COLLECTION, row_id)
            if row is None:
                return f"Event not found: {event_id}"
            evt = self._event_row_to_event(row)
        else:
            try:
                evt_opt = await runtime.backend.get_event(account.calendar_id, event_id)
            except CalendarBackendNotFoundError:
                return f"Event not found: {event_id}"
            except CalendarBackendError as exc:
                return f"Calendar error: {exc}"
            if evt_opt is None:
                return f"Event not found: {event_id}"
            evt = evt_opt
        return json.dumps(
            {
                "event_id": evt.event_id,
                "account_id": evt.account_id,
                "title": evt.title,
                "start": evt.start.isoformat(),
                "end": evt.end.isoformat(),
                "description": evt.description,
                "location": evt.location,
                "attendees": [a.to_dict() for a in evt.attendees],
                "html_link": evt.html_link,
                "all_day": evt.all_day,
                "status": evt.status.value,
                "recurring_event_id": evt.recurring_event_id,
            },
            indent=2,
        )

    async def _tool_find_free_time(self, arguments: dict[str, Any]) -> str:
        user_ctx = self._resolve_user_ctx_from_args(arguments)
        try:
            duration_minutes = int(arguments["duration_minutes"])
        except (KeyError, TypeError, ValueError):
            return "duration_minutes (int) is required."
        account_id = arguments.get("account_id") or None
        account, err = await self._resolve_tool_account(
            user_ctx, account_id, require_when_multiple=False
        )
        if err is not None:
            return err
        if account is not None:
            tz_name = account.timezone
        else:
            accessible = await self.list_accessible_accounts(user_ctx)
            tz_name = accessible[0].timezone if accessible else "UTC"

        start_arg = arguments.get("start")
        end_arg = arguments.get("end")
        try:
            time_min = (
                self._parse_tool_datetime(str(start_arg), tz_name)
                if start_arg
                else datetime.now(UTC)
            )
            time_max = (
                self._parse_tool_datetime(str(end_arg), tz_name)
                if end_arg
                else time_min + timedelta(days=7)
            )
        except ValueError as exc:
            return str(exc)
        respect_wh = bool(arguments.get("respect_working_hours", True))
        try:
            max_results = int(arguments.get("max_results", 5) or 5)
        except (TypeError, ValueError):
            max_results = 5
        attendees_raw = arguments.get("attendee_emails") or None
        attendee_emails = (
            [str(a) for a in attendees_raw] if isinstance(attendees_raw, list) else None
        )
        try:
            result = await self.find_free_time(
                account.id if account else None,
                time_min,
                time_max,
                duration_minutes,
                user_ctx,
                respect_working_hours=respect_wh,
                max_results=max_results,
                attendee_emails=attendee_emails,
            )
        except ValueError as exc:
            return str(exc)
        return json.dumps(
            {
                "slots": [
                    {
                        "start": s.start.isoformat(),
                        "end": s.end.isoformat(),
                        "slot_duration_minutes": s.slot_duration_minutes,
                        "requested_duration_minutes": s.requested_duration_minutes,
                    }
                    for s in result.slots
                ],
                "warnings": list(result.warnings),
            },
            indent=2,
        )

    @staticmethod
    def _build_create_request(
        arguments: dict[str, Any],
        account: CalendarAccount,
    ) -> tuple[EventCreateRequest, str | None]:
        from gilbert.interfaces.calendar import CalendarAttendee

        title = str(arguments.get("title") or "")
        if not title:
            return EventCreateRequest(
                title="", start=datetime.now(UTC), end=datetime.now(UTC)
            ), "title is required."
        start_raw = arguments.get("start")
        if not start_raw:
            return EventCreateRequest(
                title=title, start=datetime.now(UTC), end=datetime.now(UTC)
            ), "start is required."
        end_raw = arguments.get("end")
        duration_raw = arguments.get("duration_minutes")
        if end_raw and duration_raw:
            return EventCreateRequest(
                title=title, start=datetime.now(UTC), end=datetime.now(UTC)
            ), "Pass either end OR duration_minutes, not both."
        try:
            start = CalendarService._parse_tool_datetime(str(start_raw), account.timezone)
        except ValueError as exc:
            return EventCreateRequest(
                title=title, start=datetime.now(UTC), end=datetime.now(UTC)
            ), f"invalid start: {exc}"
        if end_raw:
            try:
                end = CalendarService._parse_tool_datetime(str(end_raw), account.timezone)
            except ValueError as exc:
                return EventCreateRequest(
                    title=title, start=start, end=start
                ), f"invalid end: {exc}"
        elif duration_raw:
            try:
                minutes = int(duration_raw)
            except (TypeError, ValueError):
                return EventCreateRequest(
                    title=title, start=start, end=start
                ), "duration_minutes must be an integer."
            if minutes <= 0:
                return EventCreateRequest(
                    title=title, start=start, end=start
                ), "duration_minutes must be > 0."
            end = start + timedelta(minutes=minutes)
        else:
            return EventCreateRequest(
                title=title, start=start, end=start
            ), "Pass either end (ISO) OR duration_minutes (int)."
        attendees_raw = arguments.get("attendees") or []
        attendees = [CalendarAttendee(email=str(a)) for a in attendees_raw if isinstance(a, str)]
        return (
            EventCreateRequest(
                title=title,
                start=start,
                end=end,
                description=str(arguments.get("description") or ""),
                location=str(arguments.get("location") or ""),
                attendees=attendees,
                all_day=bool(arguments.get("all_day", False)),
                send_invites=bool(arguments.get("send_invites", False)),
            ),
            None,
        )

    async def _tool_create_event(self, arguments: dict[str, Any]) -> str | ToolOutput:
        user_ctx = self._resolve_user_ctx_from_args(arguments)
        account, err = await self._resolve_tool_account(
            user_ctx,
            arguments.get("account_id") or None,
            require_when_multiple=True,
        )
        if err is not None:
            return err
        assert account is not None
        request, build_err = self._build_create_request(arguments, account)
        if build_err is not None:
            return build_err
        confirm = bool(arguments.get("confirm", False))
        attendee_str = (
            ", ".join(a.email for a in request.attendees) if request.attendees else "(none)"
        )
        summary = (
            f"I'm about to create '{request.title}' on "
            f"{request.start.isoformat()} (account {account.name}) — confirm?"
        )
        summary_lines = [
            f"**{request.title}**",
            f"start: {request.start.isoformat()}",
            f"end:   {request.end.isoformat()}",
            f"account: {account.name}",
            f"attendees: {attendee_str}",
            f"send_invites: {request.send_invites}",
        ]

        async def _do_create() -> str | ToolOutput:
            try:
                evt = await self.create_event(account.id, request, user_ctx)
            except CalendarPermissionError as exc:
                return str(exc)
            except CalendarBackendError as exc:
                return f"Calendar backend error: {exc}"
            return json.dumps(
                {
                    "event_id": evt.event_id,
                    "account_id": evt.account_id,
                    "title": evt.title,
                    "start": evt.start.isoformat(),
                    "end": evt.end.isoformat(),
                    "html_link": evt.html_link,
                },
                indent=2,
            )

        return await confirm_or_execute(
            confirm=confirm,
            tool_name="create_event",
            title="Create calendar event",
            summary=summary,
            summary_lines=summary_lines,
            arguments=arguments,
            execute=_do_create,
        )

    async def _tool_update_event(self, arguments: dict[str, Any]) -> str | ToolOutput:
        user_ctx = self._resolve_user_ctx_from_args(arguments)
        account_id = str(arguments.get("account_id") or "")
        event_id = str(arguments.get("event_id") or "")
        if not account_id:
            return "account_id is required."
        if not event_id:
            return "event_id is required."
        try:
            account = await self._require_account(account_id)
        except CalendarAccountNotFoundError:
            return f"Calendar account not found: {account_id}"
        try:
            self._require_access(account, user_ctx)
        except CalendarPermissionError as exc:
            return str(exc)
        runtime = self._runtimes.get(account_id)
        if runtime is None:
            return f"Calendar account {account_id} runtime is not active. Enable it and retry."
        confirm = bool(arguments.get("confirm", False))
        # The etag round-trips through the preview/confirm form via the
        # hidden ``pending_arguments`` field on the UIBlock — see
        # ``memory-calendar-service.md``. On the confirm leg we trust the
        # stashed etag instead of re-reading the event, so the OCC check
        # actually compares the etag the user *saw* in the preview
        # against the live etag at write time. Re-reading would defeat
        # the whole point.
        stashed_etag = str(arguments.get("_etag") or "")

        if confirm and not stashed_etag:
            return (
                "Cannot confirm update without preview state. Re-call "
                "update_event with confirm=false to generate a preview "
                "first."
            )

        if confirm and stashed_etag:
            # Confirm leg: use the etag the user saw at preview time.
            request, build_err = self._build_create_request(
                self._merged_update_args_from_confirm(arguments),
                account,
            )
            if build_err is not None:
                return build_err
            try:
                evt = await self.update_event(
                    account_id,
                    event_id,
                    request,
                    user_ctx,
                    if_match_etag=stashed_etag,
                )
            except CalendarBackendConflictError:
                return (
                    "The event changed since you fetched it. Call "
                    "get_event to re-read and try update_event again."
                )
            except CalendarPermissionError as exc:
                return str(exc)
            except CalendarBackendError as exc:
                return f"Calendar backend error: {exc}"
            return json.dumps(
                {
                    "event_id": evt.event_id,
                    "account_id": evt.account_id,
                    "title": evt.title,
                    "start": evt.start.isoformat(),
                    "end": evt.end.isoformat(),
                    "html_link": evt.html_link,
                },
                indent=2,
            )

        # Preview leg — read current to build the delta + capture etag.
        try:
            current = await runtime.backend.get_event(account.calendar_id, event_id)
        except CalendarBackendNotFoundError:
            return f"Event not found: {event_id}"
        except CalendarBackendError as exc:
            return f"Calendar error: {exc}"
        if current is None:
            return f"Event not found: {event_id}"

        # Build merged request for update — start with current, overlay
        # new fields from arguments.
        merged_args: dict[str, Any] = {
            "title": arguments.get("title", current.title),
            "start": arguments.get("start", current.start.isoformat()),
            "end": arguments.get("end"),
            "duration_minutes": arguments.get("duration_minutes"),
            "description": arguments.get("description", current.description),
            "location": arguments.get("location", current.location),
            "attendees": (
                arguments.get("attendees")
                if "attendees" in arguments
                else [a.email for a in current.attendees]
            ),
            "all_day": arguments.get("all_day", current.all_day),
            "send_invites": arguments.get("send_invites", False),
        }
        if not merged_args.get("end") and not merged_args.get("duration_minutes"):
            merged_args["end"] = current.end.isoformat()
        request, build_err = self._build_create_request(merged_args, account)
        if build_err is not None:
            return build_err
        delta_lines: list[str] = [f"**update {current.title}**"]
        if "title" in arguments:
            delta_lines.append(f"title: {current.title!r} → {request.title!r}")
        if "start" in arguments:
            delta_lines.append(f"start: {current.start.isoformat()} → {request.start.isoformat()}")
        if "end" in arguments or "duration_minutes" in arguments:
            delta_lines.append(f"end:   {current.end.isoformat()} → {request.end.isoformat()}")
        if "description" in arguments:
            delta_lines.append(
                f"description: {current.description!r} → {request.description!r}"
            )
        if "location" in arguments:
            delta_lines.append(f"location: {current.location!r} → {request.location!r}")
        if "attendees" in arguments:
            old = ", ".join(a.email for a in current.attendees) or "(none)"
            new = ", ".join(a.email for a in request.attendees) or "(none)"
            delta_lines.append(f"attendees: {old} → {new}")
        delta_lines.append(f"send_invites: {request.send_invites}")
        if current.recurring_event_id:
            delta_lines.append("(recurring instance — only this occurrence will change)")
        summary = f"Updating event '{current.title}' (id={event_id}) — confirm?"

        # Stash the etag and the merged-arg overlay onto the preview
        # arguments so the confirm leg can apply them without re-reading.
        preview_args = dict(arguments)
        preview_args["_etag"] = current.etag
        preview_args["_merged"] = merged_args

        return build_preview_output(
            tool_name="update_event",
            title="Update calendar event",
            summary=summary,
            summary_lines=delta_lines,
            arguments=preview_args,
        )

    @staticmethod
    def _merged_update_args_from_confirm(arguments: dict[str, Any]) -> dict[str, Any]:
        """Reconstruct the merged update args for the confirm leg.

        The preview leg stashes the merged dict under ``_merged`` so we
        don't need to re-read the event at confirm time. We also accept
        a confirm leg that lacks ``_merged`` (older flow) by falling
        back to the raw arguments.
        """
        merged = arguments.get("_merged")
        if isinstance(merged, dict):
            return dict(merged)
        return dict(arguments)

    async def _tool_delete_event(self, arguments: dict[str, Any]) -> str | ToolOutput:
        user_ctx = self._resolve_user_ctx_from_args(arguments)
        account_id = str(arguments.get("account_id") or "")
        event_id = str(arguments.get("event_id") or "")
        if not account_id:
            return "account_id is required."
        if not event_id:
            return "event_id is required."
        try:
            account = await self._require_account(account_id)
        except CalendarAccountNotFoundError:
            return f"Calendar account not found: {account_id}"
        try:
            self._require_access(account, user_ctx)
        except CalendarPermissionError as exc:
            return str(exc)
        runtime = self._runtimes.get(account_id)
        if runtime is None:
            return f"Calendar account {account_id} runtime is not active. Enable it and retry."
        # Read for preview.
        current = None
        with contextlib.suppress(CalendarBackendError):
            current = await runtime.backend.get_event(account.calendar_id, event_id)
        if current is None:
            return f"Event not found: {event_id}"
        send_cancellations = bool(arguments.get("send_cancellations", False))
        confirm = bool(arguments.get("confirm", False))
        delta_lines = [
            f"**delete {current.title}**",
            f"start: {current.start.isoformat()}",
            f"attendees: {len(current.attendees)}",
            f"send_cancellations: {send_cancellations}",
        ]
        if current.recurring_event_id:
            delta_lines.append("(recurring instance — only this one will be removed)")
        summary = f"Deleting event '{current.title}' (id={event_id}) — confirm?"

        async def _do_delete() -> str | ToolOutput:
            try:
                await self.delete_event(
                    account_id,
                    event_id,
                    user_ctx,
                    send_cancellations=send_cancellations,
                )
            except CalendarPermissionError as exc:
                return str(exc)
            except CalendarBackendError as exc:
                return f"Calendar backend error: {exc}"
            return json.dumps({"deleted": True, "event_id": event_id})

        return await confirm_or_execute(
            confirm=confirm,
            tool_name="delete_event",
            title="Delete calendar event",
            summary=summary,
            summary_lines=delta_lines,
            arguments=arguments,
            execute=_do_delete,
        )

    # ── WS RPCs ──────────────────────────────────────────────────────

    def get_ws_handlers(self) -> dict[str, Any]:
        return {
            "calendar.accounts.list": self._ws_accounts_list,
            "calendar.accounts.get": self._ws_accounts_get,
            "calendar.accounts.create": self._ws_accounts_create,
            "calendar.accounts.update": self._ws_accounts_update,
            "calendar.accounts.delete": self._ws_accounts_delete,
            "calendar.accounts.share_user": self._ws_accounts_share_user,
            "calendar.accounts.unshare_user": self._ws_accounts_unshare_user,
            "calendar.accounts.share_role": self._ws_accounts_share_role,
            "calendar.accounts.unshare_role": self._ws_accounts_unshare_role,
            "calendar.accounts.test_connection": self._ws_accounts_test,
            "calendar.accounts.probe_calendars": self._ws_accounts_probe,
            "calendar.accounts.reveal_backend_config": self._ws_accounts_reveal_backend_config,
            "calendar.events.list": self._ws_events_list,
            "calendar.events.get": self._ws_events_get,
            "calendar.events.create": self._ws_events_create,
            "calendar.events.update": self._ws_events_update,
            "calendar.events.delete": self._ws_events_delete,
            "calendar.freebusy.get": self._ws_freebusy_get,
            "calendar.find_free_time": self._ws_find_free_time,
            "calendar.backends.list": self._ws_backends_list,
        }

    @staticmethod
    def _err(frame: dict[str, Any], msg: str, code: int) -> dict[str, Any]:
        return {
            "type": "gilbert.error",
            "ref": frame.get("id"),
            "error": msg,
            "code": code,
        }

    _BACKEND_CONFIG_MASK = "********"

    @classmethod
    def _mask_backend_config(
        cls,
        backend_name: str,
        backend_config: dict[str, Any],
    ) -> dict[str, Any]:
        """Replace sensitive ConfigParam values with ``********``.

        Walks the backend's declared ``backend_config_params()`` and
        masks any key whose ConfigParam has ``sensitive=True``. Used
        anywhere the backend_config is returned to clients (default
        path is masked; ``accounts.reveal_backend_config`` returns the
        unmasked value to admins only, with an audit log line).
        """
        backends = CalendarBackend.registered_backends()
        backend_cls = backends.get(backend_name)
        if backend_cls is None:
            return dict(backend_config)
        sensitive_keys = {p.key for p in backend_cls.backend_config_params() if p.sensitive}
        result = dict(backend_config)
        for key in sensitive_keys:
            if key in result and result[key]:
                result[key] = cls._BACKEND_CONFIG_MASK
        return result

    @classmethod
    def _account_payload(
        cls,
        account: CalendarAccount,
        user_ctx: UserContext,
        *,
        reveal_backend_config: bool = False,
    ) -> dict[str, Any]:
        access = determine_access(user_ctx, account)
        backend_config = (
            dict(account.backend_config)
            if reveal_backend_config
            else cls._mask_backend_config(account.backend_name, account.backend_config)
        )
        return {
            "id": account.id,
            "name": account.name,
            "email_address": account.email_address,
            "backend_name": account.backend_name,
            "backend_config": backend_config,
            "calendar_id": account.calendar_id,
            "timezone": account.timezone,
            "working_hours_start_hour": account.working_hours_start_hour,
            "working_hours_end_hour": account.working_hours_end_hour,
            "owner_user_id": account.owner_user_id,
            "shared_with_users": list(account.shared_with_users),
            "shared_with_roles": list(account.shared_with_roles),
            "poll_enabled": account.poll_enabled,
            "poll_interval_sec": account.poll_interval_sec,
            "upcoming_event_lookahead_minutes": (account.upcoming_event_lookahead_minutes),
            "health": account.health,
            "last_error": account.last_error,
            "last_error_at": account.last_error_at,
            "created_at": account.created_at,
            "access": access.value if access is not None else None,
            "can_admin": can_admin_account(user_ctx, account),
        }

    async def _ws_accounts_list(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        user_ctx = conn.user_ctx
        out = []
        for a in await self._load_accounts():
            if determine_access(user_ctx, a) is None:
                continue
            out.append(self._account_payload(a, user_ctx))
        return {
            "type": "calendar.accounts.list.result",
            "ref": frame.get("id"),
            "accounts": out,
        }

    async def _ws_accounts_get(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        account_id = str(frame.get("account_id") or "")
        try:
            account = await self._require_account(account_id)
        except CalendarAccountNotFoundError:
            return self._err(frame, "Account not found", 404)
        if determine_access(conn.user_ctx, account) is None:
            return self._err(frame, "Forbidden", 403)
        return {
            "type": "calendar.accounts.get.result",
            "ref": frame.get("id"),
            "account": self._account_payload(account, conn.user_ctx),
        }

    async def _ws_accounts_create(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        try:
            account = CalendarAccount(
                id=str(frame.get("id") or ""),
                name=str(frame.get("name") or ""),
                email_address=str(frame.get("email_address") or ""),
                backend_name=str(frame.get("backend_name") or ""),
                backend_config=dict(frame.get("backend_config") or {}),
                calendar_id=str(frame.get("calendar_id") or "primary"),
                timezone=str(frame.get("timezone") or "UTC"),
                working_hours_start_hour=int(frame.get("working_hours_start_hour", 9) or 9),
                working_hours_end_hour=int(frame.get("working_hours_end_hour", 18) or 18),
                poll_enabled=bool(frame.get("poll_enabled", True)),
                poll_interval_sec=int(frame.get("poll_interval_sec", 300) or 300),
                upcoming_event_lookahead_minutes=int(
                    frame.get("upcoming_event_lookahead_minutes", 15) or 15
                ),
            )
        except (ValueError, TypeError) as exc:
            return self._err(frame, str(exc), 400)
        if not account.name or not account.backend_name:
            return self._err(frame, "name and backend_name are required", 400)
        try:
            created = await self.create_account(account, conn.user_ctx)
        except ValueError as exc:
            return self._err(frame, str(exc), 400)
        return {
            "type": "calendar.accounts.create.result",
            "ref": frame.get("id"),
            "account": self._account_payload(created, conn.user_ctx),
        }

    async def _ws_accounts_update(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        account_id = str(frame.get("account_id") or "")
        updates = frame.get("updates") or {}
        if not isinstance(updates, dict):
            return self._err(frame, "updates must be an object", 400)
        try:
            account = await self.update_account(account_id, updates, conn.user_ctx)
        except CalendarPermissionError as exc:
            return self._err(frame, str(exc), 403)
        except CalendarAccountNotFoundError as exc:
            return self._err(frame, str(exc), 404)
        except ValueError as exc:
            return self._err(frame, str(exc), 400)
        return {
            "type": "calendar.accounts.update.result",
            "ref": frame.get("id"),
            "account": self._account_payload(account, conn.user_ctx),
        }

    async def _ws_accounts_delete(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        account_id = str(frame.get("account_id") or "")
        try:
            await self.delete_account(account_id, conn.user_ctx)
        except CalendarPermissionError as exc:
            return self._err(frame, str(exc), 403)
        except CalendarAccountNotFoundError as exc:
            return self._err(frame, str(exc), 404)
        return {
            "type": "calendar.accounts.delete.result",
            "ref": frame.get("id"),
            "status": "ok",
        }

    async def _ws_accounts_test(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        account_id = str(frame.get("account_id") or "")
        try:
            result = await self.test_account_connection(account_id, conn.user_ctx)
        except CalendarPermissionError as exc:
            return self._err(frame, str(exc), 403)
        except CalendarAccountNotFoundError as exc:
            return self._err(frame, str(exc), 404)
        return {
            "type": "calendar.accounts.test_connection.result",
            "ref": frame.get("id"),
            **result,
        }

    async def _ws_accounts_probe(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        account_id = str(frame.get("account_id") or "")
        try:
            calendars = await self.probe_calendars(account_id, conn.user_ctx)
        except CalendarPermissionError as exc:
            return self._err(frame, str(exc), 403)
        except CalendarAccountNotFoundError as exc:
            return self._err(frame, str(exc), 404)
        except ValueError as exc:
            return self._err(frame, str(exc), 400)
        except Exception as exc:
            return self._err(frame, str(exc), 500)
        return {
            "type": "calendar.accounts.probe_calendars.result",
            "ref": frame.get("id"),
            "calendars": calendars,
        }

    async def _ws_accounts_reveal_backend_config(
        self,
        conn: Any,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        """Return the unmasked ``backend_config`` to admins only.

        Default `_account_payload` masks ConfigParam values declared
        ``sensitive=True``. The SPA's account-edit drawer calls this RPC
        on edit-open when the user has admin access so the form can
        repopulate (and not overwrite) the live secret values. Logs an
        audit line every time the secret is revealed.
        """
        account_id = str(frame.get("account_id") or "")
        try:
            account = await self._require_account(account_id)
        except CalendarAccountNotFoundError as exc:
            return self._err(frame, str(exc), 404)
        if not can_admin_account(conn.user_ctx, account):
            return self._err(frame, "Forbidden — admin access required", 403)
        logger.info(
            "calendar.accounts.reveal_backend_config: account=%s requester=%s",
            account.id,
            conn.user_ctx.user_id,
        )
        return {
            "type": "calendar.accounts.reveal_backend_config.result",
            "ref": frame.get("id"),
            "account_id": account.id,
            "backend_config": dict(account.backend_config),
        }

    async def _ws_accounts_share_user(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        return await self._ws_share_helper(conn, frame, share=True, share_role=False)

    async def _ws_accounts_unshare_user(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        return await self._ws_share_helper(conn, frame, share=False, share_role=False)

    async def _ws_accounts_share_role(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        return await self._ws_share_helper(conn, frame, share=True, share_role=True)

    async def _ws_accounts_unshare_role(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        return await self._ws_share_helper(conn, frame, share=False, share_role=True)

    async def _ws_share_helper(
        self,
        conn: Any,
        frame: dict[str, Any],
        *,
        share: bool,
        share_role: bool,
    ) -> dict[str, Any]:
        account_id = str(frame.get("account_id") or "")
        target = str(frame.get("role") or frame.get("user_id") or "")
        result_verb = (
            ("share_role" if share_role else "share_user")
            if share
            else ("unshare_role" if share_role else "unshare_user")
        )
        try:
            method = (
                self.share_role
                if (share and share_role)
                else self.unshare_role
                if (not share and share_role)
                else self.share_user
                if (share and not share_role)
                else self.unshare_user
            )
            account = await method(account_id, target, conn.user_ctx)
        except CalendarPermissionError as exc:
            return self._err(frame, str(exc), 403)
        except CalendarAccountNotFoundError as exc:
            return self._err(frame, str(exc), 404)
        return {
            "type": f"calendar.accounts.{result_verb}.result",
            "ref": frame.get("id"),
            "account": self._account_payload(account, conn.user_ctx),
        }

    async def _ws_events_list(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        try:
            time_min = datetime.fromisoformat(str(frame.get("time_min", "")))
            time_max = datetime.fromisoformat(str(frame.get("time_max", "")))
        except ValueError as exc:
            return self._err(frame, f"invalid time range: {exc}", 400)
        try:
            agg = await self.list_events(
                frame.get("account_id"),
                time_min,
                time_max,
                conn.user_ctx,
                max_results=int(frame.get("max_results", 250) or 250),
            )
        except CalendarPermissionError as exc:
            return self._err(frame, str(exc), 403)
        except CalendarAccountNotFoundError as exc:
            return self._err(frame, str(exc), 404)
        return {
            "type": "calendar.events.list.result",
            "ref": frame.get("id"),
            "events": [e.to_dict() for e in agg.events],
            "warnings": agg.warnings,
        }

    async def _ws_events_get(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        account_id = str(frame.get("account_id") or "")
        event_id = str(frame.get("event_id") or "")
        try:
            account = await self._require_account(account_id)
        except CalendarAccountNotFoundError as exc:
            return self._err(frame, str(exc), 404)
        try:
            self._require_access(account, conn.user_ctx)
        except CalendarPermissionError as exc:
            return self._err(frame, str(exc), 403)
        runtime = self._runtimes.get(account_id)
        evt: CalendarEvent | None
        if runtime is None:
            row = await self._storage.get(
                _EVENTS_COLLECTION, self._event_row_id(account_id, event_id)
            )
            evt = self._event_row_to_event(row) if row is not None else None
        else:
            try:
                evt = await runtime.backend.get_event(account.calendar_id, event_id)
            except CalendarBackendError as exc:
                return self._err(frame, str(exc), 500)
        if evt is None:
            return self._err(frame, "Event not found", 404)
        return {
            "type": "calendar.events.get.result",
            "ref": frame.get("id"),
            "event": evt.to_dict(),
        }

    async def _ws_events_create(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        account_id = str(frame.get("account_id") or "")
        try:
            account = await self._require_account(account_id)
        except CalendarAccountNotFoundError as exc:
            return self._err(frame, str(exc), 404)
        try:
            self._require_access(account, conn.user_ctx)
        except CalendarPermissionError as exc:
            return self._err(frame, str(exc), 403)
        request, build_err = self._build_create_request(dict(frame.get("event") or {}), account)
        if build_err is not None:
            return self._err(frame, build_err, 400)
        try:
            evt = await self.create_event(account_id, request, conn.user_ctx)
        except CalendarBackendError as exc:
            return self._err(frame, str(exc), 500)
        return {
            "type": "calendar.events.create.result",
            "ref": frame.get("id"),
            "event": evt.to_dict(),
        }

    async def _ws_events_update(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        account_id = str(frame.get("account_id") or "")
        event_id = str(frame.get("event_id") or "")
        try:
            account = await self._require_account(account_id)
        except CalendarAccountNotFoundError as exc:
            return self._err(frame, str(exc), 404)
        try:
            self._require_access(account, conn.user_ctx)
        except CalendarPermissionError as exc:
            return self._err(frame, str(exc), 403)
        request, build_err = self._build_create_request(dict(frame.get("event") or {}), account)
        if build_err is not None:
            return self._err(frame, build_err, 400)
        if_match_etag = str(frame.get("if_match_etag") or "")
        if not if_match_etag:
            return self._err(
                frame,
                "if_match_etag is required — read the event first and pass its etag",
                400,
            )
        try:
            evt = await self.update_event(
                account_id,
                event_id,
                request,
                conn.user_ctx,
                if_match_etag=if_match_etag,
            )
        except CalendarBackendConflictError:
            return self._err(frame, "etag mismatch — refresh and retry", 409)
        except ValueError as exc:
            return self._err(frame, str(exc), 400)
        except CalendarBackendError as exc:
            return self._err(frame, str(exc), 500)
        return {
            "type": "calendar.events.update.result",
            "ref": frame.get("id"),
            "event": evt.to_dict(),
        }

    async def _ws_events_delete(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        account_id = str(frame.get("account_id") or "")
        event_id = str(frame.get("event_id") or "")
        try:
            account = await self._require_account(account_id)
        except CalendarAccountNotFoundError as exc:
            return self._err(frame, str(exc), 404)
        try:
            self._require_access(account, conn.user_ctx)
        except CalendarPermissionError as exc:
            return self._err(frame, str(exc), 403)
        send_cancellations = bool(frame.get("send_cancellations", False))
        try:
            await self.delete_event(
                account_id,
                event_id,
                conn.user_ctx,
                send_cancellations=send_cancellations,
            )
        except CalendarBackendError as exc:
            return self._err(frame, str(exc), 500)
        return {
            "type": "calendar.events.delete.result",
            "ref": frame.get("id"),
            "status": "ok",
        }

    async def _ws_freebusy_get(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        try:
            time_min = datetime.fromisoformat(str(frame.get("time_min", "")))
            time_max = datetime.fromisoformat(str(frame.get("time_max", "")))
        except ValueError as exc:
            return self._err(frame, f"invalid time range: {exc}", 400)
        try:
            blocks = await self.free_busy(
                frame.get("account_id"),
                time_min,
                time_max,
                conn.user_ctx,
            )
        except CalendarPermissionError as exc:
            return self._err(frame, str(exc), 403)
        except CalendarAccountNotFoundError as exc:
            return self._err(frame, str(exc), 404)
        return {
            "type": "calendar.freebusy.get.result",
            "ref": frame.get("id"),
            "blocks": [
                {
                    "calendar_id": b.calendar_id,
                    "start": b.start.isoformat(),
                    "end": b.end.isoformat(),
                }
                for b in blocks
            ],
        }

    async def _ws_find_free_time(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        try:
            time_min = datetime.fromisoformat(str(frame.get("time_min", "")))
            time_max = datetime.fromisoformat(str(frame.get("time_max", "")))
            duration_minutes = int(frame.get("duration_minutes", 0))
        except (ValueError, TypeError) as exc:
            return self._err(frame, f"invalid args: {exc}", 400)
        attendees = frame.get("attendee_emails") or None
        if attendees is not None and not isinstance(attendees, list):
            return self._err(frame, "attendee_emails must be a list", 400)
        try:
            result = await self.find_free_time(
                frame.get("account_id"),
                time_min,
                time_max,
                duration_minutes,
                conn.user_ctx,
                respect_working_hours=bool(frame.get("respect_working_hours", True)),
                max_results=int(frame.get("max_results", 5) or 5),
                attendee_emails=(
                    [str(a) for a in attendees] if isinstance(attendees, list) else None
                ),
            )
        except ValueError as exc:
            return self._err(frame, str(exc), 400)
        except CalendarPermissionError as exc:
            return self._err(frame, str(exc), 403)
        except CalendarAccountNotFoundError as exc:
            return self._err(frame, str(exc), 404)
        return {
            "type": "calendar.find_free_time.result",
            "ref": frame.get("id"),
            "slots": [
                {
                    "start": s.start.isoformat(),
                    "end": s.end.isoformat(),
                    "slot_duration_minutes": s.slot_duration_minutes,
                    "requested_duration_minutes": s.requested_duration_minutes,
                }
                for s in result.slots
            ],
            "warnings": list(result.warnings),
        }

    async def _ws_backends_list(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        backends = []
        for name, cls in CalendarBackend.registered_backends().items():
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
            actions = []
            try:
                probe = cls()
                raw_actions = probe.backend_actions() if hasattr(probe, "backend_actions") else []
                actions = [
                    {
                        "key": a.key,
                        "label": a.label,
                        "description": a.description,
                        "backend_action": True,
                        "backend": name,
                        "confirm": a.confirm,
                        "required_role": a.required_role,
                        "hidden": a.hidden,
                    }
                    for a in raw_actions
                ]
            except Exception:
                actions = []
            backends.append(
                {
                    "name": name,
                    "display_name": (cls.display_name or name.replace("_", " ").title()),
                    "config_params": params,
                    "actions": actions,
                }
            )
        return {
            "type": "calendar.backends.list.result",
            "ref": frame.get("id"),
            "backends": backends,
        }


# ── Internal sentinel used by aggregator to thread per-account
# failures through ``asyncio.gather`` results without losing types. The
# helper class lives at module scope so dataclass type-narrowing works
# at runtime; consumers of ``list_events`` never see this type.


@dataclass
class _AggregateFailure:
    account_name: str
    error: str


def _calendar_service_satisfies_provider(svc: CalendarService) -> CalendarProvider:
    """Static structural-conformance hint.

    Mypy / type-checkers verify that a ``CalendarService`` instance
    satisfies the ``CalendarProvider`` protocol — if a method is
    renamed, removed, or its signature drifts, this returns a typing
    error at the file level rather than at every call site. Pure
    type-checker construct; never invoked at runtime.
    """
    return svc
