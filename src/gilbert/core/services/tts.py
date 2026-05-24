"""TTS service — wraps a TTSBackend as a discoverable service.

Adds backend-agnostic silence padding to synthesized audio so speakers
don't cut off the last word.
"""

import asyncio
import base64
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from gilbert.core.output import cleanup_old_files, get_output_dir
from gilbert.core.services._backend_actions import (
    all_backend_actions,
    invoke_backend_action,
)
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)
from gilbert.interfaces.tts import (
    AudioFormat,
    BidirectionalTTSCapability,
    StreamingTTSCapability,
    SynthesisRequest,
    SynthesisResult,
    TTSAudioChunk,
    TTSBackend,
    TTSCapabilityError,
    TTSFlushed,
    TTSStream,
    TTSStreamConfig,
    TTSStreamError,
    TTSWordTiming,
    Voice,
    append_silence,
)

logger = logging.getLogger(__name__)


def _event_to_json(ev: object, fmt: AudioFormat) -> dict[str, Any]:
    """Encode a ``TTSEvent`` for the WS wire.

    Audio bytes are base64-encoded so the JSON frame stays text-safe.
    The ``fmt`` argument is the session's output format, embedded on
    audio frames so the SPA player knows how to decode."""
    if isinstance(ev, TTSAudioChunk):
        return {
            "type": "audio",
            "audio_b64": base64.b64encode(ev.audio).decode(),
            "format": fmt.value,
        }
    if isinstance(ev, TTSWordTiming):
        return {
            "type": "word",
            "word": ev.word,
            "start_seconds": ev.start_seconds,
            "end_seconds": ev.end_seconds,
        }
    if isinstance(ev, TTSFlushed):
        return {"type": "flushed", "at_seconds": ev.at_seconds}
    if isinstance(ev, TTSStreamError):
        return {"type": "error", "message": ev.message, "recoverable": ev.recoverable}
    return {"type": "unknown"}


@dataclass
class _ActiveTTSSession:
    """Per-WS-connection TTS session state. Held only on
    ``TTSService._sessions``, never as request-scoped attrs on ``self``."""

    session_id: str
    conn_id: str
    user_id: str
    mode: str                          # "oneshot" | "bidirectional"
    fmt: AudioFormat                   # session's output format (used by _event_to_json)
    primitive: TTSStream | None        # None for oneshot
    pump_task: asyncio.Task[None] | None = None


