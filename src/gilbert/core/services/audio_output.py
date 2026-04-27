"""Audio output service — deliver text as audio to chat or speakers.

Thin wrapper tool that synthesizes text-to-speech and delivers the
result either as an embedded audio link in the chat or by playing it
on the shop speakers. Any tool that returns TTS-formatted text (e.g.
``current_recap(for_tts=True)``) can be chained through this service
to get audio delivery.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from gilbert.core.output import cleanup_old_files, get_output_dir
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.speaker import SpeakerProvider
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)
from gilbert.interfaces.tts import (
    AudioFormat,
    SynthesisRequest,
    TTSProvider,
)

logger = logging.getLogger(__name__)

# Audio files embedded in chat live longer than transient speaker
# announcements because the user may not play them immediately.
_AUDIO_FILE_TTL_SECONDS = 24 * 3600


class AudioOutputService(Service):
    """Deliver text as audio to the chat UI or the shop speakers.

    Exposes a single tool, ``audio_output``, that synthesizes text to
    speech and either embeds an audio player link in the chat or plays
    the audio through the speaker service.
    """

    def __init__(self) -> None:
        self._resolver: ServiceResolver | None = None

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="audio_output",
            capabilities=frozenset({"ai_tools"}),
            requires=frozenset(),
            optional=frozenset({"text_to_speech", "speaker_control"}),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver
        # Ensure output dir exists and purge expired files on startup
        audio_dir = get_output_dir("audio")
        cleanup_old_files(audio_dir, _AUDIO_FILE_TTL_SECONDS)
        logger.info("Audio output service started")

    async def stop(self) -> None:
        pass

    # --- ToolProvider ---

    @property
    def tool_provider_name(self) -> str:
        return "audio_output"

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="audio_output",
                slash_command="announce",
                slash_help=(
                    "Announce text over the shop speakers (or as a "
                    "playable audio link in chat). Positional form: "
                    "/announce <text> [destination]. Use "
                    "speaker_names=... to target specific speakers."
                ),
                description=(
                    "Synthesize text to speech and deliver the audio. "
                    "Use whenever the user wants to HEAR something "
                    "rather than read it — phrases like 'as audio', "
                    "'as mp3', 'give me a voice version', 'play this', "
                    "'announce this', 'speak this', 'for download'. "
                    "Default destination is 'chat', which embeds a "
                    "playable audio link in the conversation so the "
                    "user can click to listen. Set destination='speakers' "
                    "to play it on the shop Sonos speakers instead "
                    "(accepts optional volume 0-100 and speaker_names "
                    "to target specific speakers). Typical workflow: "
                    "first call another tool (e.g. current_recap with "
                    "for_tts=true) to get narrative-formatted text, "
                    "then pass that text here."
                ),
                parameters=[
                    ToolParameter(
                        name="text",
                        type=ToolParameterType.STRING,
                        description="The text to synthesize and deliver as audio.",
                        required=True,
                    ),
                    ToolParameter(
                        name="destination",
                        type=ToolParameterType.STRING,
                        description=(
                            "Where to deliver the audio: 'chat' (default, "
                            "embeds a playable audio link in chat) or "
                            "'speakers' (plays on the shop speakers)."
                        ),
                        enum=["chat", "speakers"],
                        required=False,
                    ),
                    ToolParameter(
                        name="volume",
                        type=ToolParameterType.INTEGER,
                        description=(
                            "Volume 0-100 (speakers destination only). "
                            "Falls back to the configured default announce "
                            "volume when omitted."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="speaker_names",
                        type=ToolParameterType.ARRAY,
                        description=(
                            "Specific speaker names to play on (speakers "
                            "destination only). Falls back to the configured "
                            "default announce speakers when omitted."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="context",
                        type=ToolParameterType.STRING,
                        description=(
                            "Optional one-line description of the situation "
                            "or mood (e.g. 'celebratory end-of-day recap', "
                            "'doorbell ring at front door'). Helps the TTS "
                            "engine pick expressive delivery — ignored by "
                            "backends that don't support it."
                        ),
                        required=False,
                    ),
                ],
                required_role="user",
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name != "audio_output":
            raise KeyError(f"Unknown tool: {name}")

        text = str(arguments.get("text") or "").strip()
        if not text:
            return "No text provided to synthesize."

        context = str(arguments.get("context") or "").strip()
        destination = str(arguments.get("destination") or "chat").lower()
        if destination == "speakers":
            return await self._deliver_to_speakers(text, arguments, context)
        if destination == "chat":
            return await self._deliver_to_chat(text, context)
        return f"Unknown destination '{destination}'. Use 'chat' (default) or 'speakers'."

    # --- Delivery paths ---

    async def _deliver_to_chat(self, text: str, context: str = "") -> str:
        """Synthesize text to an MP3 file and return a markdown audio link."""
        if self._resolver is None:
            return "Audio output service is not ready."

        tts_svc = self._resolver.get_capability("text_to_speech")
        if not isinstance(tts_svc, TTSProvider):
            return "Text-to-speech is not available. Cannot generate audio for chat."

        # Voice ID "" tells the TTS service to use its configured default,
        # matching the convention in SpeakerService.announce().
        request = SynthesisRequest(
            text=text,
            voice_id="",
            output_format=AudioFormat.MP3,
            context=context,
        )
        try:
            result = await tts_svc.synthesize(request)
        except Exception:
            logger.exception("TTS synthesis failed for audio_output")
            return "Failed to synthesize audio. See server logs for details."

        audio_dir = get_output_dir("audio")
        # Opportunistic cleanup on each generation
        cleanup_old_files(audio_dir, _AUDIO_FILE_TTL_SECONDS)

        short_id = uuid.uuid4().hex[:8]
        filename = f"audio-{short_id}.{result.format.value}"
        file_path = audio_dir / filename
        file_path.write_bytes(result.audio)

        # Relative URL — resolves against whatever host is serving the
        # chat UI. The /output/ prefix is a publicly-served static mount
        # in src/gilbert/web/__init__.py.
        url = f"/output/audio/{filename}"
        duration_str = f" ({result.duration_seconds:.0f}s)" if result.duration_seconds else ""
        return f"Audio ready{duration_str}. [▶ Play or download]({url})"

    async def _deliver_to_speakers(
        self, text: str, arguments: dict[str, Any], context: str = ""
    ) -> str:
        """Delegate to SpeakerProvider.announce()."""
        if self._resolver is None:
            return "Audio output service is not ready."

        speaker_svc = self._resolver.get_capability("speaker_control")
        if not isinstance(speaker_svc, SpeakerProvider):
            return "Speaker control is not available. Cannot play audio on speakers."

        volume_raw = arguments.get("volume")
        try:
            volume = int(volume_raw) if volume_raw is not None else None
        except (TypeError, ValueError):
            volume = None

        speaker_names_raw = arguments.get("speaker_names")
        speaker_names: list[str] | None
        if isinstance(speaker_names_raw, list) and speaker_names_raw:
            speaker_names = [str(s) for s in speaker_names_raw]
        else:
            speaker_names = None

        try:
            await speaker_svc.announce(
                text=text,
                speaker_names=speaker_names,
                volume=volume,
                context=context,
            )
        except Exception:
            logger.exception("Speaker announcement failed from audio_output")
            return "Failed to play audio on speakers. See server logs for details."

        preview = text if len(text) <= 80 else text[:77] + "..."
        return f'Played on speakers: "{preview}"'
