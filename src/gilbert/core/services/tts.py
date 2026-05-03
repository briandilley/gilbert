"""TTS service — wraps a TTSBackend as a discoverable service.

Adds backend-agnostic silence padding to synthesized audio so speakers
don't cut off the last word.
"""

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
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)
from gilbert.interfaces.tts import (
    AudioFormat,
    SynthesisRequest,
    SynthesisResult,
    TTSBackend,
    Voice,
    append_silence,
)

logger = logging.getLogger(__name__)


class TTSService(Service):
    """Exposes a TTSBackend as a service with text_to_speech capability."""

    def __init__(self) -> None:
        self._backend: TTSBackend | None = None
        self._backend_name: str = "elevenlabs"
        self._enabled: bool = False
        self._config: dict[str, object] = {}
        self._silence_padding: float = 3.0
        self._output_ttl_seconds: int = 3600
        self._resolver: ServiceResolver | None = None
        # ``ai_chat`` is an optional capability, but the service manager
        # doesn't honor optional deps for start order — so the AI service
        # may not be running yet when TTS starts and the at-start
        # injection silently misses. We retry on every synthesize until
        # it sticks (or the backend signals it doesn't care).
        self._ai_injected: bool = False

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="tts",
            capabilities=frozenset({"text_to_speech", "ai_tools"}),
            optional=frozenset({"configuration", "ai_chat"}),
            toggleable=True,
            toggle_description="Text-to-speech synthesis",
        )

    @property
    def backend(self) -> TTSBackend | None:
        return self._backend

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver
        config_svc = resolver.get_capability("configuration")
        section: dict[str, Any] = {}
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                section = config_svc.get_section(self.config_namespace)
                global_ttl = config_svc.get("output_ttl_seconds")
                if global_ttl is not None:
                    self._output_ttl_seconds = int(global_ttl)

        if not section.get("enabled", False):
            logger.info("TTS service disabled")
            return

        self._enabled = True

        self._config = section.get("settings", self._config)
        sp = section.get("silence_padding")
        if sp is not None:
            self._silence_padding = float(sp)

        backend_name = section.get("backend", "elevenlabs")
        self._backend_name = backend_name
        backends = TTSBackend.registered_backends()
        backend_cls = backends.get(backend_name)
        if backend_cls is None:
            raise ValueError(f"Unknown TTS backend: {backend_name}")
        self._backend = backend_cls()

        await self._backend.initialize(self._config)

        # Hand the backend an AI sampling provider if it wants one
        # (currently used by ElevenLabs to inject v3 audio tags via a
        # small model). May miss if the AI service hasn't started yet —
        # ``_ensure_ai_injection`` retries on each ``synthesize`` until
        # the provider becomes available.
        self._ensure_ai_injection()

        logger.info("TTS service started")

    def _ensure_ai_injection(self) -> None:
        """Lazily wire an AISamplingProvider into the backend.

        Idempotent. Retries each call until either (a) the backend
        doesn't satisfy ``AICapableTTSBackend`` (nothing to do, mark
        done) or (b) the AI service is up and we successfully inject.
        Once done, becomes a no-op.
        """
        if self._ai_injected:
            return
        if self._backend is None or self._resolver is None:
            return

        from gilbert.interfaces.ai import AISamplingProvider
        from gilbert.interfaces.tts import AICapableTTSBackend

        if not isinstance(self._backend, AICapableTTSBackend):
            self._ai_injected = True
            return

        ai_svc = self._resolver.get_capability("ai_chat")
        if isinstance(ai_svc, AISamplingProvider):
            self._backend.set_ai_sampling(ai_svc)
            self._ai_injected = True
            logger.info("TTS backend wired up with AI sampling provider")

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "tts"

    @property
    def config_category(self) -> str:
        return "Media"

    def config_params(self) -> list[ConfigParam]:
        params = [
            ConfigParam(
                key="silence_padding",
                type=ToolParameterType.NUMBER,
                description="Seconds of silence appended after synthesized audio.",
                default=3.0,
            ),
            ConfigParam(
                key="backend",
                type=ToolParameterType.STRING,
                description="TTS backend provider.",
                default="elevenlabs",
                restart_required=True,
                choices=tuple(TTSBackend.registered_backends().keys()),
            ),
        ]
        backends = TTSBackend.registered_backends()
        backend_cls = backends.get(self._backend_name)
        if backend_cls is not None:
            for bp in backend_cls.backend_config_params():
                params.append(
                    ConfigParam(
                        key=f"settings.{bp.key}",
                        type=bp.type,
                        description=bp.description,
                        default=bp.default,
                        restart_required=bp.restart_required,
                        sensitive=bp.sensitive,
                        choices=bp.choices,
                        choices_from=bp.choices_from,
                        multiline=bp.multiline,
                        ai_prompt=bp.ai_prompt,
                        backend_param=True,
                    )
                )
        return params

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._config = config.get("settings", self._config)
        sp = config.get("silence_padding")
        if sp is not None:
            self._silence_padding = float(sp)

    # --- ConfigActionProvider ---

    def config_actions(self) -> list[ConfigAction]:
        return all_backend_actions(
            registry=TTSBackend.registered_backends(),
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

    async def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        """Synthesize speech from text. Appends silence padding if configured."""
        if self._backend is None:
            raise RuntimeError("TTS service is not enabled")
        self._ensure_ai_injection()
        result = await self._backend.synthesize(request)
        if self._silence_padding > 0:
            padded = append_silence(result.audio, result.format, self._silence_padding)
            return SynthesisResult(
                audio=padded,
                format=result.format,
                duration_seconds=result.duration_seconds,
                characters_used=result.characters_used,
            )
        return result

    async def list_voices(self) -> list[Voice]:
        """List available voices from the backend."""
        if self._backend is None:
            raise RuntimeError("TTS service is not enabled")
        return await self._backend.list_voices()

    # --- ToolProvider protocol ---

    @property
    def tool_provider_name(self) -> str:
        return "tts"

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        return [
            ToolDefinition(
                name="synthesize",
                slash_group="tts",
                slash_command="synthesize",
                slash_help=(
                    "Synthesize speech to an MP3 file (does NOT play on "
                    "speakers — use /speaker announce for that): "
                    '/tts synthesize "<text>"'
                ),
                description=(
                    "Synthesize speech from text and save as an MP3 file. "
                    "This only generates an audio file — it does NOT play it on speakers. "
                    "To speak text out loud on speakers, use the 'announce' tool instead."
                ),
                parameters=[
                    ToolParameter(
                        name="text",
                        type=ToolParameterType.STRING,
                        description="The text to speak.",
                    ),
                ],
                required_role="everyone",
                # Each call writes its output to a UUID-named MP3, so
                # filenames never collide across concurrent callers and
                # the output-dir cleanup step is idempotent. External
                # TTS providers handle independent HTTP calls fine —
                # fan-out here is what makes multi-speaker announces
                # snappy, since announce() synthesizes per target.
                parallel_safe=True,
            ),
            ToolDefinition(
                name="list_voices",
                slash_group="tts",
                slash_command="voices",
                slash_help="List available TTS voices: /tts voices",
                description="List all available TTS voices from the provider.",
                required_role="everyone",
                parallel_safe=True,
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        match name:
            case "synthesize":
                return await self._tool_synthesize(arguments)
            case "list_voices":
                return await self._tool_list_voices()
            case _:
                raise KeyError(f"Unknown tool: {name}")

    async def _tool_synthesize(self, arguments: dict[str, Any]) -> str:
        text = arguments["text"]
        request = SynthesisRequest(text=text, voice_id="", output_format=AudioFormat.MP3)
        result = await self.synthesize(request)

        output_dir = get_output_dir("tts")
        cleanup_old_files(output_dir, self._output_ttl_seconds)

        file_path = output_dir / f"{uuid.uuid4()}.mp3"
        file_path.write_bytes(result.audio)

        return json.dumps(
            {
                "file_path": str(file_path),
                "format": "mp3",
                "duration_seconds": result.duration_seconds,
                "characters_used": result.characters_used,
            }
        )

    async def _tool_list_voices(self) -> str:
        voices = await self.list_voices()
        return json.dumps(
            [
                {
                    "voice_id": v.voice_id,
                    "name": v.name,
                    "language": v.language,
                    "description": v.description,
                }
                for v in voices
            ]
        )
