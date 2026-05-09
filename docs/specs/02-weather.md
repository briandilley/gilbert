# Feature 02 — Weather Service + Open-Meteo Backend

## Summary

Add a first-class **WeatherService** to Gilbert that exposes current conditions, hourly/daily forecasts, and severe-weather alerts as both AI tools and a `WeatherProvider` capability protocol. The default (and only-in-this-PR) backend is **Open-Meteo** — a free, no-API-key, well-documented HTTP service that covers current + forecast globally. Two further backends (`nws-weather` for NOAA in the US, `openweather` for OpenWeatherMap) are anticipated and the interface is shaped to slot them in cleanly, but they are explicitly out of scope here.

The service lands as:

- A new `WeatherBackend` ABC + dataclasses in `src/gilbert/interfaces/weather.py`.
- A new `WeatherService` (Service + ToolProvider + Configurable + ConfigActionProvider) in `src/gilbert/core/services/weather.py`, registered in `app.py` next to `WebSearchService`.
- A new `open-meteo` plugin under `std-plugins/open-meteo/` that registers the `OpenMeteoWeather` backend via the standard side-effect-import pattern.
- A `WeatherProvider` capability protocol in `src/gilbert/interfaces/weather.py` so other services (greeting, scheduler, proposals) can call into weather without touching the concrete service class.
- Four AI tools: `current_weather`, `forecast`, `weather_alerts`, `geocode_location` (read-only place-name lookup so the AI can answer "weather in Pittsburgh" without setup), plus two slash-only commands (`/weather set_home`, `/weather set_units`) that are NOT exposed as AI tools.

This pattern is the closest possible analog of the existing **WebSearchService + Tavily** wiring, with two important differences: weather has multiple shaped result types (current vs forecast vs alerts) instead of a single `WebSearchResult`, and the backend needs no API key, so the Open-Meteo plugin's `backend_config_params()` is mostly knobs (units, cache TTL, request timeout) rather than credentials.

## Motivation

Three downstream features want weather context, today:

1. **Greeting service** — "Good morning Brian, it's 6°C and pouring; bring a jacket" beats "Good morning Brian!" The greeting service should be able to *enrich* its prompt with weather without owning the integration.
2. **Scheduler / automations** — A user wants a daily timer "water the garden every evening except when ≥5 mm of rain has fallen in the last 24 h or is forecast in the next 6 h." Today the timer can fire, but it has no weather context to gate on.
3. **Proposals service** — When NWS issues a high-wind or severe-thunderstorm alert, Gilbert should be able to surface a proposal like "close the bedroom shades; storm warning issued, gusts up to 70 mph in the next 90 min."

Plus the obvious "Hey Gilbert, what's the weather?" chat interaction.

Weather is also a perfect testbed for the multi-backend pattern. Open-Meteo is great everywhere but provides no warnings; NWS provides excellent warnings but only in the US; OpenWeatherMap covers the whole globe but requires a key. The interface needs to gracefully express "this backend supports current+forecast but not alerts" so the consumer can fall through to a different source if needed — and so the AI tool can return a clean "Open-Meteo doesn't issue warnings; install the NWS plugin for US alerts" rather than a 500.

## Scope

### In scope

