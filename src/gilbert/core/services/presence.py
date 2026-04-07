"""Presence service — wraps a PresenceBackend as a discoverable service.

Polls the backend periodically, detects state changes, and publishes
events on the event bus:
- ``presence.arrived`` — user became present or nearby
- ``presence.departed`` — user became away
- ``presence.changed`` — any state transition
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.events import Event, EventBus
from gilbert.interfaces.presence import (
    PresenceBackend,
    PresenceState,
    UserPresence,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)

logger = logging.getLogger(__name__)

# Default polling interval in seconds
_DEFAULT_POLL_INTERVAL = 30


class PresenceService(Service):
    """Exposes a PresenceBackend as a discoverable service with AI tools.

    Periodically polls the backend for state changes and publishes events.
    """

    def __init__(self, backend: PresenceBackend) -> None:
        self._backend = backend
        self._config: dict[str, object] = {}
        self._poll_interval: float = _DEFAULT_POLL_INTERVAL
        self._event_bus: EventBus | None = None
        self._storage: Any = None
        self._resolver: ServiceResolver | None = None
        # Last-known state per user for change detection
        self._last_state: dict[str, PresenceState] = {}

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="presence",
            capabilities=frozenset({"presence", "ai_tools"}),
            requires=frozenset({"users", "scheduler"}),
            optional=frozenset({"configuration", "event_bus", "credentials", "entity_storage"}),
        )

    @property
    def backend(self) -> PresenceBackend:
        return self._backend

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver
        # Event bus for publishing presence changes
        event_bus_svc = resolver.get_capability("event_bus")
        if event_bus_svc is not None:
            from gilbert.core.services.event_bus import EventBusService

            if isinstance(event_bus_svc, EventBusService):
                self._event_bus = event_bus_svc.bus

        # Config
        full_section: dict[str, Any] = {}
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None:
            from gilbert.core.services.configuration import ConfigurationService

            if isinstance(config_svc, ConfigurationService):
                full_section = config_svc.get_section("presence")
                self._apply_config(full_section)

        # Storage for persisting presence state
        storage_svc = resolver.get_capability("entity_storage")
        if storage_svc is not None:
            from gilbert.core.services.storage import StorageService

            if isinstance(storage_svc, StorageService):
                self._storage = storage_svc.backend

        # Pass the full config section to the backend (not just settings).
        # Also resolve credentials and inject user service for name resolution.
        init_config: dict[str, object] = dict(full_section)

        user_svc = resolver.get_capability("users")
        if user_svc is not None:
            init_config["_user_service"] = user_svc

        cred_svc = resolver.get_capability("credentials")
        if cred_svc is not None:
            from gilbert.core.services.credentials import CredentialService

            if isinstance(cred_svc, CredentialService):
                for key in ("unifi_network", "unifi_protect"):
                    sub = init_config.get(key)
                    if isinstance(sub, dict) and sub.get("credential"):
                        cred = cred_svc.get(str(sub["credential"]))
                        if cred:
                            sub["_resolved_credential"] = cred

        await self._backend.initialize(init_config)

        # Seed initial state and persist to entity store
        try:
            all_presence = await self._backend.get_all_presence()
            for p in all_presence:
                self._last_state[p.user_id] = p.state
            await self._persist_presence(all_presence)
        except Exception:
            logger.warning("Failed to seed initial presence state", exc_info=True)

        # Register polling with scheduler
        scheduler = resolver.get_capability("scheduler")
        if scheduler is not None:
            from gilbert.core.services.scheduler import SchedulerService
            from gilbert.interfaces.scheduler import Schedule

            if isinstance(scheduler, SchedulerService):
                scheduler.add_job(
                    name="presence-poll",
                    schedule=Schedule.every(self._poll_interval),
                    callback=self._check_for_changes,
                    system=True,
                )

        logger.info(
            "Presence service started (poll_interval=%.0fs, tracking=%d users)",
            self._poll_interval,
            len(self._last_state),
        )

    def _apply_config(self, section: dict[str, Any]) -> None:
        self._config = section.get("settings", self._config)
        poll = section.get("poll_interval_seconds")
        if poll is not None:
            self._poll_interval = float(poll)

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "presence"

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="backend", type=ToolParameterType.STRING,
                description="Presence backend type.",
                restart_required=True,
            ),
            ConfigParam(
                key="enabled", type=ToolParameterType.BOOLEAN,
                description="Whether the presence service is enabled.",
                default=False, restart_required=True,
            ),
            ConfigParam(
                key="poll_interval_seconds", type=ToolParameterType.NUMBER,
                description="How often to poll for presence changes (seconds).",
                default=_DEFAULT_POLL_INTERVAL,
            ),
            ConfigParam(
                key="settings", type=ToolParameterType.OBJECT,
                description="Backend-specific settings.",
                default={},
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._apply_config(config)

    async def stop(self) -> None:
        await self._backend.close()

    # --- Polling and event detection ---

    async def _check_for_changes(self) -> None:
        """Poll backend, compare to last-known state, publish events for changes."""
        try:
            all_presence = await self._backend.get_all_presence()
        except Exception:
            logger.warning("Failed to poll presence", exc_info=True)
            return

        for p in all_presence:
            old_state = self._last_state.get(p.user_id)
            if old_state == p.state:
                continue

            self._last_state[p.user_id] = p.state

            # New user or state changed
            if old_state is not None:
                await self._emit_change(p, old_state)
            else:
                # First time seeing this user — don't emit arrived/departed
                logger.debug("New tracked user: %s (%s)", p.user_id, p.state.value)

        # Persist current state to entity store
        await self._persist_presence(all_presence)

    async def _emit_change(self, presence: UserPresence, old_state: PresenceState) -> None:
        """Publish presence change events."""
        if self._event_bus is None:
            return

        data = {
            "user_id": presence.user_id,
            "state": presence.state.value,
            "previous_state": old_state.value,
            "since": presence.since,
            "source": presence.source,
        }

        # Always emit the generic changed event
        await self._event_bus.publish(Event(
            event_type="presence.changed",
            data=data,
            source="presence",
        ))

        # Emit specific arrived/departed events
        arrived_states = {PresenceState.PRESENT, PresenceState.NEARBY}
        was_here = old_state in arrived_states
        is_here = presence.state in arrived_states

        if is_here and not was_here:
            await self._event_bus.publish(Event(
                event_type="presence.arrived",
                data=data,
                source="presence",
            ))
            logger.info("User %s arrived (%s)", presence.user_id, presence.state.value)
        elif was_here and not is_here:
            await self._event_bus.publish(Event(
                event_type="presence.departed",
                data=data,
                source="presence",
            ))
            logger.info("User %s departed", presence.user_id)
        else:
            logger.info(
                "User %s presence changed: %s → %s",
                presence.user_id, old_state.value, presence.state.value,
            )

    # --- Entity persistence ---

    async def _persist_presence(self, presence_list: list[UserPresence]) -> None:
        """Write current presence state to the entity store for quick lookups."""
        if self._storage is None:
            return
        try:
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc).isoformat()
            for p in presence_list:
                await self._storage.put("user_presence", p.user_id, {
                    "user_id": p.user_id,
                    "state": p.state.value,
                    "since": p.since,
                    "source": p.source,
                    "updated_at": now,
                })
        except Exception:
            logger.warning("Failed to persist presence to entity store", exc_info=True)

    # --- Public API ---

    async def get_presence(self, user_id: str) -> UserPresence:
        """Get presence for a specific user."""
        return await self._backend.get_presence(user_id)

    async def get_all_presence(self) -> list[UserPresence]:
        """Get presence for all tracked users."""
        return await self._backend.get_all_presence()

    async def is_present(self, user_id: str) -> bool:
        """Check if a user is present."""
        p = await self._backend.get_presence(user_id)
        return p.state == PresenceState.PRESENT

    async def is_nearby(self, user_id: str) -> bool:
        """Check if a user is present or nearby."""
        p = await self._backend.get_presence(user_id)
        return p.state in (PresenceState.PRESENT, PresenceState.NEARBY)

    async def who_is_here(self) -> list[UserPresence]:
        """Get all users who are present or nearby."""
        all_presence = await self._backend.get_all_presence()
        return [p for p in all_presence if p.state in (PresenceState.PRESENT, PresenceState.NEARBY)]

    # --- ToolProvider protocol ---

    @property
    def tool_provider_name(self) -> str:
        return "presence"

    def get_tools(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="check_presence",
                description="Check if a specific user is present, nearby, or away.",
                parameters=[
                    ToolParameter(
                        name="user_id",
                        type=ToolParameterType.STRING,
                        description="The user ID to check.",
                    ),
                ],
                required_role="everyone",
            ),
            ToolDefinition(
                name="who_is_here",
                description="List all users who are currently present or nearby.",
                required_role="everyone",
            ),
            ToolDefinition(
                name="list_all_presence",
                description="List presence state for all tracked users.",
                required_role="everyone",
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        match name:
            case "check_presence":
                return await self._tool_check_presence(arguments)
            case "who_is_here":
                return await self._tool_who_is_here()
            case "list_all_presence":
                return await self._tool_list_all()
            case _:
                raise KeyError(f"Unknown tool: {name}")

    async def _tool_check_presence(self, arguments: dict[str, Any]) -> str:
        user_id = arguments["user_id"]
        p = await self.get_presence(user_id)
        return json.dumps(await self._presence_to_dict(p))

    async def _tool_who_is_here(self) -> str:
        present = await self.who_is_here()
        return json.dumps([await self._presence_to_dict(p) for p in present])

    async def _tool_list_all(self) -> str:
        all_p = await self.get_all_presence()
        return json.dumps([await self._presence_to_dict(p) for p in all_p])

    async def _presence_to_dict(self, p: UserPresence) -> dict[str, Any]:
        """Convert a UserPresence to a dict with resolved display name."""
        display_name = p.user_id
        if self._resolver is not None:
            user_svc = self._resolver.get_capability("users")
            if user_svc is not None:
                try:
                    user = await user_svc.backend.get_user(p.user_id)
                    if user:
                        display_name = user.get("display_name", p.user_id)
                except Exception:
                    pass
        return {
            "user_id": p.user_id,
            "name": display_name,
            "state": p.state.value,
            "since": p.since,
            "source": p.source,
        }
