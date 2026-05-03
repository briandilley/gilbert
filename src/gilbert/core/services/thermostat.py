"""Thermostat service — wraps a ThermostatBackend as a discoverable service.

Thin orchestration layer mirroring ``LightsService`` / ``ShadesService``:
the backend (e.g. ``NexiaThermostat``) owns the device topology and the
wire protocol; this service exposes operations as AI tools and slash
commands, dispatches by ``thermostat_id``, and resolves user-typed
names against the cached topology.

Name resolution: an area-name equality match wins; otherwise we
substring-match the thermostat name. The service never lets free text
reach the backend.
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
from gilbert.interfaces.thermostat import (
    FAN_MODES,
    HVAC_MODES,
    ThermostatBackend,
    ThermostatInfo,
)
from gilbert.interfaces.tools import ToolDefinition, ToolParameter, ToolParameterType

logger = logging.getLogger(__name__)


def _resolve(query: str, thermostats: list[ThermostatInfo]) -> list[ThermostatInfo]:
    """Match user input against thermostat name OR area name."""
    q = query.lower().strip()
    if not q:
        return []
    by_area = [t for t in thermostats if t.area.lower() == q]
    if by_area:
        return by_area
    return [t for t in thermostats if q in t.name.lower()]


def _format_status(info: ThermostatInfo) -> str:
    unit = info.temperature_unit or "F"
    parts = [f"{info.name}: {info.current_temperature:g}°{unit}"]
    if info.current_humidity is not None:
        parts.append(f"{int(round(info.current_humidity))}% RH")
    parts.append(f"mode={info.mode}")
    setpoints: list[str] = []
    if info.heat_setpoint is not None:
        setpoints.append(f"heat {info.heat_setpoint:g}°{unit}")
    if info.cool_setpoint is not None:
        setpoints.append(f"cool {info.cool_setpoint:g}°{unit}")
    if setpoints:
        parts.append(", ".join(setpoints))
    if info.fan_mode is not None:
        parts.append(f"fan={info.fan_mode}")
    return " · ".join(parts)


class ThermostatService(Service):
    """Discover and control thermostats through a ``ThermostatBackend``."""

    slash_namespace = "climate"

    def __init__(self) -> None:
        self._backend: ThermostatBackend | None = None
        self._backend_name: str = ""
        self._enabled: bool = False
        self._config: dict[str, object] = {}

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="thermostats",
            capabilities=frozenset({"thermostats", "ai_tools"}),
            optional=frozenset({"configuration"}),
            toggleable=True,
            toggle_description="Thermostat control",
        )

    @property
    def backend(self) -> ThermostatBackend | None:
        return self._backend

    async def start(self, resolver: ServiceResolver) -> None:
        config_svc = resolver.get_capability("configuration")
        section: dict[str, Any] = {}
        if config_svc is not None and isinstance(config_svc, ConfigurationReader):
            section = config_svc.get_section(self.config_namespace)

        if not section.get("enabled", False):
            logger.info("Thermostat service disabled")
            return

        self._enabled = True
        self._config = section.get("settings", self._config)

        backend_name = str(section.get("backend", "") or "")
        self._backend_name = backend_name
        if not backend_name:
            logger.warning("Thermostats enabled but no backend selected")
            return

        backends = ThermostatBackend.registered_backends()
        backend_cls = backends.get(backend_name)
        if backend_cls is None:
            logger.warning(
                "Unknown thermostat backend %r — registered: %s",
                backend_name,
                list(backends),
            )
            return

        self._backend = backend_cls()
        try:
            await self._backend.initialize(self._config)
        except Exception:
            logger.exception("Failed to initialize thermostat backend %s", backend_name)
            self._backend = None
            return

        logger.info("Thermostat service started (backend=%s)", backend_name)

    async def stop(self) -> None:
        if self._backend is not None:
            try:
                await self._backend.close()
            except Exception:
                logger.exception("Error closing thermostat backend")
            self._backend = None

    # --- Capability proxies ---

    @property
    def supports_cooling(self) -> bool:
        return bool(self._backend and self._backend.supports_cooling)

    @property
    def supports_heating(self) -> bool:
        return bool(self._backend and self._backend.supports_heating)

    @property
    def supports_fan_mode(self) -> bool:
        return bool(self._backend and self._backend.supports_fan_mode)

    # --- Configurable ---

    @property
    def config_namespace(self) -> str:
        return "thermostats"

    @property
    def config_category(self) -> str:
        return "Climate"

    def config_params(self) -> list[ConfigParam]:
        params = [
            ConfigParam(
                key="backend",
                type=ToolParameterType.STRING,
                description="Thermostat backend type.",
                default="",
                restart_required=True,
                choices=tuple(ThermostatBackend.registered_backends().keys()),
            ),
        ]
        backends = ThermostatBackend.registered_backends()
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
                logger.exception(
                    "Failed to re-initialize thermostat backend after config change"
                )

    # --- ConfigActionProvider ---

    def config_actions(self) -> list[ConfigAction]:
        return all_backend_actions(
            registry=ThermostatBackend.registered_backends(),
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
        return "thermostats"

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        if not self._enabled or self._backend is None:
            return []

        tools: list[ToolDefinition] = [
            ToolDefinition(
                name="thermostats_list",
                slash_group="climate",
                slash_command="list",
                slash_help="List all thermostats, grouped by area: /climate list",
                description=(
                    "List every thermostat known to the active backend, grouped "
                    "by area, with current temperature and mode."
                ),
                parameters=[],
                required_role="user",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="thermostats_status",
                slash_group="climate",
                slash_command="status",
                slash_help="Show thermostat status: /climate status <name|area>",
                description=(
                    "Report current temperature, humidity, mode, fan, and "
                    "setpoints for a specific thermostat or every thermostat "
                    "in an area."
                ),
                parameters=[
                    ToolParameter(
                        name="name",
                        type=ToolParameterType.STRING,
                        description="Thermostat name or area name.",
                    ),
                ],
                required_role="user",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="thermostats_set_mode",
                slash_group="climate",
                slash_command="mode",
                slash_help=(
                    "Set HVAC mode (off|heat|cool|auto): "
                    "/climate mode <name|area> <mode>"
                ),
                description=(
                    "Change the HVAC mode on a thermostat or every thermostat "
                    "in an area. Valid modes: off, heat, cool, auto."
                ),
                parameters=[
                    ToolParameter(
                        name="name",
                        type=ToolParameterType.STRING,
                        description="Thermostat name or area name.",
                    ),
                    ToolParameter(
                        name="mode",
                        type=ToolParameterType.STRING,
                        description="HVAC mode: off, heat, cool, or auto.",
                    ),
                ],
                required_role="user",
            ),
        ]

        if self.supports_heating:
            tools.append(
                ToolDefinition(
                    name="thermostats_set_heat",
                    slash_group="climate",
                    slash_command="heat",
                    slash_help="Set heating setpoint: /climate heat <name|area> <temp>",
                    description=(
                        "Set the heating setpoint on a thermostat or every "
                        "thermostat in an area. Temperature is in the device's "
                        "native unit (typically Fahrenheit)."
                    ),
                    parameters=[
                        ToolParameter(
                            name="name",
                            type=ToolParameterType.STRING,
                            description="Thermostat name or area name.",
                        ),
                        ToolParameter(
                            name="temperature",
                            type=ToolParameterType.NUMBER,
                            description="Heating setpoint temperature.",
                        ),
                    ],
                    required_role="user",
                ),
            )

        if self.supports_cooling:
            tools.append(
                ToolDefinition(
                    name="thermostats_set_cool",
                    slash_group="climate",
                    slash_command="cool",
                    slash_help="Set cooling setpoint: /climate cool <name|area> <temp>",
                    description=(
                        "Set the cooling setpoint on a thermostat or every "
                        "thermostat in an area. Temperature is in the device's "
                        "native unit (typically Fahrenheit)."
                    ),
                    parameters=[
                        ToolParameter(
                            name="name",
                            type=ToolParameterType.STRING,
                            description="Thermostat name or area name.",
                        ),
                        ToolParameter(
                            name="temperature",
                            type=ToolParameterType.NUMBER,
                            description="Cooling setpoint temperature.",
                        ),
                    ],
                    required_role="user",
                ),
            )

        if self.supports_heating and self.supports_cooling:
            tools.append(
                ToolDefinition(
                    name="thermostats_set_range",
                    slash_group="climate",
                    slash_command="range",
                    slash_help=(
                        "Set heat/cool range for AUTO mode: "
                        "/climate range <name|area> <heat> <cool>"
                    ),
                    description=(
                        "Set both heating and cooling setpoints at once — used "
                        "by AUTO mode to define the comfort band."
                    ),
                    parameters=[
                        ToolParameter(
                            name="name",
                            type=ToolParameterType.STRING,
                            description="Thermostat name or area name.",
                        ),
                        ToolParameter(
                            name="heat",
                            type=ToolParameterType.NUMBER,
                            description="Heating setpoint (lower bound).",
                        ),
                        ToolParameter(
                            name="cool",
                            type=ToolParameterType.NUMBER,
                            description="Cooling setpoint (upper bound).",
                        ),
                    ],
                    required_role="user",
                ),
            )

        if self.supports_fan_mode:
            tools.append(
                ToolDefinition(
                    name="thermostats_set_fan_mode",
                    slash_group="climate",
                    slash_command="fan",
                    slash_help=(
                        "Set fan mode (auto|on|circulate): "
                        "/climate fan <name|area> <mode>"
                    ),
                    description=(
                        "Change the fan mode on a thermostat or every thermostat "
                        "in an area. Valid modes: auto, on, circulate."
                    ),
                    parameters=[
                        ToolParameter(
                            name="name",
                            type=ToolParameterType.STRING,
                            description="Thermostat name or area name.",
                        ),
                        ToolParameter(
                            name="mode",
                            type=ToolParameterType.STRING,
                            description="Fan mode: auto, on, or circulate.",
                        ),
                    ],
                    required_role="user",
                ),
            )

        return tools

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if not self._enabled or self._backend is None:
            return "Thermostat service is not configured."
        backend = self._backend
        args = {k: v for k, v in arguments.items() if not k.startswith("_")}

        try:
            match name:
                case "thermostats_list":
                    return await self._list(backend)
                case "thermostats_status":
                    return await self._status(backend, str(args.get("name", "")))
                case "thermostats_set_mode":
                    return await self._set_mode(
                        backend,
                        str(args.get("name", "")),
                        str(args.get("mode", "")),
                    )
                case "thermostats_set_heat":
                    if not self.supports_heating:
                        return "Heating is not supported by this backend."
                    return await self._set_setpoint(
                        backend,
                        str(args.get("name", "")),
                        args.get("temperature"),
                        direction="heat",
                    )
                case "thermostats_set_cool":
                    if not self.supports_cooling:
                        return "Cooling is not supported by this backend."
                    return await self._set_setpoint(
                        backend,
                        str(args.get("name", "")),
                        args.get("temperature"),
                        direction="cool",
                    )
                case "thermostats_set_range":
                    if not (self.supports_heating and self.supports_cooling):
                        return "This backend doesn't support both heat and cool setpoints."
                    return await self._set_range(
                        backend,
                        str(args.get("name", "")),
                        args.get("heat"),
                        args.get("cool"),
                    )
                case "thermostats_set_fan_mode":
                    if not self.supports_fan_mode:
                        return "Fan mode is not supported by this backend."
                    return await self._set_fan_mode(
                        backend,
                        str(args.get("name", "")),
                        str(args.get("mode", "")),
                    )
        except Exception as exc:
            logger.exception("thermostats tool error: %s", name)
            return f"Sorry, I had trouble with the thermostat: {exc}"
        raise KeyError(f"Unknown thermostats tool: {name}")

    # --- Tool implementations ---

    async def _list(self, backend: ThermostatBackend) -> str:
        thermostats = await backend.list_thermostats()
        if not thermostats:
            return "No thermostats found."
        by_area: dict[str, list[ThermostatInfo]] = {}
        for t in thermostats:
            by_area.setdefault(t.area or "(no area)", []).append(t)
        out: list[str] = []
        for area in sorted(by_area):
            out.append(f"# {area}")
            for t in by_area[area]:
                out.append(f"  - {_format_status(t)}")
        return "\n".join(out)

    async def _status(self, backend: ThermostatBackend, name: str) -> str:
        if not name:
            return "Specify a thermostat or area name."
        matches = _resolve(name, await backend.list_thermostats())
        if not matches:
            return f"No thermostats match '{name}'."
        parts: list[str] = []
        for t in matches:
            fresh = await backend.get_status(t.thermostat_id)
            parts.append(_format_status(fresh))
        return "\n".join(parts)

    async def _set_mode(
        self,
        backend: ThermostatBackend,
        name: str,
        mode: str,
    ) -> str:
        if not name:
            return "Specify a thermostat or area name."
        normalized = mode.lower().strip()
        if normalized not in HVAC_MODES:
            return f"Mode must be one of: {', '.join(HVAC_MODES)}."
        matches = _resolve(name, await backend.list_thermostats())
        if not matches:
            return f"No thermostats match '{name}'."
        for t in matches:
            await backend.set_mode(t.thermostat_id, normalized)
        if len(matches) == 1:
            return f"Set {matches[0].name} to {normalized}."
        return f"Set {len(matches)} thermostats in '{name}' to {normalized}."

    async def _set_setpoint(
        self,
        backend: ThermostatBackend,
        name: str,
        temperature: Any,
        *,
        direction: str,
    ) -> str:
        if not name:
            return "Specify a thermostat or area name."
        if temperature is None:
            return "Specify a temperature."
        try:
            target = float(temperature)
        except (TypeError, ValueError):
            return "Temperature must be a number."
        matches = _resolve(name, await backend.list_thermostats())
        if not matches:
            return f"No thermostats match '{name}'."
        eligible = [
            t for t in matches
            if (direction == "heat" and t.supports_heating)
            or (direction == "cool" and t.supports_cooling)
        ]
        if not eligible:
            return f"None of the thermostats matching '{name}' support {direction}."
        for t in eligible:
            if direction == "heat":
                await backend.set_setpoint(t.thermostat_id, heat=target)
            else:
                await backend.set_setpoint(t.thermostat_id, cool=target)
        unit = eligible[0].temperature_unit or "F"
        if len(eligible) == 1:
            return f"Set {eligible[0].name} {direction} setpoint to {target:g}°{unit}."
        return (
            f"Set {direction} setpoint to {target:g}°{unit} on "
            f"{len(eligible)} thermostats in '{name}'."
        )

    async def _set_range(
        self,
        backend: ThermostatBackend,
        name: str,
        heat: Any,
        cool: Any,
    ) -> str:
        if not name:
            return "Specify a thermostat or area name."
        if heat is None or cool is None:
            return "Specify both heat and cool setpoints."
        try:
            heat_val = float(heat)
            cool_val = float(cool)
        except (TypeError, ValueError):
            return "Heat and cool must be numbers."
        if heat_val >= cool_val:
            return "Heat setpoint must be lower than cool setpoint."
        matches = _resolve(name, await backend.list_thermostats())
        if not matches:
            return f"No thermostats match '{name}'."
        eligible = [
            t for t in matches
            if t.supports_heating and t.supports_cooling
        ]
        if not eligible:
            return (
                f"None of the thermostats matching '{name}' support both "
                "heating and cooling."
            )
        for t in eligible:
            await backend.set_setpoint(t.thermostat_id, heat=heat_val, cool=cool_val)
        unit = eligible[0].temperature_unit or "F"
        if len(eligible) == 1:
            return (
                f"Set {eligible[0].name} range to "
                f"{heat_val:g}°{unit}-{cool_val:g}°{unit}."
            )
        return (
            f"Set range to {heat_val:g}°{unit}-{cool_val:g}°{unit} on "
            f"{len(eligible)} thermostats in '{name}'."
        )

    async def _set_fan_mode(
        self,
        backend: ThermostatBackend,
        name: str,
        mode: str,
    ) -> str:
        if not name:
            return "Specify a thermostat or area name."
        normalized = mode.lower().strip()
        if normalized not in FAN_MODES:
            return f"Fan mode must be one of: {', '.join(FAN_MODES)}."
        matches = _resolve(name, await backend.list_thermostats())
        if not matches:
            return f"No thermostats match '{name}'."
        eligible = [t for t in matches if t.supports_fan_mode]
        if not eligible:
            return f"None of the thermostats matching '{name}' support fan mode control."
        for t in eligible:
            await backend.set_fan_mode(t.thermostat_id, normalized)
        if len(eligible) == 1:
            return f"Set {eligible[0].name} fan to {normalized}."
        return f"Set fan to {normalized} on {len(eligible)} thermostats in '{name}'."
