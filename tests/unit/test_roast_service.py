"""Tests for RoastService — random playful roasts of present people."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gilbert.core.services.roast import _DEFAULT_ROASTS, RoastService
from gilbert.core.services.scheduler import SchedulerService


class _FakeAISampling:
    """Concrete stub for AISamplingProvider — see test_greeting_service."""

    def __init__(self, content: str = "", raises: Exception | None = None) -> None:
        self._content = content
        self._raises = raises
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
        if self._raises is not None:
            raise self._raises
        return AIResponse(
            message=Message(role=MessageRole.ASSISTANT, content=self._content),
            model="test-model",
        )


class FakeScheduler(SchedulerService):
    """Captures add_job calls without running real scheduler logic."""

    def __init__(self) -> None:
        self.jobs: list[dict[str, Any]] = []

    def add_job(self, **kwargs: Any) -> MagicMock:  # type: ignore[override]
        self.jobs.append(kwargs)
        return MagicMock()


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
def roast_service() -> RoastService:
    return RoastService()


@pytest.fixture
def scheduler() -> FakeScheduler:
    return FakeScheduler()


@pytest.fixture
def resolver(scheduler: FakeScheduler) -> FakeResolver:
    r = FakeResolver()
    r.caps["scheduler"] = scheduler
    return r


class TestRoastServiceInfo:
    def test_service_info(self, roast_service: RoastService) -> None:
        info = roast_service.service_info()
        assert info.name == "roast"
        assert "roast" in info.capabilities
        assert "scheduler" in info.requires
        assert "ai_chat" in info.optional
        assert "presence" in info.optional
        assert "text_to_speech" in info.optional
        assert "speaker_control" in info.optional


class TestRoastStart:
    @pytest.mark.asyncio
    async def test_registers_hourly_job(
        self, roast_service: RoastService, resolver: FakeResolver, scheduler: FakeScheduler
    ) -> None:
        await roast_service.start(resolver)
        assert len(scheduler.jobs) == 1
        job = scheduler.jobs[0]
        assert job["name"] == "roast.hourly"
        assert job["system"] is True


class TestRoastRun:
    @pytest.mark.asyncio
    async def test_no_roast_when_dice_fails(
        self, roast_service: RoastService, resolver: FakeResolver
    ) -> None:
        """When random() > probability, no roast happens."""
        await roast_service.start(resolver)

        with patch("gilbert.core.services.roast.random") as mock_random:
            mock_random.random.return_value = 0.99  # Above 0.10 threshold
            await roast_service._run_roast()
            # Should not try to pick a person
            mock_random.choice.assert_not_called()

    @pytest.mark.asyncio
    async def test_roast_with_template_fallback(
        self, roast_service: RoastService, resolver: FakeResolver
    ) -> None:
        """When dice succeeds but no AI, uses template."""
        # Set up a fake presence service
        fake_presence = MagicMock()
        fake_person = MagicMock()
        fake_person.user_id = "alice"
        fake_person.display_name = "Alice"
        fake_presence.who_is_here = AsyncMock(return_value=[fake_person])
        resolver.caps["presence"] = fake_presence

        await roast_service.start(resolver)

        with (
            patch("gilbert.core.services.roast.random") as mock_random,
            patch("gilbert.core.services.roast.isinstance", side_effect=lambda obj, cls: True),
        ):
            mock_random.random.return_value = 0.01  # Below threshold
            mock_random.choice.side_effect = lambda x: x[0]  # Pick first item
            await roast_service._run_roast()

    @pytest.mark.asyncio
    async def test_no_roast_when_nobody_present(
        self, roast_service: RoastService, resolver: FakeResolver
    ) -> None:
        """When nobody is present, no roast happens."""
        fake_presence = MagicMock()
        fake_presence.who_is_here = AsyncMock(return_value=[])
        resolver.caps["presence"] = fake_presence

        await roast_service.start(resolver)

        with (
            patch("gilbert.core.services.roast.random") as mock_random,
            patch("gilbert.core.services.roast.isinstance", side_effect=lambda obj, cls: True),
        ):
            mock_random.random.return_value = 0.01  # Below threshold
            await roast_service._run_roast()

    @pytest.mark.asyncio
    async def test_no_presence_service(
        self, roast_service: RoastService, resolver: FakeResolver
    ) -> None:
        """When no presence service, picks no one."""
        await roast_service.start(resolver)

        with patch("gilbert.core.services.roast.random") as mock_random:
            mock_random.random.return_value = 0.01
            await roast_service._run_roast()


class TestGenerateRoast:
    @pytest.mark.asyncio
    async def test_template_roast(self, roast_service: RoastService) -> None:
        """Without AI, uses a template roast."""
        roast = await roast_service._generate_roast("Alice")
        assert "Alice" in roast

    @pytest.mark.asyncio
    async def test_ai_roast(self, roast_service: RoastService, resolver: FakeResolver) -> None:
        """With AI available, uses AI-generated roast."""
        fake_ai = _FakeAISampling(content="Hey Alice, nice work today!")
        resolver.caps["ai_chat"] = fake_ai

        await roast_service.start(resolver)
        roast = await roast_service._generate_roast("Alice")
        assert roast == "Hey Alice, nice work today!"
        # Must force zero tools regardless of profile.
        assert fake_ai.calls[0]["tools_override"] == []

    @pytest.mark.asyncio
    async def test_ai_failure_falls_back_to_template(
        self, roast_service: RoastService, resolver: FakeResolver
    ) -> None:
        """When AI fails, falls back to template."""
        fake_ai = _FakeAISampling(raises=Exception("API error"))
        resolver.caps["ai_chat"] = fake_ai

        await roast_service.start(resolver)
        roast = await roast_service._generate_roast("Bob")
        assert "Bob" in roast


class TestDefaultRoasts:
    def test_all_templates_have_name_placeholder(self) -> None:
        """Every default roast template should have a {name} placeholder."""
        for roast in _DEFAULT_ROASTS:
            assert "{name}" in roast

    def test_templates_format_correctly(self) -> None:
        """All templates should format without error."""
        for roast in _DEFAULT_ROASTS:
            formatted = roast.format(name="TestUser")
            assert "TestUser" in formatted


class TestNameResolution:
    """Verify roast service resolves user_id to display name, never roasts raw IDs."""

    @pytest.mark.asyncio
    async def test_resolves_user_id_to_display_name(
        self,
        roast_service: RoastService,
        resolver: FakeResolver,
    ) -> None:
        """Should resolve internal user_id to the user's display name."""
        from gilbert.interfaces.presence import PresenceState, UserPresence

        fake_presence = MagicMock()
        fake_presence.who_is_here = AsyncMock(
            return_value=[
                UserPresence(user_id="usr_abc123", state=PresenceState.PRESENT),
            ]
        )
        resolver.caps["presence"] = fake_presence

        # Provide a user service that resolves the ID
        fake_user_backend = MagicMock()
        fake_user_backend.get_user = AsyncMock(
            return_value={
                "_id": "usr_abc123",
                "display_name": "Brian Dilley",
                "email": "brian@test.com",
            }
        )
        fake_user_svc = MagicMock()
        fake_user_svc.backend = fake_user_backend
        resolver.caps["users"] = fake_user_svc

        await roast_service.start(resolver)

        with patch("gilbert.core.services.roast.isinstance", side_effect=lambda obj, cls: True):
            name = await roast_service._pick_random_person()
        assert name == "Brian"  # First name only

    @pytest.mark.asyncio
    async def test_uuid_user_id_without_user_service(
        self,
        roast_service: RoastService,
        resolver: FakeResolver,
    ) -> None:
        """Without a user service, UUID-style IDs are returned as-is (not ideal but safe)."""
        from gilbert.interfaces.presence import PresenceState, UserPresence

        fake_presence = MagicMock()
        fake_presence.who_is_here = AsyncMock(
            return_value=[
                UserPresence(user_id="usr_569171d4c248", state=PresenceState.PRESENT),
            ]
        )
        resolver.caps["presence"] = fake_presence
        # No users service

        await roast_service.start(resolver)

        with patch("gilbert.core.services.roast.isinstance", side_effect=lambda obj, cls: True):
            name = await roast_service._pick_random_person()
        assert name == "usr_569171d4c248"

    @pytest.mark.asyncio
    async def test_email_user_id_fallback(
        self,
        roast_service: RoastService,
        resolver: FakeResolver,
    ) -> None:
        """Email-style user_ids should be parsed into a readable name."""
        from gilbert.interfaces.presence import PresenceState, UserPresence

        fake_presence = MagicMock()
        fake_presence.who_is_here = AsyncMock(
            return_value=[
                UserPresence(user_id="john.doe@company.com", state=PresenceState.PRESENT),
            ]
        )
        resolver.caps["presence"] = fake_presence
        # No users service — fallback parsing

        await roast_service.start(resolver)

        with patch("gilbert.core.services.roast.isinstance", side_effect=lambda obj, cls: True):
            name = await roast_service._pick_random_person()
        assert name == "John Doe"

    @pytest.mark.asyncio
    async def test_user_not_found_uses_fallback(
        self,
        roast_service: RoastService,
        resolver: FakeResolver,
    ) -> None:
        """If user service can't find the user, falls back to ID parsing."""
        from gilbert.interfaces.presence import PresenceState, UserPresence

        fake_presence = MagicMock()
        fake_presence.who_is_here = AsyncMock(
            return_value=[
                UserPresence(user_id="brian.dilley@test.com", state=PresenceState.PRESENT),
            ]
        )
        resolver.caps["presence"] = fake_presence

        fake_user_backend = MagicMock()
        fake_user_backend.get_user = AsyncMock(return_value=None)
        fake_user_svc = MagicMock()
        fake_user_svc.backend = fake_user_backend
        resolver.caps["users"] = fake_user_svc

        await roast_service.start(resolver)

        with patch("gilbert.core.services.roast.isinstance", side_effect=lambda obj, cls: True):
            name = await roast_service._pick_random_person()
        assert name == "Brian Dilley"


class TestAnnounce:
    @pytest.mark.asyncio
    async def test_announce_with_speakers(
        self, roast_service: RoastService, resolver: FakeResolver
    ) -> None:
        """Announces via speaker service when available."""
        from gilbert.interfaces.speaker import SpeakerProvider

        fake_speaker = MagicMock(spec=SpeakerProvider)
        fake_speaker.announce = AsyncMock()
        resolver.caps["speaker_control"] = fake_speaker

        await roast_service.start(resolver)
        await roast_service._announce("Test roast")

        fake_speaker.announce.assert_awaited_once_with(
            "Test roast",
            speaker_names=None,
            context="Playful roast / good-natured ribbing — deliver with bite",
        )

    @pytest.mark.asyncio
    async def test_announce_without_speakers(
        self, roast_service: RoastService, resolver: FakeResolver
    ) -> None:
        """Gracefully handles missing speaker service."""
        await roast_service.start(resolver)
        # Should not raise
        await roast_service._announce("Test roast")