- `WeatherBackend` ABC with `current()`, `forecast_hourly()`, `forecast_daily()`, and `alerts()` methods + `capabilities()` discriminator.
- `CurrentWeather`, `HourlyForecast`, `DailyForecast`, `WeatherAlert`, `WeatherCondition`, `WeatherUnits` dataclasses in `interfaces/weather.py`.
- `WeatherProvider` capability protocol for in-process consumers (greeting, scheduler tools, proposals).
- `WeatherService` aggregator: holds one backend, owns the cache, owns AI prompts, exposes AI tools + slash commands.
- TTL cache (in-memory) with separate TTLs for current / hourly / daily / alerts.
- Per-user location resolution chain (per-user > presence-derived > service default), plus a `home_location` admin default.
- Per-user units preference (metric/imperial) with service-wide default.
- `OpenMeteoWeather` backend in `std-plugins/open-meteo/` covering current + hourly + daily; `alerts()` returns `[]` (Open-Meteo doesn't issue warnings).
- Geocoding helper backed by Open-Meteo's free Geocoding API (`geocoding-api.open-meteo.com/v1/search`), used by the location-config flow AND exposed as a `geocode_location` AI tool.
- **Four read-only AI tools** (`current_weather`, `forecast`, `weather_alerts`, `geocode_location`) all `parallel_safe=True` and all available at `required_role="user"`.
- **Two write slash commands** (`/weather set_home`, `/weather set_units`) — registered as `ToolDefinition` entries with a `slash_command` but excluded from the AI tool surface (see Tool Surface for how). Slash users can still type them; the AI cannot pick them up and start "helpfully" mutating user settings.
- A `home_location.set` Settings ConfigAction (two-phase ConfigAction flow) for non-CLI users.
- A persistent severe-alert delivery path: when an alert-capable backend (NWS, OpenWeather) is installed and produces alerts, `WeatherService` itself subscribes to its own `weather.alert.issued` event and calls `NotificationProvider.notify_user(...)` for every user with a configured location matching the alert's area. Severity → urgency mapping documented below; voice announcement on `EXTREME` is opt-in.
- An optional **daily weather digest** event (`weather.digest`) published by a scheduler-driven job at a configurable hour, so other services can subscribe instead of polling.
- A `weather.alert.issued` event published when a polling alert sweep sees a new alert (only fires once `alerts()` returns non-empty — i.e. when an alert-producing backend is installed). Dedup state is **persisted** in entity storage so restarts don't re-spam subscribers.

### Out of scope (this PR)

- **NWSWeather plugin** (`std-plugins/nws/`). Anticipated as the canonical alerts source for US users. The interface's `WeatherAlert` dataclass and `WeatherBackendCapabilities.alerts` flag are explicitly designed so that this plugin slots in by adding `backend_name = "nws"` and implementing `alerts()`. **Out of scope for this PR.**
- **OpenWeatherMap plugin** (`std-plugins/openweather/`). Same reasoning — it would set `backend_name = "openweather"`, declare `api_key` as a `backend_config_params()` entry with `sensitive=True`, and implement `current()` / `forecast_hourly()`. **Out of scope for this PR.**
- **Multi-backend aggregation** (e.g. "use Open-Meteo for forecast and NWS for alerts simultaneously"). Single-backend-only for now; the multi-backend aggregator pattern documented in `memory-multi-backend-pattern.md` is *not* applied here yet because we'd need at least two installed backends to motivate the design. The `WeatherService` keeps the door open by routing each method through a single resolved backend; layering can be added in a follow-up without a breaking change.
- **Frontend Settings panel** beyond what auto-renders from `config_params()`. The standard ConfigSection card is enough; no custom React component in this PR.
- **Charting / SPA dashboard card.** Anticipated, but not blocking.
- **Pollen, air quality, radar imagery, sea state.** Open-Meteo offers separate APIs for these — out of scope; can be added as additional methods on the backend later behind capability flags.
- **Sun / moon / civil twilight.** Could be exposed via Open-Meteo's daily endpoint (`sunrise`, `sunset`) — leave on the table; not wired up in tools this PR.

## Architecture

### Layer placement

```
interfaces/
  weather.py              ← WeatherBackend ABC, dataclasses, WeatherProvider Protocol
core/
  services/
    weather.py            ← WeatherService (Service + ToolProvider + Configurable)
integrations/
  (nothing — no vendor-free weather backend exists)
std-plugins/
  open-meteo/             ← Plugin + OpenMeteoWeather backend
```

This mirrors `interfaces/websearch.py` + `core/services/websearch.py` + `std-plugins/tavily/` exactly. Per the architecture rules:

- `interfaces/weather.py` imports nothing outside `interfaces/` (stdlib + `gilbert.interfaces.configuration`).
- `core/services/weather.py` imports from `interfaces/` only — never from the Open-Meteo plugin.
- The plugin imports `gilbert.interfaces.weather`, never anything from `core/services/`.
- `app.py` is the only place that imports `WeatherService` directly; it does so to register it.

### Data shapes (full type signatures)

Defined in `src/gilbert/interfaces/weather.py`. Imports stay strictly within `gilbert.interfaces.*` per the layer rules; backends in std-plugins import the same names from these modules and **never** from `gilbert.core.*`:

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.tools import ToolParameterType  # used by backends declaring backend_config_params()


class LocationNotConfiguredError(RuntimeError):
    """Raised by ``WeatherProvider`` methods when no location can be resolved.

    The AI tool layer catches this and renders an ``error`` JSON payload
    with a clear message; callers from other services may catch it to
    branch (e.g. greeting service falls back to no-weather greeting).
    """


class WeatherUnavailableError(RuntimeError):
    """Raised when the backend HTTP call fails / times out.

    Carries the underlying provider status code (when known) so the AI
    tool layer can surface ``retryable=True`` for 5xx and rate-limit
    responses.
    """

    def __init__(self, message: str, *, provider_status: int | None = None,
                 retryable: bool = True) -> None:
        super().__init__(message)
        self.provider_status = provider_status
        self.retryable = retryable


class WeatherUnits(StrEnum):
    """Caller-facing unit system. Backends may translate internally."""

    METRIC = "metric"      # °C, m/s, mm, hPa, km
    IMPERIAL = "imperial"  # °F, mph, in, hPa, mi


class WeatherCondition(StrEnum):
    """Coarse provider-neutral condition tag. Backends map their own
    codes onto this enum so consumers (greeting prompts, scheduler
    rules, proposals) don't have to know each backend's lookup table.
    """

    CLEAR = "clear"
    PARTLY_CLOUDY = "partly_cloudy"
    CLOUDY = "cloudy"
    FOG = "fog"
    MIST = "mist"
    DRIZZLE = "drizzle"
    FREEZING_DRIZZLE = "freezing_drizzle"  # WMO 56/57 — operationally distinct from sleet for road-safety gating
    RAIN = "rain"
    HEAVY_RAIN = "heavy_rain"
    FREEZING_RAIN = "freezing_rain"        # WMO 66/67 — distinct hazard
    SNOW = "snow"
    HEAVY_SNOW = "heavy_snow"
    SLEET = "sleet"
    HAIL = "hail"
    THUNDERSTORM = "thunderstorm"
    THUNDERSTORM_HAIL = "thunderstorm_hail"  # WMO 96/99 — preserves hail signal
    SMOKE = "smoke"                          # wildfire / public-health relevant
    HAZE = "haze"
    DUST = "dust"
    UNKNOWN = "unknown"


class AlertSeverity(StrEnum):
    """Follows the Common Alerting Protocol (CAP) §3.2.1.7 vocabulary.

    NWS and EU MeteoAlarm both use CAP natively. OpenWeatherMap (when
    implemented) will need a small translation table mapping its
    integer severity to these names — that table belongs in the
    ``openweather`` plugin, not here.
    """

    MINOR = "minor"
    MODERATE = "moderate"
    SEVERE = "severe"
    EXTREME = "extreme"


@dataclass(frozen=True)
class GeoLocation:
    """A resolved location. Either looked up via geocoding or hand-entered."""

    latitude: float
    longitude: float
    name: str = ""               # human-readable: "Cleveland, OH, USA"
    timezone: str = "UTC"        # IANA tz, e.g. "America/New_York"
    country_code: str = ""       # ISO-3166-1 alpha-2 ("US")


@dataclass(frozen=True)
class CurrentWeather:
    """Current observed conditions at a location."""

    location: GeoLocation
    observed_at: datetime
    temperature: float
    feels_like: float | None
    humidity_pct: float | None
    wind_speed: float
    wind_gust: float | None
    wind_direction_deg: float | None
    pressure_hpa: float | None
    precipitation_last_hour: float | None  # mm or in (matches `units`)
    cloud_cover_pct: float | None
    condition: WeatherCondition
    raw_code: str = ""              # provider's native code, opaque
    description: str = ""           # provider-supplied phrase. Empty for backends that don't return one (Open-Meteo only returns numeric codes); for NWS this is `properties.shortForecast`. Consumers must treat empty as "no phrase available" and fall back to `condition`.
    units: WeatherUnits = WeatherUnits.METRIC


@dataclass(frozen=True)
class HourlyForecast:
    """A single hour-by-hour forecast slice."""

    location: GeoLocation
    valid_at: datetime
    temperature: float
    feels_like: float | None
    precipitation: float            # total in mm or in for that hour
    precipitation_probability_pct: float | None
    wind_speed: float
    wind_gust: float | None
    wind_direction_deg: float | None
    cloud_cover_pct: float | None
    condition: WeatherCondition
    units: WeatherUnits = WeatherUnits.METRIC


@dataclass(frozen=True)
class DailyForecast:
    """A daily summary (00:00–24:00 in the location's timezone)."""

    location: GeoLocation
    date: str                       # ISO date "YYYY-MM-DD"
    temperature_high: float
    temperature_low: float
    precipitation: float
    precipitation_probability_pct: float | None
    wind_speed_max: float
    wind_gust_max: float | None
    sunrise: datetime | None
    sunset: datetime | None
    condition: WeatherCondition
    units: WeatherUnits = WeatherUnits.METRIC


@dataclass(frozen=True)
class WeatherAlert:
    """A severe-weather alert / warning."""

    alert_id: str                   # provider-stable id, dedup key
    title: str                      # "Severe Thunderstorm Warning"
    description: str                # full text from the issuing authority
    severity: AlertSeverity
    issued_at: datetime
    expires_at: datetime | None
    affected_area: str = ""         # human-readable area description
    source: str = ""                # "NWS", "EU MeteoAlarm", etc.
    url: str = ""                   # canonical URL with full bulletin


@dataclass(frozen=True)
class WeatherBackendCapabilities:
    """Discriminator advertising which methods a backend implements meaningfully.

    The base `WeatherBackend` declares all four methods as abstract so type
    checks work, but a backend can override `capabilities()` to advertise
    that (e.g.) `alerts()` will always return `[]`. Consumers branch on
    these flags to decide whether to query the backend at all.
    """

    current: bool = True
    hourly: bool = True
    daily: bool = True
    alerts: bool = False
```

### `WeatherBackend` ABC

```python
class WeatherBackend(ABC):
    """Abstract weather data provider."""

    _registry: dict[str, type[WeatherBackend]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            WeatherBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type[WeatherBackend]]:
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        """Backend-specific config (API keys, base URL overrides, etc.)."""
        return []

    def capabilities(self) -> WeatherBackendCapabilities:
        """Advertise which methods this backend implements meaningfully."""
        return WeatherBackendCapabilities()

    @abstractmethod
    async def initialize(self, config: dict[str, Any]) -> None:
        """Initialize the backend with resolved configuration."""

    @abstractmethod
    async def close(self) -> None:
        """Release HTTP clients and any other resources."""

    @abstractmethod
    async def current(
        self,
        location: GeoLocation,
        *,
        units: WeatherUnits = WeatherUnits.METRIC,
    ) -> CurrentWeather:
        """Return current observed conditions."""

    @abstractmethod
    async def forecast_hourly(
        self,
        location: GeoLocation,
        *,
        hours: int = 24,
        units: WeatherUnits = WeatherUnits.METRIC,
    ) -> list[HourlyForecast]:
        """Return up to `hours` hour-by-hour forecast slices, ascending by `valid_at`."""

    @abstractmethod
    async def forecast_daily(
        self,
        location: GeoLocation,
        *,
        days: int = 7,
        units: WeatherUnits = WeatherUnits.METRIC,
    ) -> list[DailyForecast]:
        """Return up to `days` daily summaries, starting today, ascending by `date`."""

    async def alerts(
        self,
        location: GeoLocation,
    ) -> list[WeatherAlert]:
        """Return active severe-weather alerts for a location.

        Default implementation returns ``[]`` for backends that don't issue
        warnings (e.g. Open-Meteo). Backends that *do* must override and
        flip ``capabilities().alerts`` to True.
        """
        return []

    async def geocode(self, query: str, *, count: int = 5) -> list[GeoLocation]:
        """Resolve a place-name query to candidate locations.

        Default implementation raises ``NotImplementedError``. Backends
        with a free geocoding endpoint (Open-Meteo, OpenWeather) should
        override; backends without one (NWS) leave it raising and the
        service falls back to another backend's geocoder via the registry.
        """
        raise NotImplementedError(
            f"{self.backend_name} does not provide geocoding."
        )
```

### `WeatherProvider` capability protocol

```python
@runtime_checkable
class WeatherProvider(Protocol):
    """Capability protocol exposed by WeatherService.

    Other services must use this protocol via `isinstance(svc, WeatherProvider)`
    after `resolver.get_capability("weather")` — never an `isinstance`
    check against the concrete `WeatherService` class.

    All ``get_*`` methods raise ``LocationNotConfiguredError`` when
    ``location`` is None AND no user/service-default location can be
    resolved. They raise ``WeatherUnavailableError`` when the backend
    HTTP call fails. Both are catchable typed errors — never let
    raw ``httpx`` exceptions escape these methods.
    """

    async def get_current(
        self,
        location: GeoLocation | None = None,
        *,
        user: UserContext | None = None,
        units: WeatherUnits | None = None,
    ) -> CurrentWeather: ...

    async def get_forecast_hourly(
        self,
        location: GeoLocation | None = None,
        *,
        hours: int = 24,
        user: UserContext | None = None,
        units: WeatherUnits | None = None,
    ) -> list[HourlyForecast]: ...

    async def get_forecast_daily(
        self,
        location: GeoLocation | None = None,
        *,
        days: int = 7,
        user: UserContext | None = None,
        units: WeatherUnits | None = None,
    ) -> list[DailyForecast]: ...

    async def get_alerts(
        self,
        location: GeoLocation | None = None,
        *,
        user: UserContext | None = None,
    ) -> list[WeatherAlert]: ...

    def resolve_location(self, user: UserContext | None) -> GeoLocation | None: ...

    def resolve_units(self, user: UserContext | None) -> WeatherUnits: ...
```

**Identity contract — explicit over ContextVar.** This protocol takes the full `UserContext` (not a bare `user_id` string) so callers from background jobs (greeting tasks, scheduled actions) can pass identity in explicitly without relying on a ContextVar that may not be set. This is a deliberate departure from the ContextVar-default pattern documented in `memory-multi-user-isolation.md`: the consumers of `WeatherProvider` are often non-request-bound (scheduled digest, alert-poll-driven notifications). Inside `WeatherService`, a single helper `_resolve_user(user)` is the only place that may fall back to `gilbert.core.context.get_current_user()` when `user is None`.

When `location` is omitted, the service runs the location-resolution chain (see below). When `user` is omitted, the service falls back to `get_current_user()`; if that returns `UserContext.SYSTEM`, the service-default `home_location` is used.

This protocol is what `GreetingService`, scheduler-driven jobs, and `ProposalsService` will `isinstance`-check against. It must live in `interfaces/weather.py` alongside the dataclasses so consumers can import from one place.

### `WeatherService` (aggregator)

`src/gilbert/core/services/weather.py`. Mirrors `WebSearchService` structurally but adds caching and location/units resolution.

```python
class WeatherService(Service, ToolProvider):
    """Single-backend weather aggregator. ToolProvider for AI tools.

    Capabilities: ``weather``, ``ai_tools``. (No ``ws_handlers`` — the
    service exposes ConfigActions and slash commands, both of which
    reuse the standard ``config.action.*`` and ``chat.*`` RPCs. If a
    future iteration adds a dedicated WS RPC like
    ``weather.location.suggest`` for a typeahead UI, add the capability
    alongside the implementation; do not advertise it before then.)
    """

    @property
    def config_namespace(self) -> str:
        return "weather"

    @property
    def config_category(self) -> str:
        return "Intelligence"   # matches WebSearchService — narrative AI features

    def __init__(self) -> None:
        self._enabled: bool = False
        self._backend: WeatherBackend | None = None
        self._geocoder_backend: WeatherBackend | None = None  # see "Geocoding cross-backend fallback"
        self._backend_name: str = "open-meteo"
        self._settings: dict[str, Any] = {}
        self._resolver: ServiceResolver | None = None
        self._event_bus: EventBus | None = None
        self._notifications: NotificationProvider | None = None
        self._storage: NamespacedStorageBackend | None = None  # gilbert.weather namespace

        # Service defaults (overridable per-user)
        self._default_units: WeatherUnits = WeatherUnits.METRIC
        self._default_location: GeoLocation | None = None

        # Cache (see "Cache shape")
        self._cache: _WeatherCache = _WeatherCache(max_entries=2048)
        self._cache_ttl_current_s: int = 600           # 10 min
        self._cache_ttl_hourly_s: int = 1800           # 30 min
        self._cache_ttl_daily_s: int = 3600            # 1 h
        self._cache_ttl_alerts_s: int = 300            # 5 min

        # Optional daily digest job
        self._digest_enabled: bool = False
        self._digest_hour: int = 7
        self._digest_minute: int = 0

        # Alert poll job (no-op until backend.capabilities().alerts == True)
        # Keyed by (location_key, scope_id). scope_id="system" for the
        # service-default location; per-user fan-out (when added) uses
        # scope_id=user_id without breaking the shape.
        self._alert_poll_seconds: int = 300
        self._known_alert_ids: dict[tuple[str, str], set[str]] = {}
        self._alert_dedup_loaded: bool = False  # set on first poll after restart-load

        # Severity → notification urgency map
        self._alert_urgency: dict[AlertSeverity, str] = {
            AlertSeverity.MINOR: "info",
            AlertSeverity.MODERATE: "normal",
            AlertSeverity.SEVERE: "urgent",
            AlertSeverity.EXTREME: "urgent",
        }
        self._alert_voice_minimum: AlertSeverity = AlertSeverity.EXTREME
        self._alert_voice_enabled: bool = False
```

Lifecycle:

- `start(resolver)` — resolves capabilities (`configuration`, `event_bus`, `entity_storage`, optionally `scheduler`, `notifications`, and `speaker`), reads its config section, looks up the backend class via `WeatherBackend.registered_backends()`, calls `backend.initialize(self._settings)`. If `enabled=False`, returns early (no failure). If backend lookup fails, raises with a clear message ("install the open-meteo plugin" pointer). The storage handle is acquired as `provider.create_namespaced("gilbert.weather")` so user prefs / alert dedup state both live under that prefix instead of polluting the top-level collection space.
- After backend is initialized, registers two scheduler jobs (when `scheduler` capability is present):
  - `weather.digest` — daily at `digest_hour:digest_minute` (server local timezone — see Daily digest) if `digest_enabled` is True. Calls `_publish_digest()`. Job key is constant `weather.digest`; re-registration is a no-op so a hot config reload doesn't double-register.
  - `weather.alerts.poll` — every `alert_poll_seconds` if `backend.capabilities().alerts` is True. Polls the configured admin home location, dedupes by `alert_id` against persisted state (loaded once from `gilbert.weather.alert_dedup`), publishes `weather.alert.issued` for **genuinely new** alerts (and never on the first sweep after a restart — see "Alert poll event").
- `stop()` — cancels jobs, calls `backend.close()` on both `_backend` and (if non-None) `_geocoder_backend`, persists `_known_alert_ids` to `gilbert.weather.alert_dedup`, clears the cache.

### Cache shape

A small in-memory single-flight + LRU cache keyed by `(backend_name, method, location_key, units, params)`. Including `backend_name` in the key prevents cache pollution across backend swaps and prepares the structure for the future multi-backend pattern (one `WeatherService` holding distinct backends per method).

```python
@dataclass
class _CacheEntry:
    value: Any                     # CurrentWeather | list[HourlyForecast] | ...
    fetched_at: float              # monotonic seconds — used for stale_seconds reporting
    expires_at: float              # monotonic seconds

class _WeatherCache:
    """Single-flight + bounded LRU cache.

    Uses ``asyncio.Future`` per in-flight key — the standard "single-flight"
    idiom — instead of a long-lived per-key ``asyncio.Lock`` dict that
    would leak entries forever. The future is removed from
    ``_inflight`` once the loader resolves (success or failure). Lock
    leaks are not possible because there is no lock dict.

    Entries are LRU-bounded at ``max_entries`` (default 2048) so an
    unbounded keyspace (per-user locations + varying ``hours``/``days``
    + per-user units) cannot grow without limit.
    """

    def __init__(self, *, max_entries: int = 2048) -> None:
        self._entries: collections.OrderedDict[str, _CacheEntry] = collections.OrderedDict()
        self._inflight: dict[str, asyncio.Future[Any]] = {}
        self._max_entries = max_entries

    @staticmethod
    def _key(
        backend: str,
        method: str,
        loc: GeoLocation,
        units: WeatherUnits,
        **kw: Any,
    ) -> str:
        # Round lat/lon to 4 decimals (~11 m) so close-but-not-identical
        # callers share a cache slot. Open-Meteo grids at much coarser
        # resolution anyway.
        lat = round(loc.latitude, 4)
        lon = round(loc.longitude, 4)
        extra = ",".join(f"{k}={v}" for k, v in sorted(kw.items()))
        return f"{backend}:{method}:{lat},{lon}:{units}:{extra}"

    async def get_or_fetch(
        self,
        key: str,
        ttl_s: int,
        loader: Callable[[], Awaitable[Any]],
    ) -> tuple[Any, float]:
        """Return ``(value, stale_seconds)``.

        ``stale_seconds`` is the age of the value (0 for a fresh fetch),
        propagated to the tool result so the AI can phrase carefully
        ("as of about 8 minutes ago, …").
        """
        ...

    def invalidate_prefix(self, prefix: str) -> None:
        """Drop all entries whose key starts with ``prefix``. Used by
        ``on_config_changed`` when the resolved backend changes (or
        when the alert poll detects a new SEVERE alert and we want
        to soft-bypass the current-conditions cache for that location)."""
        ...
```

**Single-flight, not per-key Lock dicts.** Under load, two AI turns in different conversations both calling `current_weather` for the same location at the same time result in **one** Open-Meteo HTTP request: the second caller awaits the first caller's `Future`. Once the future resolves, both callers receive the same value and the future is removed from `_inflight`. There is no long-lived per-key lock dict to leak.

**LRU eviction.** On every `get_or_fetch` write, if `len(_entries) > max_entries`, the least-recently-used entry is popped. Read access (cache hit within TTL) moves the entry to the most-recently-used end. Lazy expiry on read drops entries past `expires_at` before returning. This combination prevents both the lock-leak AND entry-leak failure modes.

**Cache is in-memory only.** Restart wipes it. That's fine for Open-Meteo (no rate concerns) — but if a future rate-limited backend is added, migrate cache to `entity_storage` so a plugin-install restart doesn't blast the throttled API. (Open Question 6.)

**Stale-on-failure policy.** When `get_or_fetch`'s loader raises `WeatherUnavailableError`, the cache does **not** silently serve a stale entry. The error propagates and the tool layer returns a structured error. Honesty over best-effort. If a future config knob makes stale-on-failure opt-in, the response payload must carry `{"stale": true, "stale_seconds": ...}` so the AI can caveat.

**Soft-bypass on active alerts.** When the alert-poll loop sees an active alert with severity ≥ `SEVERE` for a location, the service calls `cache.invalidate_prefix(f"{backend}:current:{lat},{lon}")` for that location so the next "is it raining now?" call hits the live API rather than serving a possibly-stale value. Implementation lives in `_poll_alerts`, not in the cache itself.

### Location resolution

`WeatherService.resolve_location(user)` walks this chain in order, returning the first hit:

1. **Per-user override.** `user_prefs.{user_id}` row (collection name `user_prefs` *inside* the `gilbert.weather` storage namespace — i.e. the underlying SQLite table is `gilbert.weather.user_prefs`). Set via the `/weather set_home` slash command or the per-user account panel.
2. **Presence-derived hint.** *Optional, conservative.* If presence service is available AND the user's most recent presence record has a `latitude`/`longitude` field, use it. **Phase 1 of this PR: do NOT use presence-derived location.** UniFi presence today doesn't carry coords. We define the hook so `NWS` / `OpenWeather` plugins or a future GPS-aware presence backend can light it up without the weather service changing.
3. **Service default `home_location`.** Stored in entity storage at `gilbert.weather.service_state._id="home_location"`. Set by an admin via the `home_location.set` Settings ConfigAction (NOT a `ConfigParam` in the section table — see "Configuration parameters" below for the rationale).
4. **None.** If nothing is configured, callers raise `LocationNotConfiguredError`. The AI tool layer catches and returns:
   ```json
   {"error": "no_home_location",
    "message": "I don't know where you are. Tell me your city and I'll set it for you, or run /weather set_home <city>.",
    "set_home_command": "/weather set_home"}
   ```
   The AI is encouraged (via tool description hint) to follow up with "what city should I use?" and either (a) call `geocode_location(query=...)` then `/weather set_home <picked>`, or (b) call `current_weather(location="<city>")` once for an ad-hoc lookup.

`resolve_units(user)` is simpler:

1. `gilbert.weather.user_prefs.{user_id}.units` if set.
2. Service default `weather.default_units`.

**Storage call discipline.** `resolve_location` and `resolve_units` MUST perform a fresh `storage.get(...)` on every call. Per-user prefs are NEVER cached on `self._user_locations: dict` or similar — that's the exact pattern flagged as forbidden in [Multi-User Isolation](.claude/memory/memory-multi-user-isolation.md). A small per-tool-call memoization keyed by `(method, user_id, request_id)` is fine; long-lived service-instance state is not. The per-user prefs row is created on demand the first time the user runs `/weather set_home` or `/weather set_units`.

**Per-user prefs ACL.** The slash-command implementations of `set_home_location` / `set_units` always write the *caller's own* prefs (read `_user_id` from injected args) — there is no cross-user write path in this PR. Reading another user's prefs is not exposed at all. If admin-on-behalf prefs setting is wanted in a future PR, the natural shape is a `set_user_home_location(user_id, query)` admin tool with the same single-collection storage; deferred — see Open Questions.

### Open-Meteo backend (`std-plugins/open-meteo/`)

Plugin layout (matches the `tavily/` template):

```
std-plugins/open-meteo/
  __init__.py
  plugin.yaml
  plugin.py
  pyproject.toml
  open_meteo_weather.py
  weather_codes.py             ← provider-code → WeatherCondition mapping
  tests/
    conftest.py
    test_open_meteo_weather.py
    test_weather_codes.py
```

`plugin.yaml`:

```yaml
name: open-meteo
version: "1.0.0"
description: "Open-Meteo weather backend — current, hourly, and daily forecasts (no API key required)"

provides:
  - open-meteo-weather

requires: []
depends_on: []
```

`plugin.py`: trivial side-effect plugin, mirrors `tavily/plugin.py`. `setup()` does `from . import open_meteo_weather  # noqa: F401`.

`pyproject.toml`: `dependencies = []` (Open-Meteo only needs `httpx`, already a Gilbert core dep).

`open_meteo_weather.py` (imports `ConfigParam` from `gilbert.interfaces.configuration` and `ToolParameterType` from `gilbert.interfaces.tools` — both inside the allowed plugin import scope):

```python
from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.tools import ToolParameterType


class OpenMeteoWeather(WeatherBackend):
    backend_name = "open-meteo"

    _FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
    _GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="timeout_seconds",
                type=ToolParameterType.INTEGER,
                description="HTTP request timeout in seconds.",
                default=15,
            ),
            ConfigParam(
                key="user_agent",
                type=ToolParameterType.STRING,
                description=(
                    "HTTP User-Agent for Open-Meteo requests. Be a good "
                    "citizen — identify your install."
                ),
                default="Gilbert/1.0 (open-meteo-plugin)",
            ),
        ]

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="test_connection",
                label="Test connection",
                description="Hit the Open-Meteo forecast endpoint for a known coordinate.",
            ),
        ]

    def capabilities(self) -> WeatherBackendCapabilities:
        return WeatherBackendCapabilities(
            current=True, hourly=True, daily=True, alerts=False,
        )

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._timeout: int = 15

    async def initialize(self, config: dict[str, Any]) -> None:
        self._timeout = int(config.get("timeout_seconds", 15))
        ua = str(config.get("user_agent", "Gilbert/1.0 (https://github.com/briandilley/gilbert)"))
        # Granular timeouts so a hung DNS / TLS handshake doesn't burn
        # the whole 15s budget on connect alone. Limits cap concurrent
        # connections — under cache-stampede or alert-poll churn this
        # prevents an unbounded socket fan-out.
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            headers={"User-Agent": ua},
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def current(self, location, *, units=WeatherUnits.METRIC) -> CurrentWeather:
        params = self._common_params(location, units)
        params["current"] = ",".join([
            "temperature_2m", "apparent_temperature", "relative_humidity_2m",
            "wind_speed_10m", "wind_gusts_10m", "wind_direction_10m",
            "pressure_msl", "precipitation", "cloud_cover", "weather_code",
        ])
        data = await self._fetch(self._FORECAST_URL, params)
        return _parse_current(data, location, units)

    async def forecast_hourly(self, location, *, hours=24, units=WeatherUnits.METRIC):
        ...
    async def forecast_daily(self, location, *, days=7, units=WeatherUnits.METRIC):
        ...
    async def geocode(self, query, *, count=5) -> list[GeoLocation]:
        ...
```

**Rate-limit posture.** Open-Meteo's free tier permits up to 600 requests/min, 5,000/hour, 10,000/day. The cache TTLs (10 min current, 30 min hourly, 1 hour daily, 5 min alerts) ensure typical home-assistant usage stays well under these limits. Geocoding is invoked only on form-submit (Settings ConfigAction) or on AI tool call — not on keystroke. **Commercial use requires a paid Open-Meteo plan / API key**; that is documented in `std-plugins/README.md`'s Open-Meteo section and is not addressed by this PR.

**Fixture lat/lon scrubbing.** Test fixtures committed under `tests/fixtures/` use a public coordinate (`41.4993, -81.6944` — Cleveland, OH) so committing a fixture doesn't dox a developer's home. `scripts/refresh_fixtures.sh` (committed alongside) re-records fixtures from the live API so the shape stays current — see Test Strategy.

`weather_codes.py` holds the WMO weather-code → `WeatherCondition` mapping. Open-Meteo's `weather_code` follows WMO 4677. The full mapping (every code from 0–99 must map to *something*; tests assert this):

| WMO code(s) | Meaning | `WeatherCondition` |
|---|---|---|
| 0 | Clear sky | `CLEAR` |
| 1, 2 | Mainly clear / partly cloudy | `PARTLY_CLOUDY` |
| 3 | Overcast | `CLOUDY` |
| 4 | Smoke (legacy WMO) | `SMOKE` |
| 5 | Haze (legacy WMO) | `HAZE` |
| 45, 48 | Fog / depositing rime fog | `FOG` |
| 51, 53, 55 | Drizzle: light/mod/dense | `DRIZZLE` |
| 56, 57 | Freezing drizzle | `FREEZING_DRIZZLE` |
| 61, 63 | Rain: light/moderate | `RAIN` |
| 65 | Rain: heavy | `HEAVY_RAIN` |
| 66, 67 | Freezing rain | `FREEZING_RAIN` |
| 71, 73 | Snow: light/moderate | `SNOW` |
| 75 | Snow: heavy | `HEAVY_SNOW` |
| 77 | Snow grains | `SNOW` |
| 80, 81 | Rain showers slight/moderate | `RAIN` |
| 82 | Rain showers violent | `HEAVY_RAIN` |
| 85 | Snow showers slight | `SNOW` |
| 86 | Snow showers heavy | `HEAVY_SNOW` |
| 95 | Thunderstorm | `THUNDERSTORM` |
| 96, 99 | Thunderstorm with hail | `THUNDERSTORM_HAIL` |
| any other | unknown / undefined | `UNKNOWN` |

The mapping is a static dict; tests assert every code 0–99 maps to a non-`UNKNOWN` value where Open-Meteo defines one and to `UNKNOWN` for codes outside the documented set (NOT a raise — graceful unknown is the contract).

### How it accommodates NWS and OpenWeather (verification)

**NWS** (`std-plugins/nws/` future plugin) needs:

- `backend_name = "nws"`. Configured base URL `https://api.weather.gov`.
- `capabilities() = WeatherBackendCapabilities(current=True, hourly=True, daily=True, alerts=True)`.
- `current()` walks the two-step NWS flow (`/points/{lat},{lon}` → `/stations/{id}/observations/latest`).
- `forecast_hourly()` / `forecast_daily()` use the per-point gridpoint forecast.
- `alerts()` queries `/alerts/active?point={lat},{lon}` and converts the GeoJSON features into `WeatherAlert` instances. `alert.alert_id = feature.id`, `severity` mapped from the `properties.severity` field, etc. — fits the dataclass exactly.
- `geocode()` raises `NotImplementedError` (NWS has no place-name lookup) — `WeatherService` falls back to whichever backend in the registry implements geocoding (Open-Meteo, OpenWeather) at config-action time. *This is why `geocode` lives on the backend, not on the service: it's a backend capability that may be borrowed across plugins.*
- Restricted to US lat/lon ranges; NWS returns 404 outside its domain. The service wrapper logs a clear error.

The `WeatherAlert` dataclass was specifically shaped to accommodate NWS GeoJSON features:

| `WeatherAlert` field | NWS GeoJSON `properties.*` |
|---|---|
| `alert_id` | `id` |
| `title` | `event` |
| `description` | `description` |
| `severity` | `severity` (Minor/Moderate/Severe/Extreme — one-to-one with `AlertSeverity`) |
| `issued_at` | `sent` |
| `expires_at` | `expires` |
| `affected_area` | `areaDesc` |
| `source` | hard-coded `"NWS"` |
| `url` | `id` (URL) or `web` |

**OpenWeatherMap** (`std-plugins/openweather/` future plugin) needs:

- `backend_name = "openweather"`.
- `backend_config_params()` adds `api_key` (`sensitive=True`).
- `capabilities() = WeatherBackendCapabilities(current=True, hourly=True, daily=True, alerts=True)` (One Call API includes alerts).
- `current()` / `forecast_*()` map cleanly onto the One Call API response.
- `alerts()` reads the `alerts` array from One Call API.
- `geocode()` uses OpenWeather's geocoding endpoint.

The interface accommodates both without modification.

### Tool return shape — deterministic summary, no second AI hop

The service does **not** spawn a second AI call to "narrate" weather. The original draft of this spec proposed a `narrate=True` flag that would route through `complete_one_shot(...)` with a hardcoded narrator prompt; that's removed. Three reasons (per the product review's blocker on this point):

