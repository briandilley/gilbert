# Feature 08 — Health / Quantified-Self Service

> **Status:** Spec / proposed (revised round 2)
> **Author:** Gilbert architecture agent
> **Last updated:** 2026-05-09
> **Related memories:** `memory-backend-pattern`, `memory-multi-backend-pattern`,
> `memory-multi-user-isolation`, `memory-inbox-service`, `memory-presence-service`,
> `memory-event-system`, `memory-access-control`, `memory-ai-prompts-configurable`,
> `memory-scheduler-service`, `memory-notification-service`,
> `memory-proposals-service`.

## 1. Why

Gilbert already knows whether a user is home (`PresenceService`), what their
calendar looks like, what's in their inbox, and what their household devices
are doing. The missing dimension is **how the user is doing physically** — sleep
last night, steps today, resting heart rate trend, weight trend, blood
pressure. Surfacing those signals turns ambient automations from "smart"
into *thoughtful*:

- Greeting integration: "Morning. You only got 5 hours of sleep — I dimmed the
  morning meeting reminders and skipped the loud playlist."
- Proposals integration: "Three nights of poor sleep — propose dimming
  bedroom lights at 21:30 instead of 22:30."
- Chat answers: "What's been off this week?" → grounded in actual metrics
  rather than vibes.
- Routine triggers: a scheduler job can react to *yesterday*'s data
  (e.g., low HRV → start the day softer).

Two implementation modes are needed up front:

1. **Pull-style ingestion (the headline "set-and-forget" path)** — a
   vendor with an OAuth API. v1 ships **Withings** because (a) the
   developer account is free, (b) OAuth 2.0 is open and stable,
   (c) coverage spans sleep / weight / blood pressure / heart rate,
   and (d) it doesn't require the user to wear a particular brand of
   watch. Once connected, sync runs in the background — the user
   doesn't have to keep a phone running.
2. **Push-style ingestion (the "free for iPhone" path)** — Apple
   Health via an iOS Shortcut hitting a per-user webhook. No app code,
   works for every iPhone user, but operationally fragile in practice
   (iOS revokes Background App Refresh on the Shortcuts app, and
   automations require the phone to be unlocked at run-time). v1
   ships a **prebuilt iCloud Shortcut** as the documented path; the
   panel surfaces "last delivery received: Nh ago" so a silently
   broken automation is visible to the user.
3. **Generic webhook** — a catch-all `HKWebhook` backend for users who
   want to point a Shortcut, a Home Assistant automation, a Garmin
   Connect IQ widget, or a custom script at Gilbert without a vendor
   adapter.

The §1 motivating greeting example ("Morning. You only got 5 hours of
sleep — I dimmed the morning meeting reminders and skipped the loud
playlist.") is **aspirational** — v1 ships the *informed* part (the
greeting prompt sees structured headline values via
`HealthProvider.health_brief_for_greeting(user_id)`) but does NOT
ship the *causal action* part (the greeting model has no automation
tools today, per `core/services/greeting.py`). The "I dimmed the
meeting reminders" sentence is a v2 outcome that requires the
greeting model to actually invoke automation tools. Surface the
data; let the user act on it. See §14.

The data is **highly sensitive**. Designing the privacy posture is at
least as important as designing the data flow — see §6.

## 2. Out of scope (v1)

- **Medical diagnosis or advice.** No tool, no prompt, no SPA card may
  imply a clinical assessment. The summary prompt explicitly says
  "describe what you see; do not suggest causes, conditions, or
  treatments." See §11.
- **Cycle tracking / fertility data.** Especially sensitive; deferred
  pending a deliberate privacy review and per-user opt-in beyond the
  global enable.
- **Conflict resolution between simultaneous readings.** If two backends
  report a steps total for the same minute, the latest `ingested_at`
  wins. No reconciliation, no average, no UI surface.
- **Full SPA dashboard with charts.** v1 ships the per-user settings
  page (with the connect/webhook URLs) plus a basic per-metric "latest
  + last 7 days" listing. Real graphs, comparisons, calendar overlays —
  v2.
- **Cross-user aggregation.** Tools never sum, average, or display
  another user's metrics. Even an admin reading metrics belonging to
  another user is gated behind an explicit permission and audit-logged
  (see §6).
- **Garmin / Oura.** Listed in the original brief; only Withings ships
  in v1. Other vendors are additive plugins later — the interface is
  designed to accommodate them without changes to core.
- **Push notifications** ("your weight just spiked"). Events are
  published; subscribing them to push is a v2 concern.
- **Knowledge-store / RAG indexing.** Health metrics are NOT indexed
  into the user's knowledge store, embedded into any vector index,
  or surfaced through any RAG retrieval path in v1. Adding that
  later requires an explicit privacy review — the data is too
  sensitive to be pulled into prompts implicitly via similarity
  search.
- **Mobile push notifications** for daily summaries. Out of scope
  until Gilbert has a mobile push channel generally; surfaced via
  the existing `NotificationService` only.

## 3. Architectural fit

### 3.1 Layer placement

| Component | Layer | Module |
|---|---|---|
| `HealthBackend` ABC + dataclasses | `interfaces/` | `src/gilbert/interfaces/health.py` |
| `HealthService` (singleton) | `core/services/` | `src/gilbert/core/services/health.py` |
| Per-user webhook routes | `web/` | `src/gilbert/web/routes/health.py` |
| Apple Health backend | std-plugin | `std-plugins/apple-health/` |
| Withings backend (OAuth) | std-plugin | `std-plugins/withings/` |
| Generic webhook backend | std-plugin | `std-plugins/hk-webhook/` |
| SPA settings + per-user view | core + per-plugin frontends | `frontend/src/components/health/` + `<plugin>/frontend/` |

Per the architecture rules:

- `interfaces/health.py` imports nothing from `core/`, `integrations/`,
  `storage/`, or `web/`.
- `HealthService` imports from `interfaces/` only and discovers
  backends through `HealthBackend.registered_backends()`.
- `web/routes/health.py` is **thin** — parses webhook payloads,
  resolves the user from the URL token via the service, and calls
  `HealthService.ingest_webhook()`. No payload shape decisions, no
  backend dispatch, no auth logic in the route.
- Plugins import only from `gilbert.interfaces.*` and their own
  internal modules. Withings's OAuth state and Apple Health's parser
  live entirely under `std-plugins/<plugin>/`.

### 3.2 Multi-backend aggregator

Per `memory-multi-backend-pattern.md`, the service holds N backends
internally. The user can run **all three backends at the same time** —
Apple Health for iPhone-collected sleep, Withings for the smart scale,
HKWebhook for a treadmill that POSTs distance after each run. Backends
are keyed in storage, never merged in flight; aggregation tools query
the merged view via storage queries, not by asking each backend.

### 3.3 Per-user state

Health data is per-user and never service-scoped. This means:

- Every storage row carries `user_id` (mandatory, indexed).
- Every backend method takes `user_id` as the first parameter.
- The webhook URL is `/webhook/health/<token>` where `<token>` resolves
  to a single user (the service holds the `token → user_id` map).
- Per-user OAuth state (Withings access tokens) is stored on a
  per-user `health_links` row, not in the global service config.

The `HealthService` itself is a singleton (per
`memory-multi-user-isolation.md`); per-user state is keyed dicts /
storage rows, never instance attributes.

## 4. Data model

### 4.1 Metric dataclasses (in `interfaces/health.py`)

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class MetricType(StrEnum):
    """Standardized set of metric kinds. Extensible — add a new entry
    here when a backend introduces a metric we want to surface; never
    smuggle backend-specific metrics in as opaque strings.

    Backends MAY emit metrics whose ``MetricType`` they don't recognize
    by setting ``metric_type`` to the new enum value; the service
    persists them as long as they parse. Tools that don't know the
    metric simply won't surface it. This keeps adding a new metric a
    one-line change in this file plus a backend update."""

    SLEEP_DURATION = "sleep_duration"          # seconds in bed asleep
    SLEEP_EFFICIENCY = "sleep_efficiency"      # 0.0–1.0
    SLEEP_DEEP = "sleep_deep"                  # seconds
    SLEEP_REM = "sleep_rem"                    # seconds
    SLEEP_AWAKE = "sleep_awake"                # seconds
    STEPS = "steps"                            # integer count
    DISTANCE = "distance"                      # meters
    ACTIVE_MINUTES = "active_minutes"          # minutes
    CALORIES_BURNED = "calories_burned"        # kcal
    HEART_RATE_RESTING = "heart_rate_resting"  # bpm
    HEART_RATE_AVG = "heart_rate_avg"          # bpm
    HRV = "hrv"                                # ms (RMSSD)
    SPO2 = "spo2"                              # 0.0–1.0
    WEIGHT = "weight"                          # kilograms
    BODY_FAT = "body_fat"                      # 0.0–1.0
    LEAN_MASS = "lean_mass"                    # kilograms
    BMI = "bmi"                                # ratio
    BLOOD_PRESSURE_SYS = "blood_pressure_sys"  # mmHg
    BLOOD_PRESSURE_DIA = "blood_pressure_dia"  # mmHg
    BODY_TEMPERATURE = "body_temperature"      # celsius
    RESPIRATORY_RATE = "respiratory_rate"      # breaths/min
    VO2_MAX = "vo2_max"                        # ml/kg/min


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
    PERCENT = "percent"             # 0.0–1.0 stored as fraction
    MMHG = "mmhg"
    CELSIUS = "C"
    FAHRENHEIT = "F"
    BREATHS_PER_MIN = "br/min"
    ML_KG_MIN = "ml/kg/min"
    COUNT = "count"


@dataclass(frozen=True)
class HealthMetric:
    """A single health reading.

    All readings are immutable once persisted. Storage uses
    ``(user_id, metric_type, recorded_at)`` as the natural key — a
    second push for the same triple replaces the existing row (last-
    write-wins by ``ingested_at``).
    """

    id: str                        # UUIDv4 generated at persist time
    user_id: str
    backend: str                   # backend_name that produced this
    metric_type: MetricType
    value: float                   # always float; counts coerce up
    unit: MetricUnit
    recorded_at: datetime          # source-reported timestamp (UTC)
    ingested_at: datetime          # service-assigned arrival time (UTC)
    source_event_id: str = ""      # provider-side id for de-dup; "" if none
    extra: dict[str, str] = field(default_factory=dict)
    # ``extra`` is intentionally string-only and provider-specific
    # (e.g. ``{"device": "Apple Watch"}``). Never structured nested
    # data — that goes through dedicated MetricTypes.
    #
    # SECURITY / PRIVACY: ``extra`` is a whitelist, not a passthrough.
    # Backends MUST NOT funnel raw HTTP headers, IPs, user-agents,
    # webhook-source identifiers, or any caller-controlled blob into
    # ``extra``. Each backend explicitly enumerates the keys it
    # populates (see §4.5 contract table); unknown keys from a
    # webhook payload are dropped. Limits enforced by
    # ``HealthMetric.from_dict`` and the route-level parser:
    #
    # - max key length: 64 chars
    # - max value length: 256 chars
    # - max keys: 16
    # - max total ``extra`` size: 1 KB serialized
    #
    # Over-cap data drops the offending key with a DEBUG log line
    # rather than failing the whole metric.

    def to_dict(self) -> dict[str, object]: ...
    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "HealthMetric": ...


@dataclass(frozen=True)
class HealthAggregate:
    """A computed summary over a window. Computed at query time; not
    persisted (so we don't have to invalidate caches on backfill)."""

    user_id: str
    metric_type: MetricType
    period_start: datetime         # window start, inclusive (UTC)
    period_end: datetime           # window end, exclusive (UTC)
    period: AggregatePeriod        # DAY / WEEK / MONTH
    sample_count: int              # how many HealthMetric rows fed in
    aggregator: AggregatorKind     # SUM / AVG / MIN / MAX / LATEST
    value: float
    unit: MetricUnit


class AggregatePeriod(StrEnum):
    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


class AggregatorKind(StrEnum):
    SUM = "sum"          # steps, distance, calories
    AVG = "avg"          # heart rate, weight (per day average)
    MIN = "min"
    MAX = "max"
    LATEST = "latest"    # weight (latest reading on the day)


# Default aggregator per metric — the service uses this when a tool
# call doesn't specify one. Must stay aligned with how the metric is
# actually emitted by backends (steps are cumulative-per-day from
# Apple Health, so SUM across a day window double-counts; we use
# LATEST instead — Apple Health Shortcut emits one row per day with
# the day's total).
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
# Fallback for any future MetricType not in this table: ``AVG``.
# The service uses the table value, falling back to ``AVG`` on lookup
# miss; tests assert every enum value has an explicit entry to
# prevent silent behavior drift.


def parse_metric_payload(raw: dict[str, object]) -> HealthMetric:
    """Shared parser for the JSON push payload (§12.1 / §12.3).

    Used by both ``AppleHealthBackend.parse_webhook`` (after its
    HealthKit-identifier translation step) and
    ``HKWebhookBackend.parse_webhook`` (directly). Lives in
    ``interfaces/health.py`` per CLAUDE.md "Shared data lives in
    interfaces/."

    Validates: numeric ``value``, parseable ``recorded_at``,
    ``recorded_at <= now + 1h`` (small clock-skew tolerance),
    ``metric_type`` in the known enum (else raises a
    ``MetricTypeUnknown`` the backend handles by dropping with an
    INFO log), unit string in the known enum.

    Caller is responsible for the ``recorded_at`` lower bound
    (``max_backfill_days``) — that's a service-level policy, not a
    parser concern. Caller also enforces the ``extra``-field
    whitelist (parser merely caps lengths/sizes per §4.1).
    """
    ...
