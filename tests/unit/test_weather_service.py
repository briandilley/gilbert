"""Unit tests for ``WeatherService`` against a stub backend.

We construct a real ``WeatherService``, attach a real SQLite storage
backend (no DB mocking), a fake event bus, fake scheduler, and a
deterministic in-memory weather backend. We exercise the public API
plus internal contracts the service spec calls out (cache shape,
location-resolution chain, alert dedup persistence, severe-alert
fan-out, geocoder fallback).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from gilbert.core.events import InMemoryEventBus
from gilbert.core.services._weather_cache import WeatherCache
from gilbert.core.services.weather import WeatherService
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.events import Event
from gilbert.interfaces.notifications import Notification, NotificationUrgency
from gilbert.interfaces.weather import (
    AlertSeverity,
    CurrentWeather,
    DailyForecast,
    GeoLocation,
    HourlyForecast,
    LocationNotConfiguredError,
    WeatherAlert,
    WeatherBackend,
    WeatherBackendCapabilities,
    WeatherCondition,
    WeatherProvider,
    WeatherUnavailableError,
    WeatherUnits,
    severity_rank,
)
from gilbert.storage.sqlite import SQLiteStorage
from tests.unit.conftest import (
    _FakeEventBusProvider,
    _FakeSchedulerProvider,
    _FakeStorageProvider,
    _make_resolver,
)

# ── Fakes ────────────────────────────────────────────────────────────


class FakeWeatherBackend(WeatherBackend):
    """In-memory weather backend used by tests.

    Tracks call counts so we can assert on cache behaviour, exposes
    knobs to swap return values, simulate failures, and to gate calls
    behind an ``asyncio.Event`` for single-flight tests.
    """

    backend_name = "fake_weather"

    def __init__(self) -> None:
        self.initialized_with: dict[str, Any] | None = None
        self.closed = False
        self.current_calls = 0
        self.hourly_calls = 0
        self.daily_calls = 0
        self.alerts_calls = 0
        self.geocode_calls = 0
        self.alerts_to_return: list[WeatherAlert] = []
        self.alerts_capability: bool = False
        self.fail_with: BaseException | None = None
        self.gate: asyncio.Event | None = None
        self.geocode_results: list[GeoLocation] = []
        self.location_to_return: GeoLocation | None = None

    def capabilities(self) -> WeatherBackendCapabilities:
        return WeatherBackendCapabilities(
            current=True, hourly=True, daily=True, alerts=self.alerts_capability,
        )

    async def initialize(self, config: dict[str, Any]) -> None:
        self.initialized_with = dict(config)

    async def close(self) -> None:
        self.closed = True

    async def current(
        self,
        location: GeoLocation,
        *,
        units: WeatherUnits = WeatherUnits.METRIC,
    ) -> CurrentWeather:
        self.current_calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_with is not None:
            raise self.fail_with
        loc = self.location_to_return or location
        return CurrentWeather(
            location=loc,
            observed_at=datetime(2026, 5, 9, 14, 30, tzinfo=UTC),
            temperature=18.4,
            feels_like=17.1,
            humidity_pct=62.0,
            wind_speed=12.3,
            wind_gust=21.7,
            wind_direction_deg=240.0,
            pressure_hpa=1014.2,
            precipitation_last_hour=0.0,
            cloud_cover_pct=75.0,
            condition=WeatherCondition.CLOUDY,
            description="",
            units=units,
        )

    async def forecast_hourly(
        self,
        location: GeoLocation,
        *,
        hours: int = 24,
        units: WeatherUnits = WeatherUnits.METRIC,
    ) -> list[HourlyForecast]:
        self.hourly_calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return [
            HourlyForecast(
                location=location,
                valid_at=datetime(2026, 5, 9, 15 + i, tzinfo=UTC),
                temperature=18.0 + i * 0.5,
                feels_like=17.0 + i * 0.5,
                precipitation=0.1 * i,
                precipitation_probability_pct=20.0 * i,
                wind_speed=12.0 + i,
                wind_gust=20.0 + i,
                wind_direction_deg=240.0,
                cloud_cover_pct=80.0,
                condition=WeatherCondition.RAIN if i >= 2 else WeatherCondition.CLOUDY,
                units=units,
            )
            for i in range(min(hours, 5))
        ]

    async def forecast_daily(
        self,
        location: GeoLocation,
        *,
        days: int = 7,
        units: WeatherUnits = WeatherUnits.METRIC,
    ) -> list[DailyForecast]:
        self.daily_calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return [
            DailyForecast(
                location=location,
                date=f"2026-05-{9 + i:02d}",
                temperature_high=20.0 + i,
                temperature_low=12.0 + i,
                precipitation=2.0,
                precipitation_probability_pct=80.0,
                wind_speed_max=15.0,
                wind_gust_max=25.0,
                sunrise=None,
                sunset=None,
                condition=WeatherCondition.CLEAR,
                units=units,
            )
            for i in range(min(days, 3))
        ]

    async def alerts(self, location: GeoLocation) -> list[WeatherAlert]:
        self.alerts_calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return list(self.alerts_to_return)

    async def geocode(self, query: str, *, count: int = 5) -> list[GeoLocation]:
        self.geocode_calls += 1
        return list(self.geocode_results)


class _NoGeocodeBackend(WeatherBackend):
    """Backend that doesn't override ``geocode`` — uses ABC default."""

    backend_name = "no_geocode_fake"

    async def initialize(self, config: dict[str, Any]) -> None:
        pass

    async def close(self) -> None:
        pass

    async def current(
        self,
        location: GeoLocation,
        *,
        units: WeatherUnits = WeatherUnits.METRIC,
    ) -> CurrentWeather:
        return CurrentWeather(
            location=location,
            observed_at=datetime.now(UTC),
            temperature=15.0,
            feels_like=14.0,
            humidity_pct=50.0,
            wind_speed=5.0,
            wind_gust=None,
            wind_direction_deg=None,
            pressure_hpa=None,
            precipitation_last_hour=0.0,
            cloud_cover_pct=10.0,
            condition=WeatherCondition.CLEAR,
            units=units,
        )

    async def forecast_hourly(
        self,
        location: GeoLocation,
        *,
        hours: int = 24,
        units: WeatherUnits = WeatherUnits.METRIC,
    ) -> list[HourlyForecast]:
        return []

    async def forecast_daily(
        self,
        location: GeoLocation,
        *,
        days: int = 7,
        units: WeatherUnits = WeatherUnits.METRIC,
    ) -> list[DailyForecast]:
        return []