1. **Double-LLM is worse for AI agentic loops.** The calling AI asked because *it* wants to compose the reply. Pre-rendering through a different model strips structured signal (numbers, units, timestamps) before the outer model sees it.
2. **The narrator voice is voiceless next to Gilbert's voice.** Soul/identity composition only happens in the main `chat()` system prompt; a `complete_one_shot` from inside a tool lacks it, so slash users would hear a bland voice that contradicts every other Gilbert reply.
3. **Slash users can still get prose.** The deterministic summary string (below) is rendered by Python, not an LLM, and is what gets shown to slash users as the assistant message.

Every tool returns a single JSON shape with a deterministic `summary` string AND the structured fields:

```json
{
  "summary": "Currently 4°C and overcast in Cleveland, OH. Light wind from the west, no precipitation. Feels like 1°C.",
  "temperature": 4.0,
  "feels_like": 1.0,
  "condition": "cloudy",
  "humidity_pct": 78,
  "wind_speed": 3.2,
  "wind_gust": null,
  "precipitation_last_hour": 0.0,
  "units": "metric",
  "observed_at": "2026-05-09T14:30:00Z",
  "location": {"name": "Cleveland, OH, USA", "latitude": 41.4993, "longitude": -81.6944, "timezone": "America/New_York"},
  "stale_seconds": 240,
  "source": "open-meteo"
}
```

