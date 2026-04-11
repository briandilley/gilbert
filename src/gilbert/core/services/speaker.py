"""Speaker service — wraps a SpeakerBackend as a discoverable service with announce support."""

import asyncio
import json
import logging
import uuid
from typing import Any

from gilbert.core.output import cleanup_old_files, get_output_dir
from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.speaker import (
    PlayRequest,
    SpeakerBackend,
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
        # Announcement queue lock — prevents announcements from stepping
        # on each other by serializing TTS + playback.
        self._announce_lock = asyncio.Lock()
        self._speaker_cache: list[Any] = []

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
    def cached_speakers(self) -> list[Any]:
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
            from gilbert.core.services.configuration import ConfigurationService

            if isinstance(config_svc, ConfigurationService):
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
        try:
            import gilbert.integrations.sonos_speaker  # noqa: F401
        except ImportError:
            pass
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
        await storage.ensure_index(IndexDefinition(
            collection=_ALIAS_COLLECTION,
            fields=["alias"],
            unique=True,
        ))

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
        from gilbert.core.services.storage import StorageService

        if isinstance(self._storage_svc, StorageService):
            return self._storage_svc.backend
        raise TypeError("Expected StorageService for entity_storage")

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

        # Import known backends so they register before we query the registry
        try:
            import gilbert.integrations.sonos_speaker  # noqa: F401
        except ImportError:
            pass

        params = [
            ConfigParam(
                key="backend", type=ToolParameterType.STRING,
                description="Speaker backend type.",
                default="sonos", restart_required=True,
                choices=tuple(SpeakerBackend.registered_backends().keys()) or ("sonos",),
            ),
            ConfigParam(
                key="default_announce_volume", type=ToolParameterType.INTEGER,
                description="Default volume level for announcements (0-100). Unset means use current volume.",
            ),
            ConfigParam(
                key="default_announce_speakers", type=ToolParameterType.ARRAY,
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
            params.append(ConfigParam(
                key=f"settings.{bp.key}", type=bp.type,
                description=bp.description, default=bp.default,
                restart_required=bp.restart_required, sensitive=bp.sensitive,
                choices=bp.choices, multiline=bp.multiline, backend_param=True,
            ))
        return params

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._apply_config(config)

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
                raise ValueError(
                    f"Alias '{alias}' collides with existing speaker name '{s.name}'"
                )

        # Check alias doesn't collide with another alias
        storage = self._get_storage_backend()
        from gilbert.interfaces.storage import Filter, FilterOp, Query

        existing = await storage.query(Query(
            collection=_ALIAS_COLLECTION,
            filters=[Filter(field="alias", op=FilterOp.EQ, value=alias.lower())],
        ))
        if existing:
            existing_id = existing[0].get("speaker_id", "")
            if existing_id != speaker_id:
                raise ValueError(
                    f"Alias '{alias}' is already assigned to speaker '{existing_id}'"
                )

        await storage.put(_ALIAS_COLLECTION, f"{speaker_id}:{alias.lower()}", {
            "speaker_id": speaker_id,
            "alias": alias.lower(),
            "display_alias": alias,
        })
        logger.info("Alias '%s' assigned to speaker %s", alias, speaker_id)

    async def remove_alias(self, alias: str) -> None:
        """Remove an alias."""
        storage = self._get_storage_backend()
        from gilbert.interfaces.storage import Filter, FilterOp, Query

        results = await storage.query(Query(
            collection=_ALIAS_COLLECTION,
            filters=[Filter(field="alias", op=FilterOp.EQ, value=alias.lower())],
        ))
        for r in results:
            await storage.delete(_ALIAS_COLLECTION, r["_id"])
        logger.info("Alias '%s' removed", alias)

    async def resolve_speaker_name(self, name: str) -> str | None:
        """Resolve a speaker name or alias to a speaker_id. Returns None if not found."""
        backend = self._require_backend()
        # Try direct match by speaker name
        speakers = await backend.list_speakers()
        for s in speakers:
            if s.name.lower() == name.lower():
                return s.speaker_id

        # Try alias lookup
        storage = self._get_storage_backend()
        from gilbert.interfaces.storage import Filter, FilterOp, Query

        results = await storage.query(Query(
            collection=_ALIAS_COLLECTION,
            filters=[Filter(field="alias", op=FilterOp.EQ, value=name.lower())],
        ))
        if results:
            return results[0].get("speaker_id")

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
        import socket
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
                return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"

    async def _resolve_target_ids(
        self, speaker_names: list[str] | None,
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
    ) -> None:
        """Play a URI on the specified speakers.

        Resolves names, prepares topology, then plays.
        """
        target_ids = await self._resolve_target_ids(speaker_names)
        await self.prepare_speakers(target_ids)

        await self._require_backend().play_uri(PlayRequest(
            uri=uri,
            speaker_ids=target_ids,
            volume=volume,
            title=title,
            position_seconds=position_seconds,
        ))

    async def stop_speakers(
        self,
        speaker_names: list[str] | None = None,
    ) -> None:
        """Stop playback on the specified speakers."""
        target_ids = await self._resolve_target_ids(speaker_names)
        await self._require_backend().stop(target_ids)

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

        Announcements are serialized via a lock so they don't step on
        each other. After starting playback, waits for the estimated
        audio duration before releasing the lock.
        """
        # Fall back to configured default speakers
        if speaker_names is None and self._default_announce_speakers:
            speaker_names = self._default_announce_speakers
        async with self._announce_lock:
            return await self._announce_inner(text, speaker_names, volume)

    async def _announce_inner(
        self,
        text: str,
        speaker_names: list[str] | None = None,
        volume: int | None = None,
    ) -> str:
        """Inner announce — must be called under _announce_lock."""
        if self._tts_svc is None:
            raise RuntimeError("TTS service is not available — cannot announce")

        from gilbert.core.services.tts import TTSService
        from gilbert.interfaces.tts import AudioFormat, SynthesisRequest

        if not isinstance(self._tts_svc, TTSService):
            raise TypeError("Expected TTSService for text_to_speech capability")

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

        # Play on speakers — topology handled by play_on_speakers
        audio_url = self._audio_url(str(file_path.resolve()))
        await self.play_on_speakers(
            uri=audio_url,
            speaker_names=speaker_names,
            volume=effective_volume,
            title=f"Announcement: {text[:50]}",
        )

        # Wait for playback to finish before releasing the lock.
        # Use audio duration if available, fall back to polling.
        target_ids = await self._resolve_target_ids(speaker_names)
        duration = self._estimate_mp3_duration(result.audio)
        if duration > 0:
            await asyncio.sleep(duration + 0.5)
        else:
            await self._wait_for_playback(target_ids)

        # Clear the queue so the announcement doesn't linger in history
        try:
            await self._require_backend().clear_queue(target_ids)
        except Exception:
            logger.debug("Failed to clear queue after announcement")

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
                    header = audio_data[i:i + 4]
                    if len(header) < 4:
                        return 0
                    # MPEG version, layer, bitrate index
                    version = (header[1] >> 3) & 0x03
                    layer = (header[1] >> 1) & 0x03
                    br_idx = (header[2] >> 4) & 0x0F
                    # MPEG1 Layer3 bitrate table
                    if version == 3 and layer == 1 and 1 <= br_idx <= 14:
                        bitrates = [
                            0, 32, 40, 48, 56, 64, 80, 96,
                            112, 128, 160, 192, 224, 256, 320,
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

    def get_tools(self) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        tools = [
            ToolDefinition(
                name="list_speakers",
                description="List all discovered speakers with their current state, volume, and group info.",
                required_role="everyone",
            ),
            ToolDefinition(
                name="play_audio",
                description="Play audio from a URI on one or more speakers.",
                parameters=[
                    ToolParameter(
                        name="uri",
                        type=ToolParameterType.STRING,
                        description="URI of the audio to play.",
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
            ),
            ToolDefinition(
                name="stop_audio",
                description="Stop playback on speakers.",
                parameters=[
                    ToolParameter(
                        name="speakers",
                        type=ToolParameterType.ARRAY,
                        description="Speaker names or aliases. If omitted, stops all.",
                        required=False,
                    ),
                ],
            ),
            ToolDefinition(
                name="set_volume",
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
            ),
            ToolDefinition(
                name="get_volume",
                description="Get the current volume of a speaker.",
                parameters=[
                    ToolParameter(
                        name="speaker",
                        type=ToolParameterType.STRING,
                        description="Speaker name or alias.",
                    ),
                ],
                required_role="everyone",
            ),
            ToolDefinition(
                name="set_speaker_alias",
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
            ),
        ]

        # Add grouping tools if the backend supports it
        if self._backend is not None and self._backend.supports_grouping:
            tools.extend([
                ToolDefinition(
                    name="list_speaker_groups",
                    description="List current speaker groups.",
                ),
                ToolDefinition(
                    name="group_speakers",
                    description="Group speakers together for synchronized playback.",
                    parameters=[
                        ToolParameter(
                            name="speakers",
                            type=ToolParameterType.ARRAY,
                            description="Speaker names or aliases to group together (at least 2).",
                        ),
                    ],
                ),
                ToolDefinition(
                    name="ungroup_speakers",
                    description="Remove speakers from their groups.",
                    parameters=[
                        ToolParameter(
                            name="speakers",
                            type=ToolParameterType.ARRAY,
                            description="Speaker names or aliases to ungroup.",
                        ),
                    ],
                ),
            ])

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

        return json.dumps({
            "status": "announced",
            "text": text,
            "audio_file": file_path,
        })

    async def _tool_list_groups(self) -> str:
        groups = await self._require_backend().list_groups()
        return json.dumps([
            {
                "group_id": g.group_id,
                "name": g.name,
                "coordinator_id": g.coordinator_id,
                "member_ids": g.member_ids,
            }
            for g in groups
        ])

    async def _tool_group_speakers(self, arguments: dict[str, Any]) -> str:
        speaker_names: list[str] = arguments["speakers"]
        speaker_ids = await self.resolve_speaker_names(speaker_names)

        try:
            group = await self._require_backend().group_speakers(speaker_ids)
        except ValueError as e:
            return json.dumps({"error": str(e)})

        return json.dumps({
            "status": "grouped",
            "group_id": group.group_id,
            "name": group.name,
            "member_ids": group.member_ids,
        })

    async def _tool_ungroup_speakers(self, arguments: dict[str, Any]) -> str:
        speaker_names: list[str] = arguments["speakers"]
        speaker_ids = await self.resolve_speaker_names(speaker_names)
        await self._require_backend().ungroup_speakers(speaker_ids)
        return json.dumps({"status": "ungrouped"})
