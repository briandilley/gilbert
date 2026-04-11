"""TTS service — wraps a TTSBackend as a discoverable service.

Adds backend-agnostic silence padding to synthesized audio so speakers
don't cut off the last word.
"""

import json
import logging
import uuid
from typing import Any

from gilbert.core.output import cleanup_old_files, get_output_dir
from gilbert.interfaces.configuration import ConfigParam
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
)

logger = logging.getLogger(__name__)

_PCM_SAMPLE_RATE = 44100


def _generate_pcm_silence(seconds: float) -> bytes:
    """Generate raw 16-bit PCM silence at 44100 Hz."""
    return b"\x00\x00" * int(_PCM_SAMPLE_RATE * seconds)


def _generate_mp3_silence(seconds: float) -> bytes:
    """Generate minimal valid MP3 silence frames (MPEG1 Layer 3, 128kbps, 44100 Hz)."""
    frame_samples = 1152
    frames_needed = int((_PCM_SAMPLE_RATE * seconds) / frame_samples) + 1
    header = b"\xff\xfb\x90\xc0"
    frame = header + b"\x00" * 413  # 417-byte frame: 4 header + 413 payload
    return frame * frames_needed


def _append_silence(audio: bytes, fmt: AudioFormat, seconds: float) -> bytes:
    """Append silence padding to audio data."""
    if seconds <= 0:
        return audio
    if fmt == AudioFormat.MP3:
        return audio + _generate_mp3_silence(seconds)
    if fmt in (AudioFormat.PCM, AudioFormat.WAV):
        return audio + _generate_pcm_silence(seconds)
    return audio


class TTSService(Service):
    """Exposes a TTSBackend as a service with text_to_speech capability."""

    def __init__(self) -> None:
        self._backend: TTSBackend | None = None
        self._backend_name: str = "elevenlabs"
        self._enabled: bool = False
        self._config: dict[str, object] = {}
        self._silence_padding: float = 3.0
        self._output_ttl_seconds: int = 3600

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="tts",
            capabilities=frozenset({"text_to_speech", "ai_tools"}),
            optional=frozenset({"configuration"}),
            toggleable=True,
            toggle_description="Text-to-speech synthesis",
        )

    @property
    def backend(self) -> TTSBackend | None:
        return self._backend

    async def start(self, resolver: ServiceResolver) -> None:
        config_svc = resolver.get_capability("configuration")
        section: dict[str, Any] = {}
        if config_svc is not None:
            from gilbert.core.services.configuration import ConfigurationService

            if isinstance(config_svc, ConfigurationService):
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
        if backend_name not in backends:
            # Import known backends to trigger registration
            try:
                import gilbert.integrations.elevenlabs_tts  # noqa: F401
            except ImportError:
                pass
            backends = TTSBackend.registered_backends()
        backend_cls = backends.get(backend_name)
        if backend_cls is None:
            raise ValueError(f"Unknown TTS backend: {backend_name}")
        self._backend = backend_cls()

        await self._backend.initialize(self._config)
        logger.info("TTS service started")

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "tts"

    @property
    def config_category(self) -> str:
        return "Media"

    def config_params(self) -> list[ConfigParam]:
        # Import known backends so they register before we query the registry
        try:
            import gilbert.integrations.elevenlabs_tts  # noqa: F401
        except ImportError:
            pass

        params = [
            ConfigParam(
                key="silence_padding", type=ToolParameterType.NUMBER,
                description="Seconds of silence appended after synthesized audio.",
                default=3.0,
            ),
            ConfigParam(
                key="backend", type=ToolParameterType.STRING,
                description="TTS backend provider.",
                default="elevenlabs", restart_required=True,
                choices=tuple(TTSBackend.registered_backends().keys()) or ("elevenlabs",),
            ),
        ]
        backends = TTSBackend.registered_backends()
        backend_cls = backends.get(self._backend_name)
        if backend_cls is not None:
            for bp in backend_cls.backend_config_params():
                params.append(ConfigParam(
                    key=f"settings.{bp.key}", type=bp.type,
                    description=bp.description, default=bp.default,
                    restart_required=bp.restart_required, sensitive=bp.sensitive,
                    choices=bp.choices, multiline=bp.multiline, backend_param=True,
                ))
        return params

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._config = config.get("settings", self._config)
        sp = config.get("silence_padding")
        if sp is not None:
            self._silence_padding = float(sp)

    async def stop(self) -> None:
        if self._backend is not None:
            await self._backend.close()

    async def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        """Synthesize speech from text. Appends silence padding if configured."""
        if self._backend is None:
            raise RuntimeError("TTS service is not enabled")
        result = await self._backend.synthesize(request)
        if self._silence_padding > 0:
            padded = _append_silence(result.audio, result.format, self._silence_padding)
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

    def get_tools(self) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        return [
            ToolDefinition(
                name="synthesize",
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
            ),
            ToolDefinition(
                name="list_voices",
                description="List all available TTS voices from the provider.",
                required_role="everyone",
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

        return json.dumps({
            "file_path": str(file_path),
            "format": "mp3",
            "duration_seconds": result.duration_seconds,
            "characters_used": result.characters_used,
        })

    async def _tool_list_voices(self) -> str:
        voices = await self.list_voices()
        return json.dumps([
            {
                "voice_id": v.voice_id,
                "name": v.name,
                "language": v.language,
                "description": v.description,
            }
            for v in voices
        ])
