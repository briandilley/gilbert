"""Test fakes for HealthService scenarios.

Mirrors the existing ``tests/unit/_fakes/`` convention so other tests
in the suite can re-use a deterministic backend rather than mocking
internals of ``HealthService``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from gilbert.interfaces.health import (
    HealthBackend,
    HealthMetric,
    LinkCompleteResult,
    LinkStartResult,
    MetricType,
    MetricUnit,
)


class FakeHealthBackend(HealthBackend):
    """A push-style backend that returns whatever metrics tests inject.

    Use ``parse_webhook_returns`` to control what the backend yields on
    a given delivery; ``_initialize_calls`` and ``_close_calls`` count
    lifecycle hits so tests can assert clean-up.
    """

    backend_name = "_fake_health"

    def __init__(self) -> None:
        self.parse_webhook_returns: list[HealthMetric] = []
        self.parse_webhook_raises: Exception | None = None
        self.sync_returns: list[HealthMetric] = []
        self.sync_raises: Exception | None = None
        self._supports_pull = False
        self._supports_push = True
        self._initialize_calls = 0
        self._close_calls = 0
        self.disconnect_calls: list[str] = []
        self.disconnect_raises: Exception | None = None

    @property
    def supports_pull(self) -> bool:
        return self._supports_pull

    @property
    def supports_push(self) -> bool:
        return self._supports_push

    async def initialize(self, config: dict[str, Any]) -> None:
        self._initialize_calls += 1

    async def close(self) -> None:
        self._close_calls += 1

    async def parse_webhook(
        self,
        user_id: str,
        body: bytes,
        headers: dict[str, str],
    ) -> list[HealthMetric]:
        if self.parse_webhook_raises is not None:
            raise self.parse_webhook_raises
        return list(self.parse_webhook_returns)

    async def sync(
        self,
        user_id: str,
        *,
        since: datetime | None = None,
    ) -> list[HealthMetric]:
        if self.sync_raises is not None:
            raise self.sync_raises
        return list(self.sync_returns)

    async def begin_link(self, user_id: str) -> LinkStartResult:
        return LinkStartResult(status="ok", webhook_url="https://example/test")

    async def complete_link(
        self,
        user_id: str,
        payload: dict[str, Any],
    ) -> LinkCompleteResult:
        return LinkCompleteResult(status="ok")

    async def disconnect(self, user_id: str) -> None:
        self.disconnect_calls.append(user_id)
        if self.disconnect_raises is not None:
            raise self.disconnect_raises

    def supported_metrics(self) -> set[MetricType]:
        return {MetricType.STEPS, MetricType.WEIGHT, MetricType.SLEEP_DURATION}


def make_metric(
    *,
    user_id: str = "u1",
    backend: str = "_fake_health",
    metric_type: MetricType = MetricType.STEPS,
    value: float = 8431.0,
    unit: MetricUnit = MetricUnit.COUNT,
    recorded_at: datetime | None = None,
    source_event_id: str = "",
    extra: dict[str, str] | None = None,
) -> HealthMetric:
    return HealthMetric(
        id="",
        user_id=user_id,
        backend=backend,
        metric_type=metric_type,
        value=value,
        unit=unit,
        recorded_at=recorded_at or datetime.now(UTC),
        ingested_at=datetime.now(UTC),
        source_event_id=source_event_id,
        extra=dict(extra or {}),
    )