```

### 4.2 Per-user link record (`health_links` collection)

One row per `(user_id, backend_name)` carrying provider-specific state
the backend needs at runtime. Examples:

| Field | Description |
|---|---|
| `_id` | `<user_id>/<backend_name>` (slash separator — `user_id` is a UUID-shaped string `usr_xxx` but slash is unambiguous) |
| `user_id` | Owner |
| `backend_name` | e.g., `withings`, `apple-health`, `hk-webhook` |
| `webhook_token_hash` | SHA-256 hex of the per-user webhook token. Indexed (unique). Only present for push backends. The raw token is shown to the user **once**, on rotation, and never persisted. |
| `webhook_token_last4` | Last four characters of the raw token, for UI display ("••••abcd"). |
| `oauth_access_token` | Plaintext in v1 SQLite (see §6.4 framing); only OAuth backends. |
| `oauth_refresh_token` | Plaintext in v1 SQLite (see §6.4 framing). |
| `oauth_expires_at` | UTC ISO timestamp |
| `oauth_user_id` | Provider-side user id (Withings `userid`) |
| `last_sync_at` | When `sync(user_id)` last ran successfully |
| `last_sync_error` | Last error message, "" on success |
| `last_delivery_at` | UTC ISO of the most recent webhook delivery accepted (push backends only). Surfaced in the UI as "last delivery received: Nh ago" so a silently-broken Shortcut is visible. |
| `enabled` | Per-user opt-in; defaults `False` until a connect/webhook |
| `created_at`, `updated_at` | ISO 8601 |

`webhook_url` is **NOT stored** on the link row — it's computed at
read time as `<gilbert_public_base_url>/webhook/health/<token>`. This
means changing the base URL doesn't require a migration; it does mean
the URL is only knowable while the raw token is in memory (i.e., at
rotation time — once the token is hashed and stored, the URL can no
longer be reconstructed by the server, so the user must save it).

The webhook token is **per-user**, not per-backend — but a single user
might be running both Apple Health and HKWebhook with different tokens;
the row identifies which backend the token routes to. The token is **48
bytes of entropy from `secrets.token_urlsafe(48)`, encoded as a
64-character URL-safe base64 string**, never a hash of anything
guessable.

**Token comparison and storage:** the lookup index is on
`SHA-256(token)` (hex), not the raw token. On each webhook delivery
the route hashes the URL-supplied token and queries
`health_links(webhook_token_hash)`. SQLite string equality on the hash
is constant-time-equivalent for our threat model (the index value is
not the secret). After the index hit, the service confirms with
`hmac.compare_digest(stored_hash, computed_hash)` to defeat any
future timing variation. The raw token is never written to the DB,
which means `.gilbert/gilbert.db` cannot be exfiltrated to inject
metrics — it can only be used to read existing rows.

**Required index:** `health_links(webhook_token_hash)` UNIQUE — every
delivery hits this index by primary lookup; without it, the route
table-scans on every POST.

### 4.2.1 OAuth state collection (`health_oauth_state`)

CSRF / one-shot state for OAuth backends. Required because the
`health_links` row doesn't exist yet during `begin_link`, and the
state cannot be carried in the session alone (the session may have
expired during the round-trip; a confused-deputy attack would
hijack the link otherwise — see §6.3 / §12.2).

| Field | Description |
|---|---|
| `_id` | The `state` value sent to the OAuth provider — 32 bytes of `secrets.token_urlsafe(24)` random |
| `user_id` | The user who initiated the flow (server-side bound — callback MUST verify the calling session matches OR the state is rejected) |
| `backend_name` | The backend the flow targets (e.g. `withings`); a state issued for one backend cannot be consumed by another |
| `created_at` | UTC ISO |
| `expires_at` | UTC ISO; `created_at + 600s` (10-minute window) |
| `consumed_at` | UTC ISO if completed; one-shot — a second callback with the same state must fail |

**Required index:** `health_oauth_state(_id)` (primary).
Garbage-collect expired states on every `begin_link` or via a cheap
periodic sweep.

### 4.3 Metrics collection (`health_metrics`)

| Field | Description |
|---|---|
| `_id` | `uuid4` |
| `user_id` | **Indexed** |
| `backend` | `backend_name` of the producing backend |
| `metric_type` | `MetricType` value |
| `value` | float |
| `unit` | `MetricUnit` value |
| `recorded_at` | UTC ISO; **indexed** |
| `ingested_at` | UTC ISO |
| `source_event_id` | provider de-dup key; "" if absent |
| `extra` | `dict[str, str]` |

**Indexes** (every read is per-user, so every index leads with `user_id`):

1. `health_metrics(user_id, metric_type, recorded_at)` — primary
   read path for `read_metrics` and `aggregate`.
2. `health_metrics(user_id, recorded_at)` — for "give me everything in
   this window."
3. `health_metrics(user_id, backend, source_event_id)` — uniqueness
   for de-duplication when a backend re-pushes an old reading.

**De-duplication rule:** when a backend ingests a new reading, the
service queries by `(user_id, backend, source_event_id)` if
`source_event_id` is non-empty; if a hit exists the row is *replaced*
(by deleting + inserting; the storage backend is JSON-document, not
RDBMS — see `memory-storage-backend.md`). For backends that don't supply
`source_event_id`, de-dup falls back to `(user_id, metric_type,
recorded_at)`. Writes are atomic per row.

### 4.4 Daily-summary collection (`health_daily_summaries`)

The scheduler computes a per-user daily summary and persists it for
fast retrieval by the greeting integration. One row per `(user_id, date)`:

| Field | Description |
|---|---|
| `_id` | `<user_id>/<YYYY-MM-DD>` (the user's local date) |
| `user_id` | **Indexed** |
| `local_date` | `YYYY-MM-DD` |
| `summary_text` | Natural-language summary (AI-generated; non-clinical) — display-only, never parsed by downstream tools (see §11.4) |
| `metrics_snapshot` | `dict[MetricType, float]` of the day's headline values |
| `flags` | `list[str]` — internal markers (`"low_sleep"`, `"sedentary"`, `"weight_drift"`); computed in code from `metrics_snapshot`, **never** parsed from `summary_text`. See §15. |
| `generated_at` | UTC ISO |

`flags` is a deliberately small, internal vocabulary (see §11 + §15)
— never a free-form set of clinical descriptors.

### 4.5 Audit-log collection (`health_audit`)

Cross-user reads (§6.1) and self-deletes (§6.6) write durable rows
here. The collection is `read=admin/write=admin` via
`acl_collections` (seeded at start, see §7.5) so an attacker who
gains user-level access cannot tamper with the audit trail through
the entities page.

| Field | Description |
|---|---|
| `_id` | UUIDv4 |
| `kind` | `"cross_user_read"` / `"self_delete_all"` / `"oauth_token_revoke"` (extensible; current vocabulary fixed) |
| `actor_user_id` | Who took the action (may be the target for self-delete; `"system"` is **disallowed** here — automated cascades use `kind="cascade"` and skip the audit row) |
| `target_user_id` | Whose data was touched |
| `accessed_at` | UTC ISO |
| `metric_types` | `list[MetricType]` (read-only kinds touched) — empty for `self_delete_all` |
| `period_start`, `period_end` | UTC ISO; window of the read |
| `request_id` | Optional correlation id from the route layer |

**Index:** `health_audit(target_user_id, accessed_at)` so a target
user can list "who has accessed my data, most recent first" via the
`/account/health/audit-log` page (see §17).

**Retention:** infinite by default. Documented as the deliberate
counterweight to "we can delete metrics; we never delete the record
of who looked at them." If `audit_retention_days > 0` is set, prune
on the same daily-retention job as metrics (§10.3) — but the default
is **never prune**.

### 4.6 Backend-specific `extra` whitelist contract

Every push/pull backend declares the keys it may populate in
`HealthMetric.extra`. The service rejects any key not in the
declared set. Additions to the contract require a backend update
(spec change), not a runtime override:

| Backend | Allowed `extra` keys | Notes |
|---|---|---|
| `apple-health` | `device` (e.g., "Apple Watch", "iPhone"), `source_app` | Drawn from HealthKit `HKDevice.name` / `HKSource.name`. Any other key from the payload is dropped with an INFO log. |
| `withings` | `device_model_id` (numeric Withings device id), `measure_grpid` (Withings' group id), `attrib` (Withings' source-attribution code) | Populated from the Withings API response. |
| `hk-webhook` | (none) | Generic webhook silently strips `extra` from delivered payloads in v1 — the back-channel for caller metadata is `source_event_id`, not arbitrary string blobs. |

The contract enforces "never log an IP / header / user-agent into a
metric row" by construction; a future plugin author can't quietly
funnel `request.client.host` into `extra` because the key isn't in
their backend's whitelist.

## 5. `HealthBackend` interface

```python
class HealthBackend(ABC):
    """Source of health data for one or more users.

    Backends are user-aware: every method takes a ``user_id``. A single
    ``HealthBackend`` instance serves every user — per-user state
    (OAuth tokens, webhook secrets) lives in the ``health_links``
    collection and is loaded by the backend on demand. The backend MUST
    NOT cache per-user secrets on ``self`` — see
    ``memory-multi-user-isolation.md``.
    """

    _registry: dict[str, type["HealthBackend"]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            HealthBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type["HealthBackend"]]: ...

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        """Backend-level (global) settings — e.g., Withings API client_id /
        client_secret. PER-USER tokens are NOT here — they belong on
        ``health_links`` rows."""
        return []

    @abstractmethod
    async def initialize(self, config: dict[str, object]) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    # ── Capability flags ─────────────────────────────────────────────

    @property
    def supports_pull(self) -> bool:
        """True if the backend can ``sync(user_id)`` from an external
        API on demand. Withings yes; Apple-Health no; HKWebhook no."""
        return False

    @property
    def supports_push(self) -> bool:
        """True if the backend ingests via a webhook. Apple-Health yes;
        HKWebhook yes; Withings no."""
        return False

    # ── Pull-style ───────────────────────────────────────────────────

    async def sync(self, user_id: str, *, since: datetime | None = None) -> int:
        """Pull new metrics from the external API for one user.
        Returns the number of metrics persisted. Default raises
        ``NotImplementedError``; pull backends override.

        ``since`` is a hint — if omitted the backend computes its own
        cursor (typically ``last_sync_at`` from the link row)."""
        raise NotImplementedError

    # ── Push-style ───────────────────────────────────────────────────

    async def parse_webhook(
        self,
        user_id: str,
        body: bytes,
        headers: dict[str, str],
    ) -> list[HealthMetric]:
        """Parse one webhook delivery into HealthMetric rows.

        Push backends override; pull backends inherit the
        ``NotImplementedError`` default. The service handles
        persistence — backends only translate."""
        raise NotImplementedError

    # ── Per-user link lifecycle ──────────────────────────────────────
    # Both kinds of backend may need to do per-user setup ("connect"
    # for OAuth, "rotate token" for webhook). The service exposes
    # these via ConfigActions on the per-user settings page.

    async def begin_link(self, user_id: str) -> LinkStartResult:
        """Start an OAuth flow or rotate a webhook token. Returns the
        URL/payload the UI shows the user. Default = no-op."""
        return LinkStartResult(status="ok", message="No link step needed.")

    async def complete_link(
        self,
        user_id: str,
        payload: dict[str, object],
    ) -> LinkCompleteResult:
        """Complete a started flow (e.g., exchange OAuth code).
        Default = no-op."""
        return LinkCompleteResult(status="ok", message="No completion step.")

    async def disconnect(self, user_id: str) -> None:
        """Revoke / forget per-user state. Default deletes the
        ``health_links`` row only — backends may override to revoke
        upstream too."""
        ...

    # ── Discovery ────────────────────────────────────────────────────

    @abstractmethod
    def supported_metrics(self) -> set[MetricType]:
        """Metrics this backend can produce. Drives the SPA's per-
        backend display and lets the service skip useless aggregations
        (e.g., asking Apple-Health for blood pressure when nothing the
        Shortcut sends maps to BP)."""
        ...
```

### 5.1 Why `user_id` is a parameter, not constructor state

A single Withings backend instance serves every user; per-user
OAuth tokens come from the link row at call time. This avoids the
"two users start a sync in parallel" race that would happen if the
backend cached `current_user_id` on `self`. Per
`memory-multi-user-isolation.md`, request-scoped state never lives on
a singleton.

### 5.2 Helper dataclasses (in `interfaces/health.py`)

```python
@dataclass(frozen=True)
class LinkStartResult:
    status: Literal["ok", "pending", "error"]
    message: str = ""
    open_url: str = ""             # OAuth: provider authorize URL
    webhook_url: str = ""          # Push: per-user webhook URL
    followup_action_key: str = ""  # for two-phase ConfigAction flow

@dataclass(frozen=True)
class LinkCompleteResult:
    status: Literal["ok", "error"]
    message: str = ""
