"""Health interfaces — backend ABC, dataclasses, capability protocol, helpers.

Defines the contract for health-data plugins (Apple Health, Withings,
HKWebhook, future Garmin/Oura) and the read-side capability protocol
that consumers (greeting, proposals, agents) use to access metrics
without depending on the concrete ``HealthService``.

Imports nothing from ``core/``, ``integrations/``, ``storage/``, or
``web/``. Stays in the interfaces layer per the layer dependency rules.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import ConfigParam

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────


HEALTH_ADMIN_ROLE = "health-admin"
"""Dedicated role for cross-user health reads. NOT auto-granted to any
user (including the built-in admin); operators grant it explicitly via
``/roles/users``. Holders see other users' metrics; reads are
audit-logged AND notify the target."""


# Caps for ``HealthMetric.extra``. Per-spec §4.1: a webhook backend
# cannot funnel arbitrary blobs into ``extra``; each backend declares
# its own whitelist of keys and the parser caps lengths/sizes here.
EXTRA_MAX_KEYS = 16
EXTRA_MAX_KEY_LEN = 64
EXTRA_MAX_VALUE_LEN = 256
EXTRA_MAX_TOTAL_BYTES = 1024


# ── Enums ────────────────────────────────────────────────────────────


class MetricType(StrEnum):
    """Standardized set of metric kinds.

    Extensible — add a new entry here when a backend introduces a metric
    we want to surface; never smuggle backend-specific metrics in as
    opaque strings. Backends that emit a metric whose ``MetricType``
    they don't recognize set ``metric_type`` to the new enum value; the
    service persists them as long as they parse. Tools that don't know
    the metric simply won't surface it.
    """

    SLEEP_DURATION = "sleep_duration"
    SLEEP_EFFICIENCY = "sleep_efficiency"
    SLEEP_DEEP = "sleep_deep"
    SLEEP_REM = "sleep_rem"
    SLEEP_AWAKE = "sleep_awake"
    STEPS = "steps"
    DISTANCE = "distance"
    ACTIVE_MINUTES = "active_minutes"
    CALORIES_BURNED = "calories_burned"
    HEART_RATE_RESTING = "heart_rate_resting"
    HEART_RATE_AVG = "heart_rate_avg"
    HRV = "hrv"
    SPO2 = "spo2"
    WEIGHT = "weight"
    BODY_FAT = "body_fat"
    LEAN_MASS = "lean_mass"
    BMI = "bmi"
    BLOOD_PRESSURE_SYS = "blood_pressure_sys"
    BLOOD_PRESSURE_DIA = "blood_pressure_dia"
    BODY_TEMPERATURE = "body_temperature"
    RESPIRATORY_RATE = "respiratory_rate"
    VO2_MAX = "vo2_max"


class MetricUnit(StrEnum):
    """Canonical units. Stored alongside the value so display code
    doesn't have to memorize MetricType→unit mappings."""

    SECONDS = "s"
    MINUTES = "min"
    HOURS = "h"
    METERS = "m"
    KILOMETERS = "km"
    KCAL = "kcal"
    BPM = "bpm"
    MS = "ms"
    KG = "kg"
    LB = "lb"
    PERCENT = "percent"
    MMHG = "mmhg"
    CELSIUS = "C"
    FAHRENHEIT = "F"
    BREATHS_PER_MIN = "br/min"
    ML_KG_MIN = "ml/kg/min"
    COUNT = "count"


class AggregatePeriod(StrEnum):
    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


class AggregatorKind(StrEnum):
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"
    LATEST = "latest"


