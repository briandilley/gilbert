# Weather Service

## Summary
Multi-backend weather aggregator that exposes current conditions, hourly/daily forecasts, and severe-weather alerts as both AI tools and the `WeatherProvider` capability protocol. Default backend is **Open-Meteo** (no API key); the interface is shaped to slot in NWS / OpenWeatherMap later. Owns a single-flight + LRU cache, the location/units resolution chain, persistent severe-alert dedup, and the daily digest event.

## Details

### Layer placement
- `src/gilbert/interfaces/weather.py` — `WeatherBackend` ABC, dataclasses (`CurrentWeather`, `HourlyForecast`, `DailyForecast`, `WeatherAlert`, `GeoLocation`), enums (`WeatherCondition`, `AlertSeverity`, `WeatherUnits`), `WeatherProvider` capability protocol, `LocationNotConfiguredError` / `WeatherUnavailableError`, and a `severity_rank()` helper that orders alert severities by **numeric rank** (not lexicographic StrEnum compare — `"severe" > "extreme"` is True alphabetically, which is the bug we explicitly avoid).
- `src/gilbert/core/services/weather.py` — `WeatherService` (Service + ToolProvider + Configurable + ConfigActionProvider). Imports only from `gilbert.interfaces.*` and the shared `_weather_cache.py` helper.
- `src/gilbert/core/services/_weather_cache.py` — extracted single-flight + LRU cache so the service file stays under ~700 lines.
- `std-plugins/open-meteo/` — first-party plugin: `OpenMeteoWeather` backend, WMO 4677 → `WeatherCondition` mapping, geocoding, `test_connection` action. No third-party deps beyond `httpx` (already in core).

