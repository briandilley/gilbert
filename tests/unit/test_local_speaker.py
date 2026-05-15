"""Tests for LocalSpeakerBackend — host-machine audio playback."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from gilbert.integrations.local_speaker import LocalSpeakerBackend
from gilbert.interfaces.speaker import PlaybackState, PlayRequest


class _FakeProc:
    """Minimal stand-in for asyncio.subprocess.Process."""

    def __init__(self, exit_after: float | None = None) -> None:
        self._exit_after = exit_after
        self._returncode: int | None = None
        self._done = asyncio.Event()
        self.terminated = False
        self.killed = False
        if exit_after is not None:
            asyncio.get_event_loop().call_later(exit_after, self._finish)

    def _finish(self, code: int = 0) -> None:
        if self._returncode is None:
            self._returncode = code
            self._done.set()

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True
        self._finish(0)

    def kill(self) -> None:
        self.killed = True
        self._finish(-9)

    async def wait(self) -> int:
        await self._done.wait()
        assert self._returncode is not None
        return self._returncode


@pytest.fixture
def patched_backend(monkeypatch: pytest.MonkeyPatch) -> LocalSpeakerBackend:
    """LocalSpeakerBackend with subprocess + httpx + filesystem mocked.

    The mocks let the test exercise the full play_uri → stop pipeline
    without needing a real audio player or network endpoint.
    """
    backend = LocalSpeakerBackend()

    # Bypass player auto-detection during initialize.
    monkeypatch.setattr(backend, "_detect_player", lambda: "ffplay")

    spawned: list[list[str]] = []
    procs: list[_FakeProc] = []

    async def fake_exec(*argv: str, **_: Any) -> _FakeProc:
        spawned.append(list(argv))
        proc = _FakeProc(exit_after=None)  # stays "running" until terminated
        procs.append(proc)
        return proc

    monkeypatch.setattr(
        "gilbert.integrations.local_speaker.asyncio.create_subprocess_exec",
        fake_exec,
    )

    # Skip the HTTP fetch — pretend every URI is already on disk.
    async def fake_materialize(self: LocalSpeakerBackend, uri: str) -> tuple[Path, bool]:
        return Path("/tmp/fake.mp3"), False

    monkeypatch.setattr(LocalSpeakerBackend, "_materialize_uri", fake_materialize)

    backend._test_spawned = spawned  # type: ignore[attr-defined]
    backend._test_procs = procs  # type: ignore[attr-defined]
    return backend


@pytest.mark.asyncio
async def test_initialize_auto_detects_player(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = LocalSpeakerBackend()
    monkeypatch.setattr(backend, "_detect_player", lambda: "afplay")
    await backend.initialize({})
    assert backend._player_cmd == "afplay"


@pytest.mark.asyncio
async def test_initialize_raises_when_no_player_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = LocalSpeakerBackend()
    monkeypatch.setattr(backend, "_detect_player", lambda: "")
    with pytest.raises(RuntimeError, match="No local audio player"):
        await backend.initialize({})


@pytest.mark.asyncio
async def test_initialize_rejects_missing_configured_player(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "gilbert.integrations.local_speaker.shutil.which", lambda _: None
    )
    backend = LocalSpeakerBackend()
    with pytest.raises(RuntimeError, match="not found on PATH"):
        await backend.initialize({"player_command": "doesnotexist"})


@pytest.mark.asyncio
async def test_initialize_uses_configured_player(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "gilbert.integrations.local_speaker.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    backend = LocalSpeakerBackend()
    await backend.initialize({"player_command": "mpv", "display_name": "Kitchen"})
    assert backend._player_cmd == "mpv"
    assert backend._display_name == "Kitchen"


@pytest.mark.asyncio
async def test_list_speakers_returns_single_virtual_speaker(
    patched_backend: LocalSpeakerBackend,
) -> None:
    await patched_backend.initialize({"display_name": "Office"})
    speakers = await patched_backend.list_speakers()
    assert len(speakers) == 1
    assert speakers[0].speaker_id == "local"
    assert speakers[0].name == "Office"
    assert speakers[0].state == PlaybackState.STOPPED


@pytest.mark.asyncio
async def test_get_speaker_unknown_id_returns_none(
    patched_backend: LocalSpeakerBackend,
) -> None:
    await patched_backend.initialize({})
    assert await patched_backend.get_speaker("not-local") is None
    info = await patched_backend.get_speaker("local")
    assert info is not None and info.speaker_id == "local"


@pytest.mark.asyncio
async def test_play_uri_spawns_player_and_marks_playing(
    patched_backend: LocalSpeakerBackend,
) -> None:
    await patched_backend.initialize({})
    await patched_backend.play_uri(
        PlayRequest(uri="http://host/clip.mp3", volume=50)
    )

    spawned = patched_backend._test_spawned  # type: ignore[attr-defined]
    assert len(spawned) == 1
    # ffplay with our standard args + -volume 50.
    assert "ffplay" in spawned[0][0]
    assert "-volume" in spawned[0]
    assert spawned[0][spawned[0].index("-volume") + 1] == "50"
    assert patched_backend._state == PlaybackState.PLAYING


@pytest.mark.asyncio
async def test_play_uri_terminates_existing_playback(
    patched_backend: LocalSpeakerBackend,
) -> None:
    await patched_backend.initialize({})
    await patched_backend.play_uri(PlayRequest(uri="http://host/a.mp3"))
    first_proc = patched_backend._test_procs[0]  # type: ignore[attr-defined]
    await patched_backend.play_uri(PlayRequest(uri="http://host/b.mp3"))
    assert first_proc.terminated is True
    assert len(patched_backend._test_spawned) == 2  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_stop_terminates_subprocess(
    patched_backend: LocalSpeakerBackend,
) -> None:
    await patched_backend.initialize({})
    await patched_backend.play_uri(PlayRequest(uri="http://host/a.mp3"))
    proc = patched_backend._test_procs[0]  # type: ignore[attr-defined]
    await patched_backend.stop()
    assert proc.terminated is True
    assert patched_backend._state == PlaybackState.STOPPED


@pytest.mark.asyncio
async def test_stop_ignores_unrelated_speaker_ids(
    patched_backend: LocalSpeakerBackend,
) -> None:
    await patched_backend.initialize({})
    await patched_backend.play_uri(PlayRequest(uri="http://host/a.mp3"))
    proc = patched_backend._test_procs[0]  # type: ignore[attr-defined]
    await patched_backend.stop(speaker_ids=["uid-foreign"])
    assert proc.terminated is False
    # And stopping with our own id (or None) does terminate.
    await patched_backend.stop(speaker_ids=["local"])
    assert proc.terminated is True


@pytest.mark.asyncio
async def test_set_volume_clamps_and_persists(
    patched_backend: LocalSpeakerBackend,
) -> None:
    await patched_backend.initialize({})
    await patched_backend.set_volume("local", 250)
    assert await patched_backend.get_volume("local") == 100
    await patched_backend.set_volume("local", -10)
    assert await patched_backend.get_volume("local") == 0
    await patched_backend.set_volume("local", 42)
    assert await patched_backend.get_volume("local") == 42


@pytest.mark.asyncio
async def test_play_uri_uses_stored_volume_when_request_volume_none(
    patched_backend: LocalSpeakerBackend,
) -> None:
    await patched_backend.initialize({})
    await patched_backend.set_volume("local", 33)
    await patched_backend.play_uri(PlayRequest(uri="http://host/a.mp3"))
    argv = patched_backend._test_spawned[0]  # type: ignore[attr-defined]
    assert argv[argv.index("-volume") + 1] == "33"


def test_build_argv_volume_scaling_per_player() -> None:
    backend = LocalSpeakerBackend()
    path = Path("/tmp/x.mp3")
    backend._player_cmd = "afplay"
    argv = backend._build_argv(path, 50)
    assert argv[:3] == ["afplay", "-v", "0.500"]
    backend._player_cmd = "ffplay"
    argv = backend._build_argv(path, 75)
    assert "-volume" in argv and argv[argv.index("-volume") + 1] == "75"
    backend._player_cmd = "mpv"
    argv = backend._build_argv(path, 20)
    assert "--volume=20" in argv
    backend._player_cmd = "mpg123"
    argv = backend._build_argv(path, 100)
    assert argv[argv.index("-f") + 1] == "32768"


def test_guess_suffix() -> None:
    assert LocalSpeakerBackend._guess_suffix("http://h/x.mp3") == ".mp3"
    assert LocalSpeakerBackend._guess_suffix("http://h/X.WAV") == ".wav"
    assert LocalSpeakerBackend._guess_suffix("http://h/clip.m4a?t=1") == ".m4a"
    assert LocalSpeakerBackend._guess_suffix("http://h/nothing") == ".mp3"


@pytest.mark.asyncio
async def test_close_terminates_playing_process(
    patched_backend: LocalSpeakerBackend,
) -> None:
    await patched_backend.initialize({})
    await patched_backend.play_uri(PlayRequest(uri="http://host/a.mp3"))
    proc = patched_backend._test_procs[0]  # type: ignore[attr-defined]
    await patched_backend.close()
    assert proc.terminated is True


def test_backend_registers_under_local_name() -> None:
    """Importing the module should auto-register the backend."""
    import gilbert.integrations.local_speaker  # noqa: F401
    from gilbert.interfaces.speaker import SpeakerBackend

    assert "local" in SpeakerBackend.registered_backends()
    assert SpeakerBackend.registered_backends()["local"] is LocalSpeakerBackend
