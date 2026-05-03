"""Presence service — wraps a PresenceBackend as a discoverable service.

Polls the backend periodically and diffs against stored records in the
entity store. Record exists = user is here. No record = user is gone.
Publishes events on the event bus:
- ``presence.arrived`` — user appeared in poll (record created)
- ``presence.departed`` — user disappeared from poll (record deleted)
"""

import json
import logging
from datetime import UTC
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
)
from gilbert.interfaces.events import Event, EventBus, EventBusProvider
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

    def __init__(self) -> None:
        self._backend: PresenceBackend | None = None
        self._backend_name: str = "unifi"
        self._enabled: bool = False
        self._config: dict[str, object] = {}
        self._poll_interval: float = _DEFAULT_POLL_INTERVAL
        self._event_bus: EventBus | None = None
        self._storage: Any = None
        self._resolver: ServiceResolver | None = None
        self._first_poll: bool = True

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="presence",
            capabilities=frozenset({"presence", "ai_tools"}),
            requires=frozenset({"users", "scheduler"}),
            optional=frozenset({"configuration", "event_bus", "credentials", "entity_storage"}),
            events=frozenset({"presence.arrived", "presence.departed"}),
            toggleable=True,
            toggle_description="User presence detection",
        )

    @property
    def backend(self) -> PresenceBackend | None:
        return self._backend

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver
        # Event bus for publishing presence changes
        event_bus_svc = resolver.get_capability("event_bus")
        if event_bus_svc is not None:
            if isinstance(event_bus_svc, EventBusProvider):
                self._event_bus = event_bus_svc.bus

        # Config
        full_section: dict[str, Any] = {}
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                full_section = config_svc.get_section("presence")
                self._apply_config(full_section)

                if not full_section.get("enabled", False):
                    logger.info("Presence service disabled")
                    return

        self._enabled = True

        # Storage for persisting presence state
        storage_svc = resolver.get_capability("entity_storage")
        if storage_svc is not None:
            from gilbert.interfaces.storage import StorageProvider

            if isinstance(storage_svc, StorageProvider):
                self._storage = storage_svc.backend

        # Create backend from registry
        backend_name = full_section.get("backend", "unifi")
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                section = config_svc.get_section("presence")
                backend_name = section.get("backend", "unifi")
        self._backend_name = backend_name

        backends = PresenceBackend.registered_backends()
        backend_cls = backends.get(backend_name)
        if backend_cls is None:
            raise ValueError(f"Unknown presence backend: {backend_name}")
        self._backend = backend_cls()

        # Pass the full config section to the backend (not just settings).
        # Also resolve credentials and inject user service for name resolution.
        init_config: dict[str, object] = dict(full_section)

        user_svc = resolver.get_capability("users")
        if user_svc is not None:
            init_config["_user_service"] = user_svc

        await self._backend.initialize(init_config)

        # First poll flag — on the very first poll we skip event emission
        # for users that have no prior stored state (prevents spurious
        # arrived events for everyone on fresh install).
        self._first_poll = True

        # Register polling with scheduler
        scheduler = resolver.get_capability("scheduler")
        if scheduler is not None:
            from gilbert.interfaces.scheduler import Schedule, SchedulerProvider

            if isinstance(scheduler, SchedulerProvider):
                scheduler.add_job(
                    name="presence-poll",
                    schedule=Schedule.every(self._poll_interval),
                    callback=self._check_for_changes,
                    system=True,
                )

        logger.info(
            "Presence service started (poll_interval=%.0fs)",
            self._poll_interval,
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

    @property
    def config_category(self) -> str:
        return "Monitoring"

    def config_params(self) -> list[ConfigParam]:
        from gilbert.interfaces.presence import PresenceBackend

        params = [
            ConfigParam(
                key="backend",
                type=ToolParameterType.STRING,
                description="Presence backend type.",
                default="unifi",
                restart_required=True,
                choices=tuple(PresenceBackend.registered_backends().keys()),
            ),
            ConfigParam(
                key="poll_interval_seconds",
                type=ToolParameterType.NUMBER,
                description="How often to poll for presence changes (seconds).",
                default=_DEFAULT_POLL_INTERVAL,
            ),
        ]
        backends = PresenceBackend.registered_backends()
        backend_cls = backends.get(self._backend_name)
        if backend_cls is not None:
            for bp in backend_cls.backend_config_params():
                params.append(
                    ConfigParam(
                        key=bp.key,
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
        self._apply_config(config)

    # --- ConfigActionProvider ---

    def config_actions(self) -> list[ConfigAction]:
        return all_backend_actions(
            registry=PresenceBackend.registered_backends(),
            current_backend=self._backend,
        )

    async def invoke_config_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        return await invoke_backend_action(self._backend, key, payload)

    async def stop(self) -> None:
        if self._backend is not None:
            await self._backend.close()
            self._backend = None
        self._enabled = False

    # --- Polling and event detection ---

    async def _check_for_changes(self) -> None:
        """Poll backend, diff against stored records, emit events, persist.

        Record exists = user is here. No record = user is gone.
        """
        if self._backend is None:
            return

        # 1. Load who was here last poll (record exists = was here)
        previously_here = await self._load_present_user_ids()

        # 2. Poll the backend
        try:
            all_presence = await self._backend.get_all_presence()
        except Exception:
            logger.warning("Failed to poll presence", exc_info=True)
            return

        # 3. Who is here now
        currently_here: dict[str, UserPresence] = {p.user_id: p for p in all_presence}

        # 4. Arrived: in current poll but not in stored records
        for user_id, p in currently_here.items():
            if user_id not in previously_here:
                if not self._first_poll:
                    await self._emit_arrived(p)
                else:
                    logger.debug("Initial tracked user: %s (%s)", user_id, p.state.value)

        # 5. Departed: in stored records but not in current poll
        for user_id in previously_here:
            if user_id not in currently_here:
                await self._emit_departed(user_id)

        # 6. Persist: delete departed, upsert current
        await self._sync_stored_presence(previously_here, currently_here)

        self._first_poll = False

    async def _emit_arrived(self, presence: UserPresence) -> None:
        """Publish presence.arrived event."""
        logger.info("User %s arrived (%s)", presence.user_id, presence.state.value)
        if self._event_bus is None:
            return
        data = {
            "user_id": presence.user_id,
            "state": presence.state.value,
            "since": presence.since,
            "source": presence.source,
        }
        await self._event_bus.publish(
            Event(
                event_type="presence.arrived",
                data=data,
                source="presence",
            )
        )

    async def _emit_departed(self, user_id: str) -> None:
        """Publish presence.departed event."""
        logger.info("User %s departed", user_id)
        if self._event_bus is None:
            return
        data = {"user_id": user_id, "state": "away", "source": "presence"}
        await self._event_bus.publish(
            Event(
                event_type="presence.departed",
                data=data,
                source="presence",
            )
        )

    # --- Entity persistence ---

    _COLLECTION = "user_presence"

    async def _load_present_user_ids(self) -> set[str]:
        """Load the set of user IDs that have a stored presence record (= were here)."""
        if self._storage is None:
            return set()
        try:
            from gilbert.interfaces.storage import Query

            records = await self._storage.query(
                Query(
                    collection=self._COLLECTION,
                    limit=500,
                )
            )
            return {r["user_id"] for r in records if "user_id" in r}
        except Exception:
            logger.warning("Failed to load stored presence", exc_info=True)
            return set()

    async def _sync_stored_presence(
        self,
        previously_here: set[str],
        currently_here: dict[str, UserPresence],
    ) -> None:
        """Sync entity store: delete departed users, upsert current ones."""
        if self._storage is None:
            return
        try:
            from datetime import datetime

            now = datetime.now(UTC).isoformat()

            # Remove records for users who left
            for user_id in previously_here:
                if user_id not in currently_here:
                    await self._storage.delete(self._COLLECTION, user_id)

            # Upsert records for users who are here
            for p in currently_here.values():
                await self._storage.put(
                    self._COLLECTION,
                    p.user_id,
                    {
                        "user_id": p.user_id,
                        "state": p.state.value,
                        "since": p.since or "",
                        "source": p.source or "",
                        "updated_at": now,
                    },
                )
        except Exception:
            logger.warning("Failed to persist presence to entity store", exc_info=True)

    # --- Public API ---

    async def get_presence(self, user_id: str) -> UserPresence:
        """Get presence for a specific user."""
        if self._backend is None:
            return UserPresence(user_id=user_id, state=PresenceState.UNKNOWN)
        return await self._backend.get_presence(user_id)

    async def get_all_presence(self) -> list[UserPresence]:
        """Get presence for all tracked users."""
        if self._backend is None:
            return []
        return await self._backend.get_all_presence()

    async def is_present(self, user_id: str) -> bool:
        """Check if a user is present."""
        if self._backend is None:
            return False
        p = await self._backend.get_presence(user_id)
        return p.state == PresenceState.PRESENT

    async def is_nearby(self, user_id: str) -> bool:
        """Check if a user is present or nearby."""
        if self._backend is None:
            return False
        p = await self._backend.get_presence(user_id)
        return p.state in (PresenceState.PRESENT, PresenceState.NEARBY)

    async def who_is_here(self) -> list[UserPresence]:
        """Get all users who are present or nearby."""
        if self._backend is None:
            return []
        all_presence = await self._backend.get_all_presence()
        return [p for p in all_presence if p.state in (PresenceState.PRESENT, PresenceState.NEARBY)]

    # --- ToolProvider protocol ---

    @property
    def tool_provider_name(self) -> str:
        return "presence"

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        return [
            ToolDefinition(
                name="check_presence",
                slash_group="presence",
                slash_command="check",
                slash_help="Check one user: /presence check <user_id>",
                description="Check if a specific user is present, nearby, or away.",
                parameters=[
                    ToolParameter(
                        name="user_id",
                        type=ToolParameterType.STRING,
                        description="The user ID to check.",
                    ),
                ],
                required_role="everyone",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="who_is_here",
                slash_group="presence",
                slash_command="here",
                slash_help="Who's currently around: /presence here",
                description="List all users who are currently present or nearby.",
                required_role="everyone",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="list_all_presence",
                slash_group="presence",
                slash_command="all",
                slash_help="Full presence snapshot: /presence all",
                description="List presence state for all tracked users.",
                required_role="everyone",
                parallel_safe=True,
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
        resolved = await self._resolve_presence(p)
        if resolved is None:
            return json.dumps({"error": f"User '{user_id}' not found."})
        return json.dumps(resolved)

    async def _tool_who_is_here(self) -> str:
        present = await self.who_is_here()
        resolved = await self._resolve_presence_list(present)
        return json.dumps(resolved)

    async def _tool_list_all(self) -> str:
        all_p = await self.get_all_presence()
        resolved = await self._resolve_presence_list(all_p)
        return json.dumps(resolved)

    async def _resolve_presence_list(
        self,
        presences: list[UserPresence],
    ) -> list[dict[str, Any]]:
        """Resolve a list of presences, filtering to known users only."""
        results = []
        for p in presences:
            resolved = await self._resolve_presence(p)
            if resolved is not None:
                results.append(resolved)
        return results

    async def _resolve_presence(
        self,
        p: UserPresence,
    ) -> dict[str, Any] | None:
        """Resolve a UserPresence to a dict with user info.

        Returns None if the user cannot be resolved to a known Gilbert
        user — unresolvable detections are excluded from tool output.
        """
        if self._resolver is None:
            return None

        from gilbert.interfaces.users import UserManagementProvider

        user_svc = self._resolver.get_capability("users")
        if not isinstance(user_svc, UserManagementProvider):
            return None

        try:
            user = await user_svc.backend.get_user(p.user_id)
        except Exception:
            return None

        if user is None:
            return None

        return {
            "user_id": p.user_id,
            "name": user.get("display_name", p.user_id),
            "email": user.get("email", ""),
            "state": p.state.value,
            "since": p.since,
            "source": p.source,
        }
