"""Anthropic Vision backend — image understanding via Claude Vision API."""

import asyncio
import base64
import logging
from typing import Any

from gilbert.interfaces.vision import VisionBackend

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "claude-sonnet-4-5-20250929"


class AnthropicVision(VisionBackend):
    """Vision backend using the Anthropic Messages API with image content."""

    backend_name = "anthropic"

    @classmethod
    def backend_config_params(cls) -> list["ConfigParam"]:
        from gilbert.interfaces.configuration import ConfigParam
        from gilbert.interfaces.tools import ToolParameterType

        return [
            ConfigParam(
                key="api_key", type=ToolParameterType.STRING,
                description="Anthropic API key.",
                sensitive=True, restart_required=True,
            ),
            ConfigParam(
                key="model", type=ToolParameterType.STRING,
                description="Vision model ID.",
                default=_DEFAULT_MODEL,
            ),
            ConfigParam(
                key="max_tokens", type=ToolParameterType.INTEGER,
                description="Maximum tokens in vision response.",
                default=4096,
            ),
        ]

    def __init__(self) -> None:
        self._api_key: str = ""
        self._model: str = _DEFAULT_MODEL
        self._max_tokens: int = 4096
        self._client: Any = None

    async def initialize(self, config: dict[str, Any]) -> None:
        self._api_key = str(config.get("api_key", ""))
        self._model = str(config.get("model", _DEFAULT_MODEL))
        self._max_tokens = int(config.get("max_tokens", 4096))

        if self._api_key:
            logger.info("Anthropic Vision backend initialized (model=%s)", self._model)
        else:
            logger.warning("Anthropic Vision backend: no API key configured")

    async def close(self) -> None:
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    async def describe_image(self, image_bytes: bytes, media_type: str) -> str:
        if not self._api_key:
            return ""

        try:
            client = self._get_client()
            b64_data = base64.standard_b64encode(image_bytes).decode("ascii")

            response = await asyncio.to_thread(
                client.messages.create,
                model=self._model,
                max_tokens=self._max_tokens,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": b64_data,
                                },
                            },
                            {
                                "type": "text",
                                "text": (
                                    "Extract ALL technical content from this page image as plain structured text. "
                                    "Include: pinout tables, wiring diagrams, connector assignments, component "
                                    "specifications, part numbers, voltage/current ratings, communication protocols, "
                                    "dimensions, torque specs, and any other technical data. Reproduce tables as "
                                    "aligned text columns. Label diagram elements clearly (e.g., 'Pin 1: CAN_H, "
                                    "Pin 2: CAN_L'). Do NOT describe the visual layout — extract the information "
                                    "content only. If the page contains no technical content, respond with an "
                                    "empty string."
                                ),
                            },
                        ],
                    }
                ],
            )

            text_parts: list[str] = []
            for block in response.content:
                if hasattr(block, "text"):
                    text_parts.append(block.text)

            return "\n".join(text_parts).strip()

        except Exception:
            logger.warning("Vision describe_image failed", exc_info=True)
            return ""
