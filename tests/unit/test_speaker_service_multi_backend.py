"""Task 8 + 9: SpeakerService._backends dict storage and _reinit_backends lifecycle tests."""

import logging

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


# ---------------------------------------------------------------------------
# Task 9: _reinit_backends lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reinit_backends_starts_enabled_drops_disabled():
    svc = SpeakerService()
    await svc._reinit_backends({
        "fake_a": {"enabled": True},
        "fake_b": {"enabled": False},
    })
    assert set(svc._backends) == {"fake_a"}

    # Flip enable states
    await svc._reinit_backends({
        "fake_a": {"enabled": False},
        "fake_b": {"enabled": True},
    })
    assert set(svc._backends) == {"fake_b"}


@pytest.mark.asyncio
async def test_reinit_backends_records_startup_failure_for_failing_backend(caplog):
    class FakeBackendBoom(FakeSpeakerBackendA):
        backend_name = "fake_boom"

        async def initialize(self, config: dict) -> None:
            raise RuntimeError("backend failure")

    svc = SpeakerService()
    with caplog.at_level(logging.WARNING, logger="gilbert.core.services.speaker"):
        await svc._reinit_backends({"fake_a": {"enabled": True}, "fake_boom": {"enabled": True}})
    assert "fake_a" in svc._backends
    assert "fake_boom" not in svc._backends
    assert "fake_boom" in svc._startup_failures
    assert "backend failure" in svc._startup_failures["fake_boom"]


@pytest.mark.asyncio
async def test_reinit_backends_drops_section_not_in_config():
    svc = SpeakerService()
    await svc._reinit_backends({"fake_a": {"enabled": True}, "fake_b": {"enabled": True}})
    assert set(svc._backends) == {"fake_a", "fake_b"}
    # Remove fake_a entirely from config
    await svc._reinit_backends({"fake_b": {"enabled": True}})
    assert set(svc._backends) == {"fake_b"}


# ---------------------------------------------------------------------------
# Task 10: list_speakers / list_speaker_groups merge across backends
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_speakers_merges_across_backends():
    svc = SpeakerService()
    svc._backends = {
        "fake_a": FakeSpeakerBackendA(),
        "fake_b": FakeSpeakerBackendB(),
    }
    speakers = await svc.list_speakers()
    backends_present = {s.backend_name for s in speakers}
    assert backends_present == {"fake_a", "fake_b"}
    ids = sorted(s.speaker_id for s in speakers)
    assert ids == ["fake_a:a1", "fake_a:a2", "fake_b:b1"]


@pytest.mark.asyncio
async def test_list_speakers_tolerates_one_backend_raising():
    svc = SpeakerService()
    a = FakeSpeakerBackendA()
    b = FakeSpeakerBackendB()

    async def boom(self=None):
        raise RuntimeError("backend unreachable")
    b.list_speakers = boom  # type: ignore[method-assign]
    svc._backends = {"fake_a": a, "fake_b": b}

    speakers = await svc.list_speakers()
    assert all(s.backend_name == "fake_a" for s in speakers)
    assert len(speakers) == 2  # fake_a still returns its two speakers


@pytest.mark.asyncio
async def test_list_speakers_returns_empty_when_no_backends():
    svc = SpeakerService()
    speakers = await svc.list_speakers()
    assert speakers == []
