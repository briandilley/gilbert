"""Vision service — image understanding via Claude Vision API.

Provides image description capabilities for other services (e.g., knowledge
indexing). Extracts technical content from diagrams, pinout tables, wiring
charts, and other image-heavy document pages.
"""

import asyncio
import base64
import logging
from typing import Any

from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver

logger = logging.getLogger(__name__)

_EXTRACTION_PROMPT = (
    "Extract ALL technical content from this page image as plain structured text. "
    "Include: pinout tables, wiring diagrams, connector assignments, component "
    "specifications, part numbers, voltage/current ratings, communication protocols, "
    "dimensions, torque specs, and any other technical data. Reproduce tables as "
    "aligned text columns. Label diagram elements clearly (e.g., 'Pin 1: CAN_H, "
    "Pin 2: CAN_L'). Do NOT describe the visual layout — extract the information "
    "content only. If the page contains no technical content, respond with an "
    "empty string."
)


class VisionService(Service):
    """Image understanding via Claude Vision API.

    Capabilities: vision
    """

    def __init__(self) -> None:
        self._api_key: str = ""
        self._model: str = "claude-sonnet-4-5-20250514"
        self._client: Any = None  # anthropic.Anthropic

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="vision",
            capabilities=frozenset({"vision"}),
            requires=frozenset({"credentials"}),
            optional=frozenset({"configuration"}),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        # Read config
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None:
            from gilbert.core.services.configuration import ConfigurationService

            if isinstance(config_svc, ConfigurationService):
                section = config_svc.get_section("knowledge")
                self._model = section.get("vision_model", self._model)
                credential_name = section.get("vision_credential", "")

                # Default to AI credential if no vision-specific one
                if not credential_name:
                    ai_section = config_svc.get_section("ai")
                    credential_name = ai_section.get("credential", "")

        # Resolve API key
        from gilbert.core.services.credentials import CredentialService
        from gilbert.interfaces.credentials import ApiKeyCredential

        cred_svc = resolver.require_capability("credentials")
        if isinstance(cred_svc, CredentialService) and credential_name:
            cred = cred_svc.get(credential_name)
            if isinstance(cred, ApiKeyCredential):
                self._api_key = cred.api_key

        if not self._api_key:
            logger.warning("Vision service started without API key — describe_image will be unavailable")
        else:
            logger.info("Vision service started (model=%s)", self._model)

    @property
    def available(self) -> bool:
        """Whether the vision service has a valid API key."""
        return bool(self._api_key)

    def _get_client(self) -> Any:
        """Lazily initialize the Anthropic client."""
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    async def describe_image(self, image_bytes: bytes, media_type: str) -> str:
        """Send an image to Claude Vision and return a text description.

        Args:
            image_bytes: Raw image data (PNG, JPEG, etc.)
            media_type: MIME type — "image/png", "image/jpeg", etc.

        Returns:
            Plain text description of technical content, or empty string on failure.
        """
        if not self._api_key:
            return ""

        try:
            client = self._get_client()
            b64_data = base64.standard_b64encode(image_bytes).decode("ascii")

            response = await asyncio.to_thread(
                client.messages.create,
                model=self._model,
                max_tokens=4096,
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
                                "text": _EXTRACTION_PROMPT,
                            },
                        ],
                    }
                ],
            )

            # Extract text from response
            text_parts: list[str] = []
            for block in response.content:
                if hasattr(block, "text"):
                    text_parts.append(block.text)

            return "\n".join(text_parts).strip()

        except Exception:
            logger.warning("Vision describe_image failed", exc_info=True)
            return ""
