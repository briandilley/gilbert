"""Tests for ``gilbert.interfaces.health`` — pure-function and dataclass tests.

The auth matrix per §16.3, the parser caps + future-tolerance + unknown-
type rejection, and the ``DEFAULT_AGGREGATOR`` completeness check all
live here so they can fail fast without booting any service.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.health import (
    DEFAULT_AGGREGATOR,
    EXTRA_MAX_KEY_LEN,
    EXTRA_MAX_KEYS,
    EXTRA_MAX_TOTAL_BYTES,
    EXTRA_MAX_VALUE_LEN,
    HEALTH_ADMIN_ROLE,
    AggregatorKind,
    GreetingBrief,
    HealthBackend,
    HealthBackendAuthError,
    HealthBackendError,
    HealthBackendNotFoundError,
    HealthBackendRateLimitError,
    HealthBackendTransientError,
    HealthMetric,
    MetricPayloadError,
    MetricType,
    MetricUnit,
    can_mutate_metrics,
    can_read_metrics,
    parse_metric_payload,
)

# ── DEFAULT_AGGREGATOR completeness ──────────────────────────────────


def test_default_aggregator_covers_every_metric_type() -> None:
    """No silent behavior drift: every MetricType has an explicit entry."""
    missing = [m for m in MetricType if m not in DEFAULT_AGGREGATOR]
    assert missing == [], f"Missing default aggregators: {missing}"
    # Every value is a valid aggregator kind.
    for kind in DEFAULT_AGGREGATOR.values():
        assert isinstance(kind, AggregatorKind)


# ── Auth matrix per §16.3 ────────────────────────────────────────────


def _make_user(user_id: str, roles: frozenset[str] = frozenset()) -> UserContext:
    return UserContext(
        user_id=user_id,
        email="",
        display_name=user_id,
        roles=roles,
    )


@pytest.mark.parametrize(
    "actor_id,target_id,is_health_admin,expected",
    [
        ("alice", "alice", False, True),
        ("alice", "bob", False, False),
        ("alice", "bob", True, True),
        ("admin", "bob", False, False),
        ("admin", "bob", True, True),
        ("system", "bob", False, True),
    ],
)
def test_can_read_metrics_matrix(
    actor_id: str,
    target_id: str,
    is_health_admin: bool,
    expected: bool,
) -> None:
    if actor_id == "system":
        actor = UserContext.SYSTEM
    else:
        actor = _make_user(actor_id)
    assert (
        can_read_metrics(actor, target_id, is_health_admin=is_health_admin) is expected
    )


def test_can_mutate_metrics_owner_only() -> None:
    alice = _make_user("alice")
    assert can_mutate_metrics(alice, "alice") is True
    assert can_mutate_metrics(alice, "bob") is False
    # Even health-admin cannot mutate another user's metrics.
    health_admin = _make_user("admin", frozenset({HEALTH_ADMIN_ROLE}))
    assert can_mutate_metrics(health_admin, "bob") is False
    # SYSTEM bypasses for cascade work.
    assert can_mutate_metrics(UserContext.SYSTEM, "bob") is True


# ── parse_metric_payload — happy path + caps + clock skew ────────────


def _now() -> datetime:
    return datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)


def test_parse_metric_happy() -> None:
    raw = {
        "type": "steps",
        "value": 8431,
        "unit": "count",
        "recorded_at": "2026-05-09T11:00:00+00:00",
    }
    metric = parse_metric_payload(raw, user_id="u1", backend="hk-webhook", now=_now())
    assert metric.metric_type is MetricType.STEPS
    assert metric.value == 8431.0
    assert metric.unit is MetricUnit.COUNT
    assert metric.recorded_at == datetime(2026, 5, 9, 11, 0, 0, tzinfo=UTC)
    assert metric.user_id == "u1"
    assert metric.backend == "hk-webhook"


def test_parse_metric_naive_recorded_at_treated_as_utc() -> None:
    raw = {
        "type": "weight",
        "value": 80.5,
        "unit": "kg",
        "recorded_at": "2026-05-09T07:00:00",
    }
    metric = parse_metric_payload(raw, now=_now())
    assert metric.recorded_at.tzinfo is not None


def test_parse_metric_future_within_tolerance_ok() -> None:
    raw = {
        "type": "weight",
        "value": 80.5,
        "unit": "kg",
        "recorded_at": (_now() + timedelta(minutes=30)).isoformat(),
    }
    parse_metric_payload(raw, now=_now())  # no raise


def test_parse_metric_far_future_rejected() -> None:
    raw = {
        "type": "weight",
        "value": 80.5,
        "unit": "kg",
        "recorded_at": (_now() + timedelta(hours=2)).isoformat(),
    }
    with pytest.raises(MetricPayloadError, match="future"):
        parse_metric_payload(raw, now=_now())


def test_parse_metric_unknown_type_rejected() -> None:
    raw = {
        "type": "alien_radiation_dose",
        "value": 1.0,
        "unit": "ms",
        "recorded_at": _now().isoformat(),
    }
    with pytest.raises(MetricPayloadError, match="unknown metric type"):
        parse_metric_payload(raw, now=_now())


def test_parse_metric_unknown_unit_rejected() -> None:
    raw = {
        "type": "weight",
        "value": 80.0,
        "unit": "stone",
        "recorded_at": _now().isoformat(),
    }
    with pytest.raises(MetricPayloadError, match="unknown unit"):
        parse_metric_payload(raw, now=_now())


def test_parse_metric_non_numeric_rejected() -> None:
    raw = {
        "type": "weight",
        "value": "heavy",
        "unit": "kg",
        "recorded_at": _now().isoformat(),
    }
    with pytest.raises(MetricPayloadError, match="not numeric"):
        parse_metric_payload(raw, now=_now())


def test_parse_metric_missing_recorded_at_rejected() -> None:
    raw = {"type": "weight", "value": 80.0, "unit": "kg"}
    with pytest.raises(MetricPayloadError, match="recorded_at"):
        parse_metric_payload(raw, now=_now())


def test_parse_metric_extra_caps_keys() -> None:
    """Over-cap keys are dropped, kept ones survive."""
    extra_raw = {f"k{i}": "v" for i in range(EXTRA_MAX_KEYS + 5)}
    raw = {
        "type": "weight",
        "value": 80.0,
        "unit": "kg",
        "recorded_at": _now().isoformat(),
        "extra": extra_raw,
    }
    metric = parse_metric_payload(raw, now=_now())
    assert len(metric.extra) <= EXTRA_MAX_KEYS


def test_parse_metric_extra_caps_value_length() -> None:
    raw = {
        "type": "weight",
        "value": 80.0,
        "unit": "kg",
        "recorded_at": _now().isoformat(),
        "extra": {"long": "x" * (EXTRA_MAX_VALUE_LEN + 10)},
    }
    metric = parse_metric_payload(raw, now=_now())
    assert "long" not in metric.extra


def test_parse_metric_extra_caps_key_length() -> None:
    raw = {
        "type": "weight",
        "value": 80.0,
        "unit": "kg",
        "recorded_at": _now().isoformat(),
        "extra": {"a" * (EXTRA_MAX_KEY_LEN + 5): "ok"},
    }
    metric = parse_metric_payload(raw, now=_now())
    assert metric.extra == {}


def test_parse_metric_extra_caps_total_bytes() -> None:
    """If the running total goes over the cap, the rest gets dropped."""
    big = "y" * (EXTRA_MAX_VALUE_LEN - 1)
    raw = {
        "type": "weight",
        "value": 80.0,
        "unit": "kg",
        "recorded_at": _now().isoformat(),
        "extra": {f"k{i:02d}": big for i in range(20)},
    }
    metric = parse_metric_payload(raw, now=_now())
    total = sum(len(k) + len(v) for k, v in metric.extra.items())
    assert total <= EXTRA_MAX_TOTAL_BYTES


# ── HealthBackend registry ───────────────────────────────────────────


class _FakeOneBackend(HealthBackend):
    backend_name = "_fake_one"

    async def initialize(self, config: dict[str, object]) -> None:
        return None

    async def close(self) -> None:
        return None

    def supported_metrics(self) -> set[MetricType]:
        return {MetricType.STEPS}


def test_subclass_registers_in_HealthBackend_registry() -> None:  # noqa: N802
    """``__init_subclass__`` registers on ``HealthBackend._registry``
    (NOT ``cls._registry``) so subclasses don't shadow the parent."""
    assert "_fake_one" in HealthBackend.registered_backends()
    assert HealthBackend.registered_backends()["_fake_one"] is _FakeOneBackend


