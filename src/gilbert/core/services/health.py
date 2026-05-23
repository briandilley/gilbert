"""Health service — multi-backend health-data aggregator with strict
owner-only privacy, audit-logged cross-user reads, and a two-step
right-to-delete wizard.

Lives at ``core/services/`` per the spec layer placement; imports
nothing from ``integrations/`` (concrete backends register themselves
via std-plugins side-effect imports). Discovers backends through
``HealthBackend.registered_backends()``.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import hmac
import json
import logging
import os
import secrets
import stat
import time
import uuid
from collections import OrderedDict, defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gilbert.core.services._ui_blocks import confirm_or_execute
from gilbert.interfaces.ai import AISamplingProvider, Message, MessageRole
from gilbert.interfaces.auth import AccessControlProvider, UserContext
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
    ConfigurationReader,
)
from gilbert.interfaces.events import Event, EventBusProvider
from gilbert.interfaces.health import (
    DEFAULT_AGGREGATOR,
    HEALTH_ADMIN_ROLE,
    AggregatePeriod,
    AggregatorKind,
    DailySummary,
    GreetingBrief,
    HealthAggregate,
    HealthBackend,
    HealthBackendAuthError,
    HealthBackendNotFoundError,
    HealthBackendRateLimitError,
    HealthBackendTransientError,
    HealthMetric,
    LinkCompleteResult,
    LinkStartResult,
    MetricType,
    MetricUnit,
    StorageAwareHealthBackend,
    can_mutate_metrics,
    can_read_metrics,
    metric_types_human_summary,
)
from gilbert.interfaces.context import get_current_user, set_current_user
from gilbert.interfaces.notifications import (
    NotificationProvider,
    NotificationUrgency,
)
from gilbert.interfaces.scheduler import Schedule, SchedulerProvider
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
)
from gilbert.interfaces.ui import ToolOutput

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("gilbert.health.audit")


# ── Collections ──────────────────────────────────────────────────────

_METRICS_COLLECTION = "health_metrics"
_LINKS_COLLECTION = "health_links"
_SUMMARIES_COLLECTION = "health_daily_summaries"
_AUDIT_COLLECTION = "health_audit"
_OAUTH_STATE_COLLECTION = "health_oauth_state"

_ACL_COLLECTION = "acl_collections"
_ROLES_COLLECTION = "acl_roles"


# ── Default prompts (non-clinical guarantee) ─────────────────────────

_DEFAULT_SUMMARY_PROMPT = """\
You write a one-paragraph factual summary of a user's health metrics
for the previous day. You are NOT a clinician. You MUST NOT diagnose,
suggest causes, suggest treatments, or compare to medical norms.

You receive a brief structured-prose description of the day's
headline values (any may be missing). Speak only about what the
data IS, not what it might mean. Don't say "this could indicate",
"this might be a sign of", or "you may want to pay attention to
this." Just describe what the metrics show.

If the data shows a normal-looking day, say so plainly: "Solid
night, normal day" or "Quiet day on the metrics." Don't manufacture
observations. If nothing is present, say so plainly.

Avoid alarming language. Do not use the words "concerning",
"abnormal", "warning", "risk", "noteworthy", or "should". Do not
mention medical conditions by name. Do not describe symptoms in
clinical-sounding language.

Tone: warm, terse, observational. Two to four sentences. Comfortable
with silence. Address the user as "you".
"""

_DEFAULT_TREND_PROMPT = """\
You describe how a single health metric has changed over a window.
You are NOT a clinician.

You receive: the metric's name, its unit, and an array of
(date, value) points covering the window.

You may describe:
  - DIRECTION (up / down / flat)
  - RATE (e.g., "about 0.3 kg per week")
  - CONSISTENCY (e.g., "steady", "bouncy", "trending then flattened")

You MUST NOT:
  - Speculate about causes
  - Suggest medical conditions
  - Suggest treatments or actions
  - Use the words "concerning", "abnormal", "warning", "risk",
    "noteworthy", or "should"