# Default aggregator per metric — must stay aligned with how backends
# emit each metric (steps are cumulative-per-day from Apple Health, so
# SUM across a day window double-counts; LATEST captures the day's
# total). Tests assert every enum value has an explicit entry.
DEFAULT_AGGREGATOR: dict[MetricType, AggregatorKind] = {
    MetricType.SLEEP_DURATION: AggregatorKind.SUM,
    MetricType.SLEEP_EFFICIENCY: AggregatorKind.AVG,
    MetricType.SLEEP_DEEP: AggregatorKind.SUM,
    MetricType.SLEEP_REM: AggregatorKind.SUM,
    MetricType.SLEEP_AWAKE: AggregatorKind.SUM,
    MetricType.STEPS: AggregatorKind.LATEST,
    MetricType.DISTANCE: AggregatorKind.LATEST,
    MetricType.ACTIVE_MINUTES: AggregatorKind.LATEST,
    MetricType.CALORIES_BURNED: AggregatorKind.LATEST,
    MetricType.HEART_RATE_RESTING: AggregatorKind.AVG,
    MetricType.HEART_RATE_AVG: AggregatorKind.AVG,
    MetricType.HRV: AggregatorKind.AVG,
    MetricType.SPO2: AggregatorKind.AVG,
    MetricType.WEIGHT: AggregatorKind.LATEST,
    MetricType.BODY_FAT: AggregatorKind.LATEST,
    MetricType.LEAN_MASS: AggregatorKind.LATEST,
    MetricType.BMI: AggregatorKind.LATEST,
    MetricType.BLOOD_PRESSURE_SYS: AggregatorKind.AVG,
    MetricType.BLOOD_PRESSURE_DIA: AggregatorKind.AVG,
    MetricType.BODY_TEMPERATURE: AggregatorKind.AVG,
    MetricType.RESPIRATORY_RATE: AggregatorKind.AVG,
    MetricType.VO2_MAX: AggregatorKind.LATEST,
}


# ── Dataclasses ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class HealthMetric:
    """A single health reading.

    All readings are immutable once persisted. Storage uses
    ``(user_id, metric_type, recorded_at)`` as the natural key — a
    second push for the same triple replaces the existing row
    (last-write-wins by ``ingested_at``).
    """

    id: str
    user_id: str
    backend: str
    metric_type: MetricType
    value: float
    unit: MetricUnit
    recorded_at: datetime
    ingested_at: datetime
    source_event_id: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "backend": self.backend,
            "metric_type": self.metric_type.value,
            "value": float(self.value),
            "unit": self.unit.value,
            "recorded_at": self.recorded_at.isoformat(),
            "ingested_at": self.ingested_at.isoformat(),
            "source_event_id": self.source_event_id,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HealthMetric:
        recorded_raw = data.get("recorded_at") or ""
        ingested_raw = data.get("ingested_at") or ""
        recorded_at = (
            datetime.fromisoformat(recorded_raw)
            if isinstance(recorded_raw, str) and recorded_raw
            else datetime.now(UTC)
        )
        ingested_at = (
            datetime.fromisoformat(ingested_raw)
            if isinstance(ingested_raw, str) and ingested_raw
            else datetime.now(UTC)
        )
        raw_extra = data.get("extra") or {}
        extra: dict[str, str] = {}
        if isinstance(raw_extra, dict):
            extra = {str(k): str(v) for k, v in raw_extra.items()}
        return cls(
            id=str(data.get("id") or data.get("_id") or ""),
            user_id=str(data.get("user_id", "")),
            backend=str(data.get("backend", "")),
            metric_type=MetricType(str(data.get("metric_type"))),
            value=float(data.get("value", 0.0)),
            unit=MetricUnit(str(data.get("unit"))),
            recorded_at=recorded_at,
            ingested_at=ingested_at,
            source_event_id=str(data.get("source_event_id", "")),
            extra=extra,
        )


@dataclass(frozen=True)
class HealthAggregate:
    """A computed summary over a window. Computed at query time; not
    persisted (so backfill doesn't require cache invalidation)."""

    user_id: str
    metric_type: MetricType
    period_start: datetime
    period_end: datetime
    period: AggregatePeriod
    sample_count: int
    aggregator: AggregatorKind
    value: float
    unit: MetricUnit


