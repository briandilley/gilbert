"""Tests for UniFi presence backend — composite aggregation, subsystems, name parsing."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gilbert.config import GilbertConfig
from gilbert.integrations.unifi.access import BadgeEvent, UniFiAccess, _classify_direction
from gilbert.integrations.unifi.network import (
    UniFiNetwork,
    WifiClient,
    extract_person_from_device_name,
    extract_person_from_hostname,
)
from gilbert.integrations.unifi.name_resolver import NameResolver, _compute_similarity, _tokenize
from gilbert.integrations.unifi.presence import UniFiPresenceBackend, _epoch_ms_to_iso
from gilbert.integrations.unifi.protect import UniFiProtect, FaceDetection
from gilbert.interfaces.presence import PresenceState


# =============================================================================
# Name extraction tests
# =============================================================================


class TestDeviceNameExtraction:
    def test_possessive_iphone(self) -> None:
        assert extract_person_from_device_name("Brian's iPhone") == "Brian"

    def test_possessive_phone(self) -> None:
        assert extract_person_from_device_name("Brian Dilley's Phone") == "Brian"

    def test_firstname_device(self) -> None:
        assert extract_person_from_device_name("Chris Pixel 8") == "Chris"

    def test_firstname_iphone(self) -> None:
        assert extract_person_from_device_name("Matt iPhone 15") == "Matt"

    def test_device_only_name(self) -> None:
        assert extract_person_from_device_name("iPhone") == ""

    def test_lowercase_rejected(self) -> None:
        assert extract_person_from_device_name("unknown device") == ""

    def test_empty(self) -> None:
        assert extract_person_from_device_name("") == ""


class TestHostnameExtraction:
    def test_simple_iphone(self) -> None:
        assert extract_person_from_hostname("Greggs-iPhone") == "Gregg"

    def test_camelcase_split(self) -> None:
        assert extract_person_from_hostname("BrianDilley-iPhone") == "Brian"

    def test_compound_name(self) -> None:
        assert extract_person_from_hostname("Gregg-Snowdens-iPhone") == "Gregg"

    def test_device_only(self) -> None:
        assert extract_person_from_hostname("iPhone") == ""

    def test_empty(self) -> None:
        assert extract_person_from_hostname("") == ""

    def test_lowercase_rejected(self) -> None:
        assert extract_person_from_hostname("myphone-wifi") == ""


# =============================================================================
# Badge direction classification tests
# =============================================================================


class TestBadgeDirection:
    def test_unlock_is_in(self) -> None:
        assert _classify_direction("access.door.unlock") == "in"

    def test_entry_is_in(self) -> None:
        assert _classify_direction("entry_granted") == "in"

    def test_exit_is_out(self) -> None:
        assert _classify_direction("access.door.exit") == "out"

    def test_lock_is_out(self) -> None:
        assert _classify_direction("door.lock") == "out"

    def test_unknown_defaults_to_in(self) -> None:
        assert _classify_direction("something_else") == "in"


# =============================================================================
# Composite presence aggregation tests
# =============================================================================


@pytest.fixture
def backend() -> UniFiPresenceBackend:
    """Create a backend with mocked subsystems and known test users."""
    b = UniFiPresenceBackend()
    b._network = AsyncMock(spec=UniFiNetwork)
    b._protect = AsyncMock(spec=UniFiProtect)
    b._access = AsyncMock(spec=UniFiAccess)
    # Default: empty results
    b._network.get_people_on_network = AsyncMock(return_value={})
    b._protect.get_face_detections = AsyncMock(return_value=[])
    b._access.get_badge_events = AsyncMock(return_value=[])
    # Pre-populate name resolver with test users so signals resolve
    b._name_resolver._users = [
        {"_id": "brian", "display_name": "Brian"},
        {"_id": "dale", "display_name": "Dale"},
        {"_id": "matt", "display_name": "Matt"},
    ]
    return b


def _badge(name: str, direction: str, ts: int = 1700000000000) -> BadgeEvent:
    return BadgeEvent(
        event_id="e1", person_name=name, direction=direction,
        door_name="Front Door", timestamp=ts,
    )


def _face(name: str, ts: int = 1700000000000) -> FaceDetection:
    return FaceDetection(
        person_name=name, camera_name="Lobby Cam", timestamp=ts, confidence=90,
    )


def _wifi(name: str, last_seen: str = "1700000000") -> dict[str, list[WifiClient]]:
    return {name: [WifiClient(
        mac="aa:bb:cc:dd:ee:ff", hostname="device", device_name=f"{name}'s Phone",
        person=name, rssi=-50, ap_name="AP1", last_seen=last_seen, is_wired=False,
    )]}


class TestAggregation:
    async def test_badge_in_yields_present(self, backend: UniFiPresenceBackend) -> None:
        backend._access.get_badge_events = AsyncMock(return_value=[
            _badge("Brian", "in"),
        ])
        result = await backend.get_all_presence()
        assert len(result) == 1
        assert result[0].user_id == "brian"
        assert result[0].state == PresenceState.PRESENT
        assert result[0].source == "unifi:access"

    async def test_badge_out_yields_away(self, backend: UniFiPresenceBackend) -> None:
        backend._access.get_badge_events = AsyncMock(return_value=[
            _badge("Brian", "out"),
        ])
        result = await backend.get_all_presence()
        assert len(result) == 1
        assert result[0].state == PresenceState.AWAY
        assert result[0].source == "unifi:access"

    async def test_face_yields_present(self, backend: UniFiPresenceBackend) -> None:
        backend._protect.get_face_detections = AsyncMock(return_value=[
            _face("Dale"),
        ])
        result = await backend.get_all_presence()
        assert len(result) == 1
        assert result[0].user_id == "dale"
        assert result[0].state == PresenceState.PRESENT
        assert result[0].source == "unifi:protect"

    async def test_wifi_phone_yields_present(self, backend: UniFiPresenceBackend) -> None:
        """A phone on WiFi means the person is physically present."""
        backend._network.get_people_on_network = AsyncMock(return_value=_wifi("Matt"))
        result = await backend.get_all_presence()
        assert len(result) == 1
        assert result[0].user_id == "matt"
        assert result[0].state == PresenceState.PRESENT
        assert result[0].source == "unifi:network"

    async def test_no_signals_returns_empty(self, backend: UniFiPresenceBackend) -> None:
        result = await backend.get_all_presence()
        assert result == []

    async def test_badge_out_overrides_face(self, backend: UniFiPresenceBackend) -> None:
        backend._access.get_badge_events = AsyncMock(return_value=[
            _badge("Brian", "out", ts=1700000002000),
        ])
        backend._protect.get_face_detections = AsyncMock(return_value=[
            _face("Brian", ts=1700000001000),
        ])
        result = await backend.get_all_presence()
        brian = [r for r in result if r.user_id == "brian"]
        assert len(brian) == 1
        assert brian[0].state == PresenceState.AWAY

    async def test_badge_in_overrides_wifi(self, backend: UniFiPresenceBackend) -> None:
        backend._access.get_badge_events = AsyncMock(return_value=[
            _badge("Brian", "in"),
        ])
        backend._network.get_people_on_network = AsyncMock(return_value=_wifi("Brian"))
        result = await backend.get_all_presence()
        brian = [r for r in result if r.user_id == "brian"]
        assert len(brian) == 1
        assert brian[0].state == PresenceState.PRESENT

    async def test_face_overrides_wifi(self, backend: UniFiPresenceBackend) -> None:
        backend._protect.get_face_detections = AsyncMock(return_value=[
            _face("Dale"),
        ])
        backend._network.get_people_on_network = AsyncMock(return_value=_wifi("Dale"))
        result = await backend.get_all_presence()
        dale = [r for r in result if r.user_id == "dale"]
        assert len(dale) == 1
        assert dale[0].state == PresenceState.PRESENT
        assert dale[0].source == "unifi:protect"

    async def test_multiple_people(self, backend: UniFiPresenceBackend) -> None:
        backend._access.get_badge_events = AsyncMock(return_value=[
            _badge("Brian", "in"),
        ])
        backend._protect.get_face_detections = AsyncMock(return_value=[
            _face("Dale"),
        ])
        backend._network.get_people_on_network = AsyncMock(return_value=_wifi("Matt"))
        result = await backend.get_all_presence()
        names = {r.user_id for r in result}
        assert "brian" in names
        assert "dale" in names
        assert "matt" in names


class TestGracefulDegradation:
    async def test_access_down_others_work(self, backend: UniFiPresenceBackend) -> None:
        from gilbert.integrations.unifi.client import UniFiConnectionError

        backend._access.get_badge_events = AsyncMock(side_effect=UniFiConnectionError("down"))
        backend._network.get_people_on_network = AsyncMock(return_value=_wifi("Matt"))
        result = await backend.get_all_presence()
        assert len(result) == 1
        assert result[0].user_id == "matt"

    async def test_all_subsystems_down(self, backend: UniFiPresenceBackend) -> None:
        from gilbert.integrations.unifi.client import UniFiConnectionError

        backend._access.get_badge_events = AsyncMock(side_effect=UniFiConnectionError("down"))
        backend._protect.get_face_detections = AsyncMock(side_effect=UniFiConnectionError("down"))
        backend._network.get_people_on_network = AsyncMock(side_effect=UniFiConnectionError("down"))
        result = await backend.get_all_presence()
        assert result == []

    async def test_no_subsystems_configured(self) -> None:
        b = UniFiPresenceBackend()
        # No subsystems initialized
        result = await b.get_all_presence()
        assert result == []


class TestGetPresence:
    async def test_known_user(self, backend: UniFiPresenceBackend) -> None:
        backend._access.get_badge_events = AsyncMock(return_value=[
            _badge("Brian", "in"),
        ])
        result = await backend.get_presence("brian")
        assert result.state == PresenceState.PRESENT

    async def test_unknown_user(self, backend: UniFiPresenceBackend) -> None:
        result = await backend.get_presence("nobody")
        assert result.state == PresenceState.UNKNOWN


# =============================================================================
# Config parsing tests
# =============================================================================


class TestConfig:
    def test_presence_defaults(self) -> None:
        config = GilbertConfig.model_validate({})
        assert config.presence.enabled is False
        assert config.presence.backend == "unifi"
        assert config.presence.poll_interval_seconds == 30

    def test_presence_full(self) -> None:
        raw = {
            "presence": {
                "enabled": True,
                "backend": "unifi",
                "poll_interval_seconds": 15,
                "unifi_network": {
                    "host": "https://192.168.1.1",
                    "credential": "unifi-net",
                },
                "unifi_protect": {
                    "host": "https://192.168.1.2",
                    "credential": "unifi-prot",
                    "verify_ssl": True,
                },
                "device_person_map": {"aa:bb:cc:dd:ee:ff": "brian"},
                "zone_aliases": {"shop": ["warehouse", "bay"]},
                "face_lookback_minutes": 15,
                "badge_lookback_hours": 12,
            }
        }
        config = GilbertConfig.model_validate(raw)
        assert config.presence.enabled is True
        assert config.presence.unifi_network.host == "https://192.168.1.1"
        assert config.presence.unifi_protect.verify_ssl is True
        assert config.presence.device_person_map["aa:bb:cc:dd:ee:ff"] == "brian"
        assert config.presence.zone_aliases["shop"] == ["warehouse", "bay"]
        assert config.presence.face_lookback_minutes == 15


# =============================================================================
# Utility tests
# =============================================================================


# =============================================================================
# Name resolver tests
# =============================================================================


class TestTokenize:
    def test_strips_device_words(self) -> None:
        tokens = _tokenize("Brian's iPhone")
        assert "iphone" not in tokens
        assert "brian" in tokens

    def test_handles_hostname(self) -> None:
        tokens = _tokenize("Greggs-iPhone")
        assert "iphone" not in tokens
        assert "greggs" in tokens

    def test_empty(self) -> None:
        assert _tokenize("") == []

    def test_device_only(self) -> None:
        assert _tokenize("iPhone") == []


class TestSimilarity:
    def test_exact_match(self) -> None:
        tokens = _tokenize("Brian Dilley")
        score = _compute_similarity(tokens, "Brian Dilley")
        assert score == 1.0

    def test_first_name_only(self) -> None:
        tokens = _tokenize("Brian")
        score = _compute_similarity(tokens, "Brian Dilley")
        assert score >= 0.5

    def test_no_match(self) -> None:
        tokens = _tokenize("Xyz")
        score = _compute_similarity(tokens, "Brian Dilley")
        assert score == 0.0

    def test_device_name_match(self) -> None:
        """'Brian's iPhone' should match 'Brian Dilley' after stripping device words."""
        tokens = _tokenize("Brian's iPhone")
        score = _compute_similarity(tokens, "Brian Dilley")
        assert score >= 0.5


