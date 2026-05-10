"""Feeds interfaces — feed model, backend ABC, capability protocol, auth helpers.

Shared by the core ``FeedsService`` / ``FeedBriefingService``, the web
layer, and plugins that provide feed backends. Imports only from other
``interfaces`` modules — never from ``core/``, ``integrations/``,
``web/``, or ``storage/``.

Closest analog: ``interfaces/inbox.py``. The structural shape (Feed +
FeedItem dataclasses, ``can_access_feed`` / ``can_admin_feed`` helpers,
``FeedsProvider`` capability protocol, error taxonomy, backend ABC with
universal registry pattern) is a deliberate copy of the inbox model —
swap "mailbox" for "feed" and "message" for "item".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, cast, runtime_checkable

from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import ConfigParam

# ── Errors ───────────────────────────────────────────────────────────


class FeedError(Exception):
    """Raised by ``FeedBackend`` operations and service-level code.

    Single error type so callers can catch ``FeedError`` and surface a
    user-legible message regardless of whether the failure was a
    network timeout, malformed XML, oversized response body, or a
    rejected URL. Individual sites raise more specific subclasses
    where the additional shape matters.
    """


class FeedAuthError(FeedError):
    """Raised when a backend rejects credentials (e.g. HTTP 401/403)."""


class FeedNotFoundError(FeedError):
    """Raised when a feed URL returns 404 or its body is empty."""


class FeedTooLargeError(FeedError):
    """Raised when a feed body exceeds ``max_response_bytes``."""


# ── Dataclasses ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class FeedItem:
    """A single item parsed out of a feed.

    Returned by ``FeedBackend.poll`` (one per item in the feed body).
    Concrete backends MUST be deterministic about ``item_uid``: the
    same item must always produce the same uid, or dedup breaks.

    Storage shape (``feed_items`` collection) layers ``score``,
    ``ai_summary``, ``read``, ``briefed_at``, etc. on top of this —
    those are service-owned, not backend-owned.
    """

    item_uid: str
    title: str
    link: str
    summary: str = ""
    author: str = ""
    published_at: datetime | None = None
    updated_at: datetime | None = None
    enclosure_url: str = ""
    enclosure_mime: str = ""
    raw_metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class FeedMeta:
    """Metadata about the feed itself, returned by ``FeedBackend.probe``.

    Used at subscribe time to validate the URL and pre-fill
    ``Feed.name`` if the caller didn't supply one.
    """

    title: str
    description: str = ""
    link: str = ""
    language: str = ""
    icon_url: str = ""


@dataclass(frozen=True)
class PollResult:
    """What ``FeedBackend.poll`` returns."""

    items: list[FeedItem]
    http_cache: dict[str, str] = field(default_factory=dict)
    suggested_min_interval_sec: int = 0
    """Hint from ``<ttl>`` and/or ``Cache-Control: max-age`` (whichever
    is larger). The service stores it on
    ``Feed.suggested_poll_interval_sec`` and uses
    ``max(feed.poll_interval_sec, suggested)`` as the effective
    cadence. ``0`` means "no hint."
    """

    not_modified: bool = False
    """``True`` when the source returned 304 Not Modified — ``items``
    will be empty. The service treats this as success: bumps
    ``last_polled_at``, does NOT bump ``consecutive_failures``."""

    status_code: int = 0
    """HTTP status code (or 0 for non-HTTP backends)."""


@dataclass
class Feed:
    """A subscription / feed source. One row per subscribed feed.

    The owner is set at creation time and never changes automatically;
    sharing is granted separately via ``shared_with_users`` /
    ``shared_with_roles``.

    ``http_cache`` is **backend bookkeeping** (etag, last_modified,
    etc.) and is **distinct from ``backend_config``** (user-supplied
    settings). Backends MUST NOT touch ``backend_config`` — a UI save
    would clobber bookkeeping if they did.
    """

    id: str = ""
    name: str = ""
    url: str = ""
    backend_name: str = "rss_atom"
    backend_config: dict[str, Any] = field(default_factory=dict)
    owner_user_id: str = ""
    shared_with_users: list[str] = field(default_factory=list)
    shared_with_roles: list[str] = field(default_factory=list)
    poll_enabled: bool = True
    poll_interval_sec: int = 1800
    category: str = ""
    importance_weight: float = 0.5
    ingest_to_knowledge: bool = False
    briefing_eligible: bool = True
    last_polled_at: str = ""
    last_error: str = ""
    consecutive_failures: int = 0
    last_poll_status_code: int = 0
    last_poll_items_total: int = 0
    last_poll_items_new: int = 0
    last_poll_duration_ms: int = 0
    http_cache: dict[str, str] = field(default_factory=dict)
    suggested_poll_interval_sec: int = 0
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "backend_name": self.backend_name,
            "backend_config": dict(self.backend_config),
            "owner_user_id": self.owner_user_id,
            "shared_with_users": list(self.shared_with_users),
            "shared_with_roles": list(self.shared_with_roles),
            "poll_enabled": self.poll_enabled,
            "poll_interval_sec": self.poll_interval_sec,
            "category": self.category,
            "importance_weight": self.importance_weight,
            "ingest_to_knowledge": self.ingest_to_knowledge,
            "briefing_eligible": self.briefing_eligible,
            "last_polled_at": self.last_polled_at,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "last_poll_status_code": self.last_poll_status_code,
            "last_poll_items_total": self.last_poll_items_total,
            "last_poll_items_new": self.last_poll_items_new,
            "last_poll_duration_ms": self.last_poll_duration_ms,
            "http_cache": dict(self.http_cache),
            "suggested_poll_interval_sec": self.suggested_poll_interval_sec,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Feed:
        return cls(
            id=str(data.get("id") or data.get("_id") or ""),
            name=str(data.get("name", "")),
            url=str(data.get("url", "")),
            backend_name=str(data.get("backend_name", "rss_atom") or "rss_atom"),
            backend_config=cast("dict[str, Any]", data.get("backend_config") or {}),
            owner_user_id=str(data.get("owner_user_id", "")),
            shared_with_users=cast("list[str]", data.get("shared_with_users") or []),
            shared_with_roles=cast("list[str]", data.get("shared_with_roles") or []),
            poll_enabled=bool(data.get("poll_enabled", True)),
            poll_interval_sec=int(data.get("poll_interval_sec", 1800) or 1800),
            category=str(data.get("category", "")),
            importance_weight=float(data.get("importance_weight", 0.5) or 0.0),
            ingest_to_knowledge=bool(data.get("ingest_to_knowledge", False)),
            briefing_eligible=bool(data.get("briefing_eligible", True)),
            last_polled_at=str(data.get("last_polled_at", "")),
            last_error=str(data.get("last_error", "")),
            consecutive_failures=int(data.get("consecutive_failures", 0) or 0),
            last_poll_status_code=int(data.get("last_poll_status_code", 0) or 0),
            last_poll_items_total=int(data.get("last_poll_items_total", 0) or 0),
            last_poll_items_new=int(data.get("last_poll_items_new", 0) or 0),
            last_poll_duration_ms=int(data.get("last_poll_duration_ms", 0) or 0),
            http_cache=cast("dict[str, str]", data.get("http_cache") or {}),
            suggested_poll_interval_sec=int(
                data.get("suggested_poll_interval_sec", 0) or 0
            ),
            created_at=str(data.get("created_at", "")),
        )

    def effective_poll_interval_sec(self) -> int:
        """Effective cadence — source-suggested cadence wins if larger.

        Hammering a feed every 30 minutes when it advertises
        ``ttl=720`` is rude and gets us rate-limited; respecting the
        suggestion is just polite and avoids the back-off path.
        """
        return max(int(self.poll_interval_sec), int(self.suggested_poll_interval_sec))


@dataclass(frozen=True)
class StoredFeedItem:
    """One ``feed_items`` row hydrated for read paths.

    The poll-time ``FeedItem`` is the backend's view; this is the
    service-level view that includes scoring, summarization,
    briefing-state, and ingestion-state.
    """

    id: str
    feed_id: str
    item_uid: str
    title: str
    link: str
    summary: str = ""
    author: str = ""
    published_at: str = ""
    updated_at: str = ""
    received_at: str = ""
    ai_summary: str = ""
    score: float = -1.0
    score_reason: str = ""
    read: bool = False
    briefed_at: str = ""
    ingested_to_knowledge: bool = False
    enclosure_url: str = ""
    enclosure_mime: str = ""
    lazy_score: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "_id": self.id,
            "feed_id": self.feed_id,
            "item_uid": self.item_uid,
            "title": self.title,
            "link": self.link,
            "summary": self.summary,
            "author": self.author,
            "published_at": self.published_at,
            "updated_at": self.updated_at,
            "received_at": self.received_at,
            "ai_summary": self.ai_summary,
            "score": self.score,
            "score_reason": self.score_reason,
            "read": self.read,
            "briefed_at": self.briefed_at,
            "ingested_to_knowledge": self.ingested_to_knowledge,
            "enclosure_url": self.enclosure_url,
            "enclosure_mime": self.enclosure_mime,
            "lazy_score": self.lazy_score,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StoredFeedItem:
        return cls(
            id=str(data.get("_id") or data.get("id") or ""),
            feed_id=str(data.get("feed_id", "")),
            item_uid=str(data.get("item_uid", "")),
            title=str(data.get("title", "")),
            link=str(data.get("link", "")),
            summary=str(data.get("summary", "")),
            author=str(data.get("author", "")),
            published_at=str(data.get("published_at", "")),
            updated_at=str(data.get("updated_at", "")),
            received_at=str(data.get("received_at", "")),
            ai_summary=str(data.get("ai_summary", "")),
            score=float(data.get("score", -1.0)),
            score_reason=str(data.get("score_reason", "")),
            read=bool(data.get("read", False)),
            briefed_at=str(data.get("briefed_at", "")),
            ingested_to_knowledge=bool(data.get("ingested_to_knowledge", False)),
            enclosure_url=str(data.get("enclosure_url", "")),
            enclosure_mime=str(data.get("enclosure_mime", "")),
            lazy_score=bool(data.get("lazy_score", False)),
        )


@dataclass(frozen=True)
class BriefingHeadline:
    """One entry in a ``BriefingResult.headlines`` list."""

    item_id: str
    title: str
    one_liner: str
    score: float
    link: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "title": self.title,
            "one_liner": self.one_liner,
            "score": self.score,
            "link": self.link,
        }


@dataclass(frozen=True)
class BriefingResult:
    """Two-artifact briefing output: spoken paragraph + headlines.

    ``spoken`` is the ~60-second flowing paragraph TTS reads; the
    chat / dashboard renders ``headlines`` as a clickable list. A
    single AI call produces both — the prompt instructs the model to
    return JSON with both fields.
    """

    spoken: str
    headlines: list[BriefingHeadline]
    item_ids: list[str]
    since: datetime
    briefing_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "spoken": self.spoken,
            "headlines": [h.to_dict() for h in self.headlines],
            "item_ids": list(self.item_ids),
            "since": self.since.isoformat(),
            "briefing_id": self.briefing_id,
        }


# ── Authorization ────────────────────────────────────────────────────


class FeedAccess(StrEnum):
    """How a user came to have access to a feed — used for UI grouping."""

    OWNER = "owner"
    ADMIN = "admin"
    SHARED_USER = "shared_user"
    SHARED_ROLE = "shared_role"


def can_access_feed(
    user_ctx: UserContext,
    feed: Feed,
    *,
    is_admin: bool = False,
) -> bool:
    """Can this user read items / use the feed in tools?

    Admin, owner, any user in ``shared_with_users``, or any user with a
    role in ``shared_with_roles`` has full access. "Full access" =
    read + briefing-eligibility + tool visibility — but NOT settings or
    share edits (those are gated by ``can_admin_feed``).
    """
    if is_admin:
        return True
    if user_ctx.user_id == feed.owner_user_id:
        return True
    if user_ctx.user_id in feed.shared_with_users:
        return True
    return bool(user_ctx.roles & set(feed.shared_with_roles))


def can_admin_feed(
    user_ctx: UserContext,
    feed: Feed,
    *,
    is_admin: bool = False,
) -> bool:
    """Can this user edit settings, change shares, or unsubscribe?

    Only the owner or a system admin. Shared users — even with full
    access — cannot change configuration or reassign sharing.
    """
    if is_admin:
        return True
    return user_ctx.user_id == feed.owner_user_id


def determine_feed_access(
    user_ctx: UserContext,
    feed: Feed,
    *,
    is_admin: bool = False,
) -> FeedAccess | None:
    """Return how the user has access to this feed, or ``None`` if none.

    Precedence: owner > admin > shared_user > shared_role. Owner beats
    admin because owner is the more durable relationship — an admin
    who's also the owner should see "owner" in the UI.
    """
    if user_ctx.user_id == feed.owner_user_id:
        return FeedAccess.OWNER
    if is_admin:
        return FeedAccess.ADMIN
    if user_ctx.user_id in feed.shared_with_users:
        return FeedAccess.SHARED_USER
    if user_ctx.roles & set(feed.shared_with_roles):
        return FeedAccess.SHARED_ROLE
    return None


# ── FeedBackend ABC ──────────────────────────────────────────────────


class FeedBackend(ABC):
    """Abstract feed backend — fetches one source.

    The backend is consulted only during ``probe`` (subscribe-time
    metadata fetch) and ``poll`` (periodic item listing). All reads
    after that go through entity storage — backends never touch
    persisted item rows.

    Concrete backends MUST be deterministic about ``item_uid``: the
    same item must always produce the same uid, or dedup breaks. The
    universal registry pattern (``__init_subclass__`` + ``backend_name``
    class attribute + ``backend_config_params()``) lets services
    discover backends after a side-effect import — see
    ``memory-backend-pattern.md``.
    """

    _registry: dict[str, type[FeedBackend]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            FeedBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type[FeedBackend]]:
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        """Backend-specific config (basic auth, custom headers, etc.)."""
        return []

    @abstractmethod
    async def initialize(self, config: dict[str, Any] | None = None) -> None:
        """Set up HTTP client, auth, etc. Called once per runtime."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Release resources."""
        ...

    @abstractmethod
    async def probe(self, url: str) -> FeedMeta:
        """Fetch the feed once and return its metadata.

        Used at subscription time to validate the URL and pre-fill
        ``Feed.name``. Raises ``FeedError`` on any failure —
        ``FeedsService.subscribe()`` catches and surfaces.
        """
        ...

    @abstractmethod
    async def poll(
        self,
        url: str,
        *,
        since: datetime | None = None,
        max_items: int = 100,
        http_cache: dict[str, str] | None = None,
    ) -> PollResult:
        """Fetch the feed and return its current items.

        ``since`` is advisory — the service does the actual dedup
        against entity storage regardless of what the backend
        returns.

        ``http_cache`` is the per-feed bookkeeping dict the service
        round-trips: backends consult ``http_cache.get("etag")`` /
        ``http_cache.get("last_modified")`` to issue conditional GETs
        and return the updated values via ``PollResult.http_cache``.
        Backends MUST NOT touch ``Feed.backend_config`` — that's
        user-supplied settings and a UI save would clobber bookkeeping.
        """
        ...