@dataclass(frozen=True)
class DailySummary:
    """Per-(user, local_date) summary persisted by the daily-summary job.

    ``summary_text`` is AI-generated and DISPLAY-ONLY — never parsed as
    instructions, never used to compute ``flags``. ``flags`` is a small
    fixed vocabulary computed in code from ``metrics_snapshot``.
    """

    user_id: str
    local_date: str  # YYYY-MM-DD
    summary_text: str
    metrics_snapshot: dict[str, float]
    flags: list[str]
    generated_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "local_date": self.local_date,
            "summary_text": self.summary_text,
            "metrics_snapshot": dict(self.metrics_snapshot),
            "flags": list(self.flags),
            "generated_at": self.generated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DailySummary:
        gen_raw = data.get("generated_at") or ""
        generated_at = (
            datetime.fromisoformat(gen_raw)
            if isinstance(gen_raw, str) and gen_raw
            else datetime.now(UTC)
        )
        snap_raw = data.get("metrics_snapshot") or {}
        snapshot: dict[str, float] = {}
        if isinstance(snap_raw, dict):
            for k, v in snap_raw.items():
                try:
                    snapshot[str(k)] = float(v)
                except (TypeError, ValueError):
                    continue
        flags_raw = data.get("flags") or []
        flags = [str(f) for f in flags_raw] if isinstance(flags_raw, list) else []
        return cls(
            user_id=str(data.get("user_id", "")),
            local_date=str(data.get("local_date", "")),
            summary_text=str(data.get("summary_text", "")),
            metrics_snapshot=snapshot,
            flags=flags,
            generated_at=generated_at,
        )


@dataclass(frozen=True)
class GreetingBrief:
    """Structured snapshot used by the greeting integration.

    The greeting prompt template receives these as variables and is
    instructed to weave a brief health observation into its greeting
    in its own voice (subject to the same non-clinical constraints as
    the daily summary). Returns ``empty(user_id)`` for users with no
    ``health_links`` rows so the greeting model gets a clear absent-
    data signal rather than zeros.
    """

    user_id: str
    has_data: bool
    sleep_hours: float | None
    sleep_efficiency: float | None
    steps_today_so_far: int | None
    weight_latest: float | None
    weight_unit: MetricUnit
    resting_hr_latest: float | None
    flags: list[str]

    @classmethod
    def empty(cls, user_id: str) -> GreetingBrief:
        return cls(
            user_id=user_id,
            has_data=False,
            sleep_hours=None,
            sleep_efficiency=None,
            steps_today_so_far=None,
            weight_latest=None,
            weight_unit=MetricUnit.KG,
            resting_hr_latest=None,
            flags=[],
        )


@dataclass(frozen=True)
class LinkStartResult:
    """Returned by ``HealthBackend.begin_link``.

    OAuth backends populate ``open_url`` with the provider's authorize
    URL; push backends populate ``webhook_url``. ``followup_action_key``
    supports the existing two-phase ConfigAction flow when needed.
    """

    status: Literal["ok", "pending", "error"]
    message: str = ""
    open_url: str = ""
    webhook_url: str = ""
    followup_action_key: str = ""


@dataclass(frozen=True)
class LinkCompleteResult:
    status: Literal["ok", "error"]
    message: str = ""


# ── Error taxonomy ───────────────────────────────────────────────────


class HealthBackendError(Exception):
    """Base for all backend-emitted errors. Pull-sync handles each
    subclass differently (rate-limit honors retry, auth disables the
    link, transient retries on next tick)."""


class HealthBackendAuthError(HealthBackendError):
    """Refresh failed; user must reconnect. Five consecutive raises
    disable the link."""


class HealthBackendRateLimitError(HealthBackendError):
    """Provider returned 429. Honor ``retry_after_seconds``."""

    def __init__(self, message: str = "", retry_after_seconds: int = 60) -> None:
        super().__init__(message)
        self.retry_after_seconds = max(0, int(retry_after_seconds))


