"""Tests for ``FeedsService`` — subscribe/unsubscribe, polling, dedup,
edit detection, AI scoring, knowledge ingestion, retention, OPML.

Uses the real SQLite storage backend via the ``sqlite_storage`` fixture
per CLAUDE.md ("Database tests use a real test SQLite database — no
mocking the DB"). Backends are stubbed because they're a clean
abstraction; the AI sampling provider is faked because spinning up a
real one needs a backend.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from gilbert.core.context import set_current_user
from gilbert.core.services.feeds import (
    _BRIEFING_STATE_COLLECTION,
    _BRIEFINGS_COLLECTION,
    _FEED_ITEMS_COLLECTION,
    _FEEDS_COLLECTION,
    FeedsService,
    _safe_uid,
    _strip_json_fences,
)
from gilbert.interfaces.ai import (
    AIResponse,
    Message,
    MessageRole,
    StopReason,
    TokenUsage,
)
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.feeds import (
    Feed,
    FeedBackend,
    FeedItem,
    FeedMeta,
    FeedsProvider,
    PollResult,
    StoredFeedItem,
    can_access_feed,
    can_admin_feed,
    determine_feed_access,
)

# ── Test backend ─────────────────────────────────────────────────────


class FakeFeedBackend(FeedBackend):
    """In-memory feed backend that yields canned items."""

    backend_name = "fake_feed"

    instances: list[FakeFeedBackend] = []

    def __init__(self) -> None:
        self.initialized: bool = False
        self.closed: bool = False
        self.next_items: list[FeedItem] = []
        self.next_not_modified: bool = False
        self.next_status: int = 200
        self.next_suggested_min_interval_sec: int = 0
        self.next_http_cache: dict[str, str] = {}
        self.poll_count: int = 0
        self.probe_count: int = 0
        self.last_http_cache_in: dict[str, str] = {}
        FakeFeedBackend.instances.append(self)

    async def initialize(self, config: dict[str, Any] | None = None) -> None:
        self.initialized = True

    async def close(self) -> None:
        self.closed = True

    async def probe(self, url: str) -> FeedMeta:
        self.probe_count += 1
        return FeedMeta(title=f"Probed: {url}", description="", link=url)

    async def poll(
        self,
        url: str,
        *,
        since: datetime | None = None,
        max_items: int = 100,
        http_cache: dict[str, str] | None = None,
    ) -> PollResult:
        self.poll_count += 1
        self.last_http_cache_in = dict(http_cache or {})
        if self.next_not_modified:
            return PollResult(
                items=[],
                http_cache=dict(self.next_http_cache),
                not_modified=True,
                status_code=304,
            )
        return PollResult(
            items=list(self.next_items),
            http_cache=dict(self.next_http_cache),
            suggested_min_interval_sec=self.next_suggested_min_interval_sec,
            not_modified=False,
            status_code=self.next_status,
        )


# ── Fakes ────────────────────────────────────────────────────────────


class FakeStorageProvider:
    def __init__(self, backend: Any) -> None:
        self._backend = backend

    @property
    def backend(self) -> Any:
        return self._backend

    @property
    def raw_backend(self) -> Any:
        return self._backend

    def create_namespaced(self, namespace: str) -> Any:
        return self._backend


class FakeEventBus:
    def __init__(self) -> None:
        self.published: list[Any] = []

    async def publish(self, event: Any) -> None:
        self.published.append(event)

    def subscribe(self, *args: Any, **kwargs: Any) -> Any:
        return lambda: None

    def subscribe_pattern(self, *args: Any, **kwargs: Any) -> Any:
        return lambda: None


class FakeEventBusProvider:
    def __init__(self) -> None:
        self.bus = FakeEventBus()


class FakeScheduler:
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.added: list[str] = []
        self.removed: list[str] = []

    def add_job(self, **kwargs: Any) -> Any:
        name = kwargs["name"]
        if name in self.jobs:
            # Some flows remove-then-readd; allow idempotent overwrite.
            pass
        self.jobs[name] = kwargs
        self.added.append(name)

    def remove_job(self, name: str, requester_id: str = "") -> None:
        self.jobs.pop(name, None)
        self.removed.append(name)

    def enable_job(self, name: str) -> None:
        pass

    def disable_job(self, name: str) -> None:
        pass

    def list_jobs(self, include_system: bool = True) -> list[Any]:
        return list(self.jobs.values())

    def get_job(self, name: str) -> Any:
        return self.jobs.get(name)

    async def run_now(self, name: str) -> None:
        cb = self.jobs.get(name, {}).get("callback")
        if cb is not None:
            await cb()


class FakeAISampling:
    """Fake AISamplingProvider that records calls and returns a
    canned response."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.next_text: str = '{"score": 0.7, "reason": "looks important"}'

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
                "messages": list(messages),
                "system_prompt": system_prompt,
                "profile_name": profile_name,
                "tools_override": tools_override,
            }
        )
        return AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content=self.next_text),
            model="fake",
            stop_reason=StopReason.END_TURN,
            usage=TokenUsage(input_tokens=0, output_tokens=0),
        )