State the start value, the end value, and the window in days.
Two or three sentences. Address the user as "you".
"""


# ── Tunable defaults ─────────────────────────────────────────────────

_DEFAULT_DAILY_SUMMARY_HOUR = 5
_DEFAULT_PULL_SYNC_INTERVAL = 6 * 3600  # 6 hours
_DEFAULT_DAILY_SUMMARY_CONCURRENCY = 8
_DEFAULT_PULL_SYNC_CONCURRENCY = 4
_DEFAULT_MAX_BACKFILL_DAYS = 90
_DEFAULT_RETENTION_DAYS = 0
_DEFAULT_AUDIT_RETENTION_DAYS = 0
_DEFAULT_PER_USER_DAILY_WRITE_CAP = 100_000
_DEFAULT_WEBHOOK_MAX_BODY_BYTES = 1_048_576
_DEFAULT_WEBHOOK_MAX_METRICS = 1000
_DEFAULT_WEBHOOK_RATE_PER_MIN = 60
_DEFAULT_WEBHOOK_UNKNOWN_RATE_PER_MIN = 30
_DEFAULT_AI_PROFILE = "standard"

_DEFAULT_FLAG_LOW_SLEEP_HOURS = 6.0
_DEFAULT_FLAG_LOW_SLEEP_NIGHTS = 3
_DEFAULT_FLAG_SEDENTARY_STEPS = 4000
_DEFAULT_FLAG_SEDENTARY_DAYS = 3
_DEFAULT_FLAG_WEIGHT_DRIFT_KG = 2.0
_DEFAULT_FLAG_WEIGHT_DRIFT_WINDOW_DAYS = 14

_OAUTH_STATE_TTL_SECONDS = 600
_AUTH_FAILURE_THRESHOLD = 5

_BUCKET_LRU_CAP = 10_000

# Hard ceiling on the row count an aggregate query loads into memory.
# A caller asking for a 10-year window with dense data (e.g. minute-
# resolution heart rate) could otherwise pull millions of rows into
# memory before the Python aggregator runs. We cap at 50k and emit a
# WARN when the result hits the cap so callers know the aggregate is
# truncated rather than partial-window-correct.
_MAX_AGGREGATE_ROWS = 50_000

# Soft cap on the in-memory ``_summary_cache``. Eviction is LRU
# (oldest entry dropped) once we exceed the cap.
_SUMMARY_CACHE_MAX = 1000


# ── Bucket helpers ───────────────────────────────────────────────────


@dataclass
class _Bucket:
    """Token-bucket per-key rate limiter (per-token / per-IP).

    Refills ``capacity`` tokens uniformly across 60 seconds. Acquires
    succeed when at least one token is present. The rate-limit per
    minute is the bucket capacity; over-budget callers get rejected.
    """

    capacity: float
    tokens: float
    last_refill: float

    @classmethod
    def fresh(cls, capacity: int) -> _Bucket:
        return cls(capacity=float(capacity), tokens=float(capacity), last_refill=time.monotonic())

    def try_consume(self, capacity: int) -> bool:
        now = time.monotonic()
        elapsed = max(0.0, now - self.last_refill)
        self.capacity = float(capacity)
        # Refill linearly: capacity tokens per minute.
        self.tokens = min(self.capacity, self.tokens + elapsed * (self.capacity / 60.0))
        self.last_refill = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


@dataclass
class _DailyCounter:
    """Rolling per-user write counter for ingest cap enforcement."""

    day: date
    count: int


@dataclass
class _LRUBucketMap:
    """LRU-capped dict of ``_Bucket`` objects keyed by hash / IP."""

    cap: int
    _data: OrderedDict[str, _Bucket] = field(default_factory=OrderedDict)

    def acquire(self, key: str, capacity: int) -> bool:
        bucket = self._data.get(key)
        if bucket is None:
            bucket = _Bucket.fresh(capacity)
            self._data[key] = bucket
            if len(self._data) > self.cap:
                self._data.popitem(last=False)
        else:
            self._data.move_to_end(key)
        return bucket.try_consume(capacity)


# ── Webhook result ───────────────────────────────────────────────────


@dataclass(frozen=True)
class WebhookResult:
    """Service-level webhook outcome — the route maps this to HTTP."""

    status: str  # "ok" / "bad_request" / "payload_too_large" /
    #         "not_found" / "rate_limited"
    received: int = 0
    dropped: int = 0
    retry_after_seconds: int = 0
    message: str = ""


# ── HealthService ────────────────────────────────────────────────────


class HealthService(Service):
    """Multi-backend health-data aggregator with PHI-adjacent privacy.

    Capabilities: ``health``, ``ai_tools``, ``ws_handlers``.
    Events:       ``health.metric.received``, ``health.metric.deleted``,
                  ``health.daily.summary``, ``health.link.connected``,
                  ``health.link.disconnected``, ``health.access.audit``.
    Toggleable via ``enabled`` config flag.

    Owner-only reads enforced in the service (not in routes); cross-
    user reads require the dedicated ``health-admin`` role AND persist
    a ``health_audit`` row AND notify the target user. AI tools never
    accept ``user_id`` from the model — they read ``_user_id`` from
    the injected tool args.
    """

    slash_namespace = "health"

    def __init__(self) -> None:
        self._storage: StorageBackend | None = None
        self._event_bus: Any = None
        self._scheduler: SchedulerProvider | None = None
        self._access_control: AccessControlProvider | None = None
        self._ai: AISamplingProvider | None = None
        self._notifications: NotificationProvider | None = None
        self._resolver: ServiceResolver | None = None

        self._backends: dict[str, HealthBackend] = {}
        self._enabled: bool = True

        # Tunables (recomputed in on_config_changed).
        self._debug_log_values: bool = False
        self._max_backfill_days: int = _DEFAULT_MAX_BACKFILL_DAYS
        self._retention_days: int = _DEFAULT_RETENTION_DAYS
        self._audit_retention_days: int = _DEFAULT_AUDIT_RETENTION_DAYS
        self._daily_summary_local_hour: int = _DEFAULT_DAILY_SUMMARY_HOUR
        self._pull_sync_interval_seconds: int = _DEFAULT_PULL_SYNC_INTERVAL
        self._daily_summary_concurrency: int = _DEFAULT_DAILY_SUMMARY_CONCURRENCY
        self._pull_sync_concurrency: int = _DEFAULT_PULL_SYNC_CONCURRENCY
        self._per_user_daily_write_cap: int = _DEFAULT_PER_USER_DAILY_WRITE_CAP
        self._webhook_max_body_bytes: int = _DEFAULT_WEBHOOK_MAX_BODY_BYTES
        self._webhook_max_metrics: int = _DEFAULT_WEBHOOK_MAX_METRICS
        self._webhook_rate_per_minute: int = _DEFAULT_WEBHOOK_RATE_PER_MIN
        self._webhook_unknown_rate_per_minute: int = _DEFAULT_WEBHOOK_UNKNOWN_RATE_PER_MIN
        self._ai_profile: str = _DEFAULT_AI_PROFILE
        self._summary_prompt: str = _DEFAULT_SUMMARY_PROMPT
        self._trend_prompt: str = _DEFAULT_TREND_PROMPT

        self._flag_low_sleep_hours: float = _DEFAULT_FLAG_LOW_SLEEP_HOURS
        self._flag_low_sleep_consecutive_nights: int = _DEFAULT_FLAG_LOW_SLEEP_NIGHTS
        self._flag_sedentary_steps: int = _DEFAULT_FLAG_SEDENTARY_STEPS
        self._flag_sedentary_consecutive_days: int = _DEFAULT_FLAG_SEDENTARY_DAYS
        self._flag_weight_drift_kg: float = _DEFAULT_FLAG_WEIGHT_DRIFT_KG
        self._flag_weight_drift_window_days: int = _DEFAULT_FLAG_WEIGHT_DRIFT_WINDOW_DAYS

        # Per-(user, backend) ingest serialization. Keyed dict on the
        # singleton — different users / different backends fan out.
        self._ingest_locks: dict[tuple[str, str], asyncio.Lock] = {}

        # Per-user write cap counter.
        self._per_user_write_caps: dict[str, _DailyCounter] = {}

        # Webhook rate-limit buckets, keyed and LRU-capped.
        self._webhook_buckets: _LRUBucketMap = _LRUBucketMap(cap=_BUCKET_LRU_CAP)
        self._webhook_ip_buckets: _LRUBucketMap = _LRUBucketMap(cap=_BUCKET_LRU_CAP)

        # Per-(user, backend) consecutive-auth-failure counter for
        # automatic-disable on persistent reconnect requirement.
        self._consecutive_auth_failures: dict[tuple[str, str], int] = {}

        # Public-base-url cache (resolved from config; backends use it
        # for OAuth callback URLs).
        self._public_base_url: str = ""

        # LRU-bounded cache of latest daily summaries.
        self._summary_cache: OrderedDict[str, DailySummary] = OrderedDict()

        # Per-state asyncio.Lock so two concurrent OAuth callbacks for
        # the same state cannot both observe ``consumed_at == None``
        # and both succeed (race repro: an attacker / browser-prefetch
        # firing the callback URL twice). Setdefault under the lock
        # ensures only the first arriver creates the lock object.
        self._oauth_state_locks: dict[str, asyncio.Lock] = {}

    # ── Service metadata ─────────────────────────────────────────────

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="health",
            capabilities=frozenset({"health", "ai_tools", "ws_handlers", "greeting_context"}),
            requires=frozenset({"entity_storage", "scheduler"}),
            optional=frozenset(
                {
                    "event_bus",
                    "configuration",
                    "access_control",
                    "ai_chat",
                    "notifications",
                    "users",
                }
            ),
            events=frozenset(
                {
                    "health.metric.received",
                    "health.metric.deleted",
                    "health.daily.summary",
                    "health.link.connected",
                    "health.link.disconnected",
                    "health.access.audit",
                }
            ),
            ai_calls=frozenset({"health_daily_summary"}),
            toggleable=True,
            toggle_description="Personal health metrics ingestion + tools",
        )

    @property
    def tool_provider_name(self) -> str:
        return "health"

    @property
    def public_base_url(self) -> str:
        """Returned to backends building OAuth callback URLs. Empty
        string means "admin hasn't set gilbert.public_base_url yet" —
        OAuth flows refuse to begin in that case."""
        return self._public_base_url

    @property
    def webhook_max_body_bytes(self) -> int:
        """Body-size cap exposed to the route layer for the
        Content-Length pre-check (defends against memory-DoS via a
        very large advertised body)."""
        return self._webhook_max_body_bytes

    # ── Lifecycle ────────────────────────────────────────────────────

    async def start(self, resolver: ServiceResolver) -> None:
        storage_svc = resolver.require_capability("entity_storage")
        if not isinstance(storage_svc, StorageProvider):
            raise TypeError("entity_storage capability does not provide StorageProvider")
        self._storage = storage_svc.backend

        # Indexes per spec §7.5.
        for index in (
            IndexDefinition(
                collection=_METRICS_COLLECTION,
                fields=["user_id", "metric_type", "recorded_at"],
            ),
            IndexDefinition(
                collection=_METRICS_COLLECTION,
                fields=["user_id", "recorded_at"],
            ),
            IndexDefinition(
                collection=_METRICS_COLLECTION,
                fields=["user_id", "backend", "source_event_id"],
            ),
            IndexDefinition(
                collection=_METRICS_COLLECTION,
                fields=["user_id", "backend", "metric_type", "recorded_at"],
            ),
            IndexDefinition(
                collection=_LINKS_COLLECTION,
                fields=["user_id"],
            ),
            IndexDefinition(
                collection=_LINKS_COLLECTION,
                fields=["webhook_token_hash"],
                unique=True,
            ),
            IndexDefinition(
                collection=_LINKS_COLLECTION,
                fields=["backend_name", "enabled"],
            ),
            IndexDefinition(
                collection=_SUMMARIES_COLLECTION,
                fields=["user_id", "local_date"],
            ),
            IndexDefinition(
                collection=_OAUTH_STATE_COLLECTION,
                fields=["_id"],
            ),
            IndexDefinition(
                collection=_AUDIT_COLLECTION,
                fields=["target_user_id", "accessed_at"],
            ),
        ):
            await self._storage.ensure_index(index)

        # Optional capabilities.
        event_bus_svc = resolver.get_capability("event_bus")
        if isinstance(event_bus_svc, EventBusProvider):
            self._event_bus = event_bus_svc.bus

        acl_svc = resolver.get_capability("access_control")
        if isinstance(acl_svc, AccessControlProvider):
            self._access_control = acl_svc

        ai_svc = resolver.get_capability("ai_chat")
        if isinstance(ai_svc, AISamplingProvider):
            self._ai = ai_svc

        notif_svc = resolver.get_capability("notifications")
        if isinstance(notif_svc, NotificationProvider):
            self._notifications = notif_svc

        self._resolver = resolver

        # Load config + cache prompts/thresholds.
        config_svc = resolver.get_capability("configuration")
        section: dict[str, Any] = {}
        if isinstance(config_svc, ConfigurationReader):
            section = config_svc.get_section_safe(self.config_namespace)
            # Read gilbert.public_base_url for OAuth callback URLs.
            try:
                gb = config_svc.get_section_safe("gilbert")
            except Exception:
                gb = {}
            self._public_base_url = str(gb.get("public_base_url") or "")

        await self.on_config_changed(section)

        if not self._enabled:
            logger.info("Health service disabled via configuration")
            return

        # Seed RBAC primitives idempotently.
        await self._seed_acl()

        # Subscribe to bus events: cascade delete + summary cache.
        if self._event_bus is not None:
            self._event_bus.subscribe("auth.user.deleted", self._on_user_deleted)
            self._event_bus.subscribe(
                "health.daily.summary",
                self._on_daily_summary_event,
            )

        # Discover and initialize every registered backend.
        await self._init_backends(section)

        # Schedule jobs.
        scheduler_svc = resolver.require_capability("scheduler")
        if not isinstance(scheduler_svc, SchedulerProvider):
            raise TypeError("scheduler capability does not provide SchedulerProvider")
        self._scheduler = scheduler_svc

        self._scheduler.add_job(
            name="health-daily-summary-tick",
            schedule=Schedule.hourly_at(0),
            callback=self._run_daily_summary_tick,
            system=True,
        )

        for name, backend in self._backends.items():
            if backend.supports_pull:
                job_name = f"health-pull-sync-{name}"
                self._scheduler.add_job(
                    name=job_name,
                    schedule=Schedule.every(self._pull_sync_interval_seconds),
                    callback=self._make_pull_sync_callback(name),
                    system=True,
                )

        if self._retention_days > 0 or self._audit_retention_days > 0:
            self._scheduler.add_job(
                name="health-retention-prune",
                schedule=Schedule.daily_at(3, 0),
                callback=self._run_retention_prune,
                system=True,
            )

        self._scheduler.add_job(
            name="health-oauth-state-gc",
            schedule=Schedule.every(600),
            callback=self._run_oauth_state_gc,
            system=True,
        )

        # Startup security warnings.
        await self._emit_security_warnings()

        logger.info(
            "Health service started — %d backend(s); summary tick hourly; "
            "pull sync %ds; retention=%dd; audit retention=%dd",
            len(self._backends),
            self._pull_sync_interval_seconds,
            self._retention_days,
            self._audit_retention_days,
        )

    async def stop(self) -> None:
        if self._scheduler is not None:
            for name in (
                "health-daily-summary-tick",
                "health-retention-prune",
                "health-oauth-state-gc",
            ):
                with contextlib.suppress(Exception):
                    self._scheduler.remove_job(name)
            for backend_name in list(self._backends.keys()):
                with contextlib.suppress(Exception):
                    self._scheduler.remove_job(f"health-pull-sync-{backend_name}")

        for backend in list(self._backends.values()):
            try:
                await backend.close()
            except Exception:
                logger.exception("Failed to close health backend %s", backend.backend_name)
        self._backends.clear()
        self._ingest_locks.clear()
        self._per_user_write_caps.clear()
        self._consecutive_auth_failures.clear()
        logger.info("Health service stopped")

    async def _init_backends(self, section: dict[str, Any]) -> None:
        """Instantiate each registered ``HealthBackend`` and call
        ``initialize()`` with its per-backend config subsection.

        Backends satisfying ``StorageAwareHealthBackend`` get the raw
        storage backend AND the public base URL injected before
        ``initialize`` so OAuth flows can read / write the link row
        and build callback URLs from a single trusted source.
        """
        for name, cls in HealthBackend.registered_backends().items():
            try:
                backend = cls()
                if isinstance(backend, StorageAwareHealthBackend):
                    backend.set_storage(self._storage)
                    backend.set_public_base_url(self._public_base_url)
                sub = section.get(name) if isinstance(section.get(name), dict) else {}
                await backend.initialize(dict(sub) if isinstance(sub, dict) else {})
                self._backends[name] = backend
                logger.info("Health backend '%s' initialized", name)
            except Exception:
                logger.exception("Failed to initialize health backend %s", name)

    async def _seed_acl(self) -> None:
        """Seed the dedicated ``health-admin`` role and the per-collection
        ACL rows so the entities page never silently exposes private
        data. Idempotent: re-seeding is a no-op if the rows exist.

        The ``health-admin`` role is NOT auto-granted to any user
        (including the built-in admin). Operators grant it explicitly
        via ``/roles/users``.
        """
        assert self._storage is not None

        # Seed ``health-admin`` role at level 0.
        existing_role = await self._storage.get(_ROLES_COLLECTION, HEALTH_ADMIN_ROLE)
        if existing_role is None:
            await self._storage.put(
                _ROLES_COLLECTION,
                HEALTH_ADMIN_ROLE,
                {
                    "name": HEALTH_ADMIN_ROLE,
                    "level": 0,
                    "builtin": False,
                    "description": (
                        "Cross-user health-data read access. Audit-logged "
                        "and target-user-notified on use."
                    ),
                },
            )
            logger.info("Seeded role '%s' at level 0", HEALTH_ADMIN_ROLE)

        # Seed per-collection ACLs.
        acl_specs = [
            (_METRICS_COLLECTION, HEALTH_ADMIN_ROLE, "admin"),
            (_LINKS_COLLECTION, HEALTH_ADMIN_ROLE, "admin"),
            (_SUMMARIES_COLLECTION, HEALTH_ADMIN_ROLE, "admin"),
            (_AUDIT_COLLECTION, HEALTH_ADMIN_ROLE, "admin"),
            (_OAUTH_STATE_COLLECTION, "admin", "admin"),
        ]
        for collection, read_role, write_role in acl_specs:
            existing_acl = await self._storage.get(_ACL_COLLECTION, collection)
            if existing_acl is None:
                await self._storage.put(
                    _ACL_COLLECTION,
                    collection,
                    {
                        "collection": collection,
                        "read_role": read_role,
                        "write_role": write_role,
                    },
                )

    async def _emit_security_warnings(self) -> None:
        """Emit startup WARNs for known-risky configurations.

        Per spec §6.4 / §6.7 / §7.5 step 7:
        - OAuth backend present + non-127.0.0.1 bind: Withings refresh
          tokens travel over plain HTTP and rest unencrypted on disk.
        - ``debug_log_values=true`` AND user count > 1: in a multi-user
          deployment, DEBUG-logging metric values bleeds one user's
          data into the operator's general log file.
        - ``.gilbert/gilbert.db`` permissions looser than 0600 (POSIX).
        """
        # File permissions check — best-effort.
        try:
            from gilbert.config import DATA_DIR

            db_path = os.path.join(str(DATA_DIR), "gilbert.db")
            if os.path.exists(db_path) and os.name == "posix":
                mode = stat.S_IMODE(os.stat(db_path).st_mode)
                if mode & 0o077:  # Any group/other bits set.
                    logger.warning(
                        "Health: %s mode %o is more permissive than 0600 — "
                        "OAuth tokens are stored in plaintext (v1)",
                        db_path,
                        mode,
                    )
        except Exception:
            logger.debug("Could not inspect DB file permissions", exc_info=True)

        # OAuth + non-localhost bind warning. The bind address lives
        # in the ``gilbert`` config section under ``web.bind_address``;
        # if no OAuth-capable backend is registered (no Withings, no
        # future Garmin/Oura/Fitbit) the warning is irrelevant.
        has_oauth_backend = any(
            b.supports_pull for b in self._backends.values()
        )
        if has_oauth_backend and self._resolver is not None:
            config_svc = self._resolver.get_capability("configuration")
            bind_address = ""
            if isinstance(config_svc, ConfigurationReader):
                try:
                    gilbert_section = config_svc.get_section_safe("gilbert")
                except Exception:
                    gilbert_section = {}
                web_section = gilbert_section.get("web") or {}
                if isinstance(web_section, dict):
                    bind_address = str(web_section.get("bind_address") or "")
            # Localhost bindings are safe; everything else trips the
            # warning (operator may have a TLS-fronting tunnel in
            # front, but the WARN is the v1 friction we owe them).
            if bind_address and bind_address not in (
                "127.0.0.1",
                "localhost",
                "::1",
            ):
                logger.warning(
                    "Health: OAuth backend(s) present and web.bind_address "
                    "is %r (not 127.0.0.1) — Withings refresh tokens travel "
                    "over plain HTTP and rest unencrypted on disk in v1. "
                    "Front Gilbert with a TLS-terminating tunnel before "
                    "exposing OAuth flows to the network.",
                    bind_address,
                )

        # debug_log_values + multi-user warning.
        if self._debug_log_values and self._storage is not None:
            try:
                user_count = await self._storage.count(
                    Query(collection="users")
                )
            except Exception:
                user_count = 0
            if user_count > 1:
                logger.warning(
                    "Health: debug_log_values=true on a multi-user instance "
                    "(%d users). DEBUG-level log lines now include metric "
                    "values — one user's readings will appear in the shared "
                    "log file. Disable for any deployment beyond a single "
                    "trusted operator.",
                    user_count,
                )

    # ── Configurable ─────────────────────────────────────────────────

    @property
    def config_namespace(self) -> str:
        return "health"

    @property
    def config_category(self) -> str:
        return "Personal Data"

    def config_params(self) -> list[ConfigParam]:
        params: list[ConfigParam] = [
            ConfigParam(
                key="enabled",
                type=ToolParameterType.BOOLEAN,
                description="Toggle the whole health service.",
                default=True,
            ),
            ConfigParam(
                key="daily_summary_local_hour",
                type=ToolParameterType.INTEGER,
                description=(
                    "Local hour to fire the per-user daily summary "
                    "(0–23). DST handled by zoneinfo. Note: the "
                    "scheduler ticks every UTC top-of-hour, so users "
                    "in fractional-offset timezones (IST = UTC+5:30, "
                    "Newfoundland = -3:30, Nepal = +5:45, ...) get "
                    "their summary at the next half-hour past this "
                    "value rather than on the exact minute."
                ),
                default=_DEFAULT_DAILY_SUMMARY_HOUR,
            ),
            ConfigParam(
                key="pull_sync_interval_seconds",
                type=ToolParameterType.INTEGER,
                description=(
                    "Interval for pull-sync backends (Withings). "
                    "Default 6h — Withings data doesn't change hourly."
                ),
                default=_DEFAULT_PULL_SYNC_INTERVAL,
            ),
            ConfigParam(
                key="daily_summary_concurrency",
                type=ToolParameterType.INTEGER,
                description="Max concurrent daily-summary tasks per tick.",
                default=_DEFAULT_DAILY_SUMMARY_CONCURRENCY,
            ),
            ConfigParam(
                key="pull_sync_concurrency",
                type=ToolParameterType.INTEGER,
                description="Max concurrent pull-sync tasks per backend per run.",
                default=_DEFAULT_PULL_SYNC_CONCURRENCY,
            ),
            ConfigParam(
                key="max_backfill_days",
                type=ToolParameterType.INTEGER,
                description=(
                    "Webhook deliveries with recorded_at older than this "
                    "are dropped."
                ),
                default=_DEFAULT_MAX_BACKFILL_DAYS,
            ),
            ConfigParam(
                key="retention_days",
                type=ToolParameterType.INTEGER,
                description="0 = keep forever. Daily prune at 03:00 UTC otherwise.",
                default=_DEFAULT_RETENTION_DAYS,
            ),
            ConfigParam(
                key="audit_retention_days",
                type=ToolParameterType.INTEGER,
                description=(
                    "0 = keep forever (default — audit rows outlive "
                    "metrics deliberately)."
                ),
                default=_DEFAULT_AUDIT_RETENTION_DAYS,
            ),
            ConfigParam(
                key="per_user_daily_write_cap",
                type=ToolParameterType.INTEGER,
                description=(
                    "Per-user metric writes-per-day cap. Drops over-cap "
                    "rows with a single INFO log line."
                ),
                default=_DEFAULT_PER_USER_DAILY_WRITE_CAP,
            ),
            ConfigParam(
                key="webhook_max_body_bytes",
                type=ToolParameterType.INTEGER,
                description="Reject webhook bodies above this size with 413.",
                default=_DEFAULT_WEBHOOK_MAX_BODY_BYTES,
            ),
            ConfigParam(
                key="webhook_max_metrics_per_delivery",
                type=ToolParameterType.INTEGER,
                description="Reject deliveries with more metrics than this with 400.",
                default=_DEFAULT_WEBHOOK_MAX_METRICS,
            ),
            ConfigParam(
                key="webhook_rate_per_minute",
                type=ToolParameterType.INTEGER,
                description="Per-token bucket size (deliveries / minute / token).",
                default=_DEFAULT_WEBHOOK_RATE_PER_MIN,
            ),
            ConfigParam(
                key="webhook_unknown_rate_per_minute",
                type=ToolParameterType.INTEGER,
                description=(
                    "Per-IP bucket on the 404 (unknown-token) path — "
                    "defends against token enumeration."
                ),
                default=_DEFAULT_WEBHOOK_UNKNOWN_RATE_PER_MIN,
            ),
            ConfigParam(
                key="debug_log_values",
                type=ToolParameterType.BOOLEAN,
                description=(
                    "If true, DEBUG logs include metric values. Global "
                    "flag — startup WARN if true with >1 user."
                ),
                default=False,
            ),
            ConfigParam(
                key="ai_profile",
                type=ToolParameterType.STRING,
                description=(
                    "AI profile for daily-summary / health_summary / "
                    "health_trend / health_now. Use 'standard' so the "
                    "non-clinical prompt constraints aren't dropped."
                ),
                default=_DEFAULT_AI_PROFILE,
                choices_from="ai_profiles",
            ),
            ConfigParam(
                key="summary_prompt",
                type=ToolParameterType.STRING,
                description=(
                    "Daily-summary system prompt. Must enforce "
                    "non-clinical framing — see the bundled default."
                ),
                default=_DEFAULT_SUMMARY_PROMPT,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="trend_prompt",
                type=ToolParameterType.STRING,
                description=(
                    "Trend system prompt. Direction / rate / consistency "
                    "framing without crossing into causes."
                ),
                default=_DEFAULT_TREND_PROMPT,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="flag_low_sleep_hours",
                type=ToolParameterType.NUMBER,
                description="Threshold for the low_sleep flag (hours).",
                default=_DEFAULT_FLAG_LOW_SLEEP_HOURS,
            ),
            ConfigParam(
                key="flag_low_sleep_consecutive_nights",
                type=ToolParameterType.INTEGER,
                description="Consecutive-night count for low_sleep.",
                default=_DEFAULT_FLAG_LOW_SLEEP_NIGHTS,
            ),
            ConfigParam(
                key="flag_sedentary_steps",
                type=ToolParameterType.INTEGER,
                description="Threshold for the sedentary flag (steps).",
                default=_DEFAULT_FLAG_SEDENTARY_STEPS,
            ),
            ConfigParam(
                key="flag_sedentary_consecutive_days",
                type=ToolParameterType.INTEGER,
                description="Consecutive-day count for sedentary.",
                default=_DEFAULT_FLAG_SEDENTARY_DAYS,
            ),
            ConfigParam(
                key="flag_weight_drift_kg",
                type=ToolParameterType.NUMBER,
                description="Magnitude (kg) for the weight_drift flag.",
                default=_DEFAULT_FLAG_WEIGHT_DRIFT_KG,
            ),
            ConfigParam(
                key="flag_weight_drift_window_days",
                type=ToolParameterType.INTEGER,
                description="Window for weight_drift (days).",
                default=_DEFAULT_FLAG_WEIGHT_DRIFT_WINDOW_DAYS,
            ),
        ]
        # Merge each backend's params under its name. Forward
        # ``ai_prompt=bp.ai_prompt`` so a future backend declaring an
        # AI prompt doesn't lose the flag.
        for name, cls in HealthBackend.registered_backends().items():
            for bp in cls.backend_config_params():
                params.append(
                    ConfigParam(
                        key=f"{name}.{bp.key}",
                        type=bp.type,
                        description=bp.description,
                        default=bp.default,
                        sensitive=bp.sensitive,
                        choices=bp.choices,
                        multiline=bp.multiline,
                        choices_from=bp.choices_from,
                        backend_param=True,
                        ai_prompt=bp.ai_prompt,
                    )
                )
        return params

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._enabled = bool(config.get("enabled", self._enabled))
        self._daily_summary_local_hour = int(
            config.get("daily_summary_local_hour", self._daily_summary_local_hour)
        )
        self._pull_sync_interval_seconds = int(
            config.get("pull_sync_interval_seconds", self._pull_sync_interval_seconds)
        )
        self._daily_summary_concurrency = int(
            config.get("daily_summary_concurrency", self._daily_summary_concurrency)
        )
        self._pull_sync_concurrency = int(
            config.get("pull_sync_concurrency", self._pull_sync_concurrency)
        )
        self._max_backfill_days = int(
            config.get("max_backfill_days", self._max_backfill_days)
        )
        self._retention_days = int(config.get("retention_days", self._retention_days))
        self._audit_retention_days = int(
            config.get("audit_retention_days", self._audit_retention_days)
        )
        self._per_user_daily_write_cap = int(
            config.get("per_user_daily_write_cap", self._per_user_daily_write_cap)
        )
        self._webhook_max_body_bytes = int(
            config.get("webhook_max_body_bytes", self._webhook_max_body_bytes)
        )
        self._webhook_max_metrics = int(
            config.get(
                "webhook_max_metrics_per_delivery", self._webhook_max_metrics
            )
        )
        self._webhook_rate_per_minute = int(
            config.get("webhook_rate_per_minute", self._webhook_rate_per_minute)
        )
        self._webhook_unknown_rate_per_minute = int(
            config.get(
                "webhook_unknown_rate_per_minute",
                self._webhook_unknown_rate_per_minute,
            )
        )
        self._debug_log_values = bool(
            config.get("debug_log_values", self._debug_log_values)
        )
        self._ai_profile = (
            str(config.get("ai_profile", "") or "") or _DEFAULT_AI_PROFILE
        )
        # AI prompts: empty override falls back to the bundled default.
        self._summary_prompt = (
            str(config.get("summary_prompt", "") or "") or _DEFAULT_SUMMARY_PROMPT
        )
        self._trend_prompt = (
            str(config.get("trend_prompt", "") or "") or _DEFAULT_TREND_PROMPT
        )

        self._flag_low_sleep_hours = float(
            config.get("flag_low_sleep_hours", self._flag_low_sleep_hours)
        )
        self._flag_low_sleep_consecutive_nights = int(
            config.get(
                "flag_low_sleep_consecutive_nights",
                self._flag_low_sleep_consecutive_nights,
            )
        )
        self._flag_sedentary_steps = int(
            config.get("flag_sedentary_steps", self._flag_sedentary_steps)
        )
        self._flag_sedentary_consecutive_days = int(
            config.get(
                "flag_sedentary_consecutive_days",
                self._flag_sedentary_consecutive_days,
            )
        )
        self._flag_weight_drift_kg = float(
            config.get("flag_weight_drift_kg", self._flag_weight_drift_kg)
        )
        self._flag_weight_drift_window_days = int(
            config.get(
                "flag_weight_drift_window_days",
                self._flag_weight_drift_window_days,
            )
        )

        # Refresh ``gilbert.public_base_url`` so an operator who set it
        # AFTER the service started doesn't have to restart Gilbert for
        # OAuth flows + webhook URL building to start working. Push the
        # new value into every storage-aware backend (Withings) AND
        # cache it locally for ``rotate_webhook_token``.
        if self._resolver is not None:
            config_svc = self._resolver.get_capability("configuration")
            if isinstance(config_svc, ConfigurationReader):
                try:
                    gb = config_svc.get_section_safe("gilbert")
                except Exception:
                    gb = {}
                self._public_base_url = str(gb.get("public_base_url") or "")
                for backend in self._backends.values():
                    if isinstance(backend, StorageAwareHealthBackend):
                        try:
                            backend.set_public_base_url(self._public_base_url)
                        except Exception:
                            logger.debug(
                                "set_public_base_url failed on backend %s",
                                getattr(backend, "backend_name", "?"),
                                exc_info=True,
                            )

    # ── Authorization helpers ────────────────────────────────────────

    def _is_health_admin(self, user_ctx: UserContext) -> bool:
        return HEALTH_ADMIN_ROLE in user_ctx.roles

    def _require_read(
        self,
        user_ctx: UserContext,
        target_user_id: str,
    ) -> None:
        if not can_read_metrics(
            user_ctx,
            target_user_id,
            is_health_admin=self._is_health_admin(user_ctx),
        ):
            raise PermissionError(
                f"User {user_ctx.user_id!r} cannot read metrics for "
                f"{target_user_id!r}"
            )

    def _require_mutate(
        self,
        user_ctx: UserContext,
        target_user_id: str,
    ) -> None:
        if not can_mutate_metrics(user_ctx, target_user_id):
            raise PermissionError(
                f"User {user_ctx.user_id!r} cannot mutate metrics for "
                f"{target_user_id!r}"
            )

    # ── Ingestion ────────────────────────────────────────────────────

    async def ingest_metrics(
        self,
        user_id: str,
        backend_name: str,
        metrics: list[HealthMetric],
    ) -> int:
        """Persist a batch of metrics for one user. Idempotent on
        ``(user_id, backend, source_event_id)`` (or ``(user_id, backend,
        metric_type, recorded_at)`` fallback when ``source_event_id`` is
        empty). Returns the count of newly-persisted rows.

        Atomicity: the dedup-then-write path acquires
        ``self._ingest_locks[(user_id, backend_name)]`` so concurrent
        deliveries for the same (user, backend) serialize. Different
        users / different backends fan out as normal.

        Events: publishes ``health.metric.received`` ONLY for newly-
        persisted rows. Duplicate deliveries silently skip the event
        publish — defeats replay-flood amplification.
        """
        if self._storage is None:
            raise RuntimeError("HealthService not started")
        if not metrics:
            return 0

        key = (user_id, backend_name)
        lock = self._ingest_locks.setdefault(key, asyncio.Lock())
        persisted = 0
        async with lock:
            for metric in metrics:
                if not self._allow_one_more_write(user_id):
                    logger.info(
                        "Health: per-user write cap exhausted for %s; "
                        "dropping over-cap rows",
                        user_id,
                    )
                    break
                # Dedup: query by source_event_id when present.
                existing_id = await self._lookup_existing(metric)
                if existing_id is not None:
                    # Last-write-wins on (user, type, recorded_at).
                    # Replace by deleting + inserting; no event emission.
                    await self._storage.delete(_METRICS_COLLECTION, existing_id)
                row_id = metric.id or f"hm_{uuid.uuid4().hex[:16]}"
                stored_metric = replace(metric, id=row_id, user_id=user_id, backend=backend_name)
                await self._storage.put(
                    _METRICS_COLLECTION, row_id, stored_metric.to_dict()
                )
                if existing_id is None:
                    persisted += 1
                    self._record_user_write(user_id)
                    await self._publish_event(
                        "health.metric.received",
                        {
                            "user_id": user_id,
                            "backend": backend_name,
                            "metric_type": metric.metric_type.value,
                            "value": float(metric.value),
                            "unit": metric.unit.value,
                            "recorded_at": metric.recorded_at.isoformat(),
                        },
                    )
                else:
                    logger.debug(
                        "Health: duplicate metric for %s/%s replaced (no event)",
                        user_id,
                        backend_name,
                    )

        logger.info(
            "Health: ingested %d metric(s) for user %s from backend %s",
            persisted,
            user_id,
            backend_name,
        )
        return persisted

    async def _lookup_existing(self, metric: HealthMetric) -> str | None:
        """Return the row id of an existing metric matching the dedup key,
        or None.

        Primary dedup: ``(user_id, backend, source_event_id)`` when
        ``source_event_id`` is non-empty. Fallback (when empty):
        ``(user_id, backend, metric_type, recorded_at)``.

        Both sides of the recorded_at comparison are normalized to UTC
        before being serialized to ISO 8601 — Python's ``isoformat``
        is not canonical (``+00:00`` vs ``Z``, microsecond presence,
        etc.), so a round-trip via ``astimezone(UTC).isoformat()``
        yields a stable shape the EQ filter actually matches. Without
        this, two distinct readings at the same wall-clock second
        from different sources would both survive instead of dedup'ing.
        """
        assert self._storage is not None
        if metric.source_event_id:
            rows = await self._storage.query(
                Query(
                    collection=_METRICS_COLLECTION,
                    filters=[
                        Filter(field="user_id", op=FilterOp.EQ, value=metric.user_id),
                        Filter(field="backend", op=FilterOp.EQ, value=metric.backend),
                        Filter(
                            field="source_event_id",
                            op=FilterOp.EQ,
                            value=metric.source_event_id,
                        ),
                    ],
                    limit=1,
                )
            )
        else:
            recorded_canonical = _canonical_iso(metric.recorded_at)
            # Fall back to a small candidate set keyed only on
            # ``(user_id, backend, metric_type)`` and filter in Python
            # — defends against either side being persisted with a
            # non-canonical ISO string from older code paths or other
            # backends. Bounded: a single (user, backend, type) tuple
            # rarely has more than a handful of rows at the same
            # second. ``limit=20`` keeps memory bounded.
            candidates = await self._storage.query(
                Query(
                    collection=_METRICS_COLLECTION,
                    filters=[
                        Filter(field="user_id", op=FilterOp.EQ, value=metric.user_id),
                        Filter(field="backend", op=FilterOp.EQ, value=metric.backend),
                        Filter(
                            field="metric_type",
                            op=FilterOp.EQ,
                            value=metric.metric_type.value,
                        ),
                    ],
                    limit=20,
                )
            )
            rows = []
            for c in candidates:
                stored_raw = str(c.get("recorded_at") or "")
                try:
                    stored_canonical = _canonical_iso(
                        datetime.fromisoformat(stored_raw)
                    )
                except ValueError:
                    continue
                if stored_canonical == recorded_canonical:
                    rows = [c]
                    break
        if not rows:
            return None
        return str(rows[0].get("id") or rows[0].get("_id") or "")

    def _allow_one_more_write(self, user_id: str) -> bool:
        """Return True if the user is under the daily write cap."""
        today = datetime.now(UTC).date()
        counter = self._per_user_write_caps.get(user_id)
        if counter is None or counter.day != today:
            counter = _DailyCounter(day=today, count=0)
            self._per_user_write_caps[user_id] = counter
        return counter.count < self._per_user_daily_write_cap

    def _record_user_write(self, user_id: str) -> None:
        today = datetime.now(UTC).date()
        counter = self._per_user_write_caps.get(user_id)
        if counter is None or counter.day != today:
            counter = _DailyCounter(day=today, count=0)
            self._per_user_write_caps[user_id] = counter
        counter.count += 1

    async def _publish_event(self, event_type: str, data: dict[str, Any]) -> None:
        if self._event_bus is None:
            return
        try:
            await self._event_bus.publish(
                Event(event_type=event_type, data=data, source="health")
            )
        except Exception:
            logger.exception("Failed to publish %s", event_type)

    # ── Webhook dispatch ─────────────────────────────────────────────

    async def ingest_webhook(
        self,
        token: str,
        body: bytes,
        headers: dict[str, str],
        *,
        remote_addr: str = "",
    ) -> WebhookResult:
        """Resolve token → user_id + backend, rate-limit-check, then
        ``backend.parse_webhook`` + ``ingest_metrics``.

        Bad payloads return a ``WebhookResult(status="bad_request")``
        rather than raising — the route layer maps to HTTP statuses.
        ``not_found`` and disabled-token cases collapse to the same
        wire shape (404, ``{"received": 0}``) to defeat enumeration.
        """
        if self._storage is None:
            return WebhookResult(status="bad_request", message="not started")

        # Body-cap check first — cheaper than a token lookup.
        if len(body) > self._webhook_max_body_bytes:
            return WebhookResult(status="payload_too_large")

        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

        # Look up the link row by token hash. Missing OR disabled both
        # collapse to the same response shape.
        link_row = await self._lookup_link_by_token_hash(token_hash)
        if link_row is None or not bool(link_row.get("enabled", False)):
            # Per-IP rate limit on the 404 path.
            if remote_addr:
                ok = self._webhook_ip_buckets.acquire(
                    remote_addr, self._webhook_unknown_rate_per_minute
                )
                if not ok:
                    return WebhookResult(status="rate_limited", retry_after_seconds=60)
            return WebhookResult(status="not_found")

        # Defense-in-depth: constant-time confirmation. The lookup
        # already matched, but a future storage layer with timing
        # variance shouldn't undermine the property.
        stored_hash = str(link_row.get("webhook_token_hash") or "")
        if not hmac.compare_digest(stored_hash, token_hash):
            return WebhookResult(status="not_found")

        # Per-token rate limit.
        if not self._webhook_buckets.acquire(token_hash, self._webhook_rate_per_minute):
            return WebhookResult(status="rate_limited", retry_after_seconds=60)

        user_id = str(link_row.get("user_id") or "")
        backend_name = str(link_row.get("backend_name") or "")
        backend = self._backends.get(backend_name)
        if backend is None or not backend.supports_push:
            return WebhookResult(status="not_found")

        try:
            metrics = await backend.parse_webhook(user_id, body, headers)
        except Exception as exc:
            logger.warning(
                "Health webhook parse failed for backend=%s user=%s: %s",
                backend_name,
                user_id,
                exc,
            )
            return WebhookResult(status="bad_request", message=str(exc))

        if len(metrics) > self._webhook_max_metrics:
            return WebhookResult(status="bad_request", message="too many metrics")

        # Drop metrics whose recorded_at is older than max_backfill_days.
        cutoff = datetime.now(UTC) - timedelta(days=self._max_backfill_days)
        accepted: list[HealthMetric] = []
        dropped = 0
        for metric in metrics:
            if metric.recorded_at < cutoff:
                dropped += 1
                continue
            accepted.append(metric)

        # Update last_delivery_at on the link row.
        link_row["last_delivery_at"] = datetime.now(UTC).isoformat()
        await self._storage.put(_LINKS_COLLECTION, str(link_row.get("_id") or ""), link_row)

        persisted = await self.ingest_metrics(user_id, backend_name, accepted)
        return WebhookResult(status="ok", received=persisted, dropped=dropped)

    async def _lookup_link_by_token_hash(
        self,
        token_hash: str,
    ) -> dict[str, Any] | None:
        assert self._storage is not None
        rows = await self._storage.query(
            Query(
                collection=_LINKS_COLLECTION,
                filters=[
                    Filter(field="webhook_token_hash", op=FilterOp.EQ, value=token_hash),
                ],
                limit=1,
            )
        )
        if not rows:
            return None
        return rows[0]

    # ── Read API (HealthProvider) ────────────────────────────────────

    async def read_metrics(
        self,
        user_id: str,
        metric_types: list[MetricType],
        since: datetime,
        until: datetime,
    ) -> list[HealthMetric]:
        """Return metrics for ``user_id`` in [since, until). Owner-only
        path — caller must be the user (or SYSTEM, or hold
        ``health-admin``)."""
        user_ctx = get_current_user()
        self._require_read(user_ctx, user_id)
        if self._storage is None:
            return []
        filters = [
            Filter(field="user_id", op=FilterOp.EQ, value=user_id),
            Filter(field="recorded_at", op=FilterOp.GTE, value=since.isoformat()),
            Filter(field="recorded_at", op=FilterOp.LT, value=until.isoformat()),
        ]
        rows = await self._storage.query(
            Query(
                collection=_METRICS_COLLECTION,
                filters=filters,
                sort=[SortField(field="recorded_at", descending=False)],
            )
        )
        type_set = {t.value for t in metric_types} if metric_types else None
        out: list[HealthMetric] = []
        for raw in rows:
            if type_set is not None and str(raw.get("metric_type")) not in type_set:
                continue
            try:
                out.append(HealthMetric.from_dict(raw))
            except Exception:
                logger.debug("Skipping malformed metric row", exc_info=True)
        # Belt-and-suspenders owner-only check on the returned rows.
        return [m for m in out if m.user_id == user_id]

    async def latest_metric(
        self,
        user_id: str,
        metric_type: MetricType,
    ) -> HealthMetric | None:
        user_ctx = get_current_user()
        self._require_read(user_ctx, user_id)
        if self._storage is None:
            return None
        rows = await self._storage.query(
            Query(
                collection=_METRICS_COLLECTION,
                filters=[
                    Filter(field="user_id", op=FilterOp.EQ, value=user_id),
                    Filter(
                        field="metric_type",
                        op=FilterOp.EQ,
                        value=metric_type.value,
                    ),
                ],
                sort=[SortField(field="recorded_at", descending=True)],
                limit=1,
            )
        )
        if not rows:
            return None
        try:
            return HealthMetric.from_dict(rows[0])
        except Exception:
            logger.debug("Latest-metric row malformed", exc_info=True)
            return None

    async def aggregate(
        self,
        user_id: str,
        metric_type: MetricType,
        period: AggregatePeriod,
        since: datetime,
        until: datetime,
        aggregator: AggregatorKind | None = None,
    ) -> list[HealthAggregate]:
        user_ctx = get_current_user()
        self._require_read(user_ctx, user_id)
        # Hard cap to defend against unbounded windows pulling millions
        # of rows into memory (e.g. a 10-year ``health_trend`` request
        # against minute-resolution heart rate). When we hit the cap
        # the aggregate is partial — emit a WARN so callers know.
        if self._storage is not None:
            total = await self._storage.count(
                Query(
                    collection=_METRICS_COLLECTION,
                    filters=[
                        Filter(field="user_id", op=FilterOp.EQ, value=user_id),
                        Filter(
                            field="metric_type",
                            op=FilterOp.EQ,
                            value=metric_type.value,
                        ),
                        Filter(
                            field="recorded_at",
                            op=FilterOp.GTE,
                            value=since.isoformat(),
                        ),
                        Filter(
                            field="recorded_at",
                            op=FilterOp.LT,
                            value=until.isoformat(),
                        ),
                    ],
                )
            )
            if total > _MAX_AGGREGATE_ROWS:
                logger.warning(
                    "Health: aggregate(%s, %s) hit %d-row cap (%d total) "
                    "— window truncated to most-recent rows",
                    user_id,
                    metric_type.value,
                    _MAX_AGGREGATE_ROWS,
                    total,
                )
        rows = await self.read_metrics(user_id, [metric_type], since, until)
        if len(rows) > _MAX_AGGREGATE_ROWS:
            rows = rows[-_MAX_AGGREGATE_ROWS:]
        if not rows:
            return []
        agg_kind = aggregator or DEFAULT_AGGREGATOR.get(metric_type, AggregatorKind.AVG)
        # Bucket by period.
        buckets: dict[tuple[datetime, datetime], list[HealthMetric]] = defaultdict(list)
        for m in rows:
            start, end = _bucket_window(m.recorded_at, period)
            buckets[(start, end)].append(m)
        out: list[HealthAggregate] = []
        for (start, end), bucket in sorted(buckets.items()):
            value = _apply_aggregator(agg_kind, [m.value for m in bucket])
            unit = bucket[0].unit
            out.append(
                HealthAggregate(
                    user_id=user_id,
                    metric_type=metric_type,
                    period_start=start,
                    period_end=end,
                    period=period,
                    sample_count=len(bucket),
                    aggregator=agg_kind,
                    value=value,
                    unit=unit,
                )
            )
        return out

    async def latest_daily_summary(
        self,
        user_id: str,
        on_or_before: datetime | None = None,
    ) -> DailySummary | None:
        user_ctx = get_current_user()
        self._require_read(user_ctx, user_id)
        if self._storage is None:
            return None
        cached = self._summary_cache.get(user_id)
        if cached is not None and on_or_before is None:
            self._summary_cache.move_to_end(user_id)
            return cached
        filters = [Filter(field="user_id", op=FilterOp.EQ, value=user_id)]
        if on_or_before is not None:
            filters.append(
                Filter(
                    field="local_date",
                    op=FilterOp.LTE,
                    value=on_or_before.date().isoformat(),
                )
            )
        rows = await self._storage.query(
            Query(
                collection=_SUMMARIES_COLLECTION,
                filters=filters,
                sort=[SortField(field="local_date", descending=True)],
                limit=1,
            )
        )
        if not rows:
            return None
        try:
            summary = DailySummary.from_dict(rows[0])
            if on_or_before is None:
                self._cache_summary(user_id, summary)
            return summary
        except Exception:
            return None

    def _cache_summary(self, user_id: str, summary: DailySummary) -> None:
        """Insert into ``_summary_cache`` with LRU eviction."""
        self._summary_cache[user_id] = summary
        self._summary_cache.move_to_end(user_id)
        while len(self._summary_cache) > _SUMMARY_CACHE_MAX:
            self._summary_cache.popitem(last=False)

    async def health_brief_for_greeting(
        self,
        user_id: str,
    ) -> GreetingBrief:
        """Structured snapshot for the greeting integration. Returns
        ``GreetingBrief.empty(user_id)`` for a user with no
        ``health_links`` rows."""
        if self._storage is None:
            return GreetingBrief.empty(user_id)
        link_rows = await self._storage.query(
            Query(
                collection=_LINKS_COLLECTION,
                filters=[Filter(field="user_id", op=FilterOp.EQ, value=user_id)],
                limit=1,
            )
        )
        if not link_rows:
            return GreetingBrief.empty(user_id)

        # Run as SYSTEM so the read passes the auth check; this method
        # is invoked from the greeting service which holds its own
        # SYSTEM context already, but defensive.
        sleep = await self.latest_metric(user_id, MetricType.SLEEP_DURATION)
        sleep_eff = await self.latest_metric(user_id, MetricType.SLEEP_EFFICIENCY)
        weight = await self.latest_metric(user_id, MetricType.WEIGHT)
        resting_hr = await self.latest_metric(user_id, MetricType.HEART_RATE_RESTING)
        steps = await self.latest_metric(user_id, MetricType.STEPS)
        latest_summary = await self.latest_daily_summary(user_id)
        flags = list(latest_summary.flags) if latest_summary is not None else []

        return GreetingBrief(
            user_id=user_id,
            has_data=True,
            sleep_hours=(sleep.value / 3600.0) if sleep else None,
            sleep_efficiency=sleep_eff.value if sleep_eff else None,
            steps_today_so_far=int(steps.value) if steps else None,
            weight_latest=weight.value if weight else None,
            weight_unit=weight.unit if weight else MetricUnit.KG,
            resting_hr_latest=resting_hr.value if resting_hr else None,
            flags=flags,
        )

    # ── GreetingContextProvider protocol ──────────────────────────────

    @property
    def greeting_context_id(self) -> str:
        return "health"

    @property
    def greeting_context_label(self) -> str:
        return "Health"

    async def greeting_context(self, user_id: str) -> "GreetingContext | None":
        """Return a labeled health fact for the greeting, or None.

        Wraps ``health_brief_for_greeting`` and formats the structured
        brief into prose. Returns None if the brief has no data."""
        from gilbert.interfaces.greeting import GreetingContext

        try:
            brief = await self.health_brief_for_greeting(user_id)
        except Exception:
            logger.debug("HealthService.greeting_context failed for %s", user_id, exc_info=True)
            return None
        if brief is None or not brief.has_data:
            return None
        parts: list[str] = []
        if brief.sleep_hours is not None:
            parts.append(f"Last night's sleep: {brief.sleep_hours:.1f}h.")
        if brief.steps_today_so_far is not None:
            parts.append(f"Steps today so far: {brief.steps_today_so_far:,}.")
        if brief.weight_latest is not None:
            parts.append(f"Latest weight: {brief.weight_latest:g} {brief.weight_unit.value}.")
        if brief.resting_hr_latest is not None:
            parts.append(f"Latest resting HR: {brief.resting_hr_latest:g} bpm.")
        if brief.flags:
            parts.append(f"Flags: {', '.join(brief.flags)}.")
        if not parts:
            return None
        return GreetingContext(provider_id="health", label="Health", prose=" ".join(parts))

    # ── Cross-user read (audit-logged) ───────────────────────────────

    async def admin_read_metrics(
        self,
        actor_ctx: UserContext,
        target_user_id: str,
        metric_types: list[MetricType],
        since: datetime,
        until: datetime,
    ) -> list[HealthMetric]:
        """Cross-user read with audit + target notification.

        Caller MUST hold ``HEALTH_ADMIN_ROLE``. Persists a
        ``health_audit`` row, fires ``health.access.audit`` on the bus,
        and sends an in-product notification to the target user. Per
        spec §6.1.1 — durable record even if the target is offline.
        """
        if not self._is_health_admin(actor_ctx) and actor_ctx.user_id != UserContext.SYSTEM.user_id:
            raise PermissionError(
                f"User {actor_ctx.user_id!r} lacks role {HEALTH_ADMIN_ROLE!r} "
                f"for cross-user read"
            )
        # Direct query — bypass the read filter since the role gate
        # above already authorized the cross-user access. Running this
        # in the admin's outer context (rather than spawning a SYSTEM-
        # context subtask) keeps the audit row's ``actor_user_id``
        # correct AND avoids a misleading no-op create_task call.
        if self._storage is None:
            return []
        filters = [
            Filter(field="user_id", op=FilterOp.EQ, value=target_user_id),
            Filter(field="recorded_at", op=FilterOp.GTE, value=since.isoformat()),
            Filter(field="recorded_at", op=FilterOp.LT, value=until.isoformat()),
        ]
        rows = await self._storage.query(
            Query(
                collection=_METRICS_COLLECTION,
                filters=filters,
                sort=[SortField(field="recorded_at", descending=False)],
            )
        )
        type_set = {t.value for t in metric_types} if metric_types else None
        metrics: list[HealthMetric] = []
        for raw in rows:
            if type_set is not None and str(raw.get("metric_type")) not in type_set:
                continue
            try:
                metrics.append(HealthMetric.from_dict(raw))
            except Exception:
                continue

        # Persist the audit row.
        await self._record_audit(
            kind="cross_user_read",
            actor_user_id=actor_ctx.user_id,
            target_user_id=target_user_id,
            metric_types=[t.value for t in metric_types],
            period_start=since,
            period_end=until,
        )

        # Notify the target user (NotificationProvider may be absent —
        # the audit row + bus event remain the durable record).
        try:
            if self._notifications is not None:
                period_human = _format_period_human(since, until)
                # Human-friendly metric names per spec §6.1.1 — the
                # notification body MUST read "sleep, weight" not
                # "sleep_duration, weight".
                types_summary = metric_types_human_summary(metric_types)
                await self._notifications.notify_user(
                    user_id=target_user_id,
                    message=(
                        f"An admin viewed your health metrics ({types_summary}, "
                        f"{period_human}). If you weren't expecting this, "
                        f"contact your administrator."
                    ),
                    urgency=NotificationUrgency.NORMAL,
                    source="health",
                    source_ref={
                        "actor_user_id": actor_ctx.user_id,
                        "action_url": "/account/health/audit-log",
                    },
                )
            else:
                logger.warning(
                    "gilbert.health.audit.notify_skipped: target=%s actor=%s",
                    target_user_id,
                    actor_ctx.user_id,
                )
        except Exception:
            logger.exception(
                "Failed to notify target %s of cross-user read by %s",
                target_user_id,
                actor_ctx.user_id,
            )

        return metrics

    async def _record_audit(
        self,
        *,
        kind: str,
        actor_user_id: str,
        target_user_id: str,
        metric_types: list[str] | None = None,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
        request_id: str = "",
        backends: list[str] | None = None,
    ) -> None:
        if self._storage is None:
            return
        accessed_at = datetime.now(UTC).isoformat()
        row_id = f"hau_{uuid.uuid4().hex[:16]}"
        row = {
            "_id": row_id,
            "id": row_id,
            "kind": kind,
            "actor_user_id": actor_user_id,
            "target_user_id": target_user_id,
            "accessed_at": accessed_at,
            "metric_types": list(metric_types or []),
            "backends": list(backends or []),
            "period_start": period_start.isoformat() if period_start else "",
            "period_end": period_end.isoformat() if period_end else "",
            "request_id": request_id,
        }
        await self._storage.put(_AUDIT_COLLECTION, row_id, row)
        audit_logger.info(
            "%s actor=%s target=%s types=%s backends=%s period=%s..%s",
            kind,
            actor_user_id,
            target_user_id,
            ",".join(metric_types or []) or "*",
            ",".join(backends or []) or "-",
            period_start.isoformat() if period_start else "-",
            period_end.isoformat() if period_end else "-",
        )
        await self._publish_event(
            "health.access.audit",
            {
                "actor_user_id": actor_user_id,
                "target_user_id": target_user_id,
                "kind": kind,
                "accessed_at": accessed_at,
                "metric_types": list(metric_types or []),
                "backends": list(backends or []),
                "period_start": period_start.isoformat() if period_start else "",
                "period_end": period_end.isoformat() if period_end else "",
            },
        )

    # ── Right to delete ──────────────────────────────────────────────

    async def preview_delete_all(self, user_id: str) -> dict[str, Any]:
        """Return counts the SPA confirmation dialog renders. Owner-only."""
        if self._storage is None:
            return {
                "metric_count": 0,
                "earliest_recorded_at": "",
                "latest_recorded_at": "",
                "backends": [],
                "summaries_count": 0,
                "audit_count": 0,
            }
        metrics = await self._storage.query(
            Query(
                collection=_METRICS_COLLECTION,
                filters=[Filter(field="user_id", op=FilterOp.EQ, value=user_id)],
                sort=[SortField(field="recorded_at", descending=False)],
            )
        )
        backends = sorted({str(r.get("backend") or "") for r in metrics if r.get("backend")})
        earliest = (
            str(metrics[0].get("recorded_at") or "") if metrics else ""
        )
        latest = (
            str(metrics[-1].get("recorded_at") or "") if metrics else ""
        )
        summaries = await self._storage.count(
            Query(
                collection=_SUMMARIES_COLLECTION,
                filters=[Filter(field="user_id", op=FilterOp.EQ, value=user_id)],
            )
        )
        audit = await self._storage.count(
            Query(
                collection=_AUDIT_COLLECTION,
                filters=[Filter(field="target_user_id", op=FilterOp.EQ, value=user_id)],
            )
        )
        return {
            "metric_count": len(metrics),
            "earliest_recorded_at": earliest,
            "latest_recorded_at": latest,
            "backends": backends,
            "summaries_count": int(summaries),
            "audit_count": int(audit),
        }

    async def delete_all_my_data(
        self,
        user_id: str,
        *,
        actor_kind: str = "self_delete_all",
    ) -> dict[str, Any]:
        """Cascade delete every metric, summary, and link row for the
        user. Persists a ``health_audit`` row that survives the
        cascade. For OAuth backends, calls ``backend.disconnect`` to
        revoke upstream BEFORE deleting the local link row.

        Returns ``{deleted_metrics, disconnected_backends,
        upstream_revoke_failures}``.
        """
        if self._storage is None:
            raise RuntimeError("HealthService not started")

        # Snapshot counts for the audit row.
        preview = await self.preview_delete_all(user_id)

        # 1. Disconnect every linked backend (revoke upstream first).
        link_rows = await self._storage.query(
            Query(
                collection=_LINKS_COLLECTION,
                filters=[Filter(field="user_id", op=FilterOp.EQ, value=user_id)],
            )
        )
        disconnected: list[str] = []
        revoke_failures: list[str] = []
        for raw in link_rows:
            backend_name = str(raw.get("backend_name") or "")
            backend = self._backends.get(backend_name)
            if backend is not None:
                try:
                    await backend.disconnect(user_id)
                except Exception as exc:
                    logger.warning(
                        "Health: disconnect failed for %s/%s: %s — local "
                        "cleanup proceeds",
                        user_id,
                        backend_name,
                        exc,
                    )
                    revoke_failures.append(backend_name)
            disconnected.append(backend_name)

        # 2. Delete every health_metrics, health_daily_summaries,
        #    health_links row for the user.
        deleted_metrics = await self._storage.delete_query(
            Query(
                collection=_METRICS_COLLECTION,
                filters=[Filter(field="user_id", op=FilterOp.EQ, value=user_id)],
            )
        )
        await self._storage.delete_query(
            Query(
                collection=_SUMMARIES_COLLECTION,
                filters=[Filter(field="user_id", op=FilterOp.EQ, value=user_id)],
            )
        )
        await self._storage.delete_query(
            Query(
                collection=_LINKS_COLLECTION,
                filters=[Filter(field="user_id", op=FilterOp.EQ, value=user_id)],
            )
        )

        # 3. Audit row (survives the cascade).
        if actor_kind == "self_delete_all":
            # Per spec §4.5: ``metric_types`` is ``list[MetricType]``
            # and MUST be empty for ``self_delete_all`` (no specific
            # types were "read"). Backends are recorded under a
            # separate ``backends`` field — overloading metric_types
            # with backend names corrupts downstream analytics.
            await self._record_audit(
                kind="self_delete_all",
                actor_user_id=user_id,
                target_user_id=user_id,
                metric_types=[],
                backends=sorted(preview.get("backends") or []),
            )

        # 4. Cascade-delete event.
        await self._publish_event(
            "health.metric.deleted",
            {
                "user_id": user_id,
                "count": int(deleted_metrics),
                "scope": "user-deleted",
            },
        )

        # 5. Drop in-memory caches for the user.
        self._summary_cache.pop(user_id, None)
        self._per_user_write_caps.pop(user_id, None)
        for key in list(self._ingest_locks.keys()):
            if key[0] == user_id:
                self._ingest_locks.pop(key, None)
        for key in list(self._consecutive_auth_failures.keys()):
            if key[0] == user_id:
                self._consecutive_auth_failures.pop(key, None)

        return {
            "deleted_metrics": int(deleted_metrics),
            "disconnected_backends": disconnected,
            "upstream_revoke_failures": revoke_failures,
        }

    async def rotate_webhook_token(
        self,
        user_id: str,
        backend_name: str,
    ) -> dict[str, Any]:
        """Rotate a per-user webhook token. Returns the raw token ONCE
        + the derived ``webhook_url`` so the SPA can show "copy this
        URL into your iOS Shortcut" — only the SHA-256 hash is
        persisted. The previous token is revoked immediately.

        Refuses for backends without ``supports_push``. Sends an
        URGENT notification reminding the user to update any device
        posting with the old token.
        """
        if self._storage is None:
            return {"status": "error", "message": "storage unavailable"}
        backend = self._backends.get(backend_name)
        if backend is None:
            return {"status": "error", "message": f"unknown backend: {backend_name}"}
        if not backend.supports_push:
            return {
                "status": "error",
                "message": (
                    f"Token rotation does not apply to backend '{backend_name}'"
                ),
            }
        raw_token = secrets.token_urlsafe(48)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        last4 = raw_token[-4:]
        link_id = f"{user_id}/{backend_name}"
        existing = await self._storage.get(_LINKS_COLLECTION, link_id) or {
            "_id": link_id,
            "user_id": user_id,
            "backend_name": backend_name,
            "enabled": True,
            "created_at": datetime.now(UTC).isoformat(),
        }
        existing["webhook_token_hash"] = token_hash
        existing["webhook_token_last4"] = last4
        existing["enabled"] = True
        existing["updated_at"] = datetime.now(UTC).isoformat()
        await self._storage.put(_LINKS_COLLECTION, link_id, existing)

        base = self._public_base_url.rstrip("/")
        webhook_url = f"{base}/webhook/health/{raw_token}" if base else ""

        # URGENT notification — rotation creates a silent-dead-drop
        # bug if the user has a device posting with the old token.
        if self._notifications is not None:
            try:
                await self._notifications.notify_user(
                    user_id=user_id,
                    message=(
                        f"Health webhook token rotated for {backend_name}. "
                        "Update your device with the new URL."
                    ),
                    urgency=NotificationUrgency.URGENT,
                    source="health",
                )
            except Exception:
                logger.debug("Token-rotate notify failed", exc_info=True)
        return {
            "status": "ok",
            "raw_token": raw_token,
            "webhook_url": webhook_url,
        }

    async def begin_link(self, user_id: str, backend_name: str) -> LinkStartResult:
        """Service-level begin_link wrapper.

        Looks up the backend by name (or returns ``status="error"``)
        and delegates to ``backend.begin_link(user_id)``.
        """
        backend = self._backends.get(backend_name)
        if backend is None:
            return LinkStartResult(
                status="error",
                message=f"unknown backend: {backend_name}",
            )
        return await backend.begin_link(user_id)

    async def complete_link(
        self,
        user_id: str,
        backend_name: str,
        payload: dict[str, Any],
    ) -> LinkCompleteResult:
        """Service-level complete_link wrapper.

        Delegates to the backend's ``complete_link`` and, on success,
        publishes ``health.link.connected``.
        """
        backend = self._backends.get(backend_name)
        if backend is None:
            return LinkCompleteResult(
                status="error",
                message=f"unknown backend: {backend_name}",
            )
        result = await backend.complete_link(user_id, payload)
        if result.status == "ok":
            await self._publish_event(
                "health.link.connected",
                {"user_id": user_id, "backend": backend_name},
            )
        return result

    async def consume_oauth_state(
        self,
        state: str,
        backend_name: str,
        caller_user_id: str,
    ) -> dict[str, Any]:
        """Validate + one-shot consume an OAuth state row.

        Returns ``{"status": "ok", "user_id": ...}`` on success,
        ``{"status": "expired"}`` on TTL expiry,
        ``{"status": "user_mismatch"}`` if the caller's session doesn't
        match the originating user (confused-deputy defense),
        ``{"status": "backend_mismatch"}`` if the state was minted for
        a different backend, ``{"status": "already"}`` if the row was
        already consumed, ``{"status": "missing"}`` if it doesn't
        exist.

        Atomicity: an in-memory ``asyncio.Lock`` keyed on the state
        token serializes the read-then-write so two concurrent
        callbacks for the same state cannot both observe ``consumed_at
        == None`` and both succeed. The TTL check is INSIDE the locked
        section so a state expiring mid-consume returns "expired"
        deterministically. The lock dict only grows; entries persist
        for process lifetime which is fine for per-state objects (each
        state is one-shot — we never come back to the same lock).
        """
        if self._storage is None:
            return {"status": "missing"}
        lock = self._oauth_state_locks.setdefault(state, asyncio.Lock())
        async with lock:
            row = await self._storage.get(_OAUTH_STATE_COLLECTION, state)
            if row is None:
                return {"status": "missing"}
            expires_at_raw = row.get("expires_at") or ""
            try:
                expires_at = datetime.fromisoformat(str(expires_at_raw))
            except ValueError:
                return {"status": "missing"}
            if datetime.now(UTC) > expires_at:
                await self._storage.delete(_OAUTH_STATE_COLLECTION, state)
                return {"status": "expired"}
            if str(row.get("user_id") or "") != caller_user_id:
                return {"status": "user_mismatch"}
            if str(row.get("backend_name") or "") != backend_name:
                return {"status": "backend_mismatch"}
            if row.get("consumed_at"):
                return {"status": "already"}
            row["consumed_at"] = datetime.now(UTC).isoformat()
            await self._storage.put(_OAUTH_STATE_COLLECTION, state, row)
            return {"status": "ok", "user_id": str(row.get("user_id") or "")}

    async def record_oauth_error(
        self,
        user_id: str,
        backend_name: str,
        error: str,
    ) -> None:
        """Record an OAuth-denied error on the link row."""
        if self._storage is None:
            return
        link_id = f"{user_id}/{backend_name}"
        existing = await self._storage.get(_LINKS_COLLECTION, link_id) or {
            "_id": link_id,
            "user_id": user_id,
            "backend_name": backend_name,
            "enabled": False,
        }
        existing["last_sync_error"] = f"oauth: {error}"
        await self._storage.put(_LINKS_COLLECTION, link_id, existing)

    async def list_admin_user_counts(self) -> list[dict[str, Any]]:
        """Per-user aggregate (counts only, no values, no per-day
        breakdown). Used by the admin overview /api/health/admin/users
        route."""
        if self._storage is None:
            return []
        rows = await self._storage.query(
            Query(
                collection=_LINKS_COLLECTION,
                sort=[SortField(field="user_id", descending=False)],
            )
        )
        by_user: dict[str, dict[str, Any]] = {}
        for r in rows:
            uid = str(r.get("user_id") or "")
            if not uid:
                continue
            info = by_user.setdefault(
                uid,
                {
                    "user_id": uid,
                    "has_data": False,
                    "backends": [],
                    "last_ingested_at": "",
                },
            )
            info["backends"].append(str(r.get("backend_name") or ""))
            for stamp_key in ("last_delivery_at", "last_sync_at"):
                stamp = str(r.get(stamp_key) or "")
                if stamp:
                    info["has_data"] = True
                    if stamp > info["last_ingested_at"]:
                        info["last_ingested_at"] = stamp
        return list(by_user.values())

    async def list_admin_audit_log(self, limit: int = 500) -> list[dict[str, Any]]:
        """Read every ``health_audit`` row (admin / health-admin)."""
        if self._storage is None:
            return []
        return await self._storage.query(
            Query(
                collection=_AUDIT_COLLECTION,
                sort=[SortField(field="accessed_at", descending=True)],
                limit=limit,
            )
        )

    async def list_my_audit_log(
        self,
        user_id: str,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Per-user audit log: every row where ``target_user_id``
        matches the calling user, sorted most recent first."""
        if self._storage is None:
            return []
        return await self._storage.query(
            Query(
                collection=_AUDIT_COLLECTION,
                filters=[
                    Filter(field="target_user_id", op=FilterOp.EQ, value=user_id),
                ],
                sort=[SortField(field="accessed_at", descending=True)],
                limit=limit,
            )
        )

    async def list_user_links(self, user_id: str) -> list[dict[str, Any]]:
        """Return every ``health_links`` row for the user, secrets
        redacted."""
        if self._storage is None:
            return []
        rows = await self._storage.query(
            Query(
                collection=_LINKS_COLLECTION,
                filters=[Filter(field="user_id", op=FilterOp.EQ, value=user_id)],
            )
        )
        return [_redact_link(r) for r in rows]

    async def user_has_active_links(self, user_id: str) -> bool:
        """Return ``True`` iff the user has at least one row in
        ``health_links`` (regardless of ``enabled``).

        Implements ``HealthLinkProvider`` so the web nav can answer
        "should /health render?" without reaching into the service's
        private storage attribute. Per spec §17.3 the conditional nav
        gate doesn't care about ``enabled`` — a disabled link still
        means the user has used the feature and the page is meaningful.
        """
        if self._storage is None:
            return False
        try:
            count = await self._storage.count(
                Query(
                    collection=_LINKS_COLLECTION,
                    filters=[
                        Filter(field="user_id", op=FilterOp.EQ, value=user_id),
                    ],
                )
            )
        except Exception:
            logger.debug(
                "user_has_active_links: storage count failed",
                exc_info=True,
            )
            return False
        return count > 0

    async def disconnect_backend(self, user_id: str, backend_name: str) -> bool:
        """Remove a single ``health_links`` row + revoke upstream.

        Historical metrics are NOT deleted — the user opted in once,
        the data is theirs. Returns True on success.
        """
        if self._storage is None:
            return False
        backend = self._backends.get(backend_name)
        upstream_ok = True
        if backend is not None:
            try:
                await backend.disconnect(user_id)
            except Exception as exc:
                logger.warning(
                    "Health: disconnect failed for %s/%s: %s — local "
                    "cleanup proceeds",
                    user_id,
                    backend_name,
                    exc,
                )
                upstream_ok = False
        link_id = f"{user_id}/{backend_name}"
        await self._storage.delete(_LINKS_COLLECTION, link_id)
        await self._publish_event(
            "health.link.disconnected",
            {
                "user_id": user_id,
                "backend": backend_name,
                "upstream_revoked": upstream_ok,
            },
        )
        return True

    # ── auth.user.deleted cascade ────────────────────────────────────

    async def _on_user_deleted(self, event: Event) -> None:
        user_id = str(event.data.get("user_id") or "")
        if not user_id:
            return
        try:
            # No audit row on system-cascade deletes (per spec §6.6).
            await self.delete_all_my_data(user_id, actor_kind="cascade")
            audit_logger.info(
                "cascade actor=system target=%s reason=auth.user.deleted",
                user_id,
            )
        except Exception:
            logger.exception(
                "Health: cascade delete failed for user %s", user_id
            )

    async def _on_daily_summary_event(self, event: Event) -> None:
        user_id = str(event.data.get("user_id") or "")
        if user_id:
            self._summary_cache.pop(user_id, None)

    # ── Scheduler primitives ─────────────────────────────────────────

    async def _run_per_user(
        self,
        user_ids: list[str],
        work: Callable[[str], Awaitable[None]],
        *,
        concurrency: int,
        label: str,
        tz_by_user: dict[str, str] | None = None,
    ) -> None:
        """Run ``work(user_id)`` for each user with bounded concurrency.

        Each task gets its own copy of contextvars so the per-task
        ``set_current_user`` doesn't leak to siblings. ``tz_by_user``
        propagates the resolved per-user timezone into the SYSTEM
        identity so downstream code reads ``user_ctx.tz`` instead of
        re-querying ``users_svc`` (which the spec pins as the
        canonical path).
        """
        if not user_ids:
            return
        sem = asyncio.Semaphore(max(1, concurrency))
        tz_map = tz_by_user or {}

        async def _one(uid: str) -> None:
            async with sem:
                ctx = contextvars.copy_context()

                async def _runner() -> None:
                    set_current_user(
                        _system_acting_for(uid, tz=tz_map.get(uid))
                    )
                    try:
                        await work(uid)
                    except Exception:
                        logger.exception("%s failed for user %s", label, uid)

                await asyncio.create_task(_runner(), context=ctx)

        await asyncio.gather(*[_one(uid) for uid in user_ids])

    async def _resolve_user_tz(self, user_id: str) -> str:
        """Best-effort resolve the user's IANA TZ string.

        Reads from the optional ``users`` capability when present.
        Returns ``""`` (= "fall back to UTC") if the user is unknown
        or hasn't set a TZ. The caller decides how to render that.
        """
        if self._resolver is None:
            return ""
        users_svc = self._resolver.get_capability("users")
        if users_svc is None:
            return ""
        try:
            user = await users_svc.get_user(user_id)  # type: ignore[attr-defined]
        except Exception:
            return ""
        if isinstance(user, dict):
            return str(user.get("tz") or "")
        # Future: typed User objects via UsersProvider — read .tz attr.
        tz_attr = getattr(user, "tz", None)
        return str(tz_attr or "")

    async def _users_due_at_current_hour(self) -> tuple[list[str], dict[str, str]]:
        """Return ``(user_ids, tz_by_user)`` whose
        ``daily_summary_local_hour`` matches the current wall-clock
        hour in their TZ.

        The TZ map is returned alongside the user_ids so the scheduler
        can populate ``UserContext.tz`` on the per-task SYSTEM identity
        rather than each work fn re-querying the users service.

        Note on resolution: the scheduler tick fires at every UTC top
        of hour, so a user in a fractional-offset TZ (IST = UTC+5:30,
        Newfoundland = -3:30, Nepal = +5:45, ...) gets their summary
        at the half-hour past their configured ``daily_summary_local_hour``
        rather than on the exact hour. Spec doesn't strictly require
        minute-level precision; the alternative — a 30-min tick —
        doubles scheduler overhead for every user globally.
        """
        if self._storage is None:
            return [], {}
        link_rows = await self._storage.query(
            Query(
                collection=_LINKS_COLLECTION,
                filters=[Filter(field="enabled", op=FilterOp.EQ, value=True)],
            )
        )
        seen: set[str] = set()
        due: list[str] = []
        tz_by_user: dict[str, str] = {}
        for raw in link_rows:
            user_id = str(raw.get("user_id") or "")
            if not user_id or user_id in seen:
                continue
            seen.add(user_id)
            tz_name = await self._resolve_user_tz(user_id)
            tz_by_user[user_id] = tz_name
            try:
                tz = ZoneInfo(tz_name) if tz_name else ZoneInfo("UTC")
            except ZoneInfoNotFoundError:
                tz = ZoneInfo("UTC")
            local_now = datetime.now(tz)
            target_hour = self._daily_summary_local_hour
            override = raw.get("daily_summary_local_hour")
            if isinstance(override, int):
                target_hour = override
            if local_now.hour == target_hour:
                due.append(user_id)
        return due, tz_by_user

    async def _run_daily_summary_tick(self) -> None:
        if self._storage is None:
            return
        try:
            user_ids, tz_by_user = await self._users_due_at_current_hour()
        except Exception:
            logger.exception("Health: daily-summary tick failed to load users")
            return
        await self._run_per_user(
            user_ids,
            self._compute_and_persist_summary,
            concurrency=self._daily_summary_concurrency,
            label="daily-summary",
            tz_by_user=tz_by_user,
        )

    def _make_pull_sync_callback(
        self,
        backend_name: str,
    ) -> Callable[[], Awaitable[None]]:
        async def _callback() -> None:
            await self._run_pull_sync(backend_name)

        return _callback

    async def _run_pull_sync(self, backend_name: str) -> None:
        if self._storage is None:
            return
        backend = self._backends.get(backend_name)
        if backend is None or not backend.supports_pull:
            return
        rows = await self._storage.query(
            Query(
                collection=_LINKS_COLLECTION,
                filters=[
                    Filter(field="backend_name", op=FilterOp.EQ, value=backend_name),
                    Filter(field="enabled", op=FilterOp.EQ, value=True),
                ],
            )
        )
        user_ids = [str(r.get("user_id") or "") for r in rows if r.get("user_id")]

        async def _sync_one(uid: str) -> None:
            try:
                metrics = await backend.sync(uid)
                await self.ingest_metrics(uid, backend_name, metrics)
                await self._update_last_sync(uid, backend_name, error="")
                self._consecutive_auth_failures.pop((uid, backend_name), None)
            except HealthBackendRateLimitError as exc:
                # Don't sleep inside the semaphore slot — pinning a
                # slot for ``retry_after_seconds`` (Withings's typical
                # 300s) starves other users on the next tick. Just
                # record the error and let the next scheduled tick
                # pick the user up; the bucket has typically refilled
                # by then.
                await self._update_last_sync(
                    uid,
                    backend_name,
                    error=f"rate-limited; retry after {exc.retry_after_seconds}s",
                )
            except HealthBackendAuthError as exc:
                await self._update_last_sync(uid, backend_name, error=f"auth: {exc}")
                key = (uid, backend_name)
                self._consecutive_auth_failures[key] = (
                    self._consecutive_auth_failures.get(key, 0) + 1
                )
                if self._consecutive_auth_failures[key] >= _AUTH_FAILURE_THRESHOLD:
                    await self._set_link_disabled(uid, backend_name)
            except (HealthBackendTransientError, HealthBackendNotFoundError) as exc:
                await self._update_last_sync(uid, backend_name, error=str(exc))
            except Exception as exc:
                await self._update_last_sync(uid, backend_name, error=str(exc))

        await self._run_per_user(
            user_ids,
            _sync_one,
            concurrency=self._pull_sync_concurrency,
            label=f"pull-sync.{backend_name}",
        )

    async def _update_last_sync(
        self,
        user_id: str,
        backend_name: str,
        *,
        error: str,
    ) -> None:
        if self._storage is None:
            return
        link_id = f"{user_id}/{backend_name}"
        row = await self._storage.get(_LINKS_COLLECTION, link_id)
        if row is None:
            return
        row["last_sync_at"] = datetime.now(UTC).isoformat()
        row["last_sync_error"] = error
        await self._storage.put(_LINKS_COLLECTION, link_id, row)

    async def _set_link_disabled(self, user_id: str, backend_name: str) -> None:
        if self._storage is None:
            return
        link_id = f"{user_id}/{backend_name}"
        row = await self._storage.get(_LINKS_COLLECTION, link_id)
        if row is None:
            return
        row["enabled"] = False
        await self._storage.put(_LINKS_COLLECTION, link_id, row)
        logger.warning(
            "Health: auto-disabled link %s/%s after %d consecutive auth failures",
            user_id,
            backend_name,
            _AUTH_FAILURE_THRESHOLD,
        )

    async def _run_retention_prune(self) -> None:
        if self._storage is None:
            return
        if self._retention_days > 0:
            cutoff = (datetime.now(UTC) - timedelta(days=self._retention_days)).isoformat()
            removed_metrics = await self._storage.delete_query(
                Query(
                    collection=_METRICS_COLLECTION,
                    filters=[Filter(field="recorded_at", op=FilterOp.LT, value=cutoff)],
                )
            )
            removed_summaries = await self._storage.delete_query(
                Query(
                    collection=_SUMMARIES_COLLECTION,
                    filters=[Filter(field="generated_at", op=FilterOp.LT, value=cutoff)],
                )
            )
            if removed_metrics or removed_summaries:
                logger.info(
                    "Health retention pruned %d metrics and %d summaries",
                    removed_metrics,
                    removed_summaries,
                )
                await self._publish_event(
                    "health.metric.deleted",
                    {
                        "user_id": "",
                        "count": int(removed_metrics),
                        "scope": "retention",
                    },
                )
        if self._audit_retention_days > 0:
            audit_cutoff = (
                datetime.now(UTC) - timedelta(days=self._audit_retention_days)
            ).isoformat()
            await self._storage.delete_query(
                Query(
                    collection=_AUDIT_COLLECTION,
                    filters=[Filter(field="accessed_at", op=FilterOp.LT, value=audit_cutoff)],
                )
            )

    async def _run_oauth_state_gc(self) -> None:
        if self._storage is None:
            return
        cutoff = datetime.now(UTC).isoformat()
        removed = await self._storage.delete_query(
            Query(
                collection=_OAUTH_STATE_COLLECTION,
                filters=[Filter(field="expires_at", op=FilterOp.LT, value=cutoff)],
            )
        )
        if removed:
            logger.debug("Health: GC'd %d expired oauth_state row(s)", removed)

    async def _compute_and_persist_summary(self, user_id: str) -> None:
        """Per-spec §10.1. Computes the user's local "yesterday" window
        via zoneinfo (DST-correct: spring-forward 23h, fall-back 25h
        by construction), reads metrics, computes flags in code, then
        calls AI for the prose summary."""
        if self._storage is None:
            return
        # Resolve user TZ. Read from the per-task ``UserContext.tz``
        # populated by ``_run_per_user`` when invoked from the
        # scheduler. Fall back to ``users_svc.get_user`` when the
        # context-injected value is missing — covers ad-hoc callers
        # (e.g. tests) that didn't go through the scheduler path.
        ctx_user = get_current_user()
        tz_name = ctx_user.tz or ""
        if not tz_name:
            tz_name = await self._resolve_user_tz(user_id)
        try:
            tz = ZoneInfo(tz_name) if tz_name else ZoneInfo("UTC")
        except ZoneInfoNotFoundError:
            tz = ZoneInfo("UTC")

        local_now = datetime.now(tz)
        local_today_0 = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        local_yest_0 = local_today_0 - timedelta(days=1)
        window_start = local_yest_0.astimezone(UTC)
        window_end = local_today_0.astimezone(UTC)

        metrics = await self.read_metrics(
            user_id,
            [],  # all types
            window_start,
            window_end,
        )

        snapshot = _headline_snapshot(metrics)
        flags = await self._compute_flags(user_id, snapshot, window_end)

        prose = _structured_prose(snapshot)

        summary_text = ""
        if self._ai is not None and prose:
            try:
                response = await self._ai.complete_one_shot(
                    messages=[Message(role=MessageRole.USER, content=prose)],
                    system_prompt=self._summary_prompt,
                    profile_name=self._ai_profile,
                )
                summary_text = (response.message.content or "").strip()
            except Exception:
                logger.exception("Health: AI summary failed for %s", user_id)
                summary_text = ""

        local_date_iso = local_yest_0.date().isoformat()
        row_id = f"{user_id}/{local_date_iso}"
        summary = DailySummary(
            user_id=user_id,
            local_date=local_date_iso,
            summary_text=summary_text,
            metrics_snapshot={k: float(v) for k, v in snapshot.items()},
            flags=flags,
            generated_at=datetime.now(UTC),
        )
        await self._storage.put(_SUMMARIES_COLLECTION, row_id, summary.to_dict())
        self._cache_summary(user_id, summary)
        await self._publish_event(
            "health.daily.summary",
            {
                "user_id": user_id,
                "local_date": local_date_iso,
                "summary_text": summary_text,
                "flags": list(flags),
                "metrics_snapshot": dict(summary.metrics_snapshot),
            },
        )

    async def _compute_flags(
        self,
        user_id: str,
        snapshot: dict[str, float],
        window_end: datetime,
    ) -> list[str]:
        """Compute the §15 flag vocabulary IN CODE — never from AI."""
        flags: list[str] = []

        # low_sleep — last N consecutive nights below threshold.
        nights = self._flag_low_sleep_consecutive_nights
        threshold_secs = self._flag_low_sleep_hours * 3600.0
        window_start = window_end - timedelta(days=nights + 1)
        sleep_metrics = await self.read_metrics(
            user_id, [MetricType.SLEEP_DURATION], window_start, window_end
        )
        if sleep_metrics:
            durations = sorted(
                [m for m in sleep_metrics],
                key=lambda m: m.recorded_at,
            )
            if len(durations) >= nights:
                last_n = durations[-nights:]
                if all(m.value < threshold_secs for m in last_n):
                    flags.append("low_sleep")

        # sedentary — last N days steps below threshold.
        days = self._flag_sedentary_consecutive_days
        steps_window_start = window_end - timedelta(days=days + 1)
        step_metrics = await self.read_metrics(
            user_id, [MetricType.STEPS], steps_window_start, window_end
        )
        if step_metrics:
            sorted_steps = sorted(step_metrics, key=lambda m: m.recorded_at)
            if len(sorted_steps) >= days:
                last_n = sorted_steps[-days:]
                if all(m.value < self._flag_sedentary_steps for m in last_n):
                    flags.append("sedentary")

        # weight_drift — magnitude over the configured window.
        wd_window_start = window_end - timedelta(
            days=self._flag_weight_drift_window_days
        )
        weights = await self.read_metrics(
            user_id, [MetricType.WEIGHT], wd_window_start, window_end
        )
        if len(weights) >= 2:
            weights_sorted = sorted(weights, key=lambda m: m.recorded_at)
            drift = weights_sorted[-1].value - weights_sorted[0].value
            if abs(drift) >= self._flag_weight_drift_kg:
                flags.append("weight_drift")

        return flags

    # ── ConfigAction (test storage) ──────────────────────────────────

    def config_actions(self) -> list[ConfigAction]:
        return []

    async def invoke_config_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        return ConfigActionResult(status="error", message=f"Unknown action: {key}")

    # ── ToolProvider ─────────────────────────────────────────────────

    def get_tools(
        self,
        user_ctx: UserContext | None = None,
    ) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        return _build_tool_definitions()

    async def execute_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> str | ToolOutput:
        match name:
            case "health_now":
                return await self._tool_health_now(arguments)
            case "latest_health":
                return await self._tool_latest_health(arguments)
            case "health_summary":
                return await self._tool_health_summary(arguments)
            case "health_trend":
                return await self._tool_health_trend(arguments)
            case "sleep_last_night":
                return await self._tool_sleep_last_night(arguments)
            case "steps_today":
                return await self._tool_steps_today(arguments)
            case "weight_trend":
                return await self._tool_weight_trend(arguments)
            case "health_links":
                return await self._tool_health_links(arguments)
            case "health_delete_my_data":
                return await self._tool_health_delete_my_data(arguments)
            case _:
                raise KeyError(f"Unknown tool: {name}")

    def _resolve_user_ctx_from_args(self, arguments: dict[str, Any]) -> UserContext:
        """Build a ``UserContext`` from injected ``_user_*`` args.

        Tools NEVER fall back to ``get_current_user()`` — missing
        ``_user_id`` is a hard error so a buggy tool runner can't
        accidentally leak data across users.
        """
        user_id = str(arguments.get("_user_id") or "")
        if not user_id:
            raise PermissionError(
                "missing _user_id — health tools require an authenticated user"
            )
        roles_raw = arguments.get("_user_roles") or []
        if isinstance(roles_raw, (list, tuple, set, frozenset)):
            roles = frozenset(str(r) for r in roles_raw)
        else:
            roles = frozenset()
        tz_raw = arguments.get("_user_tz")
        return UserContext(
            user_id=user_id,
            email=str(arguments.get("_user_email") or ""),
            display_name=str(arguments.get("_user_name") or user_id),
            roles=roles,
            tz=str(tz_raw) if isinstance(tz_raw, str) and tz_raw else None,
        )

    # ── AI tool implementations ──────────────────────────────────────

    async def _tool_health_now(self, arguments: dict[str, Any]) -> str:
        user_ctx = self._resolve_user_ctx_from_args(arguments)
        set_current_user(user_ctx)
        sleep = await self.latest_metric(user_ctx.user_id, MetricType.SLEEP_DURATION)
        steps = await self.latest_metric(user_ctx.user_id, MetricType.STEPS)
        weight = await self.latest_metric(user_ctx.user_id, MetricType.WEIGHT)
        rest_hr = await self.latest_metric(user_ctx.user_id, MetricType.HEART_RATE_RESTING)

        snapshot: dict[str, Any] = {
            "sleep_hours": (sleep.value / 3600.0) if sleep else None,
            "steps_today": int(steps.value) if steps else None,
            "weight": weight.value if weight else None,
            "weight_unit": weight.unit.value if weight else None,
            "resting_hr": rest_hr.value if rest_hr else None,
        }

        prose = _structured_prose_now(snapshot)
        ai_text = ""
        if self._ai is not None and prose:
            try:
                response = await self._ai.complete_one_shot(
                    messages=[Message(role=MessageRole.USER, content=prose)],
                    system_prompt=self._summary_prompt,
                    profile_name=self._ai_profile,
                )
                ai_text = (response.message.content or "").strip()
            except Exception:
                logger.debug("health_now: AI call failed", exc_info=True)
        return json.dumps({"snapshot": snapshot, "summary": ai_text})

    async def _tool_latest_health(self, arguments: dict[str, Any]) -> str:
        user_ctx = self._resolve_user_ctx_from_args(arguments)
        set_current_user(user_ctx)
        type_raw = str(arguments.get("metric") or arguments.get("metric_type") or "")
        try:
            metric_type = MetricType(type_raw)
        except ValueError:
            return f"Unknown metric: {type_raw}"
        latest = await self.latest_metric(user_ctx.user_id, metric_type)
        if latest is None:
            return f"No data for {metric_type.value}."
        return json.dumps(
            {
                "metric_type": latest.metric_type.value,
                "value": latest.value,
                "unit": latest.unit.value,
                "recorded_at": latest.recorded_at.isoformat(),
                "backend": latest.backend,
            }
        )

    async def _tool_health_summary(self, arguments: dict[str, Any]) -> str:
        user_ctx = self._resolve_user_ctx_from_args(arguments)
        set_current_user(user_ctx)
        period_raw = str(arguments.get("period") or "yesterday").lower()
        if period_raw in ("yesterday", ""):
            summary = await self.latest_daily_summary(user_ctx.user_id)
            if summary is None:
                return "No daily summary yet."
            return json.dumps(
                {
                    "local_date": summary.local_date,
                    "summary_text": summary.summary_text,
                    "flags": list(summary.flags),
                }
            )
        # Today / week — generate ad-hoc via prompt.
        now_utc = datetime.now(UTC)
        if period_raw == "today":
            since = now_utc - timedelta(hours=24)
        elif period_raw == "week":
            since = now_utc - timedelta(days=7)
        else:
            return f"Unknown period: {period_raw}"
        rows = await self.read_metrics(user_ctx.user_id, [], since, now_utc)
        snapshot = _headline_snapshot(rows)
        prose = _structured_prose(snapshot)
        if self._ai is None or not prose:
            return json.dumps({"snapshot": snapshot, "summary_text": ""})
        try:
            response = await self._ai.complete_one_shot(
                messages=[Message(role=MessageRole.USER, content=prose)],
                system_prompt=self._summary_prompt,
                profile_name=self._ai_profile,
            )
            return json.dumps(
                {
                    "snapshot": snapshot,
                    "summary_text": (response.message.content or "").strip(),
                }
            )
        except Exception:
            return json.dumps({"snapshot": snapshot, "summary_text": ""})

    async def _tool_health_trend(self, arguments: dict[str, Any]) -> str:
        user_ctx = self._resolve_user_ctx_from_args(arguments)
        set_current_user(user_ctx)
        type_raw = str(arguments.get("metric") or arguments.get("metric_type") or "")
        try:
            metric_type = MetricType(type_raw)
        except ValueError:
            return f"Unknown metric: {type_raw}"
        weeks = int(arguments.get("weeks") or 4)
        until = datetime.now(UTC)
        since = until - timedelta(weeks=weeks)
        metrics = await self.read_metrics(user_ctx.user_id, [metric_type], since, until)
        if not metrics:
            return f"No data for {metric_type.value} in the last {weeks} week(s)."
        sorted_metrics = sorted(metrics, key=lambda m: m.recorded_at)
        points = [
            {
                "date": m.recorded_at.date().isoformat(),
                "value": float(m.value),
            }
            for m in sorted_metrics
        ]
        prose = (
            f"metric: {metric_type.value}\n"
            f"unit: {sorted_metrics[0].unit.value}\n"
            f"points: {json.dumps(points)}\n"
            f"window_days: {(until - since).days}\n"
        )
        ai_text = ""
        if self._ai is not None:
            try:
                response = await self._ai.complete_one_shot(
                    messages=[Message(role=MessageRole.USER, content=prose)],
                    system_prompt=self._trend_prompt,
                    profile_name=self._ai_profile,
                )
                ai_text = (response.message.content or "").strip()
            except Exception:
                logger.debug("health_trend: AI failed", exc_info=True)
        return json.dumps(
            {
                "metric_type": metric_type.value,
                "unit": sorted_metrics[0].unit.value,
                "weeks": weeks,
                "points": points,
                "trend_text": ai_text,
            }
        )

    async def _tool_sleep_last_night(self, arguments: dict[str, Any]) -> str:
        user_ctx = self._resolve_user_ctx_from_args(arguments)
        set_current_user(user_ctx)
        sleep = await self.latest_metric(user_ctx.user_id, MetricType.SLEEP_DURATION)
        eff = await self.latest_metric(user_ctx.user_id, MetricType.SLEEP_EFFICIENCY)
        if sleep is None:
            return "No sleep data yet."
        return json.dumps(
            {
                "duration_seconds": sleep.value,
                "duration_hours": round(sleep.value / 3600.0, 2),
                "efficiency": eff.value if eff else None,
                "recorded_at": sleep.recorded_at.isoformat(),
            }
        )

    async def _tool_steps_today(self, arguments: dict[str, Any]) -> str:
        user_ctx = self._resolve_user_ctx_from_args(arguments)
        set_current_user(user_ctx)
        latest = await self.latest_metric(user_ctx.user_id, MetricType.STEPS)
        if latest is None:
            return "No step data yet."
        return json.dumps(
            {
                "steps": int(latest.value),
                "recorded_at": latest.recorded_at.isoformat(),
            }
        )

    async def _tool_weight_trend(self, arguments: dict[str, Any]) -> str:
        # Reuse the trend implementation with a weight default.
        merged = {**arguments, "metric": MetricType.WEIGHT.value}
        return await self._tool_health_trend(merged)

    async def _tool_health_links(self, arguments: dict[str, Any]) -> str:
        user_ctx = self._resolve_user_ctx_from_args(arguments)
        set_current_user(user_ctx)
        if self._storage is None:
            return "[]"
        rows = await self._storage.query(
            Query(
                collection=_LINKS_COLLECTION,
                filters=[
                    Filter(field="user_id", op=FilterOp.EQ, value=user_ctx.user_id)
                ],
            )
        )
        out = []
        for raw in rows:
            out.append(
                {
                    "backend": str(raw.get("backend_name") or ""),
                    "enabled": bool(raw.get("enabled", False)),
                    "last_sync_at": str(raw.get("last_sync_at") or ""),
                    "last_sync_error": str(raw.get("last_sync_error") or ""),
                    "last_delivery_at": str(raw.get("last_delivery_at") or ""),
                }
            )
        return json.dumps(out)

    async def _tool_health_delete_my_data(
        self,
        arguments: dict[str, Any],
    ) -> str | ToolOutput:
        user_ctx = self._resolve_user_ctx_from_args(arguments)
        set_current_user(user_ctx)
        # Per spec §6.6, the model cannot one-shot the delete. Return
        # the preview-then-confirm UIBlock; the user clicks DELETE
        # explicitly to proceed.
        confirm_value = str(arguments.get("confirm") or "")
        preview = await self.preview_delete_all(user_ctx.user_id)
        summary_lines = [
            f"Metrics: {preview['metric_count']}",
            f"Daily summaries: {preview['summaries_count']}",
            f"Audit rows: {preview['audit_count']}",
            f"Backends connected: {', '.join(preview['backends']) or 'none'}",
            "",
            "We will revoke Gilbert's upstream OAuth grants and delete every "
            "measurement we cached locally. Withings continues to retain the "
            "data on your behalf — to delete it from Withings, use Withings's "
            "own account-deletion flow.",
        ]
        summary_text = (
            f"About to delete {preview['metric_count']} metric reading(s) "
            "and revoke every connected backend. Confirm with the literal "
            "word DELETE."
        )

        async def _execute() -> str:
            if confirm_value != "DELETE":
                return (
                    "Refusing to delete: confirm must be the literal word "
                    "DELETE."
                )
            result = await self.delete_all_my_data(user_ctx.user_id)
            return json.dumps(result)

        return await confirm_or_execute(
            confirm=(confirm_value == "DELETE"),
            tool_name="health_delete_my_data",
            title="Delete all your health data",
            summary=summary_text,
            summary_lines=summary_lines,
            arguments={**arguments, "confirm": "DELETE"},
            execute=_execute,
            confirm_label="DELETE",
        )

    # ── WS RPC handlers ──────────────────────────────────────────────

    def get_ws_handlers(self) -> dict[str, Any]:
        return {
            "health.links.list": self._ws_links_list,
            "health.summary.latest": self._ws_summary_latest,
            "health.metrics.read": self._ws_metrics_read,
            "health.delete_all.preview": self._ws_delete_all_preview,
        }

    async def _ws_links_list(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any] | None:
        user_id = conn.user_ctx.user_id
        if self._storage is None:
            return {"type": "health.links.list.result", "ref": frame.get("id"), "items": []}
        rows = await self._storage.query(
            Query(
                collection=_LINKS_COLLECTION,
                filters=[Filter(field="user_id", op=FilterOp.EQ, value=user_id)],
            )
        )
        items = [_redact_link(r) for r in rows]
        return {"type": "health.links.list.result", "ref": frame.get("id"), "items": items}

    async def _ws_summary_latest(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any] | None:
        set_current_user(conn.user_ctx)
        summary = await self.latest_daily_summary(conn.user_ctx.user_id)
        return {
            "type": "health.summary.latest.result",
            "ref": frame.get("id"),
            "summary": summary.to_dict() if summary else None,
        }

    async def _ws_metrics_read(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any] | None:
        set_current_user(conn.user_ctx)
        types = [MetricType(t) for t in frame.get("metric_types", []) if t in MetricType.__members__.values()]
        since_raw = frame.get("since") or ""
        until_raw = frame.get("until") or ""
        if not since_raw or not until_raw:
            until = datetime.now(UTC)
            since = until - timedelta(days=7)
        else:
            since = datetime.fromisoformat(str(since_raw))
            until = datetime.fromisoformat(str(until_raw))
        rows = await self.read_metrics(conn.user_ctx.user_id, types, since, until)
        return {
            "type": "health.metrics.read.result",
            "ref": frame.get("id"),
            "items": [r.to_dict() for r in rows],
        }

    async def _ws_delete_all_preview(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any] | None:
        preview = await self.preview_delete_all(conn.user_ctx.user_id)
        return {
            "type": "health.delete_all.preview.result",
            "ref": frame.get("id"),
            "preview": preview,
        }


