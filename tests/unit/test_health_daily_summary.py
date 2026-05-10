"""Tests for the hourly TZ-aware daily-summary scheduler path.

Key invariants per spec §10.1 / §11 / §16.6:
- DST-correct boundary (zoneinfo handles spring-forward 23h /
  fall-back 25h by construction).
- Missing-data path doesn't hallucinate — the prompt receives an
  empty structured-prose body and the AI is not called.
- Prompt cache wired to ``self._summary_prompt`` (the ``ConfigParam``
  override flows through, the call site never references
  ``_DEFAULT_SUMMARY_PROMPT``).
- Non-clinical guarantee: the bundled prompt forbids the words
  ``concerning`` / ``abnormal`` / ``warning`` / ``risk`` /
  ``noteworthy`` / ``should``; a regression suite checks the prompt
  text against the forbidden-word regex.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from gilbert.core.context import set_current_user
from gilbert.core.events import InMemoryEventBus
from gilbert.core.services.health import (
    _DEFAULT_SUMMARY_PROMPT,
    _SUMMARIES_COLLECTION,
    HealthService,
    _structured_prose,
)
from gilbert.interfaces.ai import AIResponse, Message, MessageRole
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.health import (
    HealthMetric,
    MetricType,
    MetricUnit,
)
from gilbert.interfaces.storage import Filter, FilterOp, Query
from gilbert.storage.sqlite import SQLiteStorage

# ── Fakes ────────────────────────────────────────────────────────────


class _FakeStorageProvider:
    def __init__(self, backend: SQLiteStorage) -> None:
        self.backend = backend
        self.raw_backend = backend

    def create_namespaced(self, namespace: str) -> Any:
        return self.backend


class _FakeEventBusProvider:
    def __init__(self) -> None:
        self.bus = InMemoryEventBus()


class _FakeSchedulerProvider:
    def add_job(self, *args: Any, **kwargs: Any) -> Any:
        return None

    def remove_job(self, *args: Any, **kwargs: Any) -> None:
        pass

    def enable_job(self, *args: Any, **kwargs: Any) -> None:
        pass

    def disable_job(self, *args: Any, **kwargs: Any) -> None:
        pass

    def list_jobs(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    def get_job(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def run_now(self, *args: Any, **kwargs: Any) -> None:
        pass


class _RecordingAI:
    """AISamplingProvider stub that records every call so tests can
    assert ``system_prompt`` flowed through correctly."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.response_text: str = "Solid night, normal day."

    def has_profile(self, name: str) -> bool:
        return True

    async def complete_one_shot(
        self,
        *,
        messages: list[Message],
        system_prompt: str = "",
        profile_name: str | None = None,
        max_tokens: int | None = None,
        tools_override: Any = None,
    ) -> AIResponse:
        self.calls.append(
            {
                "messages": messages,
                "system_prompt": system_prompt,
                "profile_name": profile_name,
            }
        )
        return AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content=self.response_text),
            model="test-model",
        )


def _resolver(**caps: Any) -> Any:
    class _R:
        def require_capability(self, name: str) -> Any:
            if name not in caps:
                raise LookupError(name)
            return caps[name]

        def get_capability(self, name: str) -> Any:
            return caps.get(name)

        def get_all(self, name: str) -> list[Any]:
            return []

    return _R()


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
async def started_service(sqlite_storage: SQLiteStorage) -> Any:
    ai = _RecordingAI()
    svc = HealthService()
    resolver = _resolver(
        entity_storage=_FakeStorageProvider(sqlite_storage),
        event_bus=_FakeEventBusProvider(),
        scheduler=_FakeSchedulerProvider(),
        ai_chat=ai,
    )
    await svc.start(resolver)
    yield {"svc": svc, "ai": ai, "storage": sqlite_storage}
    await svc.stop()


# ── Daily-summary computation ────────────────────────────────────────


