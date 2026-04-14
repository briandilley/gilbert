"""Configuration service — runtime config management with entity storage backend.

Stores all non-bootstrap configuration in entity storage (one entity per
namespace in the ``gilbert.config`` collection).  Bootstrap sections
(storage, logging, web) remain in YAML because they're needed before
entity storage exists.

On first run the service seeds entity storage from the merged YAML config.
Subsequent starts read directly from entity storage.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable

import yaml

from gilbert.config import (
    GilbertConfig,
    OVERRIDE_CONFIG_PATH,
    YAML_ONLY_SECTIONS,
)
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionProvider,
    ConfigActionResult,
    ConfigParam,
    Configurable,
)
from gilbert.interfaces.events import Event, EventBusProvider
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import Query, StorageBackend
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)

logger = logging.getLogger(__name__)

# Type for factory functions that create services from config
ServiceFactory = Callable[[dict[str, Any]], Service]

# Entity storage collection names (within the gilbert. namespace)
_CONFIG_COLLECTION = "gilbert.config"
_META_COLLECTION = "gilbert.config_meta"
_SCHEMA_ENTITY_ID = "_schema"
_SCHEMA_VERSION = 1


class ConfigurationService(Service):
    """Manages runtime configuration with read/write, persistence, and hot-swap.

    - Holds the live GilbertConfig and raw config dict
    - On first run, seeds entity storage from merged YAML config
    - On subsequent runs, loads config from entity storage
    - Discovers Configurable services lazily
    - Tunable param changes: updates config, calls on_config_changed()
    - Structural param changes: uses registered factory to reconstruct service
    - Persists non-bootstrap changes to entity storage
    """

    def __init__(self, config: GilbertConfig) -> None:
        self._config = config
        self._raw: dict[str, Any] = config.model_dump()
        self._resolver: ServiceResolver | None = None
        self._service_manager: Any = None  # ServiceManager, set during start
        self._factories: dict[str, ServiceFactory] = {}
        self._storage: StorageBackend | None = None

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="configuration",
            capabilities=frozenset({"configuration", "ai_tools", "ws_handlers"}),
            optional=frozenset({"event_bus"}),
            events=frozenset({"config.changed"}),
        )

    @property
    def config(self) -> GilbertConfig:
        return self._config

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver
        from gilbert.interfaces.service import ServiceEnumerator

        # The resolver IS the ServiceManager
        if isinstance(resolver, ServiceEnumerator):
            self._service_manager = resolver
        logger.info("Configuration service started")

    def register_factory(self, namespace: str, factory: ServiceFactory) -> None:
        """Register a factory for reconstructing a service from config."""
        self._factories[namespace] = factory

    # --- Entity storage lifecycle ---

    async def seed_storage(self, storage: StorageBackend) -> None:
        """Seed entity storage from merged YAML config on first run.

        Checks for a ``_schema`` sentinel entity in ``gilbert.config_meta``.
        If absent, writes each non-bootstrap config section as an entity in
        ``gilbert.config`` and creates the sentinel.
        """
        self._storage = storage

        schema = await storage.get(_META_COLLECTION, _SCHEMA_ENTITY_ID)
        if schema is not None:
            logger.debug("Config already seeded (schema v%s)", schema.get("version"))
            return

        logger.info("First run — seeding entity storage from YAML config")
        for key, value in self._raw.items():
            if key in YAML_ONLY_SECTIONS:
                continue
            # Serialize through JSON to strip Pydantic/enum types
            try:
                safe = json.loads(json.dumps(value, default=str))
            except (TypeError, ValueError):
                logger.warning("Skipping non-serializable config section: %s", key)
                continue
            await storage.put(_CONFIG_COLLECTION, key, safe if isinstance(safe, dict) else {"_value": safe})

        # Write sentinel
        await storage.put(_META_COLLECTION, _SCHEMA_ENTITY_ID, {
            "version": _SCHEMA_VERSION,
            "migrated_at": datetime.now(timezone.utc).isoformat(),
            "source": "yaml",
        })
        logger.info("Config seeded to entity storage")

    async def load_from_storage(self, storage: StorageBackend) -> None:
        """Load config from entity storage, merging over YAML defaults.

        Entity-stored values override YAML defaults for non-bootstrap sections.
        The result is re-validated via ``GilbertConfig``.
        """
        self._storage = storage

        entities = await storage.query(Query(collection=_CONFIG_COLLECTION))
        if not entities:
            logger.debug("No config entities in storage — using YAML defaults")
            return

        for entity in entities:
            namespace = entity.pop("_id", None)
            if namespace is None or namespace in YAML_ONLY_SECTIONS:
                continue
            # Unwrap scalar values stored as {"_value": ...}
            if "_value" in entity and len(entity) == 1:
                self._raw[namespace] = entity["_value"]
            else:
                self._raw[namespace] = entity

        # Re-validate
        try:
            self._config = GilbertConfig.model_validate(self._raw)
            self._raw = self._config.model_dump()
        except Exception:
            logger.exception("Config validation failed after loading from storage — using YAML defaults")

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

    def get_section_safe(self, namespace: str) -> dict[str, Any]:
        """Get a service's config section as JSON-safe plain dicts.

        Pydantic model instances (e.g. credential objects) are converted
        to plain dicts via a JSON round-trip.
        """
        section = self.get_section(namespace)
        try:
            return json.loads(json.dumps(section, default=str))
        except (TypeError, ValueError):
            return {}

    # --- Write API ---

    async def set(self, path: str, value: Any) -> dict[str, Any]:
        """Set a config value, persist, validate, and notify/restart.

        Returns a status dict: {"status": "ok"} or {"status": "error", "message": ...}
        """
        # Redirect _services.X toggles to X.enabled
        parts = path.split(".")
        is_service_toggle = parts[0] == "_services" and len(parts) == 2
        if is_service_toggle:
            path = f"{parts[1]}.enabled"
            parts = path.split(".")

        # Determine namespace (first path segment)
        namespace = parts[0]

        # Check if this param is restart_required
        restart_needed = is_service_toggle  # Service toggles always require restart
        param_key = ".".join(parts[1:]) if len(parts) > 1 else ""
        configurable = self._find_configurable(namespace)
        if not restart_needed and configurable:
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

        # Persist
        await self._persist(namespace)

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

        from gilbert.interfaces.service import ServiceEnumerator

        if not isinstance(self._resolver, ServiceEnumerator):
            return result

        for svc in self._resolver.list_services().values():
            if isinstance(svc, Configurable):
                result[svc.config_namespace] = svc.config_params()

        return result

    def describe_categories(self) -> list[dict[str, Any]]:
        """Describe all config organized by category for the web UI.

        Returns a list of category dicts, each containing sections with
        their params and current values.
        """
        if self._resolver is None:
            return []

        from gilbert.interfaces.service import ServiceEnumerator

        if not isinstance(self._resolver, ServiceEnumerator):
            return []

        sm = self._resolver

        # Build the "Services" toggle section for toggleable services
        toggle_params: list[dict[str, Any]] = []
        toggle_values: dict[str, Any] = {}

        # Gather sections grouped by category
        all_services = sm.list_services()
        categories: dict[str, list[dict[str, Any]]] = {}
        for name in list(all_services.keys()):
            svc = all_services[name]
            if not isinstance(svc, Configurable):
                continue

            ns = svc.config_namespace
            if ns in YAML_ONLY_SECTIONS:
                continue

            info = svc.service_info() if isinstance(svc, Service) else None

            # Collect toggle params for the "Services" section
            if info is not None and info.toggleable:
                section = self.get_section_safe(ns)
                enabled = section.get("enabled", False)
                toggle_values[ns] = enabled
                toggle_params.append(self._serialize_param(
                    ConfigParam(
                        key=ns,
                        type=ToolParameterType.BOOLEAN,
                        description=info.toggle_description or f"Enable {info.name} service",
                        default=False,
                        restart_required=True,
                    ),
                    toggle_values,
                ))

            # Skip disabled toggleable services — their config section
            # only appears once the service is enabled via the Services tab.
            if info is not None and info.toggleable:
                section = self.get_section_safe(ns)
                if not section.get("enabled", False):
                    continue

            cat = svc.config_category
            params = svc.config_params()
            # Filter out the 'enabled' param — it's shown in the "Services" section
            if info is not None and info.toggleable:
                params = [p for p in params if p.key != "enabled"]
            if not params:
                continue
            started = name in sm.started_services
            failed = name in sm.failed_services

            section = self.get_section_safe(ns)

            actions: list[ConfigAction] = []
            if isinstance(svc, ConfigActionProvider):
                try:
                    actions = list(svc.config_actions())
                except Exception:
                    logger.exception("Error collecting config actions for %s", ns)

            categories.setdefault(cat, []).append({
                "namespace": ns,
                "service_name": name,
                "enabled": section.get("enabled", True),
                "started": started,
                "failed": failed,
                "params": [self._serialize_param(p, section) for p in params],
                "values": section,
                "actions": [self._serialize_action(a) for a in actions],
            })

        # Add the "Services" category if there are toggleable services
        if toggle_params:
            # Sort toggle params alphabetically by key
            toggle_params.sort(key=lambda p: p["key"])
            categories["Services"] = [{
                "namespace": "_services",
                "service_name": "_services",
                "enabled": True,
                "started": True,
                "failed": False,
                "params": toggle_params,
                "values": toggle_values,
            }]

        # Sort categories in a stable display order
        order = [
            "Services", "Intelligence", "Media", "Communication",
            "Security", "Monitoring", "Infrastructure",
        ]
        rank = {name: i for i, name in enumerate(order)}

        result = []
        for cat_name in sorted(categories.keys(), key=lambda c: (rank.get(c, 999), c)):
            sections = sorted(categories[cat_name], key=lambda s: s["namespace"])
            result.append({"name": cat_name, "sections": sections})
        return result

    # --- Param serialization ---

    def _serialize_param(self, p: ConfigParam, values: dict[str, Any] | None = None) -> dict[str, Any]:
        """Serialize a ConfigParam for the WS response, resolving dynamic choices.

        ``choices`` may be a list of plain strings (label = value) or a
        list of ``{"value": str, "label": str}`` objects when a dynamic
        source wants to show a friendly label distinct from the stored
        value (e.g. mailbox name vs. mailbox id).
        """
        choices: list[Any] | None = list(p.choices) if p.choices else None
        if p.choices_from:
            resolved = self._resolve_dynamic_choices(p.choices_from)
            if resolved is not None:
                choices = resolved  # may be list[str] or list[dict]
            elif values is not None and p.type.value == "array":
                # Fallback: use currently stored values as choices so the UI
                # can at least show what's selected (e.g., before backend starts)
                current = values
                for part in p.key.split("."):
                    if isinstance(current, dict):
                        current = current.get(part)
                    else:
                        current = None
                        break
                if isinstance(current, list) and current:
                    choices = [str(v) for v in current]
        return {
            "key": p.key,
            "type": p.type.value,
            "description": p.description,
            "default": p.default,
            "restart_required": p.restart_required,
            "sensitive": p.sensitive,
            "choices": choices,
            "multiline": p.multiline,
            "backend_param": p.backend_param,
        }

    def _serialize_action(self, a: ConfigAction) -> dict[str, Any]:
        """Serialize a ConfigAction for the WS response."""
        return {
            "key": a.key,
            "label": a.label,
            "description": a.description,
            "backend_action": a.backend_action,
            "backend": a.backend,
            "confirm": a.confirm,
            "required_role": a.required_role,
            "hidden": a.hidden,
        }

    def _serialize_action_result(self, r: ConfigActionResult) -> dict[str, Any]:
        return {
            "status": r.status,
            "message": r.message,
            "open_url": r.open_url,
            "followup_action": r.followup_action,
            "data": r.data,
        }

    def _resolve_dynamic_choices(
        self, source: str,
    ) -> list[str] | list[dict[str, str]] | None:
        """Resolve a dynamic choices source to a list of values or labeled options.

        Returns either a list of plain strings (label = value) or a list
        of ``{"value": ..., "label": ...}`` dicts when the source wants
        the dropdown to show a friendly label distinct from the stored
        value.
        """
        if self._resolver is None:
            return None
        if source == "speakers":
            from gilbert.interfaces.speaker import CachedSpeakerLister

            svc = self._resolver.get_capability("speaker_control")
            if isinstance(svc, CachedSpeakerLister):
                try:
                    return [s.name for s in svc.cached_speakers]
                except Exception:
                    logger.debug("speakers dynamic choices failed", exc_info=True)
        elif source == "doorbells":
            from gilbert.interfaces.doorbell import AvailableDoorbellLister

            svc = self._resolver.get_capability("doorbell")
            if isinstance(svc, AvailableDoorbellLister):
                try:
                    return list(svc.available_doorbells)
                except Exception:
                    logger.debug("doorbells dynamic choices failed", exc_info=True)
        elif source == "music_services":
            from gilbert.interfaces.music import LinkedMusicServiceLister

            svc = self._resolver.get_capability("music")
            if isinstance(svc, LinkedMusicServiceLister):
                try:
                    linked = svc.list_linked_services()
                    if linked:
                        return [str(s) for s in linked]
                except Exception:
                    logger.debug("music_services dynamic choices failed", exc_info=True)
        elif source == "inbox_mailboxes":
            # Returns labeled choices: the dropdown shows the friendly
            # name + email but stores the bare mailbox id as the value.
            # InboxService maintains a sync-readable ``cached_mailboxes``
            # property that's refreshed at boot and on every CRUD op.
            from gilbert.interfaces.inbox import CachedMailboxLister

            svc = self._resolver.get_capability("inbox")
            if isinstance(svc, CachedMailboxLister):
                try:
                    return [
                        {
                            "value": m.id,
                            "label": (
                                f"{m.name} ({m.email_address})"
                                if m.email_address else m.name
                            ),
                        }
                        for m in svc.cached_mailboxes
                    ]
                except Exception:
                    logger.debug("inbox_mailboxes dynamic choices failed", exc_info=True)
        return None

    # --- Sensitive field masking ---

    _SENSITIVE_KEYS = frozenset({"api_key", "password", "client_secret", "secret"})
    _MASK = "********"

    @classmethod
    def _mask_sensitive(
        cls, section: dict[str, Any], params: list[ConfigParam],
    ) -> dict[str, Any]:
        """Return a copy of *section* with sensitive fields masked."""
        sensitive_keys = {p.key for p in params if p.sensitive}
        result = dict(section)
        for key in sensitive_keys:
            if key in result and result[key]:
                result[key] = cls._MASK
        # Also mask any nested dict values that look like credentials
        return cls._deep_mask(result)

    @classmethod
    def _deep_mask(cls, d: dict[str, Any]) -> dict[str, Any]:
        """Recursively mask values whose keys match known sensitive names."""
        result = {}
        for k, v in d.items():
            if k in cls._SENSITIVE_KEYS and isinstance(v, str) and v:
                result[k] = cls._MASK
            elif isinstance(v, dict):
                result[k] = cls._deep_mask(v)
            else:
                result[k] = v
        return result

    # --- Internal ---

    def _find_configurable(self, namespace: str) -> Configurable | None:
        """Find the Configurable service for a given namespace."""
        if self._resolver is None:
            return None

        from gilbert.interfaces.service import ServiceEnumerator

        if not isinstance(self._resolver, ServiceEnumerator):
            return None

        # Search all registered services (not just started ones) so that
        # disabled services' config can still be viewed and modified.
        for svc in self._resolver.list_services().values():
            if isinstance(svc, Configurable) and svc.config_namespace == namespace:
                return svc
        return None

    async def _handle_restart(self, namespace: str) -> dict[str, Any]:
        """Handle a structural config change by restarting the service.

        All services are always registered. This handles:
        - Toggling enabled/disabled (service restarts with new config)
        - Backend or structural config changes (factory creates new instance)
        """
        if self._service_manager is None:
            return {"status": "error", "message": "No service manager available for restart"}

        # Find the registered service for this namespace
        svc_name: str | None = None
        for name, svc in self._service_manager.list_services().items():
            if isinstance(svc, Configurable) and svc.config_namespace == namespace:
                svc_name = name
                break

        if svc_name is None:
            return {"status": "error", "message": f"No service registered for '{namespace}'"}

        factory = self._factories.get(namespace)

        try:
            if factory is not None:
                # Factory exists — create a fresh instance and hot-swap
                section = self.get_section(namespace)
                new_svc = factory(section)
                await self._service_manager.restart_service(svc_name, new_svc)
            else:
                # No factory — just restart the existing service in place
                await self._service_manager.restart_service(svc_name)
            return {"status": "ok", "message": f"Service '{namespace}' restarted"}
        except Exception as exc:
            logger.exception("Failed to restart service %s", namespace)
            return {"status": "error", "message": f"Restart failed: {exc}"}

    @staticmethod
    def _set_nested(d: dict[str, Any], keys: list[str], value: Any) -> None:
        """Set a value in a nested dict by key path."""
        for key in keys[:-1]:
            if key not in d or not isinstance(d[key], dict):
                d[key] = {}
            d = d[key]
        d[keys[-1]] = value

    async def _persist(self, namespace: str) -> None:
        """Persist a config change to the appropriate backend.

        Non-bootstrap sections go to entity storage.
        Bootstrap sections (storage, logging, web) go to .gilbert/config.yaml.
        """
        if namespace not in YAML_ONLY_SECTIONS and self._storage is not None:
            await self._persist_to_storage(namespace)
        else:
            self._persist_to_yaml()

    async def _persist_to_storage(self, namespace: str) -> None:
        """Write a single config section to entity storage."""
        section = self._raw.get(namespace)
        if section is None or self._storage is None:
            return
        try:
            safe = json.loads(json.dumps(section, default=str))
            if isinstance(safe, dict):
                await self._storage.put(_CONFIG_COLLECTION, namespace, safe)
            else:
                await self._storage.put(_CONFIG_COLLECTION, namespace, {"_value": safe})
            logger.debug("Config persisted to entity storage: %s", namespace)
        except Exception:
            logger.exception("Failed to persist config section %s to entity storage", namespace)

    def _persist_to_yaml(self) -> None:
        """Write bootstrap config sections to .gilbert/config.yaml."""
        try:
            override_path = OVERRIDE_CONFIG_PATH
            existing: dict[str, Any] = {}
            if override_path.exists():
                with open(override_path) as f:
                    raw = yaml.safe_load(f)
                    if isinstance(raw, dict):
                        existing = raw

            # Only write YAML-only sections
            for section in YAML_ONLY_SECTIONS:
                if section in self._raw:
                    existing[section] = self._raw[section]

            override_path.parent.mkdir(parents=True, exist_ok=True)
            safe = json.loads(json.dumps(existing, default=str))
            with open(override_path, "w") as f:
                yaml.safe_dump(safe, f, default_flow_style=False, sort_keys=False)

            logger.debug("Bootstrap config persisted to %s", override_path)
        except Exception:
            logger.exception("Failed to persist bootstrap config")

    async def _publish_config_event(self, path: str, value: Any) -> None:
        """Publish a config.changed event if event bus is available."""
        if self._resolver is None:
            return
        bus_svc = self._resolver.get_capability("event_bus")
        if bus_svc is None:
            return
        if isinstance(bus_svc, EventBusProvider):
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
                slash_group="config",
                slash_command="get",
                slash_help="Read config by dot-path: /config get [path]",
                description="Get configuration values. Returns the full config or a specific value by path.",
                parameters=[
                    ToolParameter(
                        name="path",
                        type=ToolParameterType.STRING,
                        description="Dot-path to a config value (e.g., 'ai.settings.temperature'). Omit for full config.",
                        required=False,
                    ),
                ],
                required_role="admin",
            ),
            ToolDefinition(
                name="set_configuration",
                slash_group="config",
                slash_command="set",
                slash_help="Set a config value: /config set <path> <value>",
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
                required_role="admin",
            ),
            ToolDefinition(
                name="describe_configuration",
                slash_group="config",
                slash_command="describe",
                slash_help="Show config schema: /config describe [namespace]",
                description="Describe all configurable parameters with types, descriptions, and defaults.",
                parameters=[
                    ToolParameter(
                        name="namespace",
                        type=ToolParameterType.STRING,
                        description="Service namespace to describe (e.g., 'ai', 'tts'). Omit for all services.",
                        required=False,
                    ),
                ],
                required_role="admin",
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
                        "sensitive": p.sensitive,
                        "choices": list(p.choices) if p.choices else None,
                        "multiline": p.multiline,
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
                    "sensitive": p.sensitive,
                    "choices": list(p.choices) if p.choices else None,
                }
                for p in params
            ]
        return json.dumps(result)

    # --- WsHandlerProvider protocol ---

    def get_ws_handlers(self) -> dict[str, Any]:
        return {
            "config.describe.list": self._ws_describe_list,
            "config.section.get": self._ws_section_get,
            "config.section.set": self._ws_section_set,
            "config.section.reset": self._ws_section_reset,
            "config.action.invoke": self._ws_action_invoke,
        }

    async def _ws_describe_list(
        self, conn: Any, frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Return all config organized by category."""
        categories = self.describe_categories()
        return {
            "type": "config.describe.list.result",
            "ref": frame.get("id"),
            "categories": categories,
        }

    async def _ws_section_get(
        self, conn: Any, frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Return a single namespace's params + values."""
        namespace = frame.get("namespace", "")
        if not namespace:
            return {
                "type": "gilbert.error", "ref": frame.get("id"),
                "error": "namespace required", "code": 400,
            }

        configurable = self._find_configurable(namespace)
        section = self.get_section_safe(namespace)

        if configurable is None:
            return {
                "type": "config.section.get.result",
                "ref": frame.get("id"),
                "namespace": namespace,
                "params": [],
                "values": section,
            }

        params = configurable.config_params()
        return {
            "type": "config.section.get.result",
            "ref": frame.get("id"),
            "namespace": namespace,
            "params": [
                {
                    "key": p.key,
                    "type": p.type.value,
                    "description": p.description,
                    "default": p.default,
                    "restart_required": p.restart_required,
                    "sensitive": p.sensitive,
                    "choices": list(p.choices) if p.choices else None,
                }
                for p in params
            ],
            "values": section,
        }

    async def _ws_section_set(
        self, conn: Any, frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Set one or more values in a namespace."""
        namespace = frame.get("namespace", "")
        values = frame.get("values", {})
        if not namespace or not isinstance(values, dict):
            return {
                "type": "gilbert.error", "ref": frame.get("id"),
                "error": "namespace and values required", "code": 400,
            }

        # Skip masked values (user didn't change the sensitive field)
        filtered = {k: v for k, v in values.items() if v != self._MASK}

        results: dict[str, Any] = {}
        for key, value in filtered.items():
            path = f"{namespace}.{key}" if key else namespace
            result = await self.set(path, value)
            results[key] = result

        return {
            "type": "config.section.set.result",
            "ref": frame.get("id"),
            "namespace": namespace,
            "results": results,
        }

    async def _ws_action_invoke(
        self, conn: Any, frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Invoke a ConfigAction on a service, RBAC-checked by role."""
        namespace = frame.get("namespace", "")
        key = frame.get("key", "")
        payload = frame.get("payload", {}) or {}
        if not namespace or not key:
            return {
                "type": "gilbert.error", "ref": frame.get("id"),
                "error": "namespace and key required", "code": 400,
            }
        if not isinstance(payload, dict):
            return {
                "type": "gilbert.error", "ref": frame.get("id"),
                "error": "payload must be an object", "code": 400,
            }

        configurable = self._find_configurable(namespace)
        if configurable is None or not isinstance(configurable, ConfigActionProvider):
            return {
                "type": "gilbert.error", "ref": frame.get("id"),
                "error": f"No actions available for '{namespace}'", "code": 404,
            }

        # Find the action descriptor so we can RBAC-check before invoking
        try:
            actions = list(configurable.config_actions())
        except Exception:
            logger.exception("Error collecting config actions for %s", namespace)
            return {
                "type": "gilbert.error", "ref": frame.get("id"),
                "error": "Failed to list actions", "code": 500,
            }
        action = next((a for a in actions if a.key == key), None)
        if action is None:
            return {
                "type": "gilbert.error", "ref": frame.get("id"),
                "error": f"Unknown action '{key}'", "code": 404,
            }

        # RBAC check
        if self._resolver is not None:
            acl = self._resolver.get_capability("access_control")
            from gilbert.interfaces.auth import AccessControlProvider

            if isinstance(acl, AccessControlProvider):
                required_level = acl.get_role_level(action.required_role)
                user_level = getattr(conn, "user_level", 999)
                if user_level > required_level:
                    return {
                        "type": "gilbert.error", "ref": frame.get("id"),
                        "error": "Insufficient permissions for this action",
                        "code": 403,
                    }

        try:
            result = await configurable.invoke_config_action(key, payload)
        except Exception as exc:
            logger.exception("Config action %s.%s failed", namespace, key)
            result = ConfigActionResult(
                status="error", message=f"Action failed: {exc}",
            )

        return {
            "type": "config.action.invoke.result",
            "ref": frame.get("id"),
            "namespace": namespace,
            "key": key,
            "result": self._serialize_action_result(result),
        }

    async def _ws_section_reset(
        self, conn: Any, frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Reset a namespace to its default values."""
        namespace = frame.get("namespace", "")
        if not namespace:
            return {
                "type": "gilbert.error", "ref": frame.get("id"),
                "error": "namespace required", "code": 400,
            }

        configurable = self._find_configurable(namespace)
        if configurable is None:
            return {
                "type": "gilbert.error", "ref": frame.get("id"),
                "error": f"No configurable service for '{namespace}'", "code": 404,
            }

        # Build default values from config_params
        defaults: dict[str, Any] = {}
        for param in configurable.config_params():
            if param.default is not None:
                defaults[param.key] = param.default

        # Apply each default
        for key, value in defaults.items():
            await self.set(f"{namespace}.{key}", value)

        return {
            "type": "config.section.reset.result",
            "ref": frame.get("id"),
            "namespace": namespace,
            "status": "ok",
        }