`summary` is built by a small Python function `_render_current_summary(cw: CurrentWeather) -> str` (and parallel functions for hourly / daily / alerts) that:

- Uses unit suffixes from `units` (`°C`/`°F`, `km/h`/`mph`, `mm`/`in`) so the AI never has to guess. Never converts.
- Translates `WeatherCondition` to natural English via a small dict (`PARTLY_CLOUDY → "partly cloudy"`).
- Includes the location name when present.
- Includes "feels like" only when it differs from temperature by ≥3°.
- Uses unambiguous timing words for forecasts (`"around 4pm"`, `"Friday morning"`) computed from the location's `timezone`, not the server's.

For the `weather_alerts` tool, the empty-alerts response carries explicit metadata so the AI doesn't conflate "no alerts" with "this backend doesn't issue alerts":

```json
{
  "alerts": [],
  "supported": false,
  "reason": "Open-Meteo does not issue severe-weather alerts. Install the NWS plugin (US) or OpenWeather plugin for alert coverage.",
  "location": {...},
  "source": "open-meteo"
}
```

vs. the supported-but-empty case:

```json
{
  "alerts": [],
  "supported": true,
  "location": {...},
  "source": "nws"
}
```

The tool description tells the model that `supported=false` means "no data" rather than "no alerts," and asks it to mention the limitation when the user is asking about safety.

**No `narrate_*_prompt` ConfigParams.** The deterministic-summary functions have no AI prompt to configure. (The greeting service still has its own AI prompts — and those remain `ConfigParam(ai_prompt=True)` per the rule. See "Greeting integration" below.)

### Configuration parameters (`config_namespace = "weather"`)

`ConfigParam` only renders scalar `ToolParameterType` values (STRING/INTEGER/BOOL/NUMBER/ARRAY) in the Settings UI — `OBJECT` is supported as a JSON-string parameter at most. For that reason, **`home_location` is NOT a `ConfigParam`**; it lives in entity storage at `gilbert.weather.service_state._id="home_location"` and is set exclusively via the `home_location.set` Settings ConfigAction (two-phase geocode → pick). All `gilbert.yaml` references to `home_location` likewise omit it; admins set it once via the action and never touch the YAML.

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | BOOL | False | Master toggle. See note below on default. |
| `backend` | STRING | `"open-meteo"` | `choices` from `WeatherBackend.registered_backends()`. `restart_required=True`. |
| `default_units` | STRING | `"metric"` | `choices=("metric", "imperial")`. |
| `cache_ttl_current_seconds` | INTEGER | 600 | TTL for current-conditions cache. |
| `cache_ttl_hourly_seconds` | INTEGER | 1800 | TTL for hourly-forecast cache. |
| `cache_ttl_daily_seconds` | INTEGER | 3600 | TTL for daily-forecast cache. |
| `cache_ttl_alerts_seconds` | INTEGER | 300 | TTL for alerts cache. |
| `digest_enabled` | BOOL | False | Publish a daily `weather.digest` event. |
| `digest_hour` | INTEGER | 7 | Server-local hour (0–23) to publish digest. |
| `digest_minute` | INTEGER | 0 | Server-local minute. |
| `digest_horizon_hours` | INTEGER | 12 | Hourly slices in the digest payload (capped trim, see "Daily digest"). |
| `digest_horizon_days` | INTEGER | 3 | Daily slices in the digest payload. |
| `alert_poll_seconds` | INTEGER | 300 | Interval for alert polling. No effect if backend doesn't issue alerts. |
| `alert_voice_enabled` | BOOL | False | Whether the service should call `SpeakerProvider.announce` for high-severity alerts. |
| `alert_voice_minimum` | STRING | `"extreme"` | Minimum severity for voice announcement. `choices=("severe","extreme")`. |
| `settings.*` | (varies) | — | Backend-merged params via `backend_config_params()` (e.g. Open-Meteo's `timeout_seconds`, `user_agent`). |

**Note on `digest_timezone` removal.** The earlier draft of this spec carried a `digest_timezone` config to schedule the digest in a non-server timezone. Per `memory-scheduler-service.md`, **scheduler time handling is naive-local throughout** — there is no tz-aware DAILY job primitive. Adding a `digest_timezone` knob would either silently no-op or require a custom DST-aware wrapper around the scheduler. Either is wrong for this PR. The digest fires at `digest_hour:digest_minute` in the server's local timezone, full stop. If a future scheduler iteration grows tz-aware DAILY support, this knob comes back; until then it's removed.

**`enabled: false` rationale.** Open-Meteo needs no API key, so the obvious pull is to default `enabled: true`. We keep it `false` because the service is essentially useless without a configured `home_location` — defaulting `enabled=true` would surface "No home location configured" errors immediately on every weather tool call. The first-boot flow is: admin opens Settings → Weather, runs the `home_location.set` action, then flips `enabled` on. Two clicks instead of "tool errors at me until I configure it."

Per-user prefs (collection `gilbert.weather.user_prefs`, `_id` = `user_id`):

```python
{
    "_id": "<user_id>",
    "user_id": "<user_id>",
    "location": { "latitude": ..., "longitude": ..., "name": ..., "timezone": ..., "country_code": ... } | None,
    "units": "metric" | "imperial" | None,
    "updated_at": "<iso8601>"
}
```

Stored via `StorageProvider.create_namespaced("gilbert.weather")` then `backend.put("user_prefs", user_id, doc)`. Read on every `resolve_*` call — no in-memory caching of these rows on the service singleton.

Service-internal state collections (also under `gilbert.weather.*`):
- `service_state` — `home_location` row, future single-row state.
- `alert_dedup` — one row per `(location_key, scope_id)` carrying the seen `alert_id`s and `last_seen` timestamps. Persisted at `stop()` and re-loaded at `start()` so restarts don't re-fire active alerts.

### Geocoding cross-backend fallback

The active backend's `geocode()` may raise `NotImplementedError` (NWS being the canonical example). Resolution rule:

1. At `start()`, try `await self._backend.geocode("test query that returns []")` once. If it returns (empty list or otherwise), set `self._geocoder_backend = self._backend`. If it raises `NotImplementedError`, walk `WeatherBackend.registered_backends()` in declaration order and instantiate the first class whose `geocode` method is overridden (i.e. `cls.geocode is not WeatherBackend.geocode`). Call its `initialize({})` (empty config — geocoders that need keys must declare a sensible no-key default OR fail clearly). Store on `self._geocoder_backend`.
2. If no registered backend overrides `geocode`, set `self._geocoder_backend = None` and the `geocode_location` tool / `home_location.set` action both return a structured `{"error": "geocoding_unavailable", "message": "No installed weather backend supports place-name lookup. Install the open-meteo plugin."}` payload.
3. The borrowed instance lives for the service's lifetime. `stop()` calls `close()` on it.

This is why `geocode()` lives on the backend ABC, not on the service: it's a *backend capability* that can be borrowed across plugins without the service caring which one supplied it.

### ConfigActions

On the Settings page card:

- **`home_location.set`** — user enters a place-name string in the action-followup form, the service calls `self._geocoder_backend.geocode(query)` (see "Geocoding cross-backend fallback"), returns the candidate list as a follow-up form with a select. The user picks one and the service writes it into `gilbert.weather.service_state._id="home_location"`. Standard two-phase ConfigAction flow per `memory-config-actions.md`. If `_geocoder_backend is None`, the action returns an error result with a clear "install the open-meteo plugin" message.
- **`test_connection`** — proxies to `backend.invoke_backend_action("test_connection")`. Open-Meteo's implementation calls `current()` for `(0.0, 0.0)` and reports success.

### Tool Surface

The service registers six `ToolDefinition` entries under `slash_group="weather"`. Four are **read-only AI tools** — `parallel_safe=True`, `required_role="user"`, AI-visible. Two are **slash-only configuration tools** that are intentionally hidden from the AI tool discovery surface (see "Hiding the write tools from the AI" below). All six are slash-accessible.

The descriptions below tell the model **when to call**, not just *what is returned* — borrowing from `web_search`'s pattern. This is the difference between the model picking quickly and the model reading and re-reading the description on each turn.

```python
ToolDefinition(
    name="current_weather",
    slash_group="weather",
    slash_command="now",
    slash_help="Current weather: /weather now [location]",
    description=(
        "Get the current weather. Call this when the user asks about *now* "
        "— temperature, whether it's raining, how it feels outside, what "
        "to wear today. Returns temperature, conditions, wind, humidity, "
        "and a one-sentence summary. The caller's configured location is "
        "used unless `location` is given. If no location is configured "
        "anywhere, the response is a structured error you should surface "
        "to the user — offer to set their home with /weather set_home."
    ),
    parameters=[
        ToolParameter(
            name="location",
            type=ToolParameterType.STRING,
            description=(
                "Location query — a city/place name (will be geocoded), "
                "or lat,lon coordinates. Omit to use the caller's "
                "configured location."
            ),
            required=False,
        ),
    ],
    required_role="user",
    parallel_safe=True,
),

ToolDefinition(
    name="forecast",
    slash_group="weather",
    slash_command="forecast",
    slash_help="Forecast: /weather forecast [location] [hours|days]",
    description=(
        "Get a weather forecast. Call this when the user asks about *later "
        "today*, *tomorrow*, *this week*, or any future window. Use `hours` "
        "for short windows (next few hours, today) or `days` for longer "
        "(weekly outlook). Don't use for current conditions — call "
        "`current_weather` instead. Specify `hours` OR `days`, not both. "
        "If neither is given, defaults to `hours=24`."
    ),
    parameters=[
        ToolParameter(name="location", type=ToolParameterType.STRING,
                      description="Location query or lat,lon. Optional — defaults to caller's location.",
                      required=False),
        ToolParameter(name="hours", type=ToolParameterType.INTEGER,
                      description="Hour-by-hour forecast horizon (1–72).",
                      required=False),
        ToolParameter(name="days", type=ToolParameterType.INTEGER,
                      description="Daily-summary horizon (1–14).",
                      required=False),
    ],
    required_role="user",
    parallel_safe=True,
),

ToolDefinition(
    name="weather_alerts",
    slash_group="weather",
    slash_command="alerts",
    slash_help="Active severe-weather alerts: /weather alerts [location]",
    description=(
        "Get active severe-weather alerts (warnings, watches, advisories) "
        "for a location. Call this when the user asks 'any storms?' / "
        "'is there a warning out?' / before answering questions about "
        "outdoor safety. The response carries `supported: true|false` — "
        "`supported=false` means the configured backend doesn't issue "
        "alerts (e.g. Open-Meteo) and that's NOT the same as 'no alerts.' "
        "If the user is asking about safety and `supported=false`, mention "
        "the limitation rather than implying they're in the clear."
    ),
    parameters=[
        ToolParameter(name="location", type=ToolParameterType.STRING,
                      description="Location query or lat,lon. Optional.",
                      required=False),
    ],
    required_role="user",
    parallel_safe=True,
),

ToolDefinition(
    name="geocode_location",
    slash_group="weather",
    slash_command="geocode",
    slash_help="Resolve a place name: /weather geocode <query>",
    description=(
        "Resolve a place-name query to candidate lat/lon coordinates. "
        "Call this when the user mentions a place you don't have "
        "coordinates for and you need to disambiguate (e.g. 'weather in "
        "Springfield' returns multiple hits). Returns a list of "
        "candidates; pick one and pass it back to `current_weather` / "
        "`forecast` as `lat,lon` to skip a second geocoding round-trip."
    ),
    parameters=[
        ToolParameter(name="query", type=ToolParameterType.STRING,
                      description="Place name (city, region, country)."),
        ToolParameter(name="count", type=ToolParameterType.INTEGER,
                      description="Max candidates (default 5, max 10).",
                      required=False),
    ],
    required_role="user",
    parallel_safe=True,
),
```

The two write tools are slash-only:

```python
ToolDefinition(
    name="set_home_location",
    slash_group="weather",
    slash_command="set_home",
    slash_help="Set your home location: /weather set_home <city or lat,lon>",
    description="Set the caller's per-user home location for weather queries.",
    parameters=[ToolParameter(name="query", type=ToolParameterType.STRING,
                              description="Place name to geocode, or 'lat,lon'.")],
    required_role="user",
    parallel_safe=False,
    ai_visible=False,   # see below
),

ToolDefinition(
    name="set_units",
    slash_group="weather",
    slash_command="set_units",
    slash_help="Set your preferred units: /weather set_units <metric|imperial>",
    description="Set the caller's preferred units for weather output.",
    parameters=[ToolParameter(name="units", type=ToolParameterType.STRING,
                              description="metric or imperial.",
                              enum=["metric", "imperial"])],
    required_role="user",
    parallel_safe=False,
    ai_visible=False,   # see below
),
```

**Hiding the write tools from the AI.** Configuration mutations (`set_home_location`, `set_units`) are intentionally NOT exposed to the AI tool surface, because the model will too eagerly read intent into casual phrasing ("it's getting cold here" → "let me set you to imperial"). The `ai_visible` flag is added to `ToolDefinition` for this purpose. `AIService._discover_tools` filters out `ai_visible=False` entries before sending the tool list to the model; the slash-command path ignores the flag (slash invocations are always intentional). `web_search` etc. default to `ai_visible=True`. (If extending `ToolDefinition` is rejected, the equivalent fallback is to give these tools `required_role="never"` or to register them under a slash-only provider that does not declare `ai_tools` capability — but adding a single named flag is cleaner. State the choice; pick one before implementation.)

**Six tools total under one `slash_group="weather"`** — the slash_group collapse the architecture-checklist requires for multi-tool services.

**`hours` + `days` mutual exclusivity for `forecast`.** Tool-call schema can't express "exactly one of these" in JSON Schema cleanly. Validation lives in the service handler:

```python
if hours is not None and days is not None:
    return json.dumps({"error": "invalid_arguments",
                       "message": "Specify hours OR days, not both."})
```

Same handler also validates `hours in 1..72` and `days in 1..14`, returning a structured error rather than letting the upstream API reject. This is documented under "Error contract."

**`tool_provider_name = "weather"`.** No `slash_namespace` is set on the service class — that's a *plugin* concept (per `memory-slash-commands.md` § Plugin namespacing). For a core service, the bare `slash_group` already provides the prefix; explicitly setting `slash_namespace="weather"` would produce double-prefixed `/weather:weather:now` keys and is wrong. (The earlier draft of this spec recommended setting it; that recommendation was incorrect and is removed.)

**Default leaf for `/weather`.** Typing `/weather` with no subcommand is a common muscle-memory shortcut for "current weather." The dispatcher does longest-prefix lookup — to support `/weather` mapping to `current_weather`, register the same `ToolDefinition` once with `slash_group="weather", slash_command="now"` AND a second top-level alias entry with bare `slash_command="weather"`. Both point at the same `name="current_weather"` tool. Slash-uniqueness is enforced on the `(group, command)` pair, so this is allowed. Flagging in the spec because it's a conscious decision, not a default.

### Error contract

Every tool catches all backend errors (`httpx.HTTPError`, `httpx.TimeoutException`, JSON parse errors, `LocationNotConfiguredError`, `WeatherUnavailableError`) and returns a structured JSON error string. **Exceptions never escape the tool layer** — the AI cannot recover from a Python exception, but it can recover from a `{"error": ...}` dict.

Error shapes:

```json
{"error": "no_home_location",
 "message": "I don't know where you are. Tell me your city, or run /weather set_home <your city>.",
 "set_home_command": "/weather set_home"}
```

```json
{"error": "weather_unavailable",
 "message": "Open-Meteo returned 503 — try again in a moment.",
 "retryable": true,
 "provider_status": 503}
```

```json
{"error": "geocoding_unavailable",
 "message": "No installed weather backend supports place-name lookup. Install the open-meteo plugin."}
```

```json
{"error": "invalid_arguments",
 "message": "Specify hours OR days, not both.",
 "retryable": false}
```

```json
{"error": "no_results",
 "message": "No matching place found for 'asdfasdf'.",
 "retryable": false}
```

**No silent stale-on-failure.** When the backend returns 5xx and the cache has a stale entry, the tool returns the error — it does NOT serve the stale value silently. If a future config opt-in adds stale-fallback, the response carries `{"stale": true, "stale_seconds": N}` so the AI can caveat ("As of about 9 minutes ago, it wasn't raining — though I can't reach the live service right now.").

**Exceptions to the "catch and return JSON" rule.** None for tools. The `WeatherProvider` protocol methods (called by other in-process services) DO raise the typed errors (`LocationNotConfiguredError`, `WeatherUnavailableError`); each consumer catches and decides how to surface — see "Greeting integration" for the pattern.

**Anti-fabrication note for downstream prompts.** Any place a Gilbert prompt template ingests weather-tool output as JSON (greeting, daily-summary digest consumers), the prompt must explicitly say: "Quote only values present in the JSON. If a field is null, missing, or the JSON is an error, do not invent values; report the error or omit the weather mention." Retrofit the existing `_DEFAULT_GREETING_PROMPT` (or whatever its current key is) when this PR lands.

### Caller identity & multi-user safety

- `WeatherService` is a singleton per the Gilbert pattern. Per-request state (`user_id`, `conversation_id`) is **never** stored on `self`. The AI service injects `_user_id` / `_conversation_id` into tool arguments; tool implementations read these from `arguments.get("_user_id")`, never from `self`. (See `_run_one_tool` in `core/services/ai.py`.)
- The cache uses single-flight `Future`s keyed by cache key (which includes `backend_name`), and an LRU-bounded entries map — no long-lived per-key lock dict, no global lock. See "Cache shape."
- Scheduled jobs (`weather.digest`, `weather.alerts.poll`) run as `UserContext.SYSTEM`. The poll's `_known_alert_ids` dict is owned by exactly one coroutine (the poll loop) at a time; the singleton-attribute is safe in this scenario because the system context is the sole writer. The shape is `dict[(location_key, scope_id), set[str]]` so per-user fan-out (when added in a follow-up PR) is just another `scope_id` value, not a structural change.
- The `WeatherProvider` protocol takes `UserContext` (not bare `user_id` strings) as an explicit parameter on every method, so callers from background jobs pass identity in rather than relying on the service to read a `ContextVar` that may not be set in their entry point. This is a deliberate departure from the ContextVar-default pattern; the rationale is that consumers (greeting, scheduled actions) are often non-request-bound. The single fallback path `_resolve_user(user)` reads `get_current_user()` only when `user is None`. Mixing both — "optional parameter, falls back to ContextVar everywhere" — is forbidden because it tends to produce both bugs at once.

### Greeting integration (the right way)

Weather logic does **not** move into `GreetingService`. Instead:

- `GreetingService.start()` already does an optional capability lookup; we add `self._weather = resolver.get_capability("weather")`. If `None` or not `isinstance(self._weather, WeatherProvider)`, behavior is unchanged.
- A new `ConfigParam(key="include_weather", type=BOOLEAN, default=True)` lets ops disable the integration.
- A new `ConfigParam(key="weather_hint_template", type=STRING, multiline=True, ai_prompt=False, default=_DEFAULT_WEATHER_HINT_TEMPLATE)` carries the deterministic blurb template. It's a Python `str.format`-style template, NOT an AI prompt, so `ai_prompt=False`. (The AI-prompts-are-always-configurable rule applies to anything passed to a model as a system/user prompt fragment; this string is interpolated into Gilbert's main greeting prompt as context, which makes it prompt-shaped and therefore configurable per the same rule.)
- `_DEFAULT_WEATHER_HINT_TEMPLATE` (defined in `greeting.py`):

  ```
  Current weather at {location_name}: {temperature:.0f}{temp_suffix} {condition_phrase}, wind {wind_speed:.0f}{speed_suffix}{feels_like_clause}. Mention it casually if it fits the moment, otherwise ignore. Quote only the values shown — never invent additional weather details.
  ```

