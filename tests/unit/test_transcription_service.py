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
    AudioEncoding,
    AudioFormat,
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


# --- Streaming and wake-word routing tests (Task 7) ---


@pytest.mark.asyncio
async def test_open_stream_returns_backend_primitive():
    svc = TranscriptionService()
    svc._apply_config_section({
        "streaming": {
            "default": "_fake_streaming",
            "backends": {"_fake_streaming": {"enabled": True}},
        },
    })
    await svc._reinit_backends_for_role("streaming")
    cfg = StreamConfig(format=AudioFormat(AudioEncoding.PCM_S16LE))
    stream = await svc.open_stream(cfg)
    assert isinstance(stream, TranscriptionStream)
    await stream.send(b"\x00\x00")
    await stream.close()


@pytest.mark.asyncio
async def test_open_detector_returns_backend_primitive():
    svc = TranscriptionService()
    svc._apply_config_section({
        "wake_word": {
            "default": "_fake_wake",
            "backends": {"_fake_wake": {"enabled": True}},
        },
    })
    await svc._reinit_backends_for_role("wake_word")
    det = await svc.open_detector(WakeWordConfig(
        keywords=["hey"], format=AudioFormat(AudioEncoding.PCM_S16LE)
    ))
    assert isinstance(det, WakeWordDetector)
    await det.close()


def test_list_backends_returns_loaded_per_role():
    svc = TranscriptionService()
    svc._batch_backends["a"] = _FakeBatch()
    svc._streaming_backends["b"] = _FakeStreaming()
    out = svc.list_backends()
    assert out == {"batch": ["a"], "streaming": ["b"], "wake_word": []}
    assert svc.list_backends("batch") == {"batch": ["a"]}


# --- Config actions tests (Task 8) ---


def test_config_actions_aggregate_from_backend_classes():
    """Service exposes backend-declared config actions, tagged by backend."""
    # _FakeBatch doesn't declare any actions, so we just verify the call
    # shape works and returns a list. A backend that DOES declare actions
    # (e.g., LocalWhisperBackend in Task 12 or third-party backends in
    # follow-up PRs) will populate this.
    svc = TranscriptionService()
    svc._batch_backends["_fake_batch"] = _FakeBatch()
    actions = svc.config_actions()
    assert isinstance(actions, list)


# --- WS session handler tests (Task 9) ---


@pytest.mark.asyncio
async def test_start_session_creates_record_and_close_drops_it():
    from gilbert.core.services.transcription import TranscriptionService

    svc = TranscriptionService()
    svc._apply_config_section({
        "streaming": {
            "default": "_fake_streaming",
            "backends": {"_fake_streaming": {"enabled": True}},
        },
    })
    await svc._reinit_backends_for_role("streaming")

    class _Conn:
        def __init__(self) -> None:
            self.connection_id = "conn-A"
            self.user_id = "u-1"
            self.user_ctx = type("U", (), {
                "user_id": "u-1", "display_name": "Bri",
                "roles": frozenset({"everyone"}),
            })()
            self.display_name = "Bri"
            self.enqueued: list[dict] = []
            self.close_callbacks: list = []
        def enqueue(self, msg):
            self.enqueued.append(msg)
        def add_close_callback(self, cb):
            self.close_callbacks.append(cb)

    conn = _Conn()
    frame = {
        "mode": "stream",
        "format": {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1},
        "config": {"interim_results": True},
    }
    result = await svc._handle_start_session(conn, frame)
    sid = result["session_id"]
    assert sid in svc._sessions
    assert svc._sessions[sid].conn_id == "conn-A"
    assert svc._sessions[sid].user_id == "u-1"
    assert svc._sessions[sid].mode == "stream"
    # A close callback was registered so connection-drop cleans up.
    assert len(conn.close_callbacks) == 1

    await svc._handle_close_session(conn, {"session_id": sid})
    assert sid not in svc._sessions


@pytest.mark.asyncio
async def test_connection_close_callback_cleans_up_sessions():
    from gilbert.core.services.transcription import TranscriptionService

    svc = TranscriptionService()
    svc._apply_config_section({
        "streaming": {
            "default": "_fake_streaming",
            "backends": {"_fake_streaming": {"enabled": True}},
        },
    })
    await svc._reinit_backends_for_role("streaming")

    class _Conn:
        def __init__(self) -> None:
            self.connection_id = "conn-B"
            self.user_id = "u-2"
            self.user_ctx = type("U", (), {
                "user_id": "u-2", "display_name": "Eve",
                "roles": frozenset({"everyone"}),
            })()
            self.display_name = "Eve"
            self.close_callbacks: list = []
        def enqueue(self, msg): pass
        def add_close_callback(self, cb):
            self.close_callbacks.append(cb)

    conn = _Conn()
    r1 = await svc._handle_start_session(conn, {
        "mode": "stream",
        "format": {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1},
        "config": {},
    })
    r2 = await svc._handle_start_session(conn, {
        "mode": "stream",
        "format": {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1},
        "config": {},
    })
    assert {r1["session_id"], r2["session_id"]} <= set(svc._sessions)

    # Simulate the WS connection closing: callbacks fire synchronously
    # in arbitrary order. The service's callbacks schedule async cleanup,
    # so we drain the loop after firing them.
    for cb in list(conn.close_callbacks):
        cb()
    # Drain pending tasks
    for _ in range(50):
        await asyncio.sleep(0)
        if r1["session_id"] not in svc._sessions and r2["session_id"] not in svc._sessions:
            break
    assert r1["session_id"] not in svc._sessions
    assert r2["session_id"] not in svc._sessions