class HealthBackendTransientError(HealthBackendError):
    """5xx, timeout — retry on the next scheduled tick."""


class HealthBackendNotFoundError(HealthBackendError):
    """Provider's "user/resource gone." Surfaced in ``last_sync_error``."""


# ── HealthBackend ABC ────────────────────────────────────────────────


class HealthBackend(ABC):
    """Source of health data for one or more users.

    Backends are user-aware: every method takes a ``user_id``. A single
    ``HealthBackend`` instance serves every user — per-user state
    (OAuth tokens, webhook secrets) lives in the ``health_links``
    collection and is loaded by the backend on demand. The backend MUST
    NOT cache per-user secrets on ``self`` — see
    ``memory-multi-user-isolation.md``.
    """

    _registry: dict[str, type[HealthBackend]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            HealthBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type[HealthBackend]]:
        return dict(HealthBackend._registry)

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        """Backend-level (global) settings — e.g. Withings ``client_id`` /
        ``client_secret``. PER-USER tokens are NOT here; they belong on
        ``health_links`` rows."""
        return []

    @abstractmethod
    async def initialize(self, config: dict[str, Any]) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    # ── Capability flags ─────────────────────────────────────────────

    @property
    def supports_pull(self) -> bool:
        """True if the backend can ``sync(user_id)`` from an external
        API on demand. Withings yes; Apple Health / HKWebhook no."""
        return False

    @property
    def supports_push(self) -> bool:
        """True if the backend ingests via a webhook. Apple Health /
        HKWebhook yes; Withings no."""
        return False

    # ── Pull-style ───────────────────────────────────────────────────

    async def sync(
        self,
        user_id: str,
        *,
        since: datetime | None = None,
    ) -> list[HealthMetric]:
        """Pull new metrics from the external API for one user.

        Returns the parsed metrics; the service handles persistence.
        ``since`` is a hint — if omitted the backend computes its own
        cursor (typically ``last_sync_at`` from the link row).
        Default raises ``NotImplementedError``; pull backends override.
        """
        raise NotImplementedError

    # ── Push-style ───────────────────────────────────────────────────

    async def parse_webhook(
        self,
        user_id: str,
        body: bytes,
        headers: dict[str, str],
    ) -> list[HealthMetric]:
        """Parse one webhook delivery into ``HealthMetric`` rows.

        Push backends override; pull backends inherit the
        ``NotImplementedError`` default. The service handles
        persistence — backends only translate.
        """
        raise NotImplementedError

    # ── Per-user link lifecycle ──────────────────────────────────────

    async def begin_link(self, user_id: str) -> LinkStartResult:
        """Start an OAuth flow or rotate a webhook token. Default no-op."""
        return LinkStartResult(status="ok", message="No link step needed.")

    async def complete_link(
        self,
        user_id: str,
        payload: dict[str, Any],
    ) -> LinkCompleteResult:
        """Complete a started flow (e.g. exchange OAuth code). Default no-op."""
        return LinkCompleteResult(status="ok", message="No completion step.")

    async def disconnect(self, user_id: str) -> None:
        """Revoke / forget per-user state. Default = no upstream call;
        backends override to revoke OAuth grants etc. The service
        deletes the local ``health_links`` row regardless."""
        return None

    # ── Discovery ────────────────────────────────────────────────────

    @abstractmethod
    def supported_metrics(self) -> set[MetricType]:
        """Metrics this backend can produce."""
        ...


# ── HealthProvider Protocol ──────────────────────────────────────────


