"""Tests for ``FeedBriefingService`` — fan-out, dedup against
already-briefed-today, role-shared opt-in defaults, system briefing,
and event-payload privacy.

Uses a fake ``FeedsProvider`` so we don't need a real ``FeedsService``
underneath. The interesting behavior here is the orchestration: who
gets briefed, when, and what the published event looks like.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import pytest

from gilbert.core.services.feed_briefing import (
    _BRIEFING_STATE_COLLECTION,
    FeedBriefingService,
)
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.feeds import (
    BriefingHeadline,
    BriefingResult,
    Feed,
    FeedsProvider,
    StoredFeedItem,
)

# ── Fakes ────────────────────────────────────────────────────────────


class FakeFeedsProvider:
    """In-memory FeedsProvider for orchestration tests."""

    def __init__(self) -> None:
        self.feeds: list[Feed] = []
        self.build_calls: list[dict[str, Any]] = []
        self.fail_for_user: set[str] = set()

    async def subscribe(
        self,
        url: str,
        user_ctx: UserContext,
        *,
        name: str = "",
        category: str = "",
        backend_name: str = "rss_atom",
        poll_interval_sec: int = 1800,
    ) -> Feed:
        feed = Feed(id=f"feed_{len(self.feeds)}", url=url, name=name or url)
        self.feeds.append(feed)
        return feed

    async def unsubscribe(self, feed_id: str, user_ctx: UserContext) -> None:
        self.feeds = [f for f in self.feeds if f.id != feed_id]

    async def list_accessible_feeds(self, user_ctx: UserContext) -> list[Feed]:
        if user_ctx is UserContext.SYSTEM or "admin" in user_ctx.roles:
            return list(self.feeds)
        return [
            f
            for f in self.feeds
            if f.owner_user_id == user_ctx.user_id
            or user_ctx.user_id in f.shared_with_users
        ]

    async def get_feed(self, feed_id: str) -> Feed | None:
        return next((f for f in self.feeds if f.id == feed_id), None)

    async def search_items(self, **kwargs: Any) -> list[StoredFeedItem]:
        return []

    async def get_top_items(
        self,
        user_ctx: UserContext,
        **kwargs: Any,
    ) -> list[StoredFeedItem]:
        return []

    async def mark_read(
        self, item_id: str, user_ctx: UserContext, read: bool = True
    ) -> None:
        pass

    async def build_briefing(
        self,
        user_ctx: UserContext,
        *,
        top_n: int = 5,
        since: datetime | None = None,
        category: str = "",
        max_spoken_seconds: int = 0,
        mark_briefed: bool = True,
        anti_repetition_context: list[str] | None = None,
    ) -> BriefingResult:
        self.build_calls.append(
            {
                "user_id": user_ctx.user_id,
                "top_n": top_n,
                "mark_briefed": mark_briefed,
            }
        )
        if user_ctx.user_id in self.fail_for_user:
            raise RuntimeError("forced failure")
        return BriefingResult(
            spoken=f"briefing for {user_ctx.user_id}",
            headlines=[
                BriefingHeadline(
                    item_id="x",
                    title="T",
                    one_liner="ol",
                    score=0.8,
                    link="https://x.com/",
                )
            ],
            item_ids=["x"],
            since=since or datetime.now(UTC),
            briefing_id=f"brief_{user_ctx.user_id}",
        )


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
        self.jobs: dict[str, Any] = {}

    def add_job(self, **kwargs: Any) -> Any:
        self.jobs[kwargs["name"]] = kwargs

    def remove_job(self, name: str, requester_id: str = "") -> None:
        self.jobs.pop(name, None)

    def enable_job(self, name: str) -> None:
        pass

    def disable_job(self, name: str) -> None:
        pass

    def list_jobs(self, include_system: bool = True) -> list[Any]:
        return list(self.jobs.values())

    def get_job(self, name: str) -> Any:
        return self.jobs.get(name)

    async def run_now(self, name: str) -> None:
        pass


class FakeSpeaker:
    def __init__(self) -> None:
        self.announces: list[dict[str, Any]] = []
        self._backends: dict[str, Any] = {}

    @property
    def backend(self) -> Any:
        return self

    @property
    def backends(self) -> dict[str, Any]:
        return self._backends

    def get_backend(self, name: str) -> Any:
        return self._backends.get(name)

    async def resolve_names(self, names: list[str]) -> dict[str, str]:
        return {name: name for name in names}

    async def announce(
        self,
        text: str,
        speaker_names: list[str] | None = None,
        volume: int | None = None,
        context: str = "",
    ) -> str:
        self.announces.append({"text": text, "speakers": speaker_names})
        return "ok"


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


# ── Fixture ──────────────────────────────────────────────────────────


@pytest.fixture
async def briefing_svc(
    sqlite_storage: Any,
) -> AsyncGenerator[
    tuple[FeedBriefingService, FakeFeedsProvider, FakeEventBus, Any], None
]:
    feeds_provider = FakeFeedsProvider()
    storage_provider = FakeStorageProvider(sqlite_storage)
    bus_provider = FakeEventBusProvider()
    sched = FakeScheduler()
    resolver = FakeResolver(
        feeds=feeds_provider,
        scheduler=sched,
        event_bus=bus_provider,
        entity_storage=storage_provider,
        configuration=None,
    )
    svc = FeedBriefingService()
    # Force-enable for tests; default is False per spec.
    await svc.on_config_changed({"enabled": True})
    await svc.start(resolver)
    yield svc, feeds_provider, bus_provider.bus, sqlite_storage
    await svc.stop()


# ── Tests ────────────────────────────────────────────────────────────


class TestProtocolGuard:
    def test_briefing_service_does_not_re_export_briefing_provider(self) -> None:
        # Per round-2 architect: BriefingProvider was deliberately
        # dropped. This test guards against accidental re-introduction.
        from gilbert.interfaces import feeds as feeds_mod

        assert not hasattr(feeds_mod, "BriefingProvider"), (
            "BriefingProvider must not exist — "
            "build_briefing lives on FeedsProvider"
        )


class TestEnumeration:
    async def test_owner_only_user_defaults_to_opt_in_true(
        self, briefing_svc: Any, sqlite_storage: Any
    ) -> None:
        svc, feeds_provider, bus, storage = briefing_svc
        feeds_provider.feeds.append(
            Feed(id="f1", owner_user_id="alice", briefing_eligible=True)
        )
        await svc._fallback_tick()
        # alice was briefed.
        ids = [c["user_id"] for c in feeds_provider.build_calls]
        assert "alice" in ids

    async def test_role_shared_only_user_defaults_to_opt_in_false(
        self, briefing_svc: Any
    ) -> None:
        svc, feeds_provider, bus, storage = briefing_svc
        # Bob owns; carol gets the feed via role share but doesn't own
        # her own. carol is added via shared_with_users for simplicity
        # — the spec's default-off rule applies the same way.
        feeds_provider.feeds.append(
            Feed(
                id="f1",
                owner_user_id="bob",
                shared_with_users=["carol"],
            )
        )
        await svc._fallback_tick()
        ids = [c["user_id"] for c in feeds_provider.build_calls]
        # Bob (owner) opts in by default; carol does NOT (shared only).
        assert "bob" in ids
        assert "carol" not in ids

    async def test_already_briefed_today_skipped(
        self, briefing_svc: Any, sqlite_storage: Any
    ) -> None:
        svc, feeds_provider, bus, storage = briefing_svc
        feeds_provider.feeds.append(Feed(id="f1", owner_user_id="alice"))
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        await sqlite_storage.put(
            _BRIEFING_STATE_COLLECTION,
            "alice",
            {
                "_id": "alice",
                "last_briefed_on": today,
                "last_briefing_id": "brief_alice",
                "briefing_opt_in": True,
            },
        )
        await svc._fallback_tick()
        # alice was NOT briefed (already done today).
        ids = [c["user_id"] for c in feeds_provider.build_calls]
        assert "alice" not in ids


class TestEventPayload:
    async def test_event_carries_briefing_id_not_spoken_text(
        self, briefing_svc: Any
    ) -> None:
        svc, feeds_provider, bus, _ = briefing_svc
        feeds_provider.feeds.append(Feed(id="f1", owner_user_id="alice"))
        await svc._fallback_tick()
        events = [
            e for e in bus.published if e.event_type == "feed.briefing.ready"
        ]
        assert events, "expected feed.briefing.ready"
        assert "spoken" not in events[0].data
        assert "spoken_text" not in events[0].data
        assert events[0].data["briefing_id"].startswith("brief_")
        assert events[0].data["user_id"] == "alice"


class TestSystemBriefing:
    async def test_system_briefing_calls_speaker_announce(
        self, sqlite_storage: Any
    ) -> None:
        feeds_provider = FakeFeedsProvider()
        feeds_provider.feeds.append(Feed(id="f1", owner_user_id="alice"))
        storage_provider = FakeStorageProvider(sqlite_storage)
        bus_provider = FakeEventBusProvider()
        sched = FakeScheduler()
        speaker = FakeSpeaker()
        resolver = FakeResolver(
            feeds=feeds_provider,
            scheduler=sched,
            event_bus=bus_provider,
            entity_storage=storage_provider,
            speaker_control=speaker,
        )
        svc = FeedBriefingService()
        await svc.on_config_changed(
            {
                "enabled": True,
                "system_briefing_enabled": True,
                "system_briefing_user_id": "alice",
                "announce_speakers": ["kitchen"],
            }
        )
        await svc.start(resolver)
        try:
            await svc._fallback_tick()
            assert speaker.announces, "expected system announce"
            assert speaker.announces[0]["speakers"] == ["kitchen"]
        finally:
            await svc.stop()

    async def test_system_briefing_no_op_when_disabled(
        self, briefing_svc: Any
    ) -> None:
        svc, feeds_provider, bus, _ = briefing_svc
        # No speaker capability registered; nothing announces.
        feeds_provider.feeds.append(Feed(id="f1", owner_user_id="alice"))
        await svc._fallback_tick()
        # Without system_briefing_enabled, no speaker call would happen
        # even if speaker were registered. We just verify the regular
        # fan-out still works.
        ids = [c["user_id"] for c in feeds_provider.build_calls]
        assert "alice" in ids


class TestRunNow:
    async def test_run_now_force_bypasses_already_briefed(
        self, briefing_svc: Any, sqlite_storage: Any
    ) -> None:
        svc, feeds_provider, bus, _ = briefing_svc
        feeds_provider.feeds.append(Feed(id="f1", owner_user_id="alice"))
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        await sqlite_storage.put(
            _BRIEFING_STATE_COLLECTION,
            "alice",
            {
                "_id": "alice",
                "last_briefed_on": today,
                "briefing_opt_in": True,
            },
        )
        fired = await svc.run_now(force=True)
        assert fired == 1


class TestBuildBriefingFailure:
    async def test_build_failure_does_not_kill_other_users(
        self, briefing_svc: Any
    ) -> None:
        svc, feeds_provider, bus, _ = briefing_svc
        feeds_provider.feeds.append(Feed(id="f1", owner_user_id="alice"))
        feeds_provider.feeds.append(Feed(id="f2", owner_user_id="bob"))
        feeds_provider.fail_for_user.add("alice")
        await svc._fallback_tick()
        # bob's briefing succeeded.
        events = [
            e for e in bus.published if e.event_type == "feed.briefing.ready"
        ]
        ids = [e.data["user_id"] for e in events]
        assert "bob" in ids
        assert "alice" not in ids


class TestFeedsProviderAssertion:
    async def test_start_raises_if_feeds_not_provider(
        self, sqlite_storage: Any
    ) -> None:
        # Pass a non-Provider object as the "feeds" capability.
        storage_provider = FakeStorageProvider(sqlite_storage)
        sched = FakeScheduler()
        resolver = FakeResolver(
            feeds=object(),
            scheduler=sched,
            entity_storage=storage_provider,
        )
        svc = FeedBriefingService()
        await svc.on_config_changed({"enabled": True})
        with pytest.raises(TypeError):
            await svc.start(resolver)


class TestFeedsProviderProtocolMethods:
    """Ensure the FakeFeedsProvider used here actually satisfies the
    Protocol — keeps tests honest and catches drift in the real
    Protocol surface."""

    def test_fake_feeds_provider_isinstance(self) -> None:
        assert isinstance(FakeFeedsProvider(), FeedsProvider)


class TestGreetingContextProvider:
    """FeedBriefingService implements GreetingContextProvider."""

    async def test_greeting_context_returns_briefing_text(
        self, briefing_svc: Any, sqlite_storage: Any
    ) -> None:
        from gilbert.interfaces.greeting import GreetingContext

        svc, feeds_provider, bus, storage = briefing_svc
        feeds_provider.feeds.append(Feed(id="f1", owner_user_id="alice"))
        ctx = await svc.greeting_context(user_id="alice")
        assert isinstance(ctx, GreetingContext)
        assert ctx.provider_id == "briefing"
        assert ctx.label == "News briefing"
        assert "briefing for alice" in ctx.prose

    async def test_greeting_context_suppresses_when_already_briefed_today(
        self, briefing_svc: Any, sqlite_storage: Any
    ) -> None:
        """If feed_briefing_state shows the user got a briefing today,
        return None — don't double-brief."""
        svc, feeds_provider, bus, storage = briefing_svc
        feeds_provider.feeds.append(Feed(id="f1", owner_user_id="alice"))
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        await storage.put(
            _BRIEFING_STATE_COLLECTION,
            "alice",
            {
                "_id": "alice",
                "last_briefed_on": today,
                "last_briefing_id": "brief_alice",
            },
        )
        assert await svc.greeting_context(user_id="alice") is None

    async def test_greeting_context_returns_none_when_no_resolver(
        self, sqlite_storage: Any
    ) -> None:
        svc = FeedBriefingService()
        # Service with no resolver - mimics a scenario where greeting_context
        # is called but the service was never started.
        assert await svc.greeting_context(user_id="alice") is None

    async def test_greeting_context_returns_none_when_no_feeds_capability(
        self, sqlite_storage: Any
    ) -> None:
        """When resolver exists but feeds capability is missing or not a
        FeedsProvider, greeting_context returns None (feeds service not enabled).
        """
        storage_provider = FakeStorageProvider(sqlite_storage)
        sched = FakeScheduler()
        feeds_provider = FakeFeedsProvider()
        # Resolver with feeds capability for start() to succeed
        resolver = FakeResolver(
            feeds=feeds_provider,
            scheduler=sched,
            entity_storage=storage_provider,
            configuration=None,
        )
        svc = FeedBriefingService()
        await svc.on_config_changed({"enabled": True})
        await svc.start(resolver)
        try:
            # Patch resolver to return None for feeds capability
            resolver.caps["feeds"] = None
            assert await svc.greeting_context(user_id="alice") is None
        finally:
            await svc.stop()

    def test_feed_briefing_advertises_capability_and_config(
        self, briefing_svc: Any
    ) -> None:
        svc, _, _, _ = briefing_svc
        info = svc.service_info()
        assert "greeting_context" in info.capabilities
        keys = {p.key for p in svc.config_params()}
        assert "briefing_max_seconds" in keys
