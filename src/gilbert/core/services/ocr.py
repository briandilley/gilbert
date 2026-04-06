"""OCR service — text extraction from images via Tesseract.

Provides optical character recognition for document indexing. Gracefully
degrades if Tesseract or Pillow are not installed.
"""

import asyncio
import io
import logging

from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver

logger = logging.getLogger(__name__)


class OCRService(Service):
    """Text extraction from images via Tesseract OCR.

    Capabilities: ocr

    Gracefully degrades if pytesseract or Pillow are not installed —
    check the ``available`` property before calling ``extract_text``.
    """

    def __init__(self) -> None:
        self._available = False

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="ocr",
            capabilities=frozenset({"ocr"}),
            requires=frozenset(),
            optional=frozenset(),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        try:
            import pytesseract  # noqa: F401
            from PIL import Image  # noqa: F401

            self._available = True
            logger.info("OCR service started (tesseract available)")
        except ImportError:
            self._available = False
            logger.info("OCR service started (tesseract not available — OCR disabled)")

    @property
    def available(self) -> bool:
        """Whether OCR dependencies are installed and working."""
        return self._available

    async def extract_text(self, image_bytes: bytes) -> str:
        """Extract text from an image using Tesseract OCR.

        Args:
            image_bytes: Raw image data (PNG, JPEG, TIFF, etc.)

        Returns:
            Extracted text, or empty string if OCR is unavailable or fails.
        """
        if not self._available:
            return ""

        try:
            import pytesseract
            from PIL import Image

            def _ocr() -> str:
                img = Image.open(io.BytesIO(image_bytes))
                return pytesseract.image_to_string(img)

            result = await asyncio.to_thread(_ocr)
            return result.strip()

        except Exception:
            logger.warning("OCR extraction failed", exc_info=True)
            return ""
