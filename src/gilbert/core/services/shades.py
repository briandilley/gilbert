"""Shades service — wraps a ShadesBackend as a discoverable service.

Mirrors the lights service: backend-pluggable, capability-gated tools,
slash commands under the ``shades`` namespace.

Position convention: 0 = closed, 100 = open. Most physical shade
controllers use the same convention; backends are responsible for
converting if their hardware uses a different one.
"""

from __future__ import annotations

import logging
from typing import Any

from gilbert.core.services._backend_actions import (
    all_backend_actions,
    invoke_backend_action,
)
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
    ConfigurationReader,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.shades import ShadeInfo, ShadesBackend
from gilbert.interfaces.tools import ToolDefinition, ToolParameter, ToolParameterType

logger = logging.getLogger(__name__)


def _resolve(query: str, shades: list[ShadeInfo]) -> list[ShadeInfo]:
    q = query.lower().strip()
    if not q:
        return []
    by_area = [s for s in shades if s.area.lower() == q]
    if by_area:
        return by_area
    return [s for s in shades if q in s.name.lower()]


class ShadesService(Service):
    """Discover and control window shades through a ``ShadesBackend``."""

    def __init__(self) -> None:
        self._backend: ShadesBackend | None = None
        self._backend_name: str = ""
        self._enabled: bool = False
        self._config: dict[str, object] = {}

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="shades",
            capabilities=frozenset({"shades", "ai_tools"}),
            optional=frozenset({"configuration"}),
            toggleable=True,
            toggle_description="Window shade control",
        )

    @property
    def backend(self) -> ShadesBackend | None:
        return self._backend

    async def start(self, resolver: ServiceResolver) -> None:
        config_svc = resolver.get_capability("configuration")
        section: dict[str, Any] = {}
        if config_svc is not None and isinstance(config_svc, ConfigurationReader):
            section = config_svc.get_section(self.config_namespace)

        if not section.get("enabled", False):
            logger.info("Shades service disabled")
            return

        self._enabled = True
        self._config = section.get("settings", self._config)

        backend_name = str(section.get("backend", "") or "")
        self._backend_name = backend_name
        if not backend_name:
            logger.warning("Shades enabled but no backend selected")
            return

        backends = ShadesBackend.registered_backends()
        backend_cls = backends.get(backend_name)
        if backend_cls is None:
            logger.warning(
                "Unknown shades backend %r — registered: %s",
                backend_name,
                list(backends),
            )
            return

        self._backend = backend_cls()
        try:
            await self._backend.initialize(self._config)
        except Exception:
            logger.exception("Failed to initialize shades backend %s", backend_name)
            self._backend = None
            return

        logger.info("Shades service started (backend=%s)", backend_name)

    async def stop(self) -> None:
        if self._backend is not None:
            try:
                await self._backend.close()
            except Exception:
                logger.exception("Error closing shades backend")
            self._backend = None

    # --- Capability proxies ---

    @property
    def supports_position(self) -> bool:
        return bool(self._backend and self._backend.supports_position)

    @property
    def supports_stop(self) -> bool:
        return bool(self._backend and self._backend.supports_stop)

    # --- Configurable ---

    @property
    def config_namespace(self) -> str:
        return "shades"

    @property
    def config_category(self) -> str:
        return "Lighting"

    def config_params(self) -> list[ConfigParam]:
        params = [
            ConfigParam(
                key="backend",
                type=ToolParameterType.STRING,
                description="Shades backend type.",
                default="",
                restart_required=True,
                choices=tuple(ShadesBackend.registered_backends().keys()),
            ),
        ]
        backends = ShadesBackend.registered_backends()
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
        if self._backend is not None:
            try:
                await self._backend.initialize(self._config)
            except Exception:
                logger.exception("Failed to re-initialize shades backend after config change")

    # --- ConfigActionProvider ---

    def config_actions(self) -> list[ConfigAction]:
        return all_backend_actions(
            registry=ShadesBackend.registered_backends(),
            current_backend=self._backend,
        )

    async def invoke_config_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        return await invoke_backend_action(self._backend, key, payload)

    # --- ToolProvider ---

    @property
    def tool_provider_name(self) -> str:
        return "shades"

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        if not self._enabled or self._backend is None:
            return []

        tools: list[ToolDefinition] = [
            ToolDefinition(
                name="shades_list",
                slash_group="shades",
                slash_command="list",
                slash_help="List all shades, grouped by area: /shades list",
                description="List every shade known to the active backend, grouped by area.",
                parameters=[],
                required_role="user",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="shades_status",
                slash_group="shades",
                slash_command="status",
                slash_help="Show shade position: /shades status <name|area>",
                description="Report current open/closed position for a specific shade or every shade in an area.",
                parameters=[
                    ToolParameter(
                        name="name",
                        type=ToolParameterType.STRING,
                        description="Shade name or area name.",
                    ),
                ],
                required_role="user",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="shades_open",
                slash_group="shades",
                slash_command="open",
                slash_help="Open shades: /shades open <name|area>",
                description="Fully open a shade or every shade in an area.",
                parameters=[
                    ToolParameter(
                        name="name",
                        type=ToolParameterType.STRING,
                        description="Shade name or area name.",
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="shades_close",
                slash_group="shades",
                slash_command="close",
                slash_help="Close shades: /shades close <name|area>",
                description="Fully close a shade or every shade in an area.",
                parameters=[
                    ToolParameter(
                        name="name",
                        type=ToolParameterType.STRING,
                        description="Shade name or area name.",
                    ),
                ],
                required_role="user",
            ),
        ]

        # Capability-gated: only register set_position when the backend
        # advertises arbitrary positioning.
        if self.supports_position:
            tools.append(
                ToolDefinition(
                    name="shades_set_position",
                    slash_group="shades",
                    slash_command="position",
                    slash_help="Set position 0-100: /shades position <name|area> <pct>",
                    description=(
                        "Set a shade or every shade in an area to a specific "
                        "position (0=closed, 100=open). Skips position-incapable "
                        "shades."
                    ),
                    parameters=[
                        ToolParameter(
                            name="name",
                            type=ToolParameterType.STRING,
                            description="Shade name or area name.",
                        ),
                        ToolParameter(
                            name="position",
                            type=ToolParameterType.INTEGER,
                            description="Position 0-100 (0=closed, 100=open).",
                        ),
                    ],
                    required_role="user",
                ),
            )

        # Capability-gated: stop is only meaningful on backends that
        # can interrupt a moving shade.
        if self.supports_stop:
            tools.append(
                ToolDefinition(
                    name="shades_stop",
                    slash_group="shades",
                    slash_command="stop",
                    slash_help="Stop a moving shade: /shades stop <name|area>",
                    description="Stop a moving shade or every moving shade in an area.",
                    parameters=[
                        ToolParameter(
                            name="name",
                            type=ToolParameterType.STRING,
                            description="Shade name or area name.",
                        ),
                    ],
                    required_role="user",
                ),
            )

        return tools

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if not self._enabled or self._backend is None:
            return "Shades service is not configured."
        backend = self._backend
        args = {k: v for k, v in arguments.items() if not k.startswith("_")}

        try:
            match name:
                case "shades_list":
                    return await self._list(backend)
                case "shades_status":
                    return await self._status(backend, str(args.get("name", "")))
                case "shades_open":
                    return await self._open(backend, str(args.get("name", "")))
                case "shades_close":
                    return await self._close(backend, str(args.get("name", "")))
                case "shades_set_position":
                    if not self.supports_position:
                        return "Arbitrary positioning is not supported by this backend."
                    return await self._set_position(
                        backend,
                        str(args.get("name", "")),
                        args.get("position"),
                    )
                case "shades_stop":
                    if not self.supports_stop:
                        return "Stop is not supported by this backend."
                    return await self._stop(backend, str(args.get("name", "")))
        except Exception as exc:
            logger.exception("shades tool error: %s", name)
            return f"Sorry, I had trouble with the shades: {exc}"
        raise KeyError(f"Unknown shades tool: {name}")

    # --- Tool implementations ---

    async def _list(self, backend: ShadesBackend) -> str:
        shades = await backend.list_shades()
        if not shades:
            return "No shades found."
        by_area: dict[str, list[ShadeInfo]] = {}
        for shade in shades:
            by_area.setdefault(shade.area or "(no area)", []).append(shade)
        out: list[str] = []
        for area in sorted(by_area):
            out.append(f"# {area}")
            for shade in by_area[area]:
                features = []
                if shade.supports_position:
                    features.append("position")
                if shade.supports_stop:
                    features.append("stop")
                tag = f" [{', '.join(features)}]" if features else ""
                out.append(f"  - {shade.name}{tag}")
        return "\n".join(out)

    async def _status(self, backend: ShadesBackend, name: str) -> str:
        if not name:
            return "Specify a shade or area name."
        matches = _resolve(name, await backend.list_shades())
        if not matches:
            return f"No shades match '{name}'."
        parts: list[str] = []
        for shade in matches:
            position = await backend.get_position(shade.shade_id)
            if position <= 0:
                parts.append(f"{shade.name}: closed")
            elif position >= 100:
                parts.append(f"{shade.name}: open")
            else:
                parts.append(f"{shade.name}: {int(round(position))}%")
        return "\n".join(parts)

    async def _open(self, backend: ShadesBackend, name: str) -> str:
        return await self._move(backend, name, 100.0, "Opened")

    async def _close(self, backend: ShadesBackend, name: str) -> str:
        return await self._move(backend, name, 0.0, "Closed")

    async def _move(
        self,
        backend: ShadesBackend,
        name: str,
        position: float,
        verb: str,
    ) -> str:
        if not name:
            return "Specify a shade or area name."
        matches = _resolve(name, await backend.list_shades())
        if not matches:
            return f"No shades match '{name}'."
        for shade in matches:
            await backend.set_position(shade.shade_id, position)
        if len(matches) == 1:
            return f"{verb} {matches[0].name}."
        return f"{verb} {len(matches)} shades in '{name}'."

    async def _set_position(
        self,
        backend: ShadesBackend,
        name: str,
        position: Any,
    ) -> str:
        if not name:
            return "Specify a shade or area name."
        if position is None:
            return "Specify a position 0-100."
        try:
            level = max(0.0, min(100.0, float(position)))
        except (TypeError, ValueError):
            return "Position must be a number 0-100."
        matches = _resolve(name, await backend.list_shades())
        if not matches:
            return f"No shades match '{name}'."
        positionable = [s for s in matches if s.supports_position]
        if not positionable:
            return f"None of the shades matching '{name}' support arbitrary positioning."
        for shade in positionable:
            await backend.set_position(shade.shade_id, level)
        if len(positionable) == 1:
            return f"Set {positionable[0].name} to {int(round(level))}%."
        return f"Set {len(positionable)} shades in '{name}' to {int(round(level))}%."

    async def _stop(self, backend: ShadesBackend, name: str) -> str:
        if not name:
            return "Specify a shade or area name."
        matches = _resolve(name, await backend.list_shades())
        if not matches:
            return f"No shades match '{name}'."
        stoppable = [s for s in matches if s.supports_stop]
        if not stoppable:
            return f"None of the shades matching '{name}' support stop."
        for shade in stoppable:
            await backend.stop(shade.shade_id)
        if len(stoppable) == 1:
            return f"Stopped {stoppable[0].name}."
        return f"Stopped {len(stoppable)} shades in '{name}'."
