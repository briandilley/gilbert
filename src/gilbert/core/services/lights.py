"""Lights service — wraps a LightsBackend as a discoverable service.

Thin orchestration layer: the backend (e.g. ``LutronLights``) owns the
device topology and the wire protocol; this service exposes operations
as AI tools and slash commands, and dispatches by ``light_id``.

The service resolves user-typed names ("kitchen", "front porch") against
the cached topology with a simple area-then-substring match — keeping
that logic out of every backend.
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
from gilbert.interfaces.lights import LightInfo, LightsBackend
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import ToolDefinition, ToolParameter, ToolParameterType

logger = logging.getLogger(__name__)


def _resolve(query: str, lights: list[LightInfo]) -> list[LightInfo]:
    """Match user input against light name OR area name.

    Area match (case-insensitive equality) wins; falls back to substring
    match on the light name. Mirrors the old assistant's behavior so a
    user can say "kitchen" to address every kitchen light.
    """
    q = query.lower().strip()
    if not q:
        return []
    by_area = [light for light in lights if light.area.lower() == q]
    if by_area:
        return by_area
    return [light for light in lights if q in light.name.lower()]


class LightsService(Service):
    """Discover and control lights through a ``LightsBackend``."""

    def __init__(self) -> None:
        self._backend: LightsBackend | None = None
        self._backend_name: str = ""
        self._enabled: bool = False
        self._config: dict[str, object] = {}

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="lights",
            capabilities=frozenset({"lights", "ai_tools"}),
            optional=frozenset({"configuration"}),
            toggleable=True,
            toggle_description="Light control",
        )

    @property
    def backend(self) -> LightsBackend | None:
        return self._backend

    async def start(self, resolver: ServiceResolver) -> None:
        config_svc = resolver.get_capability("configuration")
        section: dict[str, Any] = {}
        if config_svc is not None and isinstance(config_svc, ConfigurationReader):
            section = config_svc.get_section(self.config_namespace)

        if not section.get("enabled", False):
            logger.info("Lights service disabled")
            return

        self._enabled = True
        self._config = section.get("settings", self._config)

        backend_name = str(section.get("backend", "") or "")
        self._backend_name = backend_name
        if not backend_name:
            logger.warning("Lights enabled but no backend selected")
            return

        backends = LightsBackend.registered_backends()
        backend_cls = backends.get(backend_name)
        if backend_cls is None:
            logger.warning(
                "Unknown lights backend %r — registered: %s",
                backend_name,
                list(backends),
            )
            return

        self._backend = backend_cls()
        try:
            await self._backend.initialize(self._config)
        except Exception:
            logger.exception("Failed to initialize lights backend %s", backend_name)
            self._backend = None
            return

        logger.info("Lights service started (backend=%s)", backend_name)

    async def stop(self) -> None:
        if self._backend is not None:
            try:
                await self._backend.close()
            except Exception:
                logger.exception("Error closing lights backend")
            self._backend = None

    # --- Capability proxies ---

    @property
    def supports_dimming(self) -> bool:
        """Whether the active backend declares dimmer support.

        Gates the ``set_brightness`` tool — same pattern as
        ``MusicService.supports_loop``.
        """
        return bool(self._backend and self._backend.supports_dimming)

    # --- Configurable ---

    @property
    def config_namespace(self) -> str:
        return "lights"

    @property
    def config_category(self) -> str:
        return "Lighting"

    def config_params(self) -> list[ConfigParam]:
        params = [
            ConfigParam(
                key="backend",
                type=ToolParameterType.STRING,
                description="Lights backend type.",
                default="",
                restart_required=True,
                choices=tuple(LightsBackend.registered_backends().keys()),
            ),
        ]
        backends = LightsBackend.registered_backends()
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
                logger.exception("Failed to re-initialize lights backend after config change")

    # --- ConfigActionProvider ---

    def config_actions(self) -> list[ConfigAction]:
        return all_backend_actions(
            registry=LightsBackend.registered_backends(),
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
        return "lights"

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        if not self._enabled or self._backend is None:
            return []

        tools: list[ToolDefinition] = [
            ToolDefinition(
                name="lights_list",
                slash_group="lights",
                slash_command="list",
                slash_help="List all lights, grouped by area: /lights list",
                description="List every light known to the active backend, grouped by area.",
                parameters=[],
                required_role="user",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="lights_status",
                slash_group="lights",
                slash_command="status",
                slash_help="Show on/off + brightness: /lights status <name|area>",
                description=(
                    "Report on/off state (and brightness for dimmers) for a "
                    "specific light or every light in an area."
                ),
                parameters=[
                    ToolParameter(
                        name="name",
                        type=ToolParameterType.STRING,
                        description="Light name or area name.",
                    ),
                ],
                required_role="user",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="lights_turn_on",
                slash_group="lights",
                slash_command="on",
                slash_help="Turn lights on: /lights on <name|area> [brightness]",
                description=(
                    "Turn on a light or every light in an area. Optional "
                    "brightness 0-100 (only honored on dimmable lights)."
                ),
                parameters=[
                    ToolParameter(
                        name="name",
                        type=ToolParameterType.STRING,
                        description="Light name or area name.",
                    ),
                    ToolParameter(
                        name="brightness",
                        type=ToolParameterType.INTEGER,
                        description="Brightness 0-100 (dimmers only).",
                        required=False,
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="lights_turn_off",
                slash_group="lights",
                slash_command="off",
                slash_help="Turn lights off: /lights off <name|area>",
                description="Turn off a light or every light in an area.",
                parameters=[
                    ToolParameter(
                        name="name",
                        type=ToolParameterType.STRING,
                        description="Light name or area name.",
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="lights_toggle",
                slash_group="lights",
                slash_command="toggle",
                slash_help="Toggle lights: /lights toggle <name|area>",
                description=(
                    "Toggle a light or every light in an area — on goes off, "
                    "off goes to full."
                ),
                parameters=[
                    ToolParameter(
                        name="name",
                        type=ToolParameterType.STRING,
                        description="Light name or area name.",
                    ),
                ],
                required_role="user",
            ),
        ]

        # Capability-gated: only register set_brightness when the backend
        # advertises ``supports_dimming``. Mirrors MusicService's gating
        # of ``set_loop`` on the speaker's ``supports_repeat``.
        if self.supports_dimming:
            tools.append(
                ToolDefinition(
                    name="lights_set_brightness",
                    slash_group="lights",
                    slash_command="brightness",
                    slash_help="Set brightness 0-100: /lights brightness <name|area> <pct>",
                    description=(
                        "Set brightness on a dimmable light or every dimmable "
                        "light in an area. Switch-only loads are skipped."
                    ),
                    parameters=[
                        ToolParameter(
                            name="name",
                            type=ToolParameterType.STRING,
                            description="Light name or area name.",
                        ),
                        ToolParameter(
                            name="brightness",
                            type=ToolParameterType.INTEGER,
                            description="Brightness 0-100.",
                        ),
                    ],
                    required_role="user",
                ),
            )

        return tools

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if not self._enabled or self._backend is None:
            return "Lights service is not configured."
        backend = self._backend
        args = {k: v for k, v in arguments.items() if not k.startswith("_")}

        try:
            match name:
                case "lights_list":
                    return await self._list(backend)
                case "lights_status":
                    return await self._status(backend, str(args.get("name", "")))
                case "lights_turn_on":
                    return await self._turn_on(
                        backend,
                        str(args.get("name", "")),
                        args.get("brightness"),
                    )
                case "lights_turn_off":
                    return await self._turn_off(backend, str(args.get("name", "")))
                case "lights_toggle":
                    return await self._toggle(backend, str(args.get("name", "")))
                case "lights_set_brightness":
                    if not self.supports_dimming:
                        return "Brightness control is not supported by this backend."
                    return await self._set_brightness(
                        backend,
                        str(args.get("name", "")),
                        args.get("brightness"),
                    )
        except Exception as exc:
            logger.exception("lights tool error: %s", name)
            return f"Sorry, I had trouble with the lights: {exc}"
        raise KeyError(f"Unknown lights tool: {name}")

    # --- Tool implementations ---

    async def _list(self, backend: LightsBackend) -> str:
        lights = await backend.list_lights()
        if not lights:
            return "No lights found."
        by_area: dict[str, list[LightInfo]] = {}
        for light in lights:
            by_area.setdefault(light.area or "(no area)", []).append(light)
        out: list[str] = []
        for area in sorted(by_area):
            out.append(f"# {area}")
            for light in by_area[area]:
                kind = "dimmable" if light.supports_dimming else "switch"
                out.append(f"  - {light.name} ({kind})")
        return "\n".join(out)

    async def _status(self, backend: LightsBackend, name: str) -> str:
        if not name:
            return "Specify a light or area name."
        matches = _resolve(name, await backend.list_lights())
        if not matches:
            return f"No lights match '{name}'."
        parts: list[str] = []
        for light in matches:
            level = await backend.get_level(light.light_id)
            if level <= 0:
                parts.append(f"{light.name}: off")
            elif light.supports_dimming:
                parts.append(f"{light.name}: on ({int(round(level))}%)")
            else:
                parts.append(f"{light.name}: on")
        return "\n".join(parts)

    async def _turn_on(
        self,
        backend: LightsBackend,
        name: str,
        brightness: Any,
    ) -> str:
        if not name:
            return "Specify a light or area name."
        matches = _resolve(name, await backend.list_lights())
        if not matches:
            return f"No lights match '{name}'."
        level = 100.0
        if brightness is not None:
            try:
                level = max(0.0, min(100.0, float(brightness)))
            except (TypeError, ValueError):
                return "Brightness must be a number 0-100."
        for light in matches:
            await backend.set_level(light.light_id, level)
        suffix = f" at {int(round(level))}%" if brightness is not None else ""
        if len(matches) == 1:
            return f"Turned on {matches[0].name}{suffix}."
        return f"Turned on {len(matches)} lights in '{name}'{suffix}."

    async def _turn_off(self, backend: LightsBackend, name: str) -> str:
        if not name:
            return "Specify a light or area name."
        matches = _resolve(name, await backend.list_lights())
        if not matches:
            return f"No lights match '{name}'."
        for light in matches:
            await backend.set_level(light.light_id, 0.0)
        if len(matches) == 1:
            return f"Turned off {matches[0].name}."
        return f"Turned off {len(matches)} lights in '{name}'."

    async def _toggle(self, backend: LightsBackend, name: str) -> str:
        if not name:
            return "Specify a light or area name."
        matches = _resolve(name, await backend.list_lights())
        if not matches:
            return f"No lights match '{name}'."
        on_count = 0
        off_count = 0
        for light in matches:
            current = await backend.get_level(light.light_id)
            if current > 0:
                await backend.set_level(light.light_id, 0.0)
                off_count += 1
            else:
                await backend.set_level(light.light_id, 100.0)
                on_count += 1
        if len(matches) == 1:
            verb = "off" if off_count else "on"
            return f"Toggled {matches[0].name} {verb}."
        return f"Toggled {len(matches)} lights in '{name}' ({on_count} on, {off_count} off)."

    async def _set_brightness(
        self,
        backend: LightsBackend,
        name: str,
        brightness: Any,
    ) -> str:
        if not name:
            return "Specify a light or area name."
        if brightness is None:
            return "Specify a brightness 0-100."
        try:
            level = max(0.0, min(100.0, float(brightness)))
        except (TypeError, ValueError):
            return "Brightness must be a number 0-100."
        matches = _resolve(name, await backend.list_lights())
        if not matches:
            return f"No lights match '{name}'."
        dimmable = [light for light in matches if light.supports_dimming]
        if not dimmable:
            return f"None of the lights matching '{name}' are dimmable."
        for light in dimmable:
            await backend.set_level(light.light_id, level)
        if len(dimmable) == 1:
            return f"Set {dimmable[0].name} to {int(round(level))}%."
        return f"Set {len(dimmable)} dimmable lights in '{name}' to {int(round(level))}%."
