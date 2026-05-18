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
    TranscriptionRequest,
    TranscriptionResult,
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

    # --- Config actions (stub — filled in in Task 8) --------------

    def config_actions(self) -> list[ConfigAction]:
        return []

    async def invoke_config_action(
        self, key: str, payload: dict[str, Any]
    ) -> ConfigActionResult:
        return ConfigActionResult(status="error", message=f"unknown action {key!r}")
