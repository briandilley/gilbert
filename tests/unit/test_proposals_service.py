"""Tests for ProposalsService — autonomous self-improvement proposals."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from gilbert.core.services.proposals import ProposalsService
from gilbert.interfaces.ai import AIResponse, Message, MessageRole
from gilbert.interfaces.events import Event
from gilbert.interfaces.proposals import (
    OBSERVATIONS_COLLECTION,
    PROPOSALS_COLLECTION,
    SOURCE_AI_TOOL,
    SOURCE_CONVERSATION_ABANDONED,
    SOURCE_CONVERSATION_ACTIVE,
    SOURCE_CONVERSATION_DELETED,
    SOURCE_EVENT,
    STATUS_APPROVED,
    STATUS_PROPOSED,
    STATUS_REJECTED,
)
from gilbert.interfaces.storage import (
    Filter,
    FilterOp,
    ForeignKeyDefinition,
    IndexDefinition,
    Query,
    StorageBackend,
)

# ── In-memory fakes ──────────────────────────────────────────────────


class FakeStorage(StorageBackend):
    """Minimal dict-based StorageBackend for unit tests."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict[str, Any]]] = {}

    async def initialize(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def put(self, collection: str, entity_id: str, data: dict[str, Any]) -> None:
        self._data.setdefault(collection, {})[entity_id] = dict(data)

    async def get(self, collection: str, entity_id: str) -> dict[str, Any] | None:
        return self._data.get(collection, {}).get(entity_id)

    async def delete(self, collection: str, entity_id: str) -> None:
        self._data.get(collection, {}).pop(entity_id, None)

    async def exists(self, collection: str, entity_id: str) -> bool:
        return entity_id in self._data.get(collection, {})

    async def query(self, query: Query) -> list[dict[str, Any]]:
        rows = list(self._data.get(query.collection, {}).values())
        for f in query.filters:
            rows = [r for r in rows if _matches(r, f)]
        for sort_field in reversed(query.sort):
            rows.sort(
                key=lambda r: r.get(sort_field.field) or "",
                reverse=sort_field.descending,
            )
        if query.offset:
            rows = rows[query.offset :]
        if query.limit is not None:
            rows = rows[: query.limit]
        return rows

    async def count(self, query: Query) -> int:
        results = await self.query(
            Query(
                collection=query.collection,
                filters=query.filters,
                sort=[],
                limit=None,
                offset=0,
            ),
        )
        return len(results)

    async def list_collections(self) -> list[str]:
        return list(self._data.keys())

    async def drop_collection(self, collection: str) -> None:
        self._data.pop(collection, None)

    async def ensure_index(self, index: IndexDefinition) -> None:
        return None

    async def list_indexes(self, collection: str) -> list[IndexDefinition]:
        return []

    async def ensure_foreign_key(self, fk: ForeignKeyDefinition) -> None:
        return None

    async def list_foreign_keys(self, collection: str) -> list[ForeignKeyDefinition]:
        return []


def _resolve_dotted(row: dict[str, Any], path: str) -> Any:
    """Walk a dot-notation path through nested dicts (matches SQLite storage)."""
    cur: Any = row
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


def _matches(row: dict[str, Any], f: Filter) -> bool:
    value = _resolve_dotted(row, f.field)
    if f.op == FilterOp.EQ:
        return value == f.value
    if f.op == FilterOp.NEQ:
        return value != f.value
    if f.op == FilterOp.IN:
        return value in (f.value or [])
    if f.op == FilterOp.EXISTS:
        return value is not None
    return False


class FakeStorageProvider:
    """Adapter satisfying ``StorageProvider`` for the fake backend."""

    def __init__(self, backend: StorageBackend) -> None:
        self._backend = backend

    @property
    def backend(self) -> StorageBackend:
        return self._backend

    @property
    def raw_backend(self) -> StorageBackend:
        return self._backend

    def create_namespaced(self, namespace: str) -> Any:
        raise NotImplementedError


class FakeBus:
    """In-memory event bus."""

    def __init__(self) -> None:
        self.published: list[Event] = []
        self._subs: list[tuple[str, Callable[[Event], Any]]] = []

    def subscribe(self, event_type: str, handler: Callable[[Event], Any]) -> Callable[[], None]:
        self._subs.append((event_type, handler))
        return lambda: None

    def subscribe_pattern(
        self, pattern: str, handler: Callable[[Event], Any]
    ) -> Callable[[], None]:
        self._subs.append((pattern, handler))
        return lambda: None

    async def publish(self, event: Event) -> None:
        self.published.append(event)


class FakeBusProvider:
    def __init__(self, bus: FakeBus) -> None:
        self._bus = bus

    @property
    def bus(self) -> FakeBus:
        return self._bus


class FakeScheduler:
    """Records add_job calls."""

    def __init__(self) -> None:
        self.jobs: list[dict[str, Any]] = []

    def add_job(self, **kwargs: Any) -> Any:
        self.jobs.append(kwargs)
        return None

    def remove_job(self, name: str, requester_id: str = "") -> None:
        return None

    def enable_job(self, name: str) -> None:
        return None

    def disable_job(self, name: str) -> None:
        return None

    def list_jobs(self, include_system: bool = True) -> list[Any]:
        return []

    def get_job(self, name: str) -> Any:
        return None

    async def run_now(self, name: str) -> None:
        return None


class FakeAI:
    """Stand-in for AISamplingProvider."""

    def __init__(self, content: str = '{"proposals": []}') -> None:
        self._content = content
        self.calls: list[dict[str, Any]] = []

    def has_profile(self, name: str) -> bool:
        return True

    async def complete_one_shot(
        self,
        *,
        messages: Any,
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
                "tools_override": tools_override,
            },
        )
        return AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content=self._content),
            model="test-model",
        )


