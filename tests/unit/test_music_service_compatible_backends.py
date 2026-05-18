"""Tests for MusicBackend.compatible_speaker_backends classmethod."""
import pytest
from gilbert.interfaces.music import MusicBackend, MusicItemKind


class _DemoMusic(MusicBackend):
    """Minimal concrete subclass for default-behavior testing."""
    backend_name = "demo"

    async def initialize(self, config: dict) -> None:
        pass

    async def close(self) -> None:
        pass

    async def list_favorites(self):
        return []

    async def list_playlists(self):
        return []

    async def search(self, query: str, *, kind: MusicItemKind = MusicItemKind.TRACK, limit: int = 10):
        return []

    async def resolve_playable(self, item):
        pass


def test_default_compatible_speaker_backends_is_wildcard():
    assert _DemoMusic.compatible_speaker_backends() == frozenset({"*"})
