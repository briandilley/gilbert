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
    PresenceDetection,
    PresenceObservation,
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

# Default retention for the per-day presence detection history.
_DEFAULT_HISTORY_RETENTION_DAYS = 30

# How often (seconds) to run the retention sweep. Tied to the day
# boundary rather than the poll cadence — sweeping more than once an
# hour buys nothing and just touches storage.
_HISTORY_SWEEP_INTERVAL = 3600.0


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
        self._history_retention_days: int = _DEFAULT_HISTORY_RETENTION_DAYS
        self._timezone: str = "UTC"

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="presence",
            capabilities=frozenset(
                {"presence", "presence_history", "ai_tools", "ws_handlers"}
            ),
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
                scheduler.add_job(
                    name="presence-history-sweep",
                    schedule=Schedule.every(_HISTORY_SWEEP_INTERVAL),
                    callback=self._prune_old_history,
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
        retention = section.get("history_retention_days")
        if retention is not None:
            self._history_retention_days = max(0, int(retention))
        tz = section.get("timezone")
        if tz:
            self._timezone = str(tz)

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
            ConfigParam(
                key="history_retention_days",
                type=ToolParameterType.INTEGER,
                description=(
                    "Days of per-user detection history kept. Older rows are "
                    "pruned hourly. 0 disables retention (rows are kept "
                    "until manually deleted)."
                ),
                default=_DEFAULT_HISTORY_RETENTION_DAYS,
            ),
            ConfigParam(
                key="timezone",
                type=ToolParameterType.STRING,
                description=(
                    "IANA timezone for bucketing detection history by day "
                    "(e.g. 'America/Los_Angeles'). Defaults to UTC."
                ),
                default="UTC",
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

        # 7. Record detection history: every poll that sees a user as
        # present/nearby rolls into a daily aggregate per source. This
        # keeps the time-series cheap (one row per user/day/source) and
        # lets other services answer "when was X around recently" with
        # a single bounded query.
        await self._record_detection_history(currently_here)

        # 8. Capture raw observations for the mapping screen. Backends
        # that haven't been retrofitted return an empty list, so this
        # is a no-op for them.
        try:
            observations = await self._backend.get_observations()
        except Exception:
            logger.warning("Failed to read observations from backend", exc_info=True)
            observations = []
        if observations:
            await self._upsert_observations(observations)

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
    _HISTORY_COLLECTION = "presence_detections"
    _OBSERVATIONS_COLLECTION = "presence_observations"

    @staticmethod
    def _observation_row_id(backend: str, thing_id: str) -> str:
        """Composite key for an observation row. The colon delimiter
        matches the backend-id convention (``"unifi:protect"`` etc.)
        and ensures uniqueness across backends with overlapping
        thing_id namespaces."""
        return f"{backend}:{thing_id}"

    async def _upsert_observations(
        self,
        observations: list[PresenceObservation],
    ) -> None:
        """Merge incoming observations into storage, preserving the
        ``mapped_user_id`` set by the mapping UI and rolling first_seen
        forward only on the very first sighting.

        Backends only need to know "what did I see this poll" — the
        service owns the persistent mapping pivot so admins can edit
        it independently of any single backend's internal state.
        """
        if self._storage is None:
            return
        from datetime import datetime

        now_iso = datetime.now(UTC).isoformat()
        for obs in observations:
            if not obs.backend or not obs.thing_id:
                continue
            row_id = self._observation_row_id(obs.backend, obs.thing_id)
            existing = await self._storage.get(self._OBSERVATIONS_COLLECTION, row_id) or {}
            first_seen = existing.get("first_seen") or obs.first_seen or now_iso
            last_seen = obs.last_seen or now_iso
            await self._storage.put(
                self._OBSERVATIONS_COLLECTION,
                row_id,
                {
                    "backend": obs.backend,
                    "thing_id": obs.thing_id,
                    "label": obs.label or existing.get("label", ""),
                    "kind": obs.kind or existing.get("kind", ""),
                    "first_seen": first_seen,
                    "last_seen": last_seen,
                    "signal_strength": (
                        obs.signal_strength
                        if obs.signal_strength is not None
                        else existing.get("signal_strength")
                    ),
                    # Mapping pivot — only the mapping API writes here,
                    # so polling never clobbers an admin-edited value.
                    "mapped_user_id": existing.get("mapped_user_id", ""),
                },
            )

    async def list_observations(
        self,
        *,
        mapped: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Return every observation we've seen, optionally filtered to
        only mapped (``True``) or only unmapped (``False``) rows.

        Rows are sorted by ``last_seen`` descending so the mapping UI
        surfaces fresh things first.
        """
        if self._storage is None:
            return []
        from gilbert.interfaces.storage import Query

        try:
            rows = await self._storage.query(Query(collection=self._OBSERVATIONS_COLLECTION))
        except Exception:
            logger.warning("Failed to list observations", exc_info=True)
            return []
        if mapped is True:
            rows = [r for r in rows if r.get("mapped_user_id")]
        elif mapped is False:
            rows = [r for r in rows if not r.get("mapped_user_id")]
        rows.sort(key=lambda r: r.get("last_seen", ""), reverse=True)
        return rows

    async def map_thing(
        self,
        backend: str,
        thing_id: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        """Map an observation to a Gilbert user_id (or ``""`` to unmap).

        Returns the updated row, or ``None`` if no such observation
        exists. Also pushes the new mapping down to the live backend
        via ``apply_thing_mappings`` so the next poll uses it without
        a restart.
        """
        if self._storage is None or not backend or not thing_id:
            return None
        row_id = self._observation_row_id(backend, thing_id)
        existing = await self._storage.get(self._OBSERVATIONS_COLLECTION, row_id)
        if not existing:
            return None
        existing["mapped_user_id"] = user_id
        await self._storage.put(self._OBSERVATIONS_COLLECTION, row_id, existing)
        await self._notify_backend_of_mapping_change()
        return existing

    async def relabel_thing(
        self,
        backend: str,
        thing_id: str,
        label: str,
    ) -> dict[str, Any] | None:
        """Admin-edited human-readable label for the mapping screen.

        Doesn't affect identity / mapping — purely cosmetic. Returns
        the updated row or ``None`` if the observation doesn't exist.
        """
        if self._storage is None or not backend or not thing_id:
            return None
        row_id = self._observation_row_id(backend, thing_id)
        existing = await self._storage.get(self._OBSERVATIONS_COLLECTION, row_id)
        if not existing:
            return None
        existing["label"] = label
        await self._storage.put(self._OBSERVATIONS_COLLECTION, row_id, existing)
        return existing

    async def _notify_backend_of_mapping_change(self) -> None:
        """Push the full current mapping set down to the backend.

        Sent as a single dict so the backend can swap state atomically
        rather than receive a stream of patches. Backends that don't
        implement the optional hook (default no-op on the ABC) are
        unaffected.
        """
        if self._backend is None or self._storage is None:
            return
        try:
            rows = await self.list_observations()
            mappings = {
                self._observation_row_id(r["backend"], r["thing_id"]): r.get("mapped_user_id", "")
                for r in rows
            }
            await self._backend.apply_thing_mappings(mappings)
        except Exception:
            logger.warning("Failed to push mapping changes to backend", exc_info=True)

    def _today_str(self) -> str:
        """Calendar date in the configured timezone — used as the bucket
        key for daily detection aggregates."""
        from datetime import datetime

        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo(self._timezone)).strftime("%Y-%m-%d")
        except Exception:
            return datetime.now(UTC).strftime("%Y-%m-%d")

    @staticmethod
    def _detection_row_id(user_id: str, date: str, source: str) -> str:
        """Composite key for a detection row. ``source`` is part of the
        key so a multi-source backend (badge + face + wifi) produces
        independent daily aggregates that the UI can break down by
        signal type."""
        return f"{user_id}|{date}|{source}"

    async def _record_detection_history(
        self,
        currently_here: dict[str, UserPresence],
    ) -> None:
        """Upsert a daily detection row for each user the backend sees
        as present or nearby. ``observation_count`` is incremented in
        place; ``first_seen`` is preserved across the day; ``last_seen``
        is bumped each poll."""
        if self._storage is None or self._history_retention_days < 0:
            return
        if not currently_here:
            return
        try:
            from datetime import datetime

            now_iso = datetime.now(UTC).isoformat()
            today = self._today_str()
            for p in currently_here.values():
                if p.state not in (PresenceState.PRESENT, PresenceState.NEARBY):
                    continue
                source = p.source or "unknown"
                row_id = self._detection_row_id(p.user_id, today, source)
                existing = await self._storage.get(self._HISTORY_COLLECTION, row_id)
                if existing:
                    first_seen = existing.get("first_seen") or now_iso
                    count = int(existing.get("observation_count", 0)) + 1
                else:
                    first_seen = now_iso
                    count = 1
                await self._storage.put(
                    self._HISTORY_COLLECTION,
                    row_id,
                    {
                        "user_id": p.user_id,
                        "date": today,
                        "source": source,
                        "first_seen": first_seen,
                        "last_seen": now_iso,
                        "observation_count": count,
                    },
                )
        except Exception:
            logger.warning("Failed to record detection history", exc_info=True)

    async def _prune_old_history(self) -> None:
        """Drop detection rows whose date is older than retention.

        A retention of 0 disables pruning. We compute the cutoff date
        once per sweep in the configured timezone and delete in a
        single query — much cheaper than reading rows then deleting,
        and the storage layer enforces per-collection isolation."""
        if self._storage is None or self._history_retention_days <= 0:
            return
        try:
            from datetime import datetime, timedelta

            from gilbert.interfaces.storage import Filter, FilterOp, Query

            try:
                from zoneinfo import ZoneInfo

                today = datetime.now(ZoneInfo(self._timezone)).date()
            except Exception:
                today = datetime.now(UTC).date()
            cutoff = (today - timedelta(days=self._history_retention_days)).isoformat()

            # Storage backends that expose ``query`` reliably across
            # filter ops let us scope the prune to the rows that
            # actually need to go. Fall back to a full scan if the
            # backend doesn't support LT (older SQLite path).
            old_rows = await self._storage.query(
                Query(
                    collection=self._HISTORY_COLLECTION,
                    filters=[Filter(field="date", op=FilterOp.LT, value=cutoff)],
                )
            )
            for row in old_rows:
                row_id = row.get("_id") or self._detection_row_id(
                    row.get("user_id", ""),
                    row.get("date", ""),
                    row.get("source", ""),
                )
                if row_id:
                    await self._storage.delete(self._HISTORY_COLLECTION, row_id)
            if old_rows:
                logger.info(
                    "Pruned %d presence detection rows older than %s",
                    len(old_rows),
                    cutoff,
                )
        except Exception:
            logger.warning("Failed to prune presence history", exc_info=True)

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

    async def get_detection_history(
        self,
        user_id: str,
        since: str = "",
        until: str = "",
    ) -> list[PresenceDetection]:
        """Return detection history rows for the user within an optional
        inclusive date window. Empty bounds = unbounded. Rows sorted
        ascending by date.

        Implements ``PresenceHistoryProvider``. Other services should
        capability-resolve this rather than instantiating ``UserPresence``
        themselves — the rolled-up rows give a stable across-source view.
        """
        if self._storage is None or not user_id:
            return []
        from gilbert.interfaces.storage import Filter, FilterOp, Query, SortField

        filters: list[Filter] = [Filter(field="user_id", op=FilterOp.EQ, value=user_id)]
        if since:
            filters.append(Filter(field="date", op=FilterOp.GTE, value=since))
        if until:
            filters.append(Filter(field="date", op=FilterOp.LTE, value=until))
        try:
            rows = await self._storage.query(
                Query(
                    collection=self._HISTORY_COLLECTION,
                    filters=filters,
                    sort=[SortField(field="date")],
                )
            )
        except Exception:
            logger.warning("Failed to query detection history", exc_info=True)
            return []
        return [
            PresenceDetection(
                user_id=str(r.get("user_id", "")),
                date=str(r.get("date", "")),
                source=str(r.get("source", "")),
                first_seen=str(r.get("first_seen", "")),
                last_seen=str(r.get("last_seen", "")),
                observation_count=int(r.get("observation_count", 0) or 0),
            )
            for r in rows
        ]

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
            ToolDefinition(
                name="presence_history",
                slash_group="presence",
                slash_command="history",
                slash_help=(
                    "Recent detection history for a person: "
                    "/presence history <name_or_user_id> [days]"
                ),
                description=(
                    "Show the last N days of presence detections for a "
                    "person. Accepts a free-form name (display name, first "
                    "name, or email local part) or a user_id; falls back "
                    "to user_id if the name is ambiguous. Default 7 days."
                ),
                parameters=[
                    ToolParameter(
                        name="name_or_user_id",
                        type=ToolParameterType.STRING,
                        description="Free-form name or exact user_id to look up.",
                    ),
                    ToolParameter(
                        name="days",
                        type=ToolParameterType.INTEGER,
                        description="How many days back to include (default 7, max retention window).",
                        required=False,
                    ),
                ],
                required_role="user",
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
            case "presence_history":
                return await self._tool_presence_history(arguments)
            case _:
                raise KeyError(f"Unknown tool: {name}")

    async def _tool_presence_history(self, arguments: dict[str, Any]) -> str:
        """Return rolled-up detection history for a person.

        ``name_or_user_id`` is resolved through
        ``UserManagementProvider.resolve_user_id_by_name`` so callers
        can pass first names, display names, or email local parts as
        well as exact user_ids. If the string is itself a known
        user_id, we use it directly (no resolver round-trip).
        """
        raw = str(arguments.get("name_or_user_id", "")).strip()
        if not raw:
            return json.dumps({"error": "name_or_user_id is required"})
        # ``or 7`` would turn an explicit 0 back into 7, masking the
        # validation — only treat None / missing as "default to 7."
        raw_days = arguments.get("days", 7)
        try:
            days = int(raw_days) if raw_days is not None else 7
        except (TypeError, ValueError):
            return json.dumps({"error": "days must be an integer >= 1"})
        if days <= 0:
            return json.dumps({"error": "days must be >= 1"})

        user_id = await self._resolve_name_or_id(raw)
        if not user_id:
            return json.dumps(
                {"error": f"Could not resolve '{raw}' to a known user."},
            )

        from datetime import datetime, timedelta

        try:
            from zoneinfo import ZoneInfo

            today = datetime.now(ZoneInfo(self._timezone)).date()
        except Exception:
            today = datetime.now(UTC).date()
        since = (today - timedelta(days=days - 1)).isoformat()
        until = today.isoformat()

        history = await self.get_detection_history(user_id, since=since, until=until)

        # Roll up across sources per day so the tool output is one
        # entry per calendar day (the per-source detail is preserved
        # under ``by_source`` if the model needs it).
        by_date: dict[str, dict[str, Any]] = {}
        for det in history:
            bucket = by_date.setdefault(
                det.date,
                {
                    "date": det.date,
                    "first_seen": det.first_seen,
                    "last_seen": det.last_seen,
                    "observation_count": 0,
                    "by_source": {},
                },
            )
            if det.first_seen and (not bucket["first_seen"] or det.first_seen < bucket["first_seen"]):
                bucket["first_seen"] = det.first_seen
            if det.last_seen and det.last_seen > bucket["last_seen"]:
                bucket["last_seen"] = det.last_seen
            bucket["observation_count"] += det.observation_count
            bucket["by_source"][det.source] = {
                "first_seen": det.first_seen,
                "last_seen": det.last_seen,
                "observation_count": det.observation_count,
            }

        days_payload = [by_date[d] for d in sorted(by_date)]
        return json.dumps(
            {
                "user_id": user_id,
                "since": since,
                "until": until,
                "days": days_payload,
            }
        )

    async def _resolve_name_or_id(self, raw: str) -> str:
        """Try the input as-is first (might be a user_id), then fall
        back to UserService.resolve_user_id_by_name."""
        if self._resolver is None:
            return ""
        from gilbert.interfaces.users import UserManagementProvider

        user_svc = self._resolver.get_capability("users")
        if not isinstance(user_svc, UserManagementProvider):
            return ""
        # Direct user_id match — cheap.
        try:
            row = await user_svc.backend.get_user(raw)
            if row is not None:
                return raw
        except Exception:
            pass
        # Free-form name → resolver. Accept any confidence the user
        # service is willing to produce — this is a read-only tool and
        # the model will see the resolved user_id in the response so
        # the human can sanity-check the mapping if it looks off.
        try:
            match = await user_svc.resolve_user_id_by_name(raw)
        except Exception:
            match = None
        return match.user_id if match else ""

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

    # --- WsHandlerProvider protocol ---

    def get_ws_handlers(self) -> dict[str, Any]:
        return {
            "presence.things.list": self._ws_things_list,
            "presence.things.map": self._ws_things_map,
            "presence.things.unmap": self._ws_things_unmap,
            "presence.things.relabel": self._ws_things_relabel,
            "presence.poll.now": self._ws_poll_now,
        }

    def _require_admin(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        """Return a 403 error frame if conn isn't an admin, else None.

        Resolves the admin level through the access-control capability
        so a custom RBAC backend can rename / re-rank the role without
        touching every WS handler — the same gating pattern the config
        service uses.
        """
        if self._resolver is None:
            return None
        acl = self._resolver.get_capability("access_control")
        from gilbert.interfaces.auth import AccessControlProvider

        if not isinstance(acl, AccessControlProvider):
            return None
        required_level = acl.get_role_level("admin")
        user_level = getattr(conn, "user_level", 999)
        if user_level > required_level:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": "Admin role required to manage presence mappings",
                "code": 403,
            }
        return None

    async def _ws_things_list(
        self,
        conn: Any,
        frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        """List observations, optionally filtered to mapped / unmapped."""
        err = self._require_admin(conn, frame)
        if err:
            return err
        mapped_arg = frame.get("mapped")
        mapped_filter: bool | None
        if mapped_arg is True or mapped_arg is False:
            mapped_filter = mapped_arg
        else:
            mapped_filter = None
        rows = await self.list_observations(mapped=mapped_filter)
        return {
            "type": "presence.things.list.result",
            "ref": frame.get("id"),
            "things": rows,
        }

    async def _ws_things_map(
        self,
        conn: Any,
        frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Map an observation to a user_id."""
        err = self._require_admin(conn, frame)
        if err:
            return err
        backend = str(frame.get("backend", ""))
        thing_id = str(frame.get("thing_id", ""))
        user_id = str(frame.get("user_id", ""))
        if not backend or not thing_id or not user_id:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": "backend, thing_id, and user_id are required",
                "code": 400,
            }
        row = await self.map_thing(backend, thing_id, user_id)
        if row is None:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": f"Unknown thing {backend}:{thing_id}",
                "code": 404,
            }
        return {
            "type": "presence.things.map.result",
            "ref": frame.get("id"),
            "thing": row,
        }

    async def _ws_things_unmap(
        self,
        conn: Any,
        frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Clear an observation's user_id mapping."""
        err = self._require_admin(conn, frame)
        if err:
            return err
        backend = str(frame.get("backend", ""))
        thing_id = str(frame.get("thing_id", ""))
        if not backend or not thing_id:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": "backend and thing_id are required",
                "code": 400,
            }
        row = await self.map_thing(backend, thing_id, "")
        if row is None:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": f"Unknown thing {backend}:{thing_id}",
                "code": 404,
            }
        return {
            "type": "presence.things.unmap.result",
            "ref": frame.get("id"),
            "thing": row,
        }

    async def _ws_things_relabel(
        self,
        conn: Any,
        frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Set a human-readable label on an observation (cosmetic)."""
        err = self._require_admin(conn, frame)
        if err:
            return err
        backend = str(frame.get("backend", ""))
        thing_id = str(frame.get("thing_id", ""))
        label = str(frame.get("label", ""))
        if not backend or not thing_id:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": "backend and thing_id are required",
                "code": 400,
            }
        row = await self.relabel_thing(backend, thing_id, label)
        if row is None:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": f"Unknown thing {backend}:{thing_id}",
                "code": 404,
            }
        return {
            "type": "presence.things.relabel.result",
            "ref": frame.get("id"),
            "thing": row,
        }

    async def _ws_poll_now(
        self,
        conn: Any,
        frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Force an immediate poll instead of waiting for the next tick.

        Useful from the mapping screen — the admin doesn't want to
        wait 30 s for the next scheduled poll to surface a brand-new
        observation. Returns the count of things currently known so
        the UI can refresh from a known consistent state.
        """
        err = self._require_admin(conn, frame)
        if err:
            return err
        try:
            await self._check_for_changes()
        except Exception as exc:
            logger.exception("Forced presence poll failed")
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": f"Poll failed: {exc}",
                "code": 500,
            }
        things = await self.list_observations()
        return {
            "type": "presence.poll.now.result",
            "ref": frame.get("id"),
            "thing_count": len(things),
        }