class FakeResolver:
    def __init__(self) -> None:
        self.caps: dict[str, Any] = {}

    def get_capability(self, cap: str) -> Any:
        return self.caps.get(cap)

    def require_capability(self, cap: str) -> Any:
        svc = self.caps.get(cap)
        if svc is None:
            raise LookupError(cap)
        return svc

    def get_all(self, cap: str) -> list[Any]:
        svc = self.caps.get(cap)
        return [svc] if svc else []


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def fake_storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def fake_bus() -> FakeBus:
    return FakeBus()


@pytest.fixture
def fake_ai() -> FakeAI:
    return FakeAI()


@pytest.fixture
def fake_scheduler() -> FakeScheduler:
    return FakeScheduler()


@pytest.fixture
def resolver(
    fake_storage: FakeStorage,
    fake_bus: FakeBus,
    fake_ai: FakeAI,
    fake_scheduler: FakeScheduler,
) -> FakeResolver:
    r = FakeResolver()
    r.caps["entity_storage"] = FakeStorageProvider(fake_storage)
    r.caps["event_bus"] = FakeBusProvider(fake_bus)
    r.caps["ai_chat"] = fake_ai
    r.caps["scheduler"] = fake_scheduler
    return r


@pytest.fixture
async def started_service(resolver: FakeResolver) -> ProposalsService:
    svc = ProposalsService()
    await svc.start(resolver)
    return svc


def _valid_proposal_blob(title: str = "Add Spotify integration") -> dict[str, Any]:
    """A proposal payload that should pass record validation."""
    return {
        "title": title,
        "summary": "Read-only Spotify now-playing.",
        "kind": "new_plugin",
        "target": "",
        "motivation": "Users repeatedly asked.",
        "evidence": [
            {
                "event_type": "ai.tool_call.refused",
                "summary": "no music tool",
                "occurred_at": "2026-04-25T19:14:00Z",
                "count": 23,
            },
        ],
        "spec": {"overview": "Plugin exposing now-playing capability."},
        "implementation_prompt": "You are implementing the spotify-now-playing plugin...",
        "impact": {"affected_components": ["ai"]},
        "risks": [{"category": "security", "description": "creds", "mitigation": "vault"}],
        "acceptance_criteria": ["AI answers what's playing"],
        "open_questions": [],
    }