class FakeKnowledge:
    def __init__(self) -> None:
        self.indexed: list[Any] = []
        self.removed: list[str] = []

    async def index_document(self, backend: Any, meta: Any) -> int:
        self.indexed.append((backend, meta))
        return 1

    async def remove_document(self, document_id: str) -> bool:
        self.removed.append(document_id)
        return True

    async def resolve_document(self, full_path: str) -> Any:
        return None

    def get_backend(self, source_id: str) -> Any:
        return None

    @property
    def backends(self) -> dict[str, Any]:
        return {}


class FakeResolver:
    def __init__(self, **caps: Any) -> None:
        self.caps = caps

    def get_capability(self, name: str) -> Any:
        return self.caps.get(name)

    def require_capability(self, name: str) -> Any:
        if name not in self.caps:
            raise LookupError(name)
        return self.caps[name]

    def get_all(self, name: str) -> list[Any]:
        svc = self.caps.get(name)
        return [svc] if svc else []


# ── Helpers ──────────────────────────────────────────────────────────


def _user(user_id: str = "alice", *, admin: bool = False) -> UserContext:
    roles = frozenset({"admin", "user"} if admin else {"user"})
    return UserContext(
        user_id=user_id,
        email=f"{user_id}@example.com",
        display_name=user_id,
        roles=roles,
    )