class TTSService(Service):
    """Exposes a TTSBackend as a service with text_to_speech capability."""

    def __init__(self) -> None:
        self._backend: TTSBackend | None = None
        self._backend_name: str = "elevenlabs"
        self._enabled: bool = False
        self._config: dict[str, object] = {}
        self._silence_padding: float = 3.0
        self._output_ttl_seconds: int = 3600
        self._resolver: ServiceResolver | None = None
        # ``ai_chat`` is an optional capability, but the service manager
        # doesn't honor optional deps for start order — so the AI service
        # may not be running yet when TTS starts and the at-start
        # injection silently misses. We retry on every synthesize until
        # it sticks (or the backend signals it doesn't care).
        self._ai_injected: bool = False
        # WS streaming sessions, keyed by session_id (UUID hex).
        self._sessions: dict[str, _ActiveTTSSession] = {}
        self._sessions_guard = asyncio.Lock()
        # Strong refs for fire-and-forget cleanup tasks scheduled
        # from sync close callbacks. Without these, the GC may
        # discard the Task before _close_session finishes.
        self._cleanup_tasks: set[asyncio.Task[None]] = set()

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="tts",
            capabilities=frozenset({"text_to_speech", "ai_tools", "ws_handlers"}),
            optional=frozenset({"configuration", "ai_chat"}),
            toggleable=True,
            toggle_description="Text-to-speech synthesis",
        )

    @property
    def backend(self) -> TTSBackend | None:
        return self._backend

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver
        config_svc = resolver.get_capability("configuration")
        section: dict[str, Any] = {}
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                section = config_svc.get_section(self.config_namespace)
                global_ttl = config_svc.get("output_ttl_seconds")
                if global_ttl is not None:
                    self._output_ttl_seconds = int(global_ttl)

        if not section.get("enabled", False):
            logger.info("TTS service disabled")
            return

        self._enabled = True

        self._config = section.get("settings", self._config)
        sp = section.get("silence_padding")
        if sp is not None:
            self._silence_padding = float(sp)

        backend_name = section.get("backend", "elevenlabs")
        self._backend_name = backend_name
        backends = TTSBackend.registered_backends()
        backend_cls = backends.get(backend_name)
        if backend_cls is None:
            raise ValueError(f"Unknown TTS backend: {backend_name}")
        self._backend = backend_cls()

        await self._backend.initialize(self._config)

        # Hand the backend an AI sampling provider if it wants one
        # (currently used by ElevenLabs to inject v3 audio tags via a
        # small model). May miss if the AI service hasn't started yet —
        # ``_ensure_ai_injection`` retries on each ``synthesize`` until
        # the provider becomes available.
        self._ensure_ai_injection()

        logger.info("TTS service started")

    def _ensure_ai_injection(self) -> None:
        """Lazily wire an AISamplingProvider into the backend.

        Idempotent. Retries each call until either (a) the backend
        doesn't satisfy ``AICapableTTSBackend`` (nothing to do, mark
        done) or (b) the AI service is up and we successfully inject.
        Once done, becomes a no-op.
        """
        if self._ai_injected:
            return
        if self._backend is None or self._resolver is None:
            return

        from gilbert.interfaces.ai import AISamplingProvider
        from gilbert.interfaces.tts import AICapableTTSBackend

        if not isinstance(self._backend, AICapableTTSBackend):
            self._ai_injected = True
            return

        ai_svc = self._resolver.get_capability("ai_chat")
        if isinstance(ai_svc, AISamplingProvider):
            self._backend.set_ai_sampling(ai_svc)
            self._ai_injected = True
            logger.info("TTS backend wired up with AI sampling provider")

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "tts"

    @property
    def config_category(self) -> str:
        return "Media"

    def config_params(self) -> list[ConfigParam]:
        params = [
            ConfigParam(
                key="silence_padding",
                type=ToolParameterType.NUMBER,
                description="Seconds of silence appended after synthesized audio.",
                default=3.0,
            ),
            ConfigParam(
                key="backend",
                type=ToolParameterType.STRING,
                description="TTS backend provider.",
                default="elevenlabs",
                restart_required=True,
                choices=tuple(TTSBackend.registered_backends().keys()),
            ),
        ]
        backends = TTSBackend.registered_backends()
        backend_cls = backends.get(self._backend_name)
        if backend_cls is not None:
            for bp in backend_cls.backend_config_params():
                params.append(
                    ConfigParam(
                        key=f"settings.{bp.key}",
                        type=bp.type,
                        description=bp.description,
                        default=bp.default,
                        restart_required=bp.restart_required,
                        sensitive=bp.sensitive,
                        choices=bp.choices,
                        choices_from=bp.choices_from,
                        multiline=bp.multiline,
                        ai_prompt=bp.ai_prompt,
                        backend_param=True,
                    )
                )
        return params

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._config = config.get("settings", self._config)
        sp = config.get("silence_padding")
        if sp is not None:
            self._silence_padding = float(sp)

    # --- ConfigActionProvider ---

    def config_actions(self) -> list[ConfigAction]:
        return all_backend_actions(
            registry=TTSBackend.registered_backends(),
            current_backend=self._backend,
        )

    async def invoke_config_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        return await invoke_backend_action(self._backend, key, payload)

    async def stop(self) -> None:
        # Tear down any open WS streaming sessions before closing the
        # backend — otherwise pump tasks would call into a closed backend
        # and the close callbacks would race with backend.close().
        async with self._sessions_guard:
            session_ids = list(self._sessions.keys())
        for sid in session_ids:
            await self._close_session(sid)
        if self._backend is not None:
            await self._backend.close()

    async def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        """Synthesize speech from text. Appends silence padding if configured."""
        if self._backend is None:
            raise RuntimeError("TTS service is not enabled")
        self._ensure_ai_injection()
        result = await self._backend.synthesize(request)
        if self._silence_padding > 0:
            padded = append_silence(result.audio, result.format, self._silence_padding)
            return SynthesisResult(
                audio=padded,
                format=result.format,
                duration_seconds=result.duration_seconds,
                characters_used=result.characters_used,
            )
        return result

    def synthesize_stream(
        self, request: SynthesisRequest,
    ) -> AsyncIterator[bytes]:
        """Synthesize speech as a stream of audio chunks.

        Synchronous ``def`` (not ``async def``) so the capability check
        raises at the call site rather than on the consumer's first
        ``async for``. An async generator body wouldn't execute until
        first ``__anext__``; consumers would then see ``TTSCapabilityError``
        mid-iteration, which is confusing.

        Streaming bypasses the service's ``silence_padding`` — that's a
        finished-buffer concept and streaming consumers manage their
        own tail.
        """
        if self._backend is None:
            raise RuntimeError("TTS service is not enabled")
        if not isinstance(self._backend, StreamingTTSCapability):
            raise TTSCapabilityError(
                f"backend {self._backend_name!r} does not support streaming synthesis"
            )
        self._ensure_ai_injection()
        return self._backend.synthesize_stream(request)

    async def open_stream(self, config: TTSStreamConfig) -> TTSStream:
        """Open a bidirectional TTS session. Raises ``TTSCapabilityError``
        if the active backend doesn't implement ``BidirectionalTTSCapability``."""
        if self._backend is None:
            raise RuntimeError("TTS service is not enabled")
        if not isinstance(self._backend, BidirectionalTTSCapability):
            raise TTSCapabilityError(
                f"backend {self._backend_name!r} does not support bidirectional streaming"
            )
        self._ensure_ai_injection()
        return await self._backend.open_stream(config)

    def supported_capabilities(self) -> frozenset[str]:
        """Report which TTS capabilities the active backend supports.

        Returns ``frozenset()`` when no backend is loaded. Otherwise
        always includes ``"batch"`` (every TTSBackend implements
        ``synthesize``), plus ``"streaming"`` and/or ``"bidirectional"``
        if the backend opts into the matching protocol.
        """
        if self._backend is None:
            return frozenset()
        caps = {"batch"}
        if isinstance(self._backend, StreamingTTSCapability):
            caps.add("streaming")
        if isinstance(self._backend, BidirectionalTTSCapability):
            caps.add("bidirectional")
        return frozenset(caps)

    async def list_voices(self) -> list[Voice]:
        """List available voices from the backend."""
        if self._backend is None:
            raise RuntimeError("TTS service is not enabled")
        return await self._backend.list_voices()

    # --- ToolProvider protocol ---

    @property
    def tool_provider_name(self) -> str:
        return "tts"

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        return [
            ToolDefinition(
                name="synthesize",
                slash_group="tts",
                slash_command="synthesize",
                slash_help=(
                    "Synthesize speech to an MP3 file (does NOT play on "
                    "speakers — use /speaker announce for that): "
                    '/tts synthesize "<text>"'
                ),
                description=(
                    "Synthesize speech from text and save as an MP3 file. "
                    "This only generates an audio file — it does NOT play it on speakers. "
                    "To speak text out loud on speakers, use the 'announce' tool instead."
                ),
                parameters=[
                    ToolParameter(
                        name="text",
                        type=ToolParameterType.STRING,
                        description="The text to speak.",
                    ),
                ],
                required_role="everyone",
                # Each call writes its output to a UUID-named MP3, so
                # filenames never collide across concurrent callers and
                # the output-dir cleanup step is idempotent. External
                # TTS providers handle independent HTTP calls fine —
                # fan-out here is what makes multi-speaker announces
                # snappy, since announce() synthesizes per target.
                parallel_safe=True,
            ),
            ToolDefinition(
                name="list_voices",
                slash_group="tts",
                slash_command="voices",
                slash_help="List available TTS voices: /tts voices",
                description="List all available TTS voices from the provider.",
                required_role="everyone",
                parallel_safe=True,
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        match name:
            case "synthesize":
                return await self._tool_synthesize(arguments)
            case "list_voices":
                return await self._tool_list_voices()
            case _:
                raise KeyError(f"Unknown tool: {name}")

    async def _tool_synthesize(self, arguments: dict[str, Any]) -> str:
        text = arguments["text"]
        request = SynthesisRequest(text=text, voice_id="", output_format=AudioFormat.MP3)
        result = await self.synthesize(request)

        output_dir = get_output_dir("tts")
        cleanup_old_files(output_dir, self._output_ttl_seconds)

        file_path = output_dir / f"{uuid.uuid4()}.mp3"
        file_path.write_bytes(result.audio)

        return json.dumps(
            {
                "file_path": str(file_path),
                "format": "mp3",
                "duration_seconds": result.duration_seconds,
                "characters_used": result.characters_used,
            }
        )

    async def _tool_list_voices(self) -> str:
        voices = await self.list_voices()
        return json.dumps(
            [
                {
                    "voice_id": v.voice_id,
                    "name": v.name,
                    "language": v.language,
                    "description": v.description,
                }
                for v in voices
            ]
        )

    # --- WsHandlerProvider --------------------------------------------

    def get_ws_handlers(self) -> dict[str, Any]:
        return {
            "tts.start_stream":  self._handle_start_stream,
            "tts.send_text":     self._handle_send_text,
            "tts.flush":         self._handle_flush,
            "tts.close_stream":  self._handle_close_stream,
        }

    async def _handle_start_stream(
        self, conn: Any, frame: dict[str, Any],
    ) -> dict[str, Any]:
        """Open a TTS stream session. Mode is ``oneshot`` (text in frame,
        audio out via tts.event) or ``bidirectional`` (push text via
        tts.send_text and tts.flush, audio out via tts.event)."""
        import contextvars

        mode = frame.get("mode", "oneshot")
        fmt = AudioFormat(frame.get("format", "mp3"))
        voice_id = str(frame.get("voice_id", ""))
        speed = float(frame.get("speed", 1.0))
        context = str(frame.get("context", ""))
        sample_rate = int(frame.get("sample_rate", 44100))
        session_id = uuid.uuid4().hex

        if mode == "oneshot":
            text = str(frame.get("text", ""))
            request = SynthesisRequest(
                text=text,
                voice_id=voice_id,
                output_format=fmt,
                speed=speed,
                context=context,
            )
            record = _ActiveTTSSession(
                session_id=session_id,
                conn_id=conn.connection_id,
                user_id=conn.user_id or "",
                mode=mode,
                fmt=fmt,
                primitive=None,
            )
            async with self._sessions_guard:
                self._sessions[session_id] = record

            conn.add_close_callback(lambda sid=session_id: self._schedule_session_cleanup(sid))

            ctx = contextvars.copy_context()
            record.pump_task = asyncio.create_task(
                self._pump_oneshot(conn, record, request),
                name=f"tts-pump-oneshot-{session_id}",
                context=ctx,
            )
            return {"session_id": session_id}

        if mode == "bidirectional":
            cfg = TTSStreamConfig(
                voice_id=voice_id, output_format=fmt, speed=speed,
                context=context, sample_rate=sample_rate,
            )
            try:
                primitive = await self.open_stream(cfg)
            except TTSCapabilityError as e:
                return {"ok": False, "error": str(e)}
            record = _ActiveTTSSession(
                session_id=session_id,
                conn_id=conn.connection_id,
                user_id=conn.user_id or "",
                mode=mode,
                fmt=fmt,
                primitive=primitive,
            )
            async with self._sessions_guard:
                self._sessions[session_id] = record

            conn.add_close_callback(lambda sid=session_id: self._schedule_session_cleanup(sid))
            ctx = contextvars.copy_context()
            record.pump_task = asyncio.create_task(
                self._pump_bidirectional(conn, record),
                name=f"tts-pump-bidi-{session_id}",
                context=ctx,
            )
            return {"session_id": session_id}

        return {"ok": False, "error": f"unknown stream mode {mode!r}"}

    async def _pump_oneshot(
        self,
        conn: Any,
        rec: _ActiveTTSSession,
        request: SynthesisRequest,
    ) -> None:
        """Drain the backend's chunk iterator, emit ``tts.event`` frames,
        then a single ``end`` event. Capability errors become a single
        ``error`` event. Always cleans up the session record."""
        try:
            # synthesize_stream is a sync call that raises TTSCapabilityError
            # immediately if the backend doesn't support streaming.
            chunk_iter = self.synthesize_stream(request)
            async for chunk in chunk_iter:
                # synthesize_stream yields raw bytes; wrap as TTSAudioChunk so
                # the wire frame format matches the bidirectional path (which
                # already emits TTSAudioChunk events from the primitive).
                conn.enqueue({
                    "type": "tts.event",
                    "session_id": rec.session_id,
                    "event": _event_to_json(TTSAudioChunk(audio=chunk), rec.fmt),
                })
            conn.enqueue({
                "type": "tts.event",
                "session_id": rec.session_id,
                "event": {"type": "end"},
            })
        except TTSCapabilityError as e:
            conn.enqueue({
                "type": "tts.event",
                "session_id": rec.session_id,
                "event": {"type": "error", "message": str(e), "recoverable": False},
            })
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("tts oneshot pump error for session %s", rec.session_id)
            conn.enqueue({
                "type": "tts.event",
                "session_id": rec.session_id,
                "event": {"type": "error", "message": str(e), "recoverable": False},
            })
        finally:
            async with self._sessions_guard:
                self._sessions.pop(rec.session_id, None)

    def _schedule_session_cleanup(self, session_id: str) -> None:
        """Sync entry point for ``WsConnection.add_close_callback``.

        Schedules ``_close_session`` as a tracked background task so the
        GC can't drop the Task mid-execution. The task is removed from
        ``_cleanup_tasks`` on completion via ``add_done_callback``.
        """
        t = asyncio.create_task(self._close_session(session_id))
        self._cleanup_tasks.add(t)
        t.add_done_callback(self._cleanup_tasks.discard)

    async def _close_session(self, session_id: str) -> None:
        """Tear down a session: cancel pump, close primitive, drop record."""
        async with self._sessions_guard:
            rec = self._sessions.pop(session_id, None)
        if rec is None:
            return
        if rec.pump_task is not None and not rec.pump_task.done():
            rec.pump_task.cancel()
        if rec.primitive is not None:
            try:
                await rec.primitive.close()
            except Exception:  # noqa: BLE001
                logger.exception("error closing TTS primitive for session %s", session_id)

    async def _pump_bidirectional(self, conn: Any, rec: _ActiveTTSSession) -> None:
        """Drain ``primitive.events()`` and push ``tts.event`` frames.
        Cleanup happens via ``_close_session`` (on socket drop or explicit
        close), not here — the pump just relays events."""
        assert rec.primitive is not None
        try:
            async for ev in rec.primitive.events():
                conn.enqueue({
                    "type": "tts.event",
                    "session_id": rec.session_id,
                    "event": _event_to_json(ev, rec.fmt),
                })
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("tts bidi pump error for session %s", rec.session_id)
            conn.enqueue({
                "type": "tts.event",
                "session_id": rec.session_id,
                "event": {"type": "error", "message": str(e), "recoverable": False},
            })

    async def _handle_send_text(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        sid = frame.get("session_id")
        text = frame.get("text")
        if not isinstance(sid, str) or not isinstance(text, str):
            return {"ok": False, "error": "missing session_id or text"}
        rec = self._sessions.get(sid)
        if rec is None or rec.conn_id != conn.connection_id or rec.primitive is None:
            return {"ok": False, "error": "unknown session"}
        await rec.primitive.send_text(text)
        return {"ok": True}

    async def _handle_flush(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        sid = frame.get("session_id")
        if not isinstance(sid, str):
            return {"ok": False, "error": "missing session_id"}
        rec = self._sessions.get(sid)
        if rec is None or rec.conn_id != conn.connection_id or rec.primitive is None:
            return {"ok": False, "error": "unknown session"}
        await rec.primitive.flush()
        return {"ok": True}

    async def _handle_close_stream(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        sid = frame.get("session_id")
        if not isinstance(sid, str):
            return {"ok": False, "error": "missing session_id"}
        rec = self._sessions.get(sid)
        if rec is not None and rec.conn_id != conn.connection_id:
            return {"ok": False, "error": "unknown session"}
        await self._close_session(sid)
        return {"ok": True}
