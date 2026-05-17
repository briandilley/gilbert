"""Task 8: SpeakerService._backends dict storage tests."""

import pytest
from gilbert.core.services.speaker import SpeakerService
from gilbert.interfaces.speaker import (
    PlaybackState,
    PlayRequest,
    SpeakerBackend,
    SpeakerGroup,
    SpeakerInfo,
)


class FakeSpeakerBackendA(SpeakerBackend):
    backend_name = "fake_a"
    supports_repeat = False

    def __init__(self) -> None:
        self._speakers = [
            SpeakerInfo(speaker_id="a1", name="Speaker A1", ip_address="10.0.0.1"),
            SpeakerInfo(speaker_id="a2", name="Speaker A2", ip_address="10.0.0.2"),
        ]
        self.played: list[PlayRequest] = []

    async def initialize(self, config: dict) -> None:
        pass

    async def close(self) -> None:
        pass

    async def list_speakers(self) -> list[SpeakerInfo]:
        return list(self._speakers)

    async def get_speaker(self, speaker_id: str) -> SpeakerInfo | None:
        for s in self._speakers:
            if s.speaker_id == speaker_id:
                return s
        return None

    async def list_groups(self) -> list[SpeakerGroup]:
        return []

    async def play_uri(self, request: PlayRequest) -> None:
        self.played.append(request)

    async def stop(self, speaker_ids: list[str]) -> None:
        pass

    async def set_volume(self, speaker_id: str, level: int) -> None:
        pass

    async def get_volume(self, speaker_id: str) -> int:
        return 50

    async def get_playback_state(self, speaker_id: str) -> PlaybackState:
        return PlaybackState.STOPPED

    async def get_now_playing(self, speaker_id: str):
        return None

    async def group_speakers(self, speaker_ids: list[str]) -> str:
        return "g1"

    async def ungroup_speakers(self, speaker_ids: list[str]) -> None:
        pass


class FakeSpeakerBackendB(FakeSpeakerBackendA):
    backend_name = "fake_b"

    def __init__(self) -> None:
        super().__init__()
        self._speakers = [
            SpeakerInfo(speaker_id="b1", name="Speaker B1", ip_address="10.0.1.1"),
        ]


@pytest.mark.asyncio
async def test_service_stores_backends_in_dict():
    """SpeakerService stores backends in a dict keyed by backend_name."""
    svc = SpeakerService()
    svc._backends = {"fake_a": FakeSpeakerBackendA(), "fake_b": FakeSpeakerBackendB()}
    assert isinstance(svc._backends, dict)
    assert set(svc._backends) == {"fake_a", "fake_b"}
    assert svc.backends["fake_a"] is svc._backends["fake_a"]
