"""Tests for SpeakerService — speaker control, aliases, grouping, and announce."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gilbert.config import GilbertConfig
from gilbert.core.services.speaker import SpeakerService
from gilbert.core.services.storage import StorageService
from gilbert.interfaces.service import ServiceResolver
from gilbert.interfaces.speaker import (
    PlayRequest,
    PlaybackState,
    SpeakerBackend,
    SpeakerGroup,
    SpeakerInfo,
)
from gilbert.interfaces.storage import StorageBackend
from gilbert.interfaces.tts import AudioFormat, SynthesisRequest, SynthesisResult, TTSBackend


class StubSpeakerBackend(SpeakerBackend):
    """In-memory speaker backend for testing."""

    def __init__(self, *, grouping: bool = True) -> None:
        self.initialized = False
        self.closed = False
        self._grouping = grouping
        self._speakers: list[SpeakerInfo] = [
            SpeakerInfo(
                speaker_id="uid-1", name="Speaker 1", ip_address="192.168.1.10",
                model="Sonos One", volume=30, state=PlaybackState.STOPPED,
            ),
            SpeakerInfo(
                speaker_id="uid-2", name="Speaker 2", ip_address="192.168.1.11",
                model="Sonos One", volume=50, state=PlaybackState.STOPPED,
            ),
            SpeakerInfo(
                speaker_id="uid-3", name="Speaker 3", ip_address="192.168.1.12",
                model="Sonos Five", volume=40, state=PlaybackState.PLAYING,
            ),
        ]
        self._groups: list[SpeakerGroup] = []
        self.last_play_request: PlayRequest | None = None
        self.stopped_ids: list[str] | None = None
        self.volume_changes: list[tuple[str, int]] = []

    async def initialize(self, config: dict[str, object]) -> None:
        self.initialized = True

    async def close(self) -> None:
        self.closed = True

    async def list_speakers(self) -> list[SpeakerInfo]:
        return list(self._speakers)

    async def get_speaker(self, speaker_id: str) -> SpeakerInfo | None:
        for s in self._speakers:
            if s.speaker_id == speaker_id:
                return s
        return None

    async def play_uri(self, request: PlayRequest) -> None:
        self.last_play_request = request

    async def stop(self, speaker_ids: list[str] | None = None) -> None:
        self.stopped_ids = speaker_ids

    async def get_volume(self, speaker_id: str) -> int:
        for s in self._speakers:
            if s.speaker_id == speaker_id:
                return s.volume
        raise KeyError(f"Speaker not found: {speaker_id}")

    async def set_volume(self, speaker_id: str, volume: int) -> None:
        self.volume_changes.append((speaker_id, volume))

    @property
    def supports_grouping(self) -> bool:
        return self._grouping

    async def list_groups(self) -> list[SpeakerGroup]:
        return list(self._groups)

    async def group_speakers(self, speaker_ids: list[str]) -> SpeakerGroup:
        group = SpeakerGroup(
            group_id="grp-1",
            name="Test Group",
            coordinator_id=speaker_ids[0],
            member_ids=list(speaker_ids),
        )
        self._groups = [group]
        return group

    async def ungroup_speakers(self, speaker_ids: list[str]) -> None:
        self._groups = []


class StubStorageBackend(StorageBackend):
    """Minimal in-memory storage for alias tests."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict[str, Any]]] = {}
        self._indexes: list[Any] = []

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
        collection = query.collection
        entities = list(self._data.get(collection, {}).values())
        for f in query.filters:
            entities = [e for e in entities if e.get(f.field) == f.value]
        return entities

    async def count(self, query: Any) -> int:
        return len(await self.query(query))

    async def list_collections(self) -> list[str]:
        return list(self._data.keys())

    async def drop_collection(self, collection: str) -> None:
        self._data.pop(collection, None)

    async def ensure_index(self, index: Any) -> None:
        self._indexes.append(index)

    async def list_indexes(self, collection: str) -> list[Any]:
        return self._indexes

    async def ensure_foreign_key(self, fk: Any) -> None:
        pass

    async def list_foreign_keys(self, collection: str) -> list[Any]:
        return []


