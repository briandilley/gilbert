"""TTS service — wraps a TTSBackend as a discoverable service."""

import json
import logging
import uuid
from dataclasses import replace
from typing import Any

from gilbert.config import TTSVoiceConfig
from gilbert.core.output import cleanup_old_files, get_output_dir
from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.credentials import ApiKeyCredential
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


class TTSService(Service):
    """Exposes a TTSBackend as a service with text_to_speech capability."""

    def __init__(
        self,
        backend: TTSBackend,
        credential_name: str,
    ) -> None:
        self._backend = backend
        self._credential_name = credential_name
        # Tunable config — loaded from ConfigurationService during start()
        self._config: dict[str, object] = {}
        self._voices: dict[str, TTSVoiceConfig] = {}
        self._default_voice: str = ""
        self._output_ttl_seconds: int = 3600

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="tts",
            capabilities=frozenset({"text_to_speech", "ai_tools"}),
            requires=frozenset({"credentials"}),
            optional=frozenset({"configuration"}),
        )

    @property
    def backend(self) -> TTSBackend:
        return self._backend

    @property
    def default_voice(self) -> str:
        return self._default_voice

    @property
    def voices(self) -> dict[str, TTSVoiceConfig]:
        return dict(self._voices)

    def resolve_voice(self, name: str) -> str:
        """Resolve a named voice to its voice_id. Raises KeyError if not found."""
        voice_cfg = self._voices.get(name)
        if voice_cfg is None:
            raise KeyError(f"Unknown voice name: {name!r}")
        return voice_cfg.voice_id

    async def start(self, resolver: ServiceResolver) -> None:
        from gilbert.core.services.credentials import CredentialService

        # Load tunable config from ConfigurationService if available
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None:
            from gilbert.core.services.configuration import ConfigurationService

            if isinstance(config_svc, ConfigurationService):
                section = config_svc.get_section("tts")
                self._apply_config(section)
                # Also pick up global output_ttl_seconds
                global_ttl = config_svc.get("output_ttl_seconds")
                if global_ttl is not None:
                    self._output_ttl_seconds = int(global_ttl)

        cred_svc = resolver.require_capability("credentials")
        if not isinstance(cred_svc, CredentialService):
            raise TypeError("Expected CredentialService for 'credentials' capability")

        cred = cred_svc.require(self._credential_name)
        if not isinstance(cred, ApiKeyCredential):
            raise TypeError(
                f"Credential '{self._credential_name}' must be an api_key credential"
            )

        init_config: dict[str, object] = {**self._config, "api_key": cred.api_key}
        await self._backend.initialize(init_config)
        logger.info("TTS service started (credential=%s)", self._credential_name)

    def _apply_config(self, section: dict[str, Any]) -> None:
        """Apply tunable config values from a config section."""
        self._default_voice = section.get("default_voice", self._default_voice)
        self._config = section.get("settings", self._config)
        raw_voices = section.get("voices", {})
        if raw_voices:
            self._voices = {
                k: TTSVoiceConfig(**v) if isinstance(v, dict) else v
                for k, v in raw_voices.items()
            }

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "tts"

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="default_voice", type=ToolParameterType.STRING,
                description="Default voice name for speech synthesis.",
                default="",
            ),
            ConfigParam(
                key="voices", type=ToolParameterType.OBJECT,
                description="Named voice mappings (name → {voice_id: str}).",
                default={},
            ),
            ConfigParam(
                key="settings", type=ToolParameterType.OBJECT,
                description="Backend-specific settings (e.g., model_id, silence_padding).",
                default={},
            ),
            ConfigParam(
                key="backend", type=ToolParameterType.STRING,
                description="TTS backend provider.",
                default="elevenlabs", restart_required=True,
            ),
            ConfigParam(
                key="credential", type=ToolParameterType.STRING,
                description="Name of the API key credential to use.",
                restart_required=True,
            ),
            ConfigParam(
                key="enabled", type=ToolParameterType.BOOLEAN,
                description="Whether the TTS service is enabled.",
                default=False, restart_required=True,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._apply_config(config)

    async def stop(self) -> None:
        await self._backend.close()

    async def synthesize(
        self, request: SynthesisRequest, *, voice_name: str | None = None
    ) -> SynthesisResult:
        """Synthesize speech from text.

        If voice_name is given, its voice_id overrides the request's voice_id.
        If request.voice_id is empty and no voice_name is given, the default voice is used.
        """
        effective_request = self._resolve_request(request, voice_name)
        return await self._backend.synthesize(effective_request)

    async def list_voices(self) -> list[Voice]:
        """List available voices."""
        return await self._backend.list_voices()

    async def get_voice(self, voice_id: str) -> Voice | None:
        """Get a voice by ID."""
        return await self._backend.get_voice(voice_id)

    def _resolve_request(
        self, request: SynthesisRequest, voice_name: str | None
    ) -> SynthesisRequest:
        """Determine the effective voice_id for a request."""
        if voice_name is not None:
            return replace(request, voice_id=self.resolve_voice(voice_name))
        if not request.voice_id and self._default_voice:
            default_id = self._voices.get(self._default_voice)
            if default_id is not None:
                return replace(request, voice_id=default_id.voice_id)
        return request

    # --- ToolProvider protocol ---

    @property
    def tool_provider_name(self) -> str:
        return "tts"

    def get_tools(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="speak",
                description="Synthesize speech from text and save as an MP3 file.",
                parameters=[
                    ToolParameter(
                        name="text",
                        type=ToolParameterType.STRING,
                        description="The text to speak.",
                    ),
                    ToolParameter(
                        name="voice_name",
                        type=ToolParameterType.STRING,
                        description="Named voice to use (from config). Uses default if omitted.",
                        required=False,
                    ),
                ],
            ),
            ToolDefinition(
                name="list_voices",
                description="List all available TTS voices.",
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        match name:
            case "speak":
                return await self._tool_speak(arguments)
            case "list_voices":
                return await self._tool_list_voices()
            case _:
                raise KeyError(f"Unknown tool: {name}")

    async def _tool_speak(self, arguments: dict[str, Any]) -> str:
        text = arguments["text"]
        voice_name = arguments.get("voice_name")

        request = SynthesisRequest(text=text, voice_id="", output_format=AudioFormat.MP3)
        result = await self.synthesize(request, voice_name=voice_name)

        # Clean up old files, then write new one
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
                "labels": v.labels,
            }
            for v in voices
        ])