class TestNameResolver:
    def test_resolve_exact(self) -> None:
        resolver = NameResolver()
        resolver._users = [
            {"_id": "brian", "display_name": "Brian Dilley"},
            {"_id": "matt", "display_name": "Matt Smith"},
        ]
        result = resolver.resolve("Brian Dilley")
        assert result is not None
        assert result.user_id == "brian"
        assert result.confidence == 1.0

    def test_resolve_first_name(self) -> None:
        resolver = NameResolver()
        resolver._users = [
            {"_id": "brian", "display_name": "Brian Dilley"},
        ]
        result = resolver.resolve("Brian")
        assert result is not None
        assert result.user_id == "brian"
        assert result.confidence >= 0.5

    def test_resolve_device_name(self) -> None:
        resolver = NameResolver()
        resolver._users = [
            {"_id": "brian", "display_name": "Brian Dilley"},
        ]
        result = resolver.resolve("Brian's iPhone")
        assert result is not None
        assert result.user_id == "brian"

    def test_resolve_no_match(self) -> None:
        resolver = NameResolver()
        resolver._users = [
            {"_id": "brian", "display_name": "Brian Dilley"},
        ]
        result = resolver.resolve("Xyz Unknown")
        assert result is None

    def test_resolve_empty(self) -> None:
        resolver = NameResolver()
        assert resolver.resolve("") is None

    def test_resolve_no_users(self) -> None:
        resolver = NameResolver()
        assert resolver.resolve("Brian") is None

    def test_resolve_by_email_local_part(self) -> None:
        resolver = NameResolver()
        resolver._users = [
            {"_id": "usr_abc", "display_name": "Brian Dilley", "email": "brian.dilley@example.com"},
        ]
        result = resolver.resolve("brian.dilley")
        assert result is not None
        assert result.user_id == "usr_abc"

    def test_resolve_by_email_full(self) -> None:
        resolver = NameResolver()
        resolver._users = [
            {"_id": "usr_abc", "display_name": "Brian Dilley", "email": "brian@example.com"},
        ]
        result = resolver.resolve("brian@example.com")
        assert result is not None
        assert result.user_id == "usr_abc"

    def test_resolve_by_email_first_name(self) -> None:
        """Email local part 'brian.dilley' should match raw name 'Brian'."""
        resolver = NameResolver()
        resolver._users = [
            {"_id": "usr_abc", "display_name": "", "email": "brian.dilley@example.com"},
        ]
        result = resolver.resolve("Brian")
        assert result is not None
        assert result.user_id == "usr_abc"

    def test_resolve_prefers_display_name_over_email(self) -> None:
        resolver = NameResolver()
        resolver._users = [
            {"_id": "usr_abc", "display_name": "Brian Dilley", "email": "bdilley@example.com"},
        ]
        # "Brian Dilley" should match via display_name with higher score than email
        result = resolver.resolve("Brian Dilley")
        assert result is not None
        assert result.confidence == 1.0

    def test_resolve_caches(self) -> None:
        resolver = NameResolver()
        resolver._users = [
            {"_id": "brian", "display_name": "Brian Dilley"},
        ]
        result1 = resolver.resolve("Brian")
        result2 = resolver.resolve("Brian")
        assert result1 is result2  # Same cached object


class TestUtils:
    def test_epoch_ms_to_iso(self) -> None:
        iso = _epoch_ms_to_iso(1700000000000)
        assert "2023-11-14" in iso

    def test_epoch_ms_zero(self) -> None:
        assert _epoch_ms_to_iso(0) == ""