# ── Tests ────────────────────────────────────────────────────────────


class TestServiceInfo:
    def test_capabilities_and_events(self) -> None:
        info = ProposalsService().service_info()
        assert info.name == "proposals"
        assert "proposals" in info.capabilities
        assert "ws_handlers" in info.capabilities
        assert "proposal.created" in info.events
        assert "proposal.status_changed" in info.events
        assert info.toggleable is True


class TestStartup:
    @pytest.mark.asyncio
    async def test_registers_scheduler_job(
        self, started_service: ProposalsService, fake_scheduler: FakeScheduler
    ) -> None:
        del started_service
        assert any(
            j["name"] == "proposals.reflection" and j["system"] is True for j in fake_scheduler.jobs
        )

    @pytest.mark.asyncio
    async def test_subscribes_to_event_patterns(
        self, started_service: ProposalsService, fake_bus: FakeBus
    ) -> None:
        del started_service
        assert len(fake_bus._subs) > 0  # at least one pattern subscribed

    @pytest.mark.asyncio
    async def test_disabled_does_nothing(
        self, resolver: FakeResolver, fake_scheduler: FakeScheduler
    ) -> None:
        svc = ProposalsService()
        await svc.on_config_changed({"enabled": False})
        await svc.start(resolver)
        assert fake_scheduler.jobs == []


class TestObservation:
    @pytest.mark.asyncio
    async def test_event_buffered_then_flushed_to_storage(
        self, started_service: ProposalsService, fake_storage: FakeStorage
    ) -> None:
        await started_service._on_event(
            Event(
                event_type="ai.tool_call.refused",
                data={"tool": "spotify", "user_id": "u1"},
                source="ai",
                timestamp=datetime.now(UTC),
            ),
        )
        # Below the flush threshold, lives in the buffer only.
        assert len(started_service._observation_buffer) == 1
        rows_before = await fake_storage.query(Query(collection=OBSERVATIONS_COLLECTION))
        assert rows_before == []

        await started_service._flush_observations()
        rows_after = await fake_storage.query(Query(collection=OBSERVATIONS_COLLECTION))
        assert len(rows_after) == 1
        assert rows_after[0]["source_type"] == SOURCE_EVENT
        assert rows_after[0]["details"]["event_type"] == "ai.tool_call.refused"

    @pytest.mark.asyncio
    async def test_noisy_events_dropped(
        self, started_service: ProposalsService
    ) -> None:
        await started_service._on_event(
            Event(
                event_type="chat.stream.text_delta",
                data={"text": "hi"},
                source="ai",
                timestamp=datetime.now(UTC),
            ),
        )
        assert started_service._observation_buffer == []

    @pytest.mark.asyncio
    async def test_self_emitted_proposal_events_dropped(
        self, started_service: ProposalsService
    ) -> None:
        await started_service._on_event(
            Event(
                event_type="proposal.created",
                data={"proposal_id": "x"},
                source="proposals",
                timestamp=datetime.now(UTC),
            ),
        )
        assert started_service._observation_buffer == []

    @pytest.mark.asyncio
    async def test_threshold_triggers_immediate_flush(
        self, resolver: FakeResolver, fake_storage: FakeStorage
    ) -> None:
        svc = ProposalsService()
        await svc.on_config_changed({"observation_flush_threshold": 2})
        await svc.start(resolver)
        for i in range(2):
            await svc._on_event(
                Event(
                    event_type=f"e{i}",
                    data={"i": i},
                    source="t",
                    timestamp=datetime.now(UTC),
                ),
            )
        rows = await fake_storage.query(Query(collection=OBSERVATIONS_COLLECTION))
        assert len(rows) == 2
        assert svc._observation_buffer == []

    def test_summarize_picks_informative_fields(self) -> None:
        summary = ProposalsService._summarize_event_data(
            {"tool": "spotify", "message": "no such tool", "irrelevant": 42},
        )
        assert "tool=spotify" in summary
        assert "message=no such tool" in summary

    def test_summarize_truncates_long_values(self) -> None:
        summary = ProposalsService._summarize_event_data({"message": "x" * 500})
        assert len(summary) < 200

    def test_summarize_empty(self) -> None:
        assert ProposalsService._summarize_event_data({}) == ""