@runtime_checkable
class HealthProvider(Protocol):
    """Capability protocol for reading health data and running syncs.

    Greeting integration / proposals / agent services consume this via
    ``resolver.get_capability("health")`` + ``isinstance``. The shape
    matches ``HealthService`` exactly so the protocol is satisfied
    structurally.
    """

    async def read_metrics(
        self,
        user_id: str,
        metric_types: list[MetricType],
        since: datetime,
        until: datetime,
    ) -> list[HealthMetric]: ...

    async def latest_metric(
        self,
        user_id: str,
        metric_type: MetricType,
    ) -> HealthMetric | None: ...

    async def aggregate(
        self,
        user_id: str,
        metric_type: MetricType,
        period: AggregatePeriod,
        since: datetime,
        until: datetime,
        aggregator: AggregatorKind | None = None,
    ) -> list[HealthAggregate]: ...

    async def latest_daily_summary(
        self,
        user_id: str,
        on_or_before: datetime | None = None,
    ) -> DailySummary | None: ...

    async def health_brief_for_greeting(
        self,
        user_id: str,
    ) -> GreetingBrief: ...


# ── Authorization helpers ────────────────────────────────────────────
#
# Pure helpers: no service deps, no concrete-class imports. The caller
# resolves ``is_health_admin`` (via membership in HEALTH_ADMIN_ROLE on
# user_ctx.roles) and passes it in.


def can_read_metrics(
    user_ctx: UserContext,
    target_user_id: str,
    *,
    is_health_admin: bool,
) -> bool:
    """Owner-only reads by default; only ``health-admin`` overrides.

    SYSTEM bypasses (scheduler / cascade work). Admins WITHOUT the
    dedicated ``health-admin`` role do NOT bypass — this is the
    deliberate departure from the inbox model: PHI-adjacent data
    requires an extra-friction admin grant.
    """
    if user_ctx.user_id == UserContext.SYSTEM.user_id:
        return True
    if user_ctx.user_id == target_user_id:
        return True
    return is_health_admin


def can_mutate_metrics(
    user_ctx: UserContext,
    target_user_id: str,
) -> bool:
    """Mutations are always owner-only — even ``health-admin`` cannot
    inject or delete health data on behalf of another user. SYSTEM
    bypasses for scheduler-driven cascade work."""
    if user_ctx.user_id == UserContext.SYSTEM.user_id:
        return True
    return user_ctx.user_id == target_user_id


# ── Shared parser ────────────────────────────────────────────────────


class MetricPayloadError(ValueError):
    """Raised by ``parse_metric_payload`` for unparseable input.

    The backend distinguishes "drop with INFO log" (unknown enum value)
    from "reject the metric" (numeric / timestamp parse failure). Both
    map to ``MetricPayloadError`` here; the backend decides what to do.
    """


def _enforce_extra_caps(extra: dict[str, str]) -> dict[str, str]:
    """Drop over-cap keys / values from ``extra`` per §4.1.

    Drops the offending key with no log line at this level (the caller
    decides whether to log; backends typically DEBUG-log). Returns a
    NEW dict — never mutates the input.
    """
    if not extra:
        return {}
    out: dict[str, str] = {}
    total_bytes = 0
    for raw_k, raw_v in extra.items():
        if len(out) >= EXTRA_MAX_KEYS:
            break
        k = str(raw_k)
        v = str(raw_v)
        if len(k) > EXTRA_MAX_KEY_LEN:
            continue
        if len(v) > EXTRA_MAX_VALUE_LEN:
            continue
        # Approximate serialized-size budget (key + value bytes).
        cost = len(k.encode("utf-8")) + len(v.encode("utf-8"))
        if total_bytes + cost > EXTRA_MAX_TOTAL_BYTES:
            continue
        total_bytes += cost
        out[k] = v
    return out