# ── Test fixtures ────────────────────────────────────────────────────


_TEST_LOC = GeoLocation(
    latitude=41.4993,
    longitude=-81.6944,
    name="Cleveland, OH, USA",
    timezone="America/New_York",
    country_code="US",
)


class _FakeNotificationProvider:
    """Records notify_user calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def notify_user(
        self,
        *,
        user_id: str,
        message: str,
        urgency: NotificationUrgency = NotificationUrgency.NORMAL,
        source: str = "system",
        source_ref: dict[str, Any] | None = None,
    ) -> Notification:
        self.calls.append(
            {
                "user_id": user_id,
                "message": message,
                "urgency": urgency,
                "source": source,
                "source_ref": source_ref,
            }
        )
        return Notification(
            id="n1",
            user_id=user_id,
            source=source,
            message=message,
            urgency=urgency,
            created_at=datetime.now(UTC),
        )


class _FakeSpeakerProvider:
    """Minimal speaker provider that records announce calls."""

    def __init__(self) -> None:
        self.announce_calls: list[dict[str, Any]] = []

    @property
    def backends(self) -> dict[str, Any]:
        return {}

    def get_backend(self, name: str) -> Any:
        return None

    async def resolve_names(self, names: list[str]) -> dict[str, str]:
        return {}

    async def announce(
        self,
        text: str,
        speaker_names: list[str] | None = None,
        volume: int | None = None,
        context: str = "",
    ) -> str:
        self.announce_calls.append(
            {"text": text, "speakers": speaker_names, "context": context}
        )
        return "ok"


async def _build_service(
    *,
    sqlite_storage: SQLiteStorage,
    backend: WeatherBackend | None = None,
    enabled: bool = True,
    config_section: dict[str, Any] | None = None,
    with_notifications: bool = False,
    with_scheduler: bool = False,
    with_event_bus: bool = True,
    with_speaker: bool = False,
) -> tuple[WeatherService, dict[str, Any]]:
    """Construct and start a ``WeatherService`` ready for tests."""
    backend = backend or FakeWeatherBackend()

    # Register the fake backend class — always idempotent in the registry.
    section = {"enabled": enabled, "backend": backend.backend_name}
    if config_section:
        section.update(config_section)

    class _ConfigSvc:
        def __init__(self, section: dict[str, Any]) -> None:
            self._section = section

        def get(self, path: str) -> Any:
            return None

        def get_section(self, namespace: str) -> dict[str, Any]:
            return dict(self._section) if namespace == "weather" else {}

        def get_section_safe(self, namespace: str) -> dict[str, Any]:
            return self.get_section(namespace)

        async def set(self, path: str, value: Any) -> dict[str, Any]:
            return self._section

    storage_provider = _FakeStorageProvider(sqlite_storage)
    event_bus_provider = _FakeEventBusProvider() if with_event_bus else None
    notifications = _FakeNotificationProvider() if with_notifications else None
    scheduler = _FakeSchedulerProvider() if with_scheduler else None
    speaker = _FakeSpeakerProvider() if with_speaker else None
    config_svc = _ConfigSvc(section)

    caps: dict[str, Any] = {
        "entity_storage": storage_provider,
        "configuration": config_svc,
    }
    if event_bus_provider is not None:
        caps["event_bus"] = event_bus_provider
    if notifications is not None:
        caps["notifications"] = notifications
    if scheduler is not None:
        caps["scheduler"] = scheduler
    if speaker is not None:
        caps["speaker_control"] = speaker

    resolver = _make_resolver(**caps)
    svc = WeatherService()

    # Substitute the stub backend (avoid touching the real registry).
    # Walk the registry to ensure our fake is registered.
    registry = WeatherBackend.registered_backends()
    assert backend.backend_name in registry, (
        f"FakeWeatherBackend should be registered as {backend.backend_name!r}"
    )

    await svc.start(resolver)

    # The service instantiates its own backend instance from the registry;
    # for tests we want assertions to see the EXACT instance the test
    # passed in, so swap it after start. The fresh instance was already
    # initialized — we close it first to mirror real lifecycle.
    if svc._enabled and svc._backend is not None and svc._backend is not backend:
        original = svc._backend
        await original.close()
        svc._backend = backend
        # Re-run initialize on the test's instance with the same config.
        await backend.initialize(svc._settings or {})
        # And re-resolve the geocoder pointer to the swapped backend.
        await svc._resolve_geocoder()

    handles: dict[str, Any] = {
        "storage": storage_provider,
        "event_bus": event_bus_provider,
        "notifications": notifications,
        "scheduler": scheduler,
        "speaker": speaker,
        "resolver": resolver,
    }
    return svc, handles


# ── Cache tests ──────────────────────────────────────────────────────


class TestWeatherCache:
    @pytest.mark.asyncio
    async def test_first_call_misses_then_cached(self) -> None:
        cache = WeatherCache(max_entries=8)
        calls = 0

        async def loader() -> str:
            nonlocal calls
            calls += 1
            return f"value-{calls}"

        v1, stale1 = await cache.get_or_fetch("k", 60, loader)
        v2, stale2 = await cache.get_or_fetch("k", 60, loader)
        assert v1 == "value-1"
        assert v2 == "value-1"
        assert calls == 1
        assert stale1 == 0.0
        # Second call's stale_seconds is the elapsed monotonic time
        assert stale2 >= 0.0

    @pytest.mark.asyncio
    async def test_concurrent_callers_single_flight(self) -> None:
        cache = WeatherCache(max_entries=8)
        gate = asyncio.Event()
        calls = 0

        async def loader() -> str:
            nonlocal calls
            calls += 1
            await gate.wait()
            return "shared"

        t1 = asyncio.create_task(cache.get_or_fetch("same-key", 60, loader))
        t2 = asyncio.create_task(cache.get_or_fetch("same-key", 60, loader))
        # Let both tasks subscribe before resolving.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        gate.set()
        v1, _ = await t1
        v2, _ = await t2
        assert v1 == "shared"
        assert v2 == "shared"
        assert calls == 1
        assert cache.inflight_size() == 0

    @pytest.mark.asyncio
    async def test_lru_eviction_at_max_entries(self) -> None:
        cache = WeatherCache(max_entries=4)
        for i in range(5):
            async def loader(i: int = i) -> int:
                return i

            await cache.get_or_fetch(f"k{i}", 60, loader)
        assert cache.size() == 4
        # The first inserted key should have been evicted (LRU).
        # Re-fetching it triggers a new loader call.
        called = {"n": 0}

        async def loader2() -> int:
            called["n"] += 1
            return 99

        await cache.get_or_fetch("k0", 60, loader2)
        assert called["n"] == 1

    @pytest.mark.asyncio
    async def test_loader_failure_does_not_leak_inflight(self) -> None:
        cache = WeatherCache(max_entries=4)

        async def loader() -> int:
            raise WeatherUnavailableError("kaboom")

        with pytest.raises(WeatherUnavailableError):
            await cache.get_or_fetch("k", 60, loader)
        assert cache.inflight_size() == 0
        # No stale-on-failure: cache empty for this key.
        assert cache.size() == 0

    @pytest.mark.asyncio
    async def test_invalidate_prefix(self) -> None:
        cache = WeatherCache(max_entries=8)

        async def loader(value: int) -> int:
            return value

        for k in ("a:1", "a:2", "b:1"):
            await cache.get_or_fetch(k, 60, lambda v=k: loader(1))
        cache.invalidate_prefix("a:")
        assert cache.size() == 1

    @pytest.mark.asyncio
    async def test_make_key_includes_backend_name(self) -> None:
        loc = GeoLocation(latitude=10.0, longitude=20.0)
        k1 = WeatherCache.make_key("open-meteo", "current", loc, WeatherUnits.METRIC)
        k2 = WeatherCache.make_key("nws", "current", loc, WeatherUnits.METRIC)
        assert k1 != k2
        assert "open-meteo" in k1
        assert "nws" in k2


# ── Severity ordering ────────────────────────────────────────────────


class TestSeverityRank:
    def test_extreme_outranks_severe(self) -> None:
        assert severity_rank(AlertSeverity.EXTREME) > severity_rank(AlertSeverity.SEVERE)

    def test_lexicographic_compare_would_be_wrong(self) -> None:
        # The whole point of severity_rank: "severe" > "extreme" lexicographically.
        assert AlertSeverity.SEVERE.value > AlertSeverity.EXTREME.value
        # But severity_rank gets the order right.
        assert severity_rank(AlertSeverity.EXTREME) > severity_rank(AlertSeverity.SEVERE)
        assert severity_rank(AlertSeverity.SEVERE) > severity_rank(AlertSeverity.MODERATE)
        assert severity_rank(AlertSeverity.MODERATE) > severity_rank(AlertSeverity.MINOR)


# ── Service lifecycle ────────────────────────────────────────────────


class TestServiceLifecycle:
    @pytest.mark.asyncio
    async def test_disabled_service_does_not_initialize_backend(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        backend = FakeWeatherBackend()
        svc, _ = await _build_service(
            sqlite_storage=sqlite_storage,
            backend=backend,
            enabled=False,
        )
        try:
            assert backend.initialized_with is None
            assert svc._backend is None
            assert svc.get_tools() == []
        finally:
            await svc.stop()

    @pytest.mark.asyncio
    async def test_enabled_service_initializes_backend(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        backend = FakeWeatherBackend()
        svc, _ = await _build_service(
            sqlite_storage=sqlite_storage,
            backend=backend,
            enabled=True,
            config_section={"settings": {"foo": "bar"}},
        )
        try:
            assert backend.initialized_with == {"foo": "bar"}
            assert svc._backend is backend
        finally:
            await svc.stop()
        assert backend.closed is True


# ── Cache + service ──────────────────────────────────────────────────


class TestServiceCache:
    @pytest.mark.asyncio
    async def test_get_current_caches_result(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        backend = FakeWeatherBackend()
        svc, _ = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        try:
            await svc._save_home_location(_TEST_LOC)
            cw1 = await svc.get_current()
            cw2 = await svc.get_current()
            assert cw1.temperature == 18.4
            assert cw2.temperature == 18.4
            assert backend.current_calls == 1
        finally:
            await svc.stop()

    @pytest.mark.asyncio
    async def test_two_concurrent_get_current_one_backend_call(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        backend = FakeWeatherBackend()
        svc, _ = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        try:
            await svc._save_home_location(_TEST_LOC)
            backend.gate = asyncio.Event()
            t1 = asyncio.create_task(svc.get_current())
            t2 = asyncio.create_task(svc.get_current())
            # Let both subscribe before unblocking.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            backend.gate.set()
            cw1 = await t1
            cw2 = await t2
            assert cw1.temperature == cw2.temperature
            assert backend.current_calls == 1
        finally:
            await svc.stop()

    @pytest.mark.asyncio
    async def test_no_silent_stale_on_failure(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        backend = FakeWeatherBackend()
        svc, _ = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        try:
            await svc._save_home_location(_TEST_LOC)
            await svc.get_current()  # populate
            # Force expiry by clearing TTL cache outright.
            svc._cache.clear()
            backend.fail_with = WeatherUnavailableError("503", provider_status=503)
            with pytest.raises(WeatherUnavailableError):
                await svc.get_current()
        finally:
            await svc.stop()


# ── Resolution ───────────────────────────────────────────────────────


class TestLocationResolution:
    @pytest.mark.asyncio
    async def test_per_user_overrides_default(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        backend = FakeWeatherBackend()
        svc, _ = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        try:
            await svc._save_home_location(_TEST_LOC)
            user_loc = GeoLocation(latitude=51.5, longitude=-0.13, name="London")
            await svc._save_user_location("u1", user_loc)
            user_ctx = UserContext(user_id="u1", email="", display_name="Alice")
            resolved = await svc.resolve_location(user_ctx)
            assert resolved is not None
            assert resolved.latitude == pytest.approx(51.5)
        finally:
            await svc.stop()

    @pytest.mark.asyncio
    async def test_no_per_user_falls_back_to_default(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        backend = FakeWeatherBackend()
        svc, _ = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        try:
            await svc._save_home_location(_TEST_LOC)
            user_ctx = UserContext(user_id="u2", email="", display_name="Bob")
            resolved = await svc.resolve_location(user_ctx)
            assert resolved is not None
            assert resolved.latitude == pytest.approx(_TEST_LOC.latitude)
        finally:
            await svc.stop()

    @pytest.mark.asyncio
    async def test_no_location_anywhere_raises_typed_error(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        backend = FakeWeatherBackend()
        svc, _ = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        try:
            with pytest.raises(LocationNotConfiguredError):
                await svc.get_current()
        finally:
            await svc.stop()

    @pytest.mark.asyncio
    async def test_units_resolution_user_overrides_default(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        backend = FakeWeatherBackend()
        svc, _ = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        try:
            await svc._save_user_units("u1", WeatherUnits.IMPERIAL)
            ctx = UserContext(user_id="u1", email="", display_name="Alice")
            assert (await svc.resolve_units(ctx)) is WeatherUnits.IMPERIAL
            ctx2 = UserContext(user_id="u2", email="", display_name="Bob")
            assert (await svc.resolve_units(ctx2)) is WeatherUnits.METRIC
        finally:
            await svc.stop()


# ── Tool execution ───────────────────────────────────────────────────


class TestToolExecution:
    @pytest.mark.asyncio
    async def test_current_weather_returns_full_payload(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        backend = FakeWeatherBackend()
        svc, _ = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        try:
            await svc._save_home_location(_TEST_LOC)
            raw = await svc.execute_tool("current_weather", {})
            payload = json.loads(raw)
            assert "summary" in payload
            assert payload["temperature"] == pytest.approx(18.4)
            assert payload["condition"] == WeatherCondition.CLOUDY.value
            assert payload["units"] == "metric"
            assert payload["source"] == backend.backend_name
            assert "stale_seconds" in payload
        finally:
            await svc.stop()

    @pytest.mark.asyncio
    async def test_current_weather_no_location_returns_structured_error(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        backend = FakeWeatherBackend()
        svc, _ = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        try:
            raw = await svc.execute_tool("current_weather", {})
            payload = json.loads(raw)
            assert payload["error"] == "no_home_location"
        finally:
            await svc.stop()

    @pytest.mark.asyncio
    async def test_forecast_hours_and_days_both_invalid(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        backend = FakeWeatherBackend()
        svc, _ = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        try:
            await svc._save_home_location(_TEST_LOC)
            raw = await svc.execute_tool("forecast", {"hours": 6, "days": 3})
            payload = json.loads(raw)
            assert payload["error"] == "invalid_arguments"
        finally:
            await svc.stop()

    @pytest.mark.asyncio
    async def test_forecast_default_hours(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        backend = FakeWeatherBackend()
        svc, _ = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        try:
            await svc._save_home_location(_TEST_LOC)
            raw = await svc.execute_tool("forecast", {})
            payload = json.loads(raw)
            assert payload["kind"] == "hourly"
            assert isinstance(payload["hours"], list)
            assert len(payload["hours"]) >= 1
        finally:
            await svc.stop()

    @pytest.mark.asyncio
    async def test_forecast_days_branch(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        backend = FakeWeatherBackend()
        svc, _ = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        try:
            await svc._save_home_location(_TEST_LOC)
            raw = await svc.execute_tool("forecast", {"days": 3})
            payload = json.loads(raw)
            assert payload["kind"] == "daily"
            assert len(payload["days"]) == 3
        finally:
            await svc.stop()

    @pytest.mark.asyncio
    async def test_forecast_hours_out_of_range(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        backend = FakeWeatherBackend()
        svc, _ = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        try:
            await svc._save_home_location(_TEST_LOC)
            raw = await svc.execute_tool("forecast", {"hours": 500})
            payload = json.loads(raw)
            assert payload["error"] == "invalid_arguments"
        finally:
            await svc.stop()

    @pytest.mark.asyncio
    async def test_weather_alerts_unsupported_backend(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        backend = FakeWeatherBackend()
        backend.alerts_capability = False
        svc, _ = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        try:
            await svc._save_home_location(_TEST_LOC)
            raw = await svc.execute_tool("weather_alerts", {})
            payload = json.loads(raw)
            assert payload["supported"] is False
            assert payload["alerts"] == []
            assert "Open-Meteo" in payload["reason"] or "alert" in payload["reason"].lower()
        finally:
            await svc.stop()

    @pytest.mark.asyncio
    async def test_weather_alerts_supported_with_data(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        backend = FakeWeatherBackend()
        backend.alerts_capability = True
        backend.alerts_to_return = [
            WeatherAlert(
                alert_id="a1",
                title="Severe Thunderstorm Warning",
                description="Storm",
                severity=AlertSeverity.SEVERE,
                issued_at=datetime.now(UTC),
                expires_at=None,
                source="Test",
            )
        ]
        svc, _ = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        try:
            await svc._save_home_location(_TEST_LOC)
            raw = await svc.execute_tool("weather_alerts", {})
            payload = json.loads(raw)
            assert payload["supported"] is True
            assert len(payload["alerts"]) == 1
            assert payload["alerts"][0]["severity"] == "severe"
        finally:
            await svc.stop()

    @pytest.mark.asyncio
    async def test_geocode_no_results(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        backend = FakeWeatherBackend()
        # Geocoder backend is the active backend; with no results...
        backend.geocode_results = []
        svc, _ = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        try:
            raw = await svc.execute_tool("geocode_location", {"query": "asdfasdf"})
            payload = json.loads(raw)
            assert payload["error"] == "no_results"
        finally:
            await svc.stop()

    @pytest.mark.asyncio
    async def test_geocode_returns_candidates(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        backend = FakeWeatherBackend()
        backend.geocode_results = [
            GeoLocation(latitude=41.5, longitude=-81.7, name="Cleveland, OH"),
            GeoLocation(latitude=35.16, longitude=-84.88, name="Cleveland, TN"),
        ]
        svc, _ = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        try:
            raw = await svc.execute_tool("geocode_location", {"query": "Cleveland"})
            payload = json.loads(raw)
            assert "candidates" in payload
            assert len(payload["candidates"]) == 2
        finally:
            await svc.stop()


# ── Tool surface visibility ──────────────────────────────────────────


class TestToolVisibility:
    @pytest.mark.asyncio
    async def test_set_home_location_is_ai_invisible(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        svc, _ = await _build_service(sqlite_storage=sqlite_storage)
        try:
            tools = svc.get_tools()
            by_name = {t.name: t for t in tools}
            assert "current_weather" in by_name
            assert by_name["current_weather"].ai_visible is True
            assert "set_home_location" in by_name
            assert by_name["set_home_location"].ai_visible is False
            assert "set_units" in by_name
            assert by_name["set_units"].ai_visible is False
        finally:
            await svc.stop()


# ── Per-user prefs round-trip ────────────────────────────────────────


class TestPerUserPrefs:
    @pytest.mark.asyncio
    async def test_set_home_location_via_tool(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        backend = FakeWeatherBackend()
        backend.geocode_results = [
            GeoLocation(latitude=51.5, longitude=-0.13, name="London"),
        ]
        svc, _ = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        try:
            args = {"_user_id": "alice", "query": "London"}
            raw = await svc.execute_tool("set_home_location", args)
            payload = json.loads(raw)
            assert "London" in payload["summary"]
            user = UserContext(user_id="alice", email="", display_name="A")
            resolved = await svc.resolve_location(user)
            assert resolved is not None
            assert resolved.name == "London"
        finally:
            await svc.stop()

    @pytest.mark.asyncio
    async def test_set_units_via_tool(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        backend = FakeWeatherBackend()
        svc, _ = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        try:
            args = {"_user_id": "alice", "units": "imperial"}
            raw = await svc.execute_tool("set_units", args)
            payload = json.loads(raw)
            assert payload["units"] == "imperial"
            ctx = UserContext(user_id="alice", email="", display_name="A")
            assert (await svc.resolve_units(ctx)) is WeatherUnits.IMPERIAL
        finally:
            await svc.stop()


# ── Alert dedup & severe-alert delivery ──────────────────────────────


class TestAlertDelivery:
    @pytest.mark.asyncio
    async def test_cold_boot_with_active_alerts_publishes_nothing(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        """Spec: first sweep after start treats every active alert as
        already-seen, even when no persisted dedup row exists.

        This is the spam-vector guard: Gilbert was down for 3h during a
        tornado warning; on restart, the first poll must NOT republish
        the still-active warning as ``weather.alert.issued``.
        """
        backend = FakeWeatherBackend()
        backend.alerts_capability = True
        backend.alerts_to_return = [
            WeatherAlert(
                alert_id="a1",
                title="Tornado Warning",
                description="Take cover",
                severity=AlertSeverity.EXTREME,
                issued_at=datetime.now(UTC),
                expires_at=None,
            ),
            WeatherAlert(
                alert_id="a2",
                title="Severe Thunderstorm",
                description="wind",
                severity=AlertSeverity.SEVERE,
                issued_at=datetime.now(UTC),
                expires_at=None,
            ),
        ]
        svc, handles = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        try:
            await svc._save_home_location(_TEST_LOC)
            # No pre-seeding: cold boot path. The persisted dedup row
            # is empty (or absent) and active alerts existed before
            # start.
            event_bus: InMemoryEventBus = handles["event_bus"].bus
            seen_events: list[Event] = []
            event_bus.subscribe(
                "weather.alert.issued", lambda e: _record(seen_events, e),
            )

            assert svc._first_sweep_done is False
            await svc._poll_alerts()
            await asyncio.sleep(0)
            # No events fire on the first sweep, even though a1/a2 are
            # genuinely-new from the dedup-row's perspective.
            assert seen_events == []
            assert svc._first_sweep_done is True
            # Both alerts are now in the seen set.
            loc_key = (
                f"{round(_TEST_LOC.latitude, 4)},{round(_TEST_LOC.longitude, 4)}"
            )
            assert svc._known_alert_ids[(loc_key, "system")] == {"a1", "a2"}
        finally:
            await svc.stop()

    @pytest.mark.asyncio
    async def test_second_poll_after_first_sweep_publishes_new_alerts(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        """After the first sweep marks active alerts as seen, the next
        poll must publish only genuinely-new alert ids.
        """
        backend = FakeWeatherBackend()
        backend.alerts_capability = True
        backend.alerts_to_return = [
            WeatherAlert(
                alert_id="a1",
                title="Existing",
                description="x",
                severity=AlertSeverity.MODERATE,
                issued_at=datetime.now(UTC),
                expires_at=None,
            ),
        ]
        svc, handles = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        try:
            await svc._save_home_location(_TEST_LOC)
            event_bus: InMemoryEventBus = handles["event_bus"].bus
            seen_events: list[Event] = []
            event_bus.subscribe(
                "weather.alert.issued", lambda e: _record(seen_events, e),
            )

            # First sweep: a1 active → suppressed.
            await svc._poll_alerts()
            await asyncio.sleep(0)
            assert seen_events == []

            # Second poll: a2 newly active (a1 still active).
            backend.alerts_to_return = [
                WeatherAlert(
                    alert_id="a1",
                    title="Existing",
                    description="x",
                    severity=AlertSeverity.MODERATE,
                    issued_at=datetime.now(UTC),
                    expires_at=None,
                ),
                WeatherAlert(
                    alert_id="a2",
                    title="New Warning",
                    description="new",
                    severity=AlertSeverity.SEVERE,
                    issued_at=datetime.now(UTC),
                    expires_at=None,
                ),
            ]
            await svc._poll_alerts()
            await asyncio.sleep(0)
            assert len(seen_events) == 1
            assert seen_events[0].data["alert_id"] == "a2"
        finally:
            await svc.stop()

    @pytest.mark.asyncio
    async def test_first_poll_after_restart_does_not_publish_existing_alerts(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        """Warm-path: even when persisted dedup has the alert, the first
        sweep still suppresses (and overwrites the seen set with the
        currently-active set).
        """
        backend = FakeWeatherBackend()
        backend.alerts_capability = True
        backend.alerts_to_return = [
            WeatherAlert(
                alert_id="a1",
                title="x",
                description="x",
                severity=AlertSeverity.MODERATE,
                issued_at=datetime.now(UTC),
                expires_at=None,
            ),
        ]
        svc, handles = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        try:
            await svc._save_home_location(_TEST_LOC)
            # Pre-seed dedup as if we'd just restarted with a1 active.
            loc_key = (
                f"{round(_TEST_LOC.latitude, 4)},{round(_TEST_LOC.longitude, 4)}"
            )
            svc._known_alert_ids[(loc_key, "system")] = {"a1"}

            event_bus: InMemoryEventBus = handles["event_bus"].bus
            seen_events: list[Event] = []
            event_bus.subscribe("weather.alert.issued", lambda e: _record(seen_events, e))

            await svc._poll_alerts()
            assert seen_events == []  # a1 was already-seen
        finally:
            await svc.stop()

    @pytest.mark.asyncio
    async def test_new_alert_publishes_event(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        backend = FakeWeatherBackend()
        backend.alerts_capability = True
        svc, handles = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        try:
            await svc._save_home_location(_TEST_LOC)
            # Mark first sweep as already done so this test exercises
            # the warm-path publish branch.
            svc._first_sweep_done = True
            event_bus: InMemoryEventBus = handles["event_bus"].bus
            seen: list[Event] = []
            event_bus.subscribe("weather.alert.issued", lambda e: _record(seen, e))

            backend.alerts_to_return = [
                WeatherAlert(
                    alert_id="new1",
                    title="High Wind Warning",
                    description="Gusts to 70mph",
                    severity=AlertSeverity.SEVERE,
                    issued_at=datetime.now(UTC),
                    expires_at=None,
                ),
            ]
            await svc._poll_alerts()
            await asyncio.sleep(0)  # let event subscribers run
            assert len(seen) == 1
            assert seen[0].data["alert_id"] == "new1"
        finally:
            await svc.stop()

    @pytest.mark.asyncio
    async def test_severe_alert_invalidates_current_cache(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        backend = FakeWeatherBackend()
        backend.alerts_capability = True
        svc, _ = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        try:
            await svc._save_home_location(_TEST_LOC)
            # Mark first sweep as already done so the severe-alert
            # publish branch (and cache invalidation) actually runs.
            svc._first_sweep_done = True
            await svc.get_current()  # populate cache
            assert svc._cache.size() == 1

            backend.alerts_to_return = [
                WeatherAlert(
                    alert_id="s1",
                    title="t",
                    description="d",
                    severity=AlertSeverity.SEVERE,
                    issued_at=datetime.now(UTC),
                    expires_at=None,
                ),
            ]
            await svc._poll_alerts()
            assert svc._cache.size() == 0
        finally:
            await svc.stop()

    @pytest.mark.asyncio
    async def test_alert_event_calls_notification_provider(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        backend = FakeWeatherBackend()
        svc, handles = await _build_service(
            sqlite_storage=sqlite_storage,
            backend=backend,
            with_notifications=True,
        )
        try:
            await svc._save_home_location(_TEST_LOC)
            await svc._save_user_location("alice", _TEST_LOC)
            event = Event(
                event_type="weather.alert.issued",
                data={
                    "alert_id": "x",
                    "title": "Tornado Warning",
                    "description": "Take cover",
                    "severity": "extreme",
                },
                source="weather",
            )
            await svc._on_alert_event(event)
            assert len(handles["notifications"].calls) == 1
            call = handles["notifications"].calls[0]
            assert call["user_id"] == "alice"
            assert call["urgency"] is NotificationUrgency.URGENT
        finally:
            await svc.stop()

    @pytest.mark.asyncio
    async def test_voice_announce_only_at_minimum_severity(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        backend = FakeWeatherBackend()
        svc, handles = await _build_service(
            sqlite_storage=sqlite_storage,
            backend=backend,
            config_section={
                "alert_voice_enabled": True,
                "alert_voice_minimum": "extreme",
            },
            with_speaker=True,
        )
        try:
            speaker = handles["speaker"]
            severe_event = Event(
                event_type="weather.alert.issued",
                data={
                    "alert_id": "x",
                    "title": "Severe Thunderstorm Warning",
                    "description": "wind",
                    "severity": "severe",
                },
                source="weather",
            )
            await svc._on_alert_event(severe_event)
            assert speaker.announce_calls == []  # below minimum

            extreme_event = Event(
                event_type="weather.alert.issued",
                data={
                    "alert_id": "y",
                    "title": "Tornado Warning",
                    "description": "Take cover",
                    "severity": "extreme",
                },
                source="weather",
            )
            await svc._on_alert_event(extreme_event)
            assert len(speaker.announce_calls) == 1
        finally:
            await svc.stop()

    @pytest.mark.asyncio
    async def test_alert_dedup_persists_across_stop(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        """Dedup state is the source of truth across restart.

        Asserts (a) keys are written to storage, (b) keys persist
        across a simulated restart and survive into the new instance's
        in-memory ``_known_alert_ids``, (c) duplicate alerts on a
        post-first-sweep poll do not re-publish.
        """
        backend = FakeWeatherBackend()
        backend.alerts_capability = True
        svc, _ = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        loc_key = (
            f"{round(_TEST_LOC.latitude, 4)},{round(_TEST_LOC.longitude, 4)}"
        )
        try:
            await svc._save_home_location(_TEST_LOC)
            # First sweep: persist x1 and y2 as already-seen.
            backend.alerts_to_return = [
                WeatherAlert(
                    alert_id="x1",
                    title="t",
                    description="d",
                    severity=AlertSeverity.MODERATE,
                    issued_at=datetime.now(UTC),
                    expires_at=None,
                ),
                WeatherAlert(
                    alert_id="y2",
                    title="t",
                    description="d",
                    severity=AlertSeverity.MINOR,
                    issued_at=datetime.now(UTC),
                    expires_at=None,
                ),
            ]
            await svc._poll_alerts()
        finally:
            await svc.stop()

        # (a) Row was written with the actual seen set.
        rows = await sqlite_storage.list_collections()
        dedup_collections = [c for c in rows if c.endswith("alert_dedup")]
        assert dedup_collections, "alert_dedup collection should exist"
        row = await sqlite_storage.get(
            dedup_collections[0], f"system:{loc_key}",
        )
        assert row is not None
        assert set(row["seen_alert_ids"]) == {"x1", "y2"}
        assert row["location_key"] == loc_key
        assert row["scope_id"] == "system"

        # (b) Restart: a fresh service loads the persisted dedup state.
        backend2 = FakeWeatherBackend()
        backend2.alerts_capability = True
        backend2.alerts_to_return = [
            WeatherAlert(
                alert_id="x1",  # still active
                title="t",
                description="d",
                severity=AlertSeverity.MODERATE,
                issued_at=datetime.now(UTC),
                expires_at=None,
            ),
            WeatherAlert(
                alert_id="y2",  # still active
                title="t",
                description="d",
                severity=AlertSeverity.MINOR,
                issued_at=datetime.now(UTC),
                expires_at=None,
            ),
        ]
        svc2, handles2 = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend2,
        )
        try:
            # Persisted state was loaded into memory.
            assert svc2._known_alert_ids[(loc_key, "system")] == {"x1", "y2"}

            event_bus: InMemoryEventBus = handles2["event_bus"].bus
            seen_events: list[Event] = []
            event_bus.subscribe(
                "weather.alert.issued", lambda e: _record(seen_events, e),
            )

            # First sweep after restart: still suppresses (no events).
            await svc2._poll_alerts()
            await asyncio.sleep(0)
            assert seen_events == []

            # (c) Second poll with the same alerts → no re-publish.
            await svc2._poll_alerts()
            await asyncio.sleep(0)
            assert seen_events == []
        finally:
            await svc2.stop()


def _record(target: list[Event], event: Event) -> Any:
    async def _async() -> None:
        target.append(event)

    return _async()


# ── Geocoder fallback ────────────────────────────────────────────────


class TestGeocoderFallback:
    @pytest.mark.asyncio
    async def test_active_backend_geocoder_used(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        backend = FakeWeatherBackend()
        svc, _ = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        try:
            assert svc._geocoder_backend is backend
        finally:
            await svc.stop()

    @pytest.mark.asyncio
    async def test_no_geocoder_backend_returns_geocoding_unavailable(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        # Use a backend with no geocode override AND ensure no other
        # registered backend has geocode (we'd need to patch the
        # registry). Instead, after start we manually clear the
        # geocoder backend and call the tool.
        backend = FakeWeatherBackend()
        svc, _ = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        try:
            svc._geocoder_backend = None
            raw = await svc.execute_tool("geocode_location", {"query": "London"})
            payload = json.loads(raw)
            assert payload["error"] == "geocoding_unavailable"
        finally:
            await svc.stop()


# ── Provider protocol identity contract ──────────────────────────────


class TestWeatherProviderProtocol:
    @pytest.mark.asyncio
    async def test_service_satisfies_weather_provider(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        backend = FakeWeatherBackend()
        svc, _ = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        try:
            assert isinstance(svc, WeatherProvider)
        finally:
            await svc.stop()

    @pytest.mark.asyncio
    async def test_get_current_with_explicit_user(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        backend = FakeWeatherBackend()
        svc, _ = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        try:
            user_loc = GeoLocation(latitude=51.5, longitude=-0.13, name="London")
            await svc._save_user_location("alice", user_loc)
            ctx = UserContext(user_id="alice", email="", display_name="A")
            cw = await svc.get_current(user=ctx)
            assert cw.location.name == "London"
        finally:
            await svc.stop()


# ── Daily digest ──────────────────────────────────────────────────────


class TestDailyDigest:
    @pytest.mark.asyncio
    async def test_publish_digest_fires_event_with_expected_shape(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        """The digest fires once when invoked and its payload contains
        the expected ``current`` / ``hourly`` / ``daily`` / ``location``
        / ``units`` / ``source`` keys.
        """
        backend = FakeWeatherBackend()
        svc, handles = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        try:
            await svc._save_home_location(_TEST_LOC)
            event_bus: InMemoryEventBus = handles["event_bus"].bus
            seen: list[Event] = []
            event_bus.subscribe("weather.digest", lambda e: _record(seen, e))

            await svc._publish_digest()
            await asyncio.sleep(0)

            assert len(seen) == 1
            payload = seen[0].data
            assert "current" in payload
            assert "hourly" in payload
            assert "daily" in payload
            assert "location" in payload
            assert payload["units"] == "metric"
            assert payload["source"] == backend.backend_name
            # Sanity-check substructure
            assert isinstance(payload["hourly"], list)
            assert isinstance(payload["daily"], list)
            assert payload["current"]["temperature"] == 18.4
        finally:
            await svc.stop()

    @pytest.mark.asyncio
    async def test_publish_digest_does_not_double_fire_same_day(
        self, sqlite_storage: SQLiteStorage,
    ) -> None:
        """Idempotency contract: invoking ``_publish_digest`` twice in
        the same calendar day fires the ``weather.digest`` event
        exactly once. Protects against config-reload / scheduler
        re-register firing the job twice on the same day.
        """
        backend = FakeWeatherBackend()
        svc, handles = await _build_service(
            sqlite_storage=sqlite_storage, backend=backend,
        )
        try:
            await svc._save_home_location(_TEST_LOC)
            event_bus: InMemoryEventBus = handles["event_bus"].bus
            seen: list[Event] = []
            event_bus.subscribe("weather.digest", lambda e: _record(seen, e))

            await svc._publish_digest()
            await asyncio.sleep(0)
            await svc._publish_digest()
            await asyncio.sleep(0)

            assert len(seen) == 1
        finally:
            await svc.stop()


# ── ConfigParam declarations ─────────────────────────────────────────


class TestGreetingWeatherHintTemplateConfig:
    def test_weather_hint_template_is_ai_prompt(self) -> None:
        """The greeting service's ``weather_hint_template`` is fed into
        the AI greeting prompt, so per the "AI Prompts Are Always
        Configurable" rule it must declare ``ai_prompt=True`` to expose
        the Author-with-AI affordance to operators.
        """
        from gilbert.core.services.greeting import GreetingService

        params = GreetingService().config_params()
        by_key = {p.key: p for p in params}
        assert "weather_hint_template" in by_key
        assert by_key["weather_hint_template"].ai_prompt is True
        assert by_key["weather_hint_template"].multiline is True