class TestParseProposalsResponse:
    def test_plain_json(self) -> None:
        text = json.dumps({"proposals": [{"title": "x"}]})
        out = ProposalsService._parse_proposals_response(text)
        assert out == [{"title": "x"}]

    def test_fenced_code_block(self) -> None:
        text = '```json\n{"proposals": [{"title": "y"}]}\n```'
        out = ProposalsService._parse_proposals_response(text)
        assert out == [{"title": "y"}]

    def test_text_with_prose_around(self) -> None:
        text = 'Here is my answer: {"proposals": []} thanks!'
        out = ProposalsService._parse_proposals_response(text)
        assert out == []

    def test_garbage_returns_empty(self) -> None:
        assert ProposalsService._parse_proposals_response("not json at all") == []

    def test_missing_proposals_key(self) -> None:
        assert ProposalsService._parse_proposals_response('{"other": []}') == []

    def test_empty_string(self) -> None:
        assert ProposalsService._parse_proposals_response("") == []


class TestBuildRecord:
    def test_happy_path(self, started_service: ProposalsService) -> None:
        rec = started_service._build_record(_valid_proposal_blob(), cycle_id="c1")
        assert rec["status"] == STATUS_PROPOSED
        assert rec["title"] == "Add Spotify integration"
        assert rec["reflection_cycle_id"] == "c1"
        assert rec["implementation_prompt"].startswith("You are implementing")
        assert rec["_id"] == rec["id"]

    def test_rejects_missing_title(self, started_service: ProposalsService) -> None:
        bad = _valid_proposal_blob()
        bad["title"] = ""
        with pytest.raises(ValueError, match="title"):
            started_service._build_record(bad, cycle_id="c1")

    def test_rejects_missing_spec(self, started_service: ProposalsService) -> None:
        bad = _valid_proposal_blob()
        bad["spec"] = {}
        with pytest.raises(ValueError, match="spec"):
            started_service._build_record(bad, cycle_id="c1")

    def test_rejects_missing_implementation_prompt(self, started_service: ProposalsService) -> None:
        bad = _valid_proposal_blob()
        bad["implementation_prompt"] = "  "
        with pytest.raises(ValueError, match="implementation_prompt"):
            started_service._build_record(bad, cycle_id="c1")

    def test_unknown_kind_falls_back_to_safe_default(
        self, started_service: ProposalsService
    ) -> None:
        bad = _valid_proposal_blob()
        bad["kind"] = "destroy_everything"
        rec = started_service._build_record(bad, cycle_id="c1")
        assert rec["kind"] == "new_plugin"


class TestReflectionGuards:
    @pytest.mark.asyncio
    async def test_skip_when_no_observations(
        self, started_service: ProposalsService, fake_ai: FakeAI
    ) -> None:
        # Defaults: min_observations_per_cycle=25; we have 0.
        created = await started_service._run_reflection(manual=False)
        assert created == 0
        assert fake_ai.calls == []  # AI was not invoked

    @pytest.mark.asyncio
    async def test_manual_bypasses_min_obs_gate(
        self, started_service: ProposalsService, fake_ai: FakeAI
    ) -> None:
        created = await started_service.trigger_reflection()
        assert created == 0  # AI returned empty, but the call DID happen
        assert len(fake_ai.calls) == 1

    @pytest.mark.asyncio
    async def test_skip_when_pending_cap_reached(
        self,
        started_service: ProposalsService,
        fake_storage: FakeStorage,
        fake_ai: FakeAI,
    ) -> None:
        # Seed enough pending proposals to hit the cap.
        for i in range(started_service._max_pending_proposals):
            await fake_storage.put(
                PROPOSALS_COLLECTION,
                f"p{i}",
                {"_id": f"p{i}", "status": STATUS_PROPOSED, "kind": "new_plugin"},
            )
        created = await started_service.trigger_reflection()
        assert created == 0
        assert fake_ai.calls == []  # AI not invoked when over cap

    @pytest.mark.asyncio
    async def test_skip_when_disabled(self, resolver: FakeResolver) -> None:
        svc = ProposalsService()
        await svc.on_config_changed({"enabled": False})
        await svc.start(resolver)
        assert await svc.trigger_reflection() == 0