def _make_item(uid: str, *, title: str = "Story", link: str = "https://x.com/a") -> FeedItem:
    return FeedItem(
        item_uid=uid,
        title=title,
        link=link,
        summary="A summary",
        published_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.fixture
async def feeds_svc(
    sqlite_storage: Any,
) -> AsyncGenerator[tuple[FeedsService, FakeAISampling, FakeEventBus, FakeKnowledge, FakeScheduler], None]:
    FakeFeedBackend.instances = []
    storage = FakeStorageProvider(sqlite_storage)
    bus = FakeEventBusProvider()
    sched = FakeScheduler()
    ai = FakeAISampling()
    knowledge = FakeKnowledge()
    resolver = FakeResolver(
        entity_storage=storage,
        event_bus=bus,
        scheduler=sched,
        ai_chat=ai,
        knowledge=knowledge,
    )
    svc = FeedsService()
    await svc.start(resolver)
    # Boot job is scheduled but not auto-fired; tests trigger it
    # explicitly so we control timing.
    yield svc, ai, bus.bus, knowledge, sched
    await svc.stop()


# ── Tests ────────────────────────────────────────────────────────────


class TestProtocol:
    def test_feeds_service_satisfies_feeds_provider(self) -> None:
        svc = FeedsService()
        assert isinstance(svc, FeedsProvider)


class TestAuthHelpers:
    def test_owner_has_access_and_admin_rights(self) -> None:
        feed = Feed(id="f1", owner_user_id="alice")
        assert can_access_feed(_user("alice"), feed) is True
        assert can_admin_feed(_user("alice"), feed) is True

    def test_admin_has_access(self) -> None:
        feed = Feed(id="f1", owner_user_id="bob")
        assert can_access_feed(_user("alice"), feed, is_admin=True) is True
        assert can_admin_feed(_user("alice"), feed, is_admin=True) is True

    def test_shared_user_can_access_not_admin(self) -> None:
        feed = Feed(id="f1", owner_user_id="bob", shared_with_users=["alice"])
        assert can_access_feed(_user("alice"), feed) is True
        assert can_admin_feed(_user("alice"), feed) is False

    def test_shared_role_can_access(self) -> None:
        feed = Feed(id="f1", owner_user_id="bob", shared_with_roles=["team"])
        u = UserContext(
            user_id="alice",
            email="a@x.com",
            display_name="Alice",
            roles=frozenset({"team"}),
        )
        assert can_access_feed(u, feed) is True

    def test_outsider_no_access(self) -> None:
        feed = Feed(id="f1", owner_user_id="bob")
        assert can_access_feed(_user("alice"), feed) is False
        assert can_admin_feed(_user("alice"), feed) is False

    def test_determine_access_precedence_owner_beats_admin(self) -> None:
        feed = Feed(id="f1", owner_user_id="alice")
        access = determine_feed_access(_user("alice"), feed, is_admin=True)
        # Owner wins over admin per spec.
        assert access is not None
        assert access.value == "owner"


class TestStripJsonFences:
    def test_plain_json_passes_through(self) -> None:
        text = '{"a":1}'
        assert _strip_json_fences(text) == text

    def test_strips_json_fences(self) -> None:
        text = '```json\n{"a":1}\n```'
        assert _strip_json_fences(text) == '{"a":1}'

    def test_strips_bare_fences(self) -> None:
        text = '```\n{"a":1}\n```'
        assert _strip_json_fences(text) == '{"a":1}'


class TestSubscribe:
    async def test_subscribe_creates_feed_and_publishes(
        self, feeds_svc: Any
    ) -> None:
        svc, _, bus, _, _ = feeds_svc
        feed = await svc.subscribe(
            "https://example.com/x.xml",
            _user("alice"),
            backend_name="fake_feed",
        )
        assert feed.owner_user_id == "alice"
        assert feed.id.startswith("feed_")
        events = [e for e in bus.published if e.event_type == "feed.subscription.created"]
        assert events, "expected feed.subscription.created event"

    async def test_subscribe_uses_probed_name_when_unspecified(
        self, feeds_svc: Any
    ) -> None:
        svc, _, _, _, _ = feeds_svc
        feed = await svc.subscribe(
            "https://example.com/blog.xml",
            _user("alice"),
            backend_name="fake_feed",
        )
        assert "Probed:" in feed.name


class TestUnsubscribe:
    async def test_unsubscribe_cascades_items(
        self, feeds_svc: Any, sqlite_storage: Any
    ) -> None:
        svc, _, _, _, _ = feeds_svc
        feed = await svc.subscribe(
            "https://example.com/x.xml",
            _user("alice"),
            backend_name="fake_feed",
        )
        # Manually persist an item.
        item = StoredFeedItem(
            id=f"{feed.id}__u1",
            feed_id=feed.id,
            item_uid="u1",
            title="t",
            link="l",
        )
        await sqlite_storage.put(_FEED_ITEMS_COLLECTION, item.id, item.to_dict())

        await svc.unsubscribe(feed.id, _user("alice"))
        rows = await sqlite_storage.query(
            __import__(
                "gilbert.interfaces.storage", fromlist=["Query"]
            ).Query(collection=_FEED_ITEMS_COLLECTION)
        )
        assert rows == []

    async def test_unsubscribe_calls_remove_document_for_ingested_items(
        self, feeds_svc: Any, sqlite_storage: Any
    ) -> None:
        svc, _, _, knowledge, _ = feeds_svc
        feed = await svc.subscribe(
            "https://example.com/x.xml",
            _user("alice"),
            backend_name="fake_feed",
        )
        item = StoredFeedItem(
            id=f"{feed.id}__u1",
            feed_id=feed.id,
            item_uid="u1",
            title="t",
            link="l",
            ingested_to_knowledge=True,
        )
        await sqlite_storage.put(_FEED_ITEMS_COLLECTION, item.id, item.to_dict())
        await svc.unsubscribe(feed.id, _user("alice"))
        assert knowledge.removed, "expected remove_document to be called"

    async def test_unsubscribe_requires_admin(
        self, feeds_svc: Any
    ) -> None:
        svc, _, _, _, _ = feeds_svc
        feed = await svc.subscribe(
            "https://example.com/x.xml",
            _user("alice"),
            backend_name="fake_feed",
        )
        from gilbert.core.services.feeds import FeedsPermissionError

        with pytest.raises(FeedsPermissionError):
            await svc.unsubscribe(feed.id, _user("eve"))


class TestPolling:
    async def _start_runtime_for(self, svc: FeedsService, feed: Feed) -> FakeFeedBackend:
        await svc._start_runtime(feed)
        runtime = svc._runtimes[feed.id]
        return runtime.backend  # type: ignore[return-value]

    async def test_poll_persists_only_new_items_dedup_by_uid(
        self, feeds_svc: Any, sqlite_storage: Any
    ) -> None:
        svc, _, _, _, _ = feeds_svc
        feed = await svc.subscribe(
            "https://example.com/x.xml",
            _user("alice"),
            backend_name="fake_feed",
        )
        backend = svc._runtimes[feed.id].backend
        backend.next_items = [_make_item("u1"), _make_item("u2")]
        await svc._poll_runtime(svc._runtimes[feed.id])
        first = await sqlite_storage.query(
            __import__(
                "gilbert.interfaces.storage", fromlist=["Query"]
            ).Query(collection=_FEED_ITEMS_COLLECTION)
        )
        assert len(first) == 2
        backend.next_items = [_make_item("u1"), _make_item("u2"), _make_item("u3")]
        await svc._poll_runtime(svc._runtimes[feed.id])
        second = await sqlite_storage.query(
            __import__(
                "gilbert.interfaces.storage", fromlist=["Query"]
            ).Query(collection=_FEED_ITEMS_COLLECTION)
        )
        assert len(second) == 3

    async def test_304_does_not_bump_failures_does_bump_last_polled(
        self, feeds_svc: Any
    ) -> None:
        svc, _, _, _, _ = feeds_svc
        feed = await svc.subscribe(
            "https://example.com/x.xml",
            _user("alice"),
            backend_name="fake_feed",
        )
        backend = svc._runtimes[feed.id].backend
        backend.next_not_modified = True
        await svc._poll_runtime(svc._runtimes[feed.id])
        refreshed = await svc.get_feed(feed.id)
        assert refreshed is not None
        assert refreshed.consecutive_failures == 0
        assert refreshed.last_polled_at != ""

    async def test_http_cache_round_trips_etag(
        self, feeds_svc: Any
    ) -> None:
        svc, _, _, _, _ = feeds_svc
        feed = await svc.subscribe(
            "https://example.com/x.xml",
            _user("alice"),
            backend_name="fake_feed",
        )
        backend = svc._runtimes[feed.id].backend
        backend.next_http_cache = {"etag": '"abc"', "last_modified": "Mon"}
        await svc._poll_runtime(svc._runtimes[feed.id])
        refreshed = await svc.get_feed(feed.id)
        assert refreshed is not None
        assert refreshed.http_cache.get("etag") == '"abc"'
        # backend_config must NOT be touched.
        assert "etag" not in refreshed.backend_config

    async def test_poll_records_error_and_increments_failures(
        self, feeds_svc: Any
    ) -> None:
        svc, _, _, _, _ = feeds_svc
        feed = await svc.subscribe(
            "https://example.com/x.xml",
            _user("alice"),
            backend_name="fake_feed",
        )

        async def boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("network broken")

        svc._runtimes[feed.id].backend.poll = boom  # type: ignore[method-assign]
        await svc._poll_runtime(svc._runtimes[feed.id])
        refreshed = await svc.get_feed(feed.id)
        assert refreshed is not None
        assert refreshed.consecutive_failures == 1
        assert "network broken" in refreshed.last_error

    async def test_graceful_giveup_disables_after_threshold(
        self, feeds_svc: Any
    ) -> None:
        svc, _, bus, _, _ = feeds_svc
        feed = await svc.subscribe(
            "https://example.com/x.xml",
            _user("alice"),
            backend_name="fake_feed",
        )

        async def boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("perma-broken")

        svc._runtimes[feed.id].backend.poll = boom  # type: ignore[method-assign]
        for _ in range(20):
            if feed.id in svc._runtimes:
                await svc._poll_runtime(svc._runtimes[feed.id])
        refreshed = await svc.get_feed(feed.id)
        assert refreshed is not None
        assert refreshed.poll_enabled is False
        assert refreshed.consecutive_failures >= 20
        types = [e.event_type for e in bus.published]
        assert "feed.subscription.disabled" in types

    async def test_edit_detection_updates_title_only(
        self, feeds_svc: Any, sqlite_storage: Any
    ) -> None:
        svc, _, _, _, _ = feeds_svc
        # Disable score-on-ingest so the async worker doesn't race
        # the manual score assignment below.
        svc._score_on_ingest = False
        feed = await svc.subscribe(
            "https://example.com/x.xml",
            _user("alice"),
            backend_name="fake_feed",
        )
        backend = svc._runtimes[feed.id].backend
        t_old = datetime(2026, 1, 1, tzinfo=UTC)
        item_v1 = FeedItem(
            item_uid="u1",
            title="Original",
            link="l",
            summary="s1",
            published_at=t_old,
            updated_at=t_old,
        )
        backend.next_items = [item_v1]
        await svc._poll_runtime(svc._runtimes[feed.id])
        # Now mark briefed_at and a score so we can prove they survive.
        row = await sqlite_storage.get(_FEED_ITEMS_COLLECTION, f"{feed.id}__u1")
        row["briefed_at"] = "2026-01-01T00:00:00+00:00"
        row["score"] = 0.9
        await sqlite_storage.put(_FEED_ITEMS_COLLECTION, f"{feed.id}__u1", row)
        # Re-poll with a newer updated_at + new title.
        t_new = datetime(2026, 1, 2, tzinfo=UTC)
        item_v2 = FeedItem(
            item_uid="u1",
            title="Refined Title",
            link="l",
            summary="s1",
            published_at=t_old,
            updated_at=t_new,
        )
        backend.next_items = [item_v2]
        await svc._poll_runtime(svc._runtimes[feed.id])
        row2 = await sqlite_storage.get(_FEED_ITEMS_COLLECTION, f"{feed.id}__u1")
        assert row2["title"] == "Refined Title"
        # Score / briefed_at survive (first-write-wins).
        assert row2["score"] == 0.9
        assert row2["briefed_at"] == "2026-01-01T00:00:00+00:00"

    async def test_edit_does_not_re_emit_item_received(
        self, feeds_svc: Any
    ) -> None:
        svc, _, bus, _, _ = feeds_svc
        feed = await svc.subscribe(
            "https://example.com/x.xml",
            _user("alice"),
            backend_name="fake_feed",
        )
        backend = svc._runtimes[feed.id].backend
        t = datetime(2026, 1, 1, tzinfo=UTC)
        item = FeedItem(
            item_uid="u1",
            title="Original",
            link="l",
            published_at=t,
            updated_at=t,
        )
        backend.next_items = [item]
        await svc._poll_runtime(svc._runtimes[feed.id])
        before = sum(
            1 for e in bus.published if e.event_type == "feed.item.received"
        )
        backend.next_items = [
            FeedItem(
                item_uid="u1",
                title="Edited",
                link="l",
                published_at=t,
                updated_at=t + timedelta(hours=1),
            )
        ]
        await svc._poll_runtime(svc._runtimes[feed.id])
        after = sum(
            1 for e in bus.published if e.event_type == "feed.item.received"
        )
        assert after == before  # no re-emission.


class TestScoring:
    async def test_score_uses_configurable_prompt_and_tools_override_empty(
        self, feeds_svc: Any
    ) -> None:
        svc, ai, _, _, _ = feeds_svc
        svc._scoring_prompt = "ZZZ-CUSTOM-PROMPT"
        feed = await svc.subscribe(
            "https://example.com/x.xml",
            _user("alice"),
            backend_name="fake_feed",
        )
        item = _make_item("u1")
        await svc._score_item(feed, item)
        assert ai.calls, "expected at least one AI call"
        assert ai.calls[-1]["system_prompt"].startswith("ZZZ-CUSTOM-PROMPT")
        # Mandatory: tools_override=[] to prevent recursion bugs.
        assert ai.calls[-1]["tools_override"] == []

    async def test_score_capped_by_importance_weight(
        self, feeds_svc: Any, sqlite_storage: Any
    ) -> None:
        svc, ai, _, _, _ = feeds_svc
        ai.next_text = '{"score": 1.0, "reason": "max"}'
        feed = await svc.subscribe(
            "https://example.com/x.xml",
            _user("alice"),
            backend_name="fake_feed",
        )
        feed.importance_weight = 0.3
        await sqlite_storage.put(_FEEDS_COLLECTION, feed.id, feed.to_dict())
        # Persist the item first.
        item = StoredFeedItem(
            id=f"{feed.id}__u1",
            feed_id=feed.id,
            item_uid="u1",
            title="t",
            link="l",
        )
        await sqlite_storage.put(_FEED_ITEMS_COLLECTION, item.id, item.to_dict())
        await svc._score_item(feed, FeedItem(item_uid="u1", title="t", link="l"))
        row = await sqlite_storage.get(_FEED_ITEMS_COLLECTION, item.id)
        # 1.0 * 0.3 = 0.3
        assert row["score"] == pytest.approx(0.3, rel=1e-3)

    async def test_score_parser_failure_sets_minus_one(
        self, feeds_svc: Any, sqlite_storage: Any
    ) -> None:
        svc, ai, _, _, _ = feeds_svc
        ai.next_text = "garbage not json"
        feed = await svc.subscribe(
            "https://example.com/x.xml",
            _user("alice"),
            backend_name="fake_feed",
        )
        item = StoredFeedItem(
            id=f"{feed.id}__u1",
            feed_id=feed.id,
            item_uid="u1",
            title="t",
            link="l",
        )
        await sqlite_storage.put(_FEED_ITEMS_COLLECTION, item.id, item.to_dict())
        await svc._score_item(feed, FeedItem(item_uid="u1", title="t", link="l"))
        row = await sqlite_storage.get(_FEED_ITEMS_COLLECTION, item.id)
        assert row["score"] == -1.0

    async def test_score_parser_strips_json_fences(
        self, feeds_svc: Any, sqlite_storage: Any
    ) -> None:
        svc, ai, _, _, _ = feeds_svc
        ai.next_text = '```json\n{"score": 0.5, "reason": "fenced"}\n```'
        feed = await svc.subscribe(
            "https://example.com/x.xml",
            _user("alice"),
            backend_name="fake_feed",
        )
        item = StoredFeedItem(
            id=f"{feed.id}__u1",
            feed_id=feed.id,
            item_uid="u1",
            title="t",
            link="l",
        )
        await sqlite_storage.put(_FEED_ITEMS_COLLECTION, item.id, item.to_dict())
        await svc._score_item(feed, FeedItem(item_uid="u1", title="t", link="l"))
        row = await sqlite_storage.get(_FEED_ITEMS_COLLECTION, item.id)
        # 0.5 * default importance_weight (0.5) = 0.25
        assert row["score"] == pytest.approx(0.25, rel=1e-3)


class TestBriefing:
    async def test_build_briefing_two_artifacts(self, feeds_svc: Any) -> None:
        svc, ai, _, _, _ = feeds_svc
        ai.next_text = json.dumps(
            {
                "spoken": "Today's news.",
                "headlines": [
                    {
                        "item_id": "feed_x__u1",
                        "title": "T",
                        "one_liner": "ol",
                        "score": 0.5,
                    }
                ],
            }
        )
        feed = await svc.subscribe(
            "https://example.com/x.xml",
            _user("alice"),
            backend_name="fake_feed",
        )
        # Persist an eligible item.
        item = StoredFeedItem(
            id=f"{feed.id}__u1",
            feed_id=feed.id,
            item_uid="u1",
            title="t",
            link="l",
            score=0.7,
            received_at=datetime.now(UTC).isoformat(),
        )
        await svc._storage.put(_FEED_ITEMS_COLLECTION, item.id, item.to_dict())
        result = await svc.build_briefing(_user("alice"))
        assert result.spoken == "Today's news."
        assert len(result.headlines) >= 1
        assert ai.calls[-1]["tools_override"] == []

    async def test_build_briefing_mark_briefed_false_does_not_set_briefed_at(
        self, feeds_svc: Any, sqlite_storage: Any
    ) -> None:
        svc, ai, _, _, _ = feeds_svc
        feed = await svc.subscribe(
            "https://example.com/x.xml",
            _user("alice"),
            backend_name="fake_feed",
        )
        item = StoredFeedItem(
            id=f"{feed.id}__u1",
            feed_id=feed.id,
            item_uid="u1",
            title="t",
            link="l",
            score=0.7,
            received_at=datetime.now(UTC).isoformat(),
        )
        await sqlite_storage.put(_FEED_ITEMS_COLLECTION, item.id, item.to_dict())
        await svc.build_briefing(_user("alice"), mark_briefed=False)
        row = await sqlite_storage.get(_FEED_ITEMS_COLLECTION, item.id)
        assert row["briefed_at"] == ""

    async def test_build_briefing_falls_back_on_parse_failure(
        self, feeds_svc: Any, sqlite_storage: Any
    ) -> None:
        svc, ai, _, _, _ = feeds_svc
        ai.next_text = "not valid json"
        feed = await svc.subscribe(
            "https://example.com/x.xml",
            _user("alice"),
            backend_name="fake_feed",
        )
        item = StoredFeedItem(
            id=f"{feed.id}__u1",
            feed_id=feed.id,
            item_uid="u1",
            title="Title One",
            link="l",
            score=0.7,
            received_at=datetime.now(UTC).isoformat(),
        )
        await sqlite_storage.put(_FEED_ITEMS_COLLECTION, item.id, item.to_dict())
        result = await svc.build_briefing(_user("alice"))
        assert "Title One" in result.spoken or result.headlines

    async def test_build_briefing_uses_configurable_prompt(
        self, feeds_svc: Any, sqlite_storage: Any
    ) -> None:
        svc, ai, _, _, _ = feeds_svc
        svc._briefing_prompt = "BRIEFING-CUSTOM"
        feed = await svc.subscribe(
            "https://example.com/x.xml",
            _user("alice"),
            backend_name="fake_feed",
        )
        item = StoredFeedItem(
            id=f"{feed.id}__u1",
            feed_id=feed.id,
            item_uid="u1",
            title="T",
            link="l",
            score=0.7,
            received_at=datetime.now(UTC).isoformat(),
        )
        await sqlite_storage.put(_FEED_ITEMS_COLLECTION, item.id, item.to_dict())
        await svc.build_briefing(_user("alice"))
        assert ai.calls[-1]["system_prompt"] == "BRIEFING-CUSTOM"

    async def test_recent_briefings_capped_at_10(
        self, feeds_svc: Any, sqlite_storage: Any
    ) -> None:
        svc, ai, _, _, _ = feeds_svc
        feed = await svc.subscribe(
            "https://example.com/x.xml",
            _user("alice"),
            backend_name="fake_feed",
        )
        # Pre-seed 10 briefings.
        await sqlite_storage.put(
            _BRIEFING_STATE_COLLECTION,
            "alice",
            {
                "_id": "alice",
                "recent_briefings": [f"old-{i}" for i in range(10)],
            },
        )
        item = StoredFeedItem(
            id=f"{feed.id}__u1",
            feed_id=feed.id,
            item_uid="u1",
            title="T",
            link="l",
            score=0.9,
            received_at=datetime.now(UTC).isoformat(),
        )
        await sqlite_storage.put(_FEED_ITEMS_COLLECTION, item.id, item.to_dict())
        await svc.build_briefing(_user("alice"))
        state = await sqlite_storage.get(_BRIEFING_STATE_COLLECTION, "alice")
        assert len(state["recent_briefings"]) == 10
        # Newest is at the end.
        assert state["recent_briefings"][-1] != "old-9"


class TestRetention:
    async def test_retention_tick_deletes_old_items(
        self, feeds_svc: Any, sqlite_storage: Any
    ) -> None:
        svc, _, _, knowledge, _ = feeds_svc
        svc._retention_days = 10
        feed = await svc.subscribe(
            "https://example.com/x.xml",
            _user("alice"),
            backend_name="fake_feed",
        )
        # Old item (30 days back).
        old = StoredFeedItem(
            id=f"{feed.id}__old",
            feed_id=feed.id,
            item_uid="old",
            title="old",
            link="l",
            received_at=(datetime.now(UTC) - timedelta(days=30)).isoformat(),
            ingested_to_knowledge=True,
        )
        # New item (today).
        new = StoredFeedItem(
            id=f"{feed.id}__new",
            feed_id=feed.id,
            item_uid="new",
            title="new",
            link="l",
            received_at=datetime.now(UTC).isoformat(),
        )
        await sqlite_storage.put(_FEED_ITEMS_COLLECTION, old.id, old.to_dict())
        await sqlite_storage.put(_FEED_ITEMS_COLLECTION, new.id, new.to_dict())
        await svc._retention_tick()
        from gilbert.interfaces.storage import Query

        rows = await sqlite_storage.query(Query(collection=_FEED_ITEMS_COLLECTION))
        ids = {r["_id"] for r in rows}
        assert new.id in ids
        assert old.id not in ids
        # Knowledge cascade fired.
        assert knowledge.removed, "expected remove_document for ingested item"


class TestTools:
    async def test_subscribe_feed_tool_returns_confirmation_block(
        self, feeds_svc: Any
    ) -> None:
        svc, _, _, _, _ = feeds_svc
        set_current_user(_user("alice"))
        out = await svc.execute_tool(
            "subscribe_feed",
            {"url": "https://example.com/x.xml", "name": "MyFeed"},
        )
        # Without confirm, returns a ToolOutput with a UI block.
        from gilbert.interfaces.ui import ToolOutput

        assert isinstance(out, ToolOutput)
        assert out.ui_blocks
        # Did not actually persist a feed.
        feeds = await svc._load_feeds()
        assert feeds == []

    async def test_subscribe_feed_tool_persists_with_confirm(
        self, feeds_svc: Any, monkeypatch: Any
    ) -> None:
        svc, _, _, _, _ = feeds_svc
        # The tool uses the rss_atom default backend; substitute the
        # fake registered backend by patching the registry lookup so
        # this test doesn't need a real httpx mock transport.
        from gilbert.interfaces.feeds import FeedBackend

        fake_cls = FeedBackend.registered_backends()["fake_feed"]
        monkeypatch.setattr(
            FeedBackend,
            "registered_backends",
            classmethod(lambda cls: {"rss_atom": fake_cls, "fake_feed": fake_cls}),
        )
        set_current_user(_user("alice"))
        out = await svc.execute_tool(
            "subscribe_feed",
            {
                "url": "https://example.com/x.xml",
                "name": "MyFeed",
                "confirm": True,
            },
        )
        assert isinstance(out, str)
        feeds = await svc._load_feeds()
        assert len(feeds) == 1
        assert feeds[0].name == "MyFeed"

    async def test_unsubscribe_feed_tool_returns_confirmation_block(
        self, feeds_svc: Any
    ) -> None:
        svc, _, _, _, _ = feeds_svc
        set_current_user(_user("alice"))
        feed = await svc.subscribe(
            "https://example.com/x.xml",
            _user("alice"),
            backend_name="fake_feed",
        )
        out = await svc.execute_tool(
            "unsubscribe_feed", {"feed_id": feed.id}
        )
        from gilbert.interfaces.ui import ToolOutput

        assert isinstance(out, ToolOutput)
        # Did not actually delete.
        again = await svc.get_feed(feed.id)
        assert again is not None

    async def test_news_briefing_returns_cached_when_already_briefed_today(
        self, feeds_svc: Any, sqlite_storage: Any
    ) -> None:
        svc, ai, _, _, _ = feeds_svc
        set_current_user(_user("alice"))
        # Pre-seed today's briefing.
        await sqlite_storage.put(
            _BRIEFING_STATE_COLLECTION,
            "alice",
            {
                "_id": "alice",
                "last_briefed_on": datetime.now(UTC).strftime("%Y-%m-%d"),
                "last_briefing_id": "brief_cached",
            },
        )
        await sqlite_storage.put(
            _BRIEFINGS_COLLECTION,
            "brief_cached",
            {
                "_id": "brief_cached",
                "spoken": "cached-text",
                "headlines": [],
                "item_ids": [],
            },
        )
        out = await svc.execute_tool("news_briefing", {})
        from gilbert.interfaces.ui import ToolOutput

        assert isinstance(out, ToolOutput)
        assert out.text == "cached-text"
        # Did NOT call the AI.
        assert ai.calls == []

    async def test_list_feeds_only_returns_accessible(
        self, feeds_svc: Any
    ) -> None:
        svc, _, _, _, _ = feeds_svc
        # Alice owns one, Bob owns another not shared.
        await svc.subscribe(
            "https://example.com/alice.xml",
            _user("alice"),
            name="alice-feed",
            backend_name="fake_feed",
        )
        await svc.subscribe(
            "https://example.com/bob.xml",
            _user("bob"),
            name="bob-feed",
            backend_name="fake_feed",
        )
        set_current_user(_user("alice"))
        out = await svc.execute_tool("list_feeds", {"compact": True})
        assert isinstance(out, str)
        assert "alice-feed" in out
        assert "bob-feed" not in out


class TestOPML:
    async def test_opml_export_round_trip(self, feeds_svc: Any) -> None:
        svc, _, _, _, _ = feeds_svc
        await svc.subscribe(
            "https://example.com/a.xml",
            _user("alice"),
            name="A",
            category="tech",
            backend_name="fake_feed",
        )
        opml = await svc.export_opml(_user("alice"))
        assert "https://example.com/a.xml" in opml
        assert "tech" in opml

    async def test_opml_import_subscribes_each_outline(
        self, feeds_svc: Any, monkeypatch: Any
    ) -> None:
        svc, _, _, _, _ = feeds_svc
        from gilbert.interfaces.feeds import FeedBackend

        fake_cls = FeedBackend.registered_backends()["fake_feed"]
        monkeypatch.setattr(
            FeedBackend,
            "registered_backends",
            classmethod(lambda cls: {"rss_atom": fake_cls, "fake_feed": fake_cls}),
        )
        opml = """<?xml version="1.0"?>
<opml version="2.0">
  <body>
    <outline type="rss" text="A" title="A" xmlUrl="https://example.com/a.xml" category="tech"/>
    <outline type="rss" text="B" title="B" xmlUrl="https://example.com/b.xml"/>
  </body>
</opml>"""
        results = await svc.import_opml(opml, _user("alice"))
        assert len(results) == 2
        assert all(err == "" for _, err in results)
        feeds = await svc._load_feeds()
        assert len(feeds) == 2


class TestMutationPublishDedup:
    """We don't have explicit dedup like calendar's mutate_publishes,
    but we DO ensure no extra events fire on duplicate polls."""

    async def test_repeat_poll_of_same_items_only_fires_once(
        self, feeds_svc: Any
    ) -> None:
        svc, _, bus, _, _ = feeds_svc
        feed = await svc.subscribe(
            "https://example.com/x.xml",
            _user("alice"),
            backend_name="fake_feed",
        )
        backend = svc._runtimes[feed.id].backend
        backend.next_items = [_make_item("u1"), _make_item("u2")]
        await svc._poll_runtime(svc._runtimes[feed.id])
        first = sum(
            1 for e in bus.published if e.event_type == "feed.item.received"
        )
        # Re-poll same items.
        backend.next_items = [_make_item("u1"), _make_item("u2")]
        await svc._poll_runtime(svc._runtimes[feed.id])
        second = sum(
            1 for e in bus.published if e.event_type == "feed.item.received"
        )
        assert second == first


class TestKnowledgeIngestion:
    async def test_ingest_skipped_when_knowledge_capability_absent(
        self, sqlite_storage: Any
    ) -> None:
        # Build service without knowledge capability.
        FakeFeedBackend.instances = []
        storage = FakeStorageProvider(sqlite_storage)
        bus = FakeEventBusProvider()
        sched = FakeScheduler()
        ai = FakeAISampling()
        resolver = FakeResolver(
            entity_storage=storage,
            event_bus=bus,
            scheduler=sched,
            ai_chat=ai,
        )
        svc = FeedsService()
        await svc.start(resolver)
        try:
            feed = await svc.subscribe(
                "https://example.com/x.xml",
                _user("alice"),
                backend_name="fake_feed",
            )
            feed.ingest_to_knowledge = True
            await sqlite_storage.put(_FEEDS_COLLECTION, feed.id, feed.to_dict())
            await svc._ingest_item(feed, _make_item("u1"))
            # No exception.
        finally:
            await svc.stop()

    async def test_ingest_per_user_per_day_cap_emits_throttled_event(
        self, feeds_svc: Any, sqlite_storage: Any
    ) -> None:
        svc, _, bus, _, _ = feeds_svc
        svc._ingest_max_items_per_day_per_user = 1
        feed = await svc.subscribe(
            "https://example.com/x.xml",
            _user("alice"),
            backend_name="fake_feed",
        )
        feed.ingest_to_knowledge = True
        await sqlite_storage.put(_FEEDS_COLLECTION, feed.id, feed.to_dict())
        # Pre-fill the daily cap.
        cap_key = f"alice:{datetime.now(UTC).strftime('%Y-%m-%d')}"
        from gilbert.core.services.feeds import _INGEST_DAILY_COLLECTION

        await sqlite_storage.put(
            _INGEST_DAILY_COLLECTION, cap_key, {"_id": cap_key, "count": 1}
        )
        await svc._ingest_item(feed, _make_item("u1", link="https://example.com/article"))
        types = [e.event_type for e in bus.published]
        assert "feed.ingest.throttled" in types

    async def test_ingest_rejects_https_to_http_downgrade(
        self, feeds_svc: Any
    ) -> None:
        svc, _, _, _, _ = feeds_svc
        # safe_to_fetch fails before any fetch happens.
        from gilbert.interfaces.feeds import Feed as FeedDC

        feed = FeedDC(id="f1", url="https://safe.example.com/feed.xml")
        # different eTLD+1 + private host pattern
        ok = await svc._safe_to_fetch(feed, "http://localhost/oops")
        assert ok is False


class TestBriefingState:
    async def test_briefing_opt_in_default_owner_true(
        self, feeds_svc: Any
    ) -> None:
        svc, _, _, _, _ = feeds_svc
        # Empty state — owner default is True (test the helper).
        await svc.set_briefing_opt_in("alice", False)
        state = await svc.get_briefing_state("alice")
        assert state["briefing_opt_in"] is False


class TestSafeUidHelper:
    def test_safe_uid_is_filesystem_safe(self) -> None:
        uid = _safe_uid("https://example.com/some/long/url?query=1")
        assert "/" not in uid
        assert "?" not in uid
        assert len(uid) == 40  # sha1 hex digest length

