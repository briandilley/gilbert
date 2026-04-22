"""Speaker service — wraps a SpeakerBackend as a discoverable service with announce support."""

import asyncio
import contextlib
import json
import logging
import uuid
from typing import Any

from gilbert.core.output import cleanup_old_files, get_output_dir
from gilbert.core.services._backend_actions import (
    all_backend_actions,
    invoke_backend_action,
)
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.speaker import (
    NowPlaying,
    PlaybackState,
    PlayRequest,
    SpeakerBackend,
    SpeakerInfo,
)
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)

logger = logging.getLogger(__name__)

# Entity collection for speaker aliases
_ALIAS_COLLECTION = "speaker_aliases"


class SpeakerService(Service):
    """Exposes a SpeakerBackend as a service with speaker control and announce capabilities."""

    def __init__(self) -> None:
        self._backend: SpeakerBackend | None = None
        self._backend_name: str = "sonos"
        self._enabled: bool = False
        self._config: dict[str, object] = {}
        self._output_ttl_seconds: int = 3600
        self._default_announce_volume: int | None = None
        self._default_announce_speakers: list[str] = []
        self._web_host: str = "0.0.0.0"
        self._web_port: int = 8000
        # Track last-used speaker set for "use last" default
        self._last_speaker_ids: list[str] = []
        # Per-speaker announcement locks. Each speaker has its own lock
        # so announcements targeting *different* speakers fan out, while
        # announcements targeting the *same* speaker still serialize
        # (otherwise snapshot/restore on that speaker would race and
        # two clips would talk over each other). Locks are created lazily
        # and acquired in sorted-ID order so two overlapping-set callers
        # can't deadlock. ``_speaker_locks_guard`` serializes the
        # get-or-create step so we don't race on dict insertion.
        self._speaker_locks: dict[str, asyncio.Lock] = {}
        self._speaker_locks_guard = asyncio.Lock()
        self._speaker_cache: list[SpeakerInfo] = []

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="speaker",
            capabilities=frozenset({"speaker_control", "ai_tools"}),
            requires=frozenset({"entity_storage"}),
            optional=frozenset({"configuration", "text_to_speech"}),
            toggleable=True,
            toggle_description="Speaker playback and control",
        )

    @property
    def backend(self) -> SpeakerBackend | None:
        return self._backend

    @property
    def cached_speakers(self) -> list[SpeakerInfo]:
        """Last-known speaker list (populated after start)."""
        return list(self._speaker_cache)

    async def start(self, resolver: ServiceResolver) -> None:
        # Store resolver references for runtime use
        self._storage_svc = resolver.require_capability("entity_storage")
        self._tts_svc = resolver.get_capability("text_to_speech")

        # Load config
        section: dict[str, Any] = {}
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                section = config_svc.get_section(self.config_namespace)
                global_ttl = config_svc.get("output_ttl_seconds")
                if global_ttl is not None:
                    self._output_ttl_seconds = int(global_ttl)
                # Read web config for building audio URLs
                web_section = config_svc.get_section("web")
                self._web_host = web_section.get("host", "0.0.0.0")
                self._web_port = int(web_section.get("port", 8765))

        if not section.get("enabled", False):
            logger.info("Speaker service disabled")
            return

        self._enabled = True
        self._apply_config(section)

        backend_name = section.get("backend", "sonos")
        self._backend_name = backend_name
        backends = SpeakerBackend.registered_backends()
        backend_cls = backends.get(backend_name)
        if backend_cls is None:
            raise ValueError(f"Unknown speaker backend: {backend_name}")
        self._backend = backend_cls()

        init_config: dict[str, object] = dict(self._config)
        await self._backend.initialize(init_config)

        # Ensure alias index
        from gilbert.interfaces.storage import IndexDefinition

        storage = self._get_storage_backend()
        await storage.ensure_index(
            IndexDefinition(
                collection=_ALIAS_COLLECTION,
                fields=["alias"],
                unique=True,
            )
        )

        # Populate speaker cache for dynamic choices
        try:
            self._speaker_cache = await self._backend.list_speakers()
        except Exception:
            logger.debug("Could not cache speakers on start")

        logger.info("Speaker service started")

    def _require_backend(self) -> SpeakerBackend:
        """Return the backend or raise if the service is not enabled."""
        if self._backend is None:
            raise RuntimeError("Speaker service is not enabled")
        return self._backend

    def _get_storage_backend(self) -> Any:
        """Get the storage backend from the storage service."""
        from gilbert.interfaces.storage import StorageProvider

        if isinstance(self._storage_svc, StorageProvider):
            return self._storage_svc.backend
        raise TypeError("Expected StorageProvider for entity_storage")

    def _apply_config(self, section: dict[str, Any]) -> None:
        """Apply tunable config values."""
        self._config = section.get("settings", self._config)
        vol = section.get("default_announce_volume")
        if vol is not None:
            self._default_announce_volume = int(vol)
        spk = section.get("default_announce_speakers")
        if isinstance(spk, list):
            self._default_announce_speakers = spk

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "speaker"

    @property
    def config_category(self) -> str:
        return "Media"

    def config_params(self) -> list[ConfigParam]:
        from gilbert.interfaces.speaker import SpeakerBackend

        params = [
            ConfigParam(
                key="backend",
                type=ToolParameterType.STRING,
                description="Speaker backend type.",
                default="sonos",
                restart_required=True,
                choices=tuple(SpeakerBackend.registered_backends().keys()),
            ),
            ConfigParam(
                key="default_announce_volume",
                type=ToolParameterType.INTEGER,
                description="Default volume level for announcements (0-100). Unset means use current volume.",
            ),
            ConfigParam(
                key="default_announce_speakers",
                type=ToolParameterType.ARRAY,
                description="Default speakers for announcements (empty = all).",
                default=[],
                choices_from="speakers",
            ),
        ]
        # Use live backend instance if available, otherwise fall back to registry class
        if self._backend is not None:
            backend_params = self._backend.backend_config_params()
        else:
            backends = SpeakerBackend.registered_backends()
            backend_cls = backends.get(self._backend_name)
            backend_params = backend_cls.backend_config_params() if backend_cls else []
        for bp in backend_params:
            params.append(
                ConfigParam(
                    key=f"settings.{bp.key}",
                    type=bp.type,
                    description=bp.description,
                    default=bp.default,
                    restart_required=bp.restart_required,
                    sensitive=bp.sensitive,
                    choices=bp.choices,
                    multiline=bp.multiline,
                    backend_param=True,
                )
            )
        return params

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._apply_config(config)

    # --- ConfigActionProvider ---

    def config_actions(self) -> list[ConfigAction]:
        return all_backend_actions(
            registry=SpeakerBackend.registered_backends(),
            current_backend=self._backend,
        )

    async def invoke_config_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        return await invoke_backend_action(self._backend, key, payload)

    async def stop(self) -> None:
        if self._backend is not None:
            await self._backend.close()

    # --- Alias management ---

    async def set_alias(self, speaker_id: str, alias: str) -> None:
        """Assign an alias name to a speaker. Raises ValueError on collision."""
        backend = self._require_backend()
        # Check the alias doesn't collide with an existing speaker name
        speakers = await backend.list_speakers()
        for s in speakers:
            if s.name.lower() == alias.lower():
                raise ValueError(f"Alias '{alias}' collides with existing speaker name '{s.name}'")

        # Check alias doesn't collide with another alias
        storage = self._get_storage_backend()
        from gilbert.interfaces.storage import Filter, FilterOp, Query

        existing = await storage.query(
            Query(
                collection=_ALIAS_COLLECTION,
                filters=[Filter(field="alias", op=FilterOp.EQ, value=alias.lower())],
            )
        )
        if existing:
            existing_id = existing[0].get("speaker_id", "")
            if existing_id != speaker_id:
                raise ValueError(f"Alias '{alias}' is already assigned to speaker '{existing_id}'")

        await storage.put(
            _ALIAS_COLLECTION,
            f"{speaker_id}:{alias.lower()}",
            {
                "speaker_id": speaker_id,
                "alias": alias.lower(),
                "display_alias": alias,
            },
        )
        logger.info("Alias '%s' assigned to speaker %s", alias, speaker_id)

    async def remove_alias(self, alias: str) -> None:
        """Remove an alias."""
        storage = self._get_storage_backend()
        from gilbert.interfaces.storage import Filter, FilterOp, Query

        results = await storage.query(
            Query(
                collection=_ALIAS_COLLECTION,
                filters=[Filter(field="alias", op=FilterOp.EQ, value=alias.lower())],
            )
        )
        for r in results:
            await storage.delete(_ALIAS_COLLECTION, r["_id"])
        logger.info("Alias '%s' removed", alias)

    async def resolve_speaker_name(self, name: str) -> str | None:
        """Resolve a speaker name or alias to a speaker_id. Returns None if not found.

        Prefers an **exact case** match on the speaker's name, so
        distinct speakers named e.g. "Garage" and "GARAGE" resolve
        to distinct ids rather than collapsing to whichever appears
        first in the list. Falls back to a case-insensitive match
        only when no exact match exists and exactly one speaker
        matches case-insensitively — ambiguous case-insensitive
        matches raise to force the caller to use the exact casing.
        """
        backend = self._require_backend()
        speakers = await backend.list_speakers()

        # 1) Exact case-sensitive name match — wins unambiguously
        # even when other speakers share the lowercased spelling.
        for s in speakers:
            if s.name == name:
                return s.speaker_id

        # 2) Case-insensitive name match — but only if it's unique.
        ci_matches = [s for s in speakers if s.name.lower() == name.lower()]
        if len(ci_matches) == 1:
            return ci_matches[0].speaker_id
        if len(ci_matches) > 1:
            names = sorted(s.name for s in ci_matches)
            raise KeyError(
                f"Ambiguous speaker name {name!r} — matches {names!r} "
                f"case-insensitively. Use the exact casing."
            )

        # 3) Alias lookup — aliases are stored lowercased.
        storage = self._get_storage_backend()
        from gilbert.interfaces.storage import Filter, FilterOp, Query

        results = await storage.query(
            Query(
                collection=_ALIAS_COLLECTION,
                filters=[Filter(field="alias", op=FilterOp.EQ, value=name.lower())],
            )
        )
        if results:
            sid = results[0].get("speaker_id")
            return str(sid) if sid is not None else None

        return None

    async def resolve_speaker_names(self, names: list[str]) -> list[str]:
        """Resolve a list of speaker names/aliases to speaker_ids."""
        ids = []
        for name in names:
            sid = await self.resolve_speaker_name(name)
            if sid is None:
                raise KeyError(f"Unknown speaker or alias: {name!r}")
            ids.append(sid)
        return ids

    def _audio_url(self, file_path: str) -> str:
        """Build an HTTP URL for an output file so speakers can fetch it.

        Speakers need to access audio over HTTP — they can't read local files.
        We discover the LAN IP by connecting a UDP socket to an external address
        (no actual traffic is sent) which reveals the local interface IP.
        """
        from pathlib import Path

        from gilbert.core.output import OUTPUT_DIR

        # Resolve relative path under output dir
        rel = Path(file_path).relative_to(OUTPUT_DIR.resolve())
        host = self._web_host
        if host in ("0.0.0.0", "127.0.0.1", "localhost"):
            host = self._get_lan_ip()
        return f"http://{host}:{self._web_port}/output/{rel}"

    @staticmethod
    def _get_lan_ip() -> str:
        """Get the machine's LAN IP address."""
        import socket

        try:
            # Connect a UDP socket to a public address to discover the local interface.
            # No data is actually sent.
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                return str(s.getsockname()[0])
        except OSError:
            return "127.0.0.1"

    async def _resolve_target_ids(
        self,
        speaker_names: list[str] | None,
    ) -> list[str]:
        """Resolve speaker names to IDs with fallback logic.

        Explicit names → resolve to IDs and cache.
        None → use last-used speakers.
        Last-used empty → use all speakers.
        """
        if speaker_names:
            ids = await self.resolve_speaker_names(speaker_names)
            self._last_speaker_ids = list(ids)
            return ids
        if self._last_speaker_ids:
            return list(self._last_speaker_ids)
        # Fall back to all speakers
        backend = self._require_backend()
        speakers = await backend.list_speakers()
        return [s.speaker_id for s in speakers]

    async def prepare_speakers(self, speaker_ids: list[str]) -> None:
        """Ensure speakers are in the correct topology before playback.

        - Single speaker: unjoined from any group for solo playback.
        - Multiple speakers: grouped together.
        - Already correct: returns immediately.

        Backends that don't support grouping are skipped.
        """
        backend = self._require_backend()
        if not backend.supports_grouping or not speaker_ids:
            return

        if len(speaker_ids) == 1:
            await backend.ungroup_speakers(speaker_ids)
        else:
            await backend.group_speakers(speaker_ids)

    # --- Playback ---

    async def play_on_speakers(
        self,
        uri: str,
        speaker_names: list[str] | None = None,
        volume: int | None = None,
        title: str = "",
        position_seconds: float | None = None,
        didl_meta: str = "",
        announce: bool = False,
    ) -> None:
        """Play a URI on the specified speakers.

        Resolves names, prepares topology, then plays. ``didl_meta`` is
        an optional legacy DIDL-Lite envelope (unused by the aiosonos
        Sonos backend, still honoured by non-Sonos backends). When
        ``announce=True`` the speaker backend may route playback through
        a short-overlay path (e.g. Sonos ``audio_clip``) that ducks the
        current music, plays the clip, and auto-restores — no
        snapshot/restore ritual required.
        """
        target_ids = await self._resolve_target_ids(speaker_names)

        # Skip topology changes for announce clips — audio_clip is a
        # per-player overlay that doesn't need grouping, and reshuffling
        # the group mid-music can cause the speaker to lose its playback
        # queue, leaving the announcement URI as the "current" track
        # that loops after restore.
        if not announce:
            await self.prepare_speakers(target_ids)

        await self._require_backend().play_uri(
            PlayRequest(
                uri=uri,
                speaker_ids=target_ids,
                volume=volume,
                title=title,
                position_seconds=position_seconds,
                didl_meta=didl_meta,
                announce=announce,
            )
        )

    async def enqueue_on_speakers(
        self,
        uri: str,
        speaker_names: list[str] | None = None,
        title: str = "",
        didl_meta: str = "",
    ) -> None:
        """Append a URI to the speaker's queue without stopping playback.

        Routes to ``SpeakerBackend.enqueue_uri``. Raises
        ``NotImplementedError`` if the backend doesn't support queueing —
        callers should guard on the music service's ``supports_queue``
        flag before invoking this.
        """
        target_ids = await self._resolve_target_ids(speaker_names)
        await self.prepare_speakers(target_ids)
        await self._require_backend().enqueue_uri(
            PlayRequest(
                uri=uri,
                speaker_ids=target_ids,
                title=title,
                didl_meta=didl_meta,
            )
        )

    async def play_queue_on_speakers(
        self,
        speaker_names: list[str] | None = None,
    ) -> bool:
        """Start or resume queue playback on the specified speakers.

        Does NOT clear the queue or add new content — callers should
        have built the queue via prior ``enqueue_on_speakers`` calls (or
        an equivalent). Raises ``NotImplementedError`` if the backend
        doesn't support queue operations.

        When playback is already in progress this is a no-op and returns
        ``False`` — otherwise the SMAPI SetAVTransportURI that normally
        precedes the Play action would reset the queue back to track 1,
        losing the listener's position mid-song. Returns ``True`` when
        a Play was actually issued.
        """
        backend = self._require_backend()
        target_ids = await self._resolve_target_ids(speaker_names)
        await self.prepare_speakers(target_ids)

        # Check playback state on the coordinator (first target). All
        # group members share transport state, so any one is enough.
        if target_ids:
            try:
                state = await backend.get_playback_state(target_ids[0])
            except Exception:
                state = PlaybackState.STOPPED
            if state == PlaybackState.PLAYING:
                return False

        await backend.play_queue(target_ids)
        return True

    async def stop_speakers(
        self,
        speaker_names: list[str] | None = None,
    ) -> None:
        """Stop playback on the specified speakers."""
        target_ids = await self._resolve_target_ids(speaker_names)
        await self._require_backend().stop(target_ids)

    async def get_now_playing(
        self,
        speaker_name: str | None = None,
    ) -> NowPlaying:
        """Return what's currently playing on a speaker.

        Speaker selection falls through in this order:

        1. If ``speaker_name`` is given, that speaker (resolved by name/alias).
        2. The first of the last-used speakers (typically the one music was
           last played on).
        3. The first speaker found whose state is ``PLAYING``.
        4. The first discovered speaker, regardless of state.

        Returns a ``NowPlaying`` with ``state=STOPPED`` if no speakers exist.
        """
        backend = self._require_backend()
        if speaker_name:
            sid = await self.resolve_speaker_name(speaker_name)
            if sid is None:
                raise KeyError(f"Unknown speaker or alias: {speaker_name!r}")
            return await backend.get_now_playing(sid)

        if self._last_speaker_ids:
            return await backend.get_now_playing(self._last_speaker_ids[0])

        speakers = await backend.list_speakers()
        if not speakers:
            return NowPlaying(state=PlaybackState.STOPPED)
        for s in speakers:
            if s.state == PlaybackState.PLAYING:
                return await backend.get_now_playing(s.speaker_id)
        return await backend.get_now_playing(speakers[0].speaker_id)

    # --- Announce ---

    async def announce(
        self,
        text: str,
        speaker_names: list[str] | None = None,
        volume: int | None = None,
    ) -> str:
        """Announce text over speakers using TTS.

        If no speaker_names are given, falls back to the configured
        default announce speakers (or all speakers if that's also empty).

        Announcements are serialized **per speaker**, not globally —
        two concurrent announcements that target disjoint speaker sets
        fan out and run in parallel, while any overlap on a shared
        speaker queues on that speaker's lock. This lets the assistant
        say different things on different speakers at the same time
        (e.g. personal greetings per room) without letting two clips
        collide on the same device's snapshot/restore state.
        """
        # Fall back to configured default speakers
        if speaker_names is None and self._default_announce_speakers:
            speaker_names = self._default_announce_speakers
        # Resolve target speakers outside any per-speaker lock so the
        # lock set is known before we start acquiring. An empty set
        # means no speakers to announce on — let _announce_inner handle
        # the degenerate case without acquiring any locks.
        target_ids = await self._resolve_target_ids(speaker_names)
        if not target_ids:
            return await self._announce_inner(
                text, speaker_names, volume, target_ids=target_ids
            )
        locks = await self._get_speaker_locks(target_ids)
        async with contextlib.AsyncExitStack() as stack:
            for lock in locks:
                await stack.enter_async_context(lock)
            return await self._announce_inner(
                text, speaker_names, volume, target_ids=target_ids
            )

    async def _get_speaker_locks(
        self, speaker_ids: list[str]
    ) -> list[asyncio.Lock]:
        """Return one lock per unique speaker ID, ordered by ID.

        Sorted-order acquisition is the standard fix for the
        multi-lock deadlock: if caller A asks for [s1, s2] and caller
        B asks for [s2, s3], both acquire them in the same global
        order so neither can end up holding a lock the other needs in
        reverse. Locks are created lazily under ``_speaker_locks_guard``
        so the get-or-create step itself is race-free.
        """
        unique_ids = sorted(set(speaker_ids))
        async with self._speaker_locks_guard:
            for sid in unique_ids:
                if sid not in self._speaker_locks:
                    self._speaker_locks[sid] = asyncio.Lock()
        return [self._speaker_locks[sid] for sid in unique_ids]

    async def _announce_inner(
        self,
        text: str,
        speaker_names: list[str] | None = None,
        volume: int | None = None,
        target_ids: list[str] | None = None,
    ) -> str:
        """Inner announce — must be called with the target speakers'
        per-speaker locks already held. Accepts a pre-resolved
        ``target_ids`` to avoid re-running name resolution (and the
        ``_last_speaker_ids`` mutation that goes with it) after the
        caller has already done so to build the lock set.
        """
        if self._tts_svc is None:
            raise RuntimeError("TTS service is not available — cannot announce")

        from gilbert.interfaces.tts import AudioFormat, SynthesisRequest, TTSProvider

        if not isinstance(self._tts_svc, TTSProvider):
            raise TypeError("Expected TTSService for text_to_speech capability")

        backend = self._require_backend()

        # Generate TTS audio
        request = SynthesisRequest(text=text, voice_id="", output_format=AudioFormat.MP3)
        result = await self._tts_svc.synthesize(request)

        # Save to a file so the speaker can access it via URI
        output_dir = get_output_dir("speaker")
        cleanup_old_files(output_dir, self._output_ttl_seconds)
        file_path = output_dir / f"announce-{uuid.uuid4()}.mp3"
        file_path.write_bytes(result.audio)

        # Determine volume
        effective_volume = volume or self._default_announce_volume

        # Snapshot current playback state so we can resume after —
        # on the aiosonos Sonos backend snapshot/restore are no-ops
        # because ``audio_clip`` (triggered by ``announce=True`` below)
        # ducks + auto-restores natively. Kept in the flow so non-
        # Sonos backends that implement snapshot/restore still work.
        if target_ids is None:
            target_ids = await self._resolve_target_ids(speaker_names)
        await backend.snapshot(target_ids)

        # Play on speakers — topology handled by play_on_speakers.
        # ``announce=True`` tells backends that support it (Sonos) to
        # route through a short-overlay clip path so the listener
        # doesn't hear music stop completely, and the previous track
        # resumes automatically when the clip ends.
        audio_url = self._audio_url(str(file_path.resolve()))
        await self.play_on_speakers(
            uri=audio_url,
            speaker_names=speaker_names,
            volume=effective_volume,
            title=f"Announcement: {text[:50]}",
            announce=True,
        )

        # Wait for playback to finish before restoring.
        # Use audio duration if available, fall back to polling.
        # On audio_clip-capable backends this is just a courtesy wait
        # before releasing the announce lock — the speaker has already
        # scheduled its own restore once the clip ends.
        duration = self._estimate_mp3_duration(result.audio)
        if duration > 0:
            await asyncio.sleep(duration + 0.5)
        else:
            await self._wait_for_playback(target_ids)

        # Restore previous playback state (no-op on aiosonos; kept for
        # non-Sonos backends that still need the manual restore).
        try:
            await backend.restore(target_ids)
        except Exception:
            logger.debug("Failed to restore playback after announcement")

        return str(file_path)

    @staticmethod
    def _estimate_mp3_duration(audio_data: bytes) -> float:
        """Estimate MP3 duration from file size and bitrate.

        Parses the first MP3 frame header to get the bitrate, then
        calculates duration = size / (bitrate / 8). Returns 0 on failure.
        """
        try:
            # Find first MP3 frame sync (0xFF 0xFB/0xFA/0xF3/0xF2)
            for i in range(min(len(audio_data) - 1, 4096)):
                if audio_data[i] == 0xFF and (audio_data[i + 1] & 0xE0) == 0xE0:
                    header = audio_data[i : i + 4]
                    if len(header) < 4:
                        return 0
                    # MPEG version, layer, bitrate index
                    version = (header[1] >> 3) & 0x03
                    layer = (header[1] >> 1) & 0x03
                    br_idx = (header[2] >> 4) & 0x0F
                    # MPEG1 Layer3 bitrate table
                    if version == 3 and layer == 1 and 1 <= br_idx <= 14:
                        bitrates = [
                            0,
                            32,
                            40,
                            48,
                            56,
                            64,
                            80,
                            96,
                            112,
                            128,
                            160,
                            192,
                            224,
                            256,
                            320,
                        ]
                        kbps = bitrates[br_idx]
                        return len(audio_data) / (kbps * 125)
            return 0
        except Exception:
            return 0

    async def _wait_for_playback(
        self,
        speaker_ids: list[str],
        timeout: float = 60.0,
        poll_interval: float = 0.5,
    ) -> None:
        """Poll speaker state until playback finishes or times out."""
        from gilbert.interfaces.speaker import PlaybackState

        if not speaker_ids:
            return

        # Use the first speaker (coordinator) to check state
        target_id = speaker_ids[0]
        elapsed = 0.0

        # Wait briefly for playback to start (TRANSITIONING → PLAYING)
        await asyncio.sleep(0.5)
        elapsed += 0.5

        while elapsed < timeout:
            try:
                state = await self._require_backend().get_playback_state(target_id)
                if state not in (PlaybackState.PLAYING, PlaybackState.TRANSITIONING):
                    return
            except Exception:
                return  # Can't check state — don't block forever

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

    # --- ToolProvider protocol ---

    @property
    def tool_provider_name(self) -> str:
        return "speaker"

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        tools = [
            ToolDefinition(
                name="list_speakers",
                slash_group="speaker",
                slash_command="list",
                slash_help="List all speakers with state + volume: /speaker list",
                description="List all discovered speakers with their current state, volume, and group info.",
                required_role="everyone",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="play_audio",
                slash_group="speaker",
                slash_command="play",
                slash_help="Play a URI on speakers: /speaker play <uri> [speakers] [volume]",
                description=(
                    "Play audio from an HTTP(S) URI on one or more speakers. "
                    "The speaker fetches the bytes over the network — it CANNOT "
                    "read local file paths or workspace-relative paths. "
                    "If you want to play a workspace file (something in "
                    "``uploads/``, ``outputs/``, or ``scratch/``), first call "
                    "``share_workspace_file`` to mint an HTTP URL for it, then "
                    "pass that URL's ``url`` field here as ``uri``. Passing a "
                    "raw path like ``uploads/song.mp3`` will fail."
                ),
                parameters=[
                    ToolParameter(
                        name="uri",
                        type=ToolParameterType.STRING,
                        description=(
                            "HTTP(S) URL of the audio to play. For workspace "
                            "files, get a URL from ``share_workspace_file`` "
                            "first — local paths will not work."
                        ),
                    ),
                    ToolParameter(
                        name="speakers",
                        type=ToolParameterType.ARRAY,
                        description="Speaker names or aliases. If omitted, uses last-used speakers or all.",
                        required=False,
                    ),
                    ToolParameter(
                        name="volume",
                        type=ToolParameterType.INTEGER,
                        description="Volume level (0-100). If omitted, uses current volume.",
                        required=False,
                    ),
                    ToolParameter(
                        name="position_seconds",
                        type=ToolParameterType.NUMBER,
                        description="Start playback at this position in seconds.",
                        required=False,
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="stop_audio",
                slash_group="speaker",
                slash_command="stop",
                slash_help="Stop playback: /speaker stop [speakers]",
                description="Stop playback on speakers.",
                parameters=[
                    ToolParameter(
                        name="speakers",
                        type=ToolParameterType.ARRAY,
                        description="Speaker names or aliases. If omitted, stops all.",
                        required=False,
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="set_volume",
                slash_group="speaker",
                slash_command="volume",
                slash_help="Set speaker volume: /speaker volume <speaker> <0-100>",
                description="Set volume on a speaker.",
                parameters=[
                    ToolParameter(
                        name="speaker",
                        type=ToolParameterType.STRING,
                        description="Speaker name or alias.",
                    ),
                    ToolParameter(
                        name="volume",
                        type=ToolParameterType.INTEGER,
                        description="Volume level (0-100).",
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="get_volume",
                slash_group="speaker",
                slash_command="get_volume",
                slash_help="Read speaker volume: /speaker get_volume <speaker>",
                description="Get the current volume of a speaker.",
                parameters=[
                    ToolParameter(
                        name="speaker",
                        type=ToolParameterType.STRING,
                        description="Speaker name or alias.",
                    ),
                ],
                required_role="everyone",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="set_speaker_alias",
                slash_group="speaker",
                slash_command="alias",
                slash_help="Alias a speaker: /speaker alias <speaker> <alias>",
                description="Assign an alias name to a speaker (e.g., 'Living Room Speaker' for 'Speaker 2'). Admin only.",
                required_role="admin",
                parameters=[
                    ToolParameter(
                        name="speaker",
                        type=ToolParameterType.STRING,
                        description="Current speaker name or ID.",
                    ),
                    ToolParameter(
                        name="alias",
                        type=ToolParameterType.STRING,
                        description="The alias name to assign.",
                    ),
                ],
            ),
            ToolDefinition(
                name="remove_speaker_alias",
                slash_group="speaker",
                slash_command="unalias",
                slash_help="Remove a speaker alias: /speaker unalias <alias>",
                description="Remove an alias from a speaker. Admin only.",
                required_role="admin",
                parameters=[
                    ToolParameter(
                        name="alias",
                        type=ToolParameterType.STRING,
                        description="The alias to remove.",
                    ),
                ],
            ),
            ToolDefinition(
                name="announce",
                slash_group="speaker",
                slash_command="announce",
                slash_help=(
                    'Speak text on speakers via TTS: /speaker announce "<text>" [speakers] [volume]'
                ),
                description=(
                    "Announce a message over speakers using text-to-speech. "
                    "This is the primary tool for speaking text out loud — it handles everything: "
                    "generates audio via TTS, groups speakers if needed, sets volume, and plays. "
                    "If no speakers specified, uses last-used speakers or all. "
                    "Use this instead of 'speak' when you want audio played on speakers."
                ),
                parameters=[
                    ToolParameter(
                        name="text",
                        type=ToolParameterType.STRING,
                        description="The text to announce.",
                    ),
                    ToolParameter(
                        name="speakers",
                        type=ToolParameterType.ARRAY,
                        description="Speaker names or aliases. If omitted, uses last-used speakers or all.",
                        required=False,
                    ),
                    ToolParameter(
                        name="volume",
                        type=ToolParameterType.INTEGER,
                        description="Volume level (0-100) for the announcement.",
                        required=False,
                    ),
                ],
                required_role="user",
                # Safe to run in parallel with other announces targeting
                # *different* speakers — per-speaker locks in the service
                # still serialize same-speaker collisions so clips never
                # overlap on one device.
                parallel_safe=True,
            ),
        ]

        # Add grouping tools if the backend supports it
        if self._backend is not None and self._backend.supports_grouping:
            tools.extend(
                [
                    ToolDefinition(
                        name="list_speaker_groups",
                        slash_group="speaker",
                        slash_command="groups",
                        slash_help="List speaker groups: /speaker groups",
                        description="List current speaker groups.",
                        required_role="user",
                    ),
                    ToolDefinition(
                        name="group_speakers",
                        slash_group="speaker",
                        slash_command="group",
                        slash_help="Group speakers for sync playback: /speaker group <s1>,<s2>",
                        description="Group speakers together for synchronized playback.",
                        parameters=[
                            ToolParameter(
                                name="speakers",
                                type=ToolParameterType.ARRAY,
                                description="Speaker names or aliases to group together (at least 2).",
                            ),
                        ],
                        required_role="user",
                    ),
                    ToolDefinition(
                        name="ungroup_speakers",
                        slash_group="speaker",
                        slash_command="ungroup",
                        slash_help="Remove speakers from groups: /speaker ungroup <s1>,<s2>",
                        description="Remove speakers from their groups.",
                        parameters=[
                            ToolParameter(
                                name="speakers",
                                type=ToolParameterType.ARRAY,
                                description="Speaker names or aliases to ungroup.",
                            ),
                        ],
                        required_role="user",
                    ),
                ]
            )

        return tools

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        match name:
            case "list_speakers":
                return await self._tool_list_speakers()
            case "play_audio":
                return await self._tool_play_audio(arguments)
            case "stop_audio":
                return await self._tool_stop_audio(arguments)
            case "set_volume":
                return await self._tool_set_volume(arguments)
            case "get_volume":
                return await self._tool_get_volume(arguments)
            case "set_speaker_alias":
                return await self._tool_set_alias(arguments)
            case "remove_speaker_alias":
                return await self._tool_remove_alias(arguments)
            case "announce":
                return await self._tool_announce(arguments)
            case "list_speaker_groups":
                return await self._tool_list_groups()
            case "group_speakers":
                return await self._tool_group_speakers(arguments)
            case "ungroup_speakers":
                return await self._tool_ungroup_speakers(arguments)
            case _:
                raise KeyError(f"Unknown tool: {name}")

    async def _tool_list_speakers(self) -> str:
        speakers = await self._require_backend().list_speakers()

        # Enrich with aliases
        storage = self._get_storage_backend()
        from gilbert.interfaces.storage import Query

        all_aliases = await storage.query(Query(collection=_ALIAS_COLLECTION))
        alias_map: dict[str, list[str]] = {}
        for a in all_aliases:
            sid = a.get("speaker_id", "")
            alias_map.setdefault(sid, []).append(a.get("display_alias", ""))

        result = []
        for s in speakers:
            entry: dict[str, Any] = {
                "speaker_id": s.speaker_id,
                "name": s.name,
                "ip_address": s.ip_address,
                "model": s.model,
                "volume": s.volume,
                "state": s.state.value,
                "group_name": s.group_name,
                "is_group_coordinator": s.is_group_coordinator,
            }
            aliases = alias_map.get(s.speaker_id)
            if aliases:
                entry["aliases"] = aliases
            result.append(entry)

        return json.dumps(result)

    async def _tool_play_audio(self, arguments: dict[str, Any]) -> str:
        uri = arguments["uri"]
        speaker_names: list[str] = arguments.get("speakers", [])
        volume: int | None = arguments.get("volume")
        position: float | None = arguments.get("position_seconds")

        await self.play_on_speakers(
            uri=uri,
            speaker_names=speaker_names or None,
            volume=volume,
            position_seconds=position,
        )
        return json.dumps({"status": "playing", "uri": uri})

    async def _tool_stop_audio(self, arguments: dict[str, Any]) -> str:
        speaker_names: list[str] = arguments.get("speakers", [])
        await self.stop_speakers(speaker_names or None)
        return json.dumps({"status": "stopped"})

    async def _tool_set_volume(self, arguments: dict[str, Any]) -> str:
        name = arguments["speaker"]
        volume = arguments["volume"]
        sid = await self.resolve_speaker_name(name)
        if sid is None:
            return json.dumps({"error": f"Speaker not found: {name}"})
        await self._require_backend().set_volume(sid, volume)
        return json.dumps({"status": "ok", "speaker": name, "volume": volume})

    async def _tool_get_volume(self, arguments: dict[str, Any]) -> str:
        name = arguments["speaker"]
        sid = await self.resolve_speaker_name(name)
        if sid is None:
            return json.dumps({"error": f"Speaker not found: {name}"})
        volume = await self._require_backend().get_volume(sid)
        return json.dumps({"speaker": name, "volume": volume})

    async def _tool_set_alias(self, arguments: dict[str, Any]) -> str:
        speaker_name = arguments["speaker"]
        alias = arguments["alias"]

        sid = await self.resolve_speaker_name(speaker_name)
        if sid is None:
            return json.dumps({"error": f"Speaker not found: {speaker_name}"})

        try:
            await self.set_alias(sid, alias)
        except ValueError as e:
            return json.dumps({"error": str(e)})

        return json.dumps({"status": "ok", "speaker": speaker_name, "alias": alias})

    async def _tool_remove_alias(self, arguments: dict[str, Any]) -> str:
        alias = arguments["alias"]
        await self.remove_alias(alias)
        return json.dumps({"status": "ok", "alias": alias})

    async def _tool_announce(self, arguments: dict[str, Any]) -> str:
        text = arguments["text"]
        speaker_names: list[str] = arguments.get("speakers", [])
        volume: int | None = arguments.get("volume")

        try:
            file_path = await self.announce(
                text=text,
                speaker_names=speaker_names or None,
                volume=volume,
            )
        except RuntimeError as e:
            return json.dumps({"error": str(e)})

        return json.dumps(
            {
                "status": "announced",
                "text": text,
                "audio_file": file_path,
            }
        )

    async def _tool_list_groups(self) -> str:
        groups = await self._require_backend().list_groups()
        return json.dumps(
            [
                {
                    "group_id": g.group_id,
                    "name": g.name,
                    "coordinator_id": g.coordinator_id,
                    "member_ids": g.member_ids,
                }
                for g in groups
            ]
        )

    async def _tool_group_speakers(self, arguments: dict[str, Any]) -> str:
        speaker_names: list[str] = arguments["speakers"]
        speaker_ids = await self.resolve_speaker_names(speaker_names)

        try:
            group = await self._require_backend().group_speakers(speaker_ids)
        except ValueError as e:
            return json.dumps({"error": str(e)})

        return json.dumps(
            {
                "status": "grouped",
                "group_id": group.group_id,
                "name": group.name,
                "member_ids": group.member_ids,
            }
        )

    async def _tool_ungroup_speakers(self, arguments: dict[str, Any]) -> str:
        speaker_names: list[str] = arguments["speakers"]
        speaker_ids = await self.resolve_speaker_names(speaker_names)
        await self._require_backend().ungroup_speakers(speaker_ids)
        return json.dumps({"status": "ungrouped"})
