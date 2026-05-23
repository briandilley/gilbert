"""Tests for the greeting → health integration.

The old ``_fetch_health_brief`` / ``_format_health_brief`` plumbing was
removed in Task 6 — health context is now contributed by HealthService
implementing GreetingContextProvider (Task 4). The protocol-satisfaction
test is kept as a regression guard.
"""

from __future__ import annotations

from typing import Any

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