- Inside `_generate_greeting()`, after composing the existing prompt, the integration block is:

  ```python
  weather_blurb = ""
  if self._include_weather and isinstance(self._weather, WeatherProvider):
      try:
          current = await self._weather.get_current(user=user_ctx)
      except LocationNotConfiguredError:
          pass            # silent — user hasn't set a location, that's fine
      except WeatherUnavailableError:
          logger.debug("Weather backend unavailable for greeting; skipping blurb")
      else:
          temp_suffix = "°F" if current.units is WeatherUnits.IMPERIAL else "°C"
          speed_suffix = "mph" if current.units is WeatherUnits.IMPERIAL else "km/h"
          condition_phrase = current.condition.value.replace("_", " ")
          feels_like_clause = (
              f", feels like {current.feels_like:.0f}{temp_suffix}"
              if current.feels_like is not None
              and abs(current.feels_like - current.temperature) >= 3
              else ""
          )
          location_name = current.location.name or "the configured location"
          weather_blurb = self._weather_hint_template.format(
              location_name=location_name,
              temperature=current.temperature,
              temp_suffix=temp_suffix,
              condition_phrase=condition_phrase,
              wind_speed=current.wind_speed,
              speed_suffix=speed_suffix,
              feels_like_clause=feels_like_clause,
          )
  ```

- The greeting prompt template gets a new optional placeholder `{weather_blurb}` interpolated only when non-empty.

Notes:
- **No "the shop" hardcoding.** The previous draft hardcoded a single user's deployment. The location name comes from `current.location.name` (which is exactly what that field is for), with a generic fallback.
- **Unit-aware rendering.** Suffixes follow `current.units`. The model never has to guess °C vs °F.
- **Typed errors only.** Catches `LocationNotConfiguredError` and `WeatherUnavailableError` specifically. A bare `except Exception` would hide programming bugs in the integration; per CLAUDE.md "Honesty is always the best policy" the right answer is to catch the documented domain errors and let real bugs surface.

This is the **only** code change inside `core/services/greeting.py` — keeps the integration loose. No imports of the concrete `WeatherService` class. No knowledge of which backend is active.

### Scheduler integration

The scheduler service does **not** acquire any direct dependency on weather. Instead, the AI gains the `forecast` and `weather_alerts` tools, and **users phrase their automations as natural-language alarms whose action calls `forecast` and gates on the result via the existing `ScheduledAction` / `ActionStep` machinery.**

Concrete example for "water the garden, but skip if rain in next 6h":

1. The user runs `/set_alarm` to create a daily 7pm alarm.
2. The alarm action is a `ScheduledAction` whose first `ActionStep` calls the `forecast` tool with `hours=6` and a `condition` step that bails out if `sum(precipitation) > threshold`.
3. The second `ActionStep` calls the watering action only when the first step's gate allowed it.

