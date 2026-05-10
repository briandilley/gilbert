"""Tests for GreetingService — presence-driven morning greetings."""

from typing import Any
from unittest.mock import AsyncMock

import pytest

from gilbert.core.services.greeting import GreetingService
from gilbert.interfaces.events import Event


class FakeStorage:
    """In-memory storage backend for testing."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict[str, Any]]] = {}

    async def get(self, collection: str, key: str) -> dict[str, Any] | None:
        return self._data.get(collection, {}).get(key)

    async def put(self, collection: str, key: str, data: dict[str, Any]) -> None:
        self._data.setdefault(collection, {})[key] = data


class FakeStorageService:
    def __init__(self) -> None:
        self.backend = FakeStorage()
        self.raw_backend = self.backend

    def create_namespaced(self, namespace: str) -> Any:
        return self.backend

    def service_info(self) -> Any:
        from gilbert.interfaces.service import ServiceInfo

        return ServiceInfo(name="storage", capabilities=frozenset({"entity_storage"}))


class FakeEventBus:
    def __init__(self) -> None:
        self.handlers: dict[str, list[Any]] = {}
        self.published: list[Event] = []

    def subscribe(self, event_type: str, handler: Any) -> Any:
        self.handlers.setdefault(event_type, []).append(handler)
        return lambda: self.handlers[event_type].remove(handler)

    async def publish(self, event: Event) -> None:
        self.published.append(event)


class _FakeAISampling:
    """Minimal AISamplingProvider stub for greeting/roast tests.

    ``AsyncMock`` doesn't satisfy ``isinstance(x, AISamplingProvider)``
    under Python 3.12+'s stricter runtime_checkable check, so tests use
    this concrete class instead.
    """

    def __init__(self, content: str = "") -> None:
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
    ) -> Any:
        from gilbert.interfaces.ai import AIResponse, Message, MessageRole

        self.calls.append(
            {
                "messages": messages,
                "system_prompt": system_prompt,
                "profile_name": profile_name,
                "max_tokens": max_tokens,
                "tools_override": tools_override,
            },
        )
        return AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content=self._content),
            model="test-model",
        )


class FakeEventBusSvc:
    def __init__(self) -> None:
        self.bus = FakeEventBus()

    def service_info(self) -> Any:
        from gilbert.interfaces.service import ServiceInfo

        return ServiceInfo(name="event_bus", capabilities=frozenset({"event_bus"}))


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


@pytest.fixture
def greeting_service() -> GreetingService:
    return GreetingService()


@pytest.fixture
def resolver() -> FakeResolver:
    r = FakeResolver()
    r.caps["event_bus"] = FakeEventBusSvc()
    storage = FakeStorageService()
    r.caps["entity_storage"] = storage
    # Patch isinstance check
    return r


class TestGreetingService:
    def test_service_info(self, greeting_service: GreetingService) -> None:
        info = greeting_service.service_info()
        assert info.name == "greeting"
        assert "greeting" in info.capabilities
        assert "event_bus" in info.requires
        assert "entity_storage" in info.requires

    @pytest.mark.asyncio
    async def test_subscribes_to_presence_arrived(
        self, greeting_service: GreetingService, resolver: FakeResolver
    ) -> None:
        await greeting_service.start(resolver)
        bus = resolver.caps["event_bus"].bus
        assert "presence.arrived" in bus.handlers
        assert len(bus.handlers["presence.arrived"]) == 1

    @pytest.mark.asyncio
    async def test_unsubscribes_on_stop(
        self, greeting_service: GreetingService, resolver: FakeResolver
    ) -> None:
        await greeting_service.start(resolver)
        bus = resolver.caps["event_bus"].bus
        assert len(bus.handlers["presence.arrived"]) == 1
        await greeting_service.stop()
        assert len(bus.handlers["presence.arrived"]) == 0

    async def test_get_display_name_from_email(self, greeting_service: GreetingService) -> None:
        assert (
            await greeting_service._get_display_name("brian.dilley@example.com") == "Brian Dilley"
        )

    async def test_get_display_name_from_plain(self, greeting_service: GreetingService) -> None:
        assert await greeting_service._get_display_name("Brian") == "Brian"

    def test_in_greeting_window(self, greeting_service: GreetingService) -> None:
        greeting_service._start_hour = 6
        greeting_service._cutoff_hour = 14
        greeting_service._timezone = "UTC"
        # This is time-dependent — just verify the method doesn't crash
        result = greeting_service._in_greeting_window()
        assert isinstance(result, bool)

    @pytest.mark.asyncio
    async def test_dedup_prevents_double_greeting(
        self, greeting_service: GreetingService, resolver: FakeResolver
    ) -> None:
        await greeting_service.start(resolver)

        # Mark user as greeted
        await greeting_service._mark_greeted("brian")

        # Should be greeted
        assert await greeting_service._has_been_greeted_today("brian") is True

        # Unknown user should not be greeted
        assert await greeting_service._has_been_greeted_today("unknown") is False

    @pytest.mark.asyncio
    async def test_generate_greeting_fallback(
        self, greeting_service: GreetingService, resolver: FakeResolver
    ) -> None:
        """Without AI service, falls back to simple greeting."""
        await greeting_service.start(resolver)
        greeting = await greeting_service._generate_greeting("Brian")
        assert "Brian" in greeting
        assert "Good morning" in greeting

    @pytest.mark.asyncio
    async def test_generate_greeting_uses_ai_chat_capability(
        self,
        greeting_service: GreetingService,
        resolver: FakeResolver,
    ) -> None:
        """AI greeting looks up 'ai_chat' capability, not 'ai'."""
        from gilbert.interfaces.ai import AISamplingProvider

        await greeting_service.start(resolver)

        fake_ai = _FakeAISampling(content="Hey Brian, welcome!")
        assert isinstance(fake_ai, AISamplingProvider)
        resolver.caps["ai_chat"] = fake_ai

        greeting = await greeting_service._generate_greeting("Brian")
        assert greeting == "Hey Brian, welcome!"
        assert len(fake_ai.calls) == 1
        # Must force zero tools regardless of profile — this is the
        # bug-fix-regression guard from the Sonos announce-loop incident.
        assert fake_ai.calls[0]["tools_override"] == []

    @pytest.mark.asyncio
    async def test_generate_greeting_wrong_capability_falls_back(
        self,
        greeting_service: GreetingService,
        resolver: FakeResolver,
    ) -> None:
        """If AI is registered under 'ai' instead of 'ai_chat', falls back."""
        await greeting_service.start(resolver)

        # Register under wrong name — should not be found
        fake_ai = _FakeAISampling(content="nope")
        resolver.caps["ai"] = fake_ai

        greeting = await greeting_service._generate_greeting("Brian")
        assert "Good morning" in greeting  # Fallback, not AI-generated
        assert fake_ai.calls == []

    @pytest.mark.asyncio
    async def test_startup_greets_already_present(
        self,
        greeting_service: GreetingService,
        resolver: FakeResolver,
    ) -> None:
        """Startup check greets people already present."""
        await greeting_service.start(resolver)
        greeting_service._start_hour = 0
        greeting_service._cutoff_hour = 24

        # Mock presence service with people present
        from gilbert.interfaces.presence import PresenceState, UserPresence

        mock_presence = AsyncMock()
        mock_presence.who_is_here = AsyncMock(
            return_value=[
                UserPresence(user_id="usr_1", state=PresenceState.PRESENT),
                UserPresence(user_id="usr_2", state=PresenceState.NEARBY),
            ]
        )
        resolver.caps["presence"] = mock_presence

        # Mock speaker to capture announcements
        mock_speaker = AsyncMock()
        resolver.caps["speaker_control"] = mock_speaker

        await greeting_service._greet_already_present()

        # Both users should have been greeted
        assert await greeting_service._has_been_greeted_today("usr_1")
        assert await greeting_service._has_been_greeted_today("usr_2")

    @pytest.mark.asyncio
    async def test_startup_skips_outside_window(
        self,
        greeting_service: GreetingService,
        resolver: FakeResolver,
    ) -> None:
        """Startup check does nothing outside greeting window."""
        await greeting_service.start(resolver)
        greeting_service._start_hour = 0
        greeting_service._cutoff_hour = 0  # Always outside window

        mock_presence = AsyncMock()
        resolver.caps["presence"] = mock_presence

        await greeting_service._greet_already_present()

        mock_presence.who_is_here.assert_not_called()


# ── FeedsProvider integration ────────────────────────────────────────


class _FakeFeedsForGreeting:
    """Bare-minimum FeedsProvider stub for greeting splice tests."""

    def __init__(self, spoken: str = "news today") -> None:
        self.calls: list[Any] = []
        self.spoken = spoken
        self.fail = False

    async def subscribe(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def unsubscribe(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError

    async def list_accessible_feeds(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    async def get_feed(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def search_items(self, **kwargs: Any) -> list[Any]:
        return []

    async def get_top_items(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    async def mark_read(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def build_briefing(self, user_ctx: Any, **kwargs: Any) -> Any:
        from datetime import UTC, datetime

        from gilbert.interfaces.feeds import BriefingResult

        self.calls.append({"user_id": user_ctx.user_id, **kwargs})
        if self.fail:
            raise RuntimeError("forced failure")
        return BriefingResult(
            spoken=self.spoken,
            headlines=[],
            item_ids=["x"],
            since=datetime.now(UTC),
            briefing_id=f"brief_{user_ctx.user_id}",
        )


class TestGreetingBriefingSplice:
    """Per spec §16 — when ``include_briefing=True`` and the user
    hasn't been briefed today, the greeting splices in the briefing
    text before announcing."""

    @pytest.mark.asyncio
    async def test_greeting_includes_briefing_when_flag_on(
        self, greeting_service: GreetingService, resolver: FakeResolver
    ) -> None:
        from gilbert.interfaces.feeds import FeedsProvider

        feeds = _FakeFeedsForGreeting(spoken="THE BRIEFING")
        assert isinstance(feeds, FeedsProvider)
        resolver.caps["feeds"] = feeds
        await greeting_service.start(resolver)
        greeting_service._include_briefing = True
        text = await greeting_service._maybe_briefing_text("alice")
        assert text == "THE BRIEFING"
        assert feeds.calls and feeds.calls[0]["user_id"] == "alice"

    @pytest.mark.asyncio
    async def test_greeting_skips_briefing_when_flag_off(
        self, greeting_service: GreetingService, resolver: FakeResolver
    ) -> None:
        feeds = _FakeFeedsForGreeting()
        resolver.caps["feeds"] = feeds
        await greeting_service.start(resolver)
        greeting_service._include_briefing = False
        text = await greeting_service._maybe_briefing_text("alice")
        assert text == ""
        assert feeds.calls == []

    @pytest.mark.asyncio
    async def test_greeting_skips_briefing_when_feeds_capability_absent(
        self, greeting_service: GreetingService, resolver: FakeResolver
    ) -> None:
        await greeting_service.start(resolver)
        greeting_service._include_briefing = True
        # No feeds capability registered.
        text = await greeting_service._maybe_briefing_text("alice")
        assert text == ""

    @pytest.mark.asyncio
    async def test_greeting_skips_briefing_when_already_briefed_today(
        self, greeting_service: GreetingService, resolver: FakeResolver
    ) -> None:
        from datetime import UTC, datetime

        feeds = _FakeFeedsForGreeting()
        resolver.caps["feeds"] = feeds
        await greeting_service.start(resolver)
        greeting_service._include_briefing = True
        # Pre-seed today's briefing on the storage backend used by greeting.
        storage = resolver.caps["entity_storage"].backend
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        await storage.put(
            "feed_briefing_state",
            "alice",
            {"_id": "alice", "last_briefed_on": today},
        )
        # _maybe_briefing_text uses self._today_str() which uses
        # self._timezone — make sure it matches.
        greeting_service._timezone = "UTC"
        text = await greeting_service._maybe_briefing_text("alice")
        assert text == ""
        assert feeds.calls == []  # never called build_briefing

    @pytest.mark.asyncio
    async def test_greeting_briefing_failure_degrades_silently(
        self, greeting_service: GreetingService, resolver: FakeResolver
    ) -> None:
        feeds = _FakeFeedsForGreeting()
        feeds.fail = True
        resolver.caps["feeds"] = feeds
        await greeting_service.start(resolver)
        greeting_service._include_briefing = True
        text = await greeting_service._maybe_briefing_text("alice")
        assert text == ""

    def test_greeting_does_not_import_briefing_provider(self) -> None:
        # Regression guard for Round-2 architect spec change. Verify
        # the symbol does not exist.
        from gilbert.interfaces import feeds as feeds_mod

        assert not hasattr(feeds_mod, "BriefingProvider")


# ── Camera-event announce tests (feature 06) ────────────────────────


class TestGreetingCameraEvents:
    """Cover the camera.event.detected announce path + dedup + mute."""

    @pytest.mark.asyncio
    async def test_announces_on_camera_package_event(
        self,
        greeting_service: GreetingService,
        resolver: FakeResolver,
    ) -> None:
        await greeting_service.start(resolver)
        # Default: announce_camera_labels = ["package"]
        called: list[str] = []

        async def fake_announce(text: str) -> None:
            called.append(text)

        greeting_service._announce = fake_announce  # type: ignore[method-assign]
        await greeting_service._on_camera_event(
            Event(
                event_type="camera.event.detected",
                data={"label": "package", "camera": "porch"},
            )
        )
        assert len(called) == 1

    @pytest.mark.asyncio
    async def test_does_not_announce_label_not_in_list(
        self,
        greeting_service: GreetingService,
        resolver: FakeResolver,
    ) -> None:
        await greeting_service.start(resolver)
        called: list[str] = []

        async def fake_announce(text: str) -> None:
            called.append(text)

        greeting_service._announce = fake_announce  # type: ignore[method-assign]
        # Default doesn't include person.
        await greeting_service._on_camera_event(
            Event(
                event_type="camera.event.detected",
                data={"label": "person", "camera": "porch"},
            )
        )
        assert called == []

    @pytest.mark.asyncio
    async def test_dedups_repeat_camera_event(
        self,
        greeting_service: GreetingService,
        resolver: FakeResolver,
    ) -> None:
        await greeting_service.start(resolver)
        called: list[str] = []

        async def fake_announce(text: str) -> None:
            called.append(text)

        greeting_service._announce = fake_announce  # type: ignore[method-assign]
        for _ in range(3):
            await greeting_service._on_camera_event(
                Event(
                    event_type="camera.event.detected",
                    data={"label": "package", "camera": "porch"},
                )
            )
        # Default dedup key for package = ["label"] — single announce.
        assert len(called) == 1

    @pytest.mark.asyncio
    async def test_announce_dedups_across_camera_zone_group(
        self,
        greeting_service: GreetingService,
        resolver: FakeResolver,
    ) -> None:
        await greeting_service.start(resolver)
        called: list[str] = []

        async def fake_announce(text: str) -> None:
            called.append(text)

        greeting_service._announce = fake_announce  # type: ignore[method-assign]
        # Configure zone groups + add person to announce labels with
        # ["label", "zone_group"] dedup.
        await greeting_service.on_config_changed(
            {
                "announce_camera_labels": ["person"],
                "camera_zone_groups": {
                    "front_entry": ["driveway", "front_porch", "front_door"]
                },
                "camera_announce_dedup_keys": {
                    "person": ["label", "zone_group"]
                },
            }
        )
        for cam in ("driveway", "front_porch", "front_door"):
            await greeting_service._on_camera_event(
                Event(
                    event_type="camera.event.detected",
                    data={"label": "person", "camera": cam},
                )
            )
        assert len(called) == 1

    @pytest.mark.asyncio
    async def test_mute_camera_alerts_returns_preview_when_not_confirmed(
        self,
        greeting_service: GreetingService,
        resolver: FakeResolver,
    ) -> None:
        await greeting_service.start(resolver)
        out = await greeting_service.execute_tool(
            "mute_camera_alerts",
            {
                "camera": "side_gate",
                "label": "person",
                "until": "8h",
            },
        )
        from gilbert.interfaces.ui import ToolOutput

        assert isinstance(out, ToolOutput)
        assert out.ui_blocks
        block = out.ui_blocks[0]
        # Confirm/Cancel buttons present.
        button_element = next(
            e for e in block.elements if e.type == "buttons"
        )
        values = {opt.value for opt in button_element.options}
        assert {"confirm", "cancel"} == values

    @pytest.mark.asyncio
    async def test_mute_camera_alerts_suppresses_announce(
        self,
        greeting_service: GreetingService,
        resolver: FakeResolver,
    ) -> None:
        await greeting_service.start(resolver)
        # Mute the (porch, package) combo.
        await greeting_service.execute_tool(
            "mute_camera_alerts",
            {
                "camera": "porch",
                "label": "package",
                "until": "8h",
                "confirm": True,
            },
        )
        called: list[str] = []

        async def fake_announce(text: str) -> None:
            called.append(text)

        greeting_service._announce = fake_announce  # type: ignore[method-assign]
        await greeting_service._on_camera_event(
            Event(
                event_type="camera.event.detected",
                data={"label": "package", "camera": "porch"},
            )
        )
        assert called == []
