"""Weather service — multi-backend weather aggregator.

Holds one ``WeatherBackend`` (default ``open-meteo``), exposes AI tools
(``current_weather``, ``forecast``, ``weather_alerts``,
``geocode_location``) and slash-only configuration tools
(``set_home_location``, ``set_units``). Implements the
``WeatherProvider`` capability protocol so other services (greeting,
scheduler, proposals) can pull weather without touching the concrete
class.

Key design points:

- **Single-flight + LRU cache** with separate TTLs per method.
  Two AI turns asking for the same location at the same time result
  in **one** backend HTTP request.
- **Per-user prefs** stored under the ``gilbert.weather`` namespace
  (``user_prefs`` and ``service_state`` collections). Resolved fresh
  on every call — never cached on ``self``.
- **Persistent severe-alert dedup** in
  ``gilbert.weather.alert_dedup``. The first poll sweep after a
  restart treats currently-active alerts as already-seen so restarts
  don't re-spam subscribers.
- **Geocoding cross-backend fallback** — when the active backend
  raises ``NotImplementedError`` from ``geocode()`` (e.g. NWS), the
  service walks the registry and instantiates the first backend whose
  ``geocode`` method is overridden.
- **Severity ordering uses a numeric rank** (not lexicographic
  StrEnum compare) — see ``severity_rank`` in
  ``gilbert.interfaces.weather``.
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gilbert.interfaces.context import get_current_user
from gilbert.core.services._backend_actions import (
    all_backend_actions,
    invoke_backend_action,
)
from gilbert.core.services._weather_cache import WeatherCache
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.events import Event, EventBus, EventBusProvider
from gilbert.interfaces.greeting import GreetingContext
from gilbert.interfaces.notifications import (
    NotificationProvider,
    NotificationUrgency,
)
from gilbert.interfaces.scheduler import Schedule, SchedulerProvider
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.speaker import SpeakerProvider
from gilbert.interfaces.storage import (
    NamespacedStorageBackend,
    StorageProvider,
)
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
    ToolProvider,
)
from gilbert.interfaces.weather import (
    AlertSeverity,
    CurrentWeather,
    DailyForecast,
    GeoLocation,
    HourlyForecast,
    LocationNotConfiguredError,
    WeatherAlert,
    WeatherBackend,
    WeatherCondition,
    WeatherUnavailableError,
    WeatherUnits,
    severity_rank,
)

logger = logging.getLogger(__name__)


_USER_PREFS_COLLECTION = "user_prefs"
_SERVICE_STATE_COLLECTION = "service_state"
_ALERT_DEDUP_COLLECTION = "alert_dedup"
_HOME_LOCATION_ID = "home_location"
_DEDUP_TTL_DAYS = 7


# ── Default tool descriptions (deterministic; no AI prompts) ──────────


_CURRENT_TOOL_DESCRIPTION = (
    "Get the current weather. Call this when the user asks about *now* "
    "— temperature, whether it's raining, how it feels outside, what "
    "to wear today. Returns temperature, conditions, wind, humidity, "
    "and a one-sentence summary. The caller's configured location is "
    "used unless `location` is given. If no location is configured "
    "anywhere, the response is a structured error you should surface "
    "to the user — offer to set their home with /weather set_home."
)

_FORECAST_TOOL_DESCRIPTION = (
    "Get a weather forecast. Call this when the user asks about *later "
    "today*, *tomorrow*, *this week*, or any future window. Use `hours` "
    "for short windows (next few hours, today) or `days` for longer "
    "(weekly outlook). Don't use for current conditions — call "
    "`current_weather` instead. Specify `hours` OR `days`, not both. "
    "If neither is given, defaults to `hours=24`."
)

_ALERTS_TOOL_DESCRIPTION = (
    "Get active severe-weather alerts (warnings, watches, advisories) "
    "for a location. Call this when the user asks 'any storms?' / "
    "'is there a warning out?' / before answering questions about "
    "outdoor safety. The response carries `supported: true|false` — "
    "`supported=false` means the configured backend doesn't issue "
    "alerts (e.g. Open-Meteo) and that's NOT the same as 'no alerts.' "
    "If the user is asking about safety and `supported=false`, mention "
    "the limitation rather than implying they're in the clear."
)

_GEOCODE_TOOL_DESCRIPTION = (
    "Resolve a place-name query to candidate lat/lon coordinates. "
    "Call this when the user mentions a place you don't have "
    "coordinates for and you need to disambiguate (e.g. 'weather in "
    "Springfield' returns multiple hits). Returns a list of "
    "candidates; pick one and pass it back to `current_weather` / "
    "`forecast` as `lat,lon` to skip a second geocoding round-trip."
)

_DEFAULT_WEATHER_HINT_TEMPLATE = (
    "Current weather at {location_name}: {temperature:.0f}{temp_suffix} "
    "{condition_phrase}, wind {wind_speed:.0f}{speed_suffix}"
    "{feels_like_clause}. Mention it casually if it fits the moment, "
    "otherwise ignore. Quote only the values shown — never invent additional "
    "weather details."
)


# ── Condition phrase translation ──────────────────────────────────────


_CONDITION_PHRASES: dict[WeatherCondition, str] = {
    WeatherCondition.CLEAR: "clear",
    WeatherCondition.PARTLY_CLOUDY: "partly cloudy",
    WeatherCondition.CLOUDY: "cloudy",
    WeatherCondition.FOG: "foggy",
    WeatherCondition.MIST: "misty",
    WeatherCondition.DRIZZLE: "drizzling",
    WeatherCondition.FREEZING_DRIZZLE: "freezing drizzle",
    WeatherCondition.RAIN: "raining",
    WeatherCondition.HEAVY_RAIN: "heavy rain",
    WeatherCondition.FREEZING_RAIN: "freezing rain",
    WeatherCondition.SNOW: "snowing",
    WeatherCondition.HEAVY_SNOW: "heavy snow",
    WeatherCondition.SLEET: "sleet",
    WeatherCondition.HAIL: "hail",
    WeatherCondition.THUNDERSTORM: "thunderstorms",
    WeatherCondition.THUNDERSTORM_HAIL: "thunderstorms with hail",
    WeatherCondition.SMOKE: "smoky",
    WeatherCondition.HAZE: "hazy",
    WeatherCondition.DUST: "dusty",
    WeatherCondition.UNKNOWN: "unknown conditions",
}


def _temp_suffix(units: WeatherUnits) -> str:
    return "°F" if units is WeatherUnits.IMPERIAL else "°C"


def _speed_suffix(units: WeatherUnits) -> str:
    return "mph" if units is WeatherUnits.IMPERIAL else "km/h"


def _precip_suffix(units: WeatherUnits) -> str:
    return "in" if units is WeatherUnits.IMPERIAL else "mm"


def _condition_phrase(condition: WeatherCondition) -> str:
    return _CONDITION_PHRASES.get(condition, "unknown conditions")


# ── Deterministic summary renderers ───────────────────────────────────


def _render_current_summary(cw: CurrentWeather) -> str:
    """Render a single-sentence English summary of current conditions."""
    parts: list[str] = []
    temp_s = _temp_suffix(cw.units)
    speed_s = _speed_suffix(cw.units)
    location_str = cw.location.name or "the current location"
    parts.append(
        f"Currently {cw.temperature:.0f}{temp_s} and "
        f"{_condition_phrase(cw.condition)} in {location_str}."
    )
    wind_clause = f"Wind {cw.wind_speed:.0f}{speed_s}"
    if cw.wind_gust is not None and cw.wind_gust > cw.wind_speed:
        wind_clause += f" (gusts to {cw.wind_gust:.0f}{speed_s})"
    parts.append(wind_clause + ".")
    if (
        cw.feels_like is not None
        and abs(cw.feels_like - cw.temperature) >= 3
    ):
        parts.append(f"Feels like {cw.feels_like:.0f}{temp_s}.")
    return " ".join(parts)


def _render_hourly_summary(items: list[HourlyForecast]) -> str:
    """Render a short summary of an hourly forecast list."""
    if not items:
        return "No forecast data available."
    first = items[0]
    last = items[-1]
    temp_s = _temp_suffix(first.units)
    precip_s = _precip_suffix(first.units)
    total_precip = sum(item.precipitation for item in items)
    high = max(item.temperature for item in items)
    low = min(item.temperature for item in items)
    location_str = first.location.name or "the configured location"
    summary = (
        f"Forecast for {location_str} from "
        f"{first.valid_at.strftime('%a %H:%M')} to "
        f"{last.valid_at.strftime('%a %H:%M')}: "
        f"{low:.0f}–{high:.0f}{temp_s}"
    )
    if total_precip > 0:
        summary += f", {total_precip:.1f}{precip_s} of precipitation expected"
    summary += "."
    return summary


def _render_daily_summary(items: list[DailyForecast]) -> str:
    """Render a short summary of a daily forecast list."""
    if not items:
        return "No forecast data available."
    first = items[0]
    last = items[-1]
    temp_s = _temp_suffix(first.units)
    location_str = first.location.name or "the configured location"
    return (
        f"Daily forecast for {location_str} from {first.date} to {last.date}: "
        f"highs {min(d.temperature_high for d in items):.0f}"
        f"–{max(d.temperature_high for d in items):.0f}{temp_s}, "
        f"lows {min(d.temperature_low for d in items):.0f}"
        f"–{max(d.temperature_low for d in items):.0f}{temp_s}."
    )


def _render_alerts_summary(alerts: list[WeatherAlert], supported: bool) -> str:
    if not supported:
        return "Severe-weather alerts are not supported by the configured backend."
    if not alerts:
        return "No active alerts."
    by_sev: dict[AlertSeverity, int] = {}
    for a in alerts:
        by_sev[a.severity] = by_sev.get(a.severity, 0) + 1
    parts: list[str] = []
    for sev in (
        AlertSeverity.EXTREME,
        AlertSeverity.SEVERE,
        AlertSeverity.MODERATE,
        AlertSeverity.MINOR,
    ):
        if sev in by_sev:
            parts.append(f"{by_sev[sev]} {sev.value}")
    return f"Active alerts: {', '.join(parts)}."


# ── Dict serialisation helpers ────────────────────────────────────────


def _location_to_dict(loc: GeoLocation) -> dict[str, Any]:
    return {
        "latitude": loc.latitude,
        "longitude": loc.longitude,
        "name": loc.name,
        "timezone": loc.timezone,
        "country_code": loc.country_code,
    }


def _location_from_dict(data: dict[str, Any] | None) -> GeoLocation | None:
    if not data:
        return None
    try:
        return GeoLocation(
            latitude=float(data.get("latitude", 0.0)),
            longitude=float(data.get("longitude", 0.0)),
            name=str(data.get("name", "")),
            timezone=str(data.get("timezone", "UTC")),
            country_code=str(data.get("country_code", "")),
        )
    except (TypeError, ValueError):
        return None


def _current_to_dict(cw: CurrentWeather, *, source: str, stale_seconds: float) -> dict[str, Any]:
    return {
        "summary": _render_current_summary(cw),
        "temperature": cw.temperature,
        "feels_like": cw.feels_like,
        "condition": cw.condition.value,
        "description": cw.description,
        "humidity_pct": cw.humidity_pct,
        "wind_speed": cw.wind_speed,
        "wind_gust": cw.wind_gust,
        "wind_direction_deg": cw.wind_direction_deg,
        "pressure_hpa": cw.pressure_hpa,
        "precipitation_last_hour": cw.precipitation_last_hour,
        "cloud_cover_pct": cw.cloud_cover_pct,
        "units": cw.units.value,
        "observed_at": cw.observed_at.isoformat(),
        "location": _location_to_dict(cw.location),
        "stale_seconds": round(stale_seconds, 1),
        "source": source,
    }


def _hourly_to_dict(item: HourlyForecast) -> dict[str, Any]:
    return {
        "valid_at": item.valid_at.isoformat(),
        "temperature": item.temperature,
        "feels_like": item.feels_like,
        "precipitation": item.precipitation,
        "precipitation_probability_pct": item.precipitation_probability_pct,
        "wind_speed": item.wind_speed,
        "wind_gust": item.wind_gust,
        "wind_direction_deg": item.wind_direction_deg,
        "cloud_cover_pct": item.cloud_cover_pct,
        "condition": item.condition.value,
        "units": item.units.value,
    }


def _daily_to_dict(item: DailyForecast) -> dict[str, Any]:
    return {
        "date": item.date,
        "temperature_high": item.temperature_high,
        "temperature_low": item.temperature_low,
        "precipitation": item.precipitation,
        "precipitation_probability_pct": item.precipitation_probability_pct,
        "wind_speed_max": item.wind_speed_max,
        "wind_gust_max": item.wind_gust_max,
        "sunrise": item.sunrise.isoformat() if item.sunrise else None,
        "sunset": item.sunset.isoformat() if item.sunset else None,
        "condition": item.condition.value,
        "units": item.units.value,
    }


def _alert_to_dict(alert: WeatherAlert) -> dict[str, Any]:
    return {
        "alert_id": alert.alert_id,
        "title": alert.title,
        "description": alert.description,
        "severity": alert.severity.value,
        "issued_at": alert.issued_at.isoformat(),
        "expires_at": alert.expires_at.isoformat() if alert.expires_at else None,
        "affected_area": alert.affected_area,
        "source": alert.source,
        "url": alert.url,
    }


def _today_iso(timezone: str) -> str:
    """Today's ISO date in the given IANA timezone (or UTC fallback)."""
    tz: Any
    try:
        tz = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        tz = UTC
    return datetime.now(tz).date().isoformat()


