"""Tests for the greeting → health integration.

Per spec §14 v1 ships the *informed* part of the marketing example —
the greeting prompt receives a structured ``GreetingBrief`` snapshot
when the HealthProvider capability is present and the greeted user
has data. The *causal-action* part ("I dimmed the meeting reminders")
is v2 and out of scope.

Coverage:
- The brief-fetch wires through ``HealthProvider`` only — no
  isinstance against the concrete service class.
- A user with no health_links yields ``GreetingBrief.empty(user_id)``
  so the greeting prompt sees an absent-data signal.
- Errors from the provider degrade silently (greeting still goes
  out, just without the health line).
"""

from __future__ import annotations

from typing import Any

from gilbert.core.services.greeting import GreetingService
from gilbert.interfaces.health import (
    GreetingBrief,
    HealthProvider,
    MetricUnit,
)


class _FakeHealth:
    """Satisfies the ``HealthProvider`` Protocol structurally."""

    def __init__(self, brief: GreetingBrief, *, raise_on_call: bool = False) -> None:
        self._brief = brief
        self._raise = raise_on_call
        self.calls: list[str] = []

    async def read_metrics(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    async def latest_metric(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def aggregate(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    async def latest_daily_summary(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def health_brief_for_greeting(self, user_id: str) -> GreetingBrief:
        self.calls.append(user_id)
        if self._raise:
            raise RuntimeError("health blew up")
        return self._brief


def test_health_provider_protocol_satisfied() -> None:
    fake = _FakeHealth(GreetingBrief.empty("u1"))
    assert isinstance(fake, HealthProvider)


async def test_fetch_health_brief_returns_provider_value() -> None:
    svc = GreetingService()
    brief = GreetingBrief(
        user_id="alice",
        has_data=True,
        sleep_hours=5.0,
        sleep_efficiency=None,
        steps_today_so_far=1234,
        weight_latest=80.5,
        weight_unit=MetricUnit.KG,
        resting_hr_latest=60.0,
        flags=["low_sleep"],
    )
    svc._health = _FakeHealth(brief)
    svc._include_health_brief = True
    result = await svc._fetch_health_brief("alice")
    assert result is brief


async def test_fetch_health_brief_returns_none_when_provider_absent() -> None:
    svc = GreetingService()
    svc._health = None
    svc._include_health_brief = True
    result = await svc._fetch_health_brief("alice")
    assert result is None


async def test_fetch_health_brief_swallows_provider_errors() -> None:
    svc = GreetingService()
    svc._health = _FakeHealth(
        GreetingBrief.empty("alice"), raise_on_call=True
    )
    svc._include_health_brief = True
    # No raise — greeting must still go out.
    result = await svc._fetch_health_brief("alice")
    assert result is None


def test_format_health_brief_renders_headline_values() -> None:
    brief = GreetingBrief(
        user_id="alice",
        has_data=True,
        sleep_hours=5.0,
        sleep_efficiency=None,
        steps_today_so_far=1234,
        weight_latest=80.5,
        weight_unit=MetricUnit.KG,
        resting_hr_latest=60.0,
        flags=["low_sleep"],
    )
    text = GreetingService._format_health_brief(brief)
    assert "5.0h" in text
    assert "1,234" in text
    assert "80.5 kg" in text
    assert "60 bpm" in text
    assert "low_sleep" in text


def test_format_health_brief_empty_for_empty_brief() -> None:
    assert GreetingService._format_health_brief(None) == ""
    assert GreetingService._format_health_brief(GreetingBrief.empty("u1")) == ""