@pytest.mark.asyncio
async def test_send_chunk_forwards_to_primitive_and_events_get_enqueued():
    import base64

    from gilbert.core.services.transcription import TranscriptionService

    svc = TranscriptionService()
    svc._apply_config_section({
        "streaming": {
            "default": "_fake_streaming",
            "backends": {"_fake_streaming": {"enabled": True}},
        },
    })
    await svc._reinit_backends_for_role("streaming")

    class _Conn:
        def __init__(self) -> None:
            self.connection_id = "conn-Z"
            self.user_id = "u-9"
            self.user_ctx = type("U", (), {
                "user_id": "u-9", "display_name": "Z",
                "roles": frozenset({"everyone"}),
            })()
            self.display_name = "Z"
            self.enqueued: list[dict] = []
            self.close_callbacks: list = []
        def enqueue(self, msg):
            self.enqueued.append(msg)
        def add_close_callback(self, cb):
            self.close_callbacks.append(cb)

    conn = _Conn()
    open_res = await svc._handle_start_session(conn, {
        "mode": "stream",
        "format": {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1},
        "config": {},
    })
    sid = open_res["session_id"]

    # Send two chunks; each produces a FinalTranscript on _FakeStream.
    payload_bytes = b"\x00\x00\x01\x00"
    chunk_b64 = base64.b64encode(payload_bytes).decode()
    await svc._handle_send_chunk(conn, {"session_id": sid, "audio_b64": chunk_b64})
    await svc._handle_send_chunk(conn, {"session_id": sid, "audio_b64": chunk_b64})

    # Let the pump task drain.
    for _ in range(50):
        await asyncio.sleep(0)
        events_so_far = [m for m in conn.enqueued if m.get("type") == "transcription.event"]
        if len(events_so_far) >= 2:
            break

    events = [m for m in conn.enqueued if m.get("type") == "transcription.event"]
    assert len(events) >= 2
    assert events[0]["session_id"] == sid
    assert events[0]["event"]["type"] == "final"
    assert events[0]["event"]["text"].startswith("chunk")

    await svc._handle_close_session(conn, {"session_id": sid})


@pytest.mark.asyncio
async def test_two_concurrent_sessions_do_not_cross_talk():
    import base64

    from gilbert.core.services.transcription import TranscriptionService

    svc = TranscriptionService()
    svc._apply_config_section({
        "streaming": {
            "default": "_fake_streaming",
            "backends": {"_fake_streaming": {"enabled": True}},
        },
    })
    await svc._reinit_backends_for_role("streaming")

    class _C:
        def __init__(self, cid: str, uid: str) -> None:
            self.connection_id = cid
            self.user_id = uid
            self.user_ctx = type("U", (), {
                "user_id": uid, "display_name": cid,
                "roles": frozenset({"everyone"}),
            })()
            self.display_name = cid
            self.enqueued: list[dict] = []
            self.close_callbacks: list = []
        def enqueue(self, msg):
            if msg.get("type") == "transcription.event":
                self.enqueued.append(msg)
        def add_close_callback(self, cb):
            self.close_callbacks.append(cb)

    a, b = _C("conn-A", "u-A"), _C("conn-B", "u-B")
    ra = await svc._handle_start_session(a, {
        "mode": "stream",
        "format": {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1},
        "config": {},
    })
    rb = await svc._handle_start_session(b, {
        "mode": "stream",
        "format": {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1},
        "config": {},
    })
    payload = base64.b64encode(b"\x00\x00").decode()
    await svc._handle_send_chunk(a, {"session_id": ra["session_id"], "audio_b64": payload})
    await svc._handle_send_chunk(b, {"session_id": rb["session_id"], "audio_b64": payload})

    for _ in range(50):
        await asyncio.sleep(0)
        if a.enqueued and b.enqueued:
            break

    assert all(m["session_id"] == ra["session_id"] for m in a.enqueued)
    assert all(m["session_id"] == rb["session_id"] for m in b.enqueued)
    await svc._handle_close_session(a, {"session_id": ra["session_id"]})
    await svc._handle_close_session(b, {"session_id": rb["session_id"]})