def parse_metric_payload(
    raw: dict[str, Any],
    *,
    user_id: str = "",
    backend: str = "",
    now: datetime | None = None,
) -> HealthMetric:
    """Shared parser for the JSON push payload (§12.1 / §12.3).

    Used by both ``AppleHealthBackend.parse_webhook`` (after its
    HealthKit-identifier translation step) and
    ``HKWebhookBackend.parse_webhook`` (directly). Lives here per
    "shared data lives in interfaces/."

    Validates: numeric ``value``, parseable ``recorded_at``,
    ``recorded_at <= now + 1h`` (small clock-skew tolerance),
    ``metric_type`` in the known enum (else raises
    ``MetricPayloadError``), unit string in the known enum.

    Caller is responsible for the ``recorded_at`` lower bound
    (``max_backfill_days``) — that's a service-level policy, not a
    parser concern. Caller also enforces the per-backend ``extra``
    whitelist (parser merely caps lengths/sizes).
    """
    now_utc = now or datetime.now(UTC)

    metric_type_raw = raw.get("type") or raw.get("metric_type")
    if not isinstance(metric_type_raw, str) or not metric_type_raw:
        raise MetricPayloadError("metric type missing")
    try:
        metric_type = MetricType(metric_type_raw)
    except ValueError as exc:
        raise MetricPayloadError(f"unknown metric type: {metric_type_raw}") from exc

    unit_raw = raw.get("unit")
    if not isinstance(unit_raw, str) or not unit_raw:
        raise MetricPayloadError("unit missing")
    try:
        unit = MetricUnit(unit_raw)
    except ValueError as exc:
        raise MetricPayloadError(f"unknown unit: {unit_raw}") from exc

    value_raw = raw.get("value")
    if isinstance(value_raw, bool) or value_raw is None:
        raise MetricPayloadError("value missing or invalid")
    try:
        value = float(value_raw)
    except (TypeError, ValueError) as exc:
        raise MetricPayloadError(f"value not numeric: {value_raw!r}") from exc

    recorded_raw = raw.get("recorded_at")
    if not isinstance(recorded_raw, str) or not recorded_raw:
        raise MetricPayloadError("recorded_at missing")
    try:
        recorded_at = datetime.fromisoformat(recorded_raw)
    except ValueError as exc:
        raise MetricPayloadError(f"recorded_at unparseable: {recorded_raw!r}") from exc
    if recorded_at.tzinfo is None:
        # Treat naive timestamps as UTC; webhook clients should send
        # offset-aware ISO 8601 but iOS Shortcuts occasionally don't.
        recorded_at = recorded_at.replace(tzinfo=UTC)
    # Clock-skew tolerance: future timestamps allowed up to now + 1h.
    if recorded_at > now_utc + timedelta(hours=1):
        raise MetricPayloadError(
            f"recorded_at too far in the future: {recorded_raw!r}"
        )

    extra_raw = raw.get("extra") or {}
    if not isinstance(extra_raw, dict):
        extra_raw = {}
    extra = _enforce_extra_caps(
        {str(k): str(v) for k, v in extra_raw.items()}
    )

    source_event_id = str(raw.get("source_event_id") or "")

    return HealthMetric(
        id="",  # populated by the service at persist time
        user_id=user_id,
        backend=backend,
        metric_type=metric_type,
        value=value,
        unit=unit,
        recorded_at=recorded_at,
        ingested_at=now_utc,
        source_event_id=source_event_id,
        extra=extra,
    )


__all__ = [
    "HEALTH_ADMIN_ROLE",
    "EXTRA_MAX_KEYS",
    "EXTRA_MAX_KEY_LEN",
    "EXTRA_MAX_VALUE_LEN",
    "EXTRA_MAX_TOTAL_BYTES",
    "MetricType",
    "MetricUnit",
    "AggregatePeriod",
    "AggregatorKind",
    "DEFAULT_AGGREGATOR",
    "HealthMetric",
    "HealthAggregate",
    "DailySummary",
    "GreetingBrief",
    "LinkStartResult",
    "LinkCompleteResult",
    "HealthBackend",
    "HealthProvider",
    "HealthBackendError",
    "HealthBackendAuthError",
    "HealthBackendRateLimitError",
    "HealthBackendTransientError",
    "HealthBackendNotFoundError",
    "MetricPayloadError",
    "can_read_metrics",
    "can_mutate_metrics",
    "parse_metric_payload",
]