@pytest.fixture
def stub_backend() -> StubSpeakerBackend:
    return StubSpeakerBackend()


@pytest.fixture
def stub_storage() -> StubStorageBackend:
    return StubStorageBackend()


@pytest.fixture
def storage_service(stub_storage: StubStorageBackend) -> StorageService:
    return StorageService(stub_storage)


@pytest.fixture
def resolver(storage_service: StorageService) -> ServiceResolver:
    mock = AsyncMock(spec=ServiceResolver)

    def get_capability(cap: str) -> Any:
        if cap == "entity_storage":
            return storage_service
        return None

    def require_capability(cap: str) -> Any:
        if cap == "entity_storage":
            return storage_service
        raise LookupError(f"Missing capability: {cap}")

    mock.get_capability.side_effect = get_capability
    mock.require_capability.side_effect = require_capability
    return mock


@pytest.fixture
def service(stub_backend: StubSpeakerBackend) -> SpeakerService:
    return SpeakerService(stub_backend)


# --- Service info ---


def test_service_info(service: SpeakerService) -> None:
    info = service.service_info()
    assert info.name == "speaker"
    assert "speaker_control" in info.capabilities
    assert "ai_tools" in info.capabilities
    assert "entity_storage" in info.requires


# --- Lifecycle ---


async def test_start_initializes_backend(
    service: SpeakerService, stub_backend: StubSpeakerBackend, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    assert stub_backend.initialized


async def test_stop_closes_backend(
    service: SpeakerService, stub_backend: StubSpeakerBackend, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    await service.stop()
    assert stub_backend.closed


# --- Tool provider ---


def test_tool_provider_name(service: SpeakerService) -> None:
    assert service.tool_provider_name == "speaker"


def test_get_tools_with_grouping(service: SpeakerService) -> None:
    tools = service.get_tools()
    names = [t.name for t in tools]
    assert "list_speakers" in names
    assert "play_audio" in names
    assert "stop_audio" in names
    assert "set_volume" in names
    assert "get_volume" in names
    assert "set_speaker_alias" in names
    assert "remove_speaker_alias" in names
    assert "announce" in names
    assert "group_speakers" in names
    assert "ungroup_speakers" in names
    assert "list_speaker_groups" in names


def test_get_tools_without_grouping() -> None:
    backend = StubSpeakerBackend(grouping=False)
    svc = SpeakerService(backend)
    tools = svc.get_tools()
    names = [t.name for t in tools]
    assert "group_speakers" not in names
    assert "ungroup_speakers" not in names
    assert "list_speaker_groups" not in names


# --- List speakers ---


async def test_tool_list_speakers(
    service: SpeakerService, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    result = await service.execute_tool("list_speakers", {})
    parsed = json.loads(result)
    assert len(parsed) == 3
    assert parsed[0]["name"] == "Speaker 1"
    assert parsed[0]["volume"] == 30
    assert parsed[2]["state"] == "playing"


# --- Volume ---


async def test_tool_set_volume(
    service: SpeakerService, stub_backend: StubSpeakerBackend, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    result = await service.execute_tool("set_volume", {"speaker": "Speaker 2", "volume": 75})
    parsed = json.loads(result)
    assert parsed["status"] == "ok"
    assert stub_backend.volume_changes == [("uid-2", 75)]


async def test_tool_get_volume(
    service: SpeakerService, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    result = await service.execute_tool("get_volume", {"speaker": "Speaker 1"})
    parsed = json.loads(result)
    assert parsed["volume"] == 30


async def test_tool_volume_unknown_speaker(
    service: SpeakerService, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    result = await service.execute_tool("get_volume", {"speaker": "Nonexistent"})
    parsed = json.loads(result)
    assert "error" in parsed


# --- Play / Stop ---


async def test_tool_play_audio(
    service: SpeakerService, stub_backend: StubSpeakerBackend, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    result = await service.execute_tool("play_audio", {
        "uri": "http://example.com/song.mp3",
        "speakers": ["Speaker 1"],
    })
    parsed = json.loads(result)
    assert parsed["status"] == "playing"
    assert stub_backend.last_play_request is not None
    assert stub_backend.last_play_request.uri == "http://example.com/song.mp3"
    assert stub_backend.last_play_request.speaker_ids == ["uid-1"]


async def test_tool_play_audio_uses_last_speakers(
    service: SpeakerService, stub_backend: StubSpeakerBackend, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    # First play with explicit speakers
    await service.execute_tool("play_audio", {
        "uri": "http://example.com/a.mp3",
        "speakers": ["Speaker 2"],
    })
    # Second play without specifying speakers — should use last
    await service.execute_tool("play_audio", {"uri": "http://example.com/b.mp3"})
    assert stub_backend.last_play_request is not None
    assert stub_backend.last_play_request.speaker_ids == ["uid-2"]


async def test_tool_stop_audio(
    service: SpeakerService, stub_backend: StubSpeakerBackend, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    result = await service.execute_tool("stop_audio", {"speakers": ["Speaker 3"]})
    parsed = json.loads(result)
    assert parsed["status"] == "stopped"
    assert stub_backend.stopped_ids == ["uid-3"]


async def test_tool_stop_audio_all(
    service: SpeakerService, stub_backend: StubSpeakerBackend, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    result = await service.execute_tool("stop_audio", {})
    parsed = json.loads(result)
    assert parsed["status"] == "stopped"
    # All speakers resolved and stopped
    assert set(stub_backend.stopped_ids) == {"uid-1", "uid-2", "uid-3"}


# --- Aliases ---


async def test_set_and_resolve_alias(
    service: SpeakerService, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    await service.set_alias("uid-2", "Living Room Speaker")

    sid = await service.resolve_speaker_name("Living Room Speaker")
    assert sid == "uid-2"


async def test_alias_collision_with_speaker_name(
    service: SpeakerService, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    with pytest.raises(ValueError, match="collides with existing speaker name"):
        await service.set_alias("uid-1", "Speaker 2")


async def test_alias_collision_with_other_alias(
    service: SpeakerService, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    await service.set_alias("uid-1", "Kitchen")
    with pytest.raises(ValueError, match="already assigned"):
        await service.set_alias("uid-2", "Kitchen")


async def test_remove_alias(
    service: SpeakerService, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    await service.set_alias("uid-2", "Bedroom")
    await service.remove_alias("Bedroom")

    sid = await service.resolve_speaker_name("Bedroom")
    assert sid is None


async def test_alias_in_play_command(
    service: SpeakerService, stub_backend: StubSpeakerBackend, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    await service.set_alias("uid-3", "Office")

    await service.execute_tool("play_audio", {
        "uri": "http://example.com/test.mp3",
        "speakers": ["Office"],
    })
    assert stub_backend.last_play_request is not None
    assert stub_backend.last_play_request.speaker_ids == ["uid-3"]


async def test_tool_set_alias(
    service: SpeakerService, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    result = await service.execute_tool("set_speaker_alias", {
        "speaker": "Speaker 1",
        "alias": "Front Porch",
    })
    parsed = json.loads(result)
    assert parsed["status"] == "ok"

    sid = await service.resolve_speaker_name("Front Porch")
    assert sid == "uid-1"


async def test_tool_remove_alias(
    service: SpeakerService, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    await service.set_alias("uid-1", "Garage")
    result = await service.execute_tool("remove_speaker_alias", {"alias": "Garage"})
    parsed = json.loads(result)
    assert parsed["status"] == "ok"


# --- Grouping ---


async def test_tool_group_speakers(
    service: SpeakerService, stub_backend: StubSpeakerBackend, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    result = await service.execute_tool("group_speakers", {
        "speakers": ["Speaker 1", "Speaker 2"],
    })
    parsed = json.loads(result)
    assert parsed["status"] == "grouped"
    assert len(parsed["member_ids"]) == 2


async def test_tool_ungroup_speakers(
    service: SpeakerService, stub_backend: StubSpeakerBackend, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    result = await service.execute_tool("ungroup_speakers", {
        "speakers": ["Speaker 1", "Speaker 2"],
    })
    parsed = json.loads(result)
    assert parsed["status"] == "ungrouped"


async def test_tool_list_groups(
    service: SpeakerService, stub_backend: StubSpeakerBackend, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    # Create a group first
    await service.execute_tool("group_speakers", {
        "speakers": ["Speaker 1", "Speaker 2"],
    })
    result = await service.execute_tool("list_speaker_groups", {})
    parsed = json.loads(result)
    assert len(parsed) == 1
    assert parsed[0]["name"] == "Test Group"


# --- Announce ---


async def test_announce_requires_tts(
    service: SpeakerService, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    result = await service.execute_tool("announce", {"text": "Hello everyone"})
    parsed = json.loads(result)
    assert "error" in parsed
    assert "TTS" in parsed["error"]


async def test_announce_with_tts(
    stub_backend: StubSpeakerBackend,
    resolver: ServiceResolver,
    storage_service: StorageService,
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    import gilbert.core.output as output_mod

    monkeypatch.setattr(output_mod, "OUTPUT_DIR", tmp_path / "output")

    # Create a mock TTS service
    from gilbert.core.services.tts import TTSService

    mock_tts = MagicMock(spec=TTSService)
    mock_tts.synthesize = AsyncMock(return_value=SynthesisResult(
        audio=b"fake-announcement-audio",
        format=AudioFormat.MP3,
        characters_used=15,
    ))

    # Build resolver that provides TTS
    mock_resolver = AsyncMock(spec=ServiceResolver)

    def get_cap(cap: str) -> Any:
        if cap == "entity_storage":
            return storage_service
        if cap == "text_to_speech":
            return mock_tts
        if cap == "configuration":
            return None
        return None

    def require_cap(cap: str) -> Any:
        if cap == "entity_storage":
            return storage_service
        raise LookupError(cap)

    mock_resolver.get_capability.side_effect = get_cap
    mock_resolver.require_capability.side_effect = require_cap

    service = SpeakerService(stub_backend)
    await service.start(mock_resolver)

    result = await service.execute_tool("announce", {
        "text": "Dinner is ready",
        "speakers": ["Speaker 1", "Speaker 2"],
        "volume": 60,
    })
    parsed = json.loads(result)
    assert parsed["status"] == "announced"
    assert parsed["text"] == "Dinner is ready"

    # Verify TTS was called
    mock_tts.synthesize.assert_awaited_once()

    # Verify audio was played on the speakers
    assert stub_backend.last_play_request is not None
    assert stub_backend.last_play_request.speaker_ids == ["uid-1", "uid-2"]
    assert stub_backend.last_play_request.volume == 60


# --- Config parsing ---


def test_config_speaker_defaults() -> None:
    config = GilbertConfig.model_validate({})
    assert config.speaker.enabled is False
    assert config.speaker.backend == "sonos"
    assert config.speaker.default_announce_volume is None
    assert config.speaker.settings == {}


def test_config_speaker_full() -> None:
    raw = {
        "speaker": {
            "enabled": True,
            "backend": "sonos",
            "default_announce_volume": 40,
            "settings": {"timeout": 5},
        }
    }
    config = GilbertConfig.model_validate(raw)
    assert config.speaker.enabled is True
    assert config.speaker.default_announce_volume == 40
    assert config.speaker.settings["timeout"] == 5


# --- Unknown tool ---


async def test_tool_unknown_raises(
    service: SpeakerService, resolver: ServiceResolver
) -> None:
    await service.start(resolver)
    with pytest.raises(KeyError, match="Unknown tool"):
        await service.execute_tool("nonexistent", {})
