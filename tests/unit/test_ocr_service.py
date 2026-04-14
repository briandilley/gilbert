"""Tests for the OCR service (backend-agnostic).

Backend-specific tests for TesseractOCR live with the tesseract plugin
under ``std-plugins/tesseract/tests/``.
"""

from unittest.mock import AsyncMock

from gilbert.core.services.ocr import OCRService
from gilbert.interfaces.ocr import OCRBackend


async def test_service_delegates_to_backend() -> None:
    mock_backend = AsyncMock(spec=OCRBackend)
    mock_backend.available = True
    mock_backend.extract_text = AsyncMock(return_value="hello world")
    mock_backend.backend_config_params.return_value = []

    svc = OCRService()
    svc._backend = mock_backend
    svc._enabled = True
    assert svc.available is True

    result = await svc.extract_text(b"image data")
    assert result == "hello world"
    mock_backend.extract_text.assert_awaited_once_with(b"image data")


def test_service_info() -> None:
    svc = OCRService()
    info = svc.service_info()
    assert info.name == "ocr"
    assert "ocr" in info.capabilities
    assert info.toggleable is True


def test_service_config_includes_backend_choice() -> None:
    svc = OCRService()
    params = svc.config_params()
    keys = [p.key for p in params]
    assert "backend" in keys
