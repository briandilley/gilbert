"""Transcription service — aggregates batch / streaming / wake-word backends.

Mirrors the multi-backend SpeakerService template: one ``Service``
instance owns multiple backend instances (one per role), exposes a
default-per-role + per-call override routing API, and provides WS RPC
handlers for browser-mic sessions.

Side-effect imports for bundled vendor-free backends live inside
``start()`` / ``config_params()`` (see SpeakerService for the pattern).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from gilbert.interfaces.auth import AccessControlProvider, UserContext
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import ToolDefinition, ToolParameter, ToolParameterType
from gilbert.interfaces.transcription import (
    BatchTranscriptionBackend,
    StreamConfig,
    StreamingTranscriptionBackend,
    TranscriptionRequest,
    TranscriptionResult,
    TranscriptionStream,
    WakeWordBackend,
    WakeWordConfig,
    WakeWordDetector,
)

logger = logging.getLogger(__name__)


def _event_to_json(ev: Any) -> dict[str, Any]:
    """Encode a TranscriptionEvent or WakeEvent for the wire."""
    from gilbert.interfaces.transcription import (
        FinalTranscript,
        PartialTranscript,
        SpeechEnded,
        SpeechStarted,
        TranscriptionError,
        WakeEvent,
    )

    if isinstance(ev, PartialTranscript):
        return {"type": "partial", "text": ev.text, "speaker_label": ev.speaker_label,
                "start_seconds": ev.start_seconds}
    if isinstance(ev, FinalTranscript):
        return {"type": "final", "text": ev.text, "start_seconds": ev.start_seconds,
                "end_seconds": ev.end_seconds, "speaker_label": ev.speaker_label,
                "confidence": ev.confidence}
    if isinstance(ev, SpeechStarted):
        return {"type": "speech_started", "at_seconds": ev.at_seconds}
    if isinstance(ev, SpeechEnded):
        return {"type": "speech_ended", "at_seconds": ev.at_seconds}
    if isinstance(ev, TranscriptionError):
        return {"type": "error", "message": ev.message, "recoverable": ev.recoverable}
    if isinstance(ev, WakeEvent):
        return {"type": "wake", "keyword": ev.keyword, "at_seconds": ev.at_seconds,
                "confidence": ev.confidence}
    return {"type": "unknown"}


@dataclass
class _ActiveSession:
    """Per-WS-connection transcription session.

    Held only on the service singleton in ``self._sessions[session_id]`` —
    never as request-scoped state on ``self``.
    """

    session_id: str
    conn_id: str
    user_id: str
    mode: str                       # "stream" | "wake_word"
    primitive: TranscriptionStream | WakeWordDetector
    pump_task: asyncio.Task[None] | None = None


class TranscriptionService(Service):
    """Aggregator over Batch/Streaming/WakeWord backends plus browser-mic plumbing."""

    def __init__(self) -> None:
        # Loaded backends, keyed by backend_name within each role.
        self._batch_backends: dict[str, BatchTranscriptionBackend] = {}
        self._streaming_backends: dict[str, StreamingTranscriptionBackend] = {}
        self._wake_word_backends: dict[str, WakeWordBackend] = {}
        self._default_batch: str = ""
        self._default_streaming: str = ""
        self._default_wake_word: str = ""
        self._enabled: bool = False
        self._output_ttl_seconds: int = 3600
        # Per-WS-connection active sessions. Keyed by session_id (UUID),
        # which a single conn_id may hold several of.
        self._sessions: dict[str, _ActiveSession] = {}
        self._sessions_guard = asyncio.Lock()
        # Per-role startup failures so the settings UI can show them.
        self._startup_failures: dict[str, dict[str, str]] = {
            "batch": {}, "streaming": {}, "wake_word": {},
        }
        self._resolver: ServiceResolver | None = None
        self._event_bus_provider: Any = None
        self._access_control: AccessControlProvider | None = None

    # --- Service ----------------------------------------------------

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="transcription",
            capabilities=frozenset({"speech_to_text", "ai_tools", "ws_handlers"}),
            optional=frozenset({"configuration", "event_bus", "access_control"}),
            toggleable=True,
            toggle_description="Speech-to-text transcription",
        )

    # --- Configurable ----------------------------------------------

    @property
    def config_namespace(self) -> str:
        return "transcription"

    @property
    def config_category(self) -> str:
        return "Media"

    def config_params(self) -> list[ConfigParam]:
        # Side-effect import so bundled backends are registered before
        # we enumerate them. Guarded so the service is still importable
        # while LocalWhisperBackend is being implemented (Task 12) and
        # so it stays resilient if the bundled module is ever removed.
        try:
            import gilbert.integrations.local_whisper  # type: ignore[import-untyped]  # noqa: F401
        except ImportError:
            pass

        batch_choices = tuple(BatchTranscriptionBackend.registered_backends().keys())
        streaming_choices = tuple(StreamingTranscriptionBackend.registered_backends().keys())
        wake_choices = tuple(WakeWordBackend.registered_backends().keys())

        params: list[ConfigParam] = [
            ConfigParam(
                key="output_ttl_seconds",
                type=ToolParameterType.NUMBER,
                description="Seconds before transient transcript files are cleaned up.",
                default=3600,
            ),
            ConfigParam(
                key="batch.default",
                type=ToolParameterType.STRING,
                description="Default backend for batch (file) transcription.",
                default=batch_choices[0] if batch_choices else "",
                choices=batch_choices,
            ),
            ConfigParam(
                key="streaming.default",
                type=ToolParameterType.STRING,
                description="Default backend for streaming transcription.",
                default=streaming_choices[0] if streaming_choices else "",
                choices=streaming_choices,
            ),
            ConfigParam(
                key="wake_word.default",
                type=ToolParameterType.STRING,
                description="Default wake-word backend.",
                default=wake_choices[0] if wake_choices else "",
                choices=wake_choices,
            ),
            ConfigParam(
                key="wake_word.sensitivity",
                type=ToolParameterType.NUMBER,
                description=(
                    "Default wake-word detection sensitivity (0..1). "
                    "Higher fires more often, lower requires a cleaner "
                    "match. Applies to whichever wake-word backend is "
                    "active. Used by consumers (voice-agent, …) when "
                    "they ask the transcription service to open a "
                    "detector."
                ),
                default=0.5,
            ),
        ]

        # Per-backend settings flattened into dotted keys, one block per role.
        for role, registry in (
            ("batch", BatchTranscriptionBackend.registered_backends()),
            ("streaming", StreamingTranscriptionBackend.registered_backends()),
            ("wake_word", WakeWordBackend.registered_backends()),
        ):
            for name, cls in registry.items():
                # Per-backend enabled toggle (off by default for everything
                # except local_whisper — which we ship enabled so the
                # service is useful out of the box).
                params.append(
                    ConfigParam(
                        key=f"{role}.backends.{name}.enabled",
                        type=ToolParameterType.BOOLEAN,
                        description=f"Enable the {name!r} {role} backend.",
                        default=(role == "batch" and name == "local_whisper"),
                        restart_required=True,
                        # ``backend_param=True`` keeps this toggle visually
                        # grouped with its sibling ``settings.*`` fields in
                        # the Settings UI, instead of floating up into the
                        # generic "service params" section.
                        backend_param=True,
                    )
                )
                for bp in cls.backend_config_params():
                    params.append(
                        ConfigParam(
                            key=f"{role}.backends.{name}.settings.{bp.key}",
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
        self._apply_config_section(config)
        for role in ("batch", "streaming", "wake_word"):
            await self._reinit_backends_for_role(role)

    # --- Backends -------------------------------------------------

    @property
    def batch_backends(self) -> Mapping[str, BatchTranscriptionBackend]:
        return self._batch_backends

    @property
    def streaming_backends(self) -> Mapping[str, StreamingTranscriptionBackend]:
        return self._streaming_backends

    @property
    def wake_word_backends(self) -> Mapping[str, WakeWordBackend]:
        return self._wake_word_backends

    # --- Internal config plumbing ---------------------------------

    def _apply_config_section(self, section: dict[str, Any]) -> None:
        """Cache the resolved transcription config section."""
        self._config_section = section
        if not isinstance(section, dict):
            return
        out_ttl = section.get("output_ttl_seconds")
        if out_ttl is not None:
            self._output_ttl_seconds = int(out_ttl)
        for role in ("batch", "streaming", "wake_word"):
            sub = section.get(role, {})
            if isinstance(sub, dict):
                default = sub.get("default")
                if isinstance(default, str):
                    setattr(self, f"_default_{role}", default)

    def _role_registry(self, role: str) -> dict[str, type]:
        if role == "batch":
            return BatchTranscriptionBackend.registered_backends()
        if role == "streaming":
            return StreamingTranscriptionBackend.registered_backends()
        if role == "wake_word":
            return WakeWordBackend.registered_backends()
        raise ValueError(f"unknown role {role!r}")

    def _role_loaded(self, role: str) -> dict[str, Any]:
        if role == "batch":
            return self._batch_backends
        if role == "streaming":
            return self._streaming_backends
        if role == "wake_word":
            return self._wake_word_backends
        raise ValueError(f"unknown role {role!r}")

    async def _reinit_backends_for_role(self, role: str) -> None:
        """Reconcile loaded backends for ``role`` against the latest config."""
        section = getattr(self, "_config_section", {}) or {}
        sub = section.get(role, {})
        backends_cfg = sub.get("backends", {}) if isinstance(sub, dict) else {}
        if not isinstance(backends_cfg, dict):
            backends_cfg = {}
        loaded = self._role_loaded(role)
        registry = self._role_registry(role)
        for name, cls in registry.items():
            cfg = backends_cfg.get(name, {})
            if not isinstance(cfg, dict):
                cfg = {}
            enabled = cfg.get("enabled", False) is True
            existing = loaded.get(name)
            if not enabled:
                if existing is not None:
                    try:
                        await existing.close()
                    except Exception:  # noqa: BLE001
                        logger.exception("error closing %s backend %r", role, name)
                    loaded.pop(name, None)
                self._startup_failures[role].pop(name, None)
                continue
            settings = cfg.get("settings", {}) if isinstance(cfg, dict) else {}
            if not isinstance(settings, dict):
                settings = {}
            if existing is None:
                try:
                    inst = cls()
                    await inst.initialize(settings)
                except Exception as exc:  # noqa: BLE001
                    self._startup_failures[role][name] = repr(exc)
                    logger.exception("failed to initialize %s backend %r", role, name)
                    continue
                loaded[name] = inst
                self._startup_failures[role].pop(name, None)
            # Already loaded: leave as-is.

    # --- Public API: BatchTranscriber -----------------------------------

    async def transcribe(
        self,
        request: TranscriptionRequest,
        backend: str | None = None,
    ) -> TranscriptionResult:
        """Transcribe audio via the configured batch backend."""
        name = backend or self._default_batch
        if not name or name not in self._batch_backends:
            # If no default but only one is loaded, use it.
            if len(self._batch_backends) == 1:
                name = next(iter(self._batch_backends))
            else:
                raise RuntimeError(
                    f"no transcription backend available for batch "
                    f"(asked for {backend!r}, default={self._default_batch!r}, "
                    f"loaded={sorted(self._batch_backends)})"
                )
        return await self._batch_backends[name].transcribe(request)

    # --- Public API: StreamingTranscriber + WakeWordListener -----

    async def open_stream(
        self,
        config: StreamConfig,
        backend: str | None = None,
    ) -> TranscriptionStream:
        """Open a streaming transcription session."""
        name = backend or self._default_streaming
        if not name or name not in self._streaming_backends:
            # If no default but only one is loaded, use it.
            if len(self._streaming_backends) == 1:
                name = next(iter(self._streaming_backends))
            else:
                raise RuntimeError(
                    f"no transcription backend available for streaming "
                    f"(asked for {backend!r}, default={self._default_streaming!r})"
                )
        return await self._streaming_backends[name].open_stream(config)

    async def open_detector(
        self,
        config: WakeWordConfig,
        backend: str | None = None,
    ) -> WakeWordDetector:
        """Open a wake-word detection session."""
        name = backend or self._default_wake_word
        if not name or name not in self._wake_word_backends:
            # If no default but only one is loaded, use it.
            if len(self._wake_word_backends) == 1:
                name = next(iter(self._wake_word_backends))
            else:
                raise RuntimeError(
                    f"no transcription backend available for wake_word "
                    f"(asked for {backend!r}, default={self._default_wake_word!r})"
                )
        return await self._wake_word_backends[name].open_detector(config)

    def list_backends(self, role: str | None = None) -> dict[str, list[str]]:
        """Return loaded backend names per role.

        With ``role=None`` returns all three roles. With ``role`` set,
        returns only that role's entry.
        """
        all_roles = {
            "batch": sorted(self._batch_backends),
            "streaming": sorted(self._streaming_backends),
            "wake_word": sorted(self._wake_word_backends),
        }
        if role is None:
            return all_roles
        return {role: all_roles[role]}

    # --- WsHandlerProvider ---------------------------------------

    def get_ws_handlers(self) -> dict[str, Any]:
        return {
            "transcription.start_session": self._handle_start_session,
            "transcription.send_chunk":    self._handle_send_chunk,
            "transcription.close_session": self._handle_close_session,
        }

    def _parse_audio_format(self, raw: dict[str, Any]) -> Any:
        from gilbert.interfaces.transcription import AudioEncoding, AudioFormat

        encoding = AudioEncoding(raw.get("encoding", "pcm_s16le"))
        return AudioFormat(
            encoding=encoding,
            sample_rate=int(raw.get("sample_rate", 16000)),
            channels=int(raw.get("channels", 1)),
        )

    async def _handle_start_session(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        """Open a browser-mic session and register a close-callback for cleanup."""
        import uuid

        from gilbert.interfaces.transcription import StreamConfig, WakeWordConfig

        mode = frame.get("mode", "stream")
        fmt = self._parse_audio_format(frame.get("format", {}))
        backend_name = frame.get("backend")
        sub = frame.get("config", {}) if isinstance(frame.get("config"), dict) else {}

        if mode == "stream":
            cfg = StreamConfig(
                format=fmt,
                language=sub.get("language"),
                prompt=sub.get("prompt", ""),
                diarize=bool(sub.get("diarize", False)),
                interim_results=bool(sub.get("interim_results", True)),
                vad_events=bool(sub.get("vad_events", True)),
            )
            primitive: TranscriptionStream | WakeWordDetector = await self.open_stream(
                cfg, backend=backend_name
            )
        elif mode == "wake_word":
            cfg2 = WakeWordConfig(
                keywords=list(sub.get("keywords", [])),
                format=fmt,
                sensitivity=float(sub.get("sensitivity", 0.5)),
            )
            primitive = await self.open_detector(cfg2, backend=backend_name)
        else:
            return {"ok": False, "error": f"unknown session mode {mode!r}"}

        session_id = uuid.uuid4().hex
        record = _ActiveSession(
            session_id=session_id,
            conn_id=conn.connection_id,
            user_id=conn.user_id or "",
            mode=mode,
            primitive=primitive,
        )
        async with self._sessions_guard:
            self._sessions[session_id] = record

        # Connection-drop cleanup. add_close_callback expects a sync
        # callable; we schedule the async cleanup as a task. Matches
        # SpeakerService._ws_browser_speaker_activate pattern at
        # core/services/speaker.py:1399.
        def _on_close(sid: str = session_id) -> None:
            asyncio.create_task(self._close_session(sid))

        conn.add_close_callback(_on_close)
        # Pump task is created lazily on first chunk in Task 10.
        return {"session_id": session_id}

    async def _handle_close_session(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        sid = frame.get("session_id")
        if not isinstance(sid, str):
            return {"ok": False, "error": "missing session_id"}
        await self._close_session(sid)
        return {"ok": True}

    async def _close_session(self, session_id: str) -> None:
        async with self._sessions_guard:
            rec = self._sessions.pop(session_id, None)
        if rec is None:
            return
        if rec.pump_task is not None:
            rec.pump_task.cancel()
        try:
            await rec.primitive.close()
        except Exception:  # noqa: BLE001
            logger.exception("error closing primitive for session %s", session_id)

    async def _handle_send_chunk(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        import base64

        sid = frame.get("session_id")
        b64 = frame.get("audio_b64")
        if not isinstance(sid, str) or not isinstance(b64, str):
            return {"ok": False, "error": "missing session_id or audio_b64"}
        rec = self._sessions.get(sid)
        if rec is None or rec.conn_id != conn.connection_id:
            return {"ok": False, "error": "unknown session"}
        try:
            chunk = base64.b64decode(b64)
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "invalid base64"}

        # Lazy-start the pump on first chunk. Spawn under a copied
        # context so logging / trace context follow the task.
        if rec.pump_task is None or rec.pump_task.done():
            import contextvars

            ctx = contextvars.copy_context()
            rec.pump_task = asyncio.create_task(
                self._pump_events(conn, rec),
                name=f"transcription-pump-{sid}",
                context=ctx,
            )
        await rec.primitive.send(chunk)
        return {"ok": True}

    async def _pump_events(self, conn: Any, rec: _ActiveSession) -> None:
        """Drain the primitive's event stream and push server-initiated frames."""
        try:
            async for ev in rec.primitive.events():
                conn.enqueue({
                    "type": "transcription.event",
                    "session_id": rec.session_id,
                    "event": _event_to_json(ev),
                })
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("transcription pump error for session %s", rec.session_id)

    # --- ToolProvider --------------------------------------------

    @property
    def tool_provider_name(self) -> str:
        return "transcription"

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        return [
            ToolDefinition(
                name="transcribe",
                slash_group="transcription",
                slash_command="transcribe",
                slash_help='Transcribe an audio file: /transcription transcribe "<path or url>"',
                description=(
                    "Transcribe audio from a file path or URL. Writes the "
                    "transcript to an output file and returns the text plus "
                    "per-segment timings."
                ),
                parameters=[
                    ToolParameter(
                        name="source",
                        type=ToolParameterType.STRING,
                        description="Path to a local audio file, or an http(s) URL.",
                    ),
                    ToolParameter(
                        name="language",
                        type=ToolParameterType.STRING,
                        description="Optional language hint (BCP-47, e.g. 'en').",
                        required=False,
                    ),
                    ToolParameter(
                        name="diarize",
                        type=ToolParameterType.BOOLEAN,
                        description="Attempt speaker diarization if the backend supports it.",
                        required=False,
                    ),
                    ToolParameter(
                        name="backend",
                        type=ToolParameterType.STRING,
                        description="Override the default batch backend by name.",
                        required=False,
                    ),
                ],
                required_role="everyone",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="backends",
                slash_group="transcription",
                slash_command="backends",
                slash_help="List loaded transcription backends: /transcription backends [role]",
                description="List currently-loaded transcription backends per role.",
                parameters=[
                    ToolParameter(
                        name="role",
                        type=ToolParameterType.STRING,
                        description="Optional role filter: batch | streaming | wake_word.",
                        required=False,
                    ),
                ],
                required_role="everyone",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="languages",
                slash_group="transcription",
                slash_command="languages",
                slash_help="List supported languages: /transcription languages [backend]",
                description="Best-effort list of supported language codes for a batch backend.",
                parameters=[
                    ToolParameter(
                        name="backend",
                        type=ToolParameterType.STRING,
                        description="Backend name (defaults to the configured batch default).",
                        required=False,
                    ),
                ],
                required_role="everyone",
                parallel_safe=True,
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        match name:
            case "transcribe":
                return await self._tool_transcribe(arguments)
            case "backends":
                return await self._tool_backends(arguments)
            case "languages":
                return await self._tool_languages(arguments)
            case _:
                raise KeyError(f"Unknown tool: {name}")

    async def _tool_transcribe(self, arguments: dict[str, Any]) -> str:
        import json
        import uuid
        from pathlib import Path

        from gilbert.core.output import cleanup_old_files, get_output_dir
        from gilbert.interfaces.transcription import (
            AudioEncoding,
            AudioFormat,
            TranscriptionRequest,
        )

        source = arguments["source"]
        language = arguments.get("language")
        diarize = bool(arguments.get("diarize", False))
        backend = arguments.get("backend") or None

        # Fetch the bytes.
        if source.startswith("http://") or source.startswith("https://"):
            import httpx

            async with httpx.AsyncClient() as client:
                resp = await client.get(source)
                resp.raise_for_status()
                audio = resp.content
        else:
            audio = Path(source).read_bytes()

        request = TranscriptionRequest(
            audio=audio,
            format=AudioFormat(AudioEncoding.AUTO),
            language=language,
            diarize=diarize,
        )
        result = await self.transcribe(request, backend=backend)

        out_dir = get_output_dir("transcription")
        cleanup_old_files(out_dir, self._output_ttl_seconds)
        out_path = out_dir / f"{uuid.uuid4().hex}.txt"
        out_path.write_text(result.text)

        return json.dumps({
            "file_path": str(out_path),
            "text": result.text,
            "segments": [
                {
                    "text": s.text,
                    "start": s.start_seconds,
                    "end": s.end_seconds,
                    "speaker_label": s.speaker_label,
                    "confidence": s.confidence,
                }
                for s in result.segments
            ],
            "language": result.language,
            "duration_seconds": result.duration_seconds,
        })

    async def _tool_backends(self, arguments: dict[str, Any]) -> str:
        import json

        role = arguments.get("role")
        return json.dumps(self.list_backends(role))

    async def _tool_languages(self, arguments: dict[str, Any]) -> str:
        import json

        backend_name = arguments.get("backend") or self._default_batch
        if not backend_name or backend_name not in self._batch_backends:
            return json.dumps([])
        langs = await self._batch_backends[backend_name].list_languages()
        return json.dumps(langs)

    # --- Lifecycle ------------------------------------------------

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver
        # Side-effect imports: register bundled vendor-free backends
        # before we ask the registries which to load. Guarded so the
        # service is still functional while LocalWhisperBackend is
        # being implemented in Task 12.
        try:
            import gilbert.integrations.local_whisper  # noqa: F401
        except ImportError:
            pass

        section: dict[str, Any] = {}
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                section = config_svc.get_section(self.config_namespace)

        if not section.get("enabled", False):
            logger.info("Transcription service disabled")
            return

        self._enabled = True
        self._apply_config_section(section)

        bus_svc = resolver.get_capability("event_bus")
        if bus_svc is not None:
            self._event_bus_provider = bus_svc
        acl_svc = resolver.get_capability("access_control")
        if isinstance(acl_svc, AccessControlProvider):
            self._access_control = acl_svc

        for role in ("batch", "streaming", "wake_word"):
            await self._reinit_backends_for_role(role)
        logger.info(
            "Transcription service started (batch=%s streaming=%s wake_word=%s)",
            sorted(self._batch_backends), sorted(self._streaming_backends),
            sorted(self._wake_word_backends),
        )

    async def stop(self) -> None:
        for bb in list(self._batch_backends.values()):
            try:
                await bb.close()
            except Exception:  # noqa: BLE001
                logger.exception("error closing transcription backend %r", bb)
        for sb in list(self._streaming_backends.values()):
            try:
                await sb.close()
            except Exception:  # noqa: BLE001
                logger.exception("error closing transcription backend %r", sb)
        for wb in list(self._wake_word_backends.values()):
            try:
                await wb.close()
            except Exception:  # noqa: BLE001
                logger.exception("error closing transcription backend %r", wb)

    # --- Config actions (Task 8) --------------------------------

    def config_actions(self) -> list[ConfigAction]:
        from gilbert.core.services._backend_actions import all_backend_actions

        actions: list[ConfigAction] = []
        for _role, registry, loaded in (
            ("batch", BatchTranscriptionBackend.registered_backends(), self._batch_backends),
            ("streaming", StreamingTranscriptionBackend.registered_backends(), self._streaming_backends),
            ("wake_word", WakeWordBackend.registered_backends(), self._wake_word_backends),
        ):
            # Picking *any* loaded instance is fine — actions are class-
            # level metadata. Loaded instance is used only for live invokes.
            current = next(iter(loaded.values()), None)
            actions.extend(all_backend_actions(registry=registry, current_backend=current))
        return actions

    async def invoke_config_action(
        self, key: str, payload: dict[str, Any]
    ) -> ConfigActionResult:
        from gilbert.core.services._backend_actions import invoke_backend_action

        # Try each role's loaded backends in turn. The action key's
        # backend-name prefix disambiguates which one to invoke.
        for loaded in (self._batch_backends, self._streaming_backends, self._wake_word_backends):
            for inst in loaded.values():
                result = await invoke_backend_action(inst, key, payload)
                # If the result is non-error, or if it's an error but not
                # the "doesn't support" sentinel, this backend owns the action.
                # Otherwise try the next one.
                msg_lower = (result.message or "").lower()
                if result.status != "error" or "doesn't support" not in msg_lower:
                    return result
        return ConfigActionResult(status="error", message=f"unknown action {key!r}")