class TestReflectionHappyPath:
    @pytest.mark.asyncio
    async def test_creates_records_from_ai_response(
        self,
        resolver: FakeResolver,
        fake_storage: FakeStorage,
        fake_bus: FakeBus,
    ) -> None:
        ai_payload = json.dumps({"proposals": [_valid_proposal_blob("Plugin A")]})
        resolver.caps["ai_chat"] = FakeAI(content=ai_payload)
        svc = ProposalsService()
        await svc.start(resolver)

        created = await svc.trigger_reflection()
        assert created == 1

        rows = await fake_storage.query(Query(collection=PROPOSALS_COLLECTION))
        assert len(rows) == 1
        assert rows[0]["title"] == "Plugin A"
        assert rows[0]["status"] == STATUS_PROPOSED

        # Observation should publish proposal.created.
        types = [e.event_type for e in fake_bus.published]
        assert "proposal.created" in types

    @pytest.mark.asyncio
    async def test_caps_per_cycle_count(self, resolver: FakeResolver) -> None:
        many = [_valid_proposal_blob(f"Plugin {i}") for i in range(20)]
        resolver.caps["ai_chat"] = FakeAI(content=json.dumps({"proposals": many}))
        svc = ProposalsService()
        await svc.on_config_changed({"max_proposals_per_cycle": 2})
        await svc.start(resolver)

        created = await svc.trigger_reflection()
        assert created == 2

    @pytest.mark.asyncio
    async def test_discards_malformed_proposals(self, resolver: FakeResolver) -> None:
        good = _valid_proposal_blob("Good")
        bad = {"title": "", "spec": {}}  # missing required fields
        resolver.caps["ai_chat"] = FakeAI(
            content=json.dumps({"proposals": [bad, good]}),
        )
        svc = ProposalsService()
        await svc.start(resolver)
        created = await svc.trigger_reflection()
        assert created == 1


class TestPruning:
    @pytest.mark.asyncio
    async def test_prune_drops_oldest_beyond_cap(
        self, resolver: FakeResolver, fake_storage: FakeStorage
    ) -> None:
        svc = ProposalsService()
        await svc.on_config_changed({"observation_cap_total": 100})
        await svc.start(resolver)

        # Seed 105 observations directly into storage with ascending
        # occurred_at timestamps so the oldest are clearly identifiable.
        base = datetime.now(UTC)
        for i in range(105):
            obs_id = f"obs-{i:03d}"
            await fake_storage.put(
                OBSERVATIONS_COLLECTION,
                obs_id,
                {
                    "_id": obs_id,
                    "occurred_at": (base + timedelta(seconds=i)).isoformat(),
                    "source_type": SOURCE_EVENT,
                    "summary": f"e{i}",
                    "details": {},
                    "consumed_in_cycle": "",
                },
            )

        await svc._prune_observations()
        remaining = await fake_storage.query(Query(collection=OBSERVATIONS_COLLECTION))
        assert len(remaining) == 100
        # The first 5 (oldest) should have been pruned.
        ids = {r["_id"] for r in remaining}
        for i in range(5):
            assert f"obs-{i:03d}" not in ids


