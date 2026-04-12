"""ElevenLabs TTS backend — text-to-speech via the ElevenLabs API."""

import logging
import time
from collections import OrderedDict
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

# Default synthesis cache capacity — enough to cover a busy day of
# recurring announcements without retaining unbounded audio in memory.
# At typical MP3 sizes (~40KB for a short phrase) this is ~10MB max.
_DEFAULT_CACHE_MAX_ENTRIES = 256

# Default cache TTL — entries expire after this many seconds. ElevenLabs
# output is deterministic for a given input, but expiring entries after
# a reasonable window bounds memory usage when lots of one-off requests
# accumulate and gives the team a path to "re-synthesize this" by
# waiting out the TTL (e.g. after changing the voice in ElevenLabs).
_DEFAULT_CACHE_TTL_SECONDS = 1800  # 30 minutes


# Cache key: everything that changes the synthesized audio bytes.
# If any of these fields differ, the backend will produce different
# output and the cache entry should not be shared.
_CacheKey = tuple[
    str,    # voice_id
    str,    # output_format value
    str,    # model_id
    str,    # text
    float | None,   # stability
    float | None,   # similarity_boost
    float,  # speed
]

# Cache value: (synthesis result, monotonic insertion timestamp) so we
# can expire entries older than the configured TTL on access.
_CacheEntry = tuple[SynthesisResult, float]


class ElevenLabsTTS(TTSBackend):
    """ElevenLabs text-to-speech implementation with an in-memory LRU cache.

    Identical synthesis requests (same text, voice, format, model, and
    voice settings) are served from the cache without hitting the API.
    This is important for recurring alarms and repeated announcements —
    a 15-second wake-up alarm would otherwise burn thousands of API
    calls per day for the same short phrase.
    """

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
            ConfigParam(
                key="cache_max_entries", type=ToolParameterType.INTEGER,
                description=(
                    "Maximum number of synthesis results to keep in the "
                    "in-memory LRU cache. Identical requests return the "
                    "cached audio without hitting the API. Set to 0 to "
                    "disable caching."
                ),
                default=_DEFAULT_CACHE_MAX_ENTRIES,
            ),
            ConfigParam(
                key="cache_ttl_seconds", type=ToolParameterType.INTEGER,
                description=(
                    "How long a cached synthesis result stays valid (in "
                    "seconds). After this, the entry is evicted on next "
                    "access and the API is called again. Default 1800 "
                    "(30 minutes). Set to 0 to disable the TTL — entries "
                    "only age out via LRU eviction."
                ),
                default=_DEFAULT_CACHE_TTL_SECONDS,
            ),
        ]

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._api_key: str = ""
        self._voice_id: str = ""
        self._model_id: str = "eleven_turbo_v2_5"
        self._cache: OrderedDict[_CacheKey, _CacheEntry] = OrderedDict()
        self._cache_max_entries: int = _DEFAULT_CACHE_MAX_ENTRIES
        self._cache_ttl_seconds: float = float(_DEFAULT_CACHE_TTL_SECONDS)
        self._cache_hits: int = 0
        self._cache_misses: int = 0
        self._cache_evictions: int = 0

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

        # Cache capacity (optional, falls back to default). Config values
        # come in as ``object`` from the dict, so coerce via ``str()``
        # which all the expected types (int/float/str) handle cleanly.
        cache_cap_raw = config.get("cache_max_entries")
        if cache_cap_raw is not None:
            try:
                self._cache_max_entries = max(0, int(str(cache_cap_raw)))
            except (TypeError, ValueError):
                self._cache_max_entries = _DEFAULT_CACHE_MAX_ENTRIES

        # Cache TTL (optional, 0 disables expiry but not eviction)
        cache_ttl_raw = config.get("cache_ttl_seconds")
        if cache_ttl_raw is not None:
            try:
                self._cache_ttl_seconds = max(0.0, float(str(cache_ttl_raw)))
            except (TypeError, ValueError):
                self._cache_ttl_seconds = float(_DEFAULT_CACHE_TTL_SECONDS)

        self._cache.clear()

        self._client = httpx.AsyncClient(
            base_url=_BASE_URL,
            headers={
                "xi-api-key": self._api_key,
                "Content-Type": "application/json",
            },
            timeout=60.0,
        )
        logger.info(
            "ElevenLabs TTS initialized (model=%s, cache_max=%d, cache_ttl=%.0fs)",
            self._model_id,
            self._cache_max_entries,
            self._cache_ttl_seconds,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        # Release cached audio so a restart starts fresh.
        self._cache.clear()

    # --- Cache ---

    def _make_cache_key(self, request: SynthesisRequest) -> _CacheKey:
        """Build a cache key from every field that affects the output audio."""
        return (
            request.voice_id,
            request.output_format.value,
            self._model_id,
            request.text,
            request.stability,
            request.similarity_boost,
            request.speed,
        )

    def _cache_get(self, key: _CacheKey) -> SynthesisResult | None:
        """LRU lookup with TTL expiry.

        Returns the stored result on hit, or None on miss or expiry.
        Expired entries are removed from the cache as a side effect
        (lazy expiration — no background sweeper needed).
        """
        if self._cache_max_entries == 0:
            return None
        entry = self._cache.get(key)
        if entry is None:
            return None

        result, inserted_at = entry
        if self._cache_ttl_seconds > 0:
            age = time.monotonic() - inserted_at
            if age >= self._cache_ttl_seconds:
                # Entry expired — evict and treat as a miss
                del self._cache[key]
                self._cache_evictions += 1
                return None

        # Refresh LRU order
        self._cache.move_to_end(key)
        return result

    def _cache_put(self, key: _CacheKey, result: SynthesisResult) -> None:
        """Insert with timestamp and LRU eviction at capacity."""
        if self._cache_max_entries == 0:
            return
        self._cache[key] = (result, time.monotonic())
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_max_entries:
            self._cache.popitem(last=False)
            self._cache_evictions += 1

    def cache_stats(self) -> dict[str, Any]:
        """Snapshot of cache metrics — used by tests and observability."""
        return {
            "size": len(self._cache),
            "max_entries": self._cache_max_entries,
            "ttl_seconds": self._cache_ttl_seconds,
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "evictions": self._cache_evictions,
        }

    async def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        # Use configured voice_id as default if request doesn't specify one
        if not request.voice_id:
            if self._voice_id:
                request = SynthesisRequest(
                    text=request.text,
                    voice_id=self._voice_id,
                    output_format=request.output_format,
                    speed=request.speed,
                    stability=request.stability,
                    similarity_boost=request.similarity_boost,
                )
            else:
                raise ValueError("No voice_id configured — set voice_id in TTS backend settings")

        # Cache hit check before touching the API
        cache_key = self._make_cache_key(request)
        cached = self._cache_get(cache_key)
        if cached is not None:
            self._cache_hits += 1
            logger.debug(
                "ElevenLabs TTS cache hit for voice=%s (%d chars)",
                request.voice_id,
                len(request.text),
            )
            return cached

        self._cache_misses += 1
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

        result = SynthesisResult(
            audio=audio,
            format=request.output_format,
            characters_used=characters_used,
        )
        # Only cache successful synthesis
        self._cache_put(cache_key, result)
        return result

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

