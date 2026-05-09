"""Weather interface — backend ABC, data shapes, and the WeatherProvider capability protocol.

This module defines:

- The shared dataclasses (``CurrentWeather``, ``HourlyForecast``,
  ``DailyForecast``, ``WeatherAlert``, ``GeoLocation``) and their
  associated enums (``WeatherCondition``, ``AlertSeverity``,
  ``WeatherUnits``).
- The ``WeatherBackend`` ABC that concrete providers (Open-Meteo, NWS,
  OpenWeatherMap) implement, with the universal ``__init_subclass__``
  registry pattern.
- The ``WeatherBackendCapabilities`` discriminator advertising which
  methods a backend implements meaningfully (e.g. Open-Meteo doesn't
  issue alerts; NWS does).
- The ``WeatherProvider`` capability protocol that in-process consumers
  (greeting service, scheduler, proposals) call into without coupling
  to the concrete service class.
- Typed errors ``LocationNotConfiguredError`` and
  ``WeatherUnavailableError`` that propagate cleanly through the
  service / tool layer.

Imports stay strictly inside ``gilbert.interfaces.*`` per the layer
rules.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Any, Protocol, runtime_checkable

from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import ConfigParam


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

    def __init__(
        self,
        message: str,
        *,
        provider_status: int | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.provider_status = provider_status
        self.retryable = retryable


class WeatherUnits(StrEnum):
    """Caller-facing unit system. Backends may translate internally."""

    METRIC = "metric"      # °C, m/s or km/h, mm, hPa, km
    IMPERIAL = "imperial"  # °F, mph, in, hPa, mi


class WeatherCondition(StrEnum):
    """Coarse provider-neutral condition tag.

    Backends map their own codes onto this enum so consumers (greeting
    prompts, scheduler rules, proposals) don't have to know each
    backend's lookup table.
    """

    CLEAR = "clear"
    PARTLY_CLOUDY = "partly_cloudy"
    CLOUDY = "cloudy"
    FOG = "fog"
    MIST = "mist"
    DRIZZLE = "drizzle"
    FREEZING_DRIZZLE = "freezing_drizzle"  # WMO 56/57 — distinct hazard for road safety
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

    NOTE: ``StrEnum`` orderings are lexicographic — comparing with
    ``>=`` against another severity value compares strings, not
    semantic severity. Use :func:`severity_rank` for ordering checks.
    """

    MINOR = "minor"
    MODERATE = "moderate"
    SEVERE = "severe"
    EXTREME = "extreme"


class _SeverityRank(IntEnum):
    """Internal numeric ordering for ``AlertSeverity``.

    Avoids the lexicographic-compare bug where ``"severe" > "extreme"``
    is True alphabetically.
    """

    MINOR = 1
    MODERATE = 2
    SEVERE = 3
    EXTREME = 4


_SEVERITY_RANK: dict[AlertSeverity, int] = {
    AlertSeverity.MINOR: _SeverityRank.MINOR.value,
    AlertSeverity.MODERATE: _SeverityRank.MODERATE.value,
    AlertSeverity.SEVERE: _SeverityRank.SEVERE.value,
    AlertSeverity.EXTREME: _SeverityRank.EXTREME.value,
}


def severity_rank(severity: AlertSeverity) -> int:
    """Return a numeric rank for an ``AlertSeverity`` for ordering checks.

    Higher = more severe. Use this any time you compare severities
    with ``>=`` / ``<`` etc. — comparing the string values directly
    is a lexicographic-compare bug.
    """
    return _SEVERITY_RANK[severity]


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
    description: str = ""           # provider phrase; empty for backends that don't return one
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

    The base ``WeatherBackend`` declares all four methods (``alerts`` is
    a default-impl no-op so it isn't abstract), but a backend can
    override ``capabilities()`` to advertise that (e.g.) ``alerts()``
    will always return ``[]``. Consumers branch on these flags to
    decide whether to query the backend at all.
    """

    current: bool = True
    hourly: bool = True
    daily: bool = True
    alerts: bool = False


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
        """Return up to *hours* hour-by-hour forecast slices, ascending by ``valid_at``."""

    @abstractmethod
    async def forecast_daily(
        self,
        location: GeoLocation,
        *,
        days: int = 7,
        units: WeatherUnits = WeatherUnits.METRIC,
    ) -> list[DailyForecast]:
        """Return up to *days* daily summaries, starting today, ascending by ``date``."""

    async def alerts(
        self,
        location: GeoLocation,
    ) -> list[WeatherAlert]:
        """Return active severe-weather alerts for a location.

        Default returns ``[]`` for backends that don't issue warnings
        (e.g. Open-Meteo). Backends that *do* must override and flip
        ``capabilities().alerts`` to ``True``.
        """
        return []

    async def geocode(self, query: str, *, count: int = 5) -> list[GeoLocation]:
        """Resolve a place-name query to candidate locations.

        Default raises ``NotImplementedError``. Backends with a free
        geocoding endpoint (Open-Meteo, OpenWeather) override; backends
        without one (NWS) leave it raising and the service falls back
        to another backend's geocoder via the registry.
        """
        raise NotImplementedError(
            f"{self.backend_name or 'backend'} does not provide geocoding."
        )


@runtime_checkable
class WeatherProvider(Protocol):
    """Capability protocol exposed by ``WeatherService``.

    Other services must use this protocol via
    ``isinstance(svc, WeatherProvider)`` after
    ``resolver.get_capability("weather")`` — never an ``isinstance``
    check against the concrete ``WeatherService`` class.

    All ``get_*`` methods raise :class:`LocationNotConfiguredError`
    when ``location`` is ``None`` AND no user / service-default
    location can be resolved. They raise :class:`WeatherUnavailableError`
    when the backend HTTP call fails. Both are catchable typed errors —
    never let raw ``httpx`` exceptions escape these methods.

    Identity is passed explicitly via ``user`` (as a full
    ``UserContext``, not a bare ``user_id`` string) so callers from
    background jobs (greeting tasks, scheduled actions) can pass
    identity in without relying on a ContextVar that may not be set.
    Inside ``WeatherService``, a single helper ``_resolve_user(user)``
    is the only place that may fall back to
    ``gilbert.core.context.get_current_user()`` when ``user is None``.
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

    async def resolve_location(self, user: UserContext | None) -> GeoLocation | None: ...

    async def resolve_units(self, user: UserContext | None) -> WeatherUnits: ...