class TestRecordObservationTool:
    @pytest.mark.asyncio
    async def test_tool_definition_is_admin_only(
        self, started_service: ProposalsService
    ) -> None:
        tools = started_service.get_tools()
        assert len(tools) == 1
        tool = tools[0]
        assert tool.name == "record_observation"
        assert tool.required_role == "admin"

    @pytest.mark.asyncio
    async def test_tool_records_observation(
        self, started_service: ProposalsService, fake_storage: FakeStorage
    ) -> None:
        result = await started_service.execute_tool(
            "record_observation",
            {
                "summary": "User asked about Spotify integration we don't have.",
                "category": "capability_gap",
                "context": "User said 'why can't you tell me what's playing?'",
            },
        )
        parsed = json.loads(result)
        assert parsed["status"] == "recorded"
        assert parsed["category"] == "capability_gap"

        await started_service._flush_observations()
        rows = await fake_storage.query(Query(collection=OBSERVATIONS_COLLECTION))
        assert len(rows) == 1
        assert rows[0]["source_type"] == SOURCE_AI_TOOL
        assert rows[0]["details"]["category"] == "capability_gap"

    @pytest.mark.asyncio
    async def test_tool_rejects_empty_summary(
        self, started_service: ProposalsService
    ) -> None:
        result = await started_service.execute_tool(
            "record_observation",
            {"summary": "  "},
        )
        parsed = json.loads(result)
        assert "error" in parsed


class TestConversationHarvest:
    @pytest.mark.asyncio
    async def test_active_conversation_produces_active_observation(
        self, resolver: FakeResolver, fake_storage: FakeStorage
    ) -> None:
        # Conversation updated 1h ago — well within the active window.
        recent_iso = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        await fake_storage.put(
            "ai_conversations",
            "c1",
            {
                "_id": "c1",
                "title": "Music chat",
                "messages": [
                    {"role": "user", "content": "what is playing?"},
                    {"role": "assistant", "content": "I don't have music state yet."},
                ],
                "updated_at": recent_iso,
                "user_id": "u1",
            },
        )
        resolver.caps["ai_chat"] = FakeAI(
            content=json.dumps(
                {
                    "observations": [
                        {"summary": "User wanted now-playing.", "category": "capability_gap"}
                    ]
                }
            )
        )
        svc = ProposalsService()
        await svc.start(resolver)

        created = await svc.trigger_harvest()
        assert created == 1

        rows = await fake_storage.query(Query(collection=OBSERVATIONS_COLLECTION))
        active = [r for r in rows if r["source_type"] == SOURCE_CONVERSATION_ACTIVE]
        assert len(active) == 1
        assert active[0]["details"]["conversation_id"] == "c1"
        assert active[0]["details"]["message_count_at_summary"] == 2

    @pytest.mark.asyncio
    async def test_abandoned_conversation_produces_abandoned_observation(
        self, resolver: FakeResolver, fake_storage: FakeStorage
    ) -> None:
        old_iso = (datetime.now(UTC) - timedelta(days=3)).isoformat()
        await fake_storage.put(
            "ai_conversations",
            "c2",
            {
                "_id": "c2",
                "messages": [{"role": "user", "content": "hi"}],
                "updated_at": old_iso,
            },
        )
        resolver.caps["ai_chat"] = FakeAI(
            content=json.dumps(
                {"observations": [{"summary": "abandoned-thought", "category": "other"}]}
            )
        )
        svc = ProposalsService()
        await svc.start(resolver)

        await svc.trigger_harvest()
        rows = await fake_storage.query(Query(collection=OBSERVATIONS_COLLECTION))
        abandoned = [r for r in rows if r["source_type"] == SOURCE_CONVERSATION_ABANDONED]
        assert len(abandoned) == 1

    @pytest.mark.asyncio
    async def test_skips_already_summarized_at_same_message_count(
        self, resolver: FakeResolver, fake_storage: FakeStorage
    ) -> None:
        # Conversation with 2 messages.
        recent_iso = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        await fake_storage.put(
            "ai_conversations",
            "c3",
            {
                "_id": "c3",
                "messages": [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}],
                "updated_at": recent_iso,
            },
        )
        # An existing observation already covers message_count=2.
        await fake_storage.put(
            OBSERVATIONS_COLLECTION,
            "obs-prev",
            {
                "_id": "obs-prev",
                "occurred_at": datetime.now(UTC).isoformat(),
                "source_type": SOURCE_CONVERSATION_ACTIVE,
                "summary": "earlier",
                "details": {
                    "conversation_id": "c3",
                    "message_count_at_summary": 2,
                },
                "consumed_in_cycle": "",
            },
        )
        fake_ai = FakeAI(content=json.dumps({"observations": []}))
        resolver.caps["ai_chat"] = fake_ai
        svc = ProposalsService()
        await svc.start(resolver)

        await svc.trigger_harvest()
        # AI was not called because the conversation was already
        # summarized at the current message count.
        assert fake_ai.calls == []

    @pytest.mark.asyncio
    async def test_caps_conversations_per_cycle(
        self, resolver: FakeResolver, fake_storage: FakeStorage
    ) -> None:
        recent_iso = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        for i in range(5):
            await fake_storage.put(
                "ai_conversations",
                f"c{i}",
                {
                    "_id": f"c{i}",
                    "messages": [{"role": "user", "content": f"m{i}"}],
                    "updated_at": recent_iso,
                },
            )
        fake_ai = FakeAI(content=json.dumps({"observations": []}))
        resolver.caps["ai_chat"] = fake_ai
        svc = ProposalsService()
        await svc.on_config_changed({"harvest_max_conversations_per_cycle": 2})
        await svc.start(resolver)

        await svc.trigger_harvest()
        assert len(fake_ai.calls) == 2


