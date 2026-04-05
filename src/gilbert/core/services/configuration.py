"""Configuration service — runtime config management with hot-swap support."""

import json
import logging
from pathlib import Path
from typing import Any, Callable

import yaml

from gilbert.config import GilbertConfig, OVERRIDE_CONFIG_PATH, _deep_merge
from gilbert.interfaces.configuration import ConfigParam, Configurable
from gilbert.interfaces.events import Event
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)

logger = logging.getLogger(__name__)

# Type for factory functions that create services from config
ServiceFactory = Callable[[dict[str, Any]], Service]


class ConfigurationService(Service):
    """Manages runtime configuration with read/write, persistence, and hot-swap.

    - Holds the live GilbertConfig and raw config dict
    - Discovers Configurable services lazily
    - Tunable param changes: updates config, calls on_config_changed()
    - Structural param changes: uses registered factory to reconstruct service
    - Persists changes to .gilbert/config.yaml (override layer only)
    """

    def __init__(self, config: GilbertConfig) -> None:
        self._config = config
        self._raw: dict[str, Any] = config.model_dump()
        self._resolver: ServiceResolver | None = None
        self._service_manager: Any = None  # ServiceManager, set during start
        self._factories: dict[str, ServiceFactory] = {}

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="configuration",
            capabilities=frozenset({"configuration", "ai_tools"}),
            optional=frozenset({"event_bus"}),
        )

    @property
    def config(self) -> GilbertConfig:
        return self._config

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver
        # Import here to avoid circular deps
        from gilbert.core.service_manager import ServiceManager

        # The resolver IS the ServiceManager
        if isinstance(resolver, ServiceManager):
            self._service_manager = resolver
        logger.info("Configuration service started")

    def register_factory(self, namespace: str, factory: ServiceFactory) -> None:
        """Register a factory for reconstructing a service from config."""
        self._factories[namespace] = factory

    # --- Read API ---

    def get(self, path: str) -> Any:
        """Get a config value by dot-path (e.g., 'ai.settings.temperature')."""
        parts = path.split(".")
        current: Any = self._raw
        for part in parts:
            if isinstance(current, dict):
                if part not in current:
                    return None
                current = current[part]
            else:
                return None
        return current

    def get_section(self, namespace: str) -> dict[str, Any]:
        """Get a service's entire config section."""
        section = self._raw.get(namespace)
        if isinstance(section, dict):
            return dict(section)
        return {}

    # --- Write API ---

    async def set(self, path: str, value: Any) -> dict[str, Any]:
        """Set a config value, persist, validate, and notify/restart.

        Returns a status dict: {"status": "ok"} or {"status": "error", "message": ...}
        """
        # Determine namespace (first path segment)
        parts = path.split(".")
        namespace = parts[0]

        # Check if this param is restart_required
        restart_needed = False
        param_key = ".".join(parts[1:]) if len(parts) > 1 else ""
        configurable = self._find_configurable(namespace)
        if configurable:
            for param in configurable.config_params():
                if param.key == param_key and param.restart_required:
                    restart_needed = True
                    break

        # Update raw config
        self._set_nested(self._raw, parts, value)

        # Validate via Pydantic
        try:
            self._config = GilbertConfig.model_validate(self._raw)
            self._raw = self._config.model_dump()
        except Exception as exc:
            # Rollback: reload from current valid config
            logger.error("Config validation failed: %s", exc)
            self._raw = self._config.model_dump()
            return {"status": "error", "message": f"Validation failed: {exc}"}

        # Persist to override file
        self._persist()

        # Notify or restart
        if restart_needed:
            result = await self._handle_restart(namespace)
        elif configurable:
            section = self.get_section(namespace)
            try:
                await configurable.on_config_changed(section)
                result = {"status": "ok", "path": path, "value": value}
            except Exception as exc:
                logger.exception("Error applying config change to %s", namespace)
                result = {"status": "error", "message": f"Apply failed: {exc}"}
        else:
            # No configurable service for this namespace (e.g., top-level settings)
            result = {"status": "ok", "path": path, "value": value}

        # Publish event
        await self._publish_config_event(path, value)

        return result

    # --- Describe API ---

    def describe_all(self) -> dict[str, list[ConfigParam]]:
        """Describe all configurable parameters across all services."""
        result: dict[str, list[ConfigParam]] = {}
        if self._resolver is None:
            return result

        from gilbert.core.service_manager import ServiceManager

        if not isinstance(self._resolver, ServiceManager):
            return result

        for name in self._resolver.started_services:
            svc = self._resolver.get_service(name)
            if svc is not None and isinstance(svc, Configurable):
                result[svc.config_namespace] = svc.config_params()

        return result

    # --- Internal ---

    def _find_configurable(self, namespace: str) -> Configurable | None:
        """Find the Configurable service for a given namespace."""
        if self._resolver is None:
            return None

        from gilbert.core.service_manager import ServiceManager

        if not isinstance(self._resolver, ServiceManager):
            return None

        for name in self._resolver.started_services:
            svc = self._resolver.get_service(name)
            if svc is not None and isinstance(svc, Configurable):
                if svc.config_namespace == namespace:
                    return svc
        return None

    async def _handle_restart(self, namespace: str) -> dict[str, Any]:
        """Handle a structural config change by restarting the service."""
        if self._service_manager is None:
            return {"status": "error", "message": "No service manager available for restart"}

        factory = self._factories.get(namespace)
        if factory is None:
            return {
                "status": "error",
                "message": f"No factory registered for '{namespace}' — cannot hot-swap",
            }

        section = self.get_section(namespace)

        # Check if service should be enabled
        enabled = section.get("enabled", True)

        # Find current service name for this namespace
        configurable = self._find_configurable(namespace)
        if configurable is None and not enabled:
            return {"status": "ok", "message": f"Service '{namespace}' is not running"}

        if configurable is None and enabled:
            # Service wasn't running but now should be
            try:
                new_svc = factory(section)
                await self._service_manager.register_and_start(new_svc)
                return {"status": "ok", "message": f"Service '{namespace}' enabled and started"}
            except Exception as exc:
                logger.exception("Failed to enable service %s", namespace)
                return {"status": "error", "message": f"Failed to enable: {exc}"}

        if configurable is not None and not enabled:
            # Service is running but should be disabled
            svc_name = configurable.config_namespace
            # Find the actual service name from the manager
            for name in self._service_manager.started_services:
                svc = self._service_manager.get_service(name)
                if svc is configurable:
                    try:
                        await svc.stop()
                        logger.info("Service disabled: %s", name)
                    except Exception:
                        logger.exception("Error stopping disabled service %s", name)
                    return {"status": "ok", "message": f"Service '{namespace}' disabled"}
            return {"status": "ok", "message": f"Service '{namespace}' disabled"}

        # Service is running and should be restarted with new config
        try:
            new_svc = factory(section)
            # Find the registered name
            for name in list(self._service_manager.started_services):
                svc = self._service_manager.get_service(name)
                if svc is not None and isinstance(svc, Configurable):
                    if svc.config_namespace == namespace:
                        await self._service_manager.restart_service(name, new_svc)
                        return {"status": "ok", "message": f"Service '{namespace}' restarted"}
        except Exception as exc:
            logger.exception("Failed to restart service %s", namespace)
            return {"status": "error", "message": f"Restart failed: {exc}"}

        return {"status": "error", "message": f"Could not find running service for '{namespace}'"}

    @staticmethod
    def _set_nested(d: dict[str, Any], keys: list[str], value: Any) -> None:
        """Set a value in a nested dict by key path."""
        for key in keys[:-1]:
            if key not in d or not isinstance(d[key], dict):
                d[key] = {}
            d = d[key]
        d[keys[-1]] = value

    def _persist(self) -> None:
        """Write current config overrides to .gilbert/config.yaml."""
        try:
            # Load existing overrides
            override_path = OVERRIDE_CONFIG_PATH
            existing: dict[str, Any] = {}
            if override_path.exists():
                with open(override_path) as f:
                    raw = yaml.safe_load(f)
                    if isinstance(raw, dict):
                        existing = raw

            # Merge current raw config into overrides
            # We write the full config to the override file since we can't
            # easily diff against defaults
            merged = _deep_merge(existing, self._raw)

            override_path.parent.mkdir(parents=True, exist_ok=True)
            with open(override_path, "w") as f:
                yaml.dump(merged, f, default_flow_style=False, sort_keys=False)

            logger.debug("Config persisted to %s", override_path)
        except Exception:
            logger.exception("Failed to persist config")

    async def _publish_config_event(self, path: str, value: Any) -> None:
        """Publish a config.changed event if event bus is available."""
        if self._resolver is None:
            return
        bus_svc = self._resolver.get_capability("event_bus")
        if bus_svc is None:
            return
        from gilbert.core.services.event_bus import EventBusService

        if isinstance(bus_svc, EventBusService):
            try:
                await bus_svc.bus.publish(Event(
                    event_type="config.changed",
                    data={"path": path, "value": value},
                    source="configuration",
                ))
            except Exception:
                logger.debug("Failed to publish config.changed event")

    # --- ToolProvider protocol ---

    @property
    def tool_provider_name(self) -> str:
        return "configuration"

    def get_tools(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="get_configuration",
                description="Get configuration values. Returns the full config or a specific value by path.",
                parameters=[
                    ToolParameter(
                        name="path",
                        type=ToolParameterType.STRING,
                        description="Dot-path to a config value (e.g., 'ai.settings.temperature'). Omit for full config.",
                        required=False,
                    ),
                ],
            ),
            ToolDefinition(
                name="set_configuration",
                description="Set a configuration value. Persists the change and notifies/restarts affected services.",
                parameters=[
                    ToolParameter(
                        name="path",
                        type=ToolParameterType.STRING,
                        description="Dot-path to the config value (e.g., 'ai.system_prompt').",
                    ),
                    ToolParameter(
                        name="value",
                        type=ToolParameterType.STRING,
                        description="The new value (will be parsed as the appropriate type).",
                    ),
                ],
            ),
            ToolDefinition(
                name="describe_configuration",
                description="Describe all configurable parameters with types, descriptions, and defaults.",
                parameters=[
                    ToolParameter(
                        name="namespace",
                        type=ToolParameterType.STRING,
                        description="Service namespace to describe (e.g., 'ai', 'tts'). Omit for all services.",
                        required=False,
                    ),
                ],
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        match name:
            case "get_configuration":
                return self._tool_get_configuration(arguments)
            case "set_configuration":
                return await self._tool_set_configuration(arguments)
            case "describe_configuration":
                return self._tool_describe_configuration(arguments)
            case _:
                raise KeyError(f"Unknown tool: {name}")

    def _tool_get_configuration(self, arguments: dict[str, Any]) -> str:
        path = arguments.get("path")
        if path:
            value = self.get(path)
            return json.dumps({"path": path, "value": value})
        # Return full config (excluding sensitive fields like credentials)
        safe = dict(self._raw)
        safe.pop("credentials", None)
        return json.dumps(safe)

    async def _tool_set_configuration(self, arguments: dict[str, Any]) -> str:
        path = arguments["path"]
        raw_value = arguments["value"]

        # Try to parse the value as JSON for non-string types
        value: Any = raw_value
        if isinstance(raw_value, str):
            try:
                value = json.loads(raw_value)
            except (json.JSONDecodeError, ValueError):
                value = raw_value

        result = await self.set(path, value)
        return json.dumps(result)

    def _tool_describe_configuration(self, arguments: dict[str, Any]) -> str:
        namespace = arguments.get("namespace")
        all_params = self.describe_all()

        if namespace:
            params = all_params.get(namespace, [])
            return json.dumps({
                "namespace": namespace,
                "parameters": [
                    {
                        "key": p.key,
                        "type": p.type.value,
                        "description": p.description,
                        "default": p.default,
                        "restart_required": p.restart_required,
                    }
                    for p in params
                ],
            })

        result: dict[str, Any] = {}
        for ns, params in all_params.items():
            result[ns] = [
                {
                    "key": p.key,
                    "type": p.type.value,
                    "description": p.description,
                    "default": p.default,
                    "restart_required": p.restart_required,
                }
                for p in params
            ]
        return json.dumps(result)
