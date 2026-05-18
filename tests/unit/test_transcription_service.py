"""Unit tests for TranscriptionService."""

from gilbert.core.services.transcription import TranscriptionService
from gilbert.interfaces.service import ServiceInfo


def test_service_info_shape():
    svc = TranscriptionService()
    info = svc.service_info()
    assert isinstance(info, ServiceInfo)
    assert info.name == "transcription"
    assert "speech_to_text" in info.capabilities
    assert "ai_tools" in info.capabilities
    assert "ws_handlers" in info.capabilities
    assert "configuration" in info.optional
    assert "event_bus" in info.optional
    assert "access_control" in info.optional
    assert info.toggleable is True


def test_service_config_namespace_and_category():
    svc = TranscriptionService()
    assert svc.config_namespace == "transcription"
    assert svc.config_category == "Media"


def test_config_params_includes_role_defaults_and_global_keys():
    svc = TranscriptionService()
    params = svc.config_params()
    keys = {p.key for p in params}
    assert "batch.default" in keys
    assert "streaming.default" in keys
    assert "wake_word.default" in keys
    assert "output_ttl_seconds" in keys