# ── Service ───────────────────────────────────────────────────────────


class WeatherService(Service, ToolProvider):
    """Single-backend weather aggregator. Service + ToolProvider.

    Capabilities: ``weather``, ``ai_tools``.

    No ``ws_handlers`` — the service exposes ConfigActions and slash
    commands, both of which reuse the standard ``config.action.*`` and
    ``chat.*`` RPCs. If a future iteration adds a dedicated WS RPC
    (e.g. ``weather.location.suggest`` for typeahead) we'll add the
    capability alongside; we don't advertise it before then.
    """

    def __init__(self) -> None:
        self._enabled: bool = False
        self._backend: WeatherBackend | None = None
        self._geocoder_backend: WeatherBackend | None = None
        self._geocoder_is_active_backend: bool = False
        self._backend_name: str = "open-meteo"
        self._settings: dict[str, Any] = {}
        self._resolver: ServiceResolver | None = None
        self._event_bus: EventBus | None = None
        self._notifications: NotificationProvider | None = None
        self._scheduler: SchedulerProvider | None = None
        self._storage: NamespacedStorageBackend | None = None

        # Service defaults (overridable per-user)
        self._default_units: WeatherUnits = WeatherUnits.METRIC

        # Cache + TTLs
        self._cache: WeatherCache = WeatherCache(max_entries=2048)
        self._cache_ttl_current_s: int = 600           # 10 min
        self._cache_ttl_hourly_s: int = 1800           # 30 min
        self._cache_ttl_daily_s: int = 3600            # 1 h
        self._cache_ttl_alerts_s: int = 300            # 5 min

        # Daily digest job
        self._digest_enabled: bool = False
        self._digest_hour: int = 7
        self._digest_minute: int = 0
        self._digest_horizon_hours: int = 12
        self._digest_horizon_days: int = 3

        # Alert poll job state
        self._alert_poll_seconds: int = 300
        # Keyed by (location_key, scope_id). scope_id="system" today.
        self._known_alert_ids: dict[tuple[str, str], set[str]] = {}
        self._alert_dedup_loaded: bool = False
        # First-sweep-after-restart suppression: on the very first
        # _poll_alerts() call after start(), every currently-active
        # alert is treated as already-seen and persisted without firing
        # a `weather.alert.issued` event. Prevents notification spam
        # when Gilbert was down during an active alert window
        # (cold-boot, post-crash, or fresh install).
        self._first_sweep_done: bool = False
        self._alert_unsubscribe: Any = None

        # Severity → notification urgency map
        self._alert_urgency: dict[AlertSeverity, NotificationUrgency] = {
            AlertSeverity.MINOR: NotificationUrgency.INFO,
            AlertSeverity.MODERATE: NotificationUrgency.NORMAL,
            AlertSeverity.SEVERE: NotificationUrgency.URGENT,
            AlertSeverity.EXTREME: NotificationUrgency.URGENT,
        }
        self._alert_voice_minimum: AlertSeverity = AlertSeverity.EXTREME
        self._alert_voice_enabled: bool = False

        # Greeting context provider
        self._weather_hint_template: str = _DEFAULT_WEATHER_HINT_TEMPLATE

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="weather",
            capabilities=frozenset({"weather", "ai_tools", "greeting_context"}),
            requires=frozenset({"entity_storage"}),
            optional=frozenset(
                {"event_bus", "scheduler", "notifications", "speaker_control",
                 "configuration"}
            ),
            events=frozenset({"weather.alert.issued", "weather.digest"}),
            toggleable=True,
            toggle_description="Weather AI tools and severe-alert delivery",
        )

    # ── Configurable ─────────────────────────────────────────────────

    @property
    def config_namespace(self) -> str:
        return "weather"

    @property
    def config_category(self) -> str:
        return "Intelligence"

    def config_params(self) -> list[ConfigParam]:
        params: list[ConfigParam] = [
            ConfigParam(
                key="enabled",
                type=ToolParameterType.BOOLEAN,
                description=(
                    "Master toggle. Defaults to off because the service "
                    "needs a configured `home_location` (set via the "
                    "Weather Settings page action) before any tool will "
                    "succeed."
                ),
                default=False,
                restart_required=True,
            ),
            ConfigParam(
                key="backend",
                type=ToolParameterType.STRING,
                description="Weather backend provider.",
                default="open-meteo",
                restart_required=True,
                choices=tuple(WeatherBackend.registered_backends().keys()) or None,
            ),
            ConfigParam(
                key="default_units",
                type=ToolParameterType.STRING,
                description="Default unit system. Per-user override available.",
                default="metric",
                choices=("metric", "imperial"),
            ),
            ConfigParam(
                key="cache_ttl_current_seconds",
                type=ToolParameterType.INTEGER,
                description="TTL for current-conditions cache.",
                default=600,
            ),
            ConfigParam(
                key="cache_ttl_hourly_seconds",
                type=ToolParameterType.INTEGER,
                description="TTL for hourly-forecast cache.",
                default=1800,
            ),
            ConfigParam(
                key="cache_ttl_daily_seconds",
                type=ToolParameterType.INTEGER,
                description="TTL for daily-forecast cache.",
                default=3600,
            ),
            ConfigParam(
                key="cache_ttl_alerts_seconds",
                type=ToolParameterType.INTEGER,
                description="TTL for alerts cache.",
                default=300,
            ),
            ConfigParam(
                key="digest_enabled",
                type=ToolParameterType.BOOLEAN,
                description="Publish a daily `weather.digest` event.",
                default=False,
            ),
            ConfigParam(
                key="digest_hour",
                type=ToolParameterType.INTEGER,
                description="Server-local hour (0–23) to publish digest.",
                default=7,
            ),
            ConfigParam(
                key="digest_minute",
                type=ToolParameterType.INTEGER,
                description="Server-local minute (0–59) to publish digest.",
                default=0,
            ),
            ConfigParam(
                key="digest_horizon_hours",
                type=ToolParameterType.INTEGER,
                description="Hourly slices in the digest payload (capped).",
                default=12,
            ),
            ConfigParam(
                key="digest_horizon_days",
                type=ToolParameterType.INTEGER,
                description="Daily slices in the digest payload.",
                default=3,
            ),
            ConfigParam(
                key="alert_poll_seconds",
                type=ToolParameterType.INTEGER,
                description=(
                    "Interval for alert polling. Has no effect if the "
                    "configured backend doesn't issue alerts."
                ),
                default=300,
            ),
            ConfigParam(
                key="alert_voice_enabled",
                type=ToolParameterType.BOOLEAN,
                description=(
                    "Whether the service should call the speaker service "
                    "to announce high-severity alerts."
                ),
                default=False,
            ),
            ConfigParam(
                key="alert_voice_minimum",
                type=ToolParameterType.STRING,
                description="Minimum severity for voice announcement.",
                default="extreme",
                choices=("severe", "extreme"),
            ),
            ConfigParam(
                key="weather_hint_template",
                type=ToolParameterType.STRING,
                description=(
                    "Prose template inserted into the greeting context block "
                    "when this provider is enabled. Placeholders: {location_name}, "
                    "{temperature}, {temp_suffix}, {condition_phrase}, "
                    "{wind_speed}, {speed_suffix}, {feels_like_clause}."
                ),
                default=_DEFAULT_WEATHER_HINT_TEMPLATE,
                multiline=True,
                ai_prompt=True,
            ),
        ]
        # Backend-specific config (timeout_seconds, user_agent for Open-Meteo, etc.)
        backends = WeatherBackend.registered_backends()
        backend_cls = backends.get(self._backend_name)
        if backend_cls is not None:
            for bp in backend_cls.backend_config_params():
                params.append(
                    ConfigParam(
                        key=f"settings.{bp.key}",
                        type=bp.type,
                        description=bp.description,
                        default=bp.default,
                        restart_required=bp.restart_required,
                        sensitive=bp.sensitive,
                        choices=bp.choices,
                        choices_from=bp.choices_from,
                        multiline=bp.multiline,
                        ai_prompt=bp.ai_prompt,
                        backend_param=True,
                    )
                )
        return params

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._apply_config(config)

    def _apply_config(self, section: dict[str, Any]) -> None:
        """Apply a config section to in-memory state."""
        self._enabled = bool(section.get("enabled", False))
        self._backend_name = str(section.get("backend", self._backend_name))
        self._settings = section.get("settings", self._settings) or {}
        try:
            self._default_units = WeatherUnits(
                str(section.get("default_units", self._default_units.value))
            )
        except ValueError:
            self._default_units = WeatherUnits.METRIC
        self._cache_ttl_current_s = int(
            section.get("cache_ttl_current_seconds", self._cache_ttl_current_s)
        )
        self._cache_ttl_hourly_s = int(
            section.get("cache_ttl_hourly_seconds", self._cache_ttl_hourly_s)
        )
        self._cache_ttl_daily_s = int(
            section.get("cache_ttl_daily_seconds", self._cache_ttl_daily_s)
        )
        self._cache_ttl_alerts_s = int(
            section.get("cache_ttl_alerts_seconds", self._cache_ttl_alerts_s)
        )
        self._digest_enabled = bool(section.get("digest_enabled", self._digest_enabled))
        self._digest_hour = int(section.get("digest_hour", self._digest_hour))
        self._digest_minute = int(section.get("digest_minute", self._digest_minute))
        self._digest_horizon_hours = int(
            section.get("digest_horizon_hours", self._digest_horizon_hours)
        )
        self._digest_horizon_days = int(
            section.get("digest_horizon_days", self._digest_horizon_days)
        )
        self._alert_poll_seconds = int(
            section.get("alert_poll_seconds", self._alert_poll_seconds)
        )
        self._alert_voice_enabled = bool(
            section.get("alert_voice_enabled", self._alert_voice_enabled)
        )
        try:
            self._alert_voice_minimum = AlertSeverity(
                str(section.get("alert_voice_minimum", self._alert_voice_minimum.value))
            )
        except ValueError:
            self._alert_voice_minimum = AlertSeverity.EXTREME
        if "weather_hint_template" in section:
            self._weather_hint_template = (
                section["weather_hint_template"] or _DEFAULT_WEATHER_HINT_TEMPLATE
            )

    # ── ConfigActionProvider ─────────────────────────────────────────

    def config_actions(self) -> list[ConfigAction]:
        actions: list[ConfigAction] = [
            ConfigAction(
                key="home_location.set",
                label="Set home location",
                description=(
                    "Look up a place name and pick a candidate to use as "
                    "the service-default home location."
                ),
            ),
            ConfigAction(
                key="home_location.pick",
                label="Pick candidate",
                description="Internal — used by the home_location.set follow-up form.",
                hidden=True,
            ),
        ]
        actions.extend(
            all_backend_actions(
                registry=WeatherBackend.registered_backends(),
                current_backend=self._backend,
            )
        )
        return actions

    async def invoke_config_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        if key == "home_location.set":
            return await self._action_home_location_set(payload)
        if key == "home_location.pick":
            return await self._action_home_location_pick(payload)
        return await invoke_backend_action(self._backend, key, payload)

    async def _action_home_location_set(
        self,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        query = str(payload.get("query", "")).strip()
        if not query:
            return ConfigActionResult(
                status="pending",
                message="Enter a place name to look up.",
                followup_action="home_location.set",
                data={
                    "form": {
                        "fields": [
                            {
                                "name": "query",
                                "label": "Place",
                                "type": "string",
                                "required": True,
                            },
                        ],
                    },
                },
            )
        candidates = await self._geocode(query, count=5)
        if not candidates:
            return ConfigActionResult(
                status="error",
                message=f"No matching place found for '{query}'.",
            )
        choices = [
            {
                "value": json.dumps(_location_to_dict(loc)),
                "label": loc.name or f"{loc.latitude:.4f}, {loc.longitude:.4f}",
            }
            for loc in candidates
        ]
        return ConfigActionResult(
            status="pending",
            message="Pick a candidate.",
            followup_action="home_location.pick",
            data={
                "form": {
                    "fields": [
                        {
                            "name": "candidate",
                            "label": "Candidate",
                            "type": "select",
                            "required": True,
                            "choices": choices,
                        },
                    ],
                },
            },
        )

    async def _action_home_location_pick(
        self,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        raw = payload.get("candidate")
        if not raw:
            return ConfigActionResult(
                status="error",
                message="No candidate selected.",
            )
        try:
            candidate = json.loads(str(raw))
        except json.JSONDecodeError:
            return ConfigActionResult(
                status="error",
                message="Invalid candidate payload.",
            )
        loc = _location_from_dict(candidate)
        if loc is None:
            return ConfigActionResult(
                status="error",
                message="Invalid candidate payload.",
            )
        await self._save_home_location(loc)
        return ConfigActionResult(
            status="ok",
            message=f"Home location set to {loc.name or 'the selected coordinates'}.",
        )

    # ── Lifecycle ────────────────────────────────────────────────────

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver

        # Load config first so we know whether to start at all.
        config_svc = resolver.get_capability("configuration")
        section: dict[str, Any] = {}
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                section = config_svc.get_section_safe(self.config_namespace)

        self._apply_config(section)

        if not self._enabled:
            logger.info("Weather service disabled via configuration")
            return

        # Storage (required)
        storage_svc = resolver.require_capability("entity_storage")
        if not isinstance(storage_svc, StorageProvider):
            raise TypeError("entity_storage does not satisfy StorageProvider")
        self._storage = storage_svc.create_namespaced("gilbert.weather")

        # Optional capabilities
        event_bus_svc = resolver.get_capability("event_bus")
        if isinstance(event_bus_svc, EventBusProvider):
            self._event_bus = event_bus_svc.bus
            # Self-subscribe for severe-alert delivery.
            self._alert_unsubscribe = self._event_bus.subscribe(
                "weather.alert.issued",
                self._on_alert_event,
            )

        notifications_svc = resolver.get_capability("notifications")
        if isinstance(notifications_svc, NotificationProvider):
            self._notifications = notifications_svc

        scheduler_svc = resolver.get_capability("scheduler")
        if isinstance(scheduler_svc, SchedulerProvider):
            self._scheduler = scheduler_svc

        # Resolve backend class via the registry (no concrete imports).
        backends = WeatherBackend.registered_backends()
        backend_cls = backends.get(self._backend_name)
        if backend_cls is None:
            raise RuntimeError(
                f"Unknown weather backend '{self._backend_name}'. "
                f"Install the open-meteo plugin (or another weather plugin) "
                f"and restart Gilbert."
            )
        self._backend = backend_cls()
        await self._backend.initialize(self._settings or {})

        # Resolve a geocoder backend (may be the same instance)
        await self._resolve_geocoder()

        # Load any persisted alert dedup rows
        await self._load_alert_dedup()
        self._alert_dedup_loaded = True
        # Reset first-sweep flag so the next _poll_alerts() suppresses
        # publishing for any currently-active alerts (whether persisted
        # dedup state matches them or not).
        self._first_sweep_done = False

        # Register scheduler jobs
        if self._scheduler is not None:
            if self._digest_enabled:
                with contextlib.suppress(Exception):
                    self._scheduler.add_job(
                        name="weather.digest",
                        schedule=Schedule.daily_at(self._digest_hour, self._digest_minute),
                        callback=self._publish_digest,
                        system=True,
                    )
            if self._backend.capabilities().alerts:
                with contextlib.suppress(Exception):
                    self._scheduler.add_job(
                        name="weather.alerts.poll",
                        schedule=Schedule.every(float(self._alert_poll_seconds)),
                        callback=self._poll_alerts,
                        system=True,
                    )

        logger.info(
            "Weather service started (backend=%s, digest=%s, alerts=%s)",
            self._backend_name,
            self._digest_enabled,
            self._backend.capabilities().alerts,
        )

    async def stop(self) -> None:
        if self._alert_unsubscribe is not None:
            with contextlib.suppress(Exception):
                self._alert_unsubscribe()
            self._alert_unsubscribe = None
        if self._scheduler is not None:
            for job in ("weather.digest", "weather.alerts.poll"):
                with contextlib.suppress(Exception):
                    self._scheduler.remove_job(job)
        # Persist alert dedup state on shutdown so the next start
        # doesn't republish active alerts.
        await self._persist_alert_dedup()
        if self._backend is not None:
            with contextlib.suppress(Exception):
                await self._backend.close()
            self._backend = None
        if (
            self._geocoder_backend is not None
            and not self._geocoder_is_active_backend
        ):
            with contextlib.suppress(Exception):
                await self._geocoder_backend.close()
        self._geocoder_backend = None
        self._geocoder_is_active_backend = False
        self._cache.clear()
        self._enabled = False

    async def _resolve_geocoder(self) -> None:
        """Pick a geocoder backend.

        Try the active backend's ``geocode()`` once; if that raises
        ``NotImplementedError``, walk the registry for the first class
        whose ``geocode`` method is overridden (i.e. its descriptor
        differs from the base ``WeatherBackend.geocode``) and
        instantiate / initialize it.
        """
        if self._backend is None:
            self._geocoder_backend = None
            return
        # Discriminator: function identity comparison, NOT a live HTTP probe.
        if type(self._backend).geocode is not WeatherBackend.geocode:
            self._geocoder_backend = self._backend
            self._geocoder_is_active_backend = True
            return
        # Walk the registry for an alternate.
        for cls in WeatherBackend.registered_backends().values():
            if cls is type(self._backend):
                continue
            if cls.geocode is WeatherBackend.geocode:
                continue
            try:
                instance = cls()
                await instance.initialize({})
            except Exception:
                logger.warning(
                    "Failed to instantiate %s for geocoder fallback",
                    cls.__name__,
                    exc_info=True,
                )
                continue
            self._geocoder_backend = instance
            self._geocoder_is_active_backend = False
            return
        self._geocoder_backend = None
        logger.info(
            "No registered backend supports geocoding; geocode_location will return "
            "geocoding_unavailable."
        )

    # ── WeatherProvider implementation ───────────────────────────────

    @property
    def tool_provider_name(self) -> str:
        return "weather"

    def _resolve_user(self, user: UserContext | None) -> UserContext:
        """Resolve the effective user.

        Single fallback path — when the caller passes ``None`` we read
        ``get_current_user()``. Mixing per-arg + ContextVar everywhere
        is forbidden because it causes both bugs at once; this is the
        only place ContextVar-fallback is allowed.
        """
        if user is not None:
            return user
        return get_current_user()

    async def resolve_location(self, user: UserContext | None) -> GeoLocation | None:
        """Resolve effective location for *user*.

        Order: per-user override → presence-derived hint (deferred — no
        backend supplies coords today) → service default home_location
        → ``None``.

        **Storage call discipline.** Reads are fresh on every call —
        per-user prefs are never cached on ``self``.
        """
        if self._storage is None:
            return None
        ctx = self._resolve_user(user)
        if ctx.user_id and ctx.user_id != UserContext.SYSTEM.user_id:
            row = await self._storage.get(_USER_PREFS_COLLECTION, ctx.user_id)
            if row:
                loc = _location_from_dict(row.get("location"))
                if loc is not None:
                    return loc
        return await self._load_home_location()

    async def resolve_units(self, user: UserContext | None) -> WeatherUnits:
        if self._storage is None:
            return self._default_units
        ctx = self._resolve_user(user)
        if ctx.user_id and ctx.user_id != UserContext.SYSTEM.user_id:
            row = await self._storage.get(_USER_PREFS_COLLECTION, ctx.user_id)
            if row:
                raw = row.get("units")
                if raw:
                    try:
                        return WeatherUnits(str(raw))
                    except ValueError:
                        pass
        return self._default_units

    async def get_current(
        self,
        location: GeoLocation | None = None,
        *,
        user: UserContext | None = None,
        units: WeatherUnits | None = None,
    ) -> CurrentWeather:
        loc, eff_units = await self._effective_location_units(location, user, units)
        return await self._cached_current(loc, eff_units)

    async def get_forecast_hourly(
        self,
        location: GeoLocation | None = None,
        *,
        hours: int = 24,
        user: UserContext | None = None,
        units: WeatherUnits | None = None,
    ) -> list[HourlyForecast]:
        loc, eff_units = await self._effective_location_units(location, user, units)
        return await self._cached_hourly(loc, hours, eff_units)

    async def get_forecast_daily(
        self,
        location: GeoLocation | None = None,
        *,
        days: int = 7,
        user: UserContext | None = None,
        units: WeatherUnits | None = None,
    ) -> list[DailyForecast]:
        loc, eff_units = await self._effective_location_units(location, user, units)
        return await self._cached_daily(loc, days, eff_units)

    async def get_alerts(
        self,
        location: GeoLocation | None = None,
        *,
        user: UserContext | None = None,
    ) -> list[WeatherAlert]:
        loc, _ = await self._effective_location_units(location, user, None)
        if self._backend is None or not self._backend.capabilities().alerts:
            return []
        return await self._cached_alerts(loc)

    async def _effective_location_units(
        self,
        location: GeoLocation | None,
        user: UserContext | None,
        units: WeatherUnits | None,
    ) -> tuple[GeoLocation, WeatherUnits]:
        loc = location
        if loc is None:
            loc = await self.resolve_location(user)
        if loc is None:
            raise LocationNotConfiguredError(
                "No location configured and none provided."
            )
        eff_units = units or await self.resolve_units(user)
        return loc, eff_units

    # ── Cache wrappers ───────────────────────────────────────────────

    async def _cached_current(
        self,
        loc: GeoLocation,
        units: WeatherUnits,
    ) -> CurrentWeather:
        if self._backend is None:
            raise WeatherUnavailableError("Weather backend not initialized")
        backend = self._backend
        key = WeatherCache.make_key(self._backend_name, "current", loc, units)

        async def loader() -> CurrentWeather:
            try:
                return await backend.current(loc, units=units)
            except WeatherUnavailableError:
                raise
            except Exception as exc:
                raise WeatherUnavailableError(str(exc)) from exc

        value, _stale = await self._cache.get_or_fetch(
            key, self._cache_ttl_current_s, loader,
        )
        # Note: callers needing stale_seconds use the public tool layer
        return cast(CurrentWeather, value)

    async def _cached_current_with_stale(
        self,
        loc: GeoLocation,
        units: WeatherUnits,
    ) -> tuple[CurrentWeather, float]:
        if self._backend is None:
            raise WeatherUnavailableError("Weather backend not initialized")
        backend = self._backend
        key = WeatherCache.make_key(self._backend_name, "current", loc, units)

        async def loader() -> CurrentWeather:
            try:
                return await backend.current(loc, units=units)
            except WeatherUnavailableError:
                raise
            except Exception as exc:
                raise WeatherUnavailableError(str(exc)) from exc

        value, stale = await self._cache.get_or_fetch(
            key, self._cache_ttl_current_s, loader,
        )
        return cast(CurrentWeather, value), stale

    async def _cached_hourly(
        self,
        loc: GeoLocation,
        hours: int,
        units: WeatherUnits,
    ) -> list[HourlyForecast]:
        if self._backend is None:
            raise WeatherUnavailableError("Weather backend not initialized")
        backend = self._backend
        key = WeatherCache.make_key(
            self._backend_name, "hourly", loc, units, hours=hours,
        )

        async def loader() -> list[HourlyForecast]:
            try:
                return await backend.forecast_hourly(loc, hours=hours, units=units)
            except WeatherUnavailableError:
                raise
            except Exception as exc:
                raise WeatherUnavailableError(str(exc)) from exc

        value, _stale = await self._cache.get_or_fetch(
            key, self._cache_ttl_hourly_s, loader,
        )
        return cast(list[HourlyForecast], value)

    async def _cached_daily(
        self,
        loc: GeoLocation,
        days: int,
        units: WeatherUnits,
    ) -> list[DailyForecast]:
        if self._backend is None:
            raise WeatherUnavailableError("Weather backend not initialized")
        backend = self._backend
        key = WeatherCache.make_key(
            self._backend_name, "daily", loc, units, days=days,
        )

        async def loader() -> list[DailyForecast]:
            try:
                return await backend.forecast_daily(loc, days=days, units=units)
            except WeatherUnavailableError:
                raise
            except Exception as exc:
                raise WeatherUnavailableError(str(exc)) from exc

        value, _stale = await self._cache.get_or_fetch(
            key, self._cache_ttl_daily_s, loader,
        )
        return cast(list[DailyForecast], value)

    async def _cached_alerts(self, loc: GeoLocation) -> list[WeatherAlert]:
        if self._backend is None or not self._backend.capabilities().alerts:
            return []
        backend = self._backend
        key = WeatherCache.make_key(
            self._backend_name, "alerts", loc, WeatherUnits.METRIC,
        )

        async def loader() -> list[WeatherAlert]:
            try:
                return await backend.alerts(loc)
            except WeatherUnavailableError:
                raise
            except Exception as exc:
                raise WeatherUnavailableError(str(exc)) from exc

        value, _stale = await self._cache.get_or_fetch(
            key, self._cache_ttl_alerts_s, loader,
        )
        return cast(list[WeatherAlert], value)

    # ── ToolProvider ─────────────────────────────────────────────────

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        return [
            ToolDefinition(
                name="current_weather",
                slash_group="weather",
                slash_command="now",
                slash_help="Current weather: /weather now [location]",
                description=_CURRENT_TOOL_DESCRIPTION,
                parameters=[
                    ToolParameter(
                        name="location",
                        type=ToolParameterType.STRING,
                        description=(
                            "Location query — a city/place name (will be "
                            "geocoded), or 'lat,lon' coordinates. Omit to "
                            "use the caller's configured location."
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
                description=_FORECAST_TOOL_DESCRIPTION,
                parameters=[
                    ToolParameter(
                        name="location",
                        type=ToolParameterType.STRING,
                        description=(
                            "Location query or 'lat,lon'. Optional — defaults "
                            "to the caller's configured location."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="hours",
                        type=ToolParameterType.INTEGER,
                        description="Hour-by-hour forecast horizon (1–72).",
                        required=False,
                    ),
                    ToolParameter(
                        name="days",
                        type=ToolParameterType.INTEGER,
                        description="Daily-summary horizon (1–14).",
                        required=False,
                    ),
                ],
                required_role="user",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="weather_alerts",
                slash_group="weather",
                slash_command="alerts",
                slash_help="Active severe-weather alerts: /weather alerts [location]",
                description=_ALERTS_TOOL_DESCRIPTION,
                parameters=[
                    ToolParameter(
                        name="location",
                        type=ToolParameterType.STRING,
                        description="Location query or 'lat,lon'. Optional.",
                        required=False,
                    ),
                ],
                required_role="user",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="geocode_location",
                slash_group="weather",
                slash_command="geocode",
                slash_help="Resolve a place name: /weather geocode <query>",
                description=_GEOCODE_TOOL_DESCRIPTION,
                parameters=[
                    ToolParameter(
                        name="query",
                        type=ToolParameterType.STRING,
                        description="Place name (city, region, country).",
                    ),
                    ToolParameter(
                        name="count",
                        type=ToolParameterType.INTEGER,
                        description="Max candidates (default 5, max 10).",
                        required=False,
                    ),
                ],
                required_role="user",
                parallel_safe=True,
            ),
            # Slash-only mutating tools — hidden from the AI.
            ToolDefinition(
                name="set_home_location",
                slash_group="weather",
                slash_command="set_home",
                slash_help="Set your home location: /weather set_home <city or lat,lon>",
                description=(
                    "Set the caller's per-user home location for weather "
                    "queries."
                ),
                parameters=[
                    ToolParameter(
                        name="query",
                        type=ToolParameterType.STRING,
                        description="Place name to geocode, or 'lat,lon'.",
                    ),
                ],
                required_role="user",
                parallel_safe=False,
                ai_visible=False,
            ),
            ToolDefinition(
                name="set_units",
                slash_group="weather",
                slash_command="set_units",
                slash_help="Set your preferred units: /weather set_units <metric|imperial>",
                description="Set the caller's preferred units for weather output.",
                parameters=[
                    ToolParameter(
                        name="units",
                        type=ToolParameterType.STRING,
                        description="metric or imperial.",
                        enum=["metric", "imperial"],
                    ),
                ],
                required_role="user",
                parallel_safe=False,
                ai_visible=False,
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        match name:
            case "current_weather":
                return await self._exec_current(arguments)
            case "forecast":
                return await self._exec_forecast(arguments)
            case "weather_alerts":
                return await self._exec_alerts(arguments)
            case "geocode_location":
                return await self._exec_geocode(arguments)
            case "set_home_location":
                return await self._exec_set_home(arguments)
            case "set_units":
                return await self._exec_set_units(arguments)
            case _:
                raise KeyError(f"Unknown tool: {name}")

    # ── Tool handlers ────────────────────────────────────────────────

    def _user_from_arguments(self, arguments: dict[str, Any]) -> UserContext | None:
        """Build a minimal UserContext from injected ``_user_id`` if present.

        The AI service injects ``_user_id``, ``_user_name``, and
        ``_user_roles`` into tool arguments. We use only ``_user_id`` to
        key per-user prefs.
        """
        user_id = arguments.get("_user_id")
        if not user_id:
            return None
        return UserContext(
            user_id=str(user_id),
            email=str(arguments.get("_user_email", "")),
            display_name=str(arguments.get("_user_name", "")),
        )

    async def _resolve_location_arg(
        self,
        location_arg: Any,
    ) -> GeoLocation | None:
        """Resolve a ``location`` tool argument to a ``GeoLocation``.

        Accepts ``"lat,lon"``, a place-name string, or a falsy value
        (return ``None``). On failure (no match), returns ``None`` —
        callers can render an appropriate error.
        """
        if not location_arg:
            return None
        s = str(location_arg).strip()
        if not s:
            return None
        # lat,lon shortcut
        if "," in s:
            parts = s.split(",", 1)
            try:
                lat = float(parts[0].strip())
                lon = float(parts[1].strip())
            except ValueError:
                pass
            else:
                if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                    return GeoLocation(latitude=lat, longitude=lon, name=s)
        # Else geocode
        candidates = await self._geocode(s, count=1)
        return candidates[0] if candidates else None

    async def _exec_current(self, arguments: dict[str, Any]) -> str:
        if self._backend is None:
            return json.dumps(
                {"error": "service_unavailable", "message": "Weather service not initialized."}
            )
        user = self._user_from_arguments(arguments)
        try:
            loc = await self._resolve_location_arg(arguments.get("location"))
            if loc is None:
                loc = await self.resolve_location(user)
            if loc is None:
                return json.dumps(
                    {
                        "error": "no_home_location",
                        "message": (
                            "I don't know where you are. Tell me your city, "
                            "or run /weather set_home <your city>."
                        ),
                        "set_home_command": "/weather set_home",
                    }
                )
            units = await self.resolve_units(user)
            cw, stale = await self._cached_current_with_stale(loc, units)
        except LocationNotConfiguredError:
            return json.dumps(
                {
                    "error": "no_home_location",
                    "message": (
                        "I don't know where you are. Tell me your city, "
                        "or run /weather set_home <your city>."
                    ),
                    "set_home_command": "/weather set_home",
                }
            )
        except WeatherUnavailableError as exc:
            return json.dumps(
                {
                    "error": "weather_unavailable",
                    "message": str(exc) or "Weather backend unavailable.",
                    "retryable": exc.retryable,
                    "provider_status": exc.provider_status,
                }
            )
        return json.dumps(_current_to_dict(cw, source=self._backend_name, stale_seconds=stale))

    async def _exec_forecast(self, arguments: dict[str, Any]) -> str:
        if self._backend is None:
            return json.dumps(
                {"error": "service_unavailable", "message": "Weather service not initialized."}
            )
        hours = arguments.get("hours")
        days = arguments.get("days")
        if hours is not None and days is not None:
            return json.dumps(
                {
                    "error": "invalid_arguments",
                    "message": "Specify hours OR days, not both.",
                    "retryable": False,
                }
            )
        if hours is None and days is None:
            hours = 24
        if hours is not None:
            try:
                hours_int = int(hours)
            except (TypeError, ValueError):
                return json.dumps(
                    {
                        "error": "invalid_arguments",
                        "message": "hours must be an integer.",
                        "retryable": False,
                    }
                )
            if not 1 <= hours_int <= 72:
                return json.dumps(
                    {
                        "error": "invalid_arguments",
                        "message": "hours must be between 1 and 72.",
                        "retryable": False,
                    }
                )
        days_int: int | None = None
        if days is not None:
            try:
                days_int = int(days)
            except (TypeError, ValueError):
                return json.dumps(
                    {
                        "error": "invalid_arguments",
                        "message": "days must be an integer.",
                        "retryable": False,
                    }
                )
            if not 1 <= days_int <= 14:
                return json.dumps(
                    {
                        "error": "invalid_arguments",
                        "message": "days must be between 1 and 14.",
                        "retryable": False,
                    }
                )

        user = self._user_from_arguments(arguments)
        try:
            loc = await self._resolve_location_arg(arguments.get("location"))
            if loc is None:
                loc = await self.resolve_location(user)
            if loc is None:
                return json.dumps(
                    {
                        "error": "no_home_location",
                        "message": (
                            "I don't know where you are. Tell me your city, "
                            "or run /weather set_home <your city>."
                        ),
                        "set_home_command": "/weather set_home",
                    }
                )
            units = await self.resolve_units(user)
            if days_int is not None:
                items = await self._cached_daily(loc, days_int, units)
                payload = {
                    "summary": _render_daily_summary(items),
                    "kind": "daily",
                    "days": [_daily_to_dict(d) for d in items],
                    "units": units.value,
                    "location": _location_to_dict(loc),
                    "source": self._backend_name,
                }
            else:
                hours_int_eff = int(hours)  # type: ignore[arg-type]
                items_h = await self._cached_hourly(loc, hours_int_eff, units)
                payload = {
                    "summary": _render_hourly_summary(items_h),
                    "kind": "hourly",
                    "hours": [_hourly_to_dict(h) for h in items_h],
                    "units": units.value,
                    "location": _location_to_dict(loc),
                    "source": self._backend_name,
                }
        except LocationNotConfiguredError:
            return json.dumps(
                {
                    "error": "no_home_location",
                    "message": (
                        "I don't know where you are. Tell me your city, "
                        "or run /weather set_home <your city>."
                    ),
                    "set_home_command": "/weather set_home",
                }
            )
        except WeatherUnavailableError as exc:
            return json.dumps(
                {
                    "error": "weather_unavailable",
                    "message": str(exc) or "Weather backend unavailable.",
                    "retryable": exc.retryable,
                    "provider_status": exc.provider_status,
                }
            )
        return json.dumps(payload)

    async def _exec_alerts(self, arguments: dict[str, Any]) -> str:
        if self._backend is None:
            return json.dumps(
                {"error": "service_unavailable", "message": "Weather service not initialized."}
            )
        user = self._user_from_arguments(arguments)
        try:
            loc = await self._resolve_location_arg(arguments.get("location"))
            if loc is None:
                loc = await self.resolve_location(user)
            if loc is None:
                return json.dumps(
                    {
                        "error": "no_home_location",
                        "message": (
                            "I don't know where you are. Tell me your city, "
                            "or run /weather set_home <your city>."
                        ),
                        "set_home_command": "/weather set_home",
                    }
                )
        except LocationNotConfiguredError:
            return json.dumps(
                {
                    "error": "no_home_location",
                    "message": (
                        "I don't know where you are. Tell me your city, "
                        "or run /weather set_home <your city>."
                    ),
                    "set_home_command": "/weather set_home",
                }
            )

        supported = bool(self._backend.capabilities().alerts)
        if not supported:
            return json.dumps(
                {
                    "summary": _render_alerts_summary([], supported=False),
                    "alerts": [],
                    "supported": False,
                    "reason": (
                        "The configured weather backend does not issue "
                        "severe-weather alerts. Install the NWS plugin (US) "
                        "or OpenWeather plugin for alert coverage."
                    ),
                    "location": _location_to_dict(loc),
                    "source": self._backend_name,
                }
            )
        try:
            alerts = await self._cached_alerts(loc)
        except WeatherUnavailableError as exc:
            return json.dumps(
                {
                    "error": "weather_unavailable",
                    "message": str(exc) or "Alerts backend unavailable.",
                    "retryable": exc.retryable,
                    "provider_status": exc.provider_status,
                }
            )
        return json.dumps(
            {
                "summary": _render_alerts_summary(alerts, supported=True),
                "alerts": [_alert_to_dict(a) for a in alerts],
                "supported": True,
                "location": _location_to_dict(loc),
                "source": self._backend_name,
            }
        )

    async def _exec_geocode(self, arguments: dict[str, Any]) -> str:
        query = str(arguments.get("query", "")).strip()
        if not query:
            return json.dumps(
                {
                    "error": "invalid_arguments",
                    "message": "query is required.",
                    "retryable": False,
                }
            )
        count_arg = arguments.get("count", 5)
        try:
            count = max(1, min(int(count_arg), 10))
        except (TypeError, ValueError):
            count = 5
        if self._geocoder_backend is None:
            return json.dumps(
                {
                    "error": "geocoding_unavailable",
                    "message": (
                        "No installed weather backend supports place-name "
                        "lookup. Install the open-meteo plugin."
                    ),
                }
            )
        try:
            candidates = await self._geocode(query, count=count)
        except WeatherUnavailableError as exc:
            return json.dumps(
                {
                    "error": "weather_unavailable",
                    "message": str(exc) or "Geocoding backend unavailable.",
                    "retryable": exc.retryable,
                    "provider_status": exc.provider_status,
                }
            )
        if not candidates:
            return json.dumps(
                {
                    "error": "no_results",
                    "message": f"No matching place found for '{query}'.",
                    "retryable": False,
                }
            )
        return json.dumps(
            {
                "summary": (
                    f"{len(candidates)} candidate(s) for '{query}'."
                ),
                "candidates": [_location_to_dict(loc) for loc in candidates],
                "query": query,
            }
        )

    async def _exec_set_home(self, arguments: dict[str, Any]) -> str:
        if self._storage is None:
            return json.dumps(
                {"error": "service_unavailable", "message": "Weather service not initialized."}
            )
        user = self._user_from_arguments(arguments)
        if user is None or not user.user_id or user.user_id == UserContext.SYSTEM.user_id:
            return json.dumps(
                {
                    "error": "no_user",
                    "message": "Sign in to set a per-user home location.",
                }
            )
        query = str(arguments.get("query", "")).strip()
        if not query:
            return json.dumps(
                {
                    "error": "invalid_arguments",
                    "message": "query is required (place name or 'lat,lon').",
                    "retryable": False,
                }
            )
        loc = await self._resolve_location_arg(query)
        if loc is None:
            return json.dumps(
                {
                    "error": "no_results",
                    "message": f"No matching place found for '{query}'.",
                    "retryable": False,
                }
            )
        await self._save_user_location(user.user_id, loc)
        return json.dumps(
            {
                "summary": (
                    f"Saved your home location to "
                    f"{loc.name or f'{loc.latitude:.4f}, {loc.longitude:.4f}'}. "
                    "Severe-weather alerts currently use the admin's home "
                    "location — your per-user location is used for current "
                    "conditions and forecasts only."
                ),
                "location": _location_to_dict(loc),
            }
        )

    async def _exec_set_units(self, arguments: dict[str, Any]) -> str:
        if self._storage is None:
            return json.dumps(
                {"error": "service_unavailable", "message": "Weather service not initialized."}
            )
        user = self._user_from_arguments(arguments)
        if user is None or not user.user_id or user.user_id == UserContext.SYSTEM.user_id:
            return json.dumps(
                {
                    "error": "no_user",
                    "message": "Sign in to set per-user units.",
                }
            )
        raw = str(arguments.get("units", "")).strip().lower()
        try:
            units = WeatherUnits(raw)
        except ValueError:
            return json.dumps(
                {
                    "error": "invalid_arguments",
                    "message": "units must be 'metric' or 'imperial'.",
                    "retryable": False,
                }
            )
        await self._save_user_units(user.user_id, units)
        return json.dumps(
            {
                "summary": f"Saved your preferred units as {units.value}.",
                "units": units.value,
            }
        )

    # ── Geocoding ────────────────────────────────────────────────────

    async def _geocode(self, query: str, *, count: int) -> list[GeoLocation]:
        if self._geocoder_backend is None:
            return []
        try:
            return await self._geocoder_backend.geocode(query, count=count)
        except NotImplementedError:
            return []
        except WeatherUnavailableError:
            raise
        except Exception as exc:  # transport / parse — wrap as unavailable
            raise WeatherUnavailableError(str(exc)) from exc

    # ── Storage helpers ──────────────────────────────────────────────

    async def _load_home_location(self) -> GeoLocation | None:
        if self._storage is None:
            return None
        row = await self._storage.get(_SERVICE_STATE_COLLECTION, _HOME_LOCATION_ID)
        if not row:
            return None
        return _location_from_dict(row.get("location"))

    async def _save_home_location(self, loc: GeoLocation) -> None:
        if self._storage is None:
            return
        await self._storage.put(
            _SERVICE_STATE_COLLECTION,
            _HOME_LOCATION_ID,
            {
                "_id": _HOME_LOCATION_ID,
                "location": _location_to_dict(loc),
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )

    async def _save_user_location(self, user_id: str, loc: GeoLocation) -> None:
        if self._storage is None:
            return
        existing = await self._storage.get(_USER_PREFS_COLLECTION, user_id) or {}
        existing.update(
            {
                "_id": user_id,
                "user_id": user_id,
                "location": _location_to_dict(loc),
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
        await self._storage.put(_USER_PREFS_COLLECTION, user_id, existing)

    async def _save_user_units(self, user_id: str, units: WeatherUnits) -> None:
        if self._storage is None:
            return
        existing = await self._storage.get(_USER_PREFS_COLLECTION, user_id) or {}
        existing.update(
            {
                "_id": user_id,
                "user_id": user_id,
                "units": units.value,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
        await self._storage.put(_USER_PREFS_COLLECTION, user_id, existing)

    async def _load_alert_dedup(self) -> None:
        """Load persisted alert dedup state.

        Old rows (older than 7 days) are GC'd at startup so the
        collection doesn't grow unboundedly across location changes.
        """
        if self._storage is None:
            return
        from gilbert.interfaces.storage import Query

        try:
            rows = await self._storage.query(Query(collection=_ALERT_DEDUP_COLLECTION))
        except Exception:
            logger.warning("Failed to load alert dedup state", exc_info=True)
            return
        cutoff = datetime.now(UTC).timestamp() - (_DEDUP_TTL_DAYS * 86400)
        for row in rows:
            try:
                last_iso = str(row.get("last_updated", ""))
                last_ts = datetime.fromisoformat(last_iso).timestamp() if last_iso else 0.0
            except ValueError:
                last_ts = 0.0
            if last_ts < cutoff:
                # GC stale row
                with contextlib.suppress(Exception):
                    await self._storage.delete(
                        _ALERT_DEDUP_COLLECTION,
                        str(row.get("_id", "")),
                    )
                continue
            loc_key = str(row.get("location_key", ""))
            scope_id = str(row.get("scope_id", "system"))
            seen = row.get("seen_alert_ids", [])
            if loc_key:
                self._known_alert_ids[(loc_key, scope_id)] = set(
                    str(x) for x in (seen if isinstance(seen, list) else [])
                )

    async def _persist_alert_dedup(self) -> None:
        if self._storage is None:
            return
        for (loc_key, scope_id), seen in self._known_alert_ids.items():
            row_id = f"{scope_id}:{loc_key}"
            with contextlib.suppress(Exception):
                await self._storage.put(
                    _ALERT_DEDUP_COLLECTION,
                    row_id,
                    {
                        "_id": row_id,
                        "location_key": loc_key,
                        "scope_id": scope_id,
                        "seen_alert_ids": sorted(seen),
                        "last_updated": datetime.now(UTC).isoformat(),
                    },
                )

    async def _persist_alert_dedup_one(
        self,
        dedup_key: tuple[str, str],
        seen: set[str],
    ) -> None:
        if self._storage is None:
            return
        loc_key, scope_id = dedup_key
        row_id = f"{scope_id}:{loc_key}"
        with contextlib.suppress(Exception):
            await self._storage.put(
                _ALERT_DEDUP_COLLECTION,
                row_id,
                {
                    "_id": row_id,
                    "location_key": loc_key,
                    "scope_id": scope_id,
                    "seen_alert_ids": sorted(seen),
                    "last_updated": datetime.now(UTC).isoformat(),
                },
            )

    # ── Alert poll ───────────────────────────────────────────────────

    async def _poll_alerts(self) -> None:
        """Periodic alert-poll job. Skipped silently when backend has no alerts."""
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

        # First-sweep-after-restart guard: treat every currently-active
        # alert as already-seen and persist without publishing. Protects
        # against re-firing alerts on cold boot or after a crash where
        # persisted dedup state does not include alerts that became
        # active during downtime. Subsequent polls publish only keys
        # not in the persisted set.
        if not self._first_sweep_done:
            seen.clear()
            seen.update(a.alert_id for a in current_alerts)
            self._first_sweep_done = True
            await self._persist_alert_dedup_one(dedup_key, seen)
            return

        new_alerts = [a for a in current_alerts if a.alert_id not in seen]
        for alert in new_alerts:
            seen.add(alert.alert_id)
            if self._event_bus is not None:
                await self._event_bus.publish(
                    Event(
                        event_type="weather.alert.issued",
                        data=_alert_to_dict(alert),
                        source="weather",
                    )
                )
            # Soft-bypass: invalidate current-conditions cache for this
            # location on SEVERE / EXTREME so the next call hits live data.
            if severity_rank(alert.severity) >= severity_rank(AlertSeverity.SEVERE):
                self._cache.invalidate_prefix(
                    f"{self._backend_name}:current:{loc_key}"
                )
        # Drop dedup entries for alerts that have aged out.
        seen.intersection_update({a.alert_id for a in current_alerts})
        await self._persist_alert_dedup_one(dedup_key, seen)

    async def _on_alert_event(self, event: Event) -> None:
        """Handle our own ``weather.alert.issued`` event — fan-out delivery.

        Severity ladder (default urgency map):

            EXTREME  → urgent
            SEVERE   → urgent
            MODERATE → normal
            MINOR    → info

        Voice opt-in via ``alert_voice_enabled``; minimum severity
        ``alert_voice_minimum`` (default ``EXTREME``). Severity
        ordering uses :func:`severity_rank` to avoid lexicographic
        StrEnum compare bugs.
        """
        try:
            severity = AlertSeverity(str(event.data.get("severity", "minor")))
        except ValueError:
            severity = AlertSeverity.MINOR
        urgency = self._alert_urgency.get(severity, NotificationUrgency.INFO)
        title = str(event.data.get("title", "Weather alert"))
        description = str(event.data.get("description", ""))
        message = f"{title} — {description[:200]}".rstrip(" —")

        # Phase-1 fan-out: notify users whose configured location
        # matches the polled location key. v1 polls only the
        # service-default location; per-user location polling is
        # explicitly out of scope for this PR.
        if self._notifications is not None:
            for user_id in await self._users_for_alert(event):
                with contextlib.suppress(Exception):
                    await self._notifications.notify_user(
                        user_id=user_id,
                        message=message,
                        urgency=urgency,
                        source="weather",
                        source_ref={"alert_id": str(event.data.get("alert_id", ""))},
                    )

        if (
            self._alert_voice_enabled
            and severity_rank(severity) >= severity_rank(self._alert_voice_minimum)
            and self._resolver is not None
        ):
            speaker = self._resolver.get_capability("speaker_control")
            if isinstance(speaker, SpeakerProvider):
                with contextlib.suppress(Exception):
                    await speaker.announce(
                        message,
                        context="severe-weather alert",
                    )

    async def _users_for_alert(self, event: Event) -> list[str]:
        """Return user_ids whose stored ``home_location`` matches the alert.

        v1: matches users whose per-user ``location`` is at the same
        rounded ``(lat, lon)`` as the service-default home (the only
        location currently polled). Users with a different per-user
        location will not receive alerts until the multi-poll PR lands.
        """
        if self._storage is None:
            return []
        home = await self._load_home_location()
        if home is None:
            return []
        # The poll publishes for the service-default home_location;
        # match per-user prefs against that key.
        target_key = (round(home.latitude, 4), round(home.longitude, 4))
        from gilbert.interfaces.storage import Query

        try:
            rows = await self._storage.query(Query(collection=_USER_PREFS_COLLECTION))
        except Exception:
            return []
        users: list[str] = []
        for row in rows:
            loc = _location_from_dict(row.get("location"))
            if loc is None:
                continue
            key = (round(loc.latitude, 4), round(loc.longitude, 4))
            if key == target_key:
                uid = str(row.get("user_id", ""))
                if uid:
                    users.append(uid)
        return users

    # ── Daily digest ─────────────────────────────────────────────────

    async def _publish_digest(self) -> None:
        """Fire the daily weather digest event (fan-out broadcast).

        Idempotency: the digest fires at most once per calendar day
        (in the home location's timezone). A ``last_digest_date`` row
        in ``service_state`` is the source of truth and survives
        restart, so a config-reload or scheduler re-register cannot
        double-fire on the same day.
        """
        if self._backend is None or self._event_bus is None:
            return
        location = await self._load_home_location()
        if location is None:
            return

        today_iso = _today_iso(location.timezone)
        if await self._digest_already_fired_today(today_iso):
            return

        units = self._default_units
        try:
            current = await self._cached_current(location, units)
            hourly = await self._cached_hourly(
                location, self._digest_horizon_hours, units,
            )
            daily = await self._cached_daily(
                location, self._digest_horizon_days, units,
            )
            if daily and daily[0].date == today_iso:
                daily = daily[1:]   # drop redundant "today" slice
        except WeatherUnavailableError:
            logger.warning("Weather digest skipped — backend unavailable")
            return

        payload: dict[str, Any] = {
            "current": _current_to_dict(current, source=self._backend_name, stale_seconds=0.0),
            "hourly": [_hourly_to_dict(h) for h in hourly][:50],
            "daily": [_daily_to_dict(d) for d in daily][:7],
            "location": _location_to_dict(location),
            "units": units.value,
            "source": self._backend_name,
        }
        await self._event_bus.publish(
            Event(
                event_type="weather.digest",
                data=payload,
                source="weather",
            )
        )
        await self._mark_digest_fired(today_iso)

    async def _digest_already_fired_today(self, today_iso: str) -> bool:
        if self._storage is None:
            return False
        try:
            row = await self._storage.get(
                _SERVICE_STATE_COLLECTION, "last_digest",
            )
        except Exception:
            return False
        if not row:
            return False
        return str(row.get("date", "")) == today_iso

    async def _mark_digest_fired(self, today_iso: str) -> None:
        if self._storage is None:
            return
        with contextlib.suppress(Exception):
            await self._storage.put(
                _SERVICE_STATE_COLLECTION,
                "last_digest",
                {
                    "_id": "last_digest",
                    "date": today_iso,
                    "fired_at": datetime.now(UTC).isoformat(),
                },
            )

    # ── GreetingContextProvider ──────────────────────────────────────

    @property
    def greeting_context_id(self) -> str:
        return "weather"

    @property
    def greeting_context_label(self) -> str:
        return "Weather"

    async def greeting_context(self, user_id: str) -> GreetingContext | None:
        """Render the current-weather template, or return None on any
        error / no-data condition. Never raises."""
        try:
            if self._backend is None:
                return None

            location = await self._load_home_location()
            if location is None:
                return None

            current = await self.get_current()

            temp_suffix = "°F" if current.units is WeatherUnits.IMPERIAL else "°C"
            speed_suffix = "mph" if current.units is WeatherUnits.IMPERIAL else "km/h"
            condition_phrase = current.condition.value.replace("_", " ")
            feels_like_clause = ""
            if (
                current.feels_like is not None
                and abs(current.feels_like - current.temperature) >= 3
            ):
                feels_like_clause = f", feels like {current.feels_like:.0f}{temp_suffix}"
            location_name = current.location.name or "the configured location"

            prose = self._weather_hint_template.format(
                location_name=location_name,
                temperature=current.temperature,
                temp_suffix=temp_suffix,
                condition_phrase=condition_phrase,
                wind_speed=current.wind_speed,
                speed_suffix=speed_suffix,
                feels_like_clause=feels_like_clause,
            )
            return GreetingContext(provider_id="weather", label="Weather", prose=prose)
        except Exception:
            logger.debug(
                "WeatherService.greeting_context failed for %s", user_id, exc_info=True
            )
            return None


__all__ = [
    "WeatherService",
    "_render_current_summary",
    "_render_hourly_summary",
    "_render_daily_summary",
    "_render_alerts_summary",
    "_today_iso",
]
