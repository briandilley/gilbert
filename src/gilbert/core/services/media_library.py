"""Media library aggregator — Plex/Jellyfin/etc., per-user mapping, AI tools.

Holds ``dict[str, MediaLibraryBackend]`` (precedent: ``AuthService`` /
``KnowledgeService``), fans library queries out across configured
backends, merges results, and dispatches playback to whichever server
owns the target client.

Per-user identity mapping lives in ``media_library_user_map``: a Gilbert
user maps to exactly one ``(backend, backend_user_id)`` pair per backend
in v1. Tools read the calling user from the injected ``_user_id``
argument; fallback to ``get_current_user()`` is FORBIDDEN per spec §11
and Appendix C.

Polling jobs (``poll_now_playing``, ``poll_recently_added``) fire from
the ``SchedulerProvider`` capability with explicit
``set_current_user(UserContext.SYSTEM)`` at job entry, matching the
calendar / knowledge precedent. The recently-added poll's first cycle
is a baseline run that emits no events (failure-mode mitigation —
without it, every restart would emit one event per item in the entire
recently-added feed).

Per-client locks (``dict[(backend_name, client_id), asyncio.Lock]``)
serialize ``play_item`` for the same TV but let unrelated TVs run in
parallel — see ``memory-multi-user-isolation.md`` for the per-target
lock pattern. A single global lock would serialize every play across
every TV across every user, which is the explicit Appendix C
anti-pattern.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import random
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any, TypeVar

from gilbert.core.context import set_current_user
from gilbert.core.services._backend_actions import (
    all_backend_actions,
    invoke_backend_action,
)
from gilbert.interfaces.ai import (
    AISamplingProvider,
    Message,
    MessageRole,
)
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
    ConfigurationReader,
)
from gilbert.interfaces.events import Event, EventBus, EventBusProvider
from gilbert.interfaces.media_library import (
    BackendHealth,
    ContinueWatchingEntry,
    MediaClient,
    MediaClientAmbiguousError,
    MediaClientNotFoundError,
    MediaItem,
    MediaKind,
    MediaLibraryBackend,
    MediaLibraryError,
    MediaLibraryUnavailableError,
    MediaPlayCommand,
    MediaSearchFilters,
    MediaSession,
    RecentlyAddedEntry,
)
from gilbert.interfaces.scheduler import Schedule, SchedulerProvider
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import (
    Filter,
    FilterOp,
    IndexDefinition,
    Query,
    StorageBackend,
    StorageProvider,
)
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)
from gilbert.interfaces.ui import ToolOutput, UIBlock, UIElement, UIOption

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

T = TypeVar("T")

_USER_MAP_COLLECTION = "media_library_user_map"
_CLIENTS_CACHE_COLLECTION = "media_library_clients_cache"

# Hard cap on search results regardless of caller request — prevents
# AI-driven "limit=10000" runaway memory.
_SEARCH_LIMIT_CAP = 50

# Per-client idempotency dedup window.
_IDEMPOTENCY_TTL_SECONDS = 5.0
_IDEMPOTENCY_HISTORY_SIZE = 5

# Visual disambiguation — top-N high-confidence matches that produce
# UIBlock poster cards instead of silent first-match.
_DISAMBIGUATION_MAX_CARDS = 5

# Cached-clients retention.
_CLIENTS_CACHE_RETENTION_SECONDS = 30 * 24 * 3600.0


# ── Default AI prompts ─────────────────────────────────────────────


_DEFAULT_RECOMMEND_NEXT_PROMPT = """\
You are helping someone pick what to watch right now. They have a
library of candidate items below — pick three based on what they
asked, weighing recency, unwatched status, and genre fit.

You will receive:
  - <user_intent>: the user's stated mood / genre / runtime, if any.
  - <candidates>: JSON list of items (title, year, genres, short
    summary, watch state, and runtime).
  - <recent_history>: a short list of the user's last few watched
    items, when known.

For each pick: one short, warm sentence on why this one — not "this
is a great choice." Be specific. Avoid recommending two items from
the same franchise.

If <recent_history> is empty (new user), recommend based on
<candidates> alone — lean on recency and genre fit.

Reply with a JSON array of three objects, each with `id` (echo back
unchanged), `reason` (one sentence), and `confidence` (0.0–1.0). No
extra commentary, no markdown — pure JSON.
"""


_DEFAULT_ITEM_DISAMBIGUATION_PROMPT = """\
The user asked to play something, but several library items matched
the title. Pick the one most likely to be the intended item based on
(a) recency (year), (b) the user's recent viewing history, (c) the
user's stated intent.

Reply with the JSON object `{"item_id": "<chosen id>", "backend":
"<plex|jellyfin>"}` — nothing else.
"""


_DEFAULT_CLIENT_DISAMBIGUATION_PROMPT = """\
You are helping pick a single playback target. The user named a
device, but several clients matched the name. Choose the one most
likely to be the right target based on (a) device type, (b) which one
the user used most recently, and (c) the user's stated intent.

Reply with the JSON object `{"client_id": "<chosen id>"}` — nothing
else.
"""


# Per-op default timeouts (seconds) — overridable via
# media_library.backend_timeout_seconds.<op> ConfigParam.
_DEFAULT_BACKEND_TIMEOUTS: dict[str, float] = {
    "search": 8.0,
    "recently_added": 8.0,
    "continue_watching": 5.0,
    "now_playing": 5.0,
    "list_clients": 3.0,
    "play": 10.0,
}


# ── Helpers ─────────────────────────────────────────────────────────


def _media_item_to_dict(item: MediaItem) -> dict[str, Any]:
    """Serialize a ``MediaItem`` to a JSON-friendly dict."""
    return {
        "id": item.id,
        "backend_name": item.backend_name,
        "server_id": item.server_id,
        "title": item.title,
        "kind": item.kind.value,
        "year": item.year,
        "duration_seconds": item.duration_seconds,
        "summary": item.summary,
        "rating": item.rating,
        "content_rating": item.content_rating,
        "genres": list(item.genres),
        "poster_url": item.poster_url,
        "parent_id": item.parent_id,
        "parent_title": item.parent_title,
        "grandparent_id": item.grandparent_id,
        "grandparent_title": item.grandparent_title,
        "season_number": item.season_number,
        "episode_number": item.episode_number,
        "library_section": item.library_section,
        "added_at": item.added_at,
        "last_viewed_at": item.last_viewed_at,
        "view_count": item.view_count,
        "view_offset_seconds": item.view_offset_seconds,
        "is_watched": item.is_watched,
    }


def _media_client_to_dict(client: MediaClient) -> dict[str, Any]:
    return {
        "client_id": client.client_id,
        "backend_name": client.backend_name,
        "server_id": client.server_id,
        "name": client.name,
        "device": client.device,
        "platform": client.platform,
        "is_online": client.is_online,
        "supports_remote_control": client.supports_remote_control,
        "supports_seek": client.supports_seek,
        "last_seen_at": client.last_seen_at,
    }


def _media_session_to_dict(session: MediaSession) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "backend_name": session.backend_name,
        "client": _media_client_to_dict(session.client),
        "item": _media_item_to_dict(session.item),
        "state": session.state.value,
        "position_seconds": session.position_seconds,
        "duration_seconds": session.duration_seconds,
        "backend_user_name": session.backend_user_name,
        "started_at": session.started_at,
        "is_transcoding": session.is_transcoding,
        "quality_label": session.quality_label,
    }


def _format_offset(seconds: float) -> str:
    """Format a duration in seconds as ``H:MM:SS`` or ``M:SS``."""
    if seconds <= 0:
        return "0:00"
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _button_label_for_item(item: MediaItem) -> str:
    """State-aware Play button label per spec §7.2 button matrix."""
    if item.view_offset_seconds > 0:
        return f"Resume ({_format_offset(item.view_offset_seconds)})"
    if item.is_watched:
        return "Watch again"
    return "Play"


_POSITION_RE = re.compile(
    r"""
    ^\s*
    (?:
        (?P<h>\d+)\s*(?:h|hr|hrs|hour|hours)\s*
        (?:(?P<hm>\d+)\s*(?:m|min|mins|minute|minutes)\s*)?
        $
        |
        (?P<m>\d+)\s*(?:m|min|mins|minute|minutes)\s*$
        |
        (?P<bare_h>\d+):(?P<bare_m>\d+):(?P<bare_s>\d+)\s*$
        |
        (?P<short_m>\d+):(?P<short_s>\d+)\s*$
        |
        (?P<sec>\d+(?:\.\d+)?)\s*(?:s|sec|secs|second|seconds)?\s*$
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)


def parse_position(text: str) -> float:
    """Lenient parser for seek positions per spec §7.2.

    Accepts:
    - ``"5m"`` / ``"5min"`` / ``"5 minutes"`` → 300
    - ``"1h22m"`` / ``"1hr22min"`` → 4920
    - ``"1:22:00"`` → 4920 (H:MM:SS)
    - ``"1:22"`` → 82 (M:SS — minutes:seconds, the more common
      shorthand for media; explicit hours use H:MM:SS)
    - ``"3700"`` / ``"3700s"`` / ``"3700 seconds"`` → 3700

    Negative offsets are NOT supported — seek is absolute.
    """
    if not text:
        raise ValueError("empty position")
    cleaned = text.strip()
    if cleaned.startswith("-"):
        raise ValueError("negative seek positions are not supported")
    match = _POSITION_RE.match(cleaned)
    if not match:
        raise ValueError(f"could not parse position: {text!r}")
    if match.group("h") is not None:
        h = int(match.group("h"))
        m = int(match.group("hm") or 0)
        return float(h * 3600 + m * 60)
    if match.group("m") is not None:
        return float(int(match.group("m")) * 60)
    if match.group("bare_h") is not None:
        h = int(match.group("bare_h"))
        m = int(match.group("bare_m"))
        s = int(match.group("bare_s"))
        return float(h * 3600 + m * 60 + s)
    if match.group("short_m") is not None:
        m = int(match.group("short_m"))
        s = int(match.group("short_s"))
        return float(m * 60 + s)
    if match.group("sec") is not None:
        return float(match.group("sec"))
    raise ValueError(f"could not parse position: {text!r}")


# ── Service ─────────────────────────────────────────────────────────