class TestArchiveEventExtraction:
    @pytest.mark.asyncio
    async def test_archiving_event_triggers_extraction(
        self, resolver: FakeResolver, fake_storage: FakeStorage
    ) -> None:
        resolver.caps["ai_chat"] = FakeAI(
            content=json.dumps(
                {
                    "observations": [
                        {"summary": "user gave up after 3 retries", "category": "recurring_frustration"}
                    ]
                }
            )
        )
        svc = ProposalsService()
        await svc.start(resolver)

        await svc._on_chat_archiving(
            Event(
                event_type="chat.conversation.archiving",
                data={
                    "conversation_id": "deleted-1",
                    "owner_id": "u1",
                    "conversation": {
                        "_id": "deleted-1",
                        "messages": [{"role": "user", "content": "ugh"}],
                        "updated_at": datetime.now(UTC).isoformat(),
                    },
                },
                source="ai",
                timestamp=datetime.now(UTC),
            ),
        )
        rows = await fake_storage.query(Query(collection=OBSERVATIONS_COLLECTION))
        deleted = [r for r in rows if r["source_type"] == SOURCE_CONVERSATION_DELETED]
        assert len(deleted) == 1


class TestReflectionConsumption:
    @pytest.mark.asyncio
    async def test_observations_marked_consumed_after_reflection(
        self, resolver: FakeResolver, fake_storage: FakeStorage
    ) -> None:
        # Seed observations.
        for i in range(3):
            obs_id = f"obs-{i}"
            await fake_storage.put(
                OBSERVATIONS_COLLECTION,
                obs_id,
                {
                    "_id": obs_id,
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "source_type": SOURCE_EVENT,
                    "summary": f"signal {i}",
                    "details": {"event_type": f"e{i}"},
                    "consumed_in_cycle": "",
                },
            )
        resolver.caps["ai_chat"] = FakeAI(content='{"proposals": []}')
        svc = ProposalsService()
        await svc.start(resolver)
        await svc.trigger_reflection()

        rows = await fake_storage.query(Query(collection=OBSERVATIONS_COLLECTION))
        for r in rows:
            assert r["consumed_in_cycle"] != ""

    @pytest.mark.asyncio
    async def test_reflection_skips_when_no_new_observations(
        self, resolver: FakeResolver, fake_storage: FakeStorage
    ) -> None:
        # Pre-consumed observation should not count toward the gate.
        await fake_storage.put(
            OBSERVATIONS_COLLECTION,
            "old",
            {
                "_id": "old",
                "occurred_at": datetime.now(UTC).isoformat(),
                "source_type": SOURCE_EVENT,
                "summary": "stale",
                "details": {},
                "consumed_in_cycle": "previous-cycle",
            },
        )
        fake_ai = FakeAI(content='{"proposals": []}')
        resolver.caps["ai_chat"] = fake_ai
        svc = ProposalsService()
        await svc.on_config_changed({"min_observations_per_cycle": 5})
        await svc.start(resolver)

        await svc._run_reflection(manual=False)
        assert fake_ai.calls == []