`ActionStep` already supports tool calls and result-based gating (per `memory-scheduler-service.md`'s `Schedule`/`ActionStep` model). The weather feature ships *the tool*; gating semantics are scheduler's existing surface. The spec deliberately does NOT add a new `weather`-specific scheduler primitive — the generic tool-call ActionStep is sufficient and stays orthogonal.

### Severe-alert delivery (NotificationService + voice)

`weather.alert.issued` events alone are not delivery — a user under a tornado warning is not well served by a reflection harvest noticing the alert later. `WeatherService` itself subscribes to its own event and dispatches:

```python
async def _on_alert_event(self, event: Event) -> None:
    severity = AlertSeverity(event.data.get("severity", "minor"))
    urgency = self._alert_urgency.get(severity, "info")
    title = event.data.get("title", "Weather alert")
    description = event.data.get("description", "")
    message = f"{title} — {description[:200]}".rstrip(" —")

    # Phase 1: deliver to all users who have a configured home_location
    # equal to (or geographically inside) the alert's affected_area.
    # Today, "geographically inside" is approximated as
    # location_key match (lat,lon to 4 decimals matches scope_id
    # of the poll). Per-user fan-out for a user with a different
    # home_location than the admin default is OUT OF SCOPE for this
    # PR — see Open Questions #1.
    if self._notifications is None:
        return
    for user_id in await self._users_for_alert(event):
        await self._notifications.notify_user(
            user_id=user_id,
            message=message,
            urgency=urgency,
            source="weather",
            source_ref=event.data.get("alert_id", ""),
        )

    # Voice on EXTREME (or SEVERE if alert_voice_minimum lowered).
    if (
        self._alert_voice_enabled
        and severity.value >= self._alert_voice_minimum.value
        and (speaker := self._resolver.get_capability("speaker_audio") if self._resolver else None) is not None
    ):
        await speaker.announce(message, voice="alert")
```

**Severity → urgency mapping:**

| `AlertSeverity` | `Notification.urgency` |
|---|---|
| `EXTREME` | `urgent` |
| `SEVERE` | `urgent` |
| `MODERATE` | `normal` |
| `MINOR` | `info` |

**Voice ladder:** opt-in via `alert_voice_enabled`. Default OFF. When ON, the service calls `SpeakerProvider.announce(...)` for severities ≥ `alert_voice_minimum` (default `EXTREME`). This is the explicit v1 ladder requested by the product review; SMS / phone-call escalation is out of scope.

**Per-user vs. service-default fan-out (v1 limitation, must be documented in the per-user `set_home_location` flow).** Only users whose stored `home_location` matches the polled location receive notifications in this PR. A user with a per-user `home_location` *different from* the admin home is NOT polled separately; they will not receive alerts for their location until the multi-poll PR lands. The user-facing text on the per-user `/weather set_home` flow says: *"Severe-weather alerts currently use the admin's home location. Your per-user location is used for current conditions and forecasts only."* This explicit limit is preferable to silently fanning out per-user polls without an SLA.

### Proposals integration (read-only)

`ProposalsService` already loads tools from any service with `ai_tools` capability when running its reflection/harvest AI calls. With `WeatherService` registering `ai_tools`, the reflection AI can opportunistically call `weather_alerts` — e.g. when reflecting on the past day's events and noticing a high-wind alert it didn't act on, it can check the alerts list and propose "wire shade-close response to high-wind alerts."

The `weather.alert.issued` event is delivered via `NotificationService` (above) for *operational urgency*; the proposals event-bus subscriber's `subscribe_pattern("*")` will *also* see the event for *pattern synthesis*. We add `weather.alert.*` to **neither** noisy-list nor allowlist — the default `*` subscription picks it up. The two subscribers serve different purposes (delivery vs. learning) and don't conflict.

### Daily digest event

When `digest_enabled=True` and `scheduler` is available, a daily job at `digest_hour:digest_minute` (server local timezone) runs `_publish_digest()`. The digest job key is constant (`weather.digest`) and registration is idempotent — re-registering on a hot config reload is a no-op.

```python
async def _publish_digest(self) -> None:
    location = await self._load_home_location()
    if location is None:
        return
    units = self._default_units
    try:
        current = await self.get_current(location, units=units)
        # Hourly slices cover the rest of today only — capped to digest_horizon_hours.
        hourly = await self.get_forecast_hourly(
            location, hours=self._digest_horizon_hours, units=units,
        )
        # Daily slices start AT TOMORROW (today is covered by the hourly block above).
        daily = await self.get_forecast_daily(
            location, days=self._digest_horizon_days, units=units,
        )
        if daily and daily[0].date == _today_iso(location.timezone):
            daily = daily[1:]   # drop the redundant "today" slice
    except WeatherUnavailableError:
        logger.warning("Weather digest skipped — backend unavailable")
        return

    payload = {
        "current": _current_to_dict(current),
        "hourly": [_hourly_to_dict(h) for h in hourly],
        "daily": [_daily_to_dict(d) for d in daily],
        "location": _location_to_dict(location),
        "units": units.value,
    }
    # Hard-cap the payload — pathological config (digest_horizon_hours=72,
    # digest_horizon_days=14) would emit a JSON blob the event-bus
    # subscribers don't want. 50 hourly slices + 7 daily slices is plenty.
    payload["hourly"] = payload["hourly"][:50]
    payload["daily"] = payload["daily"][:7]

    await self._event_bus.publish(Event(
        event_type="weather.digest",
        data=payload,
        source="weather",
    ))
```

**Routing semantics.** `weather.digest` is a fan-out broadcast event with no `conversation_id` — every subscriber receives it. It carries no user-specific data (the digest is for the service-default location). Per-user digests are an Open Question for a follow-up PR.

**Restart / DST behavior:**
- The scheduler is naive-local. A server restart at 7:00:30 *misses* the 7:00:00 digest for that day; the next fire is tomorrow at 7:00:00. No catch-up. Documented limitation.
- DST transitions follow the scheduler's underlying behavior: `Schedule.DAILY` re-computes the next fire from "now," so a spring-forward day either gets a same-day digest or the next-day depending on when DST hits the registration tick. This is acceptable until the scheduler grows tz-aware DAILY support.

### Alert poll event

Only fires when `backend.capabilities().alerts == True`. Skipped silently for Open-Meteo.

```python
async def _poll_alerts(self) -> None:
    if self._backend is None:
        return
    if not self._backend.capabilities().alerts:
        return
    location = await self._load_home_location()
    if location is None:
        return
    try:
        current_alerts = await self._backend.alerts(location)
    except WeatherUnavailableError:
        logger.warning("Alert poll failed; will retry on next tick")
        return
    except Exception:
        logger.warning("Alert poll failed (unexpected)", exc_info=True)
        return

    loc_key = f"{round(location.latitude, 4)},{round(location.longitude, 4)}"
    scope_id = "system"
    dedup_key = (loc_key, scope_id)
    seen = self._known_alert_ids.setdefault(dedup_key, set())

    if not self._alert_dedup_loaded:
        # First sweep after restart: do NOT publish the existing active
        # alerts as "new" — load persisted dedup state and treat
        # everything currently active as already-seen.
        await self._load_alert_dedup()
        seen = self._known_alert_ids.setdefault(dedup_key, set())
        seen.update(a.alert_id for a in current_alerts)
        self._alert_dedup_loaded = True
        return

    new_alerts = [a for a in current_alerts if a.alert_id not in seen]
    for alert in new_alerts:
        seen.add(alert.alert_id)
        await self._event_bus.publish(Event(
            event_type="weather.alert.issued",
            data=_alert_to_dict(alert),
            source="weather",
        ))
        # Soft-bypass: invalidate the current-conditions cache for this
        # location so the next "is it raining" call hits live data.
        if alert.severity in (AlertSeverity.SEVERE, AlertSeverity.EXTREME):
            self._cache.invalidate_prefix(
                f"{self._backend_name}:current:{loc_key}"
            )
    # Drop dedup entries for alerts that are gone, persist async.
    seen.intersection_update({a.alert_id for a in current_alerts})
    await self._persist_alert_dedup_one(dedup_key, seen)
```

**Dedup persistence.** `_known_alert_ids` is initialized on first sweep from `gilbert.weather.alert_dedup` (one row per `(location_key, scope_id)`, fields: `seen_alert_ids: list[str]`, `last_updated: datetime`). Without this, every restart re-publishes every active alert as "new" — a notification-spam vector flagged as the highest-priority bug by the engineering review. On restart, the first poll sweep treats currently-active alerts as already-seen (NOT republished) and writes them through; only alerts that appear in poll N+1 but not poll N are published. The dedup row also carries TTL semantics on `last_updated` — entries older than 7 days are GC'd at startup so the table doesn't grow unboundedly across location changes.

The dict shape is `dict[(location_key, scope_id), set[str]]` so per-user fan-out (when added) reuses the same code path with `scope_id=user_id`.

## Bootstrap configuration

Per `CLAUDE.md`'s two-tier config model, `gilbert.yaml` is bootstrap-only (`storage`, `logging`, `web`). Weather config lives in the **`gilbert.config` entity collection**, seeded by `seed_storage()` on first boot. The block below is what the seeder writes into that collection — NOT a literal `gilbert.yaml` addition.

```yaml
# Seeded into the gilbert.config entity collection on first boot.
weather:
  enabled: false
  backend: "open-meteo"
  default_units: "metric"
  cache_ttl_current_seconds: 600
  cache_ttl_hourly_seconds: 1800
  cache_ttl_daily_seconds: 3600
  cache_ttl_alerts_seconds: 300
  digest_enabled: false
  digest_hour: 7
  digest_minute: 0
  digest_horizon_hours: 12
  digest_horizon_days: 3
  alert_poll_seconds: 300
  alert_voice_enabled: false
  alert_voice_minimum: "extreme"
  # `home_location` is intentionally omitted — set via the home_location.set
  # ConfigAction, stored at gilbert.weather.service_state._id="home_location".
  # `settings.*` is populated by the Open-Meteo plugin's plugin.yaml below.
```

Plugin defaults live in `std-plugins/open-meteo/plugin.yaml`'s `config:` section (per `memory-plugin-system.md`'s three-layer merge):

```yaml
# std-plugins/open-meteo/plugin.yaml
name: open-meteo
version: "1.0.0"
description: "Open-Meteo weather backend — current, hourly, and daily forecasts (no API key required)"
provides: [open-meteo-weather]
requires: []
depends_on: []

config:
  weather:
    settings:
      timeout_seconds: 15
      user_agent: "Gilbert/1.0 (https://github.com/briandilley/gilbert)"
```

This puts plugin-specific knobs in the plugin's own manifest (which is where they belong) and leaves the service-level defaults in the seeded entity-collection block. Users override either layer in `.gilbert/config.yaml` per the standard layering rules.

The User-Agent default carries a contact URL so an Open-Meteo operator can reach the project — Open-Meteo's free-tier docs ask for a useful identifier.

(Note: `enabled: false` by default. See "Configuration parameters" for the rationale.)

## Composition root

`src/gilbert/core/app.py`, immediately after the existing `WebSearchService` registration:

```python
# Weather service — multi-backend weather aggregator. Default backend
# is Open-Meteo (no API key required), provided by the open-meteo plugin.
from gilbert.core.services.weather import WeatherService

self.service_manager.register(WeatherService())
```

## Documentation updates

- `README.md` (root) — add Weather to the integration table.
- `std-plugins/README.md` — add an Open-Meteo row to the plugin table and a per-plugin section listing what it provides (`open-meteo-weather`), deps (none beyond core), config keys (`timeout_seconds`, `user_agent`), the geocoding caveat (no API key, but please keep the contact-URL `user_agent`), and an explicit "**commercial use requires a paid Open-Meteo plan / API key**" note. Also add a small "Powered by Open-Meteo" attribution line to the Settings card (frontend `ConfigSection` already supports an `attribution` slot — verify during implementation).
- `.claude/memory/MEMORIES.md` — add a `Weather Service` entry.
- New memory file: `.claude/memory/memory-weather-service.md` covering the interface, backend pattern adoption, location/units resolution chain, cache shape (single-flight + LRU), severe-alert delivery via NotificationService, dedup persistence model, and the planned NWS / OpenWeather slot-in.
- New memory file: `.claude/memory/memory-greeting-service.md`. (No existing greeting memory file; create it as a fresh memory rather than tacking onto another.) Cover the prompt structure, the new `include_weather` / `weather_hint_template` knobs, and how the integration uses `WeatherProvider`.

## Test Strategy

Following the project's "always write tests" rule and the rule that DB tests use a real test SQLite store.

### Unit tests — interface + service (`tests/unit/test_weather_service.py`)

