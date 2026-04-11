"""ElevenLabs TTS backend — text-to-speech via the ElevenLabs API."""

import logging
from typing import Any

import httpx

from gilbert.interfaces.tts import (
    AudioFormat,
    SynthesisRequest,
    SynthesisResult,
    TTSBackend,
    Voice,
)

logger = logging.getLogger(__name__)

# ElevenLabs API base
_BASE_URL = "https://api.elevenlabs.io/v1"

# Map our AudioFormat enum to ElevenLabs output_format parameter values
_FORMAT_MAP: dict[AudioFormat, str] = {
    AudioFormat.MP3: "mp3_44100_128",
    AudioFormat.WAV: "pcm_44100",
    AudioFormat.OGG: "ogg_vorbis",
    AudioFormat.PCM: "pcm_44100",
}



class ElevenLabsTTS(TTSBackend):

    backend_name = "elevenlabs"

    @classmethod
    def backend_config_params(cls) -> list["ConfigParam"]:
        from gilbert.interfaces.configuration import ConfigParam
        from gilbert.interfaces.tools import ToolParameterType

        return [
            ConfigParam(
                key="api_key", type=ToolParameterType.STRING,
                description="ElevenLabs API key.",
                sensitive=True, restart_required=True,
            ),
            ConfigParam(
                key="voice_id", type=ToolParameterType.STRING,
                description="ElevenLabs voice ID for speech synthesis.",
                restart_required=True,
            ),
            ConfigParam(
                key="model_id", type=ToolParameterType.STRING,
                description="ElevenLabs model ID.",
                default="eleven_turbo_v2_5",
            ),
        ]
    """ElevenLabs text-to-speech implementation."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._api_key: str = ""
        self._voice_id: str = ""
        self._model_id: str = "eleven_turbo_v2_5"

    async def initialize(self, config: dict[str, object]) -> None:
        api_key = config.get("api_key")
        if not api_key or not isinstance(api_key, str):
            raise ValueError("ElevenLabs TTS requires 'api_key' in config")
        self._api_key = api_key

        self._voice_id = str(config.get("voice_id", ""))

        if "model_id" in config:
            model_id = config["model_id"]
            if isinstance(model_id, str):
                self._model_id = model_id

        self._client = httpx.AsyncClient(
            base_url=_BASE_URL,
            headers={
                "xi-api-key": self._api_key,
                "Content-Type": "application/json",
            },
            timeout=60.0,
        )
        logger.info("ElevenLabs TTS initialized (model=%s)", self._model_id)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        # Use configured voice_id as default if request doesn't specify one
        if not request.voice_id:
            if self._voice_id:
                request = SynthesisRequest(
                    text=request.text,
                    voice_id=self._voice_id,
                    output_format=request.output_format,
                )
            else:
                raise ValueError("No voice_id configured — set voice_id in TTS backend settings")

        client = self._require_client()

        output_format = _FORMAT_MAP.get(request.output_format, "mp3_44100_128")

        body: dict[str, Any] = {
            "text": request.text,
            "model_id": self._model_id,
        }

        voice_settings: dict[str, float] = {}
        if request.stability is not None:
            voice_settings["stability"] = request.stability
        if request.similarity_boost is not None:
            voice_settings["similarity_boost"] = request.similarity_boost
        if voice_settings:
            body["voice_settings"] = voice_settings

        response = await client.post(
            f"/text-to-speech/{request.voice_id}",
            json=body,
            params={"output_format": output_format},
        )
        response.raise_for_status()

        audio = response.content

        characters_used = len(request.text)

        return SynthesisResult(
            audio=audio,
            format=request.output_format,
            characters_used=characters_used,
        )

    async def list_voices(self) -> list[Voice]:
        client = self._require_client()
        response = await client.get("/voices")
        response.raise_for_status()

        data = response.json()
        voices: list[Voice] = []
        for v in data.get("voices", []):
            voices.append(
                Voice(
                    voice_id=v["voice_id"],
                    name=v.get("name", v["voice_id"]),
                    language=v.get("fine_tuning", {}).get("language"),
                    description=v.get("description"),
                    labels=v.get("labels", {}),
                )
            )
        return voices

    async def get_voice(self, voice_id: str) -> Voice | None:
        client = self._require_client()
        response = await client.get(f"/voices/{voice_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()

        v = response.json()
        return Voice(
            voice_id=v["voice_id"],
            name=v.get("name", v["voice_id"]),
            language=v.get("fine_tuning", {}).get("language"),
            description=v.get("description"),
            labels=v.get("labels", {}),
        )

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("ElevenLabs TTS not initialized — call initialize() first")
        return self._client