# ── Capability protocol ──────────────────────────────────────────────


@runtime_checkable
class FeedsProvider(Protocol):
    """What plugins / other services consume from ``FeedsService``.

    Resolved via ``resolver.get_capability("feeds")`` and
    ``isinstance``-checked against this protocol — never against the
    concrete ``FeedsService`` class.

    ``build_briefing`` lives on this single protocol (rather than on a
    separate ``BriefingProvider``) because the only callers are
    ``GreetingService`` and ``FeedBriefingService``, the implementation
    reads from feed storage and uses prompt config that already lives
    on ``FeedsService``, and a second protocol for one method on the
    same service the caller already resolved by name was
    overengineering. See spec §5 / Round 2 architect notes.
    """

    async def subscribe(
        self,
        url: str,
        user_ctx: UserContext,
        *,
        name: str = "",
        category: str = "",
        backend_name: str = "rss_atom",
        poll_interval_sec: int = 1800,
    ) -> Feed: ...

    async def unsubscribe(self, feed_id: str, user_ctx: UserContext) -> None: ...

    async def list_accessible_feeds(self, user_ctx: UserContext) -> list[Feed]: ...

    async def get_feed(self, feed_id: str) -> Feed | None: ...

    async def search_items(
        self,
        *,
        feed_id: str | None = None,
        query: str = "",
        unread_only: bool = False,
        min_score: float = 0.0,
        category: str = "",
        limit: int = 50,
        page: int = 1,
        user_ctx: UserContext | None = None,
    ) -> list[StoredFeedItem]: ...

    async def get_top_items(
        self,
        user_ctx: UserContext,
        *,
        category: str = "",
        since: datetime | None = None,
        limit: int = 5,
    ) -> list[StoredFeedItem]: ...

    async def mark_read(
        self,
        item_id: str,
        user_ctx: UserContext,
        read: bool = True,
    ) -> None: ...

    async def build_briefing(
        self,
        user_ctx: UserContext,
        *,
        top_n: int = 5,
        since: datetime | None = None,
        category: str = "",
        max_spoken_seconds: int = 0,
        mark_briefed: bool = True,
        anti_repetition_context: list[str] | None = None,
    ) -> BriefingResult: ...


@runtime_checkable
class CachedFeedLister(Protocol):
    """Protocol for anything that can report the currently-cached feeds.

    Used by ``ConfigurationService._resolve_dynamic_choices`` to
    populate ``feeds`` dropdowns on settings pages without duck-typing
    the service instance.
    """

    @property
    def cached_feeds(self) -> list[Feed]:
        """Return the last-known feed list from the service cache."""
        ...


__all__ = [
    "BriefingHeadline",
    "BriefingResult",
    "CachedFeedLister",
    "Feed",
    "FeedAccess",
    "FeedAuthError",
    "FeedBackend",
    "FeedError",
    "FeedItem",
    "FeedMeta",
    "FeedNotFoundError",
    "FeedTooLargeError",
    "FeedsProvider",
    "PollResult",
    "StoredFeedItem",
    "can_access_feed",
    "can_admin_feed",
    "determine_feed_access",
]

