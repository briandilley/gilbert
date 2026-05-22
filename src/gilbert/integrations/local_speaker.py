"""Local speaker backend — plays audio through the host machine's sound output."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import platform
import shutil
import tempfile
from pathlib import Path

import httpx

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.speaker import (
    PlaybackState,
    PlayRequest,
    SpeakerBackend,
    SpeakerInfo,
)
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)

_SPEAKER_ID = "local"
_HTTP_TIMEOUT_SECONDS = 30.0
_TERMINATE_GRACE_SECONDS = 2.0


class LocalSpeakerBackend(SpeakerBackend):
    """Plays audio on the host machine's default sound output device.

    The backend exposes a single virtual speaker (``speaker_id="local"``)
    representing the host. ``play_uri`` downloads the request's HTTP(S)
    URI to a temp file (most CLI players can't read URLs directly) and
    spawns a local audio player as an async subprocess so the call
    returns immediately and ``stop()`` can terminate playback mid-clip.

    Player selection: an explicit ``player_command`` setting wins;
    otherwise auto-detect, preferring ``afplay`` on macOS and trying
    ``ffplay``, ``mpv``, ``mpg123`` elsewhere. Per-clip volume is
    applied via the player's command-line flag — the host's system
    volume is never touched.
    """

    backend_name = "local"
    supports_repeat = False

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="player_command",
                type=ToolParameterType.STRING,
                description=(
                    "Command-line audio player to spawn. Leave empty for "
                    "auto-detection (afplay on macOS; ffplay / mpv / mpg123 "
                    "elsewhere). Must accept a file path as its final "
                    "positional argument."
                ),
                default="",
                restart_required=True,
            ),
            ConfigParam(
                key="display_name",
                type=ToolParameterType.STRING,
                description="Display name shown for this speaker in the UI.",
                default="Local Speaker",
            ),
        ]

    def __init__(self) -> None:
        self._display_name = "Local Speaker"
        self._player_cmd: str = ""
        self._volume: int = 100
        self._state: PlaybackState = PlaybackState.STOPPED
        self._proc: asyncio.subprocess.Process | None = None
        self._current_tmp: Path | None = None
        self._proc_lock = asyncio.Lock()

    # ── Lifecycle ────────────────────────────────────────────────────

    async def initialize(self, config: dict[str, object]) -> None:
        display = str(config.get("display_name", "") or "").strip()
        self._display_name = display or "Local Speaker"

        configured = str(config.get("player_command", "") or "").strip()
        if configured:
            if shutil.which(configured) is None:
                raise RuntimeError(
                    f"Configured player_command not found on PATH: {configured!r}"
                )
            self._player_cmd = configured
        else:
            detected = self._detect_player()
            if not detected:
                raise RuntimeError(
                    "No local audio player found. Install one of: afplay "
                    "(bundled with macOS), ffplay (ffmpeg), mpv, or mpg123 — "
                    "or set 'player_command' in the speaker backend settings."
                )
            self._player_cmd = detected

        logger.info(
            "Local speaker backend initialized — player=%s display=%r",
            self._player_cmd,
            self._display_name,
        )

    async def close(self) -> None:
        await self._terminate_proc()

    # ── Discovery ────────────────────────────────────────────────────

    async def list_speakers(self) -> list[SpeakerInfo]:
        return [self._speaker_info()]

    async def get_speaker(self, speaker_id: str) -> SpeakerInfo | None:
        if speaker_id != _SPEAKER_ID:
            return None
        return self._speaker_info()

    def _speaker_info(self) -> SpeakerInfo:
        return SpeakerInfo(
            speaker_id=_SPEAKER_ID,
            name=self._display_name,
            ip_address="127.0.0.1",
            model=f"local/{platform.system().lower()}",
            volume=self._volume,
            state=self._state,
        )

    # ── Playback ─────────────────────────────────────────────────────

    async def play_uri(self, request: PlayRequest) -> None:
        if request.speaker_ids and _SPEAKER_ID not in request.speaker_ids:
            logger.debug(
                "Local speaker received play_uri for unknown speaker_ids=%s; "
                "playing on host anyway",
                request.speaker_ids,
            )

        if request.volume is not None:
            effective_volume = max(0, min(100, int(request.volume)))
        else:
            effective_volume = self._volume

        audio_path, is_tempfile = await self._materialize_uri(request.uri)
        argv = self._build_argv(audio_path, effective_volume)

        # Stop any prior playback before launching the new one so two
        # clips don't talk over each other. Terminating clears
        # ``_current_tmp`` for the previous clip too.
        await self._terminate_proc()

        logger.info(
            "Local speaker: launching %s (volume=%d, announce=%s)",
            " ".join(argv),
            effective_volume,
            request.announce,
        )
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._proc = proc
        self._current_tmp = audio_path if is_tempfile else None
        self._state = PlaybackState.PLAYING
        # Fire-and-forget watcher resets state once the player exits.
        asyncio.create_task(self._watch_proc(proc, audio_path, is_tempfile))

    async def stop(self, speaker_ids: list[str] | None = None) -> None:
        # Only one virtual speaker, so any non-empty ``speaker_ids`` that
        # doesn't include us is a no-op; otherwise stop playback.
        if speaker_ids and _SPEAKER_ID not in speaker_ids:
            return
        await self._terminate_proc()

    async def get_volume(self, speaker_id: str) -> int:
        return self._volume

    async def set_volume(self, speaker_id: str, volume: int) -> None:
        self._volume = max(0, min(100, int(volume)))

    async def get_playback_state(self, speaker_id: str) -> PlaybackState:
        proc = self._proc
        if proc is not None and proc.returncode is not None:
            self._state = PlaybackState.STOPPED
            self._proc = None
        return self._state

    # ── Internals ────────────────────────────────────────────────────

    def _detect_player(self) -> str:
        system = platform.system()
        candidates: tuple[str, ...] = (
            ("afplay", "ffplay", "mpv", "mpg123")
            if system == "Darwin"
            else ("ffplay", "mpv", "mpg123")
        )
        for candidate in candidates:
            if shutil.which(candidate) is not None:
                return candidate
        return ""

    def _build_argv(self, path: Path, volume: int) -> list[str]:
        player = self._player_cmd
        name = Path(player).name
        path_str = str(path)
        if name == "afplay":
            # afplay -v takes a 0.0-1.0 multiplier.
            return [player, "-v", f"{volume / 100:.3f}", path_str]
        if name == "ffplay":
            return [
                player,
                "-nodisp",
                "-autoexit",
                "-loglevel",
                "quiet",
                "-volume",
                str(volume),
                path_str,
            ]
        if name == "mpv":
            return [
                player,
                f"--volume={volume}",
                "--no-video",
                "--really-quiet",
                path_str,
            ]
        if name == "mpg123":
            # mpg123 -f is a 0–32768 scaling factor.
            return [player, "-q", "-f", str(int(volume * 32768 / 100)), path_str]
        # Unknown / user-supplied player: pass the file path only. The
        # user owns the contract for any extra args via a wrapper script.
        return [player, path_str]

    async def _materialize_uri(self, uri: str) -> tuple[Path, bool]:
        """Resolve ``uri`` to a local file path the player can open.

        Returns ``(path, is_tempfile)``. HTTP(S) URLs are streamed to a
        temp file (most CLI players don't accept network input);
        ``file://`` and bare filesystem paths are returned as-is.
        """
        if uri.startswith("file://"):
            return Path(uri[len("file://") :]), False
        if not uri.startswith(("http://", "https://")):
            return Path(uri), False

        suffix = self._guess_suffix(uri)
        fd, tmp_name = tempfile.mkstemp(
            prefix="gilbert-localspeaker-", suffix=suffix
        )
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
                async with client.stream("GET", uri) as response:
                    response.raise_for_status()
                    with tmp_path.open("wb") as out:
                        async for chunk in response.aiter_bytes():
                            out.write(chunk)
        except Exception:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            raise
        return tmp_path, True

    @staticmethod
    def _guess_suffix(uri: str) -> str:
        path = uri.split("?", 1)[0]
        for ext in (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"):
            if path.lower().endswith(ext):
                return ext
        return ".mp3"

    async def _terminate_proc(self) -> None:
        async with self._proc_lock:
            proc = self._proc
            tmp = self._current_tmp
            self._proc = None
            self._current_tmp = None
            self._state = PlaybackState.STOPPED
            if proc is None or proc.returncode is not None:
                self._cleanup_tempfile(tmp)
                return
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_GRACE_SECONDS)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
            self._cleanup_tempfile(tmp)

    async def _watch_proc(
        self,
        proc: asyncio.subprocess.Process,
        tmp_path: Path,
        is_tempfile: bool,
    ) -> None:
        try:
            await proc.wait()
        except asyncio.CancelledError:
            return
        finally:
            # Only reset state if no newer playback has replaced this one.
            # ``_terminate_proc`` will already have nulled ``self._proc``
            # if the caller pre-empted us, in which case we leave its
            # bookkeeping alone.
            if self._proc is proc:
                self._proc = None
                self._current_tmp = None
                self._state = PlaybackState.STOPPED
            if is_tempfile:
                self._cleanup_tempfile(tmp_path)

    @staticmethod
    def _cleanup_tempfile(path: Path | None) -> None:
        if path is None:
            return
        with contextlib.suppress(OSError):
            path.unlink()