```

## 6. Privacy posture

Health data is the most sensitive collection in Gilbert. The default
posture is **owner-only access, full stop**.

### 6.1 Visibility rules

| Actor | Can read user A's metrics | Can mutate user A's metrics | Can see user A in the cross-user list |
|---|---|---|---|
| User A (themselves) | Yes | Yes (disconnect, delete-all) | n/a |
| Another user | **No** | **No** | **No** |
| Admin (default — `admin` role only) | **No** | **No** | Yes — but only `(user_id, has_data: bool, last_ingested_at)` |
| Admin with the `health-admin` role | Yes — audit-logged | No | Yes |
| `SYSTEM` (scheduler / aggregator jobs) | Yes (in scope of the running job) | Yes (insert metrics, delete-all on user delete) | Yes |

**Implementation (aligned with Gilbert RBAC primitives — see
`memory-access-control.md`):**

- Cross-user read is gated by membership in a dedicated
  **`health-admin` role**, seeded at level **0** (admin-tier),
  immutable. The seeded role is **NOT** granted to any user by
  default — including the built-in `admin` user. Operators grant it
  explicitly via the existing `/roles/users` page. This is Gilbert's
  native primitive for "an extra-friction admin capability"; we are
  *not* introducing a new "permission" abstraction.
- The pure helper `can_read_metrics(user_ctx, target_user_id, *,
  is_health_admin)` (in `interfaces/health.py`) takes the boolean.
  The call site computes `is_health_admin = "health-admin" in
  user_ctx.roles` (or via `AccessControlProvider.user_has_role` if
  that helper exists by implementation time — see §7.3).
- Every read path filters by `user_id == get_current_user().user_id`
  before returning anything. The filter happens in the *service*,
  not in the route. Routes that pass a `user_id` parameter must
  reject anything other than the current user unless the caller has
  the `health-admin` role.
- A user holding `health-admin` is **deliberately distinct** from
  the user holding `admin`. The default `admin` (whose role grants
  the `entities` page) does **not** see health rows there because
  `acl_collections` for `health_*` collections is gated to
  `health-admin` (see §7.5) — defense-in-depth even if a future
  refactor of the entities page bypasses the service.
- Cross-user read access, when granted, **persists a
  `health_audit` row** (§4.5) AND logs to the structured logger
  `gilbert.health.audit` (INFO; values redacted, only counts /
  metric types / window persisted). The user being read is
  notified via `NotificationService.notify_user(user_id=target,
  source="health", urgency="normal", ...)`. Notification copy is
  pinned in §6.1.1.

**Step-up auth (deferred to v2):** PHI-style flows in regulated
industries usually require fresh re-authentication on each cross-user
access, not a one-time role grant. v1 is one-time grant; v2 should
add a sudo-style check.

### 6.1.1 Cross-user-read notification copy

Pinned so the implementer doesn't ad-lib clinical-sounding language:

> Title: **"Health data accessed"**
> Body: "An admin viewed your health metrics ({metric_types_summary},
> {period_human}). If you weren't expecting this, contact your
> administrator."
> Action link: `/account/health/audit-log` (see §17.4)

`{metric_types_summary}` = comma-joined human-friendly names ("sleep,
weight"); `{period_human}` = "today" / "yesterday" / "the last 7 days"
/ "2026-04-01 to 2026-04-08" depending on the window.

### 6.1.2 Notification durability + offline targets

`NotificationService.notify_user(...)` persists a `Notification`
entity per `memory-notification-service.md` — the user reads it next
time they log in regardless of connection state at write time. If
`NotificationProvider` is **not** present in the resolver:

1. Write the `health_audit` row anyway.
2. Emit `health.access.audit` on the event bus anyway (the WS
   filter still delivers it to a connected target).
3. Log a WARN (`gilbert.health.audit.notify_skipped`) so an
   operator running without notifications sees the gap.

The cross-user read is **never silently un-notified**. If
`NotificationProvider` is absent AND the user is offline at access
time, the audit row is the durable record and the user sees it via
`/account/health/audit-log` on next login.

### 6.2 No aggregation across users

Tools and WS RPCs always operate on one user at a time — the current
caller. There is no "household average sleep last night" tool. If a
v2 wants that, it must be designed deliberately with explicit opt-in
from each contributing user.

The admin SPA page has a list of users and **counts only**: how many
metrics, what backends, last-ingested-at. No values, no metric types
beyond per-user totals, no per-day breakdown.

### 6.3 Webhook security

- The webhook URL is `/webhook/health/{token}` — no session cookie,
  no Authorization header. **The token is the only authorization.**
- Tokens carry **48 bytes of entropy** generated by
  `secrets.token_urlsafe(48)` and encoded as a 64-character URL-safe
  base64 string. Brute-force search space is ≈ 2^288.

- **Hash-at-rest:** the raw token is shown to the user **once** at
  rotation time and never persisted; only `SHA-256(token)` lives in
  `health_links.webhook_token_hash` (indexed UNIQUE; see §4.2).
  - Lookup: route hashes the incoming URL token, queries
    `health_links(webhook_token_hash)`. Missing → 404.
  - Confirmation: even after the index hit, the service runs
    `hmac.compare_digest(stored_hash, computed_hash)` to defeat any
    future timing leak from the storage layer's string equality.
  - This means a stolen `.gilbert/gilbert.db` cannot be used to
    inject metrics — the attacker can read the historical rows but
    cannot mint a working webhook URL.

- **Token rotation** (`POST /api/health/me/rotate-token/{backend}`)
  issues a fresh token, recomputes the hash, replaces the row's
  `webhook_token_hash` / `webhook_token_last4`, and **does not delete
  any historical metrics**. The old token is revoked immediately.
  Returns the raw new token in the response **once** plus the
  derived `webhook_url` so the SPA can show "copy this URL into
  your iOS Shortcut." On rotation, the service also calls
  `NotificationService.notify_user` with urgency `urgent` to remind
  the user to update any device that posted with the old token —
  otherwise rotation creates the silent-dead-drop bug it was meant
  to defend against.

- **Per-backend applicability:** rotate-token returns a
  `LinkStartResult(status="error", message="Token rotation does not
  apply to backend '<name>'")` for backends with `supports_push =
  False` (e.g., Withings). The route checks before invoking the
  backend.

- The webhook route **does not** create or update `UserContext` —
  the token resolves to a `user_id`, which the service uses to
  attribute the metric. Webhook deliveries do not get a session,
  cannot read other endpoints, and cannot drive AI calls.

- **Rate limiting** uses two in-memory leaky-buckets on the singleton:
  1. **Per-token bucket** keyed on `webhook_token_hash`: 60
     deliveries / minute (configurable via
     `webhook_rate_per_minute`). Past the bucket → 429 with
     `Retry-After`.
  2. **Per-IP bucket** keyed on `remote_addr`: protects the **404
     path** from token-probing. 30 attempts / minute / IP for
     unknown-token POSTs (configurable via
     `webhook_unknown_rate_per_minute`). Past the bucket → 429,
     same response shape as known-token 429 to avoid leakage.
  - Eviction: both buckets are LRU-capped at 10k entries to bound
     memory; oldest entries dropped on insert past cap. Restart wipes
     budgets — acceptable for a self-hosted v1 instance.

- **Body / payload caps** (route-level, before parsing):
  - `Content-Length > webhook_max_body_bytes` (default `1_048_576` =
     1 MB) → reject 413 (Payload Too Large).
  - `len(metrics) > webhook_max_metrics_per_delivery` (default 1000)
     → reject 400 (Bad Request).
  - Per-metric validation in `parse_metric_payload` (§4.1): numeric
     `value`, parseable `recorded_at`, `metric_type` in known enum,
     known unit, `extra` size caps. Bad metrics are dropped (not
     reject-the-batch); response is `200 {"received": N, "dropped":
     M}`.
  - Future `recorded_at` allowed up to `now + 1h` for clock skew;
     past `recorded_at` allowed back to `now - max_backfill_days`.

- **TLS:** documented in the per-user settings instructions. On
  `start()` the service emits a startup WARN if `web.bind_address !=
  127.0.0.1` AND no tunnel is configured AND TLS is not in front
  of Gilbert — webhook tokens travel over the wire and need
  transport encryption. Plain HTTP for LAN testing is supported but
  flagged.

- **Replay protection:**
  - **Replay flood (re-POST the same captured body):** dedup on
    `(user_id, backend, source_event_id)` collapses to a no-op. The
    service emits `health.metric.received` events **only for newly
    persisted rows** — duplicate detection skips the event publish
    so a replay flood cannot amplify into the event bus.
  - **Captured-token replay (the URL itself is the secret):**
    HTTPS-only is the primary defense. Out of scope to add a
    body-HMAC for v1 (it would mean threading a second per-user
    secret through the iOS Shortcut, which is fragile); the open
    questions list it for v2.
  - **Stale-history replay:** deliveries with `recorded_at` older
    than `max_backfill_days` (default 90) are silently dropped
    before reaching `ingest_metrics`.

- **Endpoint enumeration shape:** `not_found` and `disabled` (§7.7)
  collapse to the **same response** — 404, identical body
  (`{"received": 0}`), identical headers, identical latency
  characteristics (no shortcut returns; both go through the same
  rate-limit and lookup path before responding). The internal
  service distinguishes for metrics and logs; the wire response
  does not.

### 6.4 OAuth token storage (PHI-adjacent — v1 framing)

Withings access/refresh tokens live on the `health_links` row in the
entity store (`.gilbert/gilbert.db`, gitignored). Webhook tokens are
**already** hash-at-rest (§6.3); OAuth refresh tokens cannot be
hashed (we have to send them back to the provider) so they need a
different posture.

**v1 posture (this PR):**

- **Webhook tokens are hash-at-rest now** (no keychain
  infrastructure required, no opt-in). This is the high-risk
  exfiltration vector — a leaked DB cannot mint working webhook
  URLs.
- **OAuth refresh/access tokens are stored in plaintext** in v1.
  Reason: encryption-at-rest needs OS-keychain plumbing
  (Fernet-sealed-to-keychain) we do not yet have, and shipping a
  half-baked stub is worse than documenting the gap.
- **Operational gating** — when the service starts in a
  configuration where the database is exposed beyond a single
  trusted host, this is unsafe. On `start()` the service emits a
  startup WARN if **all three** are true:
  1. At least one OAuth backend is registered (currently
     `withings`).
  2. `web.bind_address != "127.0.0.1"`.
  3. No tunnel backend reports an active terminating proxy that
     enforces TLS.
  The WARN message tells the operator to either restrict the bind
  address, deploy behind a TLS proxy, or wait for v2 encryption.
  The service starts; it does not refuse to start, because a
  homelab on `127.0.0.1` is a legitimate v1 deployment.
- **File permissions:** the bootstrap docs require
  `chmod 600 .gilbert/gilbert.db`. The service does not chmod the
  file at runtime (it doesn't own the DB lifecycle), but it does
  emit a startup WARN if the file mode is more permissive than
  `0600` on POSIX systems.
- **Per-user warning in the SPA settings panel** — the Withings
  account row shows a small "Tokens stored unencrypted on this
  Gilbert instance until v2." line so the user can make an informed
  choice.

**v2 posture (separate PR, before this feature is exposed in any
multi-user-internet-reachable deployment):**

- Fernet (or libsodium secretbox) symmetric encryption of OAuth
  access/refresh tokens, with the key sealed to the OS keychain
  (same approach we plan for inbox OAuth tokens). On startup, the
  service unseals the key once; per-row decrypt is in-memory only.
- Key rotation is a separate operation that re-encrypts the column
  with a fresh key and rotates the keychain entry.
- Backwards-compatible with v1 rows: any plaintext refresh token
  on disk gets re-encrypted on next access.

**Logging:**

- Refresh tokens never appear in logs. The redaction filter in
  `core/logging.py` matches `*token*` / `*secret*` / `*password*`.
  The audit-required additional fields — `code` (OAuth authorization
  code), `state` (OAuth state), `Authorization` header,
  `webhook_url` (contains the token in plaintext) — are added to
  the redaction allowlist before this feature ships. Tests verify
  each field name produces a redacted log line.
- The audit log for cross-user reads logs only `(actor, target,
  metric_types, period, accessed_at)` — never values.

**Withings client_id and client_secret are global** (one
developer-app set per Gilbert instance) — they live in
`backend_config_params()` with `sensitive=True`, not on per-user
rows. They are the same kind of secret as e.g. the Anthropic API
key and follow the same redaction rules.

### 6.5 Knowledge / chat scope

When the chat AI mentions a health metric, the metric is fetched
from the *current user's* metrics only. Tools never receive a
`user_id` argument from the model — they read `_user_id` from the
injected tool args (per `memory-multi-user-isolation.md`). The
greeting integration runs as `SYSTEM` but always with
`set_current_user(target_user)` — the greeting goes only to that
user.

### 6.6 Right to delete

The right-to-delete is **a two-step wizard, not a single button**.
Health data is too costly (months of accumulated metrics, possibly
already informing automations) to lose to a stray click.

- **Step 1 — preview** (`GET /api/health/me/delete-all/preview`,
  WS RPC `health.delete_all.preview`): returns the *exact* count
  of affected rows so the user signs an informed check, not a blank
  one. Response shape:
  ```json
  {
    "metric_count": 4832,
    "earliest_recorded_at": "2024-11-08T00:00:00Z",
    "latest_recorded_at":   "2026-05-08T00:00:00Z",
    "backends": ["apple-health", "withings"],
    "summaries_count": 182,
    "audit_count": 4
  }
  ```
- **Step 2 — confirm** (`POST /api/health/me/delete-all`): payload
  must include `confirm: "DELETE"` (case-sensitive literal). On
  success:
  1. For each `health_links` row with `supports_pull` and an OAuth
     refresh token, attempt `backend.disconnect(user_id)` which
     **MUST revoke the upstream OAuth grant** (Withings's
     `/oauth2/revoke` endpoint) before deleting the local row. If
     revocation fails (network / API down), local disconnect still
     proceeds and a WARN is logged — local cleanup must always
     succeed; an inability to call the upstream cannot strand the
     user's local data.
  2. Delete every `health_metrics`, `health_daily_summaries`, and
     `health_links` row for the user.
  3. **Persist a `health_audit` row** (kind=`self_delete_all`,
     actor=target=user_id, with the counts from step 1) so the
     user has a permanent record of "I did this on date X."
     The audit row survives the cascade.
  4. Publish `health.metric.deleted` with `scope="user-deleted"`.
  5. Return `{deleted_metrics: N, disconnected_backends: [...],
     upstream_revoke_failures: [...]}`.

- The AI tool `health_delete_my_data` returns a confirmation
  `UIBlock` (per `memory-ui-blocks.md`) showing the same preview
  counts. The model cannot one-shot the delete; it must render the
  block and wait for the user's explicit click. The slash-command
  surface is constrained — see §9 / §U1.

- **Withings retention disclosure** (GDPR-aware): the SPA
  confirmation dialog explicitly states: "We will revoke Gilbert's
  Withings access and delete every measurement we cached locally.
  Withings continues to retain the data on your behalf — to delete
  it from Withings, use Withings's own account-deletion flow." The
  user opts in to the local-only delete; deleting Gilbert's cache
  is not the same as deleting from upstream providers, and silent
  imprecision here would be misleading.

- **User deletion cascade.** When the `UserService` deletes a user
  (via `delete_user(user_id)`), it publishes a new bus event
  `auth.user.deleted` with `{user_id, deleted_at}` payload (see
  §6.6.1 below — this is a precondition this feature carries with
  it). `HealthService` subscribes and runs the same delete cascade
  for the deleted user as a self-delete, but with `actor="system"`
  and **no audit row** (the user no longer exists to audit; we log
  to `gilbert.health.audit` instead).

- **Disconnect-without-delete** (`disconnect_backend(backend_name)`):
  removes the link row but leaves historical metrics in place — the
  user opted in once, the data is theirs. For OAuth backends, this
  also calls Withings `/oauth2/revoke` to revoke the upstream
  grant; same WARN-on-failure semantics as the delete-all path.

### 6.6.1 Precondition: `auth.user.deleted` event

`UserService.delete_user(user_id)` does not currently publish an
event. This feature adds that publish in the same PR — multiple
future services (inbox-per-user, agents, goals, …) will all need
it, and adding a no-deps event to a single service is well-scoped.

- **New event:** `auth.user.deleted`
- **Payload:** `{user_id: str, deleted_at: str (UTC ISO)}`
- **Prefix already routes** to admin-level visibility per the `auth.`
  prefix in `interfaces/acl.py`.
- **Publishing site:** `UserService.delete_user` immediately after
  the backend delete returns — fire-and-forget on the resolved
  `EventBusProvider`. If no event bus is registered, the publish
  is a no-op (the bus is optional in many test scenarios).
- **Memory update:** `memory-user-auth-system.md` adds a one-liner
  documenting the new event in the same PR.

This is also the foundation for the `health` cascade subscription
(§6.6) and is mentioned as a precondition step in §21.

### 6.7 Logging

- The existing AI-call audit log file (`api_calls.log`, the file
  that records `Soul`'s `request.user_message` against AI requests)
  is **not** appended to for health-related tool calls — health
  values are not echoed into log lines outside of `DEBUG` level.
- The service uses a **structured audit logger**
  (`gilbert.health.audit`, INFO) for cross-user reads; entries
  contain only `(actor, target, metric_types, period_start,
  period_end, accessed_at)`, never values. This is in addition to
  the durable `health_audit` collection (§4.5).
- At `INFO`, the service logs only counts: `"ingested 12 metrics for
  user xyz from backend withings"`. At `DEBUG`, individual metric
  types and values may appear; this is gated behind a config flag
  `health.debug_log_values` (default `false`) so an unexpected
  log-rotation of a debug-enabled instance doesn't leak data.
- **`debug_log_values` is global**, not per-user. Multi-user
  instances should never enable it. The service emits a startup
  WARN if `debug_log_values=true` AND the user count is > 1.
  Future work could add a per-user "this user opts into debug
  logging" field on the link row, but v1 keeps it simple at the
  cost of a global toggle. Acceptable for a single-user homelab;
  documented as risky for multi-user.
- **Log-redaction allowlist updated.** The redaction filter in
  `core/logging.py` already matches `*token*` / `*secret*` /
  `*password*`. This PR adds `code` (OAuth authorization code),
  `state` (OAuth state value), `Authorization` (case-insensitive
  match), `webhook_url` (contains the token in plaintext), and
  `oauth_*` fields to the redaction list. Tests verify each field
  name produces a `[redacted]` log line.
- **Allowlist over denylist.** The service additionally enumerates
  the field names safe to log at INFO (`user_id`, `backend`,
  `metric_type`, `count`, `local_date`, `recorded_at`) and uses
  structured logging extras to emit only those keys — a future
  field rename can't accidentally leak a value because the new
  field name isn't in the allowlist.

## 7. `HealthService` (core)

### 7.1 Service info

```python
class HealthService(Service):
    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="health",
            capabilities=frozenset({
                "health",
                "ai_tools",
                "ws_handlers",
            }),
            requires=frozenset({"entity_storage", "scheduler"}),
            optional=frozenset({
                "event_bus",
                "configuration",
                "access_control",
                "ai_chat",          # for the daily-summary prompt
                "notifications",    # for cross-user read alerts
            }),
            events=frozenset({
                "health.metric.received",
                "health.metric.deleted",
                "health.daily.summary",
                "health.link.connected",
                "health.link.disconnected",
                "health.access.audit",
            }),
            ai_calls=frozenset({"health_daily_summary"}),
            toggleable=True,
            toggle_description="Personal health metrics ingestion + tools",
        )
```

`users` is **not** a required capability. The service iterates "users
with active health links" by querying the `health_links` collection
itself (`SELECT DISTINCT user_id WHERE enabled = true`); it never
needs to enumerate the global user list. This keeps the service
decoupled from any future `UsersProvider` protocol, and it's the right
shape anyway: only users who have opted in show up in our scheduler
loops.

### 7.2 Public surface (`HealthProvider` Protocol)

A new `@runtime_checkable Protocol` in `interfaces/health.py`:

```python
@runtime_checkable
class HealthProvider(Protocol):
    """Capability protocol for reading health data and running syncs.

    Greeting integration / proposals / agent services consume this
    via ``resolver.get_capability("health")`` + ``isinstance``."""

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
    ) -> "DailySummary | None": ...

    async def health_brief_for_greeting(
        self,
        user_id: str,
    ) -> "GreetingBrief":
        """Structured snapshot for the greeting integration.

        Returns headline values + flags for the user's current
        wake-up window. The greeting prompt receives this as
        template variables (NOT the AI-generated ``summary_text``)
        so the greeting model can reason about the facts in its
        own voice, rather than stapling a pre-canned paragraph
        onto the greeting. See §14.

        Returns ``GreetingBrief.empty`` for a user with no
        ``health_links`` rows so the greeting prompt sees a clear
        absent-data signal rather than zeros.
        """
        ...
```

`GreetingBrief` is a small dataclass in `interfaces/health.py`:

```python
@dataclass(frozen=True)
class GreetingBrief:
    user_id: str
    has_data: bool
    sleep_hours: float | None        # last night, user-local
    sleep_efficiency: float | None
    steps_today_so_far: int | None
    weight_latest: float | None
    weight_unit: MetricUnit
    resting_hr_latest: float | None
    flags: list[str]                  # subset of the §15 vocabulary

    @classmethod
    def empty(cls, user_id: str) -> "GreetingBrief": ...
```

### 7.3 Authorization helpers (in `interfaces/health.py`)

Mirroring the inbox pattern (`memory-inbox-service.md`), authorization
helpers live in `interfaces/` and are pure (no service deps):

```python
def can_read_metrics(
    user_ctx: UserContext,
    target_user_id: str,
    *,
    is_health_admin: bool,
) -> bool:
    """Owner-only by default; only membership in the dedicated
    ``health-admin`` role can override.

    SYSTEM bypasses (scheduler / cascade work). Admins WITHOUT the
    ``health-admin`` role do NOT bypass — this is the deliberate
    departure from the inbox model.
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
```

The service resolves `is_health_admin` by checking
`"health-admin" in user_ctx.roles`. The role name is a constant in
`interfaces/health.py` (`HEALTH_ADMIN_ROLE = "health-admin"`) so a
single rename touches one location. If access control is disabled
entirely (no `AccessControlProvider`), the function still consults
`user_ctx.roles` directly — `roles` is on the dataclass, not behind
the provider. Secure-by-default: missing role → `False`.

**Role seeding** happens in `HealthService.start()` (see §7.5):
on first boot, the service ensures the `health-admin` role exists
at level 0 in `acl_roles` (idempotent — won't recreate if present).
The seed does **not** grant the role to any user.

### 7.4 Internal layout

```
HealthService:
    _storage: StorageBackend
    _event_bus: EventBus | None
    _scheduler: SchedulerProvider
    _ai: AISamplingProvider | None
    _access_control: AccessControlProvider | None
    _notifications: NotificationProvider | None

    _backends: dict[str, HealthBackend]   # backend_name → instance
    _enabled: bool
    _debug_log_values: bool
    _max_backfill_days: int
    _summary_prompt: str
    _trend_prompt: str
    _retention_days: int                  # 0 = forever
    _audit_retention_days: int            # 0 = forever (default)
    _daily_summary_local_hour: int        # default 5 (5am local)
    _ai_profile: str                      # AI profile to use for summaries

    # Concurrency caps for scheduler loops (§10).
    _daily_summary_concurrency: int       # default 8
    _pull_sync_concurrency: int           # default 4

    # Per-(user, backend) ingest serialization to make the
    # delete-then-insert dedup path of §4.3 atomic. Keyed dict on
    # the singleton, never a global lock — unrelated users fan out.
    _ingest_locks: dict[tuple[str, str], asyncio.Lock]

    # Flag thresholds (§15) — exposed as configurable params so a
    # user with naturally short sleep doesn't get tagged forever.
    _flag_low_sleep_hours: float          # default 6.0
    _flag_low_sleep_consecutive_nights: int   # default 3
    _flag_sedentary_steps: int            # default 4000
    _flag_sedentary_consecutive_days: int  # default 3
    _flag_weight_drift_kg: float          # default 2.0
    _flag_weight_drift_window_days: int   # default 14

    # Per-user metric-write rate cap — enforced inside ingest_metrics
    # regardless of how data arrived. Defends against a buggy
    # Shortcut posting every minute. Keyed on user_id.
    _per_user_write_caps: dict[str, _DailyCounter]

    # Rate-limit buckets, keyed.
    _webhook_buckets: dict[str, _Bucket]    # by webhook_token_hash
    _webhook_ip_buckets: dict[str, _Bucket]  # by remote_addr (404 path)
```

Per `memory-multi-user-isolation.md`, none of these attributes are
per-request state — every dict is keyed (per-user, per-token-hash,
per-IP) so concurrent deliveries / scheduler iterations don't
trample each other. The `_ingest_locks` keyed dict is exactly the
pattern from `SpeakerService._speaker_locks`.

### 7.5 Lifecycle

`start()`:

1. Resolve required capabilities (`entity_storage`, `scheduler`).
   Optional: `event_bus`, `configuration`, `access_control`,
   `ai_chat`, `notifications`.
2. Ensure indexes:
   - `health_metrics(user_id, metric_type, recorded_at)`
   - `health_metrics(user_id, recorded_at)`
   - `health_metrics(user_id, backend, source_event_id)`
   - `health_metrics(user_id, backend, metric_type, recorded_at)` —
     dedup fallback when `source_event_id` is empty (see §4.3,
     fixed in this revision).
   - `health_links(user_id)`
   - `health_links(webhook_token_hash)` UNIQUE (§4.2) — every
     webhook delivery hits this index.
   - `health_links(backend_name, enabled)` — pull-sync iterator.
   - `health_daily_summaries(user_id, local_date)`
   - `health_oauth_state(_id)` — OAuth state primary lookup.
   - `health_audit(target_user_id, accessed_at)` (§4.5).
3. Read config; cache prompts, thresholds, concurrency caps, and
   tunables. The `on_config_changed` cache uses
   `self._summary_prompt = str(config.get("summary_prompt", "")) or
   _DEFAULT_SUMMARY_PROMPT` so an empty override falls back to the
   default constant (per `memory-ai-prompts-configurable.md`).
4. Instantiate **each registered backend** by side-effect import
   (the boot-time loader has already registered backends from
   `std-plugins/`); `await backend.initialize(global_config)`.
   Plugins that fail to import (e.g., Withings missing `httpx`)
   are simply absent from `HealthBackend.registered_backends()` —
   the service never crashes on a missing backend; pull-sync and
   webhook routing both check for backend presence and surface a
   clear error.
5. **Seed RBAC primitives** via the `AccessControlProvider`
   capability (only if available; idempotent):
   - `health-admin` role at level 0 — created if missing, never
     granted to any user automatically. The operator grants it
     explicitly via `/roles/users`.
   - `acl_collections` rows for the four collections (defense in
     depth — the service is the trust boundary, but the entities
     page bypasses services):

     | Collection | `read_role` | `write_role` |
     |---|---|---|
     | `health_metrics` | `health-admin` | `admin` |
     | `health_links` | `health-admin` | `admin` |
     | `health_daily_summaries` | `health-admin` | `admin` |
     | `health_audit` | `health-admin` | `admin` |
     | `health_oauth_state` | `admin` | `admin` |

     Without these seeds, `acl_collections` defaults
     (`read=user, write=admin`) would let any user read every
     row from the entities page. **This is the difference between
     "private by default" working and silently failing.**
6. Subscribe to `auth.user.deleted` on the event bus → cascade
   delete (§6.6 / §6.6.1). Subscribe to `health.daily.summary` for
   the `latest_daily_summary` cache invalidation.
7. **Startup security warnings:**
   - WARN if any OAuth backend is registered AND
     `web.bind_address != 127.0.0.1` AND no TLS-fronting tunnel —
     OAuth tokens are plaintext in v1 (see §6.4).
   - WARN if `.gilbert/gilbert.db` POSIX permissions are looser
     than `0600`.
   - WARN if `health.debug_log_values` is `true` AND there is more
     than one user — the flag is global (see §6.7), so multi-user
     instances should not enable it.
8. Schedule jobs:
   - `health-daily-summary-tick` — **hourly** at the top of every
     hour. Iterates `health_links` rows whose owners' configured
     local hour matches the current wall-clock for their TZ. Fixes
     the §19.3 / DST issue (the previous "fires once at 5am UTC"
     design was wrong for any non-UTC user). See §10.1.
   - `health-pull-sync-<backend>` — `Schedule.every(pull_sync_interval_seconds)`
     for every backend that `supports_pull`. Default interval is
     **6 hours** (`21600`) — Withings data doesn't change hourly
     (weight is once a day, sleep is once a night), so polling
     more often just burns API quota.
   - `health-retention-prune` — `Schedule.daily_at(3, 0, UTC)` if
     `retention_days > 0`, deletes rows older than the cutoff.
     Audit rows pruned only if `audit_retention_days > 0`.
   - `health-oauth-state-gc` — `Schedule.every(600)` to delete
     expired `health_oauth_state` rows.

`stop()`:

- `remove_job` for each scheduled job.
- `await backend.close()` for each backend.
- Clear the rate-limit dicts and `_ingest_locks`.

### 7.6 Ingestion

```python
async def ingest_metrics(
    self,
    user_id: str,
    backend_name: str,
    metrics: list[HealthMetric],
) -> int:
    """Persist a batch of metrics for one user.

    Idempotent on (user_id, backend, source_event_id). Returns the
    count actually persisted (i.e., not duplicates).

    Atomicity: the dedup-then-write path acquires
    ``self._ingest_locks[(user_id, backend_name)]`` so concurrent
    deliveries for the same (user, backend) serialize. Different
    users / different backends fan out as normal.

    Events: publishes ``health.metric.received`` ONLY for newly-
    persisted rows. Duplicates are silently skipped — no event,
    no log line at INFO. This prevents a replay-flood from
    amplifying into the event bus.

    Per-user write cap: if the user's daily write count exceeds
    ``per_user_daily_write_cap`` (default 100k), additional rows
    are dropped with a single INFO log line per cap-exceed event
    (rate-limited so the log itself doesn't flood). The user's
    SPA settings panel surfaces a "you've hit today's write cap"
    banner so the user knows to investigate their data source.
    """
```

Called by:

- The webhook route after `backend.parse_webhook(...)` returns rows.
- The pull-sync scheduler job after `backend.sync(user_id)` returns.
- The "import" admin tool (out of scope for v1; future).

The method is NOT exposed as a tool — there's no AI-callable way to
inject metrics. This is a deliberate guard against an attacker
prompt-injecting fake readings.

### 7.7 Dispatch from the webhook route

```python
async def ingest_webhook(
    self,
    token: str,
    body: bytes,
    headers: dict[str, str],
    *,
    remote_addr: str = "",
) -> "WebhookResult":
    """Resolve token → user_id + backend, rate-limit-check, then
    ``backend.parse_webhook`` + ``ingest_metrics``.

    Returns a ``WebhookResult`` the route turns into an HTTP
    response. Does not raise on bad payloads — bad payloads return
    ``WebhookResult(status="bad_request", ...)``."""
```

All webhook errors bubble up here, never to the route. The route just
maps `WebhookResult.status` to an HTTP status code:

| `status` | HTTP | Notes |
|---|---|---|
| `ok` | 200 | `{"received": <count>, "dropped": <count>}` |
| `bad_request` | 400 | Parser raised on a malformed payload (whole batch unparseable). Per-metric validation failures drop the metric and return `ok` — see §6.3. |
| `payload_too_large` | 413 | `Content-Length` exceeds `webhook_max_body_bytes` (default 1 MB) |
| `not_found` | 404 | Token doesn't resolve OR the user disabled this backend. Both collapse to the same response shape (body, headers, latency) to avoid token enumeration. |
| `rate_limited` | 429 | `Retry-After` header from the bucket |

Removed: `disabled` (503). A "your token is real but disabled" 503
is itself an enumeration channel — an attacker can probe the token
space and see which tokens are real. v1 collapses this case into
`not_found` to remove the side channel.

## 8. Web routes

### 8.1 Webhook ingestion

`src/gilbert/web/routes/health.py`:

```python
router = APIRouter(prefix="")    # webhook lives at root, not /api

@router.post("/webhook/health/{token}")
async def health_webhook(
    token: str,
    request: Request,
) -> Response:
    """Per-user push-style ingestion. The token is the only auth.

    Thin route: pulls the HealthService capability, calls
    ``ingest_webhook(token, body, headers)``, maps the result to a
    response. No business logic here."""
```

The route does NOT live under `/api` because some Shortcut clients
have trouble with auth-redirected `/api` paths if a tunnel is in
front. Putting it at `/webhook/...` keeps it path-isolated.

### 8.2 Per-user account routes

The `/api/health/me/*` routes (under the existing API router) let the
SPA drive the link/disconnect/rotate flows. All require an
authenticated `UserContext`:

| Route | Purpose |
|---|---|
| `GET /api/health/me/links` | List the current user's `health_links` rows (token redacted, OAuth secrets redacted; only `enabled`, `last_sync_at`, `last_sync_error`, `webhook_url` (when push)). |
| `POST /api/health/me/connect/{backend}` | Calls `backend.begin_link(user_id)`. Returns `{open_url, followup_action_key, webhook_url}`. |
| `POST /api/health/me/complete/{backend}` | Calls `backend.complete_link(user_id, payload)`. |
| `POST /api/health/me/disconnect/{backend}` | Calls `backend.disconnect(user_id)`; deletes the row; publishes `health.link.disconnected`. |
| `POST /api/health/me/rotate-token/{backend}` | Issue a new `webhook_token` for a push backend. |
| `POST /api/health/me/delete-all` | Right-to-delete (§6.6). |
| `GET /api/health/me/metrics` | Read-metrics with `?metric_type=...&since=...&until=...`. Used by the per-metric SPA view. |
| `GET /api/health/me/summary` | Latest `DailySummary` for the current user. |

### 8.3 Admin routes (gated)

| Route | Purpose | Required role |
|---|---|---|
| `GET /api/health/admin/users` | Per-user counts (`{user_id, has_data, backends, last_ingested_at}`). No values. | `admin` |
| `GET /api/health/admin/users/{user_id}/metrics` | Read another user's metrics. | `health-admin`. Audit-logged + target-user notified. |
| `GET /api/health/admin/audit-log` | Read every `health_audit` row. | `health-admin` |

The cross-user-read route runs through `can_read_metrics()` with
`is_health_admin = "health-admin" in caller.user_ctx.roles`. Absence
of the role returns 403, regardless of `admin` membership. A user
holding `admin` but not `health-admin` sees the user list (counts
only, no values) but cannot drill in.

### 8.4 OAuth callback route (generic)

```
GET /api/health/me/oauth/{backend}/callback
```

A single route in core handles every OAuth backend — Withings today,
Garmin / Oura / Fitbit additions later require zero new routes. The
route:

1. Reads `code`, `state`, optional `error` from the query string.
2. Looks up `health_oauth_state(state)` — must exist, not expired,
   not already consumed, and `backend_name == {backend}`.
3. **Verifies `state.user_id == caller.user_ctx.user_id`** — if the
   session expired or the caller is a different user, the link
   does not complete (returns an error page asking the user to
   restart from `/account`). This defeats the confused-deputy
   attack where an attacker initiates an OAuth flow and tricks a
   victim into completing it on their account.
4. Marks the state row `consumed_at = now`.
5. If `error` query param is set (user denied access on the
   provider's screen), record `last_sync_error` and redirect to the
   account page with a flash message — no token exchange.
6. Calls `HealthService.complete_link(user_id, backend, {"code":
   code})` → `backend.complete_link(user_id, payload)`.
7. Backend exchanges `code` for `access_token` + `refresh_token`,
   persists them on the link row, returns
   `LinkCompleteResult(status="ok")`.
8. Route redirects the browser to `/account` with a success flash.

**Idempotency on double-callback:** if the user double-clicks the
redirect, the second call finds `consumed_at` set and returns a
benign "already linked" page rather than re-exchanging the code
(which Withings would reject anyway).

## 9. AI tools

All tools default to `required_role="user"` (per
`memory-access-control.md`); they read `_user_id` from the injected
arguments and operate on that user. None of the tools accept a
`user_id` argument from the model.

| Tool name | Slash | `slash_help` | Purpose |
|---|---|---|---|
| `health_now` | `/health now` | "What do you know about me right now?" | The catch-all "how am I doing?" tool — latest sleep, today's steps so far, latest weight, latest resting HR, plus a one-liner summary for the 24h window ending now. **Will be the most-invoked tool**; one shot, deterministic-data-fetch + one AI sentence. |
| `latest_health` | `/health latest <metric>` | "Latest reading of a metric: /health latest <metric>" | Most-recent reading of one metric. Pure data lookup (no AI). |
| `health_summary` | `/health summary [period]` | "Summarize a period: /health summary [today\|yesterday\|week]" | Natural-language summary, driven by `_summary_prompt`, non-clinical. **Default period is `yesterday`** because it consumes the pre-computed `health_daily_summaries` row; users wanting "today so far" use `/health now`. |
| `health_trend` | `/health trend <metric> [weeks]` | "Trend of a metric: /health trend <metric> [4]" | Direction + rate + consistency, driven by `_trend_prompt` (revised — see §11.1). |
| `sleep_last_night` | `/health sleep` | "Last night's sleep: /health sleep" | Convenience for `latest_health` of `SLEEP_DURATION` + efficiency. Pure data. |
| `steps_today` | `/health steps` | "Today's step count: /health steps" | Pure data: latest `STEPS` aggregated against the user's local date. |
| `weight_trend` | `/health weight [weeks]` | "Weight slope: /health weight [4]" | Convenience: a 4-/8-/12-week weight slope. |
| `health_links` | `/health links` | "Connected health sources: /health links" | Show the user's connected backends, their status, last delivery / last sync. |
| `health_delete_my_data` | *(no slash command)* | n/a | Erase everything — gated behind a two-step confirmation UI block (preview-counts then `confirm: "DELETE"`). **Deliberately not exposed as a slash command** so a stray tab-completion can't put it next to `/health summary` in the autocomplete list — same friction principle as `memory-ui-blocks.md` recommends for destructive flows. |

**Naming notes:**

- `slash_group = "health"` on every tool that has a slash.
- The "convenience tools" (`sleep_last_night`, `steps_today`,
  `weight_trend`, `latest_health`, `health_links`) are **pure
  storage queries** with **no AI call**. They are tools defined by
  the AI service (so the chat model can pick them) but their
  execution is deterministic. Don't conflate "AI-callable tool"
  with "tool that calls the AI."
- The tools that **do** call the AI are `health_now` (one short
  sentence), `health_summary`, and `health_trend`. They use the
  service's configured `ai_profile` (default `"standard"` — see
  §11). `light` is the wrong tier here: the prompts have careful
  forbidden-word constraints that smaller models are more likely
  to drop.

The greeting integration does NOT call these tools; it calls
`HealthProvider.health_brief_for_greeting()` (structured snapshot)
and optionally `latest_daily_summary()` directly through the
capability resolver. This keeps greeting cheap (no tool dispatch)
and gives the greeting prompt facts to react to in its own voice
(see §14).

## 10. Scheduler integration

The scheduler loops use existing primitives only — `set_current_user`
+ `copy_context()` + `asyncio.gather` with a semaphore. The earlier
draft assumed a `use_user_context` contextmanager and a
`UserContext.system_for_user(user_id)` factory; **neither exists in
`gilbert.core.context` or `gilbert.interfaces.auth` today**, and we do
not introduce them in this PR — adding new context primitives just to
match a contextmanager idiom muddies the existing
`set_current_user`-at-entry pattern (`core/services/inbox_ai_chat.py`,
`core/services/ai.py`). Snapshot/restore inline is verbose by a few
lines but avoids inventing infrastructure. The pattern below is
copy-pasteable from `core/services/ai.py` for parallel tool execution.

A bounded-concurrency helper lives in the service:

```python
async def _run_per_user(
    self,
    user_ids: list[str],
    work: Callable[[str], Awaitable[None]],
    *,
    concurrency: int,
    label: str,
) -> None:
    """Run ``work(user_id)`` for each user with bounded concurrency.

    Each task gets its own copy of contextvars (per
    ``memory-multi-user-isolation.md``) so the per-task
    ``set_current_user`` doesn't leak to siblings.
    """
    sem = asyncio.Semaphore(concurrency)

    async def _one(user_id: str) -> None:
        async with sem:
            ctx = copy_context()

            def _runner() -> Awaitable[None]:
                # Run inside the copied context so ``set_current_user``
                # below stays scoped to this task.
                set_current_user(_system_acting_for(user_id))
                return work(user_id)

            try:
                await asyncio.create_task(_runner(), context=ctx)
            except Exception:
                logger.exception("%s failed for %s", label, user_id)

    await asyncio.gather(*[_one(uid) for uid in user_ids],
                         return_exceptions=False)


def _system_acting_for(user_id: str) -> UserContext:
    """Return a SYSTEM-acting-for-target identity for scheduler work.

    The actor remains ``UserContext.SYSTEM`` (so the audit log
    correctly shows scheduler activity as ``actor="system"``, never
    masquerading as the target user); ``metadata["target_user_id"]``
    carries the target so the service's per-user filter still
    operates on the right user. The service code paths read the
    target via ``metadata.get("target_user_id")`` (falling back to
    ``user_ctx.user_id``) so SYSTEM-with-target works without the
    target-user being shadowed as the actor."""
    return replace(
        UserContext.SYSTEM,
        metadata={"target_user_id": user_id},
    )
```

Why "actor stays SYSTEM, target via metadata" rather than minting a
synthetic user identity: the audit log MUST distinguish "the
scheduler did this on behalf of user X" from "user X did this." A
synthetic `UserContext(user_id=user_id, provider="system")` would
make the daily-summary job indistinguishable from a cross-user read
in the audit trail. The `metadata["target_user_id"]` channel keeps
identity unambiguous.

### 10.1 Daily summary job — hourly TZ-aware tick

A single recurring job, `health-daily-summary-tick`, fires at the
**top of every hour** (UTC). For each tick, it queries every user
who has at least one `health_links` row with `enabled=true` AND
whose configured `daily_summary_local_hour` (per-user, default 5)
matches the current wall-clock hour in **the user's local time
zone** (read from the existing user profile — same data the alarm
service uses).

Boundary computation per user uses the user's local TZ via
`zoneinfo.ZoneInfo(user_tz)`:

```
local_now      = datetime.now(ZoneInfo(user_tz))
local_today_0  = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
local_yest_0   = local_today_0 - timedelta(days=1)
window_start   = local_yest_0.astimezone(timezone.utc)
window_end     = local_today_0.astimezone(timezone.utc)
```

This works correctly across DST transitions: spring-forward windows
are 23 hours, fall-back are 25 hours, by construction (the local
midnights are unambiguous; UTC conversion handles the offset
change). Metrics whose `recorded_at` falls in the spring-forward
gap-hour don't exist (no device emits them); fall-back fold-hour
metrics are correctly attributed by their UTC `recorded_at`.

```python
async def _run_daily_summary_tick(self) -> None:
    user_ids = await self._users_due_at_current_hour()
    await self._run_per_user(
        user_ids,
        self._compute_and_persist_summary,
        concurrency=self._daily_summary_concurrency,
        label="daily-summary",
    )
```

Per-user computation (`_compute_and_persist_summary(user_id)`):

1. Compute the user's local "yesterday" window (above).
2. Read the metrics in that window. Compute headline values (sleep
   duration, steps, resting HR, weight, BP). Missing values pass
   through as `None` to the prompt so the model says "no sleep data
   for last night" instead of hallucinating.
3. **Compute flags in code** (not from the AI) per the §15
   thresholds — `low_sleep`, `sedentary`, `weight_drift`. The
   thresholds are configurable via §11 params so a user with
   naturally short sleep doesn't get tagged forever.
4. Build a **brief structured-prose** description of the headline
   values (e.g., "Sleep: 5h 12m. Steps yesterday: 8,431. Resting
   HR: 62 bpm. No weight reading."). Pre-formatting beats handing
   the model raw JSON — fewer field-name fumbles, stronger prompts.
5. Call `AISamplingProvider.complete_one_shot(system_prompt=self._summary_prompt, ...)`.
6. Persist a `health_daily_summaries` row with the structured
   `metrics_snapshot`, the AI-generated `summary_text`, and the
   code-computed `flags`.
7. Publish `health.daily.summary` on the event bus with
   `{user_id, local_date, summary_text, flags, metrics_snapshot}`.
   The greeting service consumes this; the proposals integration
   consumes it via the observation pipeline (§15).

### 10.2 Pull-sync job

A per-backend recurring job runs every `pull_sync_interval_seconds`
(default 6 hours) and iterates active link rows for that backend
with bounded concurrency:

```python
async def _run_pull_sync(self, backend_name: str) -> None:
    rows = await self._active_links_for_backend(backend_name)
    backend = self._backends.get(backend_name)
    if backend is None or not backend.supports_pull:
        return
    user_ids = [row.user_id for row in rows]

    async def _sync_one(user_id: str) -> None:
        try:
            await backend.sync(user_id)
            await self._update_last_sync(user_id, backend_name, error="")
        except HealthBackendRateLimitError as exc:
            # Honor the provider's Retry-After.
            await self._update_last_sync(user_id, backend_name, error=str(exc))
            await asyncio.sleep(exc.retry_after_seconds)
        except HealthBackendAuthError as exc:
            # Token expired and refresh failed; mark for the SPA
            # to surface a "reconnect" prompt.
            await self._update_last_sync(user_id, backend_name, error=str(exc))
        except Exception as exc:
            await self._update_last_sync(user_id, backend_name, error=str(exc))
            raise

    await self._run_per_user(
        user_ids,
        _sync_one,
        concurrency=self._pull_sync_concurrency,
        label=f"pull-sync.{backend_name}",
    )
```

**Backend error taxonomy** (defined in `interfaces/health.py`):

- `HealthBackendAuthError` — refresh failed; user must reconnect.
- `HealthBackendRateLimitError(retry_after_seconds)` — provider
  threw 429; respect their backoff.
- `HealthBackendTransientError` — 5xx, timeout; retry on next
  scheduled run.
- `HealthBackendNotFoundError` — provider's "user/resource gone."

The `last_sync_error` string surfaces in the SPA per the link row,
so "my Withings data isn't syncing" stops being a logs-only debug
experience. After 5 consecutive `HealthBackendAuthError`s, the link
row sets `enabled=false` and surfaces a "reconnect" UI prompt.

### 10.3 Retention pruning job

If `retention_days > 0`, a daily job at 03:00 deletes rows from
`health_metrics` and `health_daily_summaries` older than `now - retention_days`.
Default `retention_days` is **0 (keep forever)** because health data is
the user's own and pruning by surprise is worse than disk usage.

## 11. Configuration (`HealthService.config_params`)

`config_namespace = "health"`, `config_category = "Personal Data"`.

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | BOOLEAN | `true` | Toggle the whole service. |
| `daily_summary_local_hour` | INTEGER | `5` | Local hour for the daily summary job (per-user override on the user profile, falling back to this default). |
| `pull_sync_interval_seconds` | INTEGER | `21600` (6h) | How often pull backends sync. **Bumped from 1h** — Withings data doesn't change hourly. |
| `daily_summary_concurrency` | INTEGER | `8` | Max concurrent daily-summary tasks per tick. |
| `pull_sync_concurrency` | INTEGER | `4` | Max concurrent pull-sync tasks per backend per run. |
| `max_backfill_days` | INTEGER | `90` | Webhook deliveries with `recorded_at` older than this are dropped. |
| `retention_days` | INTEGER | `0` | 0 = keep forever. |
| `audit_retention_days` | INTEGER | `0` | 0 = keep forever (default — audit rows outlive metrics deliberately). |
| `per_user_daily_write_cap` | INTEGER | `100000` | Per-user metric writes-per-day cap, regardless of source. Drops over-cap rows with a single INFO log. |
| `webhook_max_body_bytes` | INTEGER | `1048576` (1 MB) | Reject webhook deliveries above this size with 413. |
| `webhook_max_metrics_per_delivery` | INTEGER | `1000` | Reject deliveries with more metrics than this with 400. |
| `webhook_rate_per_minute` | INTEGER | `60` | Per-token bucket size. |
| `webhook_unknown_rate_per_minute` | INTEGER | `30` | Per-IP bucket on the 404 (unknown-token) path. |
| `debug_log_values` | BOOLEAN | `false` | If true, DEBUG logs include metric values. **Global** flag — startup WARN if true with >1 user (see §6.7). |
| `ai_profile` | STRING (`choices_from="ai_profiles"`) | `"standard"` | Which AI profile the daily-summary / health_summary / health_trend / health_now AI calls use. **`standard`** because the prompts have careful forbidden-word constraints; `light` is more likely to drop them. |
| `summary_prompt` | STRING (multiline, **`ai_prompt=true`**) | `_DEFAULT_SUMMARY_PROMPT` | Daily summary system prompt. (Author-with-AI works automatically with `ai_prompt=True`.) |
| `trend_prompt` | STRING (multiline, **`ai_prompt=true`**) | `_DEFAULT_TREND_PROMPT` | Trend interpretation system prompt. |
| `flag_low_sleep_hours` | FLOAT | `6.0` | Threshold for the `low_sleep` flag (§15). |
| `flag_low_sleep_consecutive_nights` | INTEGER | `3` | Consecutive-night count for `low_sleep`. |
| `flag_sedentary_steps` | INTEGER | `4000` | Threshold for the `sedentary` flag. |
| `flag_sedentary_consecutive_days` | INTEGER | `3` | Consecutive-day count for `sedentary`. |
| `flag_weight_drift_kg` | FLOAT | `2.0` | Magnitude (kg) for the `weight_drift` flag. |
| `flag_weight_drift_window_days` | INTEGER | `14` | Window for `weight_drift`. |

### 11.1 Bundled prompts (non-clinical)

`_DEFAULT_SUMMARY_PROMPT`:

```
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
```

`_DEFAULT_TREND_PROMPT` (revised — the original forbade
interpretation entirely, leaving a tool that produced nothing a
calculator wouldn't):

```
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
```

These constants are the `default=` of their `ConfigParam` and are
**never referenced at the call site** — the service caches them on
`self._summary_prompt` / `self._trend_prompt` in `on_config_changed`,
using the `(str(config.get(key, "")) or _DEFAULT_*)` pattern so an
empty override falls back to the bundled default. Per
`memory-ai-prompts-configurable.md`.

### 11.2 Backend-merged params

Each backend's `backend_config_params()` is merged into the service's
config under the standard `settings.<backend>.<key>` prefix with
`backend_param=True`. Forward `ai_prompt=bp.ai_prompt` if a backend
ever declares one (none do in v1). Backends declared:

- **Apple Health** — no global config; everything is per-user.
- **Withings** — global `client_id` (sensitive) and `client_secret`
  (sensitive). Per-user `oauth_*` lives on `health_links`, never in
  `backend_config_params()`.
- **HKWebhook** — no global config.

### 11.3 ConfigActions

The service's `config_actions()` exposes a single global "test
storage" action; the *interesting* actions live on the per-user
account page:

- **Per-user link flow** — for each backend, a "Connect" /
  "Disconnect" / "Rotate token" button rendered by the per-plugin
  panel under the `account.extensions` slot. The button calls the
  `/api/health/me/connect/<backend>` route (or the equivalent WS
  RPC) which delegates to `backend.begin_link(user_id)` →
  `LinkStartResult` with `open_url` for OAuth or `webhook_url` for
  push. Two-phase flow follows the existing `followup_action`
  contract from `memory-config-actions`.

### 11.4 Summary text is display-only, never instructional

The AI-generated `summary_text` is **persisted as text and rendered
as text** — never parsed as instructions, never fed back into a
tool dispatcher, never used to compute `flags`. An admin (or a
user with config access) can edit `summary_prompt` to anything; if
they edit it to something like `"Output JSON like {\"action\":
\"delete_all\"}\""`, nothing acts on the resulting text. The
`flags` field on `health_daily_summaries` is **computed in code**
from `metrics_snapshot` per the §15 thresholds — the AI never
populates `flags`, never names new flags, and the proposals
integration consumes `flags` (not `summary_text`) for its triggers.

## 12. Plugin specs

### 12.1 `std-plugins/apple-health/`

**Purpose:** Translate iOS Shortcut webhook deliveries into
`HealthMetric` rows.

**Files:**
- `plugin.yaml` — name `apple-health`, no deps.
- `plugin.py` — side-effect-imports `apple_health_backend`.
- `apple_health_backend.py` — `AppleHealthBackend(HealthBackend)`
  with `backend_name = "apple-health"`, `supports_push = True`,
  `supports_pull = False`.
- `frontend/AppleHealthPanel.tsx` — registered to slot
  `account.extensions`. Shows the user's webhook URL once a token is
  generated, plus copy-paste instructions for the iOS Shortcut.
- `frontend/panels.ts` — `registerPanel("apple-health.account",
  AppleHealthPanel)`.

**Webhook payload format:**

```json
{
  "metrics": [
    {"type": "sleep_duration", "value": 27000, "unit": "s",
     "recorded_at": "2026-05-08T07:30:00-07:00"},
    {"type": "steps", "value": 8431, "unit": "count",
     "recorded_at": "2026-05-08T23:59:00-07:00"},
    {"type": "weight", "value": 79.4, "unit": "kg",
     "recorded_at": "2026-05-08T07:00:00-07:00"}
  ]
}
```

The backend translates HealthKit identifier names (e.g.
`HKQuantityTypeIdentifierStepCount`) to `MetricType` via a fixed
mapping table; unknown types are dropped with an INFO log line.

**Headline path: prebuilt iCloud Shortcut.** The SPA panel's
*first* button is "Install our Shortcut" — a one-click iCloud
Shortcut link to a curated, signed Shortcut hosted on a GitHub
Release of the `apple-health` plugin repo. The Shortcut handles
the messy bits the manual setup gets wrong:
- Sums sleep duration over the night using HealthKit's session
  boundaries (not midnight-cuts) and reports a single
  `sleep_duration` row.
- Source-filters steps to a single source (default: iPhone) so
  iPhone + Apple Watch + third-party apps don't double-count.
- Carries `recorded_at` as the device-local sample timestamp,
  not the time the Shortcut runs.

**Supply-chain hardening** (because anyone hijacking the plugin
repo could push a malicious Shortcut that exfiltrates the user's
webhook URL): the panel shows the SHA-256 hash of the expected
Shortcut bundle next to the install button so a paranoid user can
compare. The panel also documents the manual setup so users aren't
forced to trust the prebuilt link.

**Manual fallback instructions** (also in the panel, for
non-iCloud users):

1. iOS Shortcuts → "+" → Add action → "Find Health Samples".
   Configure to find samples for whatever data you want to share.
2. Add "Get URL Contents" → URL = (the per-user URL the panel
   shows) → Method POST → JSON body using the format above.
3. Schedule the Shortcut as an Automation (e.g., daily at midnight).

**Failure-mode disclosure** — rendered above the install button so
users know what they're signing up for:

> iOS Shortcut Automations only run while your phone is unlocked
> at the scheduled time, and iOS sometimes revokes Background
> App Refresh on Shortcuts after major iOS updates. If your
> daily summary stops updating, check that the Automation is
> still enabled in Settings → Shortcuts → Automation. The panel
> below shows when we last received a delivery — if it says
> "more than 36 hours ago," that's the smoking gun.

**Last-delivery indicator** — the panel renders
`last_delivery_at` (from §4.2) as "Last delivery: 4 hours ago." A
silently-broken automation is now visible.

### 12.2 `std-plugins/withings/`

**Purpose:** OAuth 2.0 client for Withings Public Cloud API; pull
sleep, weight, BP, HR readings into `HealthMetric` rows.

**Files:**
- `plugin.yaml` — name `withings`.
- `pyproject.toml` — `dependencies = ["httpx>=0.27"]` (already
  in core; declared explicitly for clarity).
- `plugin.py` — side-effect imports `withings_backend`.
- `withings_backend.py` — `WithingsBackend(HealthBackend)` with
  `backend_name = "withings"`, `supports_pull = True`,
  `supports_push = False`.
- `frontend/WithingsPanel.tsx` — `account.extensions` panel with
  Connect / Disconnect / Sync-now buttons.

**Admin precondition: `gilbert.public_base_url` must be set.** The
Withings developer-app dashboard requires a fixed `redirect_uri`
registered ahead of time. Before any user can connect, the operator
must:
1. Set `gilbert.public_base_url` in core settings to the URL
   Gilbert is reachable at (e.g., `https://gilbert.example.com`).
2. Register `<public_base_url>/api/health/me/oauth/withings/callback`
   as the redirect URI in the Withings Developer Dashboard.

The Withings panel in §12.2 (frontend):
- Shows the configured callback URL alongside the Connect button so
  the admin can copy it into the Withings dashboard.
- **Disables Connect** if `public_base_url` is unset, with an
  explainer pointing the admin at `/system` to set it. The backend's
  `begin_link(user_id)` returns
  `LinkStartResult(status="error", message="Admin needs to set
  gilbert.public_base_url before Withings can be connected.")` if
  invoked anyway, so the user never starts an OAuth flow that's
  guaranteed to fail at the redirect step.

**OAuth flow (with state hardening — §4.2.1, §8.4):**

1. SPA: user clicks Connect.
2. SPA → `POST /api/health/me/connect/withings` →
   `HealthService.begin_link(user_id, "withings")` →
   `WithingsBackend.begin_link(user_id)`.
3. Backend mints a 32-byte random `state`, persists a
   `health_oauth_state` row `{state_id, user_id, backend_name,
   created_at, expires_at = now + 10min}`, and builds the
   `https://account.withings.com/oauth2_user/authorize2?...&state=<state_id>`
   URL.
4. Returned `LinkStartResult` carries `open_url`. SPA opens
   it in a new tab.
5. Withings redirects back to
   `<public_base_url>/api/health/me/oauth/withings/callback?code=...&state=...`.
   Route handling per §8.4: state lookup, expiry check, **server-side
   binding to the calling session's user_id** (defeats confused-deputy),
   one-shot consume, then `complete_link(user_id, "withings", {"code":
   code})`.
6. Backend exchanges `code` for `access_token` + `refresh_token`
   (POST to `https://wbsapi.withings.net/v2/oauth2`), persists on the
   `health_links` row, returns `LinkCompleteResult(status="ok")`.
7. Route redirects to `/account` with a success flash.

**Failure modes covered:**
- Withings returned `error=access_denied` (user clicked "Deny"):
  route reads the error param, records `last_sync_error`, redirects
  to `/account` with an explanatory flash; no token exchange.
- User double-clicks the redirect: state row is `consumed_at != null`
  on the second hit; route returns "already linked" benignly.
- Withings's authorization code has been used once (bug double-call,
  buggy proxy): backend's `complete_link` catches the 4xx and surfaces
  it as a clean "couldn't complete; please retry from /account."

**Sync** uses `getmeas`, `getsleep`, and `getheartlist` endpoints,
honoring the `lastupdate` cursor stored on the link row.

**Token refresh:** on a 401 from the API, the backend refreshes using
`refresh_token` (Withings access tokens last ~3h, refresh tokens are
long-lived) and retries the request once. A 401 on the refresh itself
raises `HealthBackendAuthError`; the link row's `last_sync_error`
surfaces a "reconnect" prompt in the SPA.

**Disconnect revokes upstream.** `WithingsBackend.disconnect(user_id)`
overrides the default and calls
`POST https://wbsapi.withings.net/v2/oauth2?action=revoke&...` with
the access token before deleting the local row. Revocation failure
(network, 5xx) logs a WARN but **does not** block local row deletion
— the user's belief ("I disconnected") must be honored locally even
when the upstream is unreachable.

**Right-to-delete revokes upstream too.** The §6.6 `delete_all` flow
calls `disconnect(user_id)` for every linked OAuth backend before
deleting the link row, so revocation happens as part of the delete
cascade, not as a separate step the user has to remember.

**Manual QA checklist (v1 ships without an integration test that
actually exercises Withings OAuth):**

1. Connect against a real Withings sandbox account → callback
   completes → row exists with `enabled=true`, tokens present.
2. Trigger a sync → real measurements appear in `health_metrics`.
3. Set the access token's expiry to the past → next sync refreshes
   and succeeds.
4. Revoke the grant from the Withings dashboard → next sync raises
   `HealthBackendAuthError` and surfaces a reconnect prompt.
5. Disconnect → upstream revoke succeeds, local row gone.
6. Re-connect → fresh row, sync resumes from the `lastupdate`
   cursor (or from scratch if cursor was wiped).

### 12.3 `std-plugins/hk-webhook/`

**Purpose:** Generic catch-all webhook for users who want to push
metrics from anywhere — Garmin Connect IQ widget, Home Assistant
automation, custom python script, etc.

**Files:**
- `plugin.yaml` — name `hk-webhook`.
- `pyproject.toml` — `dependencies = []`.
- `plugin.py` — side-effect imports `hk_webhook_backend`.
- `hk_webhook_backend.py` — `HKWebhookBackend(HealthBackend)` with
  `backend_name = "hk-webhook"`, `supports_push = True`.

**Payload format** is the same as Apple Health's (see §12.1).
Unknown metric types are dropped with an INFO log; this is the
only difference from Apple Health — there's no HealthKit
identifier mapping, just direct `MetricType` values in the payload.

**Frontend panel:** identical structure to Apple Health, no
Shortcut instructions — instead a curl example and a one-liner
Python snippet.

## 13. Events

| Event | Payload | Fired by |
|---|---|---|
| `health.metric.received` | `{user_id, backend, metric_type, value, unit, recorded_at}` | After each **newly-persisted** insert in `ingest_metrics`. Duplicates do NOT emit this event (defeats replay-flood amplification — see §7.6). Value IS in payload (this is the per-user event stream); see §6.7 for log redaction at INFO. Events are fanned-out per-user, never broadcast. |
| `health.metric.deleted` | `{user_id, count, scope}` (`scope` is `"user-deleted"` / `"retention"` / `"backend-disconnect"`) | Cascade deletes. |
| `health.daily.summary` | `{user_id, local_date, summary_text, flags, metrics_snapshot}` | Daily summary job. |
| `health.link.connected` | `{user_id, backend}` | After `complete_link` succeeds. |
| `health.link.disconnected` | `{user_id, backend, upstream_revoked: bool}` | After `disconnect` succeeds. `upstream_revoked` distinguishes "we just deleted the local row" from "we revoked the OAuth grant too." |
| `health.access.audit` | `{actor_user_id, target_user_id, kind, accessed_at, metric_types, period_start, period_end}` | Cross-user read or self-delete (§4.5, §6.6). |

**ACL prefix:** add `"health."` to `interfaces/acl.py` at level 100
(`user`) for default visibility — the WS fanout filter narrows
delivery further. The new per-event filter
`can_see_health_event(event)` in `web/ws_protocol.py`:

- For `health.metric.*` and `health.daily.summary`:
  `event.data["user_id"] == conn.user_id` only. Mirrors the
  notification filter pattern.
- For `health.access.audit`:
  `(event.data["actor_user_id"] == conn.user_id) OR
   (event.data["target_user_id"] == conn.user_id) OR
   ("admin" in conn.user_ctx.roles)` —
   i.e., the actor sees their own audit trail; the target sees
   they were read; admins see the global trail. Uses the same
   filter-shape as the existing `can_see_auth_event` filter for
   `auth.user.roles.changed` (`web/ws_protocol.py`).
- For `health.link.connected` / `health.link.disconnected`:
  `event.data["user_id"] == conn.user_id` only.

The filter API in `WsConnection` already supports per-event
predicates (see `can_see_workspace_event`,
`can_see_notification_event`, `can_see_chat_event` —
`web/ws_protocol.py:117-190`); this PR adds one more,
`can_see_health_event`, in the same shape. No new generic
extension point is required.

The auth-event prefix is already at level 100 in `interfaces/acl.py`
so `auth.user.deleted` (added in §6.6.1) routes through admin-only
visibility per the existing filter — no additional ACL change
required for that event.

## 14. Greeting integration

The existing greeting service (`Soul`-driven assistant message at
session start) gets a **structured** health input — not just a
pre-canned paragraph — so the greeting model can react to facts in
its own voice rather than stapling a separately-generated summary
onto its output.

**Data shape (from `HealthProvider.health_brief_for_greeting`):**

The greeting prompt template gets new variables filled from a
`GreetingBrief` (see §7.2):

- `{{ health_has_data }}` — bool; if false, greeting renders no
  health-related sentence.
- `{{ health_sleep_hours }}` — float or null
- `{{ health_steps_today_so_far }}` — int or null
- `{{ health_weight_latest }}` — float or null (with unit)
- `{{ health_resting_hr }}` — float or null
- `{{ health_flags }}` — list of `low_sleep` / `sedentary` /
  `weight_drift` strings (subset of §15 vocabulary)

**Behavior:**

- The greeting prompt receives the structured values **as facts**
  and is instructed (in the existing greeting `system_prompt`)
  that it MAY weave a brief health observation into the greeting
  in its own voice, subject to the same non-clinical constraints
  as the daily-summary prompt (no diagnose, no causes, no
  forbidden words).
- If `health_has_data` is false, the prompt is told there's no
  data and to skip any health line — no awkward "I don't know how
  you slept."
- The greeting service ALSO subscribes to
  `health.daily.summary` to invalidate any per-user
  `latest_daily_summary` cache; it can optionally surface
  `summary_text` as a *separate* "today's health" sentence
  appended to the greeting if the operator prefers the simpler
  shape (toggleable via greeting config).

**What this PR does NOT ship:**

The §1 motivating example's "I dimmed the morning meeting reminders
and skipped the loud playlist" sentence is **not produced by this
PR**. The greeting model has no automation tools today
(`tools_override=[]` in `core/services/greeting.py`); making it
*act* on the data, not just describe it, requires the greeting
service to grow tool dispatch. That's a separate feature. v1 ships
the *informed* part of the marketing line; the *causal-action*
part is v2.

The integration lives in the existing greeting code and consumes
`HealthProvider` via `resolver.get_capability("health")` +
`isinstance`. It does NOT depend on `HealthService` directly — the
capability-protocol pattern from `memory-capability-protocols.md`.

## 15. Proposals integration

`ProposalsService` (per `memory-proposals-service.md`) periodically
reflects on activity and produces self-improvement proposals. Health
data flows in via the **existing observation pipeline**, not via a
new prompt-fragment mechanism — `proposals.py` builds its reflection
prompt from observation buffer rows tagged with `source_type` and
patterns, and that's the right hook for this feature.

**Hook:** `HealthService` publishes `health.daily.summary` events
(§13). The proposals service already subscribes to bus events
matching its `observation_event_patterns` config (see
`memory-proposals-service.md`). Adding `health.daily.summary` to
that pattern list (or its successor) is the only configuration
change needed; no new contributor protocol, no new extension point.

**What proposals sees:** each `health.daily.summary` event becomes
one observation row with `source_type="event"` and a small body
synthesized from `flags` + headline values. The reflector's
existing prompt processes observations regardless of source.

**Internal flag vocabulary (small, fixed; thresholds configurable
per §11):**

- `low_sleep` — sleep duration below `flag_low_sleep_hours`
  (default 6.0) for `flag_low_sleep_consecutive_nights` (default 3)
  consecutive nights.
- `sedentary` — steps below `flag_sedentary_steps` (default 4000)
  for `flag_sedentary_consecutive_days` (default 3) consecutive
  days.
- `weight_drift` — weight change of at least `flag_weight_drift_kg`
  (default 2.0) over `flag_weight_drift_window_days` (default 14).

The thresholds are configurable so a user with naturally short
sleep doesn't get tagged forever; flags are computed in code from
`metrics_snapshot` on the daily-summary path; the AI never invents
new flags.

**What this PR does NOT ship:**

The §1 motivating example "three nights of poor sleep — propose
dimming bedroom lights at 21:30 instead of 22:30" requires the
reflector to know the user's *current* automation schedule (so it
can suggest "1 hour earlier" rather than a generic "consider an
earlier wind-down"). The reflector doesn't currently ingest
scheduler state. v1 ships the **generic** version of the proposal
("consider an earlier wind-down routine"); the **specific** time-
delta version is v2 once a "current scheduled actions" context
block lands in proposals. The §1 example is reframed accordingly.

## 16. Unit testing

### 16.1 Layout

- `tests/unit/test_health_service.py` — auth matrix, ingestion
  de-dup, aggregator math, retention pruning, cascade delete.
- `tests/unit/test_health_webhook_route.py` — token resolution,
  rate-limiting, malformed payload handling.
- `tests/unit/test_health_daily_summary.py` — summary job with a
  fake `AISamplingProvider`, locale handling, missing-data path,
  prompt cache wired correctly (no `_DEFAULT_*` reference at call
  site).
- `std-plugins/apple-health/tests/test_apple_health_backend.py` —
  payload parsing, HealthKit identifier mapping, unknown-type
  dropping.
- `std-plugins/withings/tests/test_withings_backend.py` — OAuth
  state machine with mocked `httpx.AsyncClient`, token refresh on
  401, cursor advancement.
- `std-plugins/hk-webhook/tests/test_hk_webhook_backend.py` —
  generic payload happy path + bad-shape rejection.

### 16.2 Database tests

Per CLAUDE.md, DB tests use a real SQLite database. The
`tests/integration/test_health_storage.py` exercises:

- Insert + query by `(user_id, metric_type, recorded_at)`.
- De-dup on `(user_id, backend, source_event_id)`.
- Cascade delete on `auth.user.deleted` (per §6.6.1).
- Index existence (verifying `ensure_index` actually fires).

### 16.3 Auth matrix test (mandatory)

A parametrized test covers every cell of the table in §6.1:

```python
@pytest.mark.parametrize(
    "actor, target, is_health_admin, expected",
    [
        ("alice", "alice", False, True),
        ("alice", "bob",   False, False),
        ("alice", "bob",   True,  True),     # alice has the role
        ("admin", "bob",   False, False),    # admin role alone is not enough
        ("admin", "bob",   True,  True),     # admin + health-admin
        ("system", "bob",  False, True),
    ],
)
def test_can_read_metrics(...): ...
```

The test imports `can_read_metrics` directly from
`interfaces/health.py` — pure function, no service mocking. A
companion test asserts the `health-admin` role seeded in
`acl_roles` at level 0, and asserts no user is granted the role by
default — including the built-in `admin`.

### 16.4 Multi-user isolation test

A regression test: two `ingest_webhook` calls overlap (one
`asyncio.gather` of two webhooks for two different users). Assertion:
no metric ends up under the wrong `user_id`. This is the canonical
"singleton with concurrent users" bug from
`memory-multi-user-isolation.md` and we want a guardrail before any
future refactor reintroduces a `_current_user_id` on `self`.

### 16.5 Privacy regression tests

- A user's tools never return another user's metrics — tested by
  invoking `latest_health` with `_user_id="alice"` while data exists
  for both alice and bob, asserting the result is alice-only.
- The webhook route returns `404` (not `403` and not `503`) for
  unknown tokens AND for disabled-but-real tokens — tested to
  prevent enumeration. Body and headers are byte-identical between
  the two cases.
- The audit-log entry exists for cross-user reads — assert one
  `health_audit` row per cross-user read with the right
  `target_user_id`, `metric_types`, `period_*`.
- DEBUG log values appear iff `debug_log_values=true` — caplog
  inspection.
- **`acl_collections` rows are seeded** at `start()` for
  `health_metrics`, `health_links`, `health_daily_summaries`,
  `health_audit`, `health_oauth_state` — assert each row exists
  with the expected `read_role` / `write_role`. Without this seed,
  the entities page silently exposes everything to any user.
- **Token comparison is constant-time / hash-at-rest:**
  - The `health_links.webhook_token_hash` column is a SHA-256 hex
    string, not the raw token (assert via row inspection).
  - The route uses `hmac.compare_digest` (asserted via mock /
    monkeypatch on the comparator at the call site).
- **OAuth state rejects expired / replayed / wrong-user states:**
  - State older than `expires_at` → callback returns error.
  - Already-`consumed_at` state → callback returns "already linked"
    benign response.
  - State whose `user_id` differs from the calling session's
    user_id → callback rejects (confused-deputy defense).
  - State whose `backend_name` differs from the route's
    `{backend}` → rejected.
- **Per-user write cap enforces:** ingesting more than
  `per_user_daily_write_cap` rows in 24h drops the over-cap rows
  with a single INFO log.
- **Cascade on `auth.user.deleted`:** a fake-bus publish of
  `auth.user.deleted` for `user_id="bob"` deletes every
  `health_metrics`, `health_daily_summaries`, `health_links` row
  belonging to bob, and emits one `health.metric.deleted` event
  with `scope="user-deleted"`. Alice's rows remain.
- **Notification on cross-user read:** a real
  `NotificationProvider` test fake records that `notify_user` was
  called with `user_id=target`, source=`"health"`, the right
  metric-type / period values. With NotificationProvider absent,
  the audit row is still written and a WARN is logged.
- **Log redaction:** `code`, `state`, `Authorization`,
  `webhook_url`, `oauth_*` keys produce `[redacted]` in the log
  output. Asserted via caplog with each key name in turn.
- **`extra` whitelist:** a webhook delivery that includes
  `extra={"x-forwarded-for": "1.2.3.4"}` is accepted but the
  forbidden key is dropped (it's not in the
  apple-health/hk-webhook whitelist); the persisted row's
  `extra` is empty.
- **Replay-flood absorbs without amplification:** posting the
  same `source_event_id` twice produces one `health.metric.received`
  event, not two.

### 16.6 Concurrency + isolation tests

- The §16.4 multi-user isolation test extends to scheduler loops:
  start the daily-summary tick with two users, induce a
  `set_current_user` inside one user's task, and assert the other
  user's task sees its own context (not the first user's).
- `_run_per_user` honors the `concurrency` cap: with `concurrency=2`
  and 8 users, never more than 2 tasks run simultaneously (verified
  via a counting semaphore in the test work function).

### 16.7 Prompt-output regression tests for the non-clinical guarantee

Per the product review's A3, the non-clinical claim in §11.1 +
§6.1 needs more than aspirational text in the prompt:

- A test runs the daily-summary code path against ~10 recorded
  scenarios (low sleep, high HR, dropped weight, perfect day,
  missing-everything day, very-low sleep, very-high sleep, weight
  spike, all-zeros, sparse-data) using a deterministic stub
  `AISamplingProvider`. The test asserts the prompt sent to the AI
  contains the latest constraint-laden text (no drift since last
  edit) and the canned response stored as `summary_text` does not
  match a forbidden-word regex.
- A separate **live-AI** smoke test marked `@pytest.mark.live`
  (skipped in CI) runs the actual prompt against the configured
  backend across the same scenarios and runs the same forbidden-
  word regex. Catches model-side regressions when an upstream
  provider changes behavior.
- A test asserts `summary_text` is never parsed as instructions
  anywhere in the codebase: a fixture sets `summary_text =
  '{"action": "delete_all"}'` and verifies no tool dispatcher
  consumes it.

## 17. SPA (v1)

### 17.1 Per-user account page (`account.extensions` slot)

Each backend ships its own panel under `<plugin>/frontend/`:

- Apple Health → "Apple Health" panel with webhook URL + Shortcut
  instructions.
- Withings → "Withings" panel with Connect button.
- HKWebhook → "Generic Webhook" panel with URL + curl example.

All three panels are routed through the existing
`PluginPanelSlot slot="account.extensions"` mechanism — core never
imports plugin code (`memory-plugin-ui-extensions`).

### 17.2 Health page (core)

A new `/health` route under core (since the metrics view is
backend-agnostic):

- Header with "Today's summary" pulled from
  `GET /api/health/me/summary`.
- A simple list of metrics on the left, the latest reading + last
  7 days table on the right.
- "Connected sources" sub-section listing the user's
  `health_links` (delegating render to plugin panels). Each row
  shows `last_delivery_at` (push) or `last_sync_at` (pull) +
  `last_sync_error` so a silently-broken backend is visible.
- Right-to-delete entry point at the bottom, opening the **two-
  step confirmation dialog** (preview-counts → `confirm: "DELETE"`
  literal, see §6.6). The dialog also surfaces the
  Withings-still-retains-the-data disclosure.

The page does **not** render any chart — that's v2. Native HTML
table + small sparklines (a 6-line inline SVG implementation,
pinned in the spec rather than left to the implementer) are
acceptable; no charting library dependency.

### 17.3 Discoverability — empty state

Per the product review's U3: a user with no `health_links` rows
has no in-product hint that this feature exists. Two cheap nudges:

- **Account page empty-state callout** above the
  `account.extensions` slot for users with no `health_links` rows:
  "Connect a health source — Apple Health, Withings, or generic
  webhook." Same pattern as the notification-fanout empty-state.
- **Conditional nav item:** the SPA main nav shows the `/health`
  entry only for users with at least one `health_links` row
  (regardless of `enabled`). Users without health connected don't
  need the menu item; users with it want quick access.

### 17.4 Per-user audit-log page

A new `/account/health/audit-log` route under core for **the
calling user**. Renders every `health_audit` row where
`target_user_id == current_user_id`, most recent first. Columns:

- **When:** `accessed_at` (UTC + local TZ in tooltip).
- **Kind:** `cross_user_read` / `self_delete_all` / etc.
- **Actor:** `actor_user_id` (resolved to display name, falling
  back to id; "system" for cascade jobs).
- **What:** `metric_types` summarized to "sleep, weight, …".
- **Window:** `period_start` → `period_end` if set.

The page closes the loop opened by the §6.1.1 notification — the
target sees a "view your audit log" link in the notification, lands
here, can see exactly who accessed what and when. Required closure
on the privacy-loop story per `CLAUDE.md` ("the user is trusted to
be told what's happening").

### 17.5 Admin overview

An admin-only "Health" tab on `/system` shows the §8.3 user-counts
view. No values, no per-day breakdowns, no metric-type drill-down.
Holders of `health-admin` (separate from `admin`) get an additional
"Drill in" button per row that opens
`/api/health/admin/users/{user_id}/metrics` — and triggers the
audit log + target-user notification per §6.1.

## 18. Wiring in `app.py`

`HealthService` is registered alongside the other core services in
`Gilbert.__init__` after `ScheduleService`. The plugins
(`apple-health`, `withings`, `hk-webhook`) are loaded by the plugin
loader in the normal way — their `setup()` performs the side-effect
imports that register their `HealthBackend` subclasses.

`app.py` does NOT import any concrete backend class — it only
imports `HealthService` from `core/services/health.py`. The service
discovers backends via `HealthBackend.registered_backends()` after
plugin setup completes (the boot sequence already calls
`start_all()` only after every plugin has run `setup()`).

## 19. Open questions

These are explicitly **future-work** items. v1 ships without them
because the cost / scope is not justified for v1; each one is
flagged so a future agent can pick it up deliberately.

1. **OAuth-token encryption at rest.** v1 stores Withings access /
   refresh tokens in plaintext SQLite (webhook tokens are already
   hash-at-rest, see §6.4 framing). Future work: Fernet (or
   libsodium secretbox) symmetric encryption with the key sealed to
   the OS keychain — same approach we plan for inbox OAuth tokens.
   This is a precondition for any deployment beyond a single
   trusted host (the v1 startup WARN gates this).
2. **Streaming ingestion at scale.** A single Apple Health Shortcut
   delivery is currently capped at `webhook_max_metrics_per_delivery`
   (default 1000); deliveries with more metrics return 400. If
   real-world usage exceeds this, future work is a bulk-insert
   path (write all dedup-checked rows in one storage call).
3. **Per-user retention policy.** Global `retention_days` covers
   the v1 case. Some users will want a personal retention
   ("delete my pre-2024 weight" on a per-user basis); future work
   adds a per-user retention field on the user profile.
4. **Time-zone tracking for travelers.** v1 reads the user's local
   TZ from the existing user profile. The daily-summary boundary
   handles DST correctly (§10.1), but a user who moves time zones
   *during* a day still anchors on profile TZ. Future work could
   anchor on device-local time pulled from each metric's
   `recorded_at` offset.
5. **Withings webhooks.** Withings supports outgoing webhooks that
   would let us avoid polling. v1 uses the 6-hour pull path; future
   work registers a Withings webhook against the existing
   `/webhook/health/{token}` route once we've validated the
   security model end-to-end.
6. **Garmin / Oura / Fitbit.** All three have OAuth APIs but
   Garmin's developer program requires application approval and
   Oura's rate-limits are tighter. Deferred; all fit the existing
   interface without changes.
7. **Prompt safety enforcement.** The default summary prompt
   forbids medical language, but a user with edit access could put
   anything in. Considered: a server-side post-filter that flags
   forbidden words and re-prompts. Decided against in v1 (the user
   owns their prompt for their own data); revisit if the system is
   ever shared across users with elevated medical-context risk.
8. **Mobile push for the daily summary.** Out of scope until
   Gilbert has a mobile notification path generally.
9. **Body-HMAC on Apple Health webhook deliveries.** v1's webhook
   security is "the URL is the secret." A second per-user secret
   (carried as an `X-Hk-Signature` header HMAC over the body) would
   defeat captured-token replay over plain HTTP. Threading it
   through the iOS Shortcut is fragile; deferred. HTTPS-everywhere
   is the v1 mitigation.
10. **Step-up auth for cross-user reads.** v1 makes the
    `health-admin` role a one-time grant. PHI-style flows in
    regulated industries usually require fresh re-authentication
    on each access; future work adds a sudo-style check.
11. **Greeting model with automation tools.** The §1 motivating
    "I dimmed the meeting reminders" line requires the greeting
    model to grow tool dispatch. A separate feature.
12. **Proposals current-schedule context.** The §1 "21:30 instead
    of 22:30" specificity needs the reflector to ingest current
    automation state. A separate feature against the proposals
    service.

## 20. Acceptance criteria

A reviewing agent should be able to verify each of the following
without re-reading this spec:

- **Layer rules:** `interfaces/health.py` imports nothing from
  `core/`, `integrations/`, `storage/`, or `web/`. `HealthService`
  imports nothing from `integrations/` (no concrete backend
  imports). Web routes call into the service with no business
  logic of their own.
- **Backend pattern:** `HealthBackend` follows the
  `__init_subclass__` registry pattern. Side-effect imports happen
  in each plugin's `setup()`. `HealthService` discovers backends
  via `registered_backends()`.
- **Multi-user isolation:** No `self._current_*` / `self._active_*`
  / `self._pending_*` attributes on `HealthService`. All per-user
  state is in storage or in keyed dicts (`_webhook_buckets`,
  `_webhook_ip_buckets`, `_ingest_locks`, `_per_user_write_caps`).
  Backend methods take `user_id` as a parameter, never read it
  from `self` or a global. Scheduler loops use `copy_context()`
  + `set_current_user` per-task; no invented helpers
  (`use_user_context`, `UserContext.system_for_user`) are required
  or referenced.
- **AI prompts configurable:** Both `_DEFAULT_SUMMARY_PROMPT` and
  `_DEFAULT_TREND_PROMPT` are wired to `ConfigParam(multiline=True,
  ai_prompt=True)`. The call sites read `self._summary_prompt` /
  `self._trend_prompt`, not the constants. The `on_config_changed`
  fallback uses `(value or _DEFAULT_*)` so empty overrides
  re-resolve to the bundled default.
- **Privacy / RBAC:** Owner-only reads enforced in the service (not
  in routes). Cross-user reads require membership in the dedicated
  `health-admin` role (NOT a freestanding permission, NOT auto-
  granted to `admin`) AND persist a `health_audit` row AND notify
  the target user. No tool accepts a `user_id` argument from the
  model. `acl_collections` rows seeded at `start()` for
  `health_metrics`, `health_links`, `health_daily_summaries`,
  `health_audit`, `health_oauth_state` — the entities page never
  silently exposes private data.
- **Webhook security:** Webhook tokens are SHA-256 hash-at-rest;
  the route uses `hmac.compare_digest` for confirmation. Per-token
  AND per-IP rate limits. Body and metric-count caps. Replay-flood
  doesn't amplify into the event bus (duplicates skip event
  publish). `not_found` and disabled-token cases collapse to the
  same response shape.
- **OAuth security:** State is one-shot, expiry-bound, server-side
  bound to the initiating user, backend-namespaced. Withings
  `disconnect` revokes upstream; right-to-delete revokes upstream.
- **Deletion:** An `auth.user.deleted` event (added in this PR to
  `UserService.delete_user`) triggers cascade deletion of every
  `health_metrics`, `health_daily_summaries`, and `health_links`
  row for that user. Self-delete is a two-step preview-then-confirm
  flow; an audit row survives the cascade.
- **DST-correct daily-summary boundary:** The "yesterday" window
  is computed as `[local_yesterday_midnight, local_today_midnight)`
  in the user's TZ via `zoneinfo`, then converted to UTC for the
  storage query. Spring-forward is 23 hours, fall-back is 25,
  by construction.
- **Tests:** Auth matrix is parametrized; multi-user isolation
  regression test exists (and extends to scheduler loops);
  per-backend payload parser tests exist; `acl_collections` seed
  test; `auth.user.deleted` cascade test; constant-time-comparison
  test; OAuth-state confused-deputy test; non-clinical prompt
  regression test; replay-flood-no-amplify test.
- **No medical-advice prompt path:** The bundled prompts forbid
  diagnosis / cause / treatment language. The trend prompt allows
  direction / rate / consistency framing (without crossing into
  causes) — calculator-output is no longer the spec. Proposals
  integration consumes flags via the existing observation pipeline,
  not via an invented prompt-fragment mechanism.
- **Documentation freshness:** This PR creates
  `.claude/memory/memory-health-service.md`, adds it to
  `MEMORIES.md`, updates root `README.md` integration table,
  updates `std-plugins/README.md`, updates
  `memory-user-auth-system.md` for the new `auth.user.deleted`
  event.

## 21. Implementation order suggestion (for the next agent)

Suggested incremental ordering — each step is independently testable
and ships value:

0. **Precondition: `auth.user.deleted` event.** Add the publish to
   `UserService.delete_user` in `core/services/users.py`; add a
   sentence to `memory-user-auth-system.md`. This isn't health-
   specific — multiple future services need it — but it's a hard
   dependency for the cascade in §6.6, so land it first. Tests:
   delete_user publishes the event with the right payload; no
   publish if no `EventBusProvider` is present.
1. `interfaces/health.py` — dataclasses, enums (`MetricType`,
   `MetricUnit`, `AggregatePeriod`, `AggregatorKind`),
   `DEFAULT_AGGREGATOR`, `HealthBackend` ABC, `HealthProvider`,
   `GreetingBrief`, `parse_metric_payload`, auth helpers
   (`can_read_metrics`, `can_mutate_metrics`),
   `HEALTH_ADMIN_ROLE` constant, error taxonomy
   (`HealthBackendAuthError`, `…RateLimitError`, `…TransientError`,
   `…NotFoundError`). Unit tests for the auth matrix and the parser
   (caps, future-timestamp tolerance, unknown-type drop, extra
   whitelist).
2. `core/services/health.py` skeleton: `ingest_metrics`,
   `read_metrics`, `latest_metric`, `aggregate`, with no backend
   wiring. **Includes ACL seeding** (`acl_collections` for the five
   collections + `health-admin` role at level 0). Tests against a
   real SQLite test DB; tests assert the seeded ACL rows and the
   role.
3. `web/routes/health.py` — webhook route (with hash-at-rest token
   lookup, `hmac.compare_digest` confirmation, dual rate limits,
   body / metric caps, `not_found`/`disabled` collapse), per-user
   account routes, generic OAuth callback route. Tests use a stub
   backend.
4. `std-plugins/hk-webhook/` — simplest backend, exercises the
   end-to-end push path. Tests: payload happy path, bad shapes
   rejected, `extra` whitelist drops unknown keys.
5. AI tools — `health_now` (the most-invoked one), `latest_health`,
   `health_summary`, `health_trend`, convenience tools, `health_links`.
   `health_delete_my_data` returns the two-step UI block. **No
   slash_command on `health_delete_my_data`.** All other slash-
   enabled tools have explicit `slash_help`.
6. Daily-summary scheduler job (hourly TZ-aware tick + bounded
   concurrency) + `ai_prompt`-configurable prompts. Tests: DST
   boundary, missing-data prompt path, prompt-cache wired to
   `self._summary_prompt` (not the `_DEFAULT_*` constant), prompt-
   output non-clinical regression.
7. `std-plugins/apple-health/` — HealthKit identifier mapping,
   Shortcut docs panel, prebuilt Shortcut artifact + hash display,
   last-delivery indicator, failure-mode disclosure copy.
8. `std-plugins/withings/` — OAuth flow with `health_oauth_state`
   collection, server-side state binding, generic callback route,
   pull-sync at 6h cadence, error taxonomy with `last_sync_error`
   surfacing, `disconnect` overrides default to revoke upstream.
   Manual QA checklist (§12.2) executed against a Withings sandbox
   account before merge.
9. Greeting integration (`HealthProvider.health_brief_for_greeting`
   structured payload) + proposals integration via the existing
   observation pipeline (`health.daily.summary` → observation row).
10. Two-step right-to-delete wizard (preview + confirm) +
    `health_audit` row on self-delete + Withings retention
    disclosure copy + privacy regression tests + `auth.user.deleted`
    cascade test + audit-log SPA page.
11. **Documentation:** create `.claude/memory/memory-health-service.md`,
    add to `MEMORIES.md`, update root `README.md`, update
    `std-plugins/README.md`, update `memory-user-auth-system.md`
    for the new event, update `interfaces/acl.py` with the
    `health.` prefix at level 100.

Each step should keep the architecture-violation checklist clean
(layer imports, capability protocols, AI prompts configurable,
multi-user isolation). Failing any check is a regression to fix in
the same step, not the next one.

## 22. Revision Log — Round 2

This revision (2026-05-09) incorporates feedback from three
independent reviewers (architect, product, engineering). Changes:

### Architecture (architect review)

- **Replaced `health.cross_user_read` permission with `health-admin`
  role.** The previous draft invented a "permission" abstraction that
  doesn't exist in Gilbert's RBAC; the seeded role at level 0 is the
  native primitive. (§6.1, §6.6, §7.3, §7.5, §8.3, §17.5)
- **Specified `auth.user.deleted` event** as a precondition step in
  `UserService.delete_user` rather than assuming it exists. (§6.6.1,
  §21 step 0)
- **Removed invented helpers `use_user_context` /
  `UserContext.system_for_user(user_id)`.** Scheduler loops use
  existing `set_current_user` + `copy_context()` + a new local
  helper `_run_per_user` with bounded concurrency. The actor
  remains `UserContext.SYSTEM` with `metadata["target_user_id"]`
  carrying the per-task target so audit trails distinguish "system
  did X for user Y" from "user Y did X." (§10)
- **Dropped `UsersProvider` dependency.** The service iterates
  `health_links` directly. (§7.1, §7.4)
- **Seeded `acl_collections`** for `health_metrics`, `health_links`,
  `health_daily_summaries`, `health_audit`, `health_oauth_state`
  at `start()`. Without this, the entities page silently exposes
  private rows. (§7.5)
- **Specified `health_audit` collection** with full schema, indexes,
  and retention rules — replaces references to a generic `audit_log`
  that doesn't exist. (§4.5, §6.1, §6.6, §17.4)
- **Added `health_links(webhook_token_hash)` UNIQUE index.** (§4.2,
  §7.5)
- **Generic OAuth callback route** at
  `/api/health/me/oauth/{backend}/callback`. (§8.4, §12.2)
- **Hourly TZ-aware daily-summary tick** instead of a single fire-
  time-specific job that only worked for UTC users. (§10.1)
- **`slash_help` strings** on every slash-enabled tool. (§9)
- **`extra` field is whitelist-only**; per-backend allowed keys
  pinned in §4.5. (§4.1, §4.5)
- **No knowledge-store / RAG indexing in v1.** Explicit out-of-scope.
  (§2)

### Engineering (SWE review)

- **Hash-at-rest for webhook tokens** (`SHA-256(token)` in
  `webhook_token_hash`, indexed UNIQUE; raw token shown once and
  not persisted). Constant-time confirmation via
  `hmac.compare_digest`. (§4.2, §6.3)
- **Encryption-at-rest framing for OAuth tokens.** v1 plaintext
  with operational gating (startup WARN if exposed beyond
  `127.0.0.1` without TLS); v2 keychain-sealed Fernet specced as
  a precondition for non-trusted-host deployments. (§6.4)
- **Per-IP rate-limit on the 404 path** prevents token-probing
  DoS. (§6.3, §11)
- **Replay defenses** specified precisely: dedup-no-event for
  flood resistance, HTTPS-only for captured-token replay,
  body/metrics size caps, recorded_at clock-skew window. (§6.3,
  §7.6)
- **Payload validation** — `Content-Length` cap,
  `webhook_max_metrics_per_delivery` cap, per-metric numeric /
  enum / clock-skew validation. (§6.3, §11)
- **OAuth `state` hardening** — dedicated `health_oauth_state`
  collection, server-side user-binding, 10-min expiry, one-shot
  consume, backend-namespaced; defeats confused-deputy and
  double-callback. (§4.2.1, §8.4, §12.2)
- **DST-correct daily-summary boundary** computed via `zoneinfo`
  from local midnights to UTC; spring-forward 23h / fall-back 25h
  by construction. (§10.1)
- **Concurrency caps** for daily-summary (8) and pull-sync (4) via
  semaphore + `copy_context()` per task. (§10, §11)
- **Per-(user, backend) ingest lock** (`_ingest_locks` keyed dict)
  makes the dedup-then-write path atomic against concurrent
  webhook deliveries. (§7.4, §7.6)
- **Per-user write cap** (default 100k/day) defends against a
  buggy device flooding metrics regardless of token bucket. (§7.4,
  §7.6, §11)
- **Backend error taxonomy** (`HealthBackendAuthError`,
  `RateLimitError`, `TransientError`, `NotFoundError`); pull-sync
  honors `retry_after`; persistent auth errors disable the link.
  (§10.2)
- **Withings disconnect revokes upstream.** Right-to-delete
  revokes upstream too. (§6.6, §12.2)
- **`not_found` and `disabled` webhook responses collapsed** to
  the same wire shape to defeat enumeration. (§6.3, §7.7)
- **Dedup fallback key includes `backend`**:
  `(user_id, backend, metric_type, recorded_at)` — defends against
  two distinct-source readings at the same second. (§7.5)
- **Log-redaction allowlist** extended for `code`, `state`,
  `Authorization`, `webhook_url`, `oauth_*`. (§6.4, §6.7)
- **Summary text is display-only**, never instructional;
  `flags` computed in code, never parsed from `summary_text`.
  (§4.4, §11.4)

### Product (product review)

- **§1 motivating example reframed** to match what's actually
  delivered — the *informed* part (greeting prompt sees structured
  health values) is v1; the *causal-action* part (greeting model
  invokes automation tools) is v2. (§1, §14)
- **Trend prompt rewritten** to allow direction / rate / consistency
  framing without crossing into causes — the previous version
  produced calculator output. (§11.1)
- **`health_now` tool added** as the catch-all "how am I doing
  right now?" — the most-invoked tool by user intent, mapped to
  `/health now`. (§9)
- **`health_summary` default period clarified** as yesterday;
  users wanting "today so far" use `health_now`. (§9)
- **`slash_command` dropped from `health_delete_my_data`** so a
  stray autocomplete can't put it next to `/health summary`. (§9)
- **Two-step delete wizard** with preview-counts + literal
  `"DELETE"` confirmation + Withings-still-retains-the-data
  disclosure + a surviving `health_self_deleted` audit row. (§6.6,
  §17.2)
- **Per-user audit log page** at `/account/health/audit-log`
  closes the loop opened by the cross-user-read notification.
  (§17.4)
- **iOS Shortcut realities surfaced**: prebuilt Shortcut as the
  headline path with SHA-256 hash for supply-chain verification,
  failure-mode disclosure (Background App Refresh, lock state),
  `last_delivery_at` indicator on the link row. (§4.2, §12.1)
- **Withings admin prerequisite** (`gilbert.public_base_url`
  must be set before users can connect, callback URL surfaced in
  the panel). (§12.2)
- **Token-rotation notification** — rotation triggers an urgent
  notification telling the user to update their Shortcut URL.
  (§6.3)
- **Empty-state callout** on the Account page +
  conditional `/health` nav item for discoverability. (§17.3)
- **Cross-user-read notification copy pinned** with action link
  to the user's audit log. (§6.1.1)
- **Notification durability via NotificationService** specified;
  fallback path documented if `NotificationProvider` is absent.
  (§6.1.2)
- **Pull-sync default bumped to 6h** — Withings data doesn't
  change hourly. (§10.2, §11)
- **`ai_profile` default explicit `"standard"`** (was `""`); also
  documented why `light` is wrong for these prompts. (§9, §11)
- **Flag thresholds exposed as config** so users with naturally
  short sleep aren't tagged forever. (§11, §15)
- **Proposals integration via observation pipeline** instead of an
  invented `SystemPromptContributor` extension point that
  proposals doesn't currently use. (§15)
- **Summary prompt strengthened** with "speak only about what the
  data IS" + "comfortable with silence" rules to prevent paraphrase
  drift around forbidden words. (§11.1)
- **Non-clinical prompt regression test** — both deterministic and
  live-AI variants. (§16.7)
- **`webhook_url` is computed at read time** (not stored), with
  caveat about token visibility on rotation. (§4.2)
- **Backend-specific `extra` whitelist contract** documented per
  backend. (§4.5)
