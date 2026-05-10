"""Feed briefing service — daily fan-out + event publication.

Thin scheduler + event publisher. NO AI calls of its own — calls
``FeedsProvider.build_briefing`` (defined in ``interfaces/feeds.py``)
which owns the prompt, the AI call, and the persistence. The fan-out
strategy is **presence-first, daily-fire-as-fallback** (per spec
§13.1) so a 7am presence-driven greeting doesn't race a daily-fire
job and consume the items the greeting would have spoken.

Design notes:

- The fallback fires at ``briefing_hour + presence_grace_minutes``
  (default 8:30 AM if briefing_hour=7). It iterates every user with
  at least one accessible briefing-eligible feed, skips users whose
  ``feed_briefing_state.last_briefed_on`` already matches today, and
  publishes ``feed.briefing.ready`` for each remaining user.
- Role-shared users default to ``briefing_opt_in=False`` so a
  10-person team sharing one feed doesn't generate 10 briefings.
- The event payload deliberately does NOT contain ``spoken_text`` —
  consumers RPC-fetch the full text via ``feeds.briefing.get`` so
  potentially-sensitive briefing content stays out of the WS event
  log.
- v1 is single-global-timezone. v1.x adds per-user
  ``briefing_hour`` / ``timezone`` overrides plus a 15-minute
  briefing-tick that decides which users are due.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime
from typing import Any

from gilbert.core.context import get_current_user
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.events import Event, EventBus, EventBusProvider
from gilbert.interfaces.feeds import Feed, FeedsProvider
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.speaker import SpeakerProvider
from gilbert.interfaces.storage import StorageProvider
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)

_BRIEFING_STATE_COLLECTION = "feed_briefing_state"


class FeedBriefingService(Service):
    """Daily fan-out + event publisher for feed briefings.

    Capabilities: ``feed_briefing``.
    Requires: ``feeds`` (the actual brief-builder) + ``scheduler``.
    Optional: ``event_bus``, ``configuration``, ``speaker_control``,
    ``access_control``.
    """

    def __init__(self) -> None:
        self._enabled: bool = False
        self._resolver: ServiceResolver | None = None
        self._event_bus: EventBus | None = None
        self._storage: Any = None
        self._scheduler: Any = None

        # Config — all toggles exposed as ConfigParams so the
        # Settings UI is the canonical way to enable.
        self._briefing_hour: int = 7
        self._briefing_minute: int = 0
        self._timezone: str = "UTC"
        self._briefing_top_n: int = 5
        self._briefing_since_hours: int = 24
        self._presence_grace_minutes: int = 90
        self._system_briefing_enabled: bool = False
        self._system_briefing_user_id: str = ""
        self._announce_speakers: list[str] = []

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="feed_briefing",
            capabilities=frozenset({"feed_briefing"}),
            requires=frozenset({"feeds", "scheduler"}),
            optional=frozenset(
                {
                    "event_bus",
                    "configuration",
                    "speaker_control",
                    "access_control",
                    "entity_storage",
                }
            ),
            events=frozenset({"feed.briefing.ready"}),
            ai_calls=frozenset(),
            toggleable=True,
            toggle_description="Morning news briefing fan-out and event publication",
        )

    @property
    def config_namespace(self) -> str:
        return "feed_briefing"

    @property
    def config_category(self) -> str:
        return "News & Information"

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="enabled",
                type=ToolParameterType.BOOLEAN,
                description="Master switch — opt-in feature.",
                default=False,
            ),
            ConfigParam(
                key="briefing_hour",
                type=ToolParameterType.INTEGER,
                description=(
                    "Hour-of-day to auto-generate the briefing (single global "
                    "timezone in v1)."
                ),
                default=7,
            ),
            ConfigParam(
                key="briefing_minute",
                type=ToolParameterType.INTEGER,
                description="Minute-of-hour for the briefing fire.",
                default=0,
            ),
            ConfigParam(
                key="timezone",
                type=ToolParameterType.STRING,
                description="Server-side IANA timezone for the daily fire.",
                default="UTC",
            ),
            ConfigParam(
                key="briefing_top_n",
                type=ToolParameterType.INTEGER,
                description="Items per briefing.",
                default=5,
            ),
            ConfigParam(
                key="briefing_since_hours",
                type=ToolParameterType.INTEGER,
                description="Look-back window for briefing items (hours).",
                default=24,
            ),
            ConfigParam(
                key="presence_grace_minutes",
                type=ToolParameterType.INTEGER,
                description=(
                    "If presence-driven greeting hasn't fired by "
                    "briefing_hour + this grace, the daily fan-out fires "
                    "for that user as the fallback."
                ),
                default=90,
            ),
            ConfigParam(
                key="system_briefing_enabled",
                type=ToolParameterType.BOOLEAN,
                description=(
                    "Enable a single shared shop briefing voiced over "
                    "speakers regardless of who's home."
                ),
                default=False,
            ),
            ConfigParam(
                key="system_briefing_user_id",
                type=ToolParameterType.STRING,
                description=(
                    "When system_briefing_enabled, the user_id whose "
                    "accessible feeds drive the shop briefing."
                ),
                default="",
            ),
            ConfigParam(
                key="announce_speakers",
                type=ToolParameterType.ARRAY,
                description=(
                    "Speaker names for the system briefing announcement "
                    "(used only when system_briefing_enabled)."
                ),
                default=[],
                choices_from="speakers",
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._enabled = bool(config.get("enabled", self._enabled))
        self._briefing_hour = int(config.get("briefing_hour", self._briefing_hour) or 0)
        self._briefing_minute = int(
            config.get("briefing_minute", self._briefing_minute) or 0
        )
        self._timezone = str(config.get("timezone", self._timezone) or "UTC")
        self._briefing_top_n = max(
            1, int(config.get("briefing_top_n", self._briefing_top_n) or 5)
        )
        self._briefing_since_hours = max(
            1, int(config.get("briefing_since_hours", self._briefing_since_hours) or 24)
        )
        self._presence_grace_minutes = max(
            0,
            int(
                config.get("presence_grace_minutes", self._presence_grace_minutes)
                or 0
            ),
        )
        self._system_briefing_enabled = bool(
            config.get("system_briefing_enabled", self._system_briefing_enabled)
        )
        self._system_briefing_user_id = str(
            config.get("system_briefing_user_id", self._system_briefing_user_id)
            or ""
        )
        speakers = config.get("announce_speakers", self._announce_speakers)
        if isinstance(speakers, list):
            self._announce_speakers = [str(s) for s in speakers]

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver

        config_svc = resolver.get_capability("configuration")
        section: dict[str, Any] = {}
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                section = config_svc.get_section_safe(self.config_namespace)
        await self.on_config_changed(section)

        if not self._enabled:
            logger.info("Feed briefing service disabled via configuration")
            return

        # Verify ``feeds`` capability is actually a FeedsProvider.
        feeds_svc = resolver.require_capability("feeds")
        if not isinstance(feeds_svc, FeedsProvider):
            raise TypeError(
                "feeds capability does not satisfy FeedsProvider — "
                "feed_briefing cannot start"
            )

        event_bus_svc = resolver.get_capability("event_bus")
        if isinstance(event_bus_svc, EventBusProvider):
            self._event_bus = event_bus_svc.bus

        storage_svc = resolver.get_capability("entity_storage")
        if isinstance(storage_svc, StorageProvider):
            self._storage = storage_svc.backend

        from gilbert.interfaces.scheduler import Schedule, SchedulerProvider

        scheduler_svc = resolver.require_capability("scheduler")
        if not isinstance(scheduler_svc, SchedulerProvider):
            raise TypeError(
                "scheduler capability does not provide SchedulerProvider"
            )
        self._scheduler = scheduler_svc

        # Fallback fires at briefing_hour + presence_grace.
        fire_minute = self._briefing_minute + self._presence_grace_minutes
        fire_hour = (self._briefing_hour + fire_minute // 60) % 24
        fire_minute = fire_minute % 60
        self._scheduler.add_job(
            name="feed-briefing-fallback",
            schedule=Schedule.daily_at(fire_hour, fire_minute),
            callback=self._fallback_tick,
            system=True,
        )
        logger.info(
            "Feed briefing service started (fallback fires daily at %02d:%02d)",
            fire_hour,
            fire_minute,
        )

    async def stop(self) -> None:
        if self._scheduler is not None:
            with contextlib.suppress(Exception):
                self._scheduler.remove_job("feed-briefing-fallback")

    async def _fallback_tick(self) -> None:
        """Fire briefings for users who didn't get the presence-driven path.

        Walks every accessible feed's ``owner_user_id`` /
        ``shared_with_users`` / role-resolved members, dedups on
        ``last_briefed_on`` (date-only), respects per-user
        ``briefing_opt_in`` (role-shared-only users default to off),
        and publishes ``feed.briefing.ready`` after building the
        briefing text via ``FeedsProvider.build_briefing``.
        """
        if self._resolver is None:
            return
        feeds_svc = self._resolver.get_capability("feeds")
        if not isinstance(feeds_svc, FeedsProvider):
            logger.warning("feed_briefing tick: feeds capability missing")
            return

        # System (shop) briefing — single shared announcement.
        if self._system_briefing_enabled and self._system_briefing_user_id:
            await self._fire_system_briefing(feeds_svc)

        users = await self._enumerate_users(feeds_svc)
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        for user_id, owns_feed in users.items():
            opt_in = await self._is_opted_in(user_id, owns_feed)
            if not opt_in:
                continue
            state = await self._get_state(user_id)
            if state.get("last_briefed_on") == today:
                continue
            user_ctx = UserContext(
                user_id=user_id,
                email="",
                display_name=user_id,
                roles=frozenset({"user"}),
            )
            try:
                result = await feeds_svc.build_briefing(
                    user_ctx,
                    top_n=self._briefing_top_n,
                    mark_briefed=True,
                )
            except Exception:
                logger.exception(
                    "feed_briefing tick: build_briefing failed for %s", user_id
                )
                continue
            await self._publish_event(
                "feed.briefing.ready",
                {
                    "user_id": user_id,
                    "briefing_id": result.briefing_id,
                    "item_count": len(result.item_ids),
                    "since": result.since.isoformat(),
                },
            )

    async def _fire_system_briefing(self, feeds_svc: FeedsProvider) -> None:
        target_user_id = self._system_briefing_user_id
        if not target_user_id:
            return
        user_ctx = UserContext(
            user_id=target_user_id,
            email="",
            display_name=target_user_id,
            roles=frozenset({"user"}),
        )
        try:
            result = await feeds_svc.build_briefing(
                user_ctx,
                top_n=self._briefing_top_n,
                mark_briefed=True,
            )
        except Exception:
            logger.exception(
                "system briefing: build_briefing failed for %s", target_user_id
            )
            return
        await self._publish_event(
            "feed.briefing.ready",
            {
                "user_id": target_user_id,
                "briefing_id": result.briefing_id,
                "item_count": len(result.item_ids),
                "since": result.since.isoformat(),
                "system": True,
            },
        )
        if not result.spoken or self._resolver is None:
            return
        speaker_svc = self._resolver.get_capability("speaker_control")
        if not isinstance(speaker_svc, SpeakerProvider):
            return
        try:
            await speaker_svc.announce(
                result.spoken,
                speaker_names=self._announce_speakers or None,
                context="Daily news briefing for the shop",
            )
        except Exception:
            logger.warning("system briefing: announce failed", exc_info=True)

    async def _enumerate_users(
        self, feeds_svc: FeedsProvider
    ) -> dict[str, bool]:
        """Walk every feed and union owner / shared / role-resolved users.

        Returns ``{user_id: owns_at_least_one_feed}``. Owners default
        to ``briefing_opt_in=True``; role-shared-only users default
        to ``False``.
        """
        # Use SYSTEM context to fetch all feeds.
        all_feeds = await feeds_svc.list_accessible_feeds(UserContext.SYSTEM)
        users: dict[str, bool] = {}
        for feed in all_feeds:
            if feed.owner_user_id:
                users[feed.owner_user_id] = True
            for u in feed.shared_with_users:
                users.setdefault(u, False)
            # role-resolved members
            for u in await self._role_members(feed):
                users.setdefault(u, False)
        return users

    async def _role_members(self, feed: Feed) -> list[str]:
        if not feed.shared_with_roles or self._resolver is None:
            return []
        # Best-effort: resolve via UserService if available. A real
        # role→user mapping requires the user service; absent it we
        # don't enumerate role-shared users (keeping false negatives
        # over false positives).
        users_svc = self._resolver.get_capability("users")
        if users_svc is None:
            return []
        if not hasattr(users_svc, "list_users"):
            return []
        try:
            users = await users_svc.list_users()
        except Exception:
            return []
        target_roles = set(feed.shared_with_roles)
        return [
            getattr(u, "user_id", "")
            for u in (users or [])
            if getattr(u, "user_id", "")
            and target_roles & set(getattr(u, "roles", []) or [])
        ]

    async def _get_state(self, user_id: str) -> dict[str, Any]:
        if self._storage is None:
            return {}
        row = await self._storage.get(_BRIEFING_STATE_COLLECTION, user_id)
        return dict(row) if row else {}

    async def _is_opted_in(self, user_id: str, owns_feed: bool) -> bool:
        state = await self._get_state(user_id)
        if "briefing_opt_in" in state:
            return bool(state["briefing_opt_in"])
        # Default: owners opt in, role-shared-only users opt out.
        return bool(owns_feed)

    async def _publish_event(
        self, event_type: str, data: dict[str, Any]
    ) -> None:
        if self._event_bus is None:
            return
        await self._event_bus.publish(
            Event(event_type=event_type, data=data, source="feed_briefing")
        )

    # ── Public API consumed by tests / WS RPCs ───────────────────────

    async def run_now(self, *, force: bool = False) -> int:
        """Manually trigger the fallback. Returns # of briefings fired.

        Used by ``feeds.briefing.run`` WS RPC and tests. ``force=True``
        bypasses the today-already-briefed cache.
        """
        if self._resolver is None:
            return 0
        feeds_svc = self._resolver.get_capability("feeds")
        if not isinstance(feeds_svc, FeedsProvider):
            return 0
        users = await self._enumerate_users(feeds_svc)
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        fired = 0
        for user_id, owns_feed in users.items():
            if not force and not await self._is_opted_in(user_id, owns_feed):
                continue
            if not force:
                state = await self._get_state(user_id)
                if state.get("last_briefed_on") == today:
                    continue
            user_ctx = UserContext(
                user_id=user_id,
                email="",
                display_name=user_id,
                roles=frozenset({"user"}),
            )
            try:
                result = await feeds_svc.build_briefing(
                    user_ctx, top_n=self._briefing_top_n, mark_briefed=True
                )
            except Exception:
                logger.exception(
                    "feed_briefing run_now: build_briefing failed for %s",
                    user_id,
                )
                continue
            await self._publish_event(
                "feed.briefing.ready",
                {
                    "user_id": user_id,
                    "briefing_id": result.briefing_id,
                    "item_count": len(result.item_ids),
                    "since": result.since.isoformat(),
                },
            )
            fired += 1
        return fired

    # NOTE: ``get_current_user`` is imported but used only by tests
    # exercising tool-style invocations against this service from
    # within the WS RPC dispatch. The fallback tick runs as a system
    # job so the ContextVar isn't relevant for the daily fire path.
    _ = get_current_user

