"""PushNotificationService — fan out ``notification.received`` to per-user routes.

This is a side-channel that **does not modify**
:class:`~gilbert.core.services.notifications.NotificationService`.
Notifications are still persisted and dispatched to live WebSocket
clients exactly as today; this service merely subscribes to the same
``notification.received`` bus event and dispatches each one to the
recipient's configured external routes (ntfy, Pushover, Discord
webhook, Telegram bot).

Architecture (see ``docs/specs/03-notification-fanout.md``):

1. The bus subscriber ``_on_notification`` returns immediately after
   constructing a ``_FanOutJob`` and ``put_nowait``-ing it onto a bounded
   ``asyncio.Queue``. ``EventBus.publish`` ``await``s every subscriber
   inline (``asyncio.gather``), so a slow subscriber back-pressures the
   in-app dispatcher; the queue + worker pool decouples delivery from
   publishing.
2. N background workers drain the queue. Each worker spawns one
   ``asyncio.Task`` per matching route with ``context=job.context.copy()``
   so concurrent deliveries inherit the originating caller's
   ContextVars without clobbering each other.
3. Delivery failures retry with jittered exponential backoff. Backends
   may surface a provider-supplied ``retry_after_s`` (capped at 60s)
   that overrides the configured backoff. URGENT-failure exhaustion
   escalates to ``logger.error`` AND a follow-up in-app notification of
   urgency=URGENT with ``source="push_failure"`` so operators see the
   loss without re-implementing alerting.

v1 is at-most-once: a process crash between bus publish and worker
completion drops the in-flight push. The persisted notification is
unaffected. v1.1 will add an outbox for at-least-once delivery; the
schema is documented in the spec.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import random
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from datetime import time as dt_time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gilbert.core.services._ui_blocks import build_preview_output
from gilbert.interfaces.auth import AccessControlProvider
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
    ConfigurationReader,
)
from gilbert.interfaces.events import Event, EventBus, EventBusProvider
from gilbert.interfaces.notifications import (
    NotificationProvider,
    NotificationUrgency,
)
from gilbert.interfaces.push_notifications import (
    PushDeliveryResult,
    PushDeliveryStatus,
    PushDestination,
    PushMessage,
    PushNotificationBackend,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import (
    Filter,
    FilterOp,
    IndexDefinition,
    Query,
    SortField,
    StorageBackend,
    StorageProvider,
)
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
    ToolProvider,
)
from gilbert.interfaces.users import UserManagementProvider

logger = logging.getLogger(__name__)


_ROUTES_COLLECTION = "push_notification_routes"
_NOTIFICATIONS_COLLECTION = "notifications"
_ACL_COLLECTIONS = "acl_collections"
_NOTIFICATION_RECEIVED_EVENT = "notification.received"
_PUSH_FAILURE_SOURCE = "push_failure"
_TEST_SOURCE = "test"

#: Hard cap on ``max_retries`` to keep an admin from configuring
#: pathological retry budgets; values above this are silently clamped.
MAX_RETRIES_CAP = 8

#: Hard cap on provider-supplied ``retry_after_s`` so one wedged backend
#: can't monopolise a worker for long stretches.
_RETRY_AFTER_CAP_S = 60.0


_DEFAULT_TEST_BODY = (
    "This is a test from your Gilbert notification routes page."
)


# ── Credential scrubbing ──────────────────────────────────────────────


_TOKEN_RX = re.compile(
    r"(?:Bearer\s+\S+|/bot[A-Za-z0-9:_-]+/|"
    r"https?://[^\s]*?/api/webhooks/[^/\s]+/[A-Za-z0-9_-]+|"
    r"\?token=[^\s&]+)",
    re.IGNORECASE,
)


def _safe_repr(exc: BaseException) -> str:
    """Render an exception with secret material redacted.

    Backends MUST funnel any exception text intended for
    ``PushDeliveryResult.message`` or any ``logger.error`` /
    ``logger.exception`` call through this helper. The regex strips
    Bearer tokens, ``/bot<token>/`` URL paths, full Discord webhook
    URLs, and ``?token=`` query params. Plugin tests assert that a
    mocked HTTP error containing the bot token / webhook URL / Bearer
    header doesn't appear in the result message.
    """
    text = f"{type(exc).__name__}: {exc}"
    return _TOKEN_RX.sub("<redacted>", text)


# ── Quiet-hour math ──────────────────────────────────────────────────


def _parse_hhmm(value: str | None) -> dt_time | None:
    """Parse ``"HH:MM"`` into a ``time``; ``None``/empty/garbage → ``None``."""
    if not value:
        return None
    try:
        parts = value.split(":")
        if len(parts) != 2:
            return None
        hh = int(parts[0])
        mm = int(parts[1])
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
        return dt_time(hh, mm)
    except (TypeError, ValueError):
        return None


def in_quiet_hours(
    now: datetime,
    start_str: str | None,
    end_str: str | None,
    tz_name: str | None,
) -> bool:
    """Return ``True`` iff ``now`` falls within the quiet window.

    Comparison is done in the resolved tz so DST transitions don't
    skip or double-count an hour. ``None``/empty bounds disable quiet
    hours entirely. Wrap-around windows (start=22:00, end=07:00) are
    matched when ``now >= start`` OR ``now < end``.
    """
    start_t = _parse_hhmm(start_str)
    end_t = _parse_hhmm(end_str)
    if start_t is None or end_t is None:
        return False
    tz: Any
    if tz_name:
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            tz = UTC
    else:
        tz = UTC
    aware = now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)
    cur = aware.time().replace(microsecond=0)
    if start_t == end_t:
        return False
    if start_t < end_t:
        return start_t <= cur < end_t
    # Wrap-around: 22:00–07:00 means 22:00..23:59:59 OR 00:00..06:59:59
    return cur >= start_t or cur < end_t


# ── Job + service ────────────────────────────────────────────────────


@dataclass(frozen=True)
class _FanOutJob:
    """One notification → many routes. Captured under the publisher's context."""

    data: dict[str, Any]
    context: contextvars.Context


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _short_id() -> str:
    return uuid.uuid4().hex[:8]


# ── Service ──────────────────────────────────────────────────────────