async def test_daily_summary_persists_row_and_calls_ai(
    started_service: Any,
) -> None:
    svc: HealthService = started_service["svc"]
    ai: _RecordingAI = started_service["ai"]
    storage: SQLiteStorage = started_service["storage"]

    # Persist a few metrics for alice in the past 24h.
    yesterday_local_midnight = datetime.now(UTC).replace(
        hour=2, minute=0, second=0, microsecond=0
    ) - timedelta(days=1)
    metrics = [
        HealthMetric(
            id="",
            user_id="alice",
            backend="_fake_health",
            metric_type=MetricType.SLEEP_DURATION,
            value=24000.0,
            unit=MetricUnit.SECONDS,
            recorded_at=yesterday_local_midnight + timedelta(hours=4),
            ingested_at=datetime.now(UTC),
            source_event_id="ev-sleep",
        ),
        HealthMetric(
            id="",
            user_id="alice",
            backend="_fake_health",
            metric_type=MetricType.STEPS,
            value=8431.0,
            unit=MetricUnit.COUNT,
            recorded_at=yesterday_local_midnight + timedelta(hours=12),
            ingested_at=datetime.now(UTC),
            source_event_id="ev-steps",
        ),
    ]
    await svc.ingest_metrics("alice", "_fake_health", metrics)

    await svc._compute_and_persist_summary("alice")

    rows = await storage.query(
        Query(
            collection=_SUMMARIES_COLLECTION,
            filters=[Filter(field="user_id", op=FilterOp.EQ, value="alice")],
        )
    )
    assert len(rows) == 1
    # Prompt cache wired to self._summary_prompt — every AI call
    # uses the cached value, never the _DEFAULT_* constant directly.
    assert len(ai.calls) == 1
    assert ai.calls[0]["system_prompt"] == svc._summary_prompt


async def test_daily_summary_uses_overridden_prompt_not_default_constant(
    started_service: Any,
) -> None:
    """Override the prompt via on_config_changed and verify the
    NEW prompt flows to the AI call, not the bundled ``_DEFAULT_*``
    constant."""
    svc: HealthService = started_service["svc"]
    ai: _RecordingAI = started_service["ai"]

    sentinel = "OVERRIDDEN PROMPT — sentinel value"
    await svc.on_config_changed({"summary_prompt": sentinel})
    assert svc._summary_prompt == sentinel

    # Persist data so the AI call actually fires.
    await svc.ingest_metrics(
        "alice",
        "_fake_health",
        [
            HealthMetric(
                id="",
                user_id="alice",
                backend="_fake_health",
                metric_type=MetricType.SLEEP_DURATION,
                value=24000.0,
                unit=MetricUnit.SECONDS,
                recorded_at=datetime.now(UTC) - timedelta(hours=20),
                ingested_at=datetime.now(UTC),
                source_event_id="ev-1",
            )
        ],
    )
    await svc._compute_and_persist_summary("alice")
    assert ai.calls[-1]["system_prompt"] == sentinel
    # And NOT the bundled default — defends against a regression
    # where the call site reads the constant by name.
    assert ai.calls[-1]["system_prompt"] != _DEFAULT_SUMMARY_PROMPT


async def test_daily_summary_empty_override_falls_back_to_default(
    started_service: Any,
) -> None:
    """Empty string in config falls back to the bundled default."""
    svc: HealthService = started_service["svc"]
    await svc.on_config_changed({"summary_prompt": ""})
    assert svc._summary_prompt == _DEFAULT_SUMMARY_PROMPT


async def test_daily_summary_missing_data_does_not_call_ai(
    started_service: Any,
) -> None:
    """If there's no data for the user's window, the prompt body is
    empty — don't call the model so we don't hallucinate."""
    svc: HealthService = started_service["svc"]
    ai: _RecordingAI = started_service["ai"]
    await svc._compute_and_persist_summary("alice-no-data")
    assert ai.calls == []


# ── Forbidden-word regression on the bundled prompt ──────────────────

# This is the deterministic half of the §16.7 prompt-output check —
# the prompt itself MUST forbid the listed clinical words. A
# live-AI smoke test (marked @pytest.mark.live) catches model-side
# drift; CI runs the deterministic side every commit.

_FORBIDDEN_WORDS = (
    "concerning",
    "abnormal",
    "warning",
    "risk",
    "noteworthy",
    "should",
)


