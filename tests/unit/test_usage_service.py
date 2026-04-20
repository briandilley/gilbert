"""Tests for UsageService — per-round token + cost recording and reporting."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gilbert.core.services.storage import StorageService
from gilbert.core.services.usage import (
    USAGE_COLLECTION,
    UsageService,
    _aggregate_rows,
)
from gilbert.interfaces.ai import TokenUsage
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.service import ServiceResolver
from gilbert.interfaces.storage import StorageBackend
from gilbert.interfaces.usage import (
    UsageProvider,
    UsageQuery,
    UsageRecorder,
)

# --- Storage stub (copied minimal surface from test_ai_profiles) ------------


class _StubStorage(StorageBackend):
    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict[str, Any]]] = {}

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def put(self, collection: str, entity_id: str, data: dict[str, Any]) -> None:
        self._data.setdefault(collection, {})[entity_id] = {"_id": entity_id, **data}

    async def get(self, collection: str, entity_id: str) -> dict[str, Any] | None:
        return self._data.get(collection, {}).get(entity_id)

    async def delete(self, collection: str, entity_id: str) -> None:
        self._data.get(collection, {}).pop(entity_id, None)

    async def exists(self, collection: str, entity_id: str) -> bool:
        return entity_id in self._data.get(collection, {})

    async def query(self, query: Any) -> list[dict[str, Any]]:
        col = self._data.get(query.collection, {})
        entities = list(col.values())
        # Apply filters
        from gilbert.interfaces.storage import FilterOp

        for f in query.filters:
            if f.op == FilterOp.EQ:
                entities = [e for e in entities if e.get(f.field) == f.value]
            elif f.op == FilterOp.GTE:
                entities = [
                    e for e in entities
                    if str(e.get(f.field, "")) >= str(f.value)
                ]
            elif f.op == FilterOp.LT:
                entities = [
                    e for e in entities
                    if str(e.get(f.field, "")) < str(f.value)
                ]
        if query.sort:
            for s in reversed(query.sort):
                entities.sort(key=lambda e: e.get(s.field, ""), reverse=s.descending)
        return entities

    async def count(self, query: Any) -> int:
        return len(await self.query(query))

    async def list_collections(self) -> list[str]:
        return list(self._data.keys())

    async def drop_collection(self, collection: str) -> None:
        self._data.pop(collection, None)

    async def ensure_index(self, index: Any) -> None:
        pass

    async def list_indexes(self, collection: str) -> list[Any]:
        return []

    async def ensure_foreign_key(self, fk: Any) -> None:
        pass

    async def list_foreign_keys(self, collection: str) -> list[Any]:
        return []


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture
def storage() -> _StubStorage:
    return _StubStorage()


@pytest.fixture
async def service(storage: _StubStorage) -> UsageService:
    storage_svc = StorageService(storage)
    resolver = AsyncMock(spec=ServiceResolver)
    resolver.require_capability.return_value = storage_svc
    resolver.get_capability.return_value = None
    svc = UsageService()
    await svc.start(resolver)
    return svc


def _ctx(uid: str = "u1", name: str = "Alice") -> UserContext:
    return UserContext(user_id=uid, email="", display_name=name)


# --- Protocol conformance ---------------------------------------------------


async def test_service_conforms_to_protocols(service: UsageService) -> None:
    assert isinstance(service, UsageRecorder)
    assert isinstance(service, UsageProvider)


# --- Cost calculation -------------------------------------------------------


async def test_compute_cost_known_anthropic_model(service: UsageService) -> None:
    usage = TokenUsage(
        input_tokens=1_000_000,
        output_tokens=500_000,
        cache_creation_tokens=200_000,
        cache_read_tokens=100_000,
    )
    cost = service.compute_cost(
        backend="anthropic",
        model="claude-opus-4-20250514",
        usage=usage,
    )
    # 1M × 15 + 0.5M × 75 + 0.2M × 18.75 + 0.1M × 1.50
    # = 15 + 37.50 + 3.75 + 0.15 = 56.40
    assert cost == pytest.approx(56.40, abs=1e-3)


async def test_compute_cost_unknown_model_returns_zero(service: UsageService) -> None:
    usage = TokenUsage(input_tokens=1000, output_tokens=500)
    cost = service.compute_cost(
        backend="unknown",
        model="mystery-model",
        usage=usage,
    )
    assert cost == 0.0


async def test_pricing_overrides_replace_defaults(service: UsageService) -> None:
    await service.on_config_changed(
        {
            "pricing": {
                "anthropic": {
                    # Model key uses the sanitized form (- → _, . → _) that
                    # the config UI will persist.
                    "claude_opus_4_20250514": {
                        "input_per_mtok": 20.0,
                        "output_per_mtok": 100.0,
                    }
                }
            }
        }
    )
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    cost = service.compute_cost(
        backend="anthropic",
        model="claude-opus-4-20250514",
        usage=usage,
    )
    assert cost == pytest.approx(120.0, abs=1e-3)


async def test_pricing_overrides_partial_merge_with_defaults(
    service: UsageService,
) -> None:
    # Only override input_per_mtok — output_per_mtok should keep default (75.0)
    await service.on_config_changed(
        {
            "pricing": {
                "anthropic": {
                    "claude_opus_4_20250514": {"input_per_mtok": 20.0}
                }
            }
        }
    )
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    cost = service.compute_cost(
        backend="anthropic",
        model="claude-opus-4-20250514",
        usage=usage,
    )
    # 20 + 75 = 95
    assert cost == pytest.approx(95.0, abs=1e-3)


async def test_missing_pricing_section_keeps_defaults(
    service: UsageService,
) -> None:
    await service.on_config_changed({})
    cost = service.compute_cost(
        backend="anthropic",
        model="claude-opus-4-20250514",
        usage=TokenUsage(input_tokens=1_000_000, output_tokens=0),
    )
    assert cost == pytest.approx(15.0, abs=1e-3)


def test_config_params_emits_one_field_per_rate(service: UsageService) -> None:
    """Form fields render per-rate so the UI is a plain form, not a JSON textarea."""
    params = {p.key: p for p in service.config_params()}
    # Anthropic Opus 4 has all four rate fields (input, output, cache_c, cache_r)
    assert (
        "pricing.anthropic.claude_opus_4_20250514.input_per_mtok" in params
    )
    assert (
        "pricing.anthropic.claude_opus_4_20250514.cache_creation_per_mtok"
        in params
    )
    # OpenAI has no cache_creation charge — that field should be omitted
    assert (
        "pricing.openai.gpt_4o.cache_creation_per_mtok" not in params
    )
    assert "pricing.openai.gpt_4o.cache_read_per_mtok" in params
    # Defaults surface the current shipped rates so the form shows meaningful
    # starting values instead of zeros.
    assert (
        params["pricing.anthropic.claude_opus_4_20250514.input_per_mtok"].default
        == 15.0
    )


# --- Recording --------------------------------------------------------------


async def test_record_round_writes_entity(
    service: UsageService,
    storage: _StubStorage,
) -> None:
    usage = TokenUsage(
        input_tokens=1000,
        output_tokens=500,
        cache_creation_tokens=200,
        cache_read_tokens=100,
    )
    record = await service.record_round(
        user_ctx=_ctx(),
        conversation_id="conv-abc",
        profile="standard",
        backend="anthropic",
        model="claude-opus-4-20250514",
        usage=usage,
        tool_names=["web_search", "fetch_url"],
        stop_reason="tool_use",
        round_num=2,
    )
    assert record.input_tokens == 1000
    assert record.user_id == "u1"
    # Cost must be computed and non-zero for a known model
    assert record.cost_usd > 0.0

    # Entity was persisted
    ns_col = f"gilbert.{USAGE_COLLECTION}"
    stored = list(storage._data[ns_col].values())
    assert len(stored) == 1
    row = stored[0]
    assert row["conversation_id"] == "conv-abc"
    assert row["profile"] == "standard"
    assert row["tool_names"] == ["web_search", "fetch_url"]
    assert row["round_num"] == 2
    # Date denormalization for daily grouping
    assert row["date"] == row["timestamp"][:10]


async def test_record_round_never_raises_on_storage_failure(
    service: UsageService,
) -> None:
    # Force put() to blow up
    async def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("disk full")

    service._storage.put = boom  # type: ignore[method-assign]
    # Must not raise — the AI loop can't tolerate recording failures
    record = await service.record_round(
        user_ctx=_ctx(),
        conversation_id="c1",
        profile="standard",
        backend="anthropic",
        model="claude-opus-4-20250514",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
        tool_names=[],
        stop_reason="end_turn",
        round_num=0,
    )
    assert record.input_tokens == 10


# --- Query / aggregation ----------------------------------------------------


async def _seed(service: UsageService, count_per_user: dict[str, int]) -> None:
    for uid, count in count_per_user.items():
        for i in range(count):
            await service.record_round(
                user_ctx=_ctx(uid, name=uid.upper()),
                conversation_id=f"conv-{uid}",
                profile="standard",
                backend="anthropic",
                model="claude-opus-4-20250514",
                usage=TokenUsage(input_tokens=100, output_tokens=50),
                tool_names=["web_search"] if i % 2 == 0 else [],
                stop_reason="end_turn",
                round_num=i,
            )


async def test_query_usage_ungrouped_returns_every_row(
    service: UsageService,
) -> None:
    await _seed(service, {"u1": 3, "u2": 2})
    rows = await service.query_usage(UsageQuery())
    assert len(rows) == 5
    assert all(r.rounds == 1 for r in rows)


async def test_query_usage_filter_by_user(service: UsageService) -> None:
    await _seed(service, {"u1": 3, "u2": 2})
    rows = await service.query_usage(UsageQuery(user_id="u1"))
    assert len(rows) == 3
    assert all(r.dimensions["user_id"] == "u1" for r in rows)


async def test_query_usage_group_by_user(service: UsageService) -> None:
    await _seed(service, {"u1": 3, "u2": 2})
    rows = await service.query_usage(UsageQuery(group_by=("user_id",)))
    by_user = {r.dimensions["user_id"]: r for r in rows}
    assert by_user["u1"].rounds == 3
    assert by_user["u2"].rounds == 2
    # Totals scale with round count
    assert by_user["u1"].input_tokens == 300
    assert by_user["u2"].input_tokens == 200


async def test_query_usage_group_by_tool_splits_per_tool(
    service: UsageService,
) -> None:
    """Rounds with multiple tool_names contribute a row per tool."""
    await service.record_round(
        user_ctx=_ctx(),
        conversation_id="c",
        profile="standard",
        backend="anthropic",
        model="claude-opus-4-20250514",
        usage=TokenUsage(input_tokens=100, output_tokens=50),
        tool_names=["web_search", "fetch_url"],
        stop_reason="tool_use",
        round_num=0,
    )
    rows = await service.query_usage(UsageQuery(group_by=("tool_name",)))
    by_tool = {r.dimensions["tool_name"]: r for r in rows}
    # Both tools credited with the full round (tokens are billed per round)
    assert by_tool["web_search"].input_tokens == 100
    assert by_tool["fetch_url"].input_tokens == 100


async def test_query_usage_filter_by_tool_name(service: UsageService) -> None:
    await _seed(service, {"u1": 4})  # 2 rounds have web_search, 2 don't
    rows = await service.query_usage(UsageQuery(tool_name="web_search"))
    assert len(rows) == 2


async def test_query_usage_group_by_date(service: UsageService) -> None:
    # Two rounds on distinct synthetic dates
    ns_col = f"gilbert.{USAGE_COLLECTION}"
    today = "2026-04-19"
    yesterday = "2026-04-18"
    await service._storage.put(  # type: ignore[union-attr]
        ns_col.replace("gilbert.", ""),  # StorageService already namespaces
        "r1",
        {
            "timestamp": f"{today}T10:00:00+00:00",
            "date": today,
            "user_id": "u1",
            "user_name": "U1",
            "conversation_id": "c",
            "profile": "standard",
            "backend": "anthropic",
            "model": "claude-opus-4-20250514",
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "cost_usd": 0.01,
            "tool_names": [],
            "stop_reason": "end_turn",
            "round_num": 0,
            "invocation_source": "chat",
        },
    )
    await service._storage.put(  # type: ignore[union-attr]
        ns_col.replace("gilbert.", ""),
        "r2",
        {
            "timestamp": f"{yesterday}T10:00:00+00:00",
            "date": yesterday,
            "user_id": "u1",
            "user_name": "U1",
            "conversation_id": "c",
            "profile": "standard",
            "backend": "anthropic",
            "model": "claude-opus-4-20250514",
            "input_tokens": 200,
            "output_tokens": 100,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "cost_usd": 0.02,
            "tool_names": [],
            "stop_reason": "end_turn",
            "round_num": 0,
            "invocation_source": "chat",
        },
    )
    rows = await service.query_usage(UsageQuery(group_by=("date",)))
    by_date = {r.dimensions["date"]: r for r in rows}
    assert by_date[today].input_tokens == 100
    assert by_date[yesterday].input_tokens == 200


async def test_query_usage_invalid_group_by_raises(service: UsageService) -> None:
    with pytest.raises(ValueError):
        await service.query_usage(UsageQuery(group_by=("not_a_field",)))


async def test_query_usage_date_range_filter(service: UsageService) -> None:
    await _seed(service, {"u1": 3})
    future_start = datetime.now(UTC) + timedelta(days=1)
    rows = await service.query_usage(UsageQuery(start=future_start))
    assert rows == []


async def test_list_models_with_usage(service: UsageService) -> None:
    await _seed(service, {"u1": 1})
    await service.record_round(
        user_ctx=_ctx("u2"),
        conversation_id="c",
        profile="fast",
        backend="openai",
        model="gpt-4o-mini",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
        tool_names=[],
        stop_reason="end_turn",
        round_num=0,
    )
    models = await service.list_models_with_usage()
    pairs = {(m["backend"], m["model"]) for m in models}
    assert ("anthropic", "claude-opus-4-20250514") in pairs
    assert ("openai", "gpt-4o-mini") in pairs


# --- Aggregation helper -----------------------------------------------------


def test_aggregate_rows_sums_tokens_and_costs() -> None:
    rows = [
        {
            "user_id": "u1",
            "user_name": "Alice",
            "backend": "anthropic",
            "model": "m",
            "profile": "p",
            "conversation_id": "c",
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "cost_usd": 0.01,
            "tool_names": [],
            "timestamp": "2026-04-19T10:00:00",
            "date": "2026-04-19",
            "invocation_source": "chat",
        },
        {
            "user_id": "u1",
            "user_name": "Alice",
            "backend": "anthropic",
            "model": "m",
            "profile": "p",
            "conversation_id": "c",
            "input_tokens": 200,
            "output_tokens": 75,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "cost_usd": 0.02,
            "tool_names": [],
            "timestamp": "2026-04-19T10:00:00",
            "date": "2026-04-19",
            "invocation_source": "chat",
        },
    ]
    aggs = _aggregate_rows(rows, ("user_id",))
    assert len(aggs) == 1
    agg = aggs[0]
    assert agg.rounds == 2
    assert agg.input_tokens == 300
    assert agg.output_tokens == 125
    assert agg.cost_usd == pytest.approx(0.03, abs=1e-6)