def test_health_backend_supports_flags_default_false() -> None:
    backend = _FakeOneBackend()
    assert backend.supports_pull is False
    assert backend.supports_push is False


# ── Errors ───────────────────────────────────────────────────────────


def test_error_taxonomy_inherits_from_base() -> None:
    assert issubclass(HealthBackendAuthError, HealthBackendError)
    assert issubclass(HealthBackendRateLimitError, HealthBackendError)
    assert issubclass(HealthBackendTransientError, HealthBackendError)
    assert issubclass(HealthBackendNotFoundError, HealthBackendError)


def test_rate_limit_error_carries_retry_after() -> None:
    exc = HealthBackendRateLimitError("slow down", retry_after_seconds=42)
    assert exc.retry_after_seconds == 42


def test_rate_limit_error_negative_retry_floored_to_zero() -> None:
    exc = HealthBackendRateLimitError("oops", retry_after_seconds=-1)
    assert exc.retry_after_seconds == 0


# ── HealthMetric round-trip ──────────────────────────────────────────


def test_health_metric_to_from_dict_roundtrip() -> None:
    metric = HealthMetric(
        id="m1",
        user_id="u1",
        backend="hk-webhook",
        metric_type=MetricType.STEPS,
        value=8431.0,
        unit=MetricUnit.COUNT,
        recorded_at=datetime(2026, 5, 9, 11, 0, tzinfo=UTC),
        ingested_at=datetime(2026, 5, 9, 11, 5, tzinfo=UTC),
        source_event_id="evt-123",
        extra={"device": "iPhone"},
    )
    restored = HealthMetric.from_dict(metric.to_dict())
    assert restored == metric


# ── GreetingBrief.empty ──────────────────────────────────────────────


def test_greeting_brief_empty_clear_signal() -> None:
    brief = GreetingBrief.empty("u1")
    assert brief.user_id == "u1"
    assert brief.has_data is False
    assert brief.sleep_hours is None
    assert brief.flags == []