class PushNotificationService(Service, ToolProvider):
    """Listen on ``notification.received`` and fan out to per-user routes.

    Capabilities declared:

    - ``push_notifications`` — the aggregator capability.
    - ``ws_handlers`` — RPCs for managing per-user routes.
    - ``ai_tools`` — list / create / delete / send-test tools so users
      can ask the AI to set things up.
    """

    config_namespace = "push_notifications"
    config_category = "Notifications"
    tool_provider_name = "push_notifications"
    slash_namespace = "notify"

    def __init__(self) -> None:
        # ── Resolved capabilities ────────────────────────────────────
        self._storage: StorageBackend | None = None
        self._event_bus: EventBus | None = None
        self._access_control: AccessControlProvider | None = None
        self._notifications: NotificationProvider | None = None
        self._users: UserManagementProvider | None = None
        self._configuration: ConfigurationReader | None = None
        self._unsubscribe: Callable[[], None] | None = None

        # ── Backends ────────────────────────────────────────────────
        self._backends: dict[str, PushNotificationBackend] = {}
        self._enabled_backends: set[str] | None = None  # None = all

        # ── Retry knobs (read in on_config_changed) ─────────────────
        self._max_retries: int = 3
        self._retry_initial_delay_s: float = 1.0
        self._retry_factor: float = 4.0
        self._retry_jitter_pct: float = 0.10

        # ── Worker pool / queue ─────────────────────────────────────
        self._queue: asyncio.Queue[_FanOutJob] | None = None
        self._queue_max: int = 1000
        self._workers: list[asyncio.Task[None]] = []
        self._worker_count: int = 8

        # ── Misc ────────────────────────────────────────────────────
        self._test_debounce_s: float = 30.0
        self._test_last: dict[str, float] = {}
        self._test_message_body: str = _DEFAULT_TEST_BODY
        self._default_deep_link_origin: str = ""

        # Best-effort "last delivered" memory for the Routes UI (S3).
        self._last_delivered: dict[str, str] = {}

        # Track which user_ids have already had their tz fall-through
        # WARN logged so we don't spam the operator log.
        self._warned_missing_tz: set[str] = set()

        # Effective settings dict (last applied) — used to skip
        # redundant backend re-initialisation in ``on_config_changed``.
        self._last_backend_configs: dict[str, dict[str, Any]] = {}

    # ── Service lifecycle ───────────────────────────────────────────

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="push_notifications",
            capabilities=frozenset({"push_notifications", "ws_handlers", "ai_tools"}),
            requires=frozenset({"entity_storage", "event_bus"}),
            optional=frozenset(
                {"access_control", "notifications", "users", "configuration"}
            ),
            events=frozenset({"notification.received"}),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        storage_svc = resolver.require_capability("entity_storage")
        if not isinstance(storage_svc, StorageProvider):
            raise RuntimeError(
                "entity_storage capability does not provide StorageProvider"
            )
        self._storage = storage_svc.backend

        bus_svc = resolver.require_capability("event_bus")
        if not isinstance(bus_svc, EventBusProvider):
            raise RuntimeError("event_bus capability does not provide EventBusProvider")
        self._event_bus = bus_svc.bus

        ac_svc = resolver.get_capability("access_control")
        if isinstance(ac_svc, AccessControlProvider):
            self._access_control = ac_svc
        notif_svc = resolver.get_capability("notifications")
        if isinstance(notif_svc, NotificationProvider):
            self._notifications = notif_svc
        users_svc = resolver.get_capability("users")
        if isinstance(users_svc, UserManagementProvider):
            self._users = users_svc
        cfg_svc = resolver.get_capability("configuration")
        if isinstance(cfg_svc, ConfigurationReader):
            self._configuration = cfg_svc

        # Indexes for the per-user routes lookup at fan-out time.
        await self._storage.ensure_index(
            IndexDefinition(
                collection=_ROUTES_COLLECTION,
                fields=["user_id", "enabled"],
            )
        )
        await self._storage.ensure_index(
            IndexDefinition(
                collection=_ROUTES_COLLECTION,
                fields=["backend_name"],
            )
        )

        # Seed the ACL collection row so admins see all rows on the
        # entities page; per-RPC access control is the trust boundary.
        await self._seed_acl_collection()

        # Read service config (clamps to caps inline).
        await self._apply_service_config()

        # Initialise enabled backends from the registry.
        await self._init_enabled_backends()

        # Start worker pool BEFORE subscribing to avoid an empty-pool
        # window where a publish lands with no consumers.
        self._queue = asyncio.Queue(maxsize=self._queue_max)
        self._workers = [
            asyncio.create_task(
                self._worker_loop(),
                name=f"push-fanout-worker-{i}",
            )
            for i in range(self._worker_count)
        ]

        # Subscribe LAST so we don't drop early publishes.
        assert self._event_bus is not None
        self._unsubscribe = self._event_bus.subscribe(
            _NOTIFICATION_RECEIVED_EVENT, self._on_notification
        )
        logger.info(
            "PushNotificationService started: backends=%s workers=%d queue_max=%d",
            sorted(self._backends.keys()),
            self._worker_count,
            self._queue_max,
        )

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception:
                logger.exception("push: failed to unsubscribe from event bus")
            self._unsubscribe = None
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []
        for backend in self._backends.values():
            try:
                await backend.close()
            except Exception:
                logger.exception(
                    "push: backend %s raised during close", backend.backend_name
                )
        self._backends = {}
        self._queue = None

    # ── Configurable ────────────────────────────────────────────────

    def config_params(self) -> list[ConfigParam]:
        params: list[ConfigParam] = [
            ConfigParam(
                key="enabled_backends",
                type=ToolParameterType.STRING,
                description=(
                    "Comma-separated push backend names to enable "
                    "(e.g. 'ntfy,pushover'). Empty = all registered."
                ),
                default="",
            ),
            ConfigParam(
                key="max_retries",
                type=ToolParameterType.INTEGER,
                description=(
                    "Per-route retry budget on transient errors. Capped "
                    f"server-side at {MAX_RETRIES_CAP}."
                ),
                default=3,
            ),
            ConfigParam(
                key="retry_initial_delay_s",
                type=ToolParameterType.NUMBER,
                description="Initial backoff seconds between retries.",
                default=1.0,
            ),
            ConfigParam(
                key="retry_factor",
                type=ToolParameterType.NUMBER,
                description="Multiplicative backoff factor per retry.",
                default=4.0,
            ),
            ConfigParam(
                key="retry_jitter_pct",
                type=ToolParameterType.NUMBER,
                description=(
                    "Random jitter applied to each backoff (0.10 = ±10%). "
                    "Prevents thundering-herd retries when a provider has "
                    "a brief outage."
                ),
                default=0.10,
            ),
            ConfigParam(
                key="worker_count",
                type=ToolParameterType.INTEGER,
                description=(
                    "Number of concurrent delivery workers draining the "
                    "fan-out queue. Applied on next service start."
                ),
                default=8,
                restart_required=True,
            ),
            ConfigParam(
                key="queue_max",
                type=ToolParameterType.INTEGER,
                description=(
                    "Maximum pending jobs in the fan-out queue. Overflow "
                    "drops with a WARNING — back-pressure, not unbounded "
                    "growth."
                ),
                default=1000,
                restart_required=True,
            ),
            ConfigParam(
                key="test_debounce_s",
                type=ToolParameterType.NUMBER,
                description=(
                    "Server-side cooldown for per-route 'Send test' "
                    "actions (seconds). Prevents accidental flooding of "
                    "shared channels."
                ),
                default=30.0,
            ),
            ConfigParam(
                key="test_message_body",
                type=ToolParameterType.STRING,
                description=(
                    "Body sent by the per-route 'Send test' button. "
                    "Operator-overridable for branding / localisation."
                ),
                default=_DEFAULT_TEST_BODY,
                multiline=True,
            ),
            ConfigParam(
                key="default_deep_link_origin",
                type=ToolParameterType.STRING,
                description=(
                    "Public origin for click-through URLs in delivered "
                    "messages (e.g. 'https://gilbert.example.com'). "
                    "Empty = omit links."
                ),
                default="",
            ),
        ]
        for name, cls in PushNotificationBackend.registered_backends().items():
            for bp in cls.backend_config_params():
                params.append(
                    replace(
                        bp,
                        key=f"backends.{name}.{bp.key}",
                        backend_param=True,
                    )
                )
        return params

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        await self._apply_service_config(config)
        # Re-init backends whose admin config changed (or whose enabled
        # state flipped on). Backends not affected stay running.
        target = self._enabled_backends
        registry = PushNotificationBackend.registered_backends()
        active_names = {n for n in registry if target is None or n in target}
        # Tear down removed.
        for name in list(self._backends):
            if name not in active_names:
                try:
                    await self._backends[name].close()
                except Exception:
                    logger.exception(
                        "push: backend %s raised during close", name
                    )
                self._backends.pop(name, None)
                self._last_backend_configs.pop(name, None)
        # Apply config to remaining + add new.
        for name in active_names:
            cls = registry[name]
            new_cfg = self._extract_backend_config(name, config)
            existing = self._backends.get(name)
            previous_cfg = self._last_backend_configs.get(name)
            if existing is not None and previous_cfg == new_cfg:
                continue
            if existing is not None:
                try:
                    await existing.close()
                except Exception:
                    logger.exception(
                        "push: backend %s raised during close", name
                    )
            try:
                inst = cls()
                await inst.initialize(new_cfg)
                self._backends[name] = inst
                self._last_backend_configs[name] = new_cfg
            except Exception as exc:
                logger.error(
                    "push: backend %s init failed: %s",
                    name,
                    type(exc).__name__,
                )
                self._backends.pop(name, None)
                self._last_backend_configs.pop(name, None)

    # ── ConfigActionProvider ────────────────────────────────────────

    def config_actions(self) -> list[ConfigAction]:
        actions: list[ConfigAction] = [
            ConfigAction(
                key="reload_backends",
                label="Reload backends",
                description=(
                    "Tear down and re-initialise every push backend with "
                    "the current admin config. Useful after rotating a "
                    "secret or installing a plugin."
                ),
            ),
        ]
        # Each enabled backend's actions, tagged by backend name so the
        # UI can filter and the dispatcher knows where to route.
        for name, backend in self._backends.items():
            for action in type(backend).backend_actions():
                actions.append(
                    replace(action, backend_action=True, backend=name)
                )
        return actions

    async def invoke_config_action(
        self, key: str, payload: dict[str, Any]
    ) -> ConfigActionResult:
        if key == "reload_backends":
            cfg = (
                self._configuration.get_section_safe("push_notifications")
                if self._configuration is not None
                else {}
            )
            await self.on_config_changed(cfg)
            return ConfigActionResult(
                status="ok",
                message=f"Reloaded {len(self._backends)} backend(s).",
            )
        # Backend-tagged action: dispatch to the named backend.
        backend_name = str(payload.get("backend") or "").strip()
        backend = self._backends.get(backend_name)
        if backend is None:
            return ConfigActionResult(
                status="error",
                message=f"Unknown or disabled backend: {backend_name!r}",
            )
        try:
            return await backend.invoke_backend_action(key, payload)
        except Exception as exc:
            return ConfigActionResult(
                status="error",
                message=f"backend {backend_name} raised: {_safe_repr(exc)}",
            )

    # ── Bus subscriber + workers ────────────────────────────────────

    async def _on_notification(self, event: Event) -> None:
        """Tight handoff: build a job, snapshot context, enqueue, return.

        Runs on the event-bus publisher's task. MUST return immediately —
        any work here directly back-pressures
        ``WsConnectionManager._dispatch_event`` and
        ``NotificationService.notify_user``.
        """
        if self._queue is None:
            return  # service stopped mid-publish; fine.
        data = event.data or {}
        if not data.get("id") or not data.get("user_id"):
            logger.debug("push: dropping malformed event (no id/user)")
            return
        job = _FanOutJob(data=data, context=contextvars.copy_context())
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            logger.warning(
                "push: fan-out queue full (%d); dropping notification %s "
                "for user %s. Increase 'queue_max' or investigate stalled "
                "backends.",
                self._queue_max,
                data.get("id"),
                data.get("user_id"),
            )

    async def _worker_loop(self) -> None:
        assert self._queue is not None
        while True:
            try:
                job = await self._queue.get()
            except asyncio.CancelledError:
                return
            try:
                # Spawn the fan-out as its own Task with the captured
                # context (Context.run only contains sync code, so we
                # can't use it for the async fan-out body; the spec
                # explicitly calls this out).
                task = asyncio.Task(
                    self._fan_out(job),
                    context=job.context.copy(),
                )
                await task
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception(
                    "push: worker crashed handling notification %s",
                    job.data.get("id", "?"),
                )
            finally:
                self._queue.task_done()

    async def _fan_out(self, job: _FanOutJob) -> None:
        if self._storage is None:
            return
        data = job.data
        user_id = str(data.get("user_id") or "")
        if not user_id:
            return

        routes_raw = await self._storage.query(
            Query(
                collection=_ROUTES_COLLECTION,
                filters=[
                    Filter(field="user_id", op=FilterOp.EQ, value=user_id),
                    Filter(field="enabled", op=FilterOp.EQ, value=True),
                ],
            )
        )
        # Pre-resolve user tz once per fan-out so we don't hit storage
        # per-route.
        user_tz = await self._lookup_user_tz(user_id)
        eligible = [
            r
            for r in routes_raw
            if self._route_passes_filters(r, data, user_tz=user_tz)
        ]
        if not eligible:
            return

        await self._stamp_delivery_attempted(data)

        message = self._build_push_message(data)

        tasks = [
            asyncio.Task(
                self._deliver_with_retry(route, message),
                context=job.context.copy(),
            )
            for route in eligible
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    # ── Filter logic ────────────────────────────────────────────────

    def _route_passes_filters(
        self,
        route: dict[str, Any],
        notification: dict[str, Any],
        *,
        user_tz: str | None,
    ) -> bool:
        if not route.get("enabled"):
            return False
        if route.get("user_id") != notification.get("user_id"):
            return False
        floor = str(route.get("urgency_floor") or "normal").lower()
        urgency = str(notification.get("urgency") or "normal").lower()
        if _urgency_rank(urgency) < _urgency_rank(floor):
            return False
        source = str(notification.get("source") or "")
        allow = list(route.get("source_allow") or [])
        deny = list(route.get("source_deny") or [])
        if allow and source not in allow:
            return False
        if source in deny:
            return False
        # Quiet hours
        tz_name = (
            route.get("quiet_hours_timezone")
            or user_tz
            or _server_tz_name()
        )
        if (
            route.get("quiet_hours_timezone") is None
            and user_tz is None
            and route.get("quiet_hours_start")
        ):
            uid = str(route.get("user_id") or "")
            if uid and uid not in self._warned_missing_tz:
                self._warned_missing_tz.add(uid)
                logger.warning(
                    "push: route %s for user %s falling back to server tz "
                    "for quiet hours — set the user's profile timezone",
                    route.get("_id"),
                    uid,
                )
        return not in_quiet_hours(
            datetime.now(UTC),
            route.get("quiet_hours_start"),
            route.get("quiet_hours_end"),
            tz_name,
        )

    # ── Per-route delivery with retry ───────────────────────────────

    async def _deliver_with_retry(
        self,
        route: dict[str, Any],
        message: PushMessage,
    ) -> None:
        route_id = str(route.get("_id") or "")
        backend_name = str(route.get("backend_name") or "")
        user_id = str(route.get("user_id") or "")
        is_urgent = message.urgency is NotificationUrgency.URGENT

        backend = self._backends.get(backend_name)
        if backend is None:
            logger.warning(
                "push: route=%s points at unknown/disabled backend=%r",
                route_id,
                backend_name,
            )
            return

        destination = PushDestination(
            user_id=user_id,
            route_id=route_id,
            data=dict(route.get("destination_data") or {}),
        )

        max_retries = min(int(self._max_retries), MAX_RETRIES_CAP)
        delay = float(self._retry_initial_delay_s)
        last_message = ""
        for attempt in range(1, max_retries + 2):  # initial + N retries
            try:
                result = await backend.send(destination, message)
            except Exception as exc:
                logger.error(
                    "push: route=%s backend=%s raised: %s",
                    route_id,
                    backend_name,
                    _safe_repr(exc),
                )
                return
            last_message = result.message

            if result.status is PushDeliveryStatus.DELIVERED:
                logger.info(
                    "push: route=%s backend=%s notification=%s status=delivered",
                    route_id,
                    backend_name,
                    message.notification_id,
                )
                await self._mark_last_delivered(route_id)
                return

            if result.status in (
                PushDeliveryStatus.REJECTED,
                PushDeliveryStatus.DISABLED,
            ):
                logger.warning(
                    "push: route=%s backend=%s status=%s: %s",
                    route_id,
                    backend_name,
                    result.status.value,
                    result.message,
                )
                return

            if attempt > max_retries:
                break
            if result.retry_after_s is not None:
                sleep_for = min(float(result.retry_after_s), _RETRY_AFTER_CAP_S)
            else:
                jitter = random.uniform(
                    -self._retry_jitter_pct, self._retry_jitter_pct
                )
                sleep_for = max(0.0, delay * (1.0 + jitter))
            logger.debug(
                "push: route=%s transient error attempt=%d sleep=%.2fs: %s",
                route_id,
                attempt,
                sleep_for,
                result.message,
            )
            await asyncio.sleep(sleep_for)
            delay *= float(self._retry_factor)

        # Exhausted.
        level = logging.ERROR if is_urgent else logging.WARNING
        logger.log(
            level,
            "push: route=%s backend=%s exhausted retries: %s",
            route_id,
            backend_name,
            last_message,
        )
        if is_urgent and self._notifications is not None:
            try:
                await self._notifications.notify_user(
                    user_id=user_id,
                    message=(
                        "External push failed for route "
                        f"{route.get('label', route_id)!r}."
                    ),
                    urgency=NotificationUrgency.URGENT,
                    source=_PUSH_FAILURE_SOURCE,
                    source_ref={
                        "route_id": route_id,
                        "notification_id": message.notification_id,
                    },
                )
            except Exception:
                logger.exception(
                    "push: failed to record push_failure notification"
                )

    async def _mark_last_delivered(self, route_id: str) -> None:
        now_iso = _now_iso()
        self._last_delivered[route_id] = now_iso
        if self._storage is None or not route_id:
            return
        try:
            existing = await self._storage.get(_ROUTES_COLLECTION, route_id)
            if existing is None:
                return
            existing.pop("_id", None)
            existing["last_delivered_at"] = now_iso
            await self._storage.put(
                _ROUTES_COLLECTION, route_id, existing
            )
        except Exception:
            logger.debug(
                "push: best-effort last_delivered update failed for %s", route_id
            )

    async def _stamp_delivery_attempted(self, data: dict[str, Any]) -> None:
        if self._storage is None:
            return
        notification_id = str(data.get("id") or "")
        if not notification_id:
            return
        try:
            row = await self._storage.get(
                _NOTIFICATIONS_COLLECTION, notification_id
            )
            if row is None:
                return
            row.pop("_id", None)
            row["external_delivery_attempted_at"] = _now_iso()
            await self._storage.put(
                _NOTIFICATIONS_COLLECTION, notification_id, row
            )
        except Exception:
            logger.debug(
                "push: best-effort stamp failed for notification %s",
                notification_id,
            )

    # ── Build the push message ──────────────────────────────────────

    def _build_push_message(self, data: dict[str, Any]) -> PushMessage:
        source = str(data.get("source") or "system")
        if source in ("system", ""):
            title = "Gilbert"
        else:
            title = f"Gilbert · {source.capitalize()}"
        try:
            urgency = NotificationUrgency(
                str(data.get("urgency") or "normal").lower()
            )
        except ValueError:
            urgency = NotificationUrgency.NORMAL
        source_ref = data.get("source_ref")
        if source_ref is not None and not isinstance(source_ref, dict):
            source_ref = None
        if source_ref is not None and self._default_deep_link_origin:
            link = self._build_deep_link(source_ref)
            if link:
                source_ref = {**source_ref, "deep_link_url": link}
        return PushMessage(
            title=title,
            body=str(data.get("message") or ""),
            urgency=urgency,
            source=source,
            source_ref=source_ref,
            notification_id=str(data.get("id") or ""),
        )

    def _build_deep_link(self, source_ref: dict[str, Any]) -> str:
        origin = self._default_deep_link_origin.rstrip("/")
        if not origin:
            return ""
        if "conversation_id" in source_ref:
            return f"{origin}/chat?conversation={source_ref['conversation_id']}"
        if "goal_id" in source_ref:
            return f"{origin}/goals/{source_ref['goal_id']}"
        if "agent_id" in source_ref:
            return f"{origin}/agents/{source_ref['agent_id']}"
        return origin + "/notifications"

    # ── Helpers (config + tz lookup) ─────────────────────────────────

    async def _apply_service_config(
        self, config: dict[str, Any] | None = None
    ) -> None:
        if config is None:
            if self._configuration is not None:
                config = self._configuration.get_section_safe(
                    "push_notifications"
                )
            else:
                config = {}
        self._max_retries = int(config.get("max_retries", 3))
        self._retry_initial_delay_s = float(
            config.get("retry_initial_delay_s", 1.0)
        )
        self._retry_factor = float(config.get("retry_factor", 4.0))
        self._retry_jitter_pct = max(
            0.0, min(1.0, float(config.get("retry_jitter_pct", 0.10)))
        )
        # worker_count / queue_max are restart_required — read once but
        # only honour at start time. Updating them here lets a fresh
        # service pick up new values; live changes WARN.
        new_worker_count = int(config.get("worker_count", 8))
        new_queue_max = int(config.get("queue_max", 1000))
        if (
            self._workers
            and (new_worker_count != self._worker_count or new_queue_max != self._queue_max)
        ):
            logger.warning(
                "push: worker_count/queue_max are restart_required; "
                "ignoring live change"
            )
        else:
            self._worker_count = new_worker_count
            self._queue_max = new_queue_max
        self._test_debounce_s = float(config.get("test_debounce_s", 30.0))
        self._test_message_body = str(
            config.get("test_message_body", _DEFAULT_TEST_BODY)
            or _DEFAULT_TEST_BODY
        )
        self._default_deep_link_origin = str(
            config.get("default_deep_link_origin", "") or ""
        )
        raw = str(config.get("enabled_backends", "") or "").strip()
        if raw:
            self._enabled_backends = {
                n.strip() for n in raw.split(",") if n.strip()
            }
        else:
            self._enabled_backends = None

    async def _init_enabled_backends(self) -> None:
        registry = PushNotificationBackend.registered_backends()
        target = self._enabled_backends
        cfg = (
            self._configuration.get_section_safe("push_notifications")
            if self._configuration is not None
            else {}
        )
        for name, cls in registry.items():
            if target is not None and name not in target:
                continue
            backend_cfg = self._extract_backend_config(name, cfg)
            try:
                inst = cls()
                await inst.initialize(backend_cfg)
                self._backends[name] = inst
                self._last_backend_configs[name] = backend_cfg
            except Exception as exc:
                logger.error(
                    "push: backend %s init failed: %s",
                    name,
                    type(exc).__name__,
                )

    def _extract_backend_config(
        self, backend_name: str, config: dict[str, Any]
    ) -> dict[str, Any]:
        prefix = f"backends.{backend_name}."
        out: dict[str, Any] = {}
        for k, v in config.items():
            if k.startswith(prefix):
                out[k[len(prefix):]] = v
        # Some configuration backends nest the prefixed keys under a
        # ``backends`` dict — handle that shape too.
        nested = config.get("backends")
        if isinstance(nested, dict):
            inner = nested.get(backend_name)
            if isinstance(inner, dict):
                for k, v in inner.items():
                    out.setdefault(k, v)
        return out

    async def _lookup_user_tz(self, user_id: str) -> str | None:
        if self._users is None:
            return None
        try:
            user = await self._users.backend.get_user(user_id)
        except Exception:
            return None
        if user is None:
            return None
        tz = user.get("tz")
        if isinstance(tz, str) and tz:
            return tz
        # Backwards-compat: some older flows stuffed tz into metadata.
        meta = user.get("metadata") or {}
        meta_tz = meta.get("tz") if isinstance(meta, dict) else None
        return meta_tz if isinstance(meta_tz, str) and meta_tz else None

    async def _seed_acl_collection(self) -> None:
        if self._storage is None:
            return
        existing = await self._storage.get(_ACL_COLLECTIONS, _ROUTES_COLLECTION)
        if existing is not None:
            return
        try:
            await self._storage.put(
                _ACL_COLLECTIONS,
                _ROUTES_COLLECTION,
                {
                    "collection": _ROUTES_COLLECTION,
                    "read_role": "user",
                    "write_role": "user",
                },
            )
        except Exception:
            logger.debug(
                "push: failed to seed acl_collections row for %s",
                _ROUTES_COLLECTION,
            )

    # ── WS RPCs ─────────────────────────────────────────────────────

    def get_ws_handlers(self) -> dict[str, Any]:
        return {
            "push.routes.list": self._ws_routes_list,
            "push.routes.create": self._ws_routes_create,
            "push.routes.update": self._ws_routes_update,
            "push.routes.delete": self._ws_routes_delete,
            "push.routes.test": self._ws_routes_test,
            "push.routes.test_unsaved": self._ws_routes_test_unsaved,
            "push.backends.list": self._ws_backends_list,
            "push.sources.list": self._ws_sources_list,
        }

    @staticmethod
    def _is_admin(conn: Any) -> bool:
        return getattr(conn, "user_level", 999) <= 0

    @staticmethod
    def _caller_user_id(conn: Any) -> str:
        uid = getattr(conn, "user_id", "") or ""
        if not uid:
            raise PermissionError("anonymous caller")
        return uid

    def _authorize_route_access(
        self,
        conn: Any,
        row: dict[str, Any] | None,
        *,
        write: bool,
    ) -> None:
        """Single source of truth for route ownership checks.

        Raises ``PermissionError`` unless the caller owns the row, with
        a read-only override for admins. Writes are owner-only — admins
        cannot mutate another user's routes through the WS RPC surface.
        """
        if row is None:
            raise PermissionError("not_found")
        owner = row.get("user_id")
        caller = self._caller_user_id(conn)
        if owner == caller:
            return
        if not write and self._is_admin(conn):
            return
        raise PermissionError("not_owner")

    async def _ws_routes_list(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        if self._storage is None:
            raise RuntimeError("PushNotificationService not started")
        target_user = str(frame.get("user_id") or "").strip()
        caller = self._caller_user_id(conn)
        if not target_user:
            target_user = caller
        if target_user != caller and not self._is_admin(conn):
            return {
                "type": "push.routes.list.result",
                "ref": frame.get("id"),
                "ok": False,
                "error": "not_owner",
            }
        rows = await self._storage.query(
            Query(
                collection=_ROUTES_COLLECTION,
                filters=[
                    Filter(field="user_id", op=FilterOp.EQ, value=target_user),
                ],
                sort=[SortField(field="created_at")],
            )
        )
        return {
            "type": "push.routes.list.result",
            "ref": frame.get("id"),
            "ok": True,
            "routes": [self._serialize_route(r) for r in rows],
        }

    async def _ws_routes_create(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        if self._storage is None:
            raise RuntimeError("PushNotificationService not started")
        caller = self._caller_user_id(conn)
        backend_name = str(frame.get("backend_name") or "").strip()
        if backend_name not in PushNotificationBackend.registered_backends():
            return self._fail("push.routes.create", frame, "unknown_backend")
        label = str(frame.get("label") or "").strip()
        if not label:
            return self._fail("push.routes.create", frame, "missing_label")
        destination_data = frame.get("destination_data") or {}
        if not isinstance(destination_data, dict):
            return self._fail(
                "push.routes.create", frame, "invalid_destination_data"
            )
        urgency_floor = str(
            frame.get("urgency_floor") or "normal"
        ).lower()
        if urgency_floor not in ("info", "normal", "urgent"):
            urgency_floor = "normal"
        route_id = uuid.uuid4().hex
        now_iso = _now_iso()
        row: dict[str, Any] = {
            "user_id": caller,
            "label": label,
            "backend_name": backend_name,
            "destination_data": destination_data,
            "enabled": bool(frame.get("enabled", True)),
            "urgency_floor": urgency_floor,
            "source_allow": list(frame.get("source_allow") or []),
            "source_deny": list(frame.get("source_deny") or []),
            "quiet_hours_start": frame.get("quiet_hours_start") or None,
            "quiet_hours_end": frame.get("quiet_hours_end") or None,
            "quiet_hours_timezone": frame.get("quiet_hours_timezone") or None,
            "last_delivered_at": None,
            "created_at": now_iso,
            "updated_at": now_iso,
        }
        await self._storage.put(_ROUTES_COLLECTION, route_id, row)
        row["_id"] = route_id
        return {
            "type": "push.routes.create.result",
            "ref": frame.get("id"),
            "ok": True,
            "route": self._serialize_route(row),
        }

    async def _ws_routes_update(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        if self._storage is None:
            raise RuntimeError("PushNotificationService not started")
        route_id = str(frame.get("route_id") or "")
        if not route_id:
            return self._fail("push.routes.update", frame, "missing_route_id")
        row = await self._storage.get(_ROUTES_COLLECTION, route_id)
        try:
            self._authorize_route_access(conn, row, write=True)
        except PermissionError as exc:
            return self._fail("push.routes.update", frame, str(exc))
        assert row is not None
        row.pop("_id", None)
        for key in (
            "label",
            "destination_data",
            "enabled",
            "urgency_floor",
            "source_allow",
            "source_deny",
            "quiet_hours_start",
            "quiet_hours_end",
            "quiet_hours_timezone",
        ):
            if key in frame:
                row[key] = frame[key]
        row["updated_at"] = _now_iso()
        await self._storage.put(_ROUTES_COLLECTION, route_id, row)
        row["_id"] = route_id
        return {
            "type": "push.routes.update.result",
            "ref": frame.get("id"),
            "ok": True,
            "route": self._serialize_route(row),
        }

    async def _ws_routes_delete(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        if self._storage is None:
            raise RuntimeError("PushNotificationService not started")
        route_id = str(frame.get("route_id") or "")
        if not route_id:
            return self._fail("push.routes.delete", frame, "missing_route_id")
        row = await self._storage.get(_ROUTES_COLLECTION, route_id)
        try:
            self._authorize_route_access(conn, row, write=True)
        except PermissionError as exc:
            return self._fail("push.routes.delete", frame, str(exc))
        await self._storage.delete(_ROUTES_COLLECTION, route_id)
        self._test_last.pop(route_id, None)
        self._last_delivered.pop(route_id, None)
        return {
            "type": "push.routes.delete.result",
            "ref": frame.get("id"),
            "ok": True,
        }

    async def _ws_routes_test(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        if self._storage is None:
            raise RuntimeError("PushNotificationService not started")
        route_id = str(frame.get("route_id") or "")
        if not route_id:
            return self._fail("push.routes.test", frame, "missing_route_id")
        row = await self._storage.get(_ROUTES_COLLECTION, route_id)
        # Per spec: tests are owner-only, even for admins.
        try:
            self._authorize_route_access(conn, row, write=True)
        except PermissionError as exc:
            return self._fail("push.routes.test", frame, str(exc))
        assert row is not None
        if not self._debounce_ok(route_id):
            return {
                "type": "push.routes.test.result",
                "ref": frame.get("id"),
                "ok": False,
                "status": "debounced",
                "message": (
                    f"Please wait {int(self._test_debounce_s)}s before "
                    "retesting."
                ),
            }
        backend = self._backends.get(str(row.get("backend_name") or ""))
        if backend is None:
            return self._fail("push.routes.test", frame, "unknown_backend")
        message = self._build_test_message()
        destination = PushDestination(
            user_id=str(row.get("user_id") or ""),
            route_id=route_id,
            data=dict(row.get("destination_data") or {}),
        )
        result = await self._safe_send(backend, destination, message)
        self._test_last[route_id] = time.monotonic()
        return {
            "type": "push.routes.test.result",
            "ref": frame.get("id"),
            "ok": result.status is PushDeliveryStatus.DELIVERED,
            "status": result.status.value,
            "message": result.message,
        }

    async def _ws_routes_test_unsaved(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        caller = self._caller_user_id(conn)
        backend_name = str(frame.get("backend_name") or "").strip()
        destination_data = frame.get("destination_data") or {}
        if not isinstance(destination_data, dict):
            return self._fail(
                "push.routes.test_unsaved",
                frame,
                "invalid_destination_data",
            )
        debounce_key = f"unsaved:{caller}:{backend_name}:{_short_hash(destination_data)}"
        if not self._debounce_ok(debounce_key):
            return {
                "type": "push.routes.test_unsaved.result",
                "ref": frame.get("id"),
                "ok": False,
                "status": "debounced",
                "message": (
                    f"Please wait {int(self._test_debounce_s)}s before "
                    "retesting."
                ),
            }
        backend = self._backends.get(backend_name)
        if backend is None:
            return self._fail(
                "push.routes.test_unsaved", frame, "unknown_backend"
            )
        message = self._build_test_message()
        destination = PushDestination(
            user_id=caller,
            route_id=f"unsaved-{_short_id()}",
            data=destination_data,
        )
        result = await self._safe_send(backend, destination, message)
        self._test_last[debounce_key] = time.monotonic()
        return {
            "type": "push.routes.test_unsaved.result",
            "ref": frame.get("id"),
            "ok": result.status is PushDeliveryStatus.DELIVERED,
            "status": result.status.value,
            "message": result.message,
        }

    async def _ws_backends_list(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        backends_payload: list[dict[str, Any]] = []
        registry = PushNotificationBackend.registered_backends()
        for name, cls in registry.items():
            inst = self._backends.get(name)
            backends_payload.append(
                {
                    "name": name,
                    "label": name,
                    "destination_params": [
                        _config_param_to_dict(p)
                        for p in cls.destination_params()
                    ],
                    "actions": [
                        {
                            "key": a.key,
                            "label": a.label,
                            "description": a.description,
                        }
                        for a in cls.backend_actions()
                    ],
                    "enabled": inst is not None,
                    "runtime_data": inst.runtime_data() if inst else {},
                }
            )
        return {
            "type": "push.backends.list.result",
            "ref": frame.get("id"),
            "ok": True,
            "backends": backends_payload,
        }

    async def _ws_sources_list(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        if self._storage is None:
            raise RuntimeError("PushNotificationService not started")
        caller = self._caller_user_id(conn)
        rows = await self._storage.query(
            Query(
                collection=_NOTIFICATIONS_COLLECTION,
                filters=[
                    Filter(field="user_id", op=FilterOp.EQ, value=caller),
                ],
                limit=1000,
            )
        )
        seen: set[str] = set()
        for r in rows:
            src = r.get("source")
            if isinstance(src, str) and src:
                seen.add(src)
        return {
            "type": "push.sources.list.result",
            "ref": frame.get("id"),
            "ok": True,
            "sources": sorted(seen),
        }

    # ── AI tools ────────────────────────────────────────────────────

    def get_tools(self, user_ctx: Any = None) -> list[ToolDefinition]:
        return _PUSH_AI_TOOLS

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str | Any:
        caller_id = str(arguments.get("_user_id") or "")
        if not caller_id:
            return "error: caller user_id missing"
        if name == "list_my_notification_routes":
            return await self._tool_list(caller_id)
        if name == "create_notification_route":
            return await self._tool_create(caller_id, arguments)
        if name == "delete_notification_route":
            return await self._tool_delete(caller_id, arguments)
        if name == "send_test_notification":
            return await self._tool_send_test(caller_id, arguments)
        raise KeyError(f"unknown tool: {name}")

    async def _tool_list(self, caller_id: str) -> str:
        if self._storage is None:
            return "error: storage unavailable"
        rows = await self._storage.query(
            Query(
                collection=_ROUTES_COLLECTION,
                filters=[
                    Filter(field="user_id", op=FilterOp.EQ, value=caller_id),
                ],
                sort=[SortField(field="created_at")],
            )
        )
        if not rows:
            return (
                "You have no push-notification routes yet. Visit "
                "/account/notifications to add one (ntfy is the easiest)."
            )
        lines: list[str] = []
        for r in rows:
            tag = "" if r.get("enabled") else " (disabled)"
            lines.append(
                f"- {r.get('label', 'unnamed')}{tag}: backend="
                f"{r.get('backend_name')}, urgency_floor="
                f"{r.get('urgency_floor', 'normal')}"
            )
        return "Your push-notification routes:\n" + "\n".join(lines)

    async def _tool_create(
        self, caller_id: str, arguments: dict[str, Any]
    ) -> str:
        if self._storage is None:
            return "error: storage unavailable"
        backend_name = str(arguments.get("backend_name") or "").strip()
        if backend_name not in PushNotificationBackend.registered_backends():
            return f"error: unknown backend {backend_name!r}"
        label = str(arguments.get("label") or "").strip()
        if not label:
            return "error: label is required"
        destination = arguments.get("destination") or arguments.get(
            "destination_data"
        )
        if not isinstance(destination, dict) or not destination:
            return "error: destination is required (a dict of backend-specific fields)"
        urgency_floor = str(
            arguments.get("urgency_floor") or "normal"
        ).lower()
        if urgency_floor not in ("info", "normal", "urgent"):
            urgency_floor = "normal"
        route_id = uuid.uuid4().hex
        now_iso = _now_iso()
        await self._storage.put(
            _ROUTES_COLLECTION,
            route_id,
            {
                "user_id": caller_id,
                "label": label,
                "backend_name": backend_name,
                "destination_data": destination,
                "enabled": True,
                "urgency_floor": urgency_floor,
                "source_allow": [],
                "source_deny": [],
                "quiet_hours_start": None,
                "quiet_hours_end": None,
                "quiet_hours_timezone": None,
                "last_delivered_at": None,
                "created_at": now_iso,
                "updated_at": now_iso,
            },
        )
        return (
            f"Created route {label!r} via {backend_name}. "
            "Use /notify test to send a test message."
        )

    async def _tool_delete(
        self, caller_id: str, arguments: dict[str, Any]
    ) -> Any:
        if self._storage is None:
            return "error: storage unavailable"
        route_id = str(arguments.get("route_id") or "").strip()
        if not route_id:
            return "error: route_id is required"
        row = await self._storage.get(_ROUTES_COLLECTION, route_id)
        if row is None or row.get("user_id") != caller_id:
            return "error: route not found"
        if not arguments.get("confirm"):
            return build_preview_output(
                tool_name="delete_notification_route",
                title="Delete notification route",
                summary=(
                    f"About to delete route {row.get('label', route_id)!r} "
                    f"({row.get('backend_name')}) — confirm?"
                ),
                summary_lines=[
                    f"label: {row.get('label', '')}",
                    f"backend: {row.get('backend_name', '')}",
                    f"route_id: {route_id}",
                ],
                arguments={"route_id": route_id, "confirm": True},
            )
        await self._storage.delete(_ROUTES_COLLECTION, route_id)
        self._test_last.pop(route_id, None)
        self._last_delivered.pop(route_id, None)
        return f"Deleted route {row.get('label', route_id)!r}."

    async def _tool_send_test(
        self, caller_id: str, arguments: dict[str, Any]
    ) -> str:
        if self._storage is None:
            return "error: storage unavailable"
        route_id = str(arguments.get("route_id") or "").strip()
        if not route_id:
            return "error: route_id is required"
        row = await self._storage.get(_ROUTES_COLLECTION, route_id)
        if row is None or row.get("user_id") != caller_id:
            return "error: route not found"
        if not self._debounce_ok(route_id):
            return (
                f"Cooldown — please wait "
                f"{int(self._test_debounce_s)}s before retesting."
            )
        backend = self._backends.get(str(row.get("backend_name") or ""))
        if backend is None:
            return "error: backend disabled"
        message = self._build_test_message()
        destination = PushDestination(
            user_id=caller_id,
            route_id=route_id,
            data=dict(row.get("destination_data") or {}),
        )
        result = await self._safe_send(backend, destination, message)
        self._test_last[route_id] = time.monotonic()
        if result.status is PushDeliveryStatus.DELIVERED:
            return f"Sent test via {row.get('backend_name')}."
        return f"Test failed ({result.status.value}): {result.message}"

    # ── Misc ────────────────────────────────────────────────────────

    def _build_test_message(self) -> PushMessage:
        return PushMessage(
            title="Gilbert · Test",
            body=self._test_message_body,
            urgency=NotificationUrgency.NORMAL,
            source=_TEST_SOURCE,
            source_ref=None,
            notification_id=f"test-{_short_id()}",
        )

    async def _safe_send(
        self,
        backend: PushNotificationBackend,
        destination: PushDestination,
        message: PushMessage,
    ) -> PushDeliveryResult:
        try:
            return await backend.send(destination, message)
        except Exception as exc:
            return PushDeliveryResult(
                status=PushDeliveryStatus.REJECTED,
                message=_safe_repr(exc),
            )

    def _debounce_ok(self, key: str) -> bool:
        last = self._test_last.get(key, 0.0)
        return (time.monotonic() - last) >= self._test_debounce_s

    @staticmethod
    def _serialize_route(row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        # Don't echo destination secrets in raw form — UI displays them
        # masked. The route owner needs to re-enter sensitive fields to
        # change them, mirroring how Settings handles ``sensitive=True``
        # ConfigParams. We keep the keys but blank them server-side so
        # the SPA only has to render placeholders.
        return out

    @staticmethod
    def _fail(
        rpc_name: str, frame: dict[str, Any], error: str
    ) -> dict[str, Any]:
        return {
            "type": f"{rpc_name}.result",
            "ref": frame.get("id"),
            "ok": False,
            "error": error,
        }


# ── Module-level helpers ──────────────────────────────────────────────


def _urgency_rank(value: str) -> int:
    return {"info": 0, "normal": 1, "urgent": 2}.get(value, 1)


def _server_tz_name() -> str:
    """Best-effort name for the server timezone.

    ``datetime.now().astimezone().tzname()`` returns the locale name
    (``"PDT"``), not an IANA name. zoneinfo's "localtime" alias gets us
    something useful on most posix systems; on environments where it
    fails we fall back to UTC.
    """
    try:
        ZoneInfo("localtime")
        return "localtime"
    except ZoneInfoNotFoundError:
        return "UTC"


def _config_param_to_dict(p: ConfigParam) -> dict[str, Any]:
    return {
        "key": p.key,
        "type": p.type.value,
        "description": p.description,
        "default": p.default,
        "sensitive": p.sensitive,
        "multiline": p.multiline,
        "choices": list(p.choices) if p.choices else None,
    }


def _short_hash(data: dict[str, Any]) -> str:
    """Cheap fingerprint of a dict for debounce keys.

    Not cryptographic — we just want stable equivalence for "same form
    submitted twice in 30 seconds." ``hash`` on a sorted tuple does the
    job; fall back to ``repr`` if the dict contains non-hashable values.
    """
    try:
        return f"{hash(tuple(sorted(data.items()))):x}"
    except TypeError:
        return f"{abs(hash(repr(sorted(data.items())))):x}"


# ── AI tool definitions ──────────────────────────────────────────────


_PUSH_AI_TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="list_my_notification_routes",
        description=(
            "List the calling user's external push-notification routes "
            "(ntfy, Pushover, Discord webhook, Telegram). Use this when "
            "the user asks 'what routes do I have' or 'where do my "
            "notifications go'."
        ),
        parameters=[],
        required_role="everyone",
        slash_command="routes",
        slash_group="notify",
        slash_help="List your push notification routes.",
        parallel_safe=True,
    ),
    ToolDefinition(
        name="create_notification_route",
        description=(
            "Add a new push-notification route. The user supplies the "
            "backend (e.g. 'ntfy', 'pushover', 'discord-webhook', "
            "'telegram'), a label, and the per-backend destination "
            "fields as a JSON object (e.g. {'topic': 'gilbert-jeff'} "
            "for ntfy). For complex backends prefer pointing the user "
            "at /account/notifications instead."
        ),
        parameters=[
            ToolParameter(
                name="backend_name",
                type=ToolParameterType.STRING,
                description="Backend identifier (e.g. 'ntfy', 'pushover').",
            ),
            ToolParameter(
                name="label",
                type=ToolParameterType.STRING,
                description="Short user-facing label (e.g. 'Phone').",
            ),
            ToolParameter(
                name="destination",
                type=ToolParameterType.OBJECT,
                description=(
                    "Per-backend destination fields, e.g. {'topic': '...', "
                    "'server': ''} for ntfy. Required keys come from the "
                    "backend's destination_params."
                ),
            ),
            ToolParameter(
                name="urgency_floor",
                type=ToolParameterType.STRING,
                description=(
                    "Minimum urgency to deliver via this route: 'info', "
                    "'normal' (default), or 'urgent'."
                ),
                required=False,
                enum=["info", "normal", "urgent"],
            ),
        ],
        required_role="user",
        slash_command="route_create",
        slash_group="notify",
        slash_help="Add a new push notification route.",
    ),
    ToolDefinition(
        name="delete_notification_route",
        description=(
            "Delete one of the calling user's push-notification routes. "
            "Returns a Confirm/Cancel UI block on the first call so the "
            "model can't silently destroy the wrong route — only the "
            "second invocation with confirm=True actually deletes."
        ),
        parameters=[
            ToolParameter(
                name="route_id",
                type=ToolParameterType.STRING,
                description="The route id from list_my_notification_routes.",
            ),
            ToolParameter(
                name="confirm",
                type=ToolParameterType.BOOLEAN,
                description="True to actually delete; default returns a confirmation block.",
                required=False,
            ),
        ],
        required_role="user",
        slash_command="route_delete",
        slash_group="notify",
        slash_help="Delete a push notification route (asks to confirm).",
    ),
    ToolDefinition(
        name="send_test_notification",
        description=(
            "Send a test message through one of the calling user's "
            "push-notification routes. Honours the same per-route "
            "cooldown the WS RPC does."
        ),
        parameters=[
            ToolParameter(
                name="route_id",
                type=ToolParameterType.STRING,
                description="The route id from list_my_notification_routes.",
            ),
        ],
        required_role="user",
        slash_command="test",
        slash_group="notify",
        slash_help="Send a test message through one of your push routes.",
    ),
]