- **Backend registration** — instantiating a fake `WeatherBackend` subclass with `backend_name = "fake"` registers it; `WeatherBackend.registered_backends()["fake"]` returns the class.
- **Service start/stop** — with backend disabled, no HTTP calls. With backend enabled and a stub backend, `start()` calls `backend.initialize(...)` with the resolved `settings` dict; `stop()` calls `backend.close()` and persists `_known_alert_ids` to `gilbert.weather.alert_dedup`.
- **Cache** — first call calls the backend; second call within TTL does not; cache invalidation after TTL re-fetches. Cache key includes `backend_name`: same `(method, lat, lon, units)` against two distinct backends does not share a slot.
- **Cache single-flight dedup** — two concurrent `get_current(loc)` calls result in **one** backend `current()` call. Done via a stub backend whose `current()` awaits an `asyncio.Event`. Both callers receive the same value.
- **Cache LRU bound** — `_WeatherCache(max_entries=4)` evicts the LRU entry on the 5th distinct key; tracks `_inflight` cleanup so no `Future` is left behind on either success or failure.
- **Cache stale_seconds** — fresh fetch returns `stale_seconds=0`; cache hit returns the elapsed monotonic seconds.
- **Cache no-stale-on-failure** — when the backend raises `WeatherUnavailableError` and the cache has an entry past TTL, the error propagates; the stale value is NOT returned.
- **Location resolution chain** — per-user override wins; missing per-user falls through to service default; missing both raises `LocationNotConfiguredError`. `resolve_location` performs a fresh `storage.get(...)` on every call (verified by counting backend reads).
- **Units resolution** — same chain.
- **`get_current` no-location path** — raises `LocationNotConfiguredError`; AI-tool wrapper catches and returns `{"error": "no_home_location", ...}`.
- **Identity contract** — `WeatherProvider.get_current(user=UserContext("u1"))` reads u1's prefs; `WeatherProvider.get_current(user=None)` falls back to `get_current_user()`; `WeatherProvider.get_current(user=None)` with no ContextVar uses `UserContext.SYSTEM` and the service-default location.
- **Tool execution — `current_weather`** — returns JSON with the expected fields including the deterministic `summary` string. No second AI hop is invoked.
- **Tool execution — `forecast`** — `hours` and `days` together → `{"error": "invalid_arguments"}`. `hours` outside 1–72 → invalid_arguments. Default (neither given) returns 24 hourly slices.
- **Tool execution — `weather_alerts`** — returns `{"alerts": [], "supported": false, "reason": ...}` when backend reports `alerts=False`; returns `{"alerts": [...], "supported": true}` when backend reports `alerts=True`.
- **Tool execution — `geocode_location`** — returns candidate list; empty result returns `{"error": "no_results"}`; backend without geocoding returns `{"error": "geocoding_unavailable"}`.
- **Tool surface visibility** — `get_tools(user_ctx=...)` (called via the AI's discovery path) includes `current_weather`/`forecast`/`weather_alerts`/`geocode_location` and EXCLUDES `set_home_location`/`set_units`. The slash-command path (whatever method enumerates slash-eligible tools) DOES include them.
- **Per-user prefs round-trip** — `/weather set_home <city>` writes `gilbert.weather.user_prefs.<user_id>`; subsequent `get_current` for that user reads from the row. The collection name verifies the namespace prefix.
- **Multi-user isolation** — two coroutines calling `get_current` with different `_user_id` arguments resolve different per-user locations correctly even when interleaved across `await`s.
- **Alert dedup persistence** — service start loads `gilbert.weather.alert_dedup`; first poll after restart with active alerts publishes ZERO `weather.alert.issued` events; second poll publishes only genuinely-new alerts. After `stop()`, the persisted row contains the seen set.
- **Severe-alert delivery** — when the alert-poll publishes `weather.alert.issued` with `severity=EXTREME`, the `_on_alert_event` subscriber calls `NotificationProvider.notify_user(...)` with `urgency="urgent"` for every user with a matching `home_location`. With `alert_voice_enabled=True`, `SpeakerProvider.announce(...)` is called.
- **Cache soft-bypass on SEVERE alert** — current-conditions cache for the alert's location is invalidated after a SEVERE/EXTREME alert publishes.
- **Geocoder fallback** — when `_backend.geocode` raises `NotImplementedError`, service walks the registry and instantiates an alternate; with no candidate, the `geocode_location` tool returns `geocoding_unavailable`.

### Unit tests — Open-Meteo backend (`std-plugins/open-meteo/tests/test_open_meteo_weather.py`)

- **Weather code mapping** — every code 0–99 has a defined mapping; codes outside the WMO 4677 set map to `UNKNOWN` and don't raise.
- **`current()` parses real Open-Meteo response shape** — fixture JSON captured from the actual API, parsed into `CurrentWeather`. HTTP client mocked via `httpx.MockTransport`. Fixtures use Cleveland, OH coords (no developer-home leak per CLAUDE.md privacy).
- **`forecast_hourly()` parses arrays correctly** — `time` array length matches every variable array length.
- **`forecast_daily()` parses sunrise/sunset as datetimes** in the location's timezone.
- **Units pass-through** — when `units=IMPERIAL`, `temperature_unit=fahrenheit` etc. are sent in the query string.
- **`alerts()` returns `[]`** — Open-Meteo doesn't issue warnings.
- **`geocode()` parses the geocoding endpoint** — returns `GeoLocation` list including timezone and country code.
- **`test_connection` action** — succeeds with a live-shape fixture; fails with a clear message on transport error.
- **`capabilities()` reports `alerts=False`**.
- **Timeout granularity** — `httpx.AsyncClient` is constructed with `httpx.Timeout(...)` and `httpx.Limits(...)`; the test asserts the constructor call shape rather than network behavior.

`std-plugins/open-meteo/tests/conftest.py` is copied from `unifi/tests/conftest.py` (NOT `tavily/tests/conftest.py`) because Open-Meteo has multiple internal modules with relative imports — the unifi conftest is the multi-module template.

### Fixture refresh

`scripts/refresh_fixtures.sh` (committed) re-records fixture JSON by hitting the live Open-Meteo API at the canonical Cleveland coordinate. Run when the upstream API contract shifts. CI does NOT run live API tests; an opt-in integration test suite (`pytest -m integration`) hits the live API and validates fixture shape compatibility.

### Architecture compliance tests

- `tests/unit/test_slash_command_uniqueness.py` already runs across all tools — adding the six `weather` tools must not collide.
- The existing layer-compliance test in `tests/unit/` (verify location during implementation; consolidate, don't duplicate) is extended to grep `src/gilbert/core/services/weather.py` for forbidden imports (no `gilbert.integrations.*`, no `gilbert.web.*`, no `gilbert.storage.*`), `src/gilbert/interfaces/weather.py` for `gilbert.core.*` imports, and `std-plugins/open-meteo/*.py` for `gilbert.core.services.*`/`gilbert.web.*` imports. If the existing test framework doesn't cover plugin directories, add the new assertions there rather than creating a feature-specific compliance test.

## File Manifest

New files:

- `src/gilbert/interfaces/weather.py` — ABC, dataclasses, `WeatherProvider` protocol, `LocationNotConfiguredError` / `WeatherUnavailableError`. ~320 lines.
- `src/gilbert/core/services/weather.py` — `WeatherService` aggregator + tool definitions + alert delivery. ~600 lines (with `_WeatherCache` extracted).
- `src/gilbert/core/services/_weather_cache.py` — `_WeatherCache` (single-flight + LRU). ~150 lines. Extracted because `weather.py` would otherwise grow past 700 lines; same pattern as other helper-private modules.
- `std-plugins/open-meteo/__init__.py` — empty package marker.
- `std-plugins/open-meteo/plugin.yaml` — plugin manifest.
- `std-plugins/open-meteo/plugin.py` — side-effect `setup()`. ~30 lines.
- `std-plugins/open-meteo/pyproject.toml` — uv workspace member, `dependencies = []`.
- `std-plugins/open-meteo/open_meteo_weather.py` — `OpenMeteoWeather` backend. ~350 lines.
- `std-plugins/open-meteo/weather_codes.py` — WMO code → `WeatherCondition` mapping. ~80 lines.
- `std-plugins/open-meteo/tests/conftest.py` — pytest plugin registration. Copy from `unifi/tests/conftest.py` (multi-module template — Open-Meteo has internal `from .weather_codes import …` imports).
- `std-plugins/open-meteo/scripts/refresh_fixtures.sh` — dev-only script that re-records fixture JSON from the live Open-Meteo API.
- `std-plugins/open-meteo/tests/test_open_meteo_weather.py` — backend tests with fixture JSON. ~250 lines.
- `std-plugins/open-meteo/tests/test_weather_codes.py` — code-mapping tests. ~80 lines.
- `std-plugins/open-meteo/tests/fixtures/forecast_response.json` — captured Open-Meteo response.
- `std-plugins/open-meteo/tests/fixtures/geocoding_response.json` — captured geocoding response.
- `tests/unit/test_weather_service.py` — service-level tests with stub backend. ~500 lines.
- `tests/unit/test_weather_layer_compliance.py` — import-rule audit. ~30 lines.
- `.claude/memory/memory-weather-service.md` — agent memory capturing what the service is, how it discovers backends, the location-resolution chain, the cache shape, and the planned NWS/OpenWeather slot-in.

Modified files:

- `src/gilbert/core/app.py` — register `WeatherService()`. ~3 lines added.
- `src/gilbert/config.py` — no Pydantic model needed for weather (it lives in entity storage, not bootstrap `gilbert.yaml`). The `seed_storage()` helper grows a weather block — verify location during implementation.
- `src/gilbert/interfaces/tools.py` — add `ai_visible: bool = True` field to `ToolDefinition`. ~3 lines. (See "Hiding the write tools from the AI." If review pushes back on adding this flag, the alternative is to move the two write tools to a separate slash-only provider; the spec's preferred path is the flag.)
- `src/gilbert/core/services/ai.py` — `_discover_tools` filters out `ai_visible=False` entries from the AI-facing tool list. The slash-command discovery path keeps them. ~5 lines.
- `src/gilbert/core/services/greeting.py` — add weather enrichment block, `include_weather` BOOL ConfigParam, `weather_hint_template` STRING ConfigParam (multiline, `ai_prompt=False`), prompt-anti-fabrication addendum to the existing greeting prompt template. ~50 lines.
- `README.md` — add Weather row to integration table; mention `open-meteo` plugin.
- `std-plugins/README.md` — add Open-Meteo plugin row + per-plugin section (incl. commercial-use note + attribution).
- `.claude/memory/MEMORIES.md` — add `Weather Service` and `Greeting Service` index entries.

## Open Questions / Future

1. **Per-user severe-alert fan-out.** v1 polls only the service-default `home_location` and notifies users whose `home_location` matches. A user with a per-user `home_location` different from the admin home does NOT get alerts for their location until per-user polling lands. Open question for the human: is the per-user polling SLA a v2 priority? (The cost is N polls/interval where N = unique configured user locations.)
2. **Admin-on-behalf prefs.** Should an admin be able to set another user's `home_location`? v1 says no (`/weather set_home` always writes the caller's own row). If yes in a follow-up PR, the natural shape is `set_user_home_location(user_id, query)` admin-only.
3. **Default location vs admin home terminology.** `home_location` = admin/service default. Per-user equivalent is `user_prefs.{user_id}.location`. Spec uses both terms but consistently — admin-home for service-default, user-home for per-user. Worth a final naming pass during implementation.
4. **Presence-derived location.** Designed-in but not wired up. Lights up when a presence backend grows lat/lon (e.g. a phone-GPS backend). Not blocking.
5. **Per-room location.** Some homes/offices span enough geography that "kitchen weather" vs "office weather" might genuinely differ — irrelevant today, deferred indefinitely.
6. **Backend fallback chain.** If we ever want "use Open-Meteo for forecast and NWS for alerts simultaneously", the current single-backend-on-the-service model will need to grow into the multi-backend aggregator pattern. The cache key already includes `backend_name`, so this can be added without a breaking interface change — the service just holds `dict[str, WeatherBackend]` (one per method) and routes per-method.
7. **Historical weather.** Open-Meteo has a separate Historical Weather API. Not in scope; could be a future tool (`weather_history`).
8. **Persistent cache for rate-limited backends.** If a future backend has rate limits AND Gilbert hot-installs trigger restarts, a plugin install would wipe the cache and re-hit the throttled API. Migrate cache to `entity_storage` when the first rate-limited backend lands.
9. **Multi-location digests.** Today the digest publishes one event for the service-default location. A future enhancement could publish per-user digests for each user with a configured location.
10. **`weather.digest` consumer in this PR.** None. The event exists for future "morning summary announcement" and proposals consumption. If a v1 consumer is wanted in this PR, add it explicitly.
11. **`/weather` no-subcommand muscle-memory shortcut.** Spec proposes registering both `slash_group=weather + slash_command=now` AND a top-level `slash_command=weather` alias for the same `current_weather` tool. Confirm the dispatcher accepts the duplicate-target during implementation.
12. **`ai_visible` flag on `ToolDefinition`.** If review prefers a different mechanism (e.g. registering write tools under a separate provider that doesn't declare `ai_tools`), pivot. The spec's preferred path is the named flag.
13. **DST behavior of digest.** Documented as "scheduler's underlying behavior — naive-local, no catch-up." If a tz-aware DAILY scheduler primitive lands, re-introduce `digest_timezone`.

## Related

- [Backend Pattern](../../.claude/memory/memory-backend-pattern.md) — universal ABC + registry pattern this feature follows.
- [Multi-backend Aggregator Pattern](../../.claude/memory/memory-multi-backend-pattern.md) — why one `WeatherService` holds N backends rather than registering N services.
- [Web Search Service](../../.claude/memory/memory-websearch-service.md) — closest existing analog.
- [Plugin System](../../.claude/memory/memory-plugin-system.md) — uv workspace, side-effect `setup()`, runtime install path.
- [Service System](../../.claude/memory/memory-service-system.md) — Service/ToolProvider/Configurable contracts.
- [Capability Protocols](../../.claude/memory/memory-capability-protocols.md) — `WeatherProvider` belongs in `interfaces/`.
- [AI Prompts Are Always Configurable](../../.claude/memory/memory-ai-prompts-configurable.md) — applies to `weather_hint_template` on `GreetingService` (the deterministic blurb interpolated into the AI greeting prompt).
- [Multi-User Isolation](../../.claude/memory/memory-multi-user-isolation.md) — singleton-safe state, `_user_id` injection, per-key locks.
- [Scheduler Service](../../.claude/memory/memory-scheduler-service.md) — system jobs (`weather.digest`, `weather.alerts.poll`) and `ActionStep` gating for "skip if rain forecast."
- [Event System](../../.claude/memory/memory-event-system.md) — `weather.digest`, `weather.alert.issued` event types.
- [Architecture Violation Checklist](../../.claude/memory/memory-architecture-checklist.md) — layer rules this spec is verified against.
- [Notification Service](../../.claude/memory/memory-notification-service.md) — used by the severe-alert delivery subscriber.
- [Slash Commands](../../.claude/memory/memory-slash-commands.md) — `slash_group="weather"` collapse + the AI-visible / slash-only distinction for `set_home_location` / `set_units`.
- [Storage Backend](../../.claude/memory/memory-storage-backend.md) — `StorageProvider.create_namespaced("gilbert.weather")` for prefs / dedup state.
- Open-Meteo Forecast API — https://open-meteo.com/en/docs
- Open-Meteo Geocoding API — https://open-meteo.com/en/docs/geocoding-api

## Revision Log — Round 2

This spec was revised based on three independent reviews:
`02-architect.md`, `02-product.md`, `02-engineering.md`.

### Blockers — addressed

- `[architect.blocker.1]` `home_location` config-vs-action-vs-yaml contradiction. Removed the row from the `ConfigParam` table; documented that `home_location` lives ONLY in entity storage at `gilbert.weather.service_state._id="home_location"`, set via the `home_location.set` ConfigAction. Removed it from the seeded YAML block.
- `[architect.blocker.2]` Plugin import paths for `ConfigParam` / `ToolParameterType`. Added explicit imports in the data-shapes intro and the Open-Meteo backend code block.
- `[architect.blocker.3]` `WeatherProvider` taking `user_id: str` instead of `UserContext`. Changed every protocol method's identity parameter to `user: UserContext | None = None`. Documented the explicit-over-ContextVar deliberate departure.
- `[architect.blocker.4]` Per-user prefs collection namespacing. Renamed from top-level `user_weather_prefs` to `user_prefs` *inside* the `gilbert.weather` storage namespace (full collection name `gilbert.weather.user_prefs`). Spelled out the ACL story (self-service writes only; no cross-user read or write in this PR).
- `[architect.blocker.5]` `narrate_ai_profile` and `choices_from="ai_profiles"`. Verified the constant exists in core (`inbox_ai_chat.py`, `roast.py`). Moot regardless because the entire `narrate` path was removed (see `product.blocker.1`).
- `[product.blocker.1]` `narrate=true` flag is a category mistake. Removed `narrate` parameter from all tools, removed `narrate_*_prompt` and `narrate_ai_profile` ConfigParams, replaced with deterministic `_render_*_summary` Python functions that produce a `summary` string in every tool response. No second AI hop.
- `[product.blocker.2]` Severe-alert delivery hand-waved. Added `Severe-alert delivery` section: `WeatherService` itself subscribes to `weather.alert.issued`, calls `NotificationProvider.notify_user(...)` with documented severity → urgency mapping (`EXTREME`/`SEVERE`→urgent, `MODERATE`→normal, `MINOR`→info), opt-in voice on `EXTREME` via `alert_voice_enabled`. Documented v1 limitation that polling uses only the service-default location.
- `[product.blocker.3]` Greeting hardcoded prompt-shaped string. Added `weather_hint_template` ConfigParam (multiline, `ai_prompt=False`), pulled location name from `current.location.name`, made unit suffixes derive from `current.units`, replaced `except Exception` with typed `LocationNotConfiguredError` / `WeatherUnavailableError` catches. Removed the "at the shop" hardcoding.
- `[swe.required.2.1]` Cache key omits backend_name. Added `backend_name` to the `_key()` signature.
- `[swe.required.2.2]` Cache lock dictionary leak. Replaced per-key `asyncio.Lock` dict with single-flight `asyncio.Future` idiom (no long-lived lock dict).
- `[swe.required.2.3]` No cache eviction. Added LRU eviction (`max_entries=2048`) using `collections.OrderedDict`.
- `[swe.required.2.4]` Per-user prefs storage path unspecified. Specified `provider.create_namespaced("gilbert.weather")` then `backend.put("user_prefs", user_id, doc)`. Typed `self._storage: NamespacedStorageBackend | None`.
- `[swe.required.2.5]` Per-user prefs caching on self forbidden. Added explicit "fresh `storage.get(...)` on every call" sentence under Location resolution.
- `[swe.required.2.6]` Daily digest timezone / idempotency / restart. Removed `digest_timezone` ConfigParam (server local timezone, full stop). Documented missed-digest-on-restart skip behavior. Documented constant job key + idempotent re-registration.
- `[swe.required.2.7]` Alert poll dedup never persists. Added `gilbert.weather.alert_dedup` collection + load-once-on-first-sweep semantics that explicitly avoid re-publishing currently-active alerts after restart. Added the unit test for it.
- `[swe.required.2.8]` Error path / structured errors / anti-fabrication. Added "Error contract" subsection with shapes for `no_home_location`, `weather_unavailable`, `geocoding_unavailable`, `invalid_arguments`, `no_results`. Stale-on-failure explicitly forbidden. Anti-fabrication addendum required on greeting prompt.
- `[swe.required.2.9]` `httpx.AsyncClient` lifecycle / timeouts. Switched to `httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)` and `httpx.Limits(max_connections=10, max_keepalive_connections=5)`.
- `[swe.required.2.10]` WMO weather code coverage. Added a complete code → `WeatherCondition` table covering 0, 1-3, 4 (smoke), 5 (haze), 45/48, 51/53/55, 56/57 (freezing drizzle), 61-65, 66/67 (freezing rain), 71-75, 77, 80-82, 85/86, 95, 96/99 (thunderstorm with hail). Added `FREEZING_DRIZZLE`, `FREEZING_RAIN`, `THUNDERSTORM_HAIL`, `SMOKE`, `HAZE`, `MIST`, `DUST` to the `WeatherCondition` enum.
- `[swe.required.2.11]` Open-Meteo rate-limit posture / acceptable use. Added "Rate-limit posture" paragraph with the documented limits (600/min, 5k/hour, 10k/day) and the commercial-use note. Updated default `User-Agent` to `Gilbert/1.0 (https://github.com/briandilley/gilbert)`.
- `[swe.required.2.12]` Geocoding failure modes. Added explicit "Geocoding cross-backend fallback" section specifying the at-start probe, registry walk, borrowed-instance lifecycle (`initialize({})` + `close()` in `stop()`), and `geocoding_unavailable` error path.
- `[swe.required.2.14]` `WeatherProvider` `user_id` ContextVar drift. Pinned Option A explicitly: protocol takes `UserContext`, single `_resolve_user(user)` helper does the ContextVar fallback; "optional parameter, falls back to ContextVar everywhere" pattern explicitly forbidden.
- `[swe.required.2.16]` `slash_namespace` for non-plugin services is wrong. Removed the recommendation to set `slash_namespace="weather"`; added a note explaining why core services should not.
- `[swe.required.2.18]` `forecast` hours+days mutual exclusivity. Added explicit error contract for the case (`{"error": "invalid_arguments"}`) plus 1–72 / 1–14 range validation in the handler.

### Important — addressed

- `[architect.important.1]` `WsHandlerProvider` capability claim with no handlers. Updated docstring to declare only `weather` + `ai_tools` capabilities.
- `[architect.important.2]` Cache lock leak. Same as `swe.required.2.2`.
- `[architect.important.3]` `_known_alert_ids` future-proofing. Pre-baked the shape as `dict[(location_key, scope_id), set[str]]` with `scope_id="system"` today.
- `[architect.important.4]` Geocoding cross-backend fallback under-specified. Same as `swe.required.2.12`.
- `[architect.important.5]` Cache key missing backend. Same as `swe.required.2.1`.
- `[architect.important.6]` `forecast` mutual-exclusivity. Same as `swe.required.2.18`.
- `[architect.important.7]` Settings stanza in `gilbert.yaml` duplicates plugin defaults. Moved `timeout_seconds` / `user_agent` to `std-plugins/open-meteo/plugin.yaml`'s `config:` section. Documented explicitly that the seeded entity-collection block is NOT a literal `gilbert.yaml` addition.
- `[architect.important.8]` `LocationNotConfiguredError`. Added typed exception class to `interfaces/weather.py`; documented contract on `WeatherProvider`.
- `[architect.important.9]` Greeting hardcoded prompt. Same as `product.blocker.3`.
- `[architect.important.10]` `parallel_safe=True` text contradiction. Reworded to "the four read tools are `parallel_safe=True`; the two write tools are `parallel_safe=False`."
- `[architect.important.11]` `tool_provider_name` vs `slash_namespace`. Fixed by removing the recommendation entirely (per `swe.required.2.16`).
- `[architect.important.12]` Multi-conversation digest delivery. Documented `weather.digest` as a fan-out broadcast event with no `conversation_id`.
- `[product.important.4]` Tool granularity / over-split. `set_home_location` / `set_units` are now slash-only via `ai_visible=False`.
- `[product.important.5]` Tool descriptions need to teach when to call. Rewrote all three weather tool descriptions in the "Use when the user asks about X" pattern; added the `weather_alerts` `supported=false` "no data, not no alerts" guidance.
- `[product.important.6]` Tool return shape. Specified the JSON shape (deterministic `summary` + structured fields) for every tool. Documented the `_render_*_summary` Python functions.
- `[product.important.7]` Caching trade-offs vs accuracy. Added `stale_seconds` field to all tool responses. Added soft-bypass on SEVERE/EXTREME alerts (cache invalidation for the affected location).
- `[product.important.8]` Profile inclusion. Documented "tools default to `tool_mode=all` so weather lands in `light`/`standard`/`advanced` automatically; greeting uses `tools_override=[]` so its own tool surface is unaffected."
- `[product.important.9]` Location resolution from AI's POV. Specified the `no_home_location` error shape and that the AI is encouraged to follow up with "what city should I use?" + ad-hoc `current_weather(location=…)`. Added `geocode_location` as an AI tool (was previously only a ConfigAction).
- `[product.important.10]` Slash-command coverage. Documented the `/weather` no-subcommand muscle-memory shortcut (Open Question 11) and explicitly that no top-level `/forecast` is provided.
- `[product.important.11]` `slash_namespace` claim. Same as `swe.required.2.16`.
- `[swe.observation, smaller]` `gilbert.yaml` is bootstrap-only. Reframed the bootstrap-config section to clarify it's the entity-collection seed, not a literal YAML file change.
- `[swe.observation, smaller]` Greeting "the shop". Removed.
- `[swe.observation, smaller]` Greeting `except Exception`. Replaced with typed catches.
- `[swe.observation, smaller]` Layer compliance test. Documented consolidating into the existing test rather than adding a feature-specific one.
- `[swe.observation, smaller]` `memory-greeting-service.md` hedge. Resolved to "create it fresh" in the documentation list.
- `[swe.observation, smaller]` `AlertSeverity` ↔ CAP. Added docstring noting CAP §3.2.1.7 lineage and the OpenWeather translation requirement.

### Nits — addressed

- `[architect.nit.1]` `config_namespace` / `config_category` as class attributes. Switched to `@property` accessors matching `WebSearchService` / `PresenceService`.
- `[architect.nit.2]` `config_category="Monitoring"` choice. Switched to `"Intelligence"` matching `WebSearchService` (weather is more narrative-AI than passive-monitoring).
- `[architect.nit.3]` `enum=["metric","imperial"]` vs `choices=`. Verified against `interfaces/tools.py`: the field IS `enum: list[str] | None`. Architect was incorrect here; spec retains `enum=...`.
- `[architect.nit.4]` Strengthen narrate-prompt unit instruction. Moot — narrate path removed.
- `[architect.nit.5]` Multi-module conftest. Updated to "copy from `unifi/tests/conftest.py`" not `tavily/tests/conftest.py`.
- `[architect.nit.6]` Layer-compliance redundancy. Same as `swe.observation`.
- `[architect.nit.7]` `weather.py` line count. Extracted `_WeatherCache` to `_weather_cache.py` private module.
- `[architect.nit.8]` `WMO_CODE_MAP` location. Confirmed plugin-internal; tests cover `UNKNOWN` for unknown codes.
- `[product.nit.12]` Narrate prompts voiceless. Moot.
- `[product.nit.13]` `WeatherCondition` enum gaps. Added `SMOKE`, `HAZE`, `MIST`, `DUST`, `FREEZING_DRIZZLE`, `FREEZING_RAIN`, `THUNDERSTORM_HAIL`.
- `[product.nit.14]` `description` field semantics. Documented per-backend behavior (empty for Open-Meteo; `properties.shortForecast` for NWS).
- `[product.nit.15]` Parallel-tool-use hint silent on weather. Documented in the tool surface section that all read tools are `parallel_safe=True`.
- `[product.nit.16]` Daily digest consumers. Acknowledged as Open Question 10.
- `[product.nit.17]` Digest hourly+daily overlap. Switched defaults to `digest_horizon_hours=12` (rest-of-today only) and `digest_horizon_days=3` with explicit "drop today's redundant daily slice" logic.
- `[product.nit.18]` Fixture privacy. Documented Cleveland coords as the canonical fixture lat/lon.
- `[product.nit.19]` Open-Meteo attribution. Documented Settings card attribution + `std-plugins/README.md` per-plugin section.
- `[swe.smaller, AlertSeverity ↔ CAP]` Added docstring.
- `[swe.smaller, units cache]` Documented the 2x-cache-entries-per-location trade-off (acceptable; no change).

### Deferred / not addressed

- `[product.open.5]` DST behavior of digest. Documented as "scheduler's underlying behavior — naive-local, no catch-up." Will revisit if the scheduler grows tz-aware DAILY support. Tracked in Open Question 13.
- `[product.open.6]` System-prompt nudge for `weather_alerts` on safety-relevant questions. Out of scope — that's a soul/identity-style hint and belongs in a separate prompt iteration. Noted but not added.
- `[architect.open.1]` Per-user storage authority (admin-on-behalf prefs). Deferred to follow-up PR. Tracked in Open Question 2.
- `[architect.open.2]` Default-location vs admin-home terminology consistency. Deferred to a final naming pass at implementation. Tracked in Open Question 3.
- `[architect.open.4]` `WeatherProvider` file split. Deferred — keep one file until it grows past ~500 lines.
- `[architect.open.5]` `weather.digest` payload size cap. Implemented (hard-capped at 50 hourly + 7 daily slices regardless of config).
- `[architect.open.6]` Persistent cache for rate-limited backends. Tracked in Open Question 8.
- `[swe.required.2.13, integration tests]` Live API integration test. Documented as opt-in via `pytest -m integration`. Skipped by default; not added to this PR's CI.
- `[swe.required.2.15]` `_known_alert_ids` multi-user audit comment. Added the "owned by exactly one coroutine" sentence under Caller identity.
- `[swe.required.2.17]` `ToolParameter` range constraints. Documented validation in the service handler with structured errors. (If `ToolParameter` is later extended with `min`/`max`, this section can be slimmed — not blocking.)
- `[swe.smaller, ConfigParam OBJECT support]` Verified during implementation; spec removes `home_location` from the ConfigParam table entirely so this is moot.
- `[product.open.3]` `geocode()` multi-candidate handling. Currently — both ConfigAction (UI form select) and tool (returns full list and asks the AI to pick). Documented in the geocode_location tool description ("disambiguate"). Not deferred per se; resolved in the spec as written.
- `[product.open.4]` `_known_alert_ids` lifetime / restart spam. Resolved by `swe.required.2.7`.

