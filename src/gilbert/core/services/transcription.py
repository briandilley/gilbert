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

from gilbert.interfaces.auth import AccessControlProvider
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.transcription import (
    BatchTranscriptionBackend,
    StreamingTranscriptionBackend,
    TranscriptionStream,
    WakeWordBackend,
    WakeWordDetector,
)

logger = logging.getLogger(__name__)


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
        """Apply config updates without a full service restart.

        Defers actual backend reinit until Task 5 wires ``_reinit_backends``.
        """
        out_ttl = config.get("output_ttl_seconds")
        if out_ttl is not None:
            self._output_ttl_seconds = int(out_ttl)
        for role in ("batch", "streaming", "wake_word"):
            section = config.get(role, {})
            if not isinstance(section, dict):
                continue
            default = section.get("default")
            if isinstance(default, str):
                setattr(self, f"_default_{role}", default)

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

    # --- Lifecycle (stubs — filled in in later tasks) -------------

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver

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

    # --- Config actions (stub — filled in in Task 8) --------------

    def config_actions(self) -> list[ConfigAction]:
        return []

    async def invoke_config_action(
        self, key: str, payload: dict[str, Any]
    ) -> ConfigActionResult:
        return ConfigActionResult(status="error", message=f"unknown action {key!r}")