class TestStatusTransitions:
    @pytest.mark.asyncio
    async def test_update_status_persists_and_publishes(
        self,
        started_service: ProposalsService,
        fake_storage: FakeStorage,
        fake_bus: FakeBus,
    ) -> None:
        record = started_service._build_record(_valid_proposal_blob(), cycle_id="c1")
        await fake_storage.put(PROPOSALS_COLLECTION, record["_id"], record)

        updated = await started_service.update_status(
            record["_id"],
            STATUS_APPROVED,
            actor_user_id="admin1",
        )
        assert updated is not None
        assert updated["status"] == STATUS_APPROVED

        types = [e.event_type for e in fake_bus.published]
        assert "proposal.status_changed" in types

    @pytest.mark.asyncio
    async def test_update_status_rejects_invalid(self, started_service: ProposalsService) -> None:
        with pytest.raises(ValueError):
            await started_service.update_status("nope", "bogus", "u1")

    @pytest.mark.asyncio
    async def test_update_status_returns_none_for_missing(
        self, started_service: ProposalsService
    ) -> None:
        result = await started_service.update_status(
            "missing-id",
            STATUS_REJECTED,
            "u1",
        )
        assert result is None


class TestNotes:
    @pytest.mark.asyncio
    async def test_add_note_appends(
        self, started_service: ProposalsService, fake_storage: FakeStorage
    ) -> None:
        record = started_service._build_record(_valid_proposal_blob(), cycle_id="c1")
        await fake_storage.put(PROPOSALS_COLLECTION, record["_id"], record)

        updated = await started_service.add_note(record["_id"], "Looks good.", "u1")
        assert updated is not None
        assert len(updated["admin_notes"]) == 1
        assert updated["admin_notes"][0]["author_id"] == "u1"

    @pytest.mark.asyncio
    async def test_add_note_rejects_empty(self, started_service: ProposalsService) -> None:
        with pytest.raises(ValueError):
            await started_service.add_note("any", "   ", "u1")


class TestWsAdminGating:
    """All proposals.* WS handlers must reject non-admin connections."""

    @pytest.mark.asyncio
    async def test_list_blocks_non_admin(self, started_service: ProposalsService) -> None:
        non_admin = _fake_conn(user_level=100)
        result = await started_service._ws_list(non_admin, {"id": "f1"})
        assert result["type"] == "gilbert.error"
        assert result["code"] == 403

    @pytest.mark.asyncio
    async def test_admin_can_list(
        self, started_service: ProposalsService, fake_storage: FakeStorage
    ) -> None:
        record = started_service._build_record(_valid_proposal_blob(), cycle_id="c1")
        await fake_storage.put(PROPOSALS_COLLECTION, record["_id"], record)

        admin = _fake_conn(user_level=0)
        result = await started_service._ws_list(admin, {"id": "f1"})
        assert result["type"] == "proposals.list.result"
        assert len(result["proposals"]) == 1

    @pytest.mark.asyncio
    async def test_get_returns_404(self, started_service: ProposalsService) -> None:
        admin = _fake_conn(user_level=0)
        result = await started_service._ws_get(admin, {"id": "f1", "proposal_id": "missing"})
        assert result["code"] == 404


# ── Helpers ──────────────────────────────────────────────────────────


class _FakeUserCtx:
    def __init__(self, user_id: str = "admin1") -> None:
        self.user_id = user_id


class _FakeConn:
    def __init__(self, user_level: int = 0) -> None:
        self.user_level = user_level
        self.user_ctx = _FakeUserCtx()


def _fake_conn(user_level: int = 0) -> _FakeConn:
    return _FakeConn(user_level=user_level)