### Backend pattern (universal)
`WeatherBackend` follows the standard `__init_subclass__` registry. Concrete backends set `backend_name = "open-meteo"` (etc.) and are auto-registered when their module is imported (the plugin's `setup()` does `from . import open_meteo_weather`). `capabilities() -> WeatherBackendCapabilities` advertises which methods are meaningful — Open-Meteo returns `alerts=False` and the alert poll job skips silently.

### Cache shape
Single-flight using `asyncio.Future` per in-flight key (NOT a per-key Lock dict that could leak entries forever) plus an LRU-bounded `OrderedDict` (`max_entries=2048`). Cache key includes `backend_name` so a backend swap doesn't pollute. Round lat/lon to 4 decimals (~11 m) so close-but-not-identical callers share a slot.

`get_or_fetch(key, ttl_s, loader)` returns `(value, stale_seconds)`. **Stale-on-failure is forbidden** — when the loader raises, the error propagates. The cache never silently serves a stale entry.

`invalidate_prefix(prefix)` is used by `_poll_alerts` to soft-bypass the current-conditions cache for a location after a SEVERE/EXTREME alert publishes.

### Location resolution chain
`resolve_location(user)` walks: per-user override (`gilbert.weather.user_prefs.{user_id}.location`) → presence-derived (designed-in but not wired up — UniFi presence has no coords today) → service default (`gilbert.weather.service_state._id="home_location"`) → `None`. Reads are fresh on every call — per-user prefs are NEVER cached on `self` (forbidden multi-user isolation pattern).

`resolve_units(user)` is the same shape — per-user `units` field then service default `default_units`.

### Identity contract — explicit over ContextVar
`WeatherProvider` methods take `user: UserContext | None`, not a bare `user_id` string. Callers from background jobs (greeting, scheduled actions) pass identity in explicitly. Inside `WeatherService`, `_resolve_user(user)` is the **only** place that may fall back to `gilbert.core.context.get_current_user()` when `user is None`. Mixing per-arg + ContextVar everywhere is forbidden.

### Tool surface (six tools, one slash_group="weather")
- AI-visible (`parallel_safe=True`, `required_role="user"`): `current_weather`, `forecast`, `weather_alerts`, `geocode_location`.
- Slash-only (`ai_visible=False`): `set_home_location`, `set_units`. The `ai_visible` flag was added to `ToolDefinition` for this purpose; `AIService._discover_tools` filters out `ai_visible=False` entries before sending to the model.

`forecast` validates `hours OR days` (mutually exclusive) and ranges (`hours 1-72`, `days 1-14`), returning structured `{"error": "invalid_arguments"}` payloads. Default with neither given is `hours=24`.

### Severe-alert delivery
`WeatherService` self-subscribes to `weather.alert.issued`. `_on_alert_event` calls `NotificationProvider.notify_user(...)` for every user whose stored `home_location` matches the polled location key. Severity → urgency map: `EXTREME`/`SEVERE` → urgent, `MODERATE` → normal, `MINOR` → info. Voice opt-in via `alert_voice_enabled` (default off); minimum severity `alert_voice_minimum` (default `EXTREME`). Severity ordering uses `severity_rank()`.

**v1 limitation:** poll uses only the service-default location; users with a per-user `home_location` different from the admin home will not get alerts for their location until the multi-poll PR lands. Documented in the per-user `set_home_location` confirmation message.

### Alert dedup persistence
`_known_alert_ids: dict[(location_key, scope_id), set[str]]` (scope_id="system" today, leaves room for per-user fan-out without a structural change). Persisted to `gilbert.weather.alert_dedup` on `stop()` and on each poll's tail. **First sweep after `start()` (cold boot, post-crash, or fresh install) treats every currently-active alert as already-seen and persists the dedup row WITHOUT publishing any `weather.alert.issued` events.** A `_first_sweep_done` flag guards this — it's reset to False in `start()` and flipped to True at the end of the first `_poll_alerts()` call. Subsequent polls publish only alert ids not in the persisted set. This protects against the spam vector where Gilbert was down during an active alert window: the just-restarted service must not re-blast every subscriber. Rows older than 7 days are GC'd at startup.

### Geocoding cross-backend fallback
`geocode()` lives on the backend ABC because it's a *backend capability* that may be borrowed across plugins. Default impl raises `NotImplementedError`. Resolution at `start()`:
1. If `type(self._backend).geocode is not WeatherBackend.geocode`, the active backend supplies geocoding (no probe HTTP call — function-identity discriminator).
2. Otherwise walk `WeatherBackend.registered_backends()` for the first class whose `geocode` is overridden, instantiate, `initialize({})`, store on `self._geocoder_backend`.
3. If none, `geocode_location` returns `geocoding_unavailable` with a clear "install the open-meteo plugin" message.

### Daily digest event
When `digest_enabled=True` and the scheduler is available, fires `weather.digest` daily at `digest_hour:digest_minute` (server local timezone — the scheduler is naive-local, no tz-aware DAILY primitive yet). Restart skips the missed fire and waits for tomorrow. **Idempotent within a calendar day**: a `last_digest` row in `service_state` (date in the home-location timezone) is checked at the top of `_publish_digest` and updated after a successful publish, so a config-reload or scheduler re-register cannot double-fire on the same day. Drops the redundant "today" daily slice when also emitting hourly. Hard-capped at 50 hourly + 7 daily slices regardless of config to bound payload size.

### Configuration parameters (`config_namespace = "weather"`)
- `enabled` (BOOL, default false — service is useless without `home_location`).
- `backend` (STRING, choices from registry).
- `default_units` (`metric`/`imperial`).
- `cache_ttl_*_seconds` for current/hourly/daily/alerts.
- `digest_enabled`, `digest_hour`, `digest_minute`, `digest_horizon_hours`, `digest_horizon_days`.
- `alert_poll_seconds`, `alert_voice_enabled`, `alert_voice_minimum`.
- `settings.*` merged from `backend_config_params()` (Open-Meteo's `timeout_seconds`, `user_agent`).

`home_location` is NOT a `ConfigParam` — it lives only in entity storage at `gilbert.weather.service_state._id="home_location"`, set via the `home_location.set` two-phase Settings ConfigAction.

### Greeting integration
`GreetingService.start()` does `resolver.get_capability("weather")` and stores it as `WeatherProvider | None`. New `include_weather` (BOOL, default true) and `weather_hint_template` (STRING, multiline, `ai_prompt=True`) ConfigParams. `_build_weather_blurb(user)` interpolates the template with deterministic values; the blurb gets injected into the AI greeting prompt as `Context: …`, so `ai_prompt=True` per the AI-Prompts-Are-Always-Configurable rule. Catches `LocationNotConfiguredError` (silent skip) and `WeatherUnavailableError` (logged debug) by exact type — no bare `except Exception`.

### Slot-in for NWS / OpenWeather
Both fit the interface unchanged:
- **NWS**: `backend_name = "nws"`, `capabilities().alerts = True`, `geocode()` raises `NotImplementedError` (cross-backend fallback handles it). `WeatherAlert` was specifically shaped to map NWS GeoJSON `properties.{event,severity,sent,expires,areaDesc}` 1:1.
- **OpenWeather**: `backend_name = "openweather"`, `backend_config_params()` adds `api_key` (`sensitive=True`), `capabilities().alerts = True` (One Call API), `geocode()` overrides.

## Related
- [Backend Pattern](memory-backend-pattern.md) — universal ABC + registry pattern this feature follows.
- [Multi-backend Aggregator Pattern](memory-multi-backend-pattern.md) — service holds backends; the cache key already includes `backend_name` to support a future per-method multi-backend layout.
- [Capability Protocols](memory-capability-protocols.md) — `WeatherProvider` belongs in `interfaces/`.
- [AI Prompts Are Always Configurable](memory-ai-prompts-configurable.md) — `weather_hint_template` on `GreetingService` ships with `ai_prompt=True` because its rendered output is interpolated into the AI greeting prompt as context.
- [Multi-User Isolation](memory-multi-user-isolation.md) — singleton-safe state, fresh `storage.get` per call, no per-user prefs cache on `self`.
- [Scheduler Service](memory-scheduler-service.md) — `weather.digest` and `weather.alerts.poll` system jobs.
- [Notification Service](memory-notification-service.md) — used by severe-alert delivery.
- [Slash Commands](memory-slash-commands.md) — `slash_group="weather"` collapse + the AI-visible / slash-only distinction for `set_home_location` / `set_units`.
- [Web Search Service](memory-websearch-service.md) — closest existing analog.
- `src/gilbert/interfaces/weather.py`, `src/gilbert/core/services/weather.py`, `src/gilbert/core/services/_weather_cache.py`
- `std-plugins/open-meteo/`
- Open-Meteo Forecast API — https://open-meteo.com/en/docs
- Open-Meteo Geocoding API — https://open-meteo.com/en/docs/geocoding-api

