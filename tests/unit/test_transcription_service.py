"""Unit tests for TranscriptionService."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from gilbert.core.services.transcription import TranscriptionService
from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.service import ServiceInfo
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.transcription import (
    BatchTranscriptionBackend,
    FinalTranscript,
    StreamConfig,
    StreamingTranscriptionBackend,
    TranscriptionEvent,
    TranscriptionRequest,
    TranscriptionResult,
    TranscriptionStream,
    WakeEvent,
    WakeWordBackend,
    WakeWordConfig,
    WakeWordDetector,
)


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


# --- Fake backends and shared helpers for service tests ---------------


class _FakeBatch(BatchTranscriptionBackend):
    backend_name = "_fake_batch"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="api_key",
                type=ToolParameterType.STRING,
                description="API key.",
                default="",
                sensitive=True,
            ),
        ]

    def __init__(self) -> None:
        self.initialized_with: dict[str, object] | None = None
        self.closed = False
        self.calls: list[TranscriptionRequest] = []

    async def initialize(self, config: Any) -> None:
        self.initialized_with = dict(config)

    async def close(self) -> None:
        self.closed = True

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        self.calls.append(request)
        return TranscriptionResult(text="fake", language="en")


class _FakeStream(TranscriptionStream):
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.closed = False
        self._queue: asyncio.Queue[TranscriptionEvent | None] = asyncio.Queue()

    async def send(self, chunk: bytes) -> None:
        self.sent.append(chunk)
        await self._queue.put(FinalTranscript(
            text=f"chunk{len(self.sent)}", start_seconds=0.0, end_seconds=0.1,
        ))

    async def close(self) -> None:
        self.closed = True
        await self._queue.put(None)

    def events(self) -> AsyncIterator[TranscriptionEvent]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[TranscriptionEvent]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item


class _FakeStreaming(StreamingTranscriptionBackend):
    backend_name = "_fake_streaming"

    def __init__(self) -> None:
        self.opened: list[StreamConfig] = []
        self.streams: list[_FakeStream] = []

    async def initialize(self, config: Any) -> None:
        pass

    async def close(self) -> None:
        pass

    async def open_stream(self, config: StreamConfig) -> TranscriptionStream:
        self.opened.append(config)
        s = _FakeStream()
        self.streams.append(s)
        return s


class _FakeDetector(WakeWordDetector):
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self._queue: asyncio.Queue[WakeEvent | None] = asyncio.Queue()
        self.closed = False

    async def send(self, chunk: bytes) -> None:
        self.sent.append(chunk)

    async def close(self) -> None:
        self.closed = True
        await self._queue.put(None)

    def events(self) -> AsyncIterator[WakeEvent]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[WakeEvent]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item


class _FakeWake(WakeWordBackend):
    backend_name = "_fake_wake"

    def __init__(self) -> None:
        self.detectors: list[_FakeDetector] = []

    async def initialize(self, config: Any) -> None:
        pass

    async def close(self) -> None:
        pass

    async def open_detector(self, config: WakeWordConfig) -> WakeWordDetector:
        d = _FakeDetector()
        self.detectors.append(d)
        return d


# --- Backend loading tests -------------------------------------------


@pytest.mark.asyncio
async def test_start_loads_enabled_batch_backend():
    svc = TranscriptionService()
    svc._apply_config_section({
        "batch": {
            "default": "_fake_batch",
            "backends": {"_fake_batch": {"enabled": True, "settings": {"api_key": "k"}}},
        },
    })
    await svc._reinit_backends_for_role("batch")
    assert "_fake_batch" in svc.batch_backends
    backend = svc.batch_backends["_fake_batch"]
    assert isinstance(backend, _FakeBatch)
    assert backend.initialized_with == {"api_key": "k"}


@pytest.mark.asyncio
async def test_disabling_backend_closes_and_drops_it():
    svc = TranscriptionService()
    svc._apply_config_section({
        "batch": {"backends": {"_fake_batch": {"enabled": True, "settings": {}}}},
    })
    await svc._reinit_backends_for_role("batch")
    instance = svc.batch_backends["_fake_batch"]

    # Now flip it off and reinit.
    svc._apply_config_section({
        "batch": {"backends": {"_fake_batch": {"enabled": False}}},
    })
    await svc._reinit_backends_for_role("batch")
    assert "_fake_batch" not in svc.batch_backends
    assert instance.closed is True


@pytest.mark.asyncio
async def test_startup_failure_is_recorded_not_raised():
    """A backend that raises during initialize is recorded; service keeps running."""

    class _Boom(BatchTranscriptionBackend):
        backend_name = "_boom_batch"

        async def initialize(self, config: Any) -> None:
            raise RuntimeError("boom")

        async def close(self) -> None:
            pass

        async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
            raise NotImplementedError

    try:
        svc = TranscriptionService()
        svc._apply_config_section({
            "batch": {"backends": {"_boom_batch": {"enabled": True}}},
        })
        await svc._reinit_backends_for_role("batch")
        assert "_boom_batch" not in svc.batch_backends
        assert "_boom_batch" in svc._startup_failures["batch"]
        assert "boom" in svc._startup_failures["batch"]["_boom_batch"]
    finally:
        BatchTranscriptionBackend._registry.pop("_boom_batch", None)


# --- Batch routing tests (Task 6) ---


@pytest.mark.asyncio
async def test_transcribe_routes_to_default():
    svc = TranscriptionService()
    svc._apply_config_section({
        "batch": {
            "default": "_fake_batch",
            "backends": {"_fake_batch": {"enabled": True, "settings": {}}},
        },
    })
    await svc._reinit_backends_for_role("batch")
    result = await svc.transcribe(TranscriptionRequest(audio=b"\x00" * 10))
    assert result.text == "fake"
    assert svc.batch_backends["_fake_batch"].calls[0].audio == b"\x00" * 10


@pytest.mark.asyncio
async def test_transcribe_explicit_backend_overrides_default():
    class _OtherBatch(BatchTranscriptionBackend):
        backend_name = "_other_batch"

        def __init__(self):
            self.calls = []

        async def initialize(self, config):
            pass

        async def close(self):
            pass

        async def transcribe(self, request):
            self.calls.append(request)
            return TranscriptionResult(text="other")

    try:
        svc = TranscriptionService()
        svc._apply_config_section({
            "batch": {
                "default": "_fake_batch",
                "backends": {
                    "_fake_batch": {"enabled": True},
                    "_other_batch": {"enabled": True},
                },
            },
        })
        await svc._reinit_backends_for_role("batch")
        out = await svc.transcribe(TranscriptionRequest(audio=b"\x00"), backend="_other_batch")
        assert out.text == "other"
        # Default backend was NOT used
        assert svc.batch_backends["_fake_batch"].calls == []
    finally:
        BatchTranscriptionBackend._registry.pop("_other_batch", None)


@pytest.mark.asyncio
async def test_transcribe_raises_when_no_backend_available():
    svc = TranscriptionService()  # nothing loaded, no default
    with pytest.raises(RuntimeError, match="no transcription backend available"):
        await svc.transcribe(TranscriptionRequest(audio=b""))