# ── Helpers ──────────────────────────────────────────────────────────


def _system_acting_for(user_id: str, *, tz: str | None = None) -> UserContext:
    """Return a SYSTEM-acting-for-target identity for scheduler work.

    The actor remains ``UserContext.SYSTEM`` (so the audit log
    correctly shows scheduler activity as ``actor="system"``);
    ``metadata["target_user_id"]`` carries the target so service code
    paths that need it can pull it via metadata. ``tz`` propagates the
    target user's timezone so scheduler code paths read it from the
    typed ``UserContext`` rather than poking at ``users_svc`` again.
    """
    return replace(
        UserContext.SYSTEM,
        metadata={"target_user_id": user_id},
        tz=tz,
    )


def _canonical_iso(when: datetime) -> str:
    """Return a stable UTC ISO-8601 string for ``when``.

    Python's ``datetime.isoformat`` is not canonical: ``+00:00`` vs
    ``Z`` suffix, optional microseconds, naive vs aware all round-trip
    differently. This helper coerces to aware-UTC then formats so two
    semantically identical timestamps from different sources compare
    equal.
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(UTC).isoformat()


def _redact_link(row: dict[str, Any]) -> dict[str, Any]:
    """Project a link row for client display; never expose secrets."""
    return {
        "backend_name": str(row.get("backend_name") or ""),
        "enabled": bool(row.get("enabled", False)),
        "last_sync_at": str(row.get("last_sync_at") or ""),
        "last_sync_error": str(row.get("last_sync_error") or ""),
        "last_delivery_at": str(row.get("last_delivery_at") or ""),
        "webhook_token_last4": str(row.get("webhook_token_last4") or ""),
        "supports_webhook": bool(row.get("webhook_token_hash")),
    }


def _bucket_window(
    when: datetime,
    period: AggregatePeriod,
) -> tuple[datetime, datetime]:
    if period is AggregatePeriod.HOUR:
        start = when.replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=1)
    elif period is AggregatePeriod.DAY:
        start = when.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif period is AggregatePeriod.WEEK:
        # ISO week: monday midnight UTC.
        d = when.date()
        monday = d - timedelta(days=d.weekday())
        start = datetime.combine(monday, datetime.min.time(), tzinfo=when.tzinfo or UTC)
        end = start + timedelta(days=7)
    else:  # MONTH
        start = when.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
    return (start, end)


def _apply_aggregator(kind: AggregatorKind, values: list[float]) -> float:
    if not values:
        return 0.0
    if kind is AggregatorKind.SUM:
        return float(sum(values))
    if kind is AggregatorKind.AVG:
        return float(sum(values) / len(values))
    if kind is AggregatorKind.MIN:
        return float(min(values))
    if kind is AggregatorKind.MAX:
        return float(max(values))
    return float(values[-1])  # LATEST


def _headline_snapshot(metrics: list[HealthMetric]) -> dict[str, float]:
    """Pull headline values for the daily summary prompt."""
    snapshot: dict[str, float] = {}
    by_type: dict[str, list[HealthMetric]] = defaultdict(list)
    for m in metrics:
        by_type[m.metric_type.value].append(m)
    for kind, rows in by_type.items():
        rows.sort(key=lambda r: r.recorded_at)
        try:
            mt = MetricType(kind)
            agg = DEFAULT_AGGREGATOR.get(mt, AggregatorKind.AVG)
        except ValueError:
            agg = AggregatorKind.LATEST
        snapshot[kind] = _apply_aggregator(agg, [r.value for r in rows])
    return snapshot


def _structured_prose(snapshot: dict[str, float]) -> str:
    """Brief structured-prose for the model.

    Pre-formatting beats handing the model raw JSON — fewer field-name
    fumbles, stronger prompt adherence.
    """
    parts: list[str] = []
    sleep_secs = snapshot.get(MetricType.SLEEP_DURATION.value)
    if sleep_secs is not None:
        h = int(sleep_secs // 3600)
        m = int((sleep_secs % 3600) // 60)
        parts.append(f"Sleep: {h}h {m}m.")
    eff = snapshot.get(MetricType.SLEEP_EFFICIENCY.value)
    if eff is not None:
        parts.append(f"Sleep efficiency: {round(eff * 100)}%.")
    steps = snapshot.get(MetricType.STEPS.value)
    if steps is not None:
        parts.append(f"Steps: {int(steps):,}.")
    weight = snapshot.get(MetricType.WEIGHT.value)
    if weight is not None:
        parts.append(f"Weight: {weight:g} kg.")
    rhr = snapshot.get(MetricType.HEART_RATE_RESTING.value)
    if rhr is not None:
        parts.append(f"Resting HR: {rhr:g} bpm.")
    bp_sys = snapshot.get(MetricType.BLOOD_PRESSURE_SYS.value)
    bp_dia = snapshot.get(MetricType.BLOOD_PRESSURE_DIA.value)
    if bp_sys is not None and bp_dia is not None:
        parts.append(f"BP: {int(bp_sys)}/{int(bp_dia)} mmHg.")
    if not parts:
        return ""
    return " ".join(parts)


def _structured_prose_now(snapshot: dict[str, Any]) -> str:
    parts: list[str] = []
    sh = snapshot.get("sleep_hours")
    if sh is not None:
        parts.append(f"Sleep: {sh:.2f}h.")
    steps = snapshot.get("steps_today")
    if steps is not None:
        parts.append(f"Steps so far today: {int(steps):,}.")
    w = snapshot.get("weight")
    wu = snapshot.get("weight_unit") or "kg"
    if w is not None:
        parts.append(f"Latest weight: {float(w):g} {wu}.")
    rhr = snapshot.get("resting_hr")
    if rhr is not None:
        parts.append(f"Latest resting HR: {float(rhr):g} bpm.")
    if not parts:
        return ""
    return " ".join(parts)


def _format_period_human(since: datetime, until: datetime) -> str:
    """Return a short human description ('today', 'yesterday', '7 days', ...)."""
    delta = until - since
    if delta <= timedelta(hours=24):
        return "today"
    if delta <= timedelta(hours=48):
        return "yesterday"
    if delta <= timedelta(days=7):
        return "the last 7 days"
    return f"{since.date().isoformat()} to {until.date().isoformat()}"


# ── Tool definitions ─────────────────────────────────────────────────


def _build_tool_definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="health_now",
            slash_group="health",
            slash_command="now",
            slash_help="What do you know about me right now?",
            description=(
                "Catch-all 'how am I doing?' tool — latest sleep, today's "
                "steps so far, latest weight, latest resting HR, plus a "
                "short non-clinical sentence for the 24h window ending now."
            ),
            parameters=[],
            required_role="user",
            parallel_safe=True,
        ),
        ToolDefinition(
            name="latest_health",
            slash_group="health",
            slash_command="latest",
            slash_help="Latest reading of a metric: /health latest <metric>",
            description=(
                "Return the most-recent reading of a single metric for the "
                "current user. Pure data lookup — no AI call."
            ),
            parameters=[
                ToolParameter(
                    name="metric",
                    type=ToolParameterType.STRING,
                    description="Metric type (e.g. weight, steps, sleep_duration).",
                )
            ],
            required_role="user",
            parallel_safe=True,
        ),
        ToolDefinition(
            name="health_summary",
            slash_group="health",
            slash_command="summary",
            slash_help="Summarize a period: /health summary [today|yesterday|week]",
            description=(
                "Return a non-clinical natural-language summary of the user's "
                "health metrics for a period. Default period is yesterday — "
                "consumes the pre-computed daily summary row. Users wanting "
                "'today so far' should use /health now instead."
            ),
            parameters=[
                ToolParameter(
                    name="period",
                    type=ToolParameterType.STRING,
                    description="today | yesterday | week",
                    required=False,
                    default="yesterday",
                    enum=["today", "yesterday", "week"],
                )
            ],
            required_role="user",
            parallel_safe=True,
        ),
        ToolDefinition(
            name="health_trend",
            slash_group="health",
            slash_command="trend",
            slash_help="Trend of a metric: /health trend <metric> [4]",
            description=(
                "Describe direction / rate / consistency of a single metric "
                "over a window of weeks. Non-clinical — no causes, no "
                "diagnoses, no actions."
            ),
            parameters=[
                ToolParameter(
                    name="metric",
                    type=ToolParameterType.STRING,
                    description="Metric type (e.g. weight, hrv, steps).",
                ),
                ToolParameter(
                    name="weeks",
                    type=ToolParameterType.INTEGER,
                    description="Window in weeks (default 4).",
                    required=False,
                    default=4,
                ),
            ],
            required_role="user",
            parallel_safe=True,
        ),
        ToolDefinition(
            name="sleep_last_night",
            slash_group="health",
            slash_command="sleep",
            slash_help="Last night's sleep: /health sleep",
            description="Convenience: last night's sleep duration + efficiency. Pure data.",
            parameters=[],
            required_role="user",
            parallel_safe=True,
        ),
        ToolDefinition(
            name="steps_today",
            slash_group="health",
            slash_command="steps",
            slash_help="Today's step count: /health steps",
            description="Convenience: today's step count. Pure data.",
            parameters=[],
            required_role="user",
            parallel_safe=True,
        ),
        ToolDefinition(
            name="weight_trend",
            slash_group="health",
            slash_command="weight",
            slash_help="Weight slope: /health weight [4]",
            description="Convenience: weight slope over the last N weeks (default 4).",
            parameters=[
                ToolParameter(
                    name="weeks",
                    type=ToolParameterType.INTEGER,
                    description="Window in weeks.",
                    required=False,
                    default=4,
                )
            ],
            required_role="user",
            parallel_safe=True,
        ),
        ToolDefinition(
            name="health_links",
            slash_group="health",
            slash_command="links",
            slash_help="Connected health sources: /health links",
            description=(
                "List the user's connected health backends with status "
                "(last sync, last delivery, errors). Pure data."
            ),
            parameters=[],
            required_role="user",
            parallel_safe=True,
        ),
        ToolDefinition(
            name="health_delete_my_data",
            # Deliberately NO slash_command — destructive, must require
            # the two-step preview/confirm UIBlock flow per spec §9 / §22.
            description=(
                "Erase everything Gilbert has cached about the calling user's "
                "health: every metric, every daily summary, every link to a "
                "connected backend. Returns a preview block first; the actual "
                "delete only fires when the user types DELETE explicitly."
            ),
            parameters=[
                ToolParameter(
                    name="confirm",
                    type=ToolParameterType.STRING,
                    description=(
                        "Type DELETE to actually delete. Anything else "
                        "returns the preview / confirmation block."
                    ),
                    required=False,
                    default="",
                )
            ],
            required_role="user",
        ),
    ]