def test_default_summary_prompt_forbids_clinical_words() -> None:
    """The bundled summary prompt must explicitly forbid every clinical
    word in the regression vocabulary. If a future edit waters down the
    forbidden-word list, this test fails before the prompt ships."""
    text = _DEFAULT_SUMMARY_PROMPT.lower()
    # The prompt must contain a "do not use" line listing each word.
    for word in _FORBIDDEN_WORDS:
        assert word in text, f"Bundled summary prompt no longer forbids {word!r}"


def test_default_summary_prompt_disallows_diagnose_and_treat() -> None:
    text = _DEFAULT_SUMMARY_PROMPT.lower()
    for required in ("diagnose", "cause", "treatment"):
        assert required in text, (
            f"Bundled summary prompt no longer mentions {required!r} — "
            "the non-clinical guard rail is missing"
        )


# ── DST-correct boundary check ───────────────────────────────────────


def test_dst_spring_forward_window_is_23_hours() -> None:
    """zoneinfo handles spring-forward correctly — the local-midnight
    pair converted to UTC spans exactly 23 hours.

    The HealthService boundary computation uses the same shape:
        local_today_0  = local_now.replace(hour=0, ...)
        local_yest_0   = local_today_0 - timedelta(days=1)
        window_utc_*   = local_*.astimezone(UTC)
    """
    tz = ZoneInfo("America/Los_Angeles")
    # 2024-03-10 was the spring-forward day in US Pacific.
    local_today_0 = datetime(2024, 3, 11, 0, 0, tzinfo=tz)
    local_yest_0 = datetime(2024, 3, 10, 0, 0, tzinfo=tz)
    span = (local_today_0.astimezone(UTC) - local_yest_0.astimezone(UTC))
    assert span == timedelta(hours=23)


def test_dst_fall_back_window_is_25_hours() -> None:
    tz = ZoneInfo("America/Los_Angeles")
    # 2024-11-03 was the fall-back day in US Pacific.
    local_today_0 = datetime(2024, 11, 4, 0, 0, tzinfo=tz)
    local_yest_0 = datetime(2024, 11, 3, 0, 0, tzinfo=tz)
    span = (local_today_0.astimezone(UTC) - local_yest_0.astimezone(UTC))
    assert span == timedelta(hours=25)


# ── _structured_prose rendering ──────────────────────────────────────


def test_structured_prose_empty_returns_empty_string() -> None:
    assert _structured_prose({}) == ""


def test_structured_prose_emits_headline_values() -> None:
    snapshot = {
        MetricType.SLEEP_DURATION.value: 24000.0,
        MetricType.STEPS.value: 8431.0,
        MetricType.WEIGHT.value: 80.5,
        MetricType.HEART_RATE_RESTING.value: 60.0,
    }
    text = _structured_prose(snapshot)
    assert "Sleep:" in text
    assert "Steps:" in text
    assert "Weight:" in text
    assert "Resting HR:" in text


# ── _compute_flags is code-driven, not AI-driven ────────────────────


async def test_flags_low_sleep_computed_in_code(
    started_service: Any,
) -> None:
    """`flags` come from code thresholds, not from parsing
    ``summary_text``."""
    svc: HealthService = started_service["svc"]
    svc._flag_low_sleep_consecutive_nights = 3
    svc._flag_low_sleep_hours = 6.0

    base = datetime.now(UTC)
    sleep_metrics = [
        HealthMetric(
            id="",
            user_id="alice",
            backend="_fake_health",
            metric_type=MetricType.SLEEP_DURATION,
            value=4 * 3600.0,  # 4h — below 6h threshold
            unit=MetricUnit.SECONDS,
            recorded_at=base - timedelta(days=i, hours=2),
            ingested_at=base,
            source_event_id=f"ev-sleep-{i}",
        )
        for i in range(3)
    ]
    await svc.ingest_metrics("alice", "_fake_health", sleep_metrics)
    set_current_user(UserContext.SYSTEM)
    flags = await svc._compute_flags(
        "alice",
        snapshot={MetricType.SLEEP_DURATION.value: 4 * 3600.0},
        window_end=base,
    )
    assert "low_sleep" in flags