class MediaLibraryService(Service):
    """Multi-backend media library aggregator (Plex / Jellyfin / …).

    Capabilities: ``media_library``, ``ai_tools``.
    """

    config_namespace = "media_library"
    config_category = "Media"
    slash_namespace = "media"

    def __init__(self) -> None:
        self._enabled: bool = False
        self._backends: dict[str, MediaLibraryBackend] = {}
        self._resolver: ServiceResolver | None = None
        self._storage: StorageBackend | None = None
        self._event_bus: EventBus | None = None

        # Active prompt strings — initialised to defaults so a tool call
        # before the first config-change doesn't AttributeError. Mirror
        # the ``str(...) or _DEFAULT`` falsy-fallback in
        # on_config_changed so an empty-string override resolves to
        # the default.
        self._recommend_next_prompt: str = _DEFAULT_RECOMMEND_NEXT_PROMPT
        self._item_disambiguation_prompt: str = _DEFAULT_ITEM_DISAMBIGUATION_PROMPT
        self._client_disambiguation_prompt: str = _DEFAULT_CLIENT_DISAMBIGUATION_PROMPT

        # Per-op timeouts cache (configurable). Recomputed in
        # on_config_changed.
        self._backend_timeouts: dict[str, float] = dict(_DEFAULT_BACKEND_TIMEOUTS)

        # Polling-loop diff caches — service-lifetime, NOT per-user.
        # Keyed by backend-side identifiers so two Gilbert users can
        # never see each other's data through these caches.
        self._poll_last_sessions: dict[tuple[str, str], MediaSession] = {}
        self._poll_last_added_at: dict[tuple[str, str], float] = {}
        self._poll_first_run_done: set[str] = set()

        # Adaptive-backoff state for poll_now_playing.
        self._now_playing_idle_count: int = 0
        self._now_playing_current_interval: float = 30.0
        self._now_playing_base_interval: float = 30.0
        self._now_playing_idle_threshold: int = 10
        self._now_playing_idle_max_interval: float = 300.0

        # Per-client play locks.
        self._client_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._client_locks_guard = asyncio.Lock()
        # Recent (idempotency_key, completed_at) per client for dedup.
        self._client_idempotency: dict[
            tuple[str, str], list[tuple[str, float, str]]
        ] = {}

        # Per-backend health state.
        self._health: dict[str, BackendHealth] = {}

        # Configuration
        self._default_kind: str = "movie"
        self._preferred_genres: tuple[str, ...] = ()
        self._ai_profile: str = "standard"
        self._client_disambiguation_threshold: int = 3
        self._recommend_next_max_candidates: int = 30
        self._poll_now_playing_enabled: bool = True
        self._poll_recently_added_enabled: bool = True
        self._poll_recently_added_interval: float = 300.0

        # Lazy-resolved scheduler (kept so a config-change can re-add
        # / refresh the poll-now-playing job at the new interval).
        self._scheduler: SchedulerProvider | None = None

    # ── Service lifecycle ───────────────────────────────────────────

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="media_library",
            capabilities=frozenset({"media_library", "ai_tools"}),
            requires=frozenset({"entity_storage"}),
            optional=frozenset(
                {"configuration", "event_bus", "ai_chat", "scheduler"}
            ),
            events=frozenset(
                {
                    "media.playback.started",
                    "media.playback.stopped",
                    "media.recently_added",
                    "media.backend.health_changed",
                }
            ),
            toggleable=True,
            toggle_description="Plex / Jellyfin video library and playback",
        )

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver

        storage_svc = resolver.require_capability("entity_storage")
        if isinstance(storage_svc, StorageProvider):
            self._storage = storage_svc.backend

        if self._storage is not None:
            await self._storage.ensure_index(
                IndexDefinition(
                    collection=_USER_MAP_COLLECTION,
                    fields=["gilbert_user_id", "backend_name"],
                    unique=True,
                )
            )
            await self._storage.ensure_index(
                IndexDefinition(
                    collection=_CLIENTS_CACHE_COLLECTION,
                    fields=["backend_name", "client_id"],
                    unique=True,
                )
            )

        bus_svc = resolver.get_capability("event_bus")
        if isinstance(bus_svc, EventBusProvider):
            self._event_bus = bus_svc.bus

        section: dict[str, Any] = {}
        config_svc = resolver.get_capability("configuration")
        if isinstance(config_svc, ConfigurationReader):
            section = config_svc.get_section_safe(self.config_namespace)
            self._apply_config(section)

        if not section.get("enabled", False):
            logger.info("Media library service disabled (enabled=false)")
            return

        self._enabled = True

        backends_section = section.get("backends", {})
        if not isinstance(backends_section, dict):
            backends_section = {}

        registry = MediaLibraryBackend.registered_backends()
        for backend_name, backend_cls in registry.items():
            sub = backends_section.get(backend_name, {})
            if not isinstance(sub, dict) or not sub.get("enabled", False):
                continue

            settings = sub.get("settings", {})
            if not isinstance(settings, dict):
                settings = {}

            try:
                instance = backend_cls()
                await instance.initialize(settings)
            except Exception as exc:
                # Failed initialize flips that backend to ``unhealthy`` —
                # the rest of the aggregator continues.
                logger.warning(
                    "Media library backend %s failed to initialize: %s",
                    backend_name,
                    exc,
                    exc_info=True,
                )
                self._set_health(backend_name, "unhealthy", error=str(exc))
                continue

            self._backends[backend_name] = instance
            self._set_health(backend_name, "healthy")
            logger.info("Media library backend %s initialized", backend_name)

        # Reap clients-cache rows older than 30 days (spec §6.9).
        await self._reap_clients_cache()

        # Schedule polling jobs via the SchedulerProvider capability.
        scheduler = resolver.get_capability("scheduler")
        if isinstance(scheduler, SchedulerProvider):
            self._scheduler = scheduler
            jitter_now = random.uniform(0.0, self._now_playing_current_interval)
            jitter_recent = random.uniform(0.0, self._poll_recently_added_interval)
            now_dt = _now_dt()
            if self._poll_now_playing_enabled and self._backends:
                scheduler.add_job(
                    name="media_library.poll_now_playing",
                    schedule=Schedule.every(
                        self._now_playing_current_interval,
                        start_at=now_dt + _seconds(jitter_now),
                    ),
                    callback=self._poll_now_playing,
                    system=True,
                )
            if self._poll_recently_added_enabled and self._backends:
                scheduler.add_job(
                    name="media_library.poll_recently_added",
                    schedule=Schedule.every(
                        self._poll_recently_added_interval,
                        start_at=now_dt + _seconds(jitter_recent),
                    ),
                    callback=self._poll_recently_added,
                    system=True,
                )

            # Subscribe to our own bus so a tool-driven start_play
            # immediately resets the now-playing cadence.
            if self._event_bus is not None:
                self._event_bus.subscribe(
                    "media.playback.started", self._on_playback_started_event
                )

        logger.info(
            "Media library service started (%d backend(s): %s)",
            len(self._backends),
            ", ".join(self._backends),
        )

    async def stop(self) -> None:
        for name, backend in list(self._backends.items()):
            try:
                await backend.close()
            except Exception:
                logger.debug(
                    "Media library backend %s close raised", name, exc_info=True
                )
        self._backends.clear()
        self._enabled = False

    # ── Configurable ────────────────────────────────────────────────

    def config_params(self) -> list[ConfigParam]:
        # NOTE: Computed lazily on every call (NOT cached in __init__)
        # so plugins that loaded after the first call still surface in
        # the next Settings refresh — see spec §6.2 "Plugin load timing".
        params: list[ConfigParam] = [
            ConfigParam(
                key="enabled",
                type=ToolParameterType.BOOLEAN,
                description="Enable the media library service.",
                default=False,
                restart_required=True,
            ),
            ConfigParam(
                key="default_client",
                type=ToolParameterType.STRING,
                description=(
                    "Default client name when the user doesn't specify "
                    "one. Falls back to last-used."
                ),
                default="",
            ),
            ConfigParam(
                key="default_kind",
                type=ToolParameterType.STRING,
                description="Default media kind for ambiguous searches.",
                default="movie",
                choices=("movie", "show", "episode", "music_video"),
            ),
            ConfigParam(
                key="preferred_genres",
                type=ToolParameterType.STRING,
                description=(
                    "Comma-separated genres used by recommend_next to "
                    "pick candidates. Household-level default in v1; "
                    "per-user preferences are deferred to v2. Empty = "
                    "no preference."
                ),
                default="",
            ),
            ConfigParam(
                key="ai_profile",
                type=ToolParameterType.STRING,
                description=(
                    "AI profile used for recommend_next and "
                    "disambiguation calls. Avoid 'light' — small models "
                    "hallucinate client names."
                ),
                default="standard",
                choices_from="ai_profiles",
            ),
            ConfigParam(
                key="client_disambiguation_threshold",
                type=ToolParameterType.INTEGER,
                description=(
                    "Minimum candidate count before invoking the AI "
                    "to pick a client. Below this, deterministic "
                    "ordering (last-used > online > alphabetical) is "
                    "used."
                ),
                default=3,
            ),
            ConfigParam(
                key="recommend_next_max_candidates",
                type=ToolParameterType.INTEGER,
                description=(
                    "Cap on candidates passed into the recommend_next "
                    "AI call. Each item's summary is truncated to 200 "
                    "chars."
                ),
                default=30,
            ),
            ConfigParam(
                key="backend_timeout_seconds.search",
                type=ToolParameterType.NUMBER,
                description="Per-backend timeout (seconds) for search.",
                default=8.0,
            ),
            ConfigParam(
                key="backend_timeout_seconds.recently_added",
                type=ToolParameterType.NUMBER,
                description="Per-backend timeout for recently_added.",
                default=8.0,
            ),
            ConfigParam(
                key="backend_timeout_seconds.continue_watching",
                type=ToolParameterType.NUMBER,
                description="Per-backend timeout for continue_watching.",
                default=5.0,
            ),
            ConfigParam(
                key="backend_timeout_seconds.now_playing",
                type=ToolParameterType.NUMBER,
                description="Per-backend timeout for now_playing.",
                default=5.0,
            ),
            ConfigParam(
                key="backend_timeout_seconds.list_clients",
                type=ToolParameterType.NUMBER,
                description="Per-backend timeout for list_clients.",
                default=3.0,
            ),
            ConfigParam(
                key="backend_timeout_seconds.play",
                type=ToolParameterType.NUMBER,
                description="Per-client timeout for play.",
                default=10.0,
            ),
            ConfigParam(
                key="poll_now_playing.enabled",
                type=ToolParameterType.BOOLEAN,
                description=(
                    "Poll for in-progress sessions and emit "
                    "media.playback.started/stopped events."
                ),
                default=True,
            ),
            ConfigParam(
                key="poll_now_playing.interval_seconds",
                type=ToolParameterType.INTEGER,
                description=(
                    "Base poll interval. Adaptive backoff doubles up "
                    "to idle_max_interval_seconds when no sessions seen."
                ),
                default=30,
            ),
            ConfigParam(
                key="poll_now_playing.idle_threshold",
                type=ToolParameterType.INTEGER,
                description="Empty polls before backoff kicks in.",
                default=10,
            ),
            ConfigParam(
                key="poll_now_playing.idle_max_interval_seconds",
                type=ToolParameterType.INTEGER,
                description="Cap on the backed-off interval.",
                default=300,
            ),
            ConfigParam(
                key="poll_recently_added.enabled",
                type=ToolParameterType.BOOLEAN,
                description=(
                    "Poll for newly-added items and emit "
                    "media.recently_added events. The first poll cycle "
                    "after restart is a baseline run that emits no "
                    "events."
                ),
                default=True,
            ),
            ConfigParam(
                key="poll_recently_added.interval_seconds",
                type=ToolParameterType.INTEGER,
                description="How often to poll recently-added.",
                default=300,
            ),
            ConfigParam(
                key="recommend_next_prompt",
                type=ToolParameterType.STRING,
                description=(
                    "System prompt for the recommend_next AI call. "
                    "Leave blank to use the bundled default."
                ),
                default=_DEFAULT_RECOMMEND_NEXT_PROMPT,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="item_disambiguation_prompt",
                type=ToolParameterType.STRING,
                description=(
                    "System prompt used when AI item disambiguation is "
                    "needed (rare; visual UIBlock picker is the "
                    "default). Leave blank for default."
                ),
                default=_DEFAULT_ITEM_DISAMBIGUATION_PROMPT,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="client_disambiguation_prompt",
                type=ToolParameterType.STRING,
                description=(
                    "System prompt used when the user's named client "
                    "matches multiple devices. Leave blank for default."
                ),
                default=_DEFAULT_CLIENT_DISAMBIGUATION_PROMPT,
                multiline=True,
                ai_prompt=True,
            ),
        ]

        # Per-backend enable + settings — computed lazily so newly-loaded
        # plugins surface on the next Settings refresh.
        for backend_name, backend_cls in MediaLibraryBackend.registered_backends().items():
            params.append(
                ConfigParam(
                    key=f"backends.{backend_name}.enabled",
                    type=ToolParameterType.BOOLEAN,
                    description=f"Enable the {backend_name} backend.",
                    default=False,
                    restart_required=True,
                    backend_param=True,
                )
            )
            for bp in backend_cls.backend_config_params():
                params.append(
                    ConfigParam(
                        key=f"backends.{backend_name}.settings.{bp.key}",
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
        # Re-initialize each enabled backend with its updated settings.
        backends_section = config.get("backends", {})
        if not isinstance(backends_section, dict):
            backends_section = {}
        for backend_name, instance in list(self._backends.items()):
            sub = backends_section.get(backend_name, {})
            if not isinstance(sub, dict):
                continue
            settings = sub.get("settings", {})
            if not isinstance(settings, dict):
                settings = {}
            try:
                await instance.initialize(settings)
            except Exception:
                logger.exception(
                    "Media library backend %s reinitialize failed", backend_name
                )
                self._set_health(
                    backend_name, "unhealthy", error="reinitialize failed"
                )
                continue
            # Re-link wiped any cached per-user state inside the backend
            # (its on_config_changed handler should clear); reset health
            # to healthy until the next call confirms.
            self._set_health(backend_name, "healthy")

    def _apply_config(self, section: dict[str, Any]) -> None:
        # AI prompts — falsy-fallback so empty string resolves to default.
        self._recommend_next_prompt = (
            str(section.get("recommend_next_prompt", "") or "")
            or _DEFAULT_RECOMMEND_NEXT_PROMPT
        )
        self._item_disambiguation_prompt = (
            str(section.get("item_disambiguation_prompt", "") or "")
            or _DEFAULT_ITEM_DISAMBIGUATION_PROMPT
        )
        self._client_disambiguation_prompt = (
            str(section.get("client_disambiguation_prompt", "") or "")
            or _DEFAULT_CLIENT_DISAMBIGUATION_PROMPT
        )

        self._default_kind = str(section.get("default_kind", "movie") or "movie")
        genres_raw = str(section.get("preferred_genres", "") or "")
        self._preferred_genres = tuple(
            g.strip() for g in genres_raw.split(",") if g.strip()
        )
        self._ai_profile = str(section.get("ai_profile", "standard") or "standard")
        try:
            self._client_disambiguation_threshold = int(
                section.get("client_disambiguation_threshold", 3) or 3
            )
        except (TypeError, ValueError):
            self._client_disambiguation_threshold = 3
        try:
            self._recommend_next_max_candidates = int(
                section.get("recommend_next_max_candidates", 30) or 30
            )
        except (TypeError, ValueError):
            self._recommend_next_max_candidates = 30

        timeouts_raw = section.get("backend_timeout_seconds", {})
        if isinstance(timeouts_raw, dict):
            for op, default in _DEFAULT_BACKEND_TIMEOUTS.items():
                try:
                    self._backend_timeouts[op] = float(
                        timeouts_raw.get(op, default) or default
                    )
                except (TypeError, ValueError):
                    self._backend_timeouts[op] = default

        poll_now = section.get("poll_now_playing", {})
        if isinstance(poll_now, dict):
            self._poll_now_playing_enabled = bool(
                poll_now.get("enabled", True)
            )
            try:
                self._now_playing_base_interval = float(
                    poll_now.get("interval_seconds", 30) or 30
                )
            except (TypeError, ValueError):
                self._now_playing_base_interval = 30.0
            self._now_playing_current_interval = self._now_playing_base_interval
            try:
                self._now_playing_idle_threshold = int(
                    poll_now.get("idle_threshold", 10) or 10
                )
            except (TypeError, ValueError):
                self._now_playing_idle_threshold = 10
            try:
                self._now_playing_idle_max_interval = float(
                    poll_now.get("idle_max_interval_seconds", 300) or 300
                )
            except (TypeError, ValueError):
                self._now_playing_idle_max_interval = 300.0

        poll_recent = section.get("poll_recently_added", {})
        if isinstance(poll_recent, dict):
            self._poll_recently_added_enabled = bool(
                poll_recent.get("enabled", True)
            )
            try:
                self._poll_recently_added_interval = float(
                    poll_recent.get("interval_seconds", 300) or 300
                )
            except (TypeError, ValueError):
                self._poll_recently_added_interval = 300.0

    # ── ConfigActionProvider ────────────────────────────────────────

    def config_actions(self) -> list[ConfigAction]:
        actions: list[ConfigAction] = []
        # Service-level actions for the User Mappings + Health UI.
        actions.append(
            ConfigAction(
                key="test_backend",
                label="Test backend",
                description=(
                    "Ping a backend (Plex /identity, Jellyfin /System/Info)."
                ),
                required_role="admin",
            )
        )
        actions.append(
            ConfigAction(
                key="list_user_mappings",
                label="List user mappings",
                description="Read-only enumeration of current mappings.",
                required_role="admin",
                hidden=True,
            )
        )
        actions.append(
            ConfigAction(
                key="list_backend_users",
                label="List backend users",
                description=(
                    "Enumerate users on a backend "
                    "(payload: {backend: '...'})."
                ),
                required_role="admin",
                hidden=True,
            )
        )
        actions.append(
            ConfigAction(
                key="set_user_mapping",
                label="Link user",
                description=(
                    "Persist a Gilbert↔backend mapping (payload: "
                    "{gilbert_user_id, backend, backend_user_id, "
                    "backend_username})."
                ),
                required_role="admin",
                hidden=True,
            )
        )
        actions.append(
            ConfigAction(
                key="unlink_user_mapping",
                label="Unlink user",
                description=(
                    "Remove an existing mapping (payload: "
                    "{gilbert_user_id, backend})."
                ),
                required_role="admin",
                hidden=True,
            )
        )
        actions.append(
            ConfigAction(
                key="list_backend_health",
                label="Backend health",
                description="Per-backend health for the Settings banner.",
                required_role="admin",
                hidden=True,
            )
        )
        actions.append(
            ConfigAction(
                key="list_gilbert_users",
                label="List Gilbert users",
                description=(
                    "Read-only Gilbert user list for the User Mappings "
                    "dropdown."
                ),
                required_role="admin",
                hidden=True,
            )
        )
        # Per-backend actions are forwarded with a "<backend>." prefix
        # so two backends can declare the same leaf name (e.g. both
        # ``link_account``) without colliding.
        registry = MediaLibraryBackend.registered_backends()
        backend_actions = all_backend_actions(
            registry=registry,
            current_backend=None,  # use a fresh probe for each backend
        )
        for action in backend_actions:
            backend = action.backend
            actions.append(
                replace(
                    action,
                    key=f"{backend}.{action.key}" if backend else action.key,
                )
            )
        # Also forward instance-aware actions (e.g. once a backend is
        # running its ``backend_actions()`` may reflect live state).
        for backend_name, instance in self._backends.items():
            for action in all_backend_actions(
                registry={backend_name: type(instance)},
                current_backend=instance,
            ):
                # Replace the duplicate from the probe pass.
                key = f"{backend_name}.{action.key}"
                actions = [a for a in actions if a.key != key]
                actions.append(replace(action, key=key))
        return actions

    async def invoke_config_action(
        self, key: str, payload: dict[str, Any]
    ) -> ConfigActionResult:
        if key == "list_user_mappings":
            mappings = await self.list_user_mappings()
            return ConfigActionResult(
                status="ok",
                message=f"{len(mappings)} mapping(s).",
                data={"mappings": mappings},
            )
        if key == "list_backend_users":
            backend_name = str(payload.get("backend") or "").strip()
            if not backend_name:
                return ConfigActionResult(
                    status="error",
                    message="list_backend_users requires {backend}.",
                )
            try:
                users = await self.list_backend_users(backend_name)
            except MediaLibraryError as exc:
                return ConfigActionResult(
                    status="error", message=str(exc)
                )
            return ConfigActionResult(
                status="ok",
                message=f"{len(users)} user(s) on {backend_name}.",
                data={"users": users, "backend": backend_name},
            )
        if key == "set_user_mapping":
            gid = str(payload.get("gilbert_user_id") or "").strip()
            backend_name = str(payload.get("backend") or "").strip()
            buid = str(payload.get("backend_user_id") or "").strip()
            buname = str(payload.get("backend_username") or "").strip()
            if not gid or not backend_name or not buid:
                return ConfigActionResult(
                    status="error",
                    message=(
                        "set_user_mapping requires {gilbert_user_id, "
                        "backend, backend_user_id}."
                    ),
                )
            try:
                await self.set_user_mapping(
                    gid, backend_name, buid, backend_username=buname
                )
            except Exception as exc:
                return ConfigActionResult(
                    status="error", message=str(exc)
                )
            return ConfigActionResult(
                status="ok", message="Mapping saved."
            )
        if key == "unlink_user_mapping":
            gid = str(payload.get("gilbert_user_id") or "").strip()
            backend_name = str(payload.get("backend") or "").strip()
            if not gid or not backend_name:
                return ConfigActionResult(
                    status="error",
                    message=(
                        "unlink_user_mapping requires {gilbert_user_id, "
                        "backend}."
                    ),
                )
            removed = await self.unlink_user_mapping(gid, backend_name)
            return ConfigActionResult(
                status="ok",
                message=("Unlinked." if removed else "No mapping to unlink."),
            )
        if key == "list_backend_health":
            return ConfigActionResult(
                status="ok",
                message="Backend health snapshot.",
                data={"health": await self.list_backend_health()},
            )
        if key == "list_gilbert_users":
            gilbert_users: list[dict[str, str]] = []
            if self._resolver is not None:
                users_svc = self._resolver.get_capability("users")
                if users_svc is not None:
                    list_users = getattr(users_svc, "list_users", None)
                    if list_users is not None:
                        try:
                            raw = await list_users()
                        except Exception as exc:
                            return ConfigActionResult(
                                status="error", message=str(exc)
                            )
                        for u in raw or []:
                            gilbert_users.append(
                                {
                                    "user_id": str(u.get("_id") or ""),
                                    "display_name": str(
                                        u.get("display_name") or ""
                                    ),
                                    "email": str(u.get("email") or ""),
                                }
                            )
            return ConfigActionResult(
                status="ok",
                message=f"{len(gilbert_users)} Gilbert user(s).",
                data={"users": gilbert_users},
            )
        if key == "test_backend":
            backend_name = str(payload.get("backend") or "").strip()
            if not backend_name:
                return ConfigActionResult(
                    status="error",
                    message="test_backend requires {backend: <name>}",
                )
            instance = self._backends.get(backend_name)
            if instance is None:
                return ConfigActionResult(
                    status="error",
                    message=f"Backend '{backend_name}' is not running.",
                )
            try:
                clients = await asyncio.wait_for(
                    instance.list_clients(),
                    timeout=self._backend_timeouts.get("list_clients", 3.0),
                )
                self._set_health(backend_name, "healthy")
                return ConfigActionResult(
                    status="ok",
                    message=(
                        f"{backend_name} reachable — {len(clients)} client(s)."
                    ),
                    data={"client_count": len(clients)},
                )
            except Exception as exc:
                self._set_health(backend_name, "unhealthy", error=str(exc))
                return ConfigActionResult(
                    status="error",
                    message=f"{backend_name} unreachable: {exc}",
                )

        # Per-backend action: "<backend>.<action>"
        backend_name, _, action_key = key.partition(".")
        if not backend_name or not action_key:
            return ConfigActionResult(
                status="error",
                message=f"Malformed action key '{key}' — expected '<backend>.<action>'",
            )
        instance = self._backends.get(backend_name)
        if instance is not None:
            return await invoke_backend_action(instance, action_key, payload)
        # Backend not running yet — try a fresh probe so link flows
        # work before the backend is enabled.
        registry = MediaLibraryBackend.registered_backends()
        cls = registry.get(backend_name)
        if cls is None:
            return ConfigActionResult(
                status="error",
                message=f"Unknown backend '{backend_name}'.",
            )
        try:
            probe = cls()
        except Exception as exc:
            return ConfigActionResult(
                status="error",
                message=f"Could not instantiate {backend_name}: {exc}",
            )
        return await invoke_backend_action(probe, action_key, payload)

    # ── Capability properties ───────────────────────────────────────
    #
    # Capability gating reads "configured-and-supports-X," NOT
    # "currently-healthy-and-supports-X" — tools never disappear
    # mid-conversation due to backend health flips.

    @property
    def supports_now_playing(self) -> bool:
        return any(b.supports_now_playing for b in self._backends.values())

    @property
    def supports_continue_watching(self) -> bool:
        return any(b.supports_continue_watching for b in self._backends.values())

    @property
    def supports_recently_added(self) -> bool:
        return any(b.supports_recently_added for b in self._backends.values())

    @property
    def supports_seek(self) -> bool:
        return any(b.supports_seek for b in self._backends.values())

    @property
    def supports_next_episode(self) -> bool:
        return any(b.supports_next_episode for b in self._backends.values())

    @property
    def supports_recommend_next(self) -> bool:
        # AI capability + at least one backend.
        if not self._backends:
            return False
        if self._resolver is None:
            return False
        ai_svc = self._resolver.get_capability("ai_chat")
        return isinstance(ai_svc, AISamplingProvider)

    # ── Health surface ──────────────────────────────────────────────

    def _set_health(
        self, backend_name: str, status: str, error: str = ""
    ) -> None:
        """Update health state and emit ``media.backend.health_changed``
        on transitions.
        """
        previous = self._health.get(backend_name)
        previous_status = previous.status if previous else "healthy"
        now = time.time()
        if status == "healthy":
            new_health = BackendHealth(
                backend_name=backend_name,
                status="healthy",
                last_error="",
                last_error_at=previous.last_error_at if previous else 0.0,
                last_success_at=now,
            )
        else:
            new_health = BackendHealth(
                backend_name=backend_name,
                status=status,
                last_error=error,
                last_error_at=now,
                last_success_at=previous.last_success_at if previous else 0.0,
            )
        self._health[backend_name] = new_health
        if (
            previous_status != status
            and self._event_bus is not None
        ):
            asyncio.create_task(
                self._event_bus.publish(
                    Event(
                        event_type="media.backend.health_changed",
                        data={
                            "backend": backend_name,
                            "status": status,
                            "previous_status": previous_status,
                            "error": error,
                        },
                        source="media_library",
                    )
                ),
                name=f"media-health-changed-{backend_name}",
            )

    async def list_backend_health(self) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for backend_name in self._backends:
            health = self._health.get(
                backend_name,
                BackendHealth(backend_name=backend_name, status="healthy"),
            )
            out.append(asdict(health))
        return out

    # ── Fan-out helper ──────────────────────────────────────────────

    async def _fanout(
        self,
        op: Callable[[MediaLibraryBackend], Awaitable[T]],
        *,
        timeout_seconds: float,
        op_name: str,
    ) -> list[tuple[str, T | BaseException]]:
        """Run ``op`` against every backend with a per-backend timeout.

        Returns ``[(backend_name, result_or_exception)]``. Exceptions
        (including ``TimeoutError`` from ``asyncio.wait_for``) are
        returned, not raised, so callers can surface partial results.
        """
        if not self._backends:
            return []

        async def _wrap(name: str, backend: MediaLibraryBackend) -> T | BaseException:
            try:
                return await asyncio.wait_for(op(backend), timeout=timeout_seconds)
            except TimeoutError as exc:
                logger.warning(
                    "Media library %s on %s timed out after %.1fs",
                    op_name,
                    name,
                    timeout_seconds,
                )
                self._set_health(name, "degraded", error=f"{op_name} timeout")
                return exc
            except MediaLibraryUnavailableError as exc:
                logger.warning(
                    "Media library %s on %s unavailable: %s",
                    op_name,
                    name,
                    exc,
                )
                self._set_health(name, "unhealthy", error=str(exc))
                return exc
            except MediaLibraryError as exc:
                logger.warning(
                    "Media library %s on %s domain error: %s",
                    op_name,
                    name,
                    exc,
                )
                return exc
            except Exception as exc:
                logger.warning(
                    "Media library %s on %s failed: %s",
                    op_name,
                    name,
                    exc,
                    exc_info=True,
                )
                self._set_health(name, "degraded", error=str(exc))
                return exc

        names = list(self._backends.keys())
        # Spawn each task with a copy_context so a ContextVar.set inside
        # one branch can't leak to siblings.
        results: list[T | BaseException] = await asyncio.gather(
            *(
                self._spawn_with_context(_wrap(name, self._backends[name]))
                for name in names
            ),
            return_exceptions=False,
        )
        # Update health to "healthy" for backends that returned a real
        # result (not an exception).
        for name, result in zip(names, results, strict=False):
            if not isinstance(result, BaseException):
                self._set_health(name, "healthy")
        return list(zip(names, results, strict=False))

    async def _spawn_with_context(self, coro: Awaitable[T]) -> T:
        """Run ``coro`` in a copied context so ContextVar mutations
        inside don't leak.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()

        async def _runner() -> None:
            try:
                future.set_result(await coro)
            except BaseException as exc:  # noqa: BLE001
                if not future.done():
                    future.set_exception(exc)

        task = asyncio.Task(_runner(), context=contextvars.copy_context())
        try:
            return await future
        finally:
            if not task.done():
                task.cancel()

    # ── User mapping CRUD ───────────────────────────────────────────

    async def resolve_backend_user(
        self, gilbert_user_id: str, backend_name: str
    ) -> str:
        """Return the backend's user id for this Gilbert user, or ''.

        Empty string means 'fall back to backend's primary / admin user'.
        """
        if self._storage is None or not gilbert_user_id or not backend_name:
            return ""
        rows = await self._storage.query(
            Query(
                collection=_USER_MAP_COLLECTION,
                filters=[
                    Filter(
                        field="gilbert_user_id",
                        op=FilterOp.EQ,
                        value=gilbert_user_id,
                    ),
                    Filter(
                        field="backend_name",
                        op=FilterOp.EQ,
                        value=backend_name,
                    ),
                ],
                limit=1,
            )
        )
        if rows:
            return str(rows[0].get("backend_user_id") or "")
        return ""

    async def set_user_mapping(
        self,
        gilbert_user_id: str,
        backend_name: str,
        backend_user_id: str,
        backend_username: str = "",
    ) -> None:
        if self._storage is None:
            raise RuntimeError("Storage backend not available")
        if not gilbert_user_id or not backend_name:
            raise ValueError("gilbert_user_id and backend_name are required")
        # Upsert: if a row exists for (user, backend), overwrite.
        existing = await self._storage.query(
            Query(
                collection=_USER_MAP_COLLECTION,
                filters=[
                    Filter(
                        field="gilbert_user_id",
                        op=FilterOp.EQ,
                        value=gilbert_user_id,
                    ),
                    Filter(
                        field="backend_name",
                        op=FilterOp.EQ,
                        value=backend_name,
                    ),
                ],
                limit=1,
            )
        )
        now = time.time()
        if existing:
            doc_id = existing[0]["_id"]
            row = {
                "gilbert_user_id": gilbert_user_id,
                "backend_name": backend_name,
                "backend_user_id": backend_user_id,
                "backend_username": backend_username,
                "created_at": existing[0].get("created_at", now),
                "updated_at": now,
            }
            await self._storage.put(_USER_MAP_COLLECTION, doc_id, row)
        else:
            doc_id = f"mlu_{uuid.uuid4().hex[:12]}"
            row = {
                "gilbert_user_id": gilbert_user_id,
                "backend_name": backend_name,
                "backend_user_id": backend_user_id,
                "backend_username": backend_username,
                "created_at": now,
                "updated_at": now,
            }
            await self._storage.put(_USER_MAP_COLLECTION, doc_id, row)

    async def unlink_user_mapping(
        self, gilbert_user_id: str, backend_name: str
    ) -> bool:
        if self._storage is None:
            return False
        existing = await self._storage.query(
            Query(
                collection=_USER_MAP_COLLECTION,
                filters=[
                    Filter(
                        field="gilbert_user_id",
                        op=FilterOp.EQ,
                        value=gilbert_user_id,
                    ),
                    Filter(
                        field="backend_name",
                        op=FilterOp.EQ,
                        value=backend_name,
                    ),
                ],
                limit=1,
            )
        )
        if not existing:
            return False
        await self._storage.delete(_USER_MAP_COLLECTION, existing[0]["_id"])
        return True

    async def list_user_mappings(
        self, gilbert_user_id: str = ""
    ) -> list[dict[str, str]]:
        if self._storage is None:
            return []
        filters: list[Filter] = []
        if gilbert_user_id:
            filters.append(
                Filter(
                    field="gilbert_user_id",
                    op=FilterOp.EQ,
                    value=gilbert_user_id,
                )
            )
        rows = await self._storage.query(
            Query(collection=_USER_MAP_COLLECTION, filters=filters)
        )
        return [
            {
                "gilbert_user_id": str(r.get("gilbert_user_id", "")),
                "backend_name": str(r.get("backend_name", "")),
                "backend_user_id": str(r.get("backend_user_id", "")),
                "backend_username": str(r.get("backend_username", "")),
            }
            for r in rows
        ]

    async def list_backend_users(self, backend_name: str) -> list[dict[str, str]]:
        backend = self._backends.get(backend_name)
        if backend is None:
            raise MediaLibraryUnavailableError(
                f"Backend '{backend_name}' is not running."
            )
        return await backend.list_backend_users()

    # ── Aggregating reads ───────────────────────────────────────────

    async def search(
        self,
        query: str,
        *,
        kind: MediaKind | None = None,
        gilbert_user_id: str | None = None,
        filters: MediaSearchFilters | None = None,
    ) -> list[MediaItem]:
        """Fan out ``search`` and merge by stable round-robin interleaving.

        ``MediaSearchFilters.limit`` is service-side capped at 50.
        Per-backend relevance ordering is preserved (no homegrown
        Levenshtein); the round-robin merge alternates backends in
        ``backend_name`` ascending order.
        """
        effective_filters = filters or MediaSearchFilters()
        if kind is not None:
            kinds = effective_filters.kinds or (kind,)
            effective_filters = replace(effective_filters, kinds=kinds)
        capped_limit = min(effective_filters.limit, _SEARCH_LIMIT_CAP)
        effective_filters = replace(effective_filters, limit=capped_limit)

        async def _op(b: MediaLibraryBackend) -> list[MediaItem]:
            backend_user_id = ""
            if gilbert_user_id:
                backend_user_id = await self.resolve_backend_user(
                    gilbert_user_id, b.backend_name
                )
            return await b.search(
                query,
                filters=effective_filters,
                backend_user_id=backend_user_id,
            )

        results = await self._fanout(
            _op,
            timeout_seconds=self._backend_timeouts.get("search", 8.0),
            op_name="search",
        )
        per_backend: dict[str, list[MediaItem]] = {}
        for name, result in results:
            if isinstance(result, BaseException):
                continue
            per_backend[name] = result

        return _round_robin_merge(per_backend, capped_limit)

    async def recently_added(
        self,
        *,
        kind: MediaKind | None = None,
        limit: int = 10,
    ) -> list[RecentlyAddedEntry]:
        async def _op(b: MediaLibraryBackend) -> list[RecentlyAddedEntry]:
            if not b.supports_recently_added:
                return []
            return await b.recently_added(kind=kind, limit=limit)

        results = await self._fanout(
            _op,
            timeout_seconds=self._backend_timeouts.get("recently_added", 8.0),
            op_name="recently_added",
        )
        merged: list[RecentlyAddedEntry] = []
        for _name, result in results:
            if isinstance(result, BaseException):
                continue
            merged.extend(result)
        merged.sort(
            key=lambda e: (-e.added_at, e.item.backend_name, e.item.id)
        )
        return merged[:limit]

    async def continue_watching(
        self,
        *,
        gilbert_user_id: str,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Per-user continue-watching with the missing-mapping policy.

        Returns ``{entries, unmapped_backends, hint?}``. Backends without
        a mapping for this user are silently skipped (per spec §6.3) —
        never silent admin-token fallback.
        """
        if not self._backends:
            return {"entries": [], "unmapped_backends": []}

        unmapped: list[str] = []
        mapped: dict[str, str] = {}
        for backend_name in self._backends:
            mapped_id = await self.resolve_backend_user(
                gilbert_user_id, backend_name
            )
            if mapped_id:
                mapped[backend_name] = mapped_id
            else:
                unmapped.append(backend_name)

        if not mapped:
            return {
                "entries": [],
                "unmapped_backends": unmapped,
                "error": (
                    "No backend account linked to your Gilbert user; "
                    "ask an admin to run /media link-user"
                ),
            }

        async def _op(b: MediaLibraryBackend) -> list[ContinueWatchingEntry]:
            if not b.supports_continue_watching:
                return []
            backend_user_id = mapped.get(b.backend_name, "")
            if not backend_user_id:
                return []
            return await b.continue_watching(
                backend_user_id=backend_user_id, limit=limit
            )

        results = await self._fanout(
            _op,
            timeout_seconds=self._backend_timeouts.get(
                "continue_watching", 5.0
            ),
            op_name="continue_watching",
        )
        per_backend: dict[str, list[ContinueWatchingEntry]] = {}
        for name, result in results:
            if isinstance(result, BaseException):
                continue
            per_backend[name] = result

        merged_items: list[ContinueWatchingEntry] = []
        # Round-robin across mapped backends.
        backends_sorted = sorted(per_backend.keys())
        idx = 0
        while True:
            added_this_round = False
            for name in backends_sorted:
                items = per_backend[name]
                if idx < len(items):
                    merged_items.append(items[idx])
                    added_this_round = True
                    if len(merged_items) >= limit:
                        break
            if not added_this_round or len(merged_items) >= limit:
                break
            idx += 1

        out: dict[str, Any] = {
            "entries": merged_items,
            "unmapped_backends": unmapped,
        }
        if unmapped:
            out["hint"] = (
                f"Continue-watching from {', '.join(sorted(mapped))} "
                f"only — {', '.join(unmapped)} not linked to your "
                f"Gilbert user."
            )
        return out

    async def list_clients(self) -> list[MediaClient]:
        """Union across backends. Offline (cached) clients re-surface
        with ``is_online=False`` so the AI can phrase 'asleep'.
        """
        async def _op(b: MediaLibraryBackend) -> list[MediaClient]:
            return await b.list_clients()

        results = await self._fanout(
            _op,
            timeout_seconds=self._backend_timeouts.get("list_clients", 3.0),
            op_name="list_clients",
        )

        live: list[MediaClient] = []
        live_keys: set[tuple[str, str]] = set()
        per_backend_returned: set[str] = set()
        for name, result in results:
            if isinstance(result, BaseException):
                continue
            per_backend_returned.add(name)
            for client in result:
                live.append(client)
                live_keys.add((client.backend_name, client.client_id))

        # Cache update — merge-not-replace.
        if self._storage is not None:
            now = time.time()
            for client in live:
                row_id = f"{client.backend_name}:{client.client_id}"
                row = {
                    "backend_name": client.backend_name,
                    "client_id": client.client_id,
                    "name": client.name,
                    "device": client.device,
                    "platform": client.platform,
                    "last_seen_at": now,
                    "address": client.address,
                }
                # Preserve existing last_used_at if present.
                existing = await self._storage.get(
                    _CLIENTS_CACHE_COLLECTION, row_id
                )
                if existing:
                    row["last_used_at"] = existing.get("last_used_at", 0.0)
                else:
                    row["last_used_at"] = 0.0
                await self._storage.put(
                    _CLIENTS_CACHE_COLLECTION, row_id, row
                )

            # Re-surface offline cached clients (last_seen_at within 30 days)
            # for backends that DID return a response — if a backend
            # itself is unreachable, we don't want to surface its
            # potentially-stale cached clients.
            cutoff = now - _CLIENTS_CACHE_RETENTION_SECONDS
            cached_rows = await self._storage.query(
                Query(
                    collection=_CLIENTS_CACHE_COLLECTION,
                    filters=[
                        Filter(
                            field="last_seen_at",
                            op=FilterOp.GTE,
                            value=cutoff,
                        )
                    ],
                )
            )
            for row in cached_rows:
                backend_name = str(row.get("backend_name", ""))
                if backend_name not in per_backend_returned:
                    continue
                client_id = str(row.get("client_id", ""))
                if (backend_name, client_id) in live_keys:
                    continue
                live.append(
                    MediaClient(
                        client_id=client_id,
                        backend_name=backend_name,
                        server_id="",
                        name=str(row.get("name", "")),
                        device=str(row.get("device", "")),
                        platform=str(row.get("platform", "")),
                        address=str(row.get("address", "")),
                        is_online=False,
                        last_seen_at=_as_float(row.get("last_seen_at", 0.0)),
                    )
                )

        return live

    async def now_playing(
        self, client_name: str | None = None
    ) -> list[MediaSession]:
        """Live, NOT cached. Spec §7.2: the tool path bypasses the
        polled cache so users asking 'what's playing right now?'
        get sub-second freshness.
        """
        async def _op(b: MediaLibraryBackend) -> list[MediaSession]:
            if not b.supports_now_playing:
                return []
            return await b.now_playing()

        results = await self._fanout(
            _op,
            timeout_seconds=self._backend_timeouts.get("now_playing", 5.0),
            op_name="now_playing",
        )
        sessions: list[MediaSession] = []
        for _name, result in results:
            if isinstance(result, BaseException):
                continue
            sessions.extend(result)

        if client_name:
            needle = client_name.strip().lower()
            sessions = [
                s for s in sessions if needle in s.client.name.lower()
            ]
        return sessions

    # ── Client resolution ───────────────────────────────────────────

    async def find_clients(self, name_or_id: str) -> list[MediaClient]:
        """Return ALL clients whose name (substring, case-insensitive)
        or client_id (exact) matches.
        """
        clients = await self.list_clients()
        if not name_or_id:
            return list(clients)
        needle = name_or_id.strip().lower()
        matches: list[MediaClient] = []
        for client in clients:
            if client.client_id == name_or_id or needle in client.name.lower():
                matches.append(client)
        return matches

    async def find_client(
        self, name_or_id: str, *, gilbert_user_id: str = ""
    ) -> MediaClient:
        """Resolve to a single client, raising on 0 or ambiguous matches."""
        candidates = await self.find_clients(name_or_id)
        if not candidates:
            raise MediaClientNotFoundError(
                f"No client named '{name_or_id}' on any configured backend"
            )
        if len(candidates) == 1:
            return candidates[0]
        # Disambiguation — apply the deterministic ordering when the
        # candidate count is below the configured threshold; AI is only
        # invoked above the threshold (and even then only when AI
        # capability is available).
        ordered = await self._deterministic_client_order(
            candidates, gilbert_user_id
        )
        if len(ordered) >= self._client_disambiguation_threshold:
            ai_resolved = await self._ai_pick_client(ordered)
            if ai_resolved is not None:
                return ai_resolved
        # Multiple candidates without an AI resolver — surface as
        # ambiguous so the caller can present a UIBlock picker.
        raise MediaClientAmbiguousError(
            f"Multiple matches for client '{name_or_id}'",
            candidates=ordered,
        )

    async def _deterministic_client_order(
        self, candidates: list[MediaClient], gilbert_user_id: str
    ) -> list[MediaClient]:
        """Last-used (per user) > online > alphabetical."""
        last_used: dict[tuple[str, str], float] = {}
        if self._storage is not None and gilbert_user_id:
            for client in candidates:
                row = await self._storage.get(
                    _CLIENTS_CACHE_COLLECTION,
                    f"{client.backend_name}:{client.client_id}",
                )
                if row:
                    used_for = row.get("last_used_for_user_id", "")
                    if used_for == gilbert_user_id:
                        last_used[
                            (client.backend_name, client.client_id)
                        ] = float(row.get("last_used_at", 0.0) or 0.0)
        return sorted(
            candidates,
            key=lambda c: (
                -(last_used.get((c.backend_name, c.client_id), 0.0)),
                0 if c.is_online else 1,
                c.name.lower(),
            ),
        )

    async def _ai_pick_client(
        self, candidates: list[MediaClient]
    ) -> MediaClient | None:
        if self._resolver is None:
            return None
        ai_svc = self._resolver.get_capability("ai_chat")
        if not isinstance(ai_svc, AISamplingProvider):
            return None
        payload = json.dumps(
            [_media_client_to_dict(c) for c in candidates], default=str
        )
        try:
            resp = await ai_svc.complete_one_shot(
                messages=[
                    Message(
                        role=MessageRole.USER,
                        content=f"<candidates>{payload}</candidates>",
                    )
                ],
                system_prompt=self._client_disambiguation_prompt,
                profile_name=self._ai_profile,
            )
        except Exception:
            logger.warning(
                "Client disambiguation AI call failed", exc_info=True
            )
            return None
        response_text = resp.message.content if resp.message else ""
        try:
            parsed = json.loads(response_text or "{}")
        except (json.JSONDecodeError, AttributeError):
            return None
        if not isinstance(parsed, dict):
            return None
        chosen = str(parsed.get("client_id", ""))
        for c in candidates:
            if c.client_id == chosen:
                return c
        return None

    # ── Playback ────────────────────────────────────────────────────

    async def _client_lock(
        self, backend_name: str, client_id: str
    ) -> asyncio.Lock:
        async with self._client_locks_guard:
            key = (backend_name, client_id)
            lock = self._client_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._client_locks[key] = lock
            return lock

    def _check_idempotency(
        self, key: tuple[str, str], idempotency_key: str
    ) -> str | None:
        """Return a cached idempotent outcome if one matches within the
        TTL. Returns ``None`` to indicate proceed-and-record.
        """
        if not idempotency_key:
            return None
        history = self._client_idempotency.get(key, [])
        now = time.monotonic()
        # Trim expired entries.
        history = [
            h for h in history if (now - h[1]) <= _IDEMPOTENCY_TTL_SECONDS
        ]
        self._client_idempotency[key] = history
        for prev_key, _ts, prev_outcome in history:
            if prev_key == idempotency_key:
                return prev_outcome
        return None

    def _record_idempotency(
        self, key: tuple[str, str], idempotency_key: str, outcome: str
    ) -> None:
        if not idempotency_key:
            return
        history = self._client_idempotency.setdefault(key, [])
        history.append((idempotency_key, time.monotonic(), outcome))
        if len(history) > _IDEMPOTENCY_HISTORY_SIZE:
            del history[: len(history) - _IDEMPOTENCY_HISTORY_SIZE]

    async def play_item(
        self,
        item: MediaItem,
        client: MediaClient,
        *,
        offset_seconds: float = 0.0,
        gilbert_user_id: str = "",
        initiator: str = "user",
        idempotency_key: str = "",
    ) -> str:
        """Resolve to ``play_item`` outcome (``"played" | "deduped"``)."""
        backend = self._backends.get(client.backend_name)
        if backend is None:
            raise MediaLibraryUnavailableError(
                f"Backend '{client.backend_name}' is not running."
            )

        lock_key = (client.backend_name, client.client_id)
        lock = await self._client_lock(*lock_key)
        async with lock:
            cached = self._check_idempotency(lock_key, idempotency_key)
            if cached is not None:
                return "deduped"
            backend_user_id = ""
            if gilbert_user_id:
                backend_user_id = await self.resolve_backend_user(
                    gilbert_user_id, client.backend_name
                )
            command = MediaPlayCommand(
                item=item,
                client=client,
                offset_seconds=offset_seconds,
                idempotency_key=idempotency_key,
            )
            try:
                await asyncio.wait_for(
                    backend.play(command, backend_user_id=backend_user_id),
                    timeout=self._backend_timeouts.get("play", 10.0),
                )
            except TimeoutError:
                self._set_health(
                    client.backend_name, "degraded", error="play timeout"
                )
                raise MediaLibraryUnavailableError(
                    f"play on {client.backend_name} timed out"
                ) from None
            except MediaLibraryError as exc:
                self._set_health(
                    client.backend_name, "unhealthy", error=str(exc)
                )
                raise

            self._set_health(client.backend_name, "healthy")
            self._record_idempotency(lock_key, idempotency_key, "played")

            # Update clients-cache last_used_at for this user.
            if self._storage is not None and gilbert_user_id:
                row_id = f"{client.backend_name}:{client.client_id}"
                row = await self._storage.get(
                    _CLIENTS_CACHE_COLLECTION, row_id
                ) or {}
                row.update(
                    {
                        "backend_name": client.backend_name,
                        "client_id": client.client_id,
                        "name": client.name,
                        "device": client.device,
                        "platform": client.platform,
                        "address": client.address,
                        "last_seen_at": time.time(),
                        "last_used_at": time.time(),
                        "last_used_for_user_id": gilbert_user_id,
                    }
                )
                await self._storage.put(
                    _CLIENTS_CACHE_COLLECTION, row_id, row
                )

            # Emit media.playback.started.
            if self._event_bus is not None:
                await self._event_bus.publish(
                    Event(
                        event_type="media.playback.started",
                        data={
                            "backend": client.backend_name,
                            "client_id": client.client_id,
                            "client_name": client.name,
                            "item_id": item.id,
                            "item_title": item.title,
                            "item_kind": item.kind.value,
                            "item_year": item.year,
                            "user_id": gilbert_user_id,
                            "library_section": item.library_section,
                            "initiator": initiator,
                        },
                        source="media_library",
                    )
                )
            return "played"

    async def pause_client(self, client: MediaClient) -> None:
        backend = self._require_backend(client.backend_name)
        await backend.pause(client.client_id)

    async def resume_client(self, client: MediaClient) -> None:
        backend = self._require_backend(client.backend_name)
        await backend.resume(client.client_id)

    async def stop_client(self, client: MediaClient) -> None:
        backend = self._require_backend(client.backend_name)
        await backend.stop(client.client_id)

    async def seek_client(
        self, client: MediaClient, position_seconds: float
    ) -> None:
        backend = self._require_backend(client.backend_name)
        if not backend.supports_seek:
            raise MediaLibraryUnavailableError(
                f"{client.backend_name} does not support seek"
            )
        await backend.seek(client.client_id, position_seconds)

    def _require_backend(self, backend_name: str) -> MediaLibraryBackend:
        backend = self._backends.get(backend_name)
        if backend is None:
            raise MediaLibraryUnavailableError(
                f"Backend '{backend_name}' is not running."
            )
        return backend

    async def next_episode_for(
        self, item: MediaItem, *, gilbert_user_id: str
    ) -> MediaItem | None:
        backend = self._backends.get(item.backend_name)
        if backend is None or not backend.supports_next_episode:
            return None
        backend_user_id = await self.resolve_backend_user(
            gilbert_user_id, item.backend_name
        )
        return await backend.next_episode(
            item.id, backend_user_id=backend_user_id
        )

    # ── Polling ─────────────────────────────────────────────────────

    async def _poll_now_playing(self) -> None:
        # Always set SYSTEM at job entry (not relying on the implicit
        # default) — matches knowledge / calendar / camera precedent.
        set_current_user(UserContext.SYSTEM)
        if not self._backends:
            return

        async def _op(b: MediaLibraryBackend) -> list[MediaSession]:
            if not b.supports_now_playing:
                return []
            return await b.now_playing()

        results = await self._fanout(
            _op,
            timeout_seconds=self._backend_timeouts.get("now_playing", 5.0),
            op_name="now_playing",
        )

        current: dict[tuple[str, str], MediaSession] = {}
        for _name, result in results:
            if isinstance(result, BaseException):
                continue
            for session in result:
                current[(session.backend_name, session.session_id)] = session

        prev = self._poll_last_sessions
        # New sessions → media.playback.started
        new_keys = set(current) - set(prev)
        # Disappeared → media.playback.stopped
        gone_keys = set(prev) - set(current)

        if self._event_bus is not None:
            for key in new_keys:
                session = current[key]
                await self._event_bus.publish(
                    Event(
                        event_type="media.playback.started",
                        data={
                            "backend": session.backend_name,
                            "client_id": session.client.client_id,
                            "client_name": session.client.name,
                            "item_id": session.item.id,
                            "item_title": session.item.title,
                            "item_kind": session.item.kind.value,
                            "item_year": session.item.year,
                            "user_id": "",  # poll-detected
                            "backend_user_name": session.backend_user_name,
                            "library_section": session.item.library_section,
                            "initiator": "external",
                        },
                        source="media_library",
                    )
                )
            for key in gone_keys:
                session = prev[key]
                pct = 0.0
                if session.duration_seconds > 0:
                    pct = (
                        session.position_seconds / session.duration_seconds
                    )
                await self._event_bus.publish(
                    Event(
                        event_type="media.playback.stopped",
                        data={
                            "backend": session.backend_name,
                            "client_id": session.client.client_id,
                            "client_name": session.client.name,
                            "item_id": session.item.id,
                            "item_title": session.item.title,
                            "item_kind": session.item.kind.value,
                            "position_seconds": session.position_seconds,
                            "progress_pct": pct,
                            "user_id": "",
                            "backend_user_name": session.backend_user_name,
                            "library_section": session.item.library_section,
                            "initiator": "external",
                        },
                        source="media_library",
                    )
                )

        self._poll_last_sessions = current

        # Adaptive backoff.
        if not current:
            self._now_playing_idle_count += 1
            if self._now_playing_idle_count >= self._now_playing_idle_threshold:
                self._now_playing_current_interval = min(
                    self._now_playing_current_interval * 2.0,
                    self._now_playing_idle_max_interval,
                )
        else:
            if self._now_playing_idle_count > 0 or (
                self._now_playing_current_interval
                != self._now_playing_base_interval
            ):
                self._now_playing_idle_count = 0
                self._now_playing_current_interval = (
                    self._now_playing_base_interval
                )

    async def _poll_recently_added(self) -> None:
        set_current_user(UserContext.SYSTEM)
        if not self._backends:
            return
        is_baseline = (
            "media_library.poll_recently_added" not in self._poll_first_run_done
        )

        async def _op(b: MediaLibraryBackend) -> list[RecentlyAddedEntry]:
            if not b.supports_recently_added:
                return []
            return await b.recently_added(limit=20)

        results = await self._fanout(
            _op,
            timeout_seconds=self._backend_timeouts.get("recently_added", 8.0),
            op_name="recently_added",
        )

        if is_baseline:
            for name, result in results:
                if isinstance(result, BaseException):
                    continue
                for entry in result:
                    section = entry.item.library_section or ""
                    key = (name, section)
                    self._poll_last_added_at[key] = max(
                        self._poll_last_added_at.get(key, 0.0),
                        entry.added_at,
                    )
            self._poll_first_run_done.add(
                "media_library.poll_recently_added"
            )
            return

        if self._event_bus is None:
            return

        for name, result in results:
            if isinstance(result, BaseException):
                continue
            for entry in result:
                section = entry.item.library_section or ""
                key = (name, section)
                last = self._poll_last_added_at.get(key, 0.0)
                if entry.added_at <= last:
                    continue
                await self._event_bus.publish(
                    Event(
                        event_type="media.recently_added",
                        data={
                            "backend": name,
                            "library_section": section,
                            "item_id": entry.item.id,
                            "item_title": entry.item.title,
                            "item_kind": entry.item.kind.value,
                            "item_year": entry.item.year,
                            "added_at": entry.added_at,
                        },
                        source="media_library",
                    )
                )
                self._poll_last_added_at[key] = entry.added_at

    async def _on_playback_started_event(self, event: Event) -> None:
        # Tool-driven play resets adaptive cadence so the next poll
        # fires on the next tick rather than 5 minutes from now.
        self._now_playing_idle_count = 0
        self._now_playing_current_interval = self._now_playing_base_interval

    async def _reap_clients_cache(self) -> None:
        if self._storage is None:
            return
        cutoff = time.time() - _CLIENTS_CACHE_RETENTION_SECONDS
        try:
            await self._storage.delete_query(
                Query(
                    collection=_CLIENTS_CACHE_COLLECTION,
                    filters=[
                        Filter(
                            field="last_seen_at",
                            op=FilterOp.LT,
                            value=cutoff,
                        )
                    ],
                )
            )
        except Exception:
            logger.debug("Clients-cache reap failed", exc_info=True)

    # ── Tools ───────────────────────────────────────────────────────

    @property
    def tool_provider_name(self) -> str:
        return "media_library"

    def get_tools(
        self, user_ctx: UserContext | None = None
    ) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        tools: list[ToolDefinition] = [
            ToolDefinition(
                name="list_media_clients",
                slash_group="media",
                slash_command="clients",
                slash_help=(
                    "List media clients (TVs, phones, etc.) Gilbert can "
                    "cast to."
                ),
                description=(
                    "List all video clients across configured Plex/"
                    "Jellyfin backends. Includes offline (last-known) "
                    "clients with is_online=false so the AI can phrase "
                    "'the Apple TV is asleep'."
                ),
                required_role="everyone",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="search_media",
                slash_group="media",
                slash_command="search",
                slash_help="Search the library: /media search <query>",
                description=(
                    "Search your video library (movies, shows, episodes, "
                    "optionally photos / music videos / music tracks if "
                    "explicitly requested via `kind`). For audio "
                    "playback to a speaker use `play_music` instead."
                ),
                parameters=[
                    ToolParameter(
                        name="query",
                        type=ToolParameterType.STRING,
                        description="Title, actor, or other free-text query.",
                    ),
                    ToolParameter(
                        name="kind",
                        type=ToolParameterType.STRING,
                        description=(
                            "Restrict to a kind. Defaults exclude music."
                        ),
                        required=False,
                        enum=[
                            "movie",
                            "show",
                            "episode",
                            "music_album",
                            "music_track",
                        ],
                    ),
                    ToolParameter(
                        name="limit",
                        type=ToolParameterType.INTEGER,
                        description="Max results (capped at 50).",
                        required=False,
                    ),
                ],
                required_role="user",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="play_on",
                slash_group="media",
                slash_command="play",
                slash_help=(
                    "Play a movie/episode on a TV: /media play <title> "
                    "on <client>"
                ),
                description=(
                    "Play **video** content (movies, shows, episodes) on "
                    "a TV/phone client. The `client` parameter is **a "
                    "single client name**, not a list — video plays on "
                    "one screen at a time. For audio playback to a "
                    "speaker use `play_music` instead."
                ),
                parameters=[
                    ToolParameter(
                        name="title",
                        type=ToolParameterType.STRING,
                        description="What to look up.",
                    ),
                    ToolParameter(
                        name="client",
                        type=ToolParameterType.STRING,
                        description=(
                            "Target client name (substring match)."
                        ),
                    ),
                    ToolParameter(
                        name="kind",
                        type=ToolParameterType.STRING,
                        description="Optional kind hint.",
                        required=False,
                        enum=[
                            "movie",
                            "show",
                            "episode",
                            "music_video",
                        ],
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="play_media_id",
                description=(
                    "Play a specific item by `(backend_name, item_id)` — "
                    "use after `search_media` returns a target you can "
                    "address directly. Button-invoked from search / "
                    "recently-added / continue-watching UI blocks."
                ),
                parameters=[
                    ToolParameter(
                        name="backend",
                        type=ToolParameterType.STRING,
                        description="plex | jellyfin",
                    ),
                    ToolParameter(
                        name="item_id",
                        type=ToolParameterType.STRING,
                        description="Backend-specific item id.",
                    ),
                    ToolParameter(
                        name="client",
                        type=ToolParameterType.STRING,
                        description="Target client name.",
                    ),
                    ToolParameter(
                        name="offset_seconds",
                        type=ToolParameterType.NUMBER,
                        description=(
                            "Optional offset. Ignored for button-driven "
                            "plays — the handler re-resolves via "
                            "get_item(backend_user_id=<clicker>)."
                        ),
                        required=False,
                    ),
                ],
                required_role="user",
            ),
        ]

        if self.supports_recently_added:
            tools.append(
                ToolDefinition(
                    name="recently_added",
                    slash_group="media",
                    slash_command="recent",
                    slash_help=(
                        "Recently added: /media recent [kind=movie|show]"
                    ),
                    description=(
                        "Surface recently-added items across configured "
                        "media servers. State-aware Play / Resume / "
                        "Watch again buttons per result."
                    ),
                    parameters=[
                        ToolParameter(
                            name="kind",
                            type=ToolParameterType.STRING,
                            description="Restrict to a kind.",
                            required=False,
                            enum=["movie", "show", "episode"],
                        ),
                        ToolParameter(
                            name="limit",
                            type=ToolParameterType.INTEGER,
                            description="Max results.",
                            required=False,
                        ),
                    ],
                    required_role="everyone",
                    parallel_safe=True,
                )
            )

        if self.supports_continue_watching:
            tools.append(
                ToolDefinition(
                    name="continue_watching",
                    slash_group="media",
                    slash_command="on-deck",
                    slash_help="Show what to resume: /media on-deck",
                    description=(
                        "Per-user 'continue watching' / on-deck list. "
                        "Reads the calling user's mapping. Returns "
                        "Resume-button UI blocks."
                    ),
                    parameters=[],
                    required_role="user",
                    parallel_safe=True,
                )
            )

        if self.supports_now_playing:
            tools.append(
                ToolDefinition(
                    name="now_playing",
                    slash_group="media",
                    slash_command="now",
                    slash_help="What's playing now: /media now [client]",
                    description=(
                        "What's currently playing across configured media "
                        "backends. Live (NOT cached) so 'right now?' is "
                        "always sub-second fresh."
                    ),
                    parameters=[
                        ToolParameter(
                            name="client",
                            type=ToolParameterType.STRING,
                            description="Optional client substring filter.",
                            required=False,
                        ),
                    ],
                    required_role="everyone",
                    parallel_safe=True,
                )
            )

        # playback_control is registered whenever any backend is
        # configured. The `seek` action is gated on supports_seek but
        # the tool itself stays for pause / resume / stop.
        playback_actions = ["pause", "resume", "stop"]
        if self.supports_seek:
            playback_actions.append("seek")
        tools.append(
            ToolDefinition(
                name="playback_control",
                slash_group="media",
                slash_command="pause",
                slash_help=(
                    "Pause / resume / stop / seek active session."
                ),
                description=(
                    "Pause, resume, stop, or seek the active session on "
                    "a media client. Use action=seek with the position "
                    "parameter to jump to a specific point."
                ),
                parameters=[
                    ToolParameter(
                        name="action",
                        type=ToolParameterType.STRING,
                        description="pause | resume | stop | seek",
                        enum=playback_actions,
                    ),
                    ToolParameter(
                        name="client",
                        type=ToolParameterType.STRING,
                        description=(
                            "Optional. Auto-picks the active session if "
                            "exactly one is playing."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="position",
                        type=ToolParameterType.STRING,
                        description=(
                            "Position for action=seek. Accepts '5m', "
                            "'1h22m', '1:22:00' (H:MM:SS), '1:22' "
                            "(M:SS), '3700' (raw seconds)."
                        ),
                        required=False,
                    ),
                ],
                required_role="user",
            )
        )

        if self.supports_recommend_next:
            tools.append(
                ToolDefinition(
                    name="recommend_next",
                    slash_group="media",
                    slash_command="recommend",
                    slash_help="Get a recommendation: /media recommend",
                    description=(
                        "AI-driven recommendation pass over your "
                        "library. Combines continue-watching + "
                        "recently-added + unwatched preferred-genre "
                        "search candidates. Optional `intent` is a "
                        "free-text mood/genre/runtime hint."
                    ),
                    parameters=[
                        ToolParameter(
                            name="kind",
                            type=ToolParameterType.STRING,
                            description="Optional kind filter.",
                            required=False,
                            enum=["movie", "show", "episode"],
                        ),
                        ToolParameter(
                            name="intent",
                            type=ToolParameterType.STRING,
                            description=(
                                "Free-text mood / genre / runtime."
                            ),
                            required=False,
                        ),
                    ],
                    required_role="user",
                )
            )

        # Admin tools
        tools.extend(
            [
                ToolDefinition(
                    name="media_library_link_user",
                    slash_group="media",
                    slash_command="link-user",
                    slash_help=(
                        "Map a Gilbert user to a Plex/Jellyfin account: "
                        "/media link-user alice plex alice_plex"
                    ),
                    description=(
                        "Map a Gilbert user to a Plex/Jellyfin account."
                    ),
                    parameters=[
                        ToolParameter(
                            name="gilbert_user",
                            type=ToolParameterType.STRING,
                            description="Gilbert username or user_id.",
                        ),
                        ToolParameter(
                            name="backend",
                            type=ToolParameterType.STRING,
                            description="plex | jellyfin",
                        ),
                        ToolParameter(
                            name="backend_username",
                            type=ToolParameterType.STRING,
                            description="The user's name on the backend.",
                        ),
                    ],
                    required_role="admin",
                ),
                ToolDefinition(
                    name="media_library_unlink_user",
                    slash_group="media",
                    slash_command="unlink-user",
                    slash_help=(
                        "Remove a Gilbert<->backend user mapping."
                    ),
                    description=(
                        "Remove an existing Gilbert<->backend user mapping."
                    ),
                    parameters=[
                        ToolParameter(
                            name="gilbert_user",
                            type=ToolParameterType.STRING,
                            description="Gilbert username or user_id.",
                        ),
                        ToolParameter(
                            name="backend",
                            type=ToolParameterType.STRING,
                            description="plex | jellyfin",
                        ),
                    ],
                    required_role="admin",
                ),
                ToolDefinition(
                    name="media_library_list_user_mappings",
                    slash_group="media",
                    slash_command="user-mappings",
                    slash_help="List Gilbert<->backend user mappings.",
                    description="List Gilbert<->backend user mappings.",
                    parameters=[],
                    required_role="admin",
                    parallel_safe=True,
                ),
            ]
        )

        return tools

    async def execute_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> str | ToolOutput:
        if name == "list_media_clients":
            return await self._tool_list_media_clients(arguments)
        if name == "search_media":
            return await self._tool_search_media(arguments)
        if name == "play_on":
            return await self._tool_play_on(arguments)
        if name == "play_media_id":
            return await self._tool_play_media_id(arguments)
        if name == "recently_added":
            return await self._tool_recently_added(arguments)
        if name == "continue_watching":
            return await self._tool_continue_watching(arguments)
        if name == "now_playing":
            return await self._tool_now_playing(arguments)
        if name == "playback_control":
            return await self._tool_playback_control(arguments)
        if name == "recommend_next":
            return await self._tool_recommend_next(arguments)
        if name == "media_library_link_user":
            return await self._tool_link_user(arguments)
        if name == "media_library_unlink_user":
            return await self._tool_unlink_user(arguments)
        if name == "media_library_list_user_mappings":
            return await self._tool_list_user_mappings(arguments)
        raise KeyError(f"Unknown tool: {name}")

    # ── Tool implementations ────────────────────────────────────────

    @staticmethod
    def _require_user_id(arguments: dict[str, Any]) -> str | None:
        user_id = arguments.get("_user_id")
        if not user_id or not isinstance(user_id, str):
            return None
        return str(user_id)

    async def _tool_list_media_clients(
        self, arguments: dict[str, Any]
    ) -> str:
        clients = await self.list_clients()
        return json.dumps(
            {"clients": [_media_client_to_dict(c) for c in clients]},
            default=str,
        )

    async def _tool_search_media(
        self, arguments: dict[str, Any]
    ) -> ToolOutput:
        user_id = self._require_user_id(arguments) or ""
        query = str(arguments.get("query") or "").strip()
        if not query:
            return ToolOutput(text=json.dumps({"error": "query is required"}))
        kind_str = str(arguments.get("kind") or "").strip()
        kind: MediaKind | None = None
        if kind_str:
            try:
                kind = MediaKind(kind_str)
            except ValueError:
                kind = None
        try:
            limit = int(arguments.get("limit") or 30)
        except (TypeError, ValueError):
            limit = 30

        # Music kinds default-excluded — caller has to opt in via
        # explicit `kind=music_*`. This is the MusicService seam.
        kinds: tuple[MediaKind, ...]
        if kind is None:
            kinds = (
                MediaKind.MOVIE,
                MediaKind.SHOW,
                MediaKind.EPISODE,
                MediaKind.SEASON,
            )
        else:
            kinds = (kind,)

        filters = MediaSearchFilters(kinds=kinds, limit=limit)
        items = await self.search(
            query, kind=kind, gilbert_user_id=user_id, filters=filters
        )

        text = json.dumps(
            {"results": [_media_item_to_dict(i) for i in items]},
            default=str,
        )
        ui_blocks = [_build_media_result_block(item) for item in items[:10]]
        return ToolOutput(text=text, ui_blocks=ui_blocks)

    async def _tool_play_on(self, arguments: dict[str, Any]) -> ToolOutput:
        user_id = self._require_user_id(arguments)
        if not user_id:
            return ToolOutput(
                text=json.dumps(
                    {"error": "Internal: tool invoked without _user_id"}
                )
            )
        title = str(arguments.get("title") or "").strip()
        client_name = str(arguments.get("client") or "").strip()
        if not title or not client_name:
            return ToolOutput(
                text=json.dumps(
                    {"error": "title and client are required"}
                )
            )
        kind_str = str(arguments.get("kind") or "").strip()
        kind: MediaKind | None = None
        if kind_str:
            try:
                kind = MediaKind(kind_str)
            except ValueError:
                kind = None

        # 1. search
        filters = MediaSearchFilters(
            kinds=(kind,) if kind else (
                MediaKind.MOVIE,
                MediaKind.SHOW,
                MediaKind.EPISODE,
                MediaKind.SEASON,
            ),
            limit=10,
        )
        items = await self.search(
            title, kind=kind, gilbert_user_id=user_id, filters=filters
        )

        if not items:
            return ToolOutput(
                text=json.dumps(
                    {
                        "error": (
                            f"Nothing in your library matches '{title}'"
                        ),
                        "suggestion": (
                            f"/radarr.find {title} — to add it"
                        ),
                    }
                )
            )

        # 2. visual disambiguation if 2+ matches
        if len(items) >= 2:
            cards = items[:_DISAMBIGUATION_MAX_CARDS]
            blocks = [_build_disambiguation_block(item) for item in cards]
            text = json.dumps(
                {
                    "candidates": [
                        _media_item_to_dict(i) for i in cards
                    ],
                    "hint": (
                        f"Multiple library items match '{title}'. "
                        f"Pick one to play on '{client_name}'."
                    ),
                },
                default=str,
            )
            return ToolOutput(text=text, ui_blocks=blocks)

        chosen = items[0]
        return await self._dispatch_play_for_item(
            chosen,
            client_name=client_name,
            user_id=user_id,
            initiator="user",
        )

    async def _tool_play_media_id(
        self, arguments: dict[str, Any]
    ) -> ToolOutput:
        user_id = self._require_user_id(arguments)
        if not user_id:
            return ToolOutput(
                text=json.dumps(
                    {"error": "Internal: tool invoked without _user_id"}
                )
            )
        backend_name = str(arguments.get("backend") or "").strip()
        item_id = str(arguments.get("item_id") or "").strip()
        client_name = str(arguments.get("client") or "").strip()
        if not backend_name or not item_id or not client_name:
            return ToolOutput(
                text=json.dumps(
                    {
                        "error": (
                            "backend, item_id, and client are required"
                        )
                    }
                )
            )
        backend = self._backends.get(backend_name)
        if backend is None:
            return ToolOutput(
                text=json.dumps(
                    {"error": f"Backend '{backend_name}' is not running."}
                )
            )

        # Re-resolve the item via the clicker's mapping so the offset
        # belongs to the clicker, NOT the original searcher (spec §5.1).
        backend_user_id = await self.resolve_backend_user(
            user_id, backend_name
        )
        item = await backend.get_item(
            item_id, backend_user_id=backend_user_id
        )
        if item is None:
            return ToolOutput(
                text=json.dumps(
                    {"error": f"Item {item_id} not found on {backend_name}"}
                )
            )
        return await self._dispatch_play_for_item(
            item,
            client_name=client_name,
            user_id=user_id,
            initiator="user",
        )

    async def _dispatch_play_for_item(
        self,
        item: MediaItem,
        *,
        client_name: str,
        user_id: str,
        initiator: str,
    ) -> ToolOutput:
        # 3. show/season → next-episode resolution
        if item.kind in (MediaKind.SHOW, MediaKind.SEASON):
            if not self.supports_next_episode:
                return ToolOutput(
                    text=json.dumps(
                        {
                            "error": (
                                "Episode resolution unavailable on this "
                                "backend"
                            ),
                            "suggestion": (
                                "Use /media search to pick a specific "
                                "episode"
                            ),
                        }
                    )
                )
            next_ep = await self.next_episode_for(
                item, gilbert_user_id=user_id
            )
            if next_ep is None:
                # Caught up UIBlock
                return _build_caught_up_block(item)
            item = next_ep

        # 4. resolve client
        try:
            client = await self.find_client(
                client_name, gilbert_user_id=user_id
            )
        except MediaClientNotFoundError as exc:
            return ToolOutput(
                text=json.dumps(
                    {
                        "error": str(exc),
                        "available": [
                            _media_client_to_dict(c)
                            for c in await self.list_clients()
                        ],
                    },
                    default=str,
                )
            )
        except MediaClientAmbiguousError as exc:
            return ToolOutput(
                text=json.dumps(
                    {
                        "error": str(exc),
                        "candidates": [
                            _media_client_to_dict(c) for c in exc.candidates
                        ],
                    },
                    default=str,
                ),
                ui_blocks=[
                    _build_client_disambiguation_block(c, item)
                    for c in exc.candidates
                ],
            )

        idempotency_key = f"{client.client_id}:{item.id}"
        offset = item.view_offset_seconds if item.view_offset_seconds > 0 else 0.0
        try:
            outcome = await self.play_item(
                item,
                client,
                offset_seconds=offset,
                gilbert_user_id=user_id,
                initiator=initiator,
                idempotency_key=idempotency_key,
            )
        except MediaLibraryError as exc:
            return ToolOutput(
                text=json.dumps({"error": str(exc)})
            )

        return ToolOutput(
            text=json.dumps(
                {
                    "status": "playing"
                    if outcome != "deduped"
                    else "deduped",
                    "title": item.title,
                    "client": client.name,
                    "backend": client.backend_name,
                    "offset_seconds": offset,
                    "resumed": offset > 0,
                    "resolved_episode": (
                        {
                            "title": item.title,
                            "season_number": item.season_number,
                            "episode_number": item.episode_number,
                        }
                        if item.kind == MediaKind.EPISODE
                        else None
                    ),
                },
                default=str,
            )
        )

    async def _tool_recently_added(
        self, arguments: dict[str, Any]
    ) -> ToolOutput:
        kind_str = str(arguments.get("kind") or "").strip()
        kind: MediaKind | None = None
        if kind_str:
            try:
                kind = MediaKind(kind_str)
            except ValueError:
                kind = None
        try:
            limit = int(arguments.get("limit") or 10)
        except (TypeError, ValueError):
            limit = 10
        entries = await self.recently_added(kind=kind, limit=limit)
        text = json.dumps(
            {
                "entries": [
                    {
                        "item": _media_item_to_dict(e.item),
                        "added_at": e.added_at,
                    }
                    for e in entries
                ]
            },
            default=str,
        )
        ui_blocks = [_build_media_result_block(e.item) for e in entries]
        return ToolOutput(text=text, ui_blocks=ui_blocks)

    async def _tool_continue_watching(
        self, arguments: dict[str, Any]
    ) -> ToolOutput:
        user_id = self._require_user_id(arguments)
        if not user_id:
            return ToolOutput(
                text=json.dumps(
                    {"error": "Internal: tool invoked without _user_id"}
                )
            )
        try:
            limit = int(arguments.get("limit") or 10)
        except (TypeError, ValueError):
            limit = 10
        result = await self.continue_watching(
            gilbert_user_id=user_id, limit=limit
        )
        entries = result.get("entries", [])
        unmapped = result.get("unmapped_backends", [])
        out: dict[str, Any] = {
            "entries": [
                {
                    "item": _media_item_to_dict(e.item),
                    "next_up": e.next_up,
                }
                for e in entries
            ],
            "unmapped_backends": unmapped,
        }
        if "error" in result:
            out["error"] = result["error"]
        if "hint" in result:
            out["hint"] = result["hint"]
        ui_blocks = [
            _build_media_result_block(e.item) for e in entries
        ]
        return ToolOutput(
            text=json.dumps(out, default=str), ui_blocks=ui_blocks
        )

    async def _tool_now_playing(
        self, arguments: dict[str, Any]
    ) -> str:
        client_name = str(arguments.get("client") or "").strip() or None
        sessions = await self.now_playing(client_name=client_name)
        return json.dumps(
            {"sessions": [_media_session_to_dict(s) for s in sessions]},
            default=str,
        )

    async def _tool_playback_control(
        self, arguments: dict[str, Any]
    ) -> str:
        user_id = self._require_user_id(arguments)
        if not user_id:
            return json.dumps(
                {"error": "Internal: tool invoked without _user_id"}
            )
        action = str(arguments.get("action") or "").strip()
        client_name = str(arguments.get("client") or "").strip()
        position_str = str(arguments.get("position") or "").strip()

        if action not in ("pause", "resume", "stop", "seek"):
            return json.dumps({"error": f"Unknown action '{action}'"})

        # Resolve client.
        if client_name:
            try:
                client = await self.find_client(
                    client_name, gilbert_user_id=user_id
                )
            except MediaClientNotFoundError as exc:
                return json.dumps(
                    {
                        "error": str(exc),
                        "available_clients": [
                            _media_client_to_dict(c)
                            for c in await self.list_clients()
                        ],
                    },
                    default=str,
                )
            except MediaClientAmbiguousError as exc:
                return json.dumps(
                    {
                        "error": str(exc),
                        "candidates": [
                            _media_client_to_dict(c)
                            for c in exc.candidates
                        ],
                    },
                    default=str,
                )
        else:
            sessions = await self.now_playing()
            if len(sessions) == 1:
                client = sessions[0].client
            elif not sessions:
                return json.dumps({"error": "No active sessions to control"})
            else:
                return json.dumps(
                    {
                        "error": "Multiple active sessions — specify client",
                        "candidates": [
                            _media_client_to_dict(s.client) for s in sessions
                        ],
                    },
                    default=str,
                )

        try:
            if action == "pause":
                await self.pause_client(client)
            elif action == "resume":
                await self.resume_client(client)
            elif action == "stop":
                await self.stop_client(client)
            elif action == "seek":
                if not position_str:
                    return json.dumps(
                        {"error": "position is required for action=seek"}
                    )
                try:
                    position = parse_position(position_str)
                except ValueError as exc:
                    return json.dumps(
                        {"error": f"could not parse position: {exc}"}
                    )
                await self.seek_client(client, position)
        except MediaLibraryError as exc:
            return json.dumps({"error": str(exc)})

        result: dict[str, Any] = {
            "status": f"{action}ed" if action != "stop" else "stopped",
            "client": client.name,
            "backend": client.backend_name,
        }
        if action == "seek":
            result["position_seconds"] = parse_position(position_str)
        return json.dumps(result, default=str)

    async def _tool_recommend_next(
        self, arguments: dict[str, Any]
    ) -> ToolOutput:
        user_id = self._require_user_id(arguments)
        if not user_id:
            return ToolOutput(
                text=json.dumps(
                    {"error": "Internal: tool invoked without _user_id"}
                )
            )
        if self._resolver is None:
            return ToolOutput(
                text=json.dumps({"error": "AI not available"})
            )
        ai_svc = self._resolver.get_capability("ai_chat")
        if not isinstance(ai_svc, AISamplingProvider):
            return ToolOutput(
                text=json.dumps({"error": "AI not available"})
            )

        intent = str(arguments.get("intent") or "").strip()

        # Parallel candidate gathering with overall budget (15s).
        # Per-source caps from spec §7.2 (5 / 10 / 15 = 30 default).
        async def _continue_watching() -> list[MediaItem]:
            r = await self.continue_watching(
                gilbert_user_id=user_id, limit=5
            )
            return [e.item for e in r.get("entries", [])][:5]

        async def _recently() -> list[MediaItem]:
            entries = await self.recently_added(limit=10)
            return [e.item for e in entries][:10]

        async def _genre_search() -> list[MediaItem]:
            if not self._preferred_genres:
                return []
            genre = self._preferred_genres[0]
            filters = MediaSearchFilters(
                kinds=(MediaKind.MOVIE, MediaKind.SHOW),
                genre=genre,
                unwatched_only=True,
                limit=15,
            )
            items = await self.search(
                genre, gilbert_user_id=user_id, filters=filters
            )
            return items[:15]

        try:
            cw, recent, genre = await asyncio.wait_for(
                asyncio.gather(
                    _continue_watching(),
                    _recently(),
                    _genre_search(),
                    return_exceptions=True,
                ),
                timeout=15.0,
            )
        except TimeoutError:
            return ToolOutput(
                text=json.dumps(
                    {"error": "recommend_next candidate gather timed out"}
                )
            )

        cw_items = cw if isinstance(cw, list) else []
        recent_items = recent if isinstance(recent, list) else []
        genre_items = genre if isinstance(genre, list) else []

        # Dedup + cap
        seen: set[tuple[str, str]] = set()
        candidates: list[MediaItem] = []
        for source in (cw_items, recent_items, genre_items):
            for item in source:
                key = (item.backend_name, item.id)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(item)
                if len(candidates) >= self._recommend_next_max_candidates:
                    break
            if len(candidates) >= self._recommend_next_max_candidates:
                break

        if not candidates:
            return ToolOutput(
                text=json.dumps(
                    {
                        "error": (
                            "No candidates for recommendation — try "
                            "/media search or /media recent first."
                        )
                    }
                )
            )

        # Build prompt input — truncate summary to 200 chars.
        candidate_payload = [
            {
                "id": c.id,
                "backend": c.backend_name,
                "title": c.title,
                "year": c.year,
                "kind": c.kind.value,
                "genres": list(c.genres),
                "summary": (c.summary[:200] + "…")
                if len(c.summary) > 200
                else c.summary,
                "is_watched": c.is_watched,
                "duration_seconds": c.duration_seconds,
            }
            for c in candidates
        ]
        history_payload = [
            {
                "id": c.id,
                "title": c.title,
                "year": c.year,
                "kind": c.kind.value,
            }
            for c in cw_items[:5]
        ]
        message_text = (
            f"<user_intent>{intent}</user_intent>\n"
            f"<candidates>{json.dumps(candidate_payload)}</candidates>\n"
            f"<recent_history>{json.dumps(history_payload)}</recent_history>"
        )

        try:
            resp = await ai_svc.complete_one_shot(
                messages=[
                    Message(role=MessageRole.USER, content=message_text)
                ],
                system_prompt=self._recommend_next_prompt,
                profile_name=self._ai_profile,
            )
        except Exception as exc:
            logger.warning("recommend_next AI call failed: %s", exc)
            # Graceful degradation — first 3 of continue_watching.
            picks = candidates[:3]
            blocks = [_build_media_result_block(item) for item in picks]
            return ToolOutput(
                text=json.dumps(
                    {
                        "fallback": True,
                        "results": [
                            _media_item_to_dict(i) for i in picks
                        ],
                    },
                    default=str,
                ),
                ui_blocks=blocks,
            )

        response_text = resp.message.content if resp.message else ""
        try:
            parsed = json.loads(response_text or "[]")
        except json.JSONDecodeError:
            parsed = []
        chosen: list[MediaItem] = []
        if isinstance(parsed, list):
            for entry in parsed:
                if not isinstance(entry, dict):
                    continue
                pick_id = str(entry.get("id", ""))
                for c in candidates:
                    if c.id == pick_id:
                        chosen.append(c)
                        break
                if len(chosen) >= 3:
                    break
        if not chosen:
            chosen = candidates[:3]

        ui_blocks = [_build_media_result_block(item) for item in chosen]
        text = json.dumps(
            {
                "results": [
                    {
                        "item": _media_item_to_dict(c),
                    }
                    for c in chosen
                ]
            },
            default=str,
        )
        return ToolOutput(text=text, ui_blocks=ui_blocks)

    async def _resolve_gilbert_user_for_admin(
        self, gilbert_user: str
    ) -> tuple[str, str] | tuple[None, str]:
        """Resolve a Gilbert username/user_id and return ``(id, name)``
        or ``(None, available_list_str)``.
        """
        if self._resolver is None:
            return (None, "users service unavailable")
        users_svc = self._resolver.get_capability("users")
        if users_svc is None:
            return (None, "users service unavailable")
        list_users = getattr(users_svc, "list_users", None)
        if list_users is None:
            return (None, "users service unavailable")
        users = await list_users()
        for u in users:
            if u.get("_id") == gilbert_user:
                return (gilbert_user, str(u.get("display_name") or u.get("email") or gilbert_user))
        for u in users:
            if u.get("display_name") == gilbert_user or u.get("email") == gilbert_user:
                return (str(u["_id"]), str(u.get("display_name") or u.get("email") or gilbert_user))
        names = [
            str(u.get("display_name") or u.get("email") or u.get("_id"))
            for u in users
        ]
        return (None, ", ".join(names))

    async def _tool_link_user(self, arguments: dict[str, Any]) -> str:
        gilbert_user_arg = str(arguments.get("gilbert_user") or "").strip()
        backend_name = str(arguments.get("backend") or "").strip()
        backend_username = str(arguments.get("backend_username") or "").strip()
        if not gilbert_user_arg or not backend_name or not backend_username:
            return json.dumps(
                {
                    "error": (
                        "gilbert_user, backend, and backend_username "
                        "are required"
                    )
                }
            )
        if backend_name not in self._backends:
            return json.dumps(
                {"error": f"Backend '{backend_name}' is not running."}
            )

        gid, name_or_avail = await self._resolve_gilbert_user_for_admin(
            gilbert_user_arg
        )
        if gid is None:
            return json.dumps(
                {
                    "error": (
                        f"No Gilbert user named '{gilbert_user_arg}' or "
                        f"with id '{gilbert_user_arg}'"
                    ),
                    "available": name_or_avail,
                }
            )

        backend = self._backends[backend_name]
        try:
            users = await backend.list_backend_users()
        except MediaLibraryError as exc:
            return json.dumps({"error": str(exc)})
        backend_user_id = ""
        for u in users:
            if (
                u.get("username") == backend_username
                or u.get("display_name") == backend_username
                or u.get("id") == backend_username
            ):
                backend_user_id = str(u.get("id"))
                break
        if not backend_user_id:
            return json.dumps(
                {
                    "error": (
                        f"No backend user '{backend_username}' on "
                        f"'{backend_name}'"
                    ),
                    "available": [
                        str(u.get("username") or u.get("display_name"))
                        for u in users
                    ],
                }
            )
        try:
            await self.set_user_mapping(
                gid,
                backend_name,
                backend_user_id,
                backend_username=backend_username,
            )
        except Exception as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps(
            {
                "status": "linked",
                "gilbert_user_id": gid,
                "backend": backend_name,
                "backend_user_id": backend_user_id,
                "backend_username": backend_username,
            }
        )

    async def _tool_unlink_user(self, arguments: dict[str, Any]) -> str:
        gilbert_user_arg = str(arguments.get("gilbert_user") or "").strip()
        backend_name = str(arguments.get("backend") or "").strip()
        if not gilbert_user_arg or not backend_name:
            return json.dumps(
                {"error": "gilbert_user and backend are required"}
            )
        gid, name_or_avail = await self._resolve_gilbert_user_for_admin(
            gilbert_user_arg
        )
        if gid is None:
            return json.dumps(
                {
                    "error": (
                        f"No Gilbert user named '{gilbert_user_arg}'"
                    ),
                    "available": name_or_avail,
                }
            )
        removed = await self.unlink_user_mapping(gid, backend_name)
        return json.dumps(
            {
                "status": "unlinked" if removed else "no_mapping",
                "gilbert_user_id": gid,
                "backend": backend_name,
            }
        )

    async def _tool_list_user_mappings(
        self, arguments: dict[str, Any]
    ) -> str:
        rows = await self.list_user_mappings()
        return json.dumps({"mappings": rows})


# ── Module helpers ──────────────────────────────────────────────────


def _as_float(value: object) -> float:
    """Coerce a value retrieved from JSON-shaped storage to ``float``.

    Storage rows are typed ``dict[str, Any]``; mypy strict mode rejects
    bare ``float(value)`` on an ``object``. Returns ``0.0`` for any
    value that can't be converted.
    """
    if value is None:
        return 0.0
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _now_dt() -> Any:
    """Return a naive-local ``datetime`` for SchedulerProvider start_at."""
    from datetime import datetime
    return datetime.now()


def _seconds(s: float) -> Any:
    from datetime import timedelta
    return timedelta(seconds=max(0.0, s))


def _round_robin_merge(
    per_backend: dict[str, list[MediaItem]], limit: int
) -> list[MediaItem]:
    """Merge per-backend search results by stable round-robin.

    Backends are iterated in ``backend_name`` ascending order; each
    cycle takes the next item from each backend's already-ranked list.
    Trim the merged list to ``limit`` after merge.
    """
    if not per_backend:
        return []
    ordered = sorted(per_backend.keys())
    merged: list[MediaItem] = []
    idx = 0
    while True:
        added_this_round = False
        for name in ordered:
            items = per_backend[name]
            if idx < len(items):
                merged.append(items[idx])
                added_this_round = True
                if len(merged) >= limit:
                    return merged
        if not added_this_round:
            return merged
        idx += 1


def _build_media_result_block(item: MediaItem) -> UIBlock:
    """Search / recently-added / continue-watching result card."""
    label_lines = [f"**{item.title}**"]
    if item.year:
        label_lines.append(f"{item.year} · {item.kind.value.title()}")
    elif item.kind:
        label_lines.append(item.kind.value.title())
    if item.summary:
        snippet = item.summary[:140]
        if len(item.summary) > 140:
            snippet += "…"
        label_lines.append(snippet)
    label_text = "\n".join(label_lines)

    elements: list[UIElement] = []
    if item.poster_url:
        elements.append(
            UIElement(
                type="image",
                name="poster",
                url=item.poster_url,
                label=item.title,
                max_width=120,
            )
        )
    elements.append(UIElement(type="label", name="info", label=label_text))
    elements.append(
        UIElement(
            type="text",
            name="backend",
            label="",
            default=item.backend_name,
        )
    )
    elements.append(
        UIElement(
            type="text",
            name="item_id",
            label="",
            default=item.id,
        )
    )
    elements.append(
        UIElement(
            type="buttons",
            name="client",
            options=[
                UIOption(
                    value="",
                    label=_button_label_for_item(item),
                ),
            ],
        )
    )
    return UIBlock(
        title=item.title,
        elements=elements,
        submit_label=_button_label_for_item(item),
        tool_name="play_media_id",
    )


def _build_disambiguation_block(item: MediaItem) -> UIBlock:
    """Visual disambiguation card for play_on with multiple matches."""
    return _build_media_result_block(item)


def _build_client_disambiguation_block(
    client: MediaClient, item: MediaItem
) -> UIBlock:
    """Picker block when find_client raises MediaClientAmbiguousError."""
    label_text = f"**{client.name}**\n{client.device or 'unknown device'}"
    elements: list[UIElement] = [
        UIElement(type="label", name="info", label=label_text),
        UIElement(
            type="text",
            name="backend",
            label="",
            default=item.backend_name,
        ),
        UIElement(
            type="text",
            name="item_id",
            label="",
            default=item.id,
        ),
        UIElement(
            type="buttons",
            name="client",
            options=[UIOption(value=client.name, label="Play here")],
        ),
    ]
    return UIBlock(
        title=client.name,
        elements=elements,
        submit_label="Play here",
        tool_name="play_media_id",
    )


def _build_caught_up_block(show_item: MediaItem) -> ToolOutput:
    """The 'you're caught up' UIBlock from spec §7.1."""
    summary = (
        f"You're caught up on {show_item.title} — every episode watched."
    )
    elements: list[UIElement] = [
        UIElement(type="label", name="info", label=summary),
        UIElement(
            type="buttons",
            name="action",
            options=[
                UIOption(value="restart", label="Restart from S1E1"),
                UIOption(value="upcoming", label="Show what's coming next"),
                UIOption(value="cancel", label="Cancel"),
            ],
        ),
    ]
    block = UIBlock(
        title=f"Caught up on {show_item.title}",
        elements=elements,
        submit_label="Restart",
        tool_name="play_media_id",
    )
    return ToolOutput(
        text=json.dumps(
            {
                "status": "caught_up",
                "show": show_item.title,
            }
        ),
        ui_blocks=[block],
    )


__all__ = [
    "MediaLibraryService",
    "parse_position",
]
