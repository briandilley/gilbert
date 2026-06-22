"""Feeds service — multi-feed RSS / Atom polling, scoring, ingestion, briefing.

Per-user feed subscriptions, polled on a configurable cadence, deduped,
scored against a configurable AI prompt, optionally ingested into the
knowledge service for vector search, and ready to power the daily
briefing. Closest analog: ``InboxService``. The runtime registry,
scheduler integration, sharing model, and event taxonomy are deliberate
copies of inbox with the email-specific bits replaced by feed-specific
bits.

Top-level pieces:

- ``_runtimes: dict[feed_id, _FeedRuntime]`` — one ``FeedBackend`` +
  one ``feeds-poll-{feed_id}`` job per ``poll_enabled`` feed.
- Async ``_score_queue`` + ``_ingest_queue`` decouple slow AI calls
  and slow article fetches from the poll loop. Bounded workers, a
  service-wide poll semaphore, cold-start jitter, and graceful
  give-up at 20 consecutive failures keep the system from melting
  into either the AI provider or the source feed.
- ``build_briefing`` is the AI-driven daily briefing builder. The
  prompt is configurable; the call is ``tools_override=[]`` so the
  model can never accidentally fan out to other tools (the
  ``Sonos audio-clip-loop`` recursion bug from
  ``memory-ai-context-profiles.md``).
- The synthetic ``feed_articles`` ``DocumentBackend`` is owned
  PRIVATELY here — never registered with ``KnowledgeService``.
  Knowledge ingestion calls ``KnowledgeProvider.index_document``
  directly with that instance.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import random
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx

from gilbert.core.services._ui_blocks import confirm_or_execute
from gilbert.interfaces.ai import AISamplingProvider, Message, MessageRole
from gilbert.interfaces.auth import AccessControlProvider, UserContext
from gilbert.interfaces.configuration import (
    ConfigParam,
)
from gilbert.interfaces.context import get_current_user
from gilbert.interfaces.events import Event, EventBus, EventBusProvider
from gilbert.interfaces.feeds import (
    BriefingHeadline,
    BriefingResult,
    Feed,
    FeedBackend,
    FeedError,
    FeedItem,
    FeedsProvider,
    PollResult,
    StoredFeedItem,
    can_access_feed,
    can_admin_feed,
    determine_feed_access,
)
from gilbert.interfaces.knowledge import (
    DocumentMeta,
    DocumentType,
    KnowledgeProvider,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import (
    Filter,
    FilterOp,
    IndexDefinition,
    Query,
    SortField,
    StorageProvider,
)
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)
from gilbert.interfaces.ui import ToolOutput, UIBlock, UIElement

logger = logging.getLogger(__name__)

_FEEDS_COLLECTION = "feeds"
_FEED_ITEMS_COLLECTION = "feed_items"
_BRIEFINGS_COLLECTION = "feed_briefings"
_BRIEFING_STATE_COLLECTION = "feed_briefing_state"
_INGEST_DAILY_COLLECTION = "feed_ingest_daily"

_RESCORE_TICK_INTERVAL_SEC = 30 * 60  # 30 minutes
_RETENTION_TICK_INTERVAL_SEC = 24 * 60 * 60  # 1 day
_LAZY_SCORE_TICK_INTERVAL_SEC = 24 * 60 * 60  # 1 day — daily backlog drain

_DEFAULT_SCORING_PROMPT = (
    "You are a news triage assistant. Score each item for importance to "
    "the user (0.0 = ignore, 1.0 = critical). Consider relevance, novelty, "
    "and impact. The user's stated interests follow the item. Respond with "
    "JSON ONLY — no prose, no commentary, no markdown fences:\n"
    '{"score": 0.0-1.0, "reason": "one short sentence"}\n'
    "Output JSON only — wrap in no other text."
)
_DEFAULT_SUMMARIZATION_PROMPT = (
    "Summarize the following news item in 2-3 plain sentences. Capture the "
    "facts (who, what, where, when, why). No editorializing, no headlines, "
    "no bullet points. Plain prose only."
)
_DEFAULT_BRIEFING_PROMPT = (
    "You are Gilbert, generating the user's daily news briefing. Produce "
    "a SINGLE flowing paragraph suitable for TTS — no bullets, no numbered "
    "lists, no headers. Mention items by name with brief context. Vary your "
    "tone across days (witty, warm, dramatic, deadpan, poetic, nerdy) — "
    "the user has heard yesterday's briefing, be different today. Recent "
    "briefings (avoid repeating their phrasing) are listed in the user "
    "message. Keep the spoken text under 200 words.\n\n"
    "Respond with JSON ONLY — no prose outside the JSON, no markdown fences:\n"
    '{"spoken": "...", "headlines": [{"item_id": "...", "title": "...", '
    '"one_liner": "...", "score": 0.0-1.0}, ...]}\n'
    "Use the item_ids exactly as supplied. Order headlines by importance."
)
_DEFAULT_KNOWLEDGE_RECOMMENDATION_PROMPT = (
    "You are advising a user about which RSS feeds to ingest into their "
    "knowledge base for vector search. For each feed, decide whether the "
    "ingest_to_knowledge flag should be ENABLED or DISABLED. Consider: "
    "how often the user reads or marks items from this feed, average score "
    "of recent items, the user's stated interests, and whether the feed "
    "produces deep-content articles vs. headline links. Respond with "
    "JSON ONLY — no markdown fences:\n"
    '{"recommendations": [{"feed_id": "...", "recommendation": "enable"|'
    '"disable", "rationale": "one short sentence"}, ...]}'
)

# Minimal, deliberately-conservative paywall heuristic. Real heuristics
# go in a v2 plugin; this just keeps the ~4-line stub from reaching the
# vector index where it'd surface for "how do I subscribe to <site>".
_PAYWALL_RE = re.compile(
    r"(subscribe to read|create a free account|sign in to continue"
    r"|already a subscriber|to read this article|to continue reading)",
    re.IGNORECASE,
)

# Tolerant JSON-fence stripper (per spec §8.1 — saves ~5% of cheap-
# profile failures). Matches a single leading ```json\n / ```\n and a
# single trailing \n```.
_JSON_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n(.*?)\n```\s*$",
    re.DOTALL,
)


def _strip_json_fences(text: str) -> str:
    if not text:
        return text
    match = _JSON_FENCE_RE.match(text.strip())
    if match:
        return match.group(1)
    return text


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _ms(t0: float) -> int:
    return max(0, int((time.perf_counter() - t0) * 1000))


def _safe_uid(item_uid: str) -> str:
    """Filesystem-safe form of an arbitrary item_uid for the cache path."""
    return hashlib.sha1(item_uid.encode("utf-8")).hexdigest()


# Multi-part public suffixes we recognise without pulling in the full
# tldextract / publicsuffix2 dependency. Covers the most common cases
# the spec §9 cheap-eTLD+1 check has historically gotten wrong
# (`bbc.co.uk` → `co.uk` reducing to a same-suffix match). Long form
# wins by being scanned first.
_KNOWN_MULTIPART_SUFFIXES: tuple[str, ...] = (
    "co.uk",
    "com.au",
    "net.au",
    "org.au",
    "co.nz",
    "co.jp",
    "ne.jp",
    "or.jp",
    "ac.uk",
    "gov.uk",
    "ac.jp",
    "co.in",
    "co.za",
    "com.br",
    "co.kr",
    "com.mx",
    "com.tr",
    "com.cn",
    "com.hk",
)


def _registrable_suffix(host: str) -> str:
    """Return the eTLD+1 form of ``host`` (best-effort).

    Recognises common multi-part suffixes from `_KNOWN_MULTIPART_SUFFIXES`
    so `bbc.co.uk` reduces to `bbc.co.uk` (not `co.uk`); falls back to
    the last-two-labels heuristic otherwise. This is *advisory* — the
    SSRF guard's load-bearing layer is the always-block private/
    metadata host check, not this suffix comparison.
    """
    host = host.lower().strip(".")
    if not host:
        return ""
    parts = host.split(".")
    for suffix in _KNOWN_MULTIPART_SUFFIXES:
        if host.endswith("." + suffix) and len(parts) >= len(suffix.split(".")) + 1:
            n = len(suffix.split(".")) + 1
            return ".".join(parts[-n:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _is_private_or_metadata_host(host: str) -> bool:
    """True when ``host`` resolves to a private / loopback / link-local /
    cloud-metadata address (IPv4 + IPv6). String-only — no DNS lookup.

    Blocks:
        - IPv4 RFC1918 (10/8, 172.16/12, 192.168/16) + loopback (127/8)
        - IPv4 link-local (169.254/16 — AWS / GCE metadata service)
        - IPv4 broadcast / current-network (0/8, 255.255.255.255)
        - IPv4 CGNAT (100.64/10)
        - IPv4 multicast (224/4)
        - IPv6 loopback (::1), link-local (fe80::/10), ULA (fc00::/7),
          multicast (ff00::/8), unspecified (::)
        - Hostname literals: ``localhost``, ``ip6-localhost``
        - The empty / wildcard host
    """
    import ipaddress

    h = host.strip("[]")
    if not h or h in {"localhost", "ip6-localhost", "ip6-loopback"}:
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        # Not an IP literal — only literal-IP / known-hostname checks
        # apply here. (DNS-rebinding-style attacks would need the actual
        # resolver result; ``httpx`` will follow DNS so we'd add a hook
        # there for paranoid deployments.)
        return False
    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return True
    if ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return True
    if isinstance(ip, ipaddress.IPv4Address):
        # CGNAT 100.64.0.0/10 — not flagged as ``is_private`` until 3.13.
        if ipaddress.IPv4Address("100.64.0.0") <= ip <= ipaddress.IPv4Address("100.127.255.255"):
            return True
    return False


def _doc_id_for(source_id: str, feed_id: str, item_uid: str) -> str:
    """Stable ``KnowledgeProvider`` document_id for a feed item.

    MUST match the path used when indexing the article via
    ``KnowledgeProvider.index_document`` so cascade deletion (unsubscribe
    + retention) actually finds the document. The indexed path is
    ``"<feed_id>/<safe_uid>.html"`` — keep the ``.html`` suffix in lockstep
    or knowledge entries leak forever.
    """
    return f"{source_id}:{feed_id}/{_safe_uid(item_uid)}.html"


def _sanitize_user_text(value: str) -> str:
    """Sanitize free-form user text before concatenating into a prompt.

    Strip leading/trailing whitespace and neutralize backticks
    (mitigates prompt-injection via ``user_interests`` config). The
    field still goes in as USER content, never into the SYSTEM prompt.
    """
    if not value:
        return ""
    cleaned = value.strip().replace("`", "'")
    return cleaned


@dataclass
class _FeedRuntime:
    """In-memory per-feed state: backend instance + scheduler job name."""

    feed: Feed
    backend: FeedBackend
    poll_job_name: str = ""


class FeedsPermissionError(PermissionError):
    """Raised when a caller lacks access to a feed or feed item."""


class FeedNotFoundError(LookupError):
    """Raised when a feed_id does not resolve."""


class FeedsService(Service):
    """Multi-feed RSS / Atom service.

    Capabilities: ``feeds``, ``ai_tools``, ``ws_handlers``.
    Owns three AI prompts (scoring, summarization, briefing), all
    configurable via ``ConfigParam(ai_prompt=True)``. Briefing builder
    lives here (not on ``FeedBriefingService``) because the owning
    service runs the AI call and the prompt belongs where the call is.
    """

    def __init__(self) -> None:
        # Service-lifetime handles.
        self._storage: Any = None
        self._event_bus: EventBus | None = None
        self._scheduler: Any = None
        self._knowledge: KnowledgeProvider | None = None
        self._access_control: AccessControlProvider | None = None
        self._resolver: ServiceResolver | None = None

        # Runtime registry (keyed by feed_id, fine).
        self._runtimes: dict[str, _FeedRuntime] = {}
        self._cached_feeds: list[Feed] = []
        self._poll_locks: dict[str, asyncio.Lock] = {}

        # Service-wide config.
        self._enabled: bool = False
        self._max_items_per_poll: int = 100
        self._max_summary_length: int = 4000
        self._default_poll_interval_sec: int = 1800
        self._summarize_on_ingest: bool = False
        self._score_on_ingest: bool = True
        self._initial_score_cap: int = 50
        self._max_concurrent_polls: int = 8
        self._max_concurrent_scoring: int = 4
        self._max_concurrent_ingestion: int = 2
        self._max_first_poll_jitter_sec: int = 30
        self._retention_days: int = 90
        self._ingest_max_items_per_day_per_user: int = 200
        self._respect_robots_txt: bool = True

        # AI profiles + prompts (cached for hot path).
        self._scoring_ai_profile: str = "light"
        self._summarization_ai_profile: str = "light"
        self._briefing_ai_profile: str = "medium"
        self._scoring_prompt: str = _DEFAULT_SCORING_PROMPT
        self._summarization_prompt: str = _DEFAULT_SUMMARIZATION_PROMPT
        self._briefing_prompt: str = _DEFAULT_BRIEFING_PROMPT
        self._knowledge_recommendation_prompt: str = (
            _DEFAULT_KNOWLEDGE_RECOMMENDATION_PROMPT
        )
        self._user_interests: str = ""

        # Async pipelines. Queues hold tuples of (Feed, item) where
        # ``item`` may be either a fresh ``FeedItem`` from the poll
        # path or a re-enqueued ``StoredFeedItem`` from the rescore
        # tick — typed as the ABC parent so both fit.
        self._poll_semaphore: asyncio.Semaphore | None = None
        self._score_queue: (
            asyncio.Queue[tuple[Feed, FeedItem | StoredFeedItem]] | None
        ) = None
        self._ingest_queue: (
            asyncio.Queue[tuple[Feed, FeedItem | StoredFeedItem]] | None
        ) = None
        self._score_workers: list[asyncio.Task[None]] = []
        self._ingest_workers: list[asyncio.Task[None]] = []
        self._score_drops_total: int = 0
        self._lazy_score_total: int = 0
        self._ingest_total: dict[str, int] = {}

        # Service-wide first-sync cap (§6.4e). Tracked as a remaining
        # budget rather than a counter so a config-reload that bumps
        # ``initial_score_cap`` upward immediately enlarges the budget.
        # Recomputed on ``start()`` and on every ``initial_score_cap``
        # config change. Items beyond the cap are persisted with
        # ``score=-1.0`` and ``lazy_score=True``; the rescore tick (and
        # a dedicated lazy-score tick) drains them later.
        self._initial_score_remaining: int = 50

        # Synthetic ``feed_articles`` backend — owned privately, never
        # registered with KnowledgeService. Lazy-imported in ``start``
        # so unit tests of unrelated services don't import it.
        self._feed_doc_backend: Any = None

        # Article-fetch HTTP client (owned by the service, not the
        # per-feed backend, so we can fan out body fetches across many
        # feeds with one connection pool).
        self._http_client: httpx.AsyncClient | None = None

        # robots.txt cache — instance-level so a service restart drops
        # it cleanly and tests don't share state across runs.
        self._robots_cache: dict[str, tuple[float, Any]] = {}

    @property
    def cached_feeds(self) -> list[Feed]:
        """Sync snapshot — used by config dynamic choices."""
        return list(self._cached_feeds)

    # ── Service metadata ─────────────────────────────────────────────

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="feeds",
            capabilities=frozenset({"feeds", "ai_tools", "ws_handlers"}),
            requires=frozenset({"entity_storage", "scheduler"}),
            optional=frozenset(
                {
                    "event_bus",
                    "knowledge",
                    "configuration",
                    "access_control",
                    "ai_chat",
                }
            ),
            events=frozenset(
                {
                    "feed.item.received",
                    "feed.item.scored",
                    "feed.subscription.created",
                    "feed.subscription.updated",
                    "feed.subscription.deleted",
                    "feed.subscription.shares.changed",
                    "feed.subscription.disabled",
                    "feed.ingest.throttled",
                }
            ),
            ai_calls=frozenset({"feed_score", "feed_summarize", "feed_briefing"}),
            toggleable=True,
            toggle_description="RSS / news feed polling, scoring, and briefing builder",
        )

    # ── Configurable protocol ────────────────────────────────────────

    @property
    def config_namespace(self) -> str:
        return "feeds"

    @property
    def config_category(self) -> str:
        return "News & Information"

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="enabled",
                type=ToolParameterType.BOOLEAN,
                description="Master switch for the feeds service.",
                default=True,
            ),
            ConfigParam(
                key="max_items_per_poll",
                type=ToolParameterType.INTEGER,
                description="Maximum items pulled per poll fire.",
                default=100,
            ),
            ConfigParam(
                key="max_summary_length",
                type=ToolParameterType.INTEGER,
                description="Truncate stored summary text at this length.",
                default=4000,
            ),
            ConfigParam(
                key="default_poll_interval_sec",
                type=ToolParameterType.INTEGER,
                description="Default cadence for new subscriptions (seconds).",
                default=1800,
            ),
            ConfigParam(
                key="summarize_on_ingest",
                type=ToolParameterType.BOOLEAN,
                description="If True, run the summarization AI call on every new item.",
                default=False,
            ),
            ConfigParam(
                key="score_on_ingest",
                type=ToolParameterType.BOOLEAN,
                description=(
                    "If True (default), scoring runs as part of poll fan-out. "
                    "If False, scoring runs lazily when a tool reads items."
                ),
                default=True,
            ),
            ConfigParam(
                key="initial_score_cap",
                type=ToolParameterType.INTEGER,
                description=(
                    "Maximum scoring calls fanned out from the first-sync "
                    "boot. Items beyond the cap stay at score=-1.0 until "
                    "lazy-scored on read."
                ),
                default=50,
            ),
            ConfigParam(
                key="max_concurrent_polls",
                type=ToolParameterType.INTEGER,
                description="Service-wide poll-fan-out cap.",
                default=8,
            ),
            ConfigParam(
                key="max_concurrent_scoring",
                type=ToolParameterType.INTEGER,
                description="Bound on parallel AI scoring workers.",
                default=4,
            ),
            ConfigParam(
                key="max_concurrent_ingestion",
                type=ToolParameterType.INTEGER,
                description="Bound on parallel knowledge body fetches.",
                default=2,
            ),
            ConfigParam(
                key="max_first_poll_jitter_sec",
                type=ToolParameterType.INTEGER,
                description="First-poll stagger window (seconds) for new subscriptions.",
                default=30,
            ),
            ConfigParam(
                key="retention_days",
                type=ToolParameterType.INTEGER,
                description=(
                    "Hard-delete feed items older than this. 0 = keep forever."
                ),
                default=90,
            ),
            ConfigParam(
                key="ingest_max_items_per_day_per_user",
                type=ToolParameterType.INTEGER,
                description=(
                    "Per-user daily cap on knowledge ingestion. Items above "
                    "the cap emit feed.ingest.throttled and are skipped."
                ),
                default=200,
            ),
            ConfigParam(
                key="respect_robots_txt",
                type=ToolParameterType.BOOLEAN,
                description="Honor robots.txt for article body fetches.",
                default=True,
            ),
            ConfigParam(
                key="scoring_ai_profile",
                type=ToolParameterType.STRING,
                description="AI profile for the scoring call.",
                default="light",
                choices_from="ai_profiles",
            ),
            ConfigParam(
                key="summarization_ai_profile",
                type=ToolParameterType.STRING,
                description="AI profile for the summarization call.",
                default="light",
                choices_from="ai_profiles",
            ),
            ConfigParam(
                key="briefing_ai_profile",
                type=ToolParameterType.STRING,
                description=(
                    "AI profile for the briefing call. Higher than light "
                    "by default — a coherent, varied 200-word paragraph "
                    "is harder than a JSON classification."
                ),
                default="medium",
                choices_from="ai_profiles",
            ),
            ConfigParam(
                key="user_interests",
                type=ToolParameterType.STRING,
                description=(
                    "Free-form description of what the user cares about. "
                    "Concatenated into the scoring USER message (NOT the "
                    "system prompt). Sanitized before use to mitigate "
                    "prompt injection."
                ),
                default="",
                multiline=True,
            ),
            ConfigParam(
                key="scoring_prompt",
                type=ToolParameterType.STRING,
                description=(
                    "System prompt for the importance-scoring AI call. "
                    "Output JSON only — wrap in no other text. Leave blank "
                    "to use the bundled default."
                ),
                default=_DEFAULT_SCORING_PROMPT,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="summarization_prompt",
                type=ToolParameterType.STRING,
                description=(
                    "System prompt for the per-item summarization AI call. "
                    "Leave blank to use the bundled default."
                ),
                default=_DEFAULT_SUMMARIZATION_PROMPT,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="briefing_prompt",
                type=ToolParameterType.STRING,
                description=(
                    "System prompt for the daily briefing AI call. The "
                    "model returns JSON with a flowing-paragraph 'spoken' "
                    "field plus a 'headlines' list. Leave blank to use "
                    "the bundled default."
                ),
                default=_DEFAULT_BRIEFING_PROMPT,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="knowledge_recommendation_prompt",
                type=ToolParameterType.STRING,
                description=(
                    "System prompt for the recommend_knowledge_ingestion "
                    "tool. Leave blank to use the bundled default."
                ),
                default=_DEFAULT_KNOWLEDGE_RECOMMENDATION_PROMPT,
                multiline=True,
                ai_prompt=True,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._enabled = bool(config.get("enabled", False))
        self._max_items_per_poll = int(
            config.get("max_items_per_poll", self._max_items_per_poll) or 100
        )
        self._max_summary_length = int(
            config.get("max_summary_length", self._max_summary_length) or 4000
        )
        self._default_poll_interval_sec = int(
            config.get("default_poll_interval_sec", self._default_poll_interval_sec)
            or 1800
        )
        self._summarize_on_ingest = bool(
            config.get("summarize_on_ingest", self._summarize_on_ingest)
        )
        self._score_on_ingest = bool(
            config.get("score_on_ingest", self._score_on_ingest)
        )
        new_cap = int(
            config.get("initial_score_cap", self._initial_score_cap) or 50
        )
        # Bump the live budget if the cap rose; never silently shrink
        # below what's already consumed.
        if new_cap > self._initial_score_cap:
            self._initial_score_remaining += new_cap - self._initial_score_cap
        self._initial_score_cap = new_cap
        self._max_concurrent_polls = max(
            1, int(config.get("max_concurrent_polls", self._max_concurrent_polls) or 8)
        )
        self._max_concurrent_scoring = max(
            1,
            int(
                config.get("max_concurrent_scoring", self._max_concurrent_scoring) or 4
            ),
        )
        self._max_concurrent_ingestion = max(
            1,
            int(
                config.get("max_concurrent_ingestion", self._max_concurrent_ingestion)
                or 2
            ),
        )
        self._max_first_poll_jitter_sec = max(
            0,
            int(
                config.get(
                    "max_first_poll_jitter_sec", self._max_first_poll_jitter_sec
                )
                or 0
            ),
        )
        self._retention_days = max(
            0, int(config.get("retention_days", self._retention_days) or 0)
        )
        self._ingest_max_items_per_day_per_user = max(
            0,
            int(
                config.get(
                    "ingest_max_items_per_day_per_user",
                    self._ingest_max_items_per_day_per_user,
                )
                or 0
            ),
        )
        self._respect_robots_txt = bool(
            config.get("respect_robots_txt", self._respect_robots_txt)
        )
        self._scoring_ai_profile = (
            str(config.get("scoring_ai_profile", "") or "")
            or self._scoring_ai_profile
        )
        self._summarization_ai_profile = (
            str(config.get("summarization_ai_profile", "") or "")
            or self._summarization_ai_profile
        )
        self._briefing_ai_profile = (
            str(config.get("briefing_ai_profile", "") or "")
            or self._briefing_ai_profile
        )
        self._user_interests = str(config.get("user_interests", "") or "")
        self._scoring_prompt = (
            str(config.get("scoring_prompt", "") or "") or _DEFAULT_SCORING_PROMPT
        )
        self._summarization_prompt = (
            str(config.get("summarization_prompt", "") or "")
            or _DEFAULT_SUMMARIZATION_PROMPT
        )
        self._briefing_prompt = (
            str(config.get("briefing_prompt", "") or "")
            or _DEFAULT_BRIEFING_PROMPT
        )
        self._knowledge_recommendation_prompt = (
            str(config.get("knowledge_recommendation_prompt", "") or "")
            or _DEFAULT_KNOWLEDGE_RECOMMENDATION_PROMPT
        )

    # ── Service lifecycle ────────────────────────────────────────────

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver

        # Side-effect import so the registry knows about the built-in
        # backend without a direct concrete import.
        try:
            import gilbert.integrations.rss_feeds  # noqa: F401
        except ImportError:
            logger.warning("feedparser not installed — RSS feeds disabled")

        # Required: storage.
        storage_svc = resolver.require_capability("entity_storage")
        if not isinstance(storage_svc, StorageProvider):
            raise TypeError("entity_storage capability does not provide StorageProvider")
        self._storage = storage_svc.backend

        await self._ensure_indexes()

        # Optional capabilities.
        event_bus_svc = resolver.get_capability("event_bus")
        if isinstance(event_bus_svc, EventBusProvider):
            self._event_bus = event_bus_svc.bus

        knowledge_svc = resolver.get_capability("knowledge")
        if isinstance(knowledge_svc, KnowledgeProvider):
            self._knowledge = knowledge_svc

        acl_svc = resolver.get_capability("access_control")
        if isinstance(acl_svc, AccessControlProvider):
            self._access_control = acl_svc

        # Load global config.
        config_svc = resolver.get_capability("configuration")
        section: dict[str, Any] = {}
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                section = config_svc.get_section_safe(self.config_namespace)

        await self.on_config_changed(section)
        # Fresh start = fresh first-sync budget.
        self._initial_score_remaining = max(0, self._initial_score_cap)

        if not self._enabled:
            logger.info("Feeds service disabled via configuration")
            return

        # Required: scheduler.
        from gilbert.interfaces.scheduler import Schedule, SchedulerProvider

        scheduler_svc = resolver.require_capability("scheduler")
        if not isinstance(scheduler_svc, SchedulerProvider):
            raise TypeError("scheduler capability does not provide SchedulerProvider")
        self._scheduler = scheduler_svc

        # Initialize the synthetic feed_articles document backend.
        from gilbert.integrations.feed_documents import FeedDocumentBackend

        self._feed_doc_backend = FeedDocumentBackend()
        await self._feed_doc_backend.initialize({})

        # Article-fetch HTTP client (used by ingestion).
        self._http_client = httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=False,
        )

        # Async pipelines.
        self._poll_semaphore = asyncio.Semaphore(self._max_concurrent_polls)
        self._score_queue = asyncio.Queue(maxsize=10000)
        self._ingest_queue = asyncio.Queue(maxsize=10000)
        # Workers are lazy: we spawn them once we know we have at
        # least one runtime to feed them. Calling spawn here is safe
        # because asyncio.Queue can be drained from multiple workers
        # even if the queue is never used.
        self._spawn_score_workers()
        self._spawn_ingest_workers()

        # Boot the per-feed runtimes asynchronously so backend
        # initialize() (HTTP client setup) doesn't block start().
        self._scheduler.add_job(
            name="feeds-boot",
            schedule=Schedule.once_after(0),
            callback=self._boot_runtimes,
            system=True,
        )

        # Re-score sweep — picks up items left at score=-1.0 within
        # the last 24h (boot cap, AI failure, queue-full drop).
        self._scheduler.add_job(
            name="feeds-rescore-tick",
            schedule=Schedule.every(_RESCORE_TICK_INTERVAL_SEC),
            callback=self._rescore_tick,
            system=True,
        )

        # Retention sweep — daily.
        self._scheduler.add_job(
            name="feeds-retention-tick",
            schedule=Schedule.every(_RETENTION_TICK_INTERVAL_SEC),
            callback=self._retention_tick,
            system=True,
        )

        # Lazy-score backlog drain — daily.
        self._scheduler.add_job(
            name="feeds-lazy-score-tick",
            schedule=Schedule.every(_LAZY_SCORE_TICK_INTERVAL_SEC),
            callback=self._lazy_score_tick,
            system=True,
        )

        logger.info(
            "Feeds service started (boot deferred, rescore tick %ds, retention tick %ds, lazy-score tick %ds)",
            _RESCORE_TICK_INTERVAL_SEC,
            _RETENTION_TICK_INTERVAL_SEC,
            _LAZY_SCORE_TICK_INTERVAL_SEC,
        )

    async def stop(self) -> None:
        # Cancel scheduled jobs.
        if self._scheduler is not None:
            for name in (
                "feeds-boot",
                "feeds-rescore-tick",
                "feeds-retention-tick",
                "feeds-lazy-score-tick",
            ):
                with contextlib.suppress(Exception):
                    self._scheduler.remove_job(name)
            for runtime in list(self._runtimes.values()):
                if runtime.poll_job_name:
                    with contextlib.suppress(Exception):
                        self._scheduler.remove_job(runtime.poll_job_name)

        # Drain the score queue, then cancel workers.
        if self._score_queue is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._score_queue.join(), timeout=10.0)
        for task in self._score_workers:
            task.cancel()
        await asyncio.gather(*self._score_workers, return_exceptions=True)
        self._score_workers.clear()

        if self._ingest_queue is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._ingest_queue.join(), timeout=10.0)
        for task in self._ingest_workers:
            task.cancel()
        await asyncio.gather(*self._ingest_workers, return_exceptions=True)
        self._ingest_workers.clear()

        # Close every backend instance.
        for runtime in list(self._runtimes.values()):
            try:
                await runtime.backend.close()
            except Exception:
                logger.exception("Error closing backend for feed %s", runtime.feed.id)
        self._runtimes.clear()

        if self._http_client is not None:
            with contextlib.suppress(Exception):
                await self._http_client.aclose()
            self._http_client = None

        logger.info("Feeds service stopped")

    async def _ensure_indexes(self) -> None:
        for idx in (
            IndexDefinition(collection=_FEEDS_COLLECTION, fields=["owner_user_id"]),
            IndexDefinition(collection=_FEEDS_COLLECTION, fields=["poll_enabled"]),
            IndexDefinition(
                collection=_FEED_ITEMS_COLLECTION,
                fields=["feed_id", "received_at"],
            ),
            IndexDefinition(
                collection=_FEED_ITEMS_COLLECTION,
                fields=["feed_id", "item_uid"],
            ),
            IndexDefinition(collection=_FEED_ITEMS_COLLECTION, fields=["read"]),
            IndexDefinition(collection=_FEED_ITEMS_COLLECTION, fields=["score"]),
            IndexDefinition(
                collection=_FEED_ITEMS_COLLECTION, fields=["briefed_at"]
            ),
        ):
            with contextlib.suppress(Exception):
                await self._storage.ensure_index(idx)

    # ── Runtime registry ─────────────────────────────────────────────

    async def _load_feeds(self) -> list[Feed]:
        rows = await self._storage.query(Query(collection=_FEEDS_COLLECTION))
        return [Feed.from_dict(row) for row in rows]

    async def _refresh_cache(self) -> None:
        try:
            self._cached_feeds = await self._load_feeds()
        except Exception:
            logger.exception("Feeds: failed to refresh feed cache")

    async def _boot_runtimes(self) -> None:
        """One-shot: spin up a runtime per ``poll_enabled`` feed.

        The lazy first-sync AI cap (``initial_score_cap``) is
        enforced by counting items enqueued for scoring as the polls
        complete — items beyond the cap are persisted with
        ``lazy_score=True`` and ``score=-1.0`` so a tool that needs
        them can score them on demand.
        """
        try:
            feeds = await self._load_feeds()
        except Exception:
            logger.exception("Feeds boot: failed to load feeds")
            return

        self._cached_feeds = list(feeds)
        for feed in feeds:
            if feed.poll_enabled:
                try:
                    await self._start_runtime(feed)
                except Exception:
                    logger.exception(
                        "Feeds boot: failed to start runtime for %s",
                        feed.id,
                    )
        logger.info("Feeds boot: %d runtime(s) started", len(self._runtimes))

    async def _start_runtime(self, feed: Feed) -> None:
        assert self._scheduler is not None
        backends = FeedBackend.registered_backends()
        backend_cls = backends.get(feed.backend_name)
        if backend_cls is None:
            raise ValueError(f"Unknown feed backend: {feed.backend_name}")
        backend = backend_cls()
        await backend.initialize(dict(feed.backend_config))

        from gilbert.interfaces.scheduler import Schedule

        # Cold-start jitter — bounded by both the configured
        # max_first_poll_jitter_sec and the feed's own poll interval
        # (no point waiting longer than the next scheduled fire).
        jitter_window = float(
            min(
                max(0, self._max_first_poll_jitter_sec),
                feed.poll_interval_sec,
            )
        )
        jitter = random.uniform(0, jitter_window) if jitter_window > 0 else 0.0
        first_fire = datetime.now() + timedelta(seconds=jitter)
        poll_job_name = f"feeds-poll-{feed.id}"
        callback = self._make_poll_callback(feed.id)
        self._scheduler.add_job(
            name=poll_job_name,
            schedule=Schedule.every(
                feed.effective_poll_interval_sec(),
                start_at=first_fire,
            ),
            callback=callback,
            system=True,
        )

        self._runtimes[feed.id] = _FeedRuntime(
            feed=feed,
            backend=backend,
            poll_job_name=poll_job_name,
        )
        logger.info(
            "Feed runtime started: id=%s backend=%s poll=%ds jitter=%.1fs",
            feed.id,
            feed.backend_name,
            feed.effective_poll_interval_sec(),
            jitter,
        )

    async def _stop_runtime(self, feed_id: str) -> None:
        runtime = self._runtimes.pop(feed_id, None)
        if runtime is None:
            return
        if self._scheduler is not None and runtime.poll_job_name:
            with contextlib.suppress(Exception):
                self._scheduler.remove_job(runtime.poll_job_name)
        try:
            await runtime.backend.close()
        except Exception:
            logger.exception("Error closing feed backend %s", feed_id)

    async def _restart_runtime(self, feed: Feed) -> None:
        await self._stop_runtime(feed.id)
        if feed.poll_enabled:
            await self._start_runtime(feed)

    def _make_poll_callback(self, feed_id: str) -> Any:
        async def _cb() -> None:
            runtime = self._runtimes.get(feed_id)
            if runtime is None:
                return
            await self._poll_runtime(runtime)

        return _cb

    # ── Authorization helpers ────────────────────────────────────────

    def _is_admin(self, user_ctx: UserContext) -> bool:
        if user_ctx.user_id == UserContext.SYSTEM.user_id:
            return True
        if self._access_control is not None:
            return self._access_control.get_effective_level(user_ctx) <= 0
        return "admin" in user_ctx.roles

    def _require_access(self, feed: Feed, user_ctx: UserContext) -> None:
        if not can_access_feed(
            user_ctx, feed, is_admin=self._is_admin(user_ctx)
        ):
            raise FeedsPermissionError(
                f"User {user_ctx.user_id!r} cannot access feed {feed.id!r}"
            )

    def _require_admin(self, feed: Feed, user_ctx: UserContext) -> None:
        if not can_admin_feed(
            user_ctx, feed, is_admin=self._is_admin(user_ctx)
        ):
            raise FeedsPermissionError(
                f"User {user_ctx.user_id!r} cannot administer feed {feed.id!r}"
            )

    async def _require_feed(self, feed_id: str) -> Feed:
        row = await self._storage.get(_FEEDS_COLLECTION, feed_id)
        if row is None:
            raise FeedNotFoundError(f"Feed not found: {feed_id}")
        return Feed.from_dict(row)

    # ── Subscribe / unsubscribe / mutation ───────────────────────────

    async def subscribe(
        self,
        url: str,
        user_ctx: UserContext,
        *,
        name: str = "",
        category: str = "",
        backend_name: str = "rss_atom",
        poll_interval_sec: int = 0,
    ) -> Feed:
        backends = FeedBackend.registered_backends()
        backend_cls = backends.get(backend_name)
        if backend_cls is None:
            raise FeedError(f"Unknown feed backend: {backend_name}")

        # Idempotency (§S5): if this user already owns a subscription
        # to the same URL, return it instead of minting a duplicate
        # row + duplicate runtime + duplicate scheduler job. OPML
        # re-imports rely on this — a 30-feed OPML re-imported should
        # be a no-op, not a 60-feed footgun.
        existing_rows = await self._storage.query(
            Query(
                collection=_FEEDS_COLLECTION,
                filters=[
                    Filter(field="owner_user_id", op=FilterOp.EQ, value=user_ctx.user_id),
                    Filter(field="url", op=FilterOp.EQ, value=url),
                ],
            )
        )
        if existing_rows:
            return Feed.from_dict(existing_rows[0])

        # Probe the URL — backend constructed transiently for the probe.
        probe_backend = backend_cls()
        await probe_backend.initialize({})
        try:
            meta = await probe_backend.probe(url)
        finally:
            await probe_backend.close()

        feed = Feed(
            id=f"feed_{uuid.uuid4().hex[:12]}",
            name=name or meta.title or url,
            url=url,
            backend_name=backend_name,
            owner_user_id=user_ctx.user_id,
            category=category,
            poll_interval_sec=int(poll_interval_sec or self._default_poll_interval_sec),
            created_at=_now_utc().isoformat(),
        )
        await self._storage.put(_FEEDS_COLLECTION, feed.id, feed.to_dict())
        await self._refresh_cache()

        if self._enabled and feed.poll_enabled:
            try:
                await self._start_runtime(feed)
            except Exception:
                logger.exception(
                    "Failed to start runtime for newly subscribed feed %s",
                    feed.id,
                )

        await self._publish_feed_event("feed.subscription.created", feed)
        return feed

    async def unsubscribe(self, feed_id: str, user_ctx: UserContext) -> None:
        feed = await self._require_feed(feed_id)
        self._require_admin(feed, user_ctx)
        await self._stop_runtime(feed.id)

        # Cascade items + their knowledge entries.
        items = await self._storage.query(
            Query(
                collection=_FEED_ITEMS_COLLECTION,
                filters=[Filter(field="feed_id", op=FilterOp.EQ, value=feed.id)],
            )
        )
        for row in items:
            row_id = row.get("_id", "")
            if row.get("ingested_to_knowledge") and self._knowledge is not None:
                with contextlib.suppress(Exception):
                    await self._knowledge.remove_document(
                        _doc_id_for(
                            self._feed_doc_backend.source_id,
                            feed.id,
                            str(row.get("item_uid", "")),
                        )
                    )
            await self._storage.delete(_FEED_ITEMS_COLLECTION, row_id)

        await self._storage.delete(_FEEDS_COLLECTION, feed.id)
        await self._refresh_cache()
        await self._publish_feed_event("feed.subscription.deleted", feed)

    async def update_feed(
        self,
        feed_id: str,
        updates: dict[str, Any],
        user_ctx: UserContext,
    ) -> Feed:
        feed = await self._require_feed(feed_id)
        self._require_admin(feed, user_ctx)

        immutable = {"id", "owner_user_id", "created_at", "http_cache"}
        share_fields = {"shared_with_users", "shared_with_roles"}
        restart_fields = {
            "url",
            "backend_name",
            "backend_config",
            "poll_enabled",
            "poll_interval_sec",
        }
        needs_restart = False
        for key, value in updates.items():
            if key in immutable or key in share_fields:
                continue
            if not hasattr(feed, key):
                continue
            if key in restart_fields:
                needs_restart = True
            setattr(feed, key, value)

        await self._storage.put(_FEEDS_COLLECTION, feed.id, feed.to_dict())
        await self._refresh_cache()

        if self._enabled and needs_restart:
            try:
                await self._restart_runtime(feed)
            except Exception:
                logger.exception(
                    "Failed to restart feed runtime after update: %s", feed.id
                )

        await self._publish_feed_event("feed.subscription.updated", feed)
        return feed

    async def share_user(
        self, feed_id: str, target_user_id: str, user_ctx: UserContext
    ) -> Feed:
        feed = await self._require_feed(feed_id)
        self._require_admin(feed, user_ctx)
        if target_user_id and target_user_id not in feed.shared_with_users:
            feed.shared_with_users.append(target_user_id)
            await self._storage.put(_FEEDS_COLLECTION, feed.id, feed.to_dict())
            await self._publish_shares_changed(feed)
        return feed

    async def unshare_user(
        self, feed_id: str, target_user_id: str, user_ctx: UserContext
    ) -> Feed:
        feed = await self._require_feed(feed_id)
        self._require_admin(feed, user_ctx)
        if target_user_id in feed.shared_with_users:
            feed.shared_with_users.remove(target_user_id)
            await self._storage.put(_FEEDS_COLLECTION, feed.id, feed.to_dict())
            await self._publish_shares_changed(feed)
        return feed

    async def share_role(
        self, feed_id: str, role: str, user_ctx: UserContext
    ) -> Feed:
        feed = await self._require_feed(feed_id)
        self._require_admin(feed, user_ctx)
        if role and role not in feed.shared_with_roles:
            feed.shared_with_roles.append(role)
            await self._storage.put(_FEEDS_COLLECTION, feed.id, feed.to_dict())
            await self._publish_shares_changed(feed)
        return feed

    async def unshare_role(
        self, feed_id: str, role: str, user_ctx: UserContext
    ) -> Feed:
        feed = await self._require_feed(feed_id)
        self._require_admin(feed, user_ctx)
        if role in feed.shared_with_roles:
            feed.shared_with_roles.remove(role)
            await self._storage.put(_FEEDS_COLLECTION, feed.id, feed.to_dict())
            await self._publish_shares_changed(feed)
        return feed

    # ── Read paths ────────────────────────────────────────────────────

    async def get_feed(self, feed_id: str) -> Feed | None:
        row = await self._storage.get(_FEEDS_COLLECTION, feed_id)
        if row is None:
            return None
        return Feed.from_dict(row)

    async def list_accessible_feeds(self, user_ctx: UserContext) -> list[Feed]:
        feeds = await self._load_feeds()
        is_admin = self._is_admin(user_ctx)
        return [f for f in feeds if can_access_feed(user_ctx, f, is_admin=is_admin)]

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
    ) -> list[StoredFeedItem]:
        if user_ctx is None:
            user_ctx = get_current_user()
        accessible = await self.list_accessible_feeds(user_ctx)
        accessible_ids = {f.id for f in accessible}
        if not accessible_ids:
            return []
        if feed_id is not None and feed_id not in accessible_ids:
            return []

        filters: list[Filter] = []
        target_ids = [feed_id] if feed_id else list(accessible_ids)
        if len(target_ids) == 1:
            filters.append(Filter(field="feed_id", op=FilterOp.EQ, value=target_ids[0]))
        else:
            filters.append(Filter(field="feed_id", op=FilterOp.IN, value=target_ids))
        if unread_only:
            filters.append(Filter(field="read", op=FilterOp.EQ, value=False))
        if min_score > 0:
            filters.append(Filter(field="score", op=FilterOp.GTE, value=min_score))

        rows = await self._storage.query(
            Query(
                collection=_FEED_ITEMS_COLLECTION,
                filters=filters,
                sort=[SortField(field="received_at", descending=True)],
                limit=max(1, limit) * max(1, page),
            )
        )

        feed_by_id = {f.id: f for f in accessible}
        if category:
            # Guard ``is not None`` BEFORE dereferencing ``.category`` —
            # list-comp ``if`` clauses run left-to-right, so reversing
            # the order would AttributeError on stale rows referencing a
            # feed_id we can no longer access. Inline tuple-unpack keeps
            # the lookup single-shot.
            filtered: list[dict[str, Any]] = []
            for r in rows:
                feed = feed_by_id.get(str(r.get("feed_id", "")))
                if feed is not None and feed.category == category:
                    filtered.append(r)
            rows = filtered

        items = [StoredFeedItem.from_dict(r) for r in rows]
        if query:
            q = query.lower()
            items = [
                i
                for i in items
                if q in i.title.lower()
                or q in i.summary.lower()
                or q in i.ai_summary.lower()
            ]

        offset = max(0, (page - 1) * limit)
        return items[offset : offset + limit]

    async def get_top_items(
        self,
        user_ctx: UserContext,
        *,
        category: str = "",
        since: datetime | None = None,
        limit: int = 5,
    ) -> list[StoredFeedItem]:
        accessible = await self.list_accessible_feeds(user_ctx)
        eligible = [f for f in accessible if f.briefing_eligible]
        if category:
            eligible = [f for f in eligible if f.category == category]
        feed_ids = [f.id for f in eligible]
        if not feed_ids:
            return []
        since_dt = since or (_now_utc() - timedelta(hours=24))
        filters: list[Filter] = [
            Filter(field="feed_id", op=FilterOp.IN, value=feed_ids),
            Filter(field="briefed_at", op=FilterOp.EQ, value=""),
            Filter(field="score", op=FilterOp.GTE, value=0.0),
            Filter(
                field="received_at",
                op=FilterOp.GTE,
                value=since_dt.isoformat(),
            ),
        ]
        rows = await self._storage.query(
            Query(
                collection=_FEED_ITEMS_COLLECTION,
                filters=filters,
                sort=[SortField(field="received_at", descending=True)],
            )
        )
        items = [StoredFeedItem.from_dict(r) for r in rows]
        items.sort(key=self._effective_score, reverse=True)
        return items[: max(1, limit)]

    @staticmethod
    def _effective_score(item: StoredFeedItem) -> float:
        """Score scaled by recency decay (half-life 24h)."""
        rec = _parse_iso(item.received_at) or _now_utc()
        age_hours = max(
            0.0, (_now_utc() - rec).total_seconds() / 3600.0
        )
        return float(item.score) * math.exp(-age_hours / 24.0)

    async def get_item(self, item_id: str) -> StoredFeedItem | None:
        row = await self._storage.get(_FEED_ITEMS_COLLECTION, item_id)
        if row is None:
            return None
        return StoredFeedItem.from_dict(row)

    async def mark_read(
        self,
        item_id: str,
        user_ctx: UserContext,
        read: bool = True,
    ) -> None:
        item = await self.get_item(item_id)
        if item is None:
            raise FeedNotFoundError(f"Feed item not found: {item_id}")
        feed = await self._require_feed(item.feed_id)
        self._require_access(feed, user_ctx)
        row = item.to_dict()
        row["read"] = bool(read)
        await self._storage.put(_FEED_ITEMS_COLLECTION, item_id, row)

    # ── Polling pipeline ──────────────────────────────────────────────

    async def _poll_runtime(self, runtime: _FeedRuntime) -> None:
        feed = runtime.feed
        # Per-feed lock prevents concurrent fires from double-persisting
        # if the scheduler ever overlaps. Cheap and bug-killing.
        lock = self._poll_locks.setdefault(feed.id, asyncio.Lock())
        async with lock:
            if self._poll_semaphore is None:
                return
            # Capture FIRST-SYNC status BEFORE _mark_polled_ok bumps
            # last_polled_at — used to apply the §6.4e cap.
            is_first_sync = not feed.last_polled_at
            t0 = time.perf_counter()
            try:
                async with self._poll_semaphore:
                    result = await runtime.backend.poll(
                        feed.url,
                        since=_parse_iso(feed.last_polled_at),
                        max_items=self._max_items_per_poll,
                        http_cache=dict(feed.http_cache),
                    )
            except Exception as exc:
                await self._record_poll_error(feed, exc)
                return

            if result.not_modified:
                await self._mark_polled_ok(
                    feed, result, items_total=0, items_new=0, duration_ms=_ms(t0)
                )
                return

            new_items, edited_items = await self._dedup_and_persist(feed, result.items)
            await self._mark_polled_ok(
                feed,
                result,
                items_total=len(result.items),
                items_new=len(new_items),
                duration_ms=_ms(t0),
            )

            for item in new_items:
                await self._publish_item_received(feed, item)
                if self._score_on_ingest:
                    if is_first_sync and self._initial_score_remaining <= 0:
                        # Cap exhausted — flag for lazy scoring.
                        await self._mark_lazy_score(feed, item)
                    else:
                        if is_first_sync:
                            self._initial_score_remaining -= 1
                        await self._enqueue_score(feed, item)
                if feed.ingest_to_knowledge:
                    await self._enqueue_ingest(feed, item)

            if is_first_sync:
                logger.info(
                    "Feeds first-sync for %s: %d new items, score budget remaining=%d",
                    feed.id,
                    len(new_items),
                    self._initial_score_remaining,
                )

            # Edited items are not re-emitted, re-scored, or re-ingested.
            _ = edited_items

    async def _record_poll_error(self, feed: Feed, exc: Exception) -> None:
        feed.consecutive_failures += 1
        feed.last_error = str(exc)
        feed.last_polled_at = _now_utc().isoformat()
        await self._storage.put(_FEEDS_COLLECTION, feed.id, feed.to_dict())
        logger.warning(
            "Feed poll failed for %s (failures=%d): %s",
            feed.id,
            feed.consecutive_failures,
            exc,
        )

        # Back-off after 3 failures.
        if 3 <= feed.consecutive_failures < 20 and self._scheduler is not None:
            from gilbert.interfaces.scheduler import Schedule

            base = max(60, feed.poll_interval_sec)
            effective = min(86400, base * (2 ** (feed.consecutive_failures - 2)))
            with contextlib.suppress(Exception):
                self._scheduler.remove_job(f"feeds-poll-{feed.id}")
            with contextlib.suppress(Exception):
                self._scheduler.add_job(
                    name=f"feeds-poll-{feed.id}",
                    schedule=Schedule.every(effective),
                    callback=self._make_poll_callback(feed.id),
                    system=True,
                )

        # Graceful give-up at 20.
        if feed.consecutive_failures >= 20:
            feed.poll_enabled = False
            await self._storage.put(_FEEDS_COLLECTION, feed.id, feed.to_dict())
            await self._stop_runtime(feed.id)
            await self._publish_event(
                "feed.subscription.disabled",
                {
                    "feed_id": feed.id,
                    "owner_user_id": feed.owner_user_id,
                    "reason": "consecutive_failure_threshold",
                    "last_error": feed.last_error,
                },
            )
            logger.warning(
                "Feed %s auto-disabled after %d consecutive failures",
                feed.id,
                feed.consecutive_failures,
            )

    async def _mark_polled_ok(
        self,
        feed: Feed,
        result: PollResult,
        *,
        items_total: int,
        items_new: int,
        duration_ms: int,
    ) -> None:
        was_failed = feed.consecutive_failures > 0
        feed.consecutive_failures = 0
        feed.last_error = ""
        feed.last_polled_at = _now_utc().isoformat()
        feed.last_poll_status_code = int(result.status_code or 0)
        feed.last_poll_items_total = int(items_total)
        feed.last_poll_items_new = int(items_new)
        feed.last_poll_duration_ms = int(duration_ms)
        feed.http_cache = dict(result.http_cache or {})
        feed.suggested_poll_interval_sec = int(
            result.suggested_min_interval_sec or 0
        )
        await self._storage.put(_FEEDS_COLLECTION, feed.id, feed.to_dict())
        # Restore the original cadence after a back-off recovery.
        if was_failed and self._scheduler is not None:
            from gilbert.interfaces.scheduler import Schedule

            with contextlib.suppress(Exception):
                self._scheduler.remove_job(f"feeds-poll-{feed.id}")
            with contextlib.suppress(Exception):
                self._scheduler.add_job(
                    name=f"feeds-poll-{feed.id}",
                    schedule=Schedule.every(feed.effective_poll_interval_sec()),
                    callback=self._make_poll_callback(feed.id),
                    system=True,
                )

        # Update the in-memory runtime feed copy too so subsequent polls
        # carry the new http_cache.
        runtime = self._runtimes.get(feed.id)
        if runtime is not None:
            runtime.feed = feed

    async def _dedup_and_persist(
        self,
        feed: Feed,
        items: list[FeedItem],
    ) -> tuple[list[StoredFeedItem], list[StoredFeedItem]]:
        new_items: list[StoredFeedItem] = []
        edited_items: list[StoredFeedItem] = []
        for raw in items:
            item_uid = raw.item_uid or ""
            if not item_uid:
                continue
            stored_id = f"{feed.id}__{item_uid}"
            existing = await self._storage.get(_FEED_ITEMS_COLLECTION, stored_id)
            now_iso = _now_utc().isoformat()
            published_iso = (
                raw.published_at.isoformat() if raw.published_at else now_iso
            )
            updated_iso = (
                raw.updated_at.isoformat() if raw.updated_at else published_iso
            )
            summary = (raw.summary or "")[: max(0, self._max_summary_length)]
            if existing is None:
                stored = StoredFeedItem(
                    id=stored_id,
                    feed_id=feed.id,
                    item_uid=item_uid,
                    title=raw.title,
                    link=raw.link,
                    summary=summary,
                    author=raw.author,
                    published_at=published_iso,
                    updated_at=updated_iso,
                    received_at=now_iso,
                    enclosure_url=raw.enclosure_url,
                    enclosure_mime=raw.enclosure_mime,
                )
                await self._storage.put(
                    _FEED_ITEMS_COLLECTION, stored_id, stored.to_dict()
                )
                new_items.append(stored)
                continue
            # Edit detection.
            existing_updated = str(existing.get("updated_at", "") or "")
            title_diff = str(existing.get("title", "")) != raw.title
            summary_diff = str(existing.get("summary", "")) != summary
            if (
                updated_iso
                and existing_updated
                and updated_iso > existing_updated
                and (title_diff or summary_diff)
            ):
                existing["title"] = raw.title
                existing["summary"] = summary
                existing["updated_at"] = updated_iso
                await self._storage.put(_FEED_ITEMS_COLLECTION, stored_id, existing)
                edited_items.append(StoredFeedItem.from_dict(existing))
        return new_items, edited_items

    # ── Score queue ──────────────────────────────────────────────────

    def _spawn_score_workers(self) -> None:
        # Stop any old workers (config-reload case).
        for task in self._score_workers:
            task.cancel()
        self._score_workers = []
        for _ in range(max(1, self._max_concurrent_scoring)):
            self._score_workers.append(asyncio.create_task(self._score_worker_loop()))

    def _spawn_ingest_workers(self) -> None:
        for task in self._ingest_workers:
            task.cancel()
        self._ingest_workers = []
        for _ in range(max(1, self._max_concurrent_ingestion)):
            self._ingest_workers.append(
                asyncio.create_task(self._ingest_worker_loop())
            )

    async def _enqueue_score(self, feed: Feed, item: FeedItem | StoredFeedItem) -> None:
        if self._score_queue is None:
            return
        try:
            self._score_queue.put_nowait((feed, item))
        except asyncio.QueueFull:
            self._score_drops_total += 1
            logger.warning(
                "feeds: score queue full — dropping item %s/%s (drops=%d)",
                feed.id,
                getattr(item, "item_uid", "?"),
                self._score_drops_total,
            )
            # Drop into the lazy-score backlog so the rescore tick still
            # picks the item up — beats silently losing it.
            await self._mark_lazy_score(feed, item)

    async def _mark_lazy_score(
        self, feed: Feed, item: FeedItem | StoredFeedItem
    ) -> None:
        """Persist an item with ``lazy_score=True`` + ``score=-1.0``.

        Used by (a) the §6.4e first-sync cap and (b) the score-queue-full
        drop path, so neither failure mode loses the item — both surface
        through the rescore tick / lazy-score tick later.
        """
        stored_id = f"{feed.id}__{item.item_uid}"
        existing = await self._storage.get(_FEED_ITEMS_COLLECTION, stored_id)
        if existing is None:
            return
        existing["lazy_score"] = True
        # Keep score=-1.0 explicitly so the rescore-tick filter picks it up.
        existing["score"] = -1.0
        await self._storage.put(_FEED_ITEMS_COLLECTION, stored_id, existing)
        self._lazy_score_total += 1

    async def _enqueue_ingest(self, feed: Feed, item: FeedItem | StoredFeedItem) -> None:
        if self._ingest_queue is None:
            return
        try:
            self._ingest_queue.put_nowait((feed, item))
        except asyncio.QueueFull:
            logger.warning(
                "feeds: ingest queue full — dropping item %s/%s",
                feed.id,
                getattr(item, "item_uid", "?"),
            )

    async def _score_worker_loop(self) -> None:
        assert self._score_queue is not None
        try:
            while True:
                feed, item = await self._score_queue.get()
                try:
                    await self._score_item(feed, item)
                except Exception:
                    logger.exception(
                        "Scoring failure for %s/%s", feed.id, item.item_uid
                    )
                finally:
                    self._score_queue.task_done()
        except asyncio.CancelledError:
            pass

    async def _ingest_worker_loop(self) -> None:
        assert self._ingest_queue is not None
        try:
            while True:
                feed, item = await self._ingest_queue.get()
                try:
                    await self._ingest_item(feed, item)
                except Exception:
                    logger.exception(
                        "Ingestion failure for %s/%s", feed.id, item.item_uid
                    )
                finally:
                    self._ingest_queue.task_done()
        except asyncio.CancelledError:
            pass

    def _ai_capability(self) -> AISamplingProvider | None:
        if self._resolver is None:
            return None
        ai_svc = self._resolver.get_capability("ai_chat")
        if isinstance(ai_svc, AISamplingProvider):
            return ai_svc
        return None

    async def _score_item(
        self, feed: Feed, item: FeedItem | StoredFeedItem
    ) -> None:
        ai = self._ai_capability()
        stored_id = f"{feed.id}__{item.item_uid}"
        if ai is None:
            return

        title = item.title
        summary = item.summary
        link = item.link
        user_msg = (
            f"Feed: {feed.name}\nCategory: {feed.category or '(none)'}\n"
            f"Title: {title}\nLink: {link}\nSummary: {summary}\n\n"
            f"User interests: {_sanitize_user_text(self._user_interests) or '(none)'}\n\n"
            "Score this item now."
        )
        try:
            response = await ai.complete_one_shot(
                messages=[Message(role=MessageRole.USER, content=user_msg)],
                system_prompt=self._scoring_prompt,
                profile_name=self._scoring_ai_profile,
                tools_override=[],
            )
            text = (response.message.content or "").strip()
            text = _strip_json_fences(text)
            payload = json.loads(text)
            raw_score = float(payload.get("score", -1.0))
            score = max(0.0, min(1.0, raw_score)) * float(feed.importance_weight)
            score = max(0.0, min(1.0, score))
            reason = str(payload.get("reason", "") or "")
        except Exception:
            score = -1.0
            reason = ""
            logger.warning(
                "Scoring parse failed for %s — leaving at -1.0", stored_id, exc_info=True
            )

        existing = await self._storage.get(_FEED_ITEMS_COLLECTION, stored_id)
        if existing is None:
            return
        existing["score"] = score
        existing["score_reason"] = reason
        existing["lazy_score"] = False
        await self._storage.put(_FEED_ITEMS_COLLECTION, stored_id, existing)

        await self._publish_event(
            "feed.item.scored",
            {
                "feed_id": feed.id,
                "item_id": stored_id,
                "score": score,
                "score_reason": reason,
            },
        )

    async def _rescore_tick(self) -> None:
        """Sweep score=-1.0 items (within 24h) and re-enqueue them.

        Per spec §6.4c: capped at ``max_concurrent_scoring * 10`` per
        tick so a backlog can drain over multiple ticks without ever
        starving the live poll path. ``lazy_score=True`` items
        (initial-sync cap or score-queue-full drops) live indefinitely
        until ``_lazy_score_tick`` catches them — the 24h horizon here
        is just the recency-decay budget for fresh scoring failures.
        """
        cutoff = (_now_utc() - timedelta(hours=24)).isoformat()
        rows = await self._storage.query(
            Query(
                collection=_FEED_ITEMS_COLLECTION,
                filters=[
                    Filter(field="score", op=FilterOp.LT, value=0.0),
                    Filter(field="received_at", op=FilterOp.GTE, value=cutoff),
                ],
                limit=max(1, self._max_concurrent_scoring) * 10,
            )
        )
        for row in rows:
            stored = StoredFeedItem.from_dict(row)
            feed = await self.get_feed(stored.feed_id)
            if feed is None:
                continue
            await self._enqueue_score(
                feed,
                FeedItem(
                    item_uid=stored.item_uid,
                    title=stored.title,
                    link=stored.link,
                    summary=stored.summary,
                ),
            )

    async def _lazy_score_tick(self) -> None:
        """Drain a small batch of ``lazy_score=True`` items per tick.

        First-sync overflow (§6.4e) and score-queue-full drops both flag
        items here. Run at a slow cadence (daily) and bound the per-tick
        batch at ``max_concurrent_scoring * 10`` so a 1000-item backlog
        drains over weeks rather than blowing the AI budget at once.

        Items beyond the 24h rescore-tick window would otherwise be
        stranded — this tick is the safety net.
        """
        rows = await self._storage.query(
            Query(
                collection=_FEED_ITEMS_COLLECTION,
                filters=[
                    Filter(field="lazy_score", op=FilterOp.EQ, value=True),
                ],
                sort=[SortField(field="received_at", descending=False)],
                limit=max(1, self._max_concurrent_scoring) * 10,
            )
        )
        if not rows:
            return
        logger.info(
            "feeds: lazy-score tick draining %d item(s) (total flagged=%d)",
            len(rows),
            self._lazy_score_total,
        )
        for row in rows:
            stored = StoredFeedItem.from_dict(row)
            feed = await self.get_feed(stored.feed_id)
            if feed is None:
                continue
            # Clear the flag eagerly so a re-tick before scoring completes
            # doesn't double-enqueue. _score_item will overwrite the row
            # with the real score (or leave -1.0 on parse failure, which
            # the rescore tick then catches inside the 24h window).
            existing = await self._storage.get(_FEED_ITEMS_COLLECTION, stored.id)
            if existing is not None:
                existing["lazy_score"] = False
                await self._storage.put(_FEED_ITEMS_COLLECTION, stored.id, existing)
            await self._enqueue_score(
                feed,
                FeedItem(
                    item_uid=stored.item_uid,
                    title=stored.title,
                    link=stored.link,
                    summary=stored.summary,
                ),
            )

    # ── Knowledge ingestion ──────────────────────────────────────────

    async def _ingest_item(
        self, feed: Feed, item: FeedItem | StoredFeedItem
    ) -> None:
        if self._knowledge is None:
            return
        # Per-user-per-day cap.
        cap_key = f"{feed.owner_user_id}:{_now_utc().strftime('%Y-%m-%d')}"
        existing_count = await self._storage.get(_INGEST_DAILY_COLLECTION, cap_key)
        current = int((existing_count or {}).get("count", 0) or 0)
        if current >= self._ingest_max_items_per_day_per_user:
            await self._publish_event(
                "feed.ingest.throttled",
                {
                    "user_id": feed.owner_user_id,
                    "feed_id": feed.id,
                    "current_count": current,
                    "cap": self._ingest_max_items_per_day_per_user,
                },
            )
            return

        link = item.link
        if not link:
            return

        # SSRF / politeness checks.
        if not await self._safe_to_fetch(feed, link):
            return

        body, mime, status = await self._fetch_article_body(link)
        if body is None:
            return
        # Narrow to HTML (per spec §9 step 6 — closed allow-list).
        # PDFs, images, calendars, plain text, RSS-as-text get skipped;
        # only article bodies belong in the vector index.
        if mime not in {"text/html", "application/xhtml+xml"}:
            logger.info(
                "feeds: skipping ingestion — non-HTML content-type %r for %s",
                mime,
                link,
            )
            return

        text = self._html_to_text(body.decode("utf-8", errors="replace"))
        if len(text) < 1024 or _PAYWALL_RE.search(text):
            logger.info(
                "feeds: skipping likely paywall or stub for %s (%d chars)",
                link,
                len(text),
            )
            return

        # Cache the bytes (observability).
        # Path layout MUST stay aligned with `_doc_id_for` — cascade
        # delete keys off the same composition.
        rel_path = f"{feed.id}/{_safe_uid(item.item_uid)}.html"
        cache_path = self._feed_doc_backend.base_dir / rel_path
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(body)

        meta = DocumentMeta(
            source_id=self._feed_doc_backend.source_id,
            path=rel_path,
            name=item.title or item.item_uid,
            document_type=DocumentType.TEXT,
            size_bytes=len(body),
            last_modified=_now_utc().isoformat(),
            mime_type=mime or "text/html",
            external_url=link,
        )
        try:
            await self._knowledge.index_document(self._feed_doc_backend, meta)
        except Exception:
            logger.warning(
                "feeds: knowledge.index_document failed for %s",
                meta.document_id,
                exc_info=True,
            )
            return

        # Mark stored item ingested.
        stored_id = f"{feed.id}__{item.item_uid}"
        existing = await self._storage.get(_FEED_ITEMS_COLLECTION, stored_id)
        if existing is not None:
            existing["ingested_to_knowledge"] = True
            await self._storage.put(_FEED_ITEMS_COLLECTION, stored_id, existing)

        await self._storage.put(
            _INGEST_DAILY_COLLECTION,
            cap_key,
            {"_id": cap_key, "count": current + 1},
        )

    async def _safe_to_fetch(self, feed: Feed, link: str) -> bool:
        """SSRF guard for article-body fetches.

        Two layers:
        (a) ALWAYS-block private / loopback / link-local / cloud-metadata
            hosts — IPv4 and IPv6 — regardless of feed/link relationship,
            so a feed whose own URL resolves locally can't smuggle a
            metadata-server fetch through.
        (b) eTLD+1 advisory — different registrable suffix is allowed
            (sites legitimately link out) but combined with (a) the
            metadata path still blocks.

        This is NOT a complete public-suffix-list implementation; the
        deny-list-based eTLD+1 check below recognises the common
        multi-part suffixes (co.uk, com.au, …) so ``bbc.co.uk`` doesn't
        reduce to ``co.uk``. The advisory exists so a hijacked feed
        can't easily pivot off-domain — the ``always-block`` set above
        is the load-bearing privacy guard.
        """
        parsed = urlparse(link)
        if parsed.scheme not in {"http", "https"}:
            return False

        link_host = (parsed.hostname or "").lower()
        if not link_host:
            return False
        if _is_private_or_metadata_host(link_host):
            logger.info("feeds: refusing fetch to private / metadata host: %s", link)
            return False

        # eTLD+1 SSRF guard (multi-part suffix aware).
        try:
            feed_host = (urlparse(feed.url).hostname or "").lower()
            if feed_host:
                feed_top = _registrable_suffix(feed_host)
                link_top = _registrable_suffix(link_host)
                if feed_top != link_top and _is_private_or_metadata_host(
                    link_host
                ):
                    return False
        except Exception:
            return False

        if self._respect_robots_txt:
            allowed = await self._robots_allows(parsed.scheme, parsed.netloc, parsed.path)
            if not allowed:
                logger.info("feeds: robots.txt disallows %s", link)
                return False
        return True

    async def _robots_allows(self, scheme: str, netloc: str, path: str) -> bool:
        from urllib.robotparser import RobotFileParser

        key = f"{scheme}://{netloc}"
        now = time.monotonic()
        cached = self._robots_cache.get(key)
        rp: Any
        if cached is not None and now - cached[0] < 3600:
            rp = cached[1]
        else:
            rp = RobotFileParser()
            rp.set_url(f"{key}/robots.txt")
            try:
                if self._http_client is None:
                    return True
                response = await self._http_client.get(
                    f"{key}/robots.txt", timeout=5.0
                )
                if response.status_code == 200:
                    rp.parse(response.text.splitlines())
                else:
                    rp.parse([])
            except Exception:
                rp.parse([])
            self._robots_cache[key] = (now, rp)
        try:
            return bool(rp.can_fetch("GilbertFeeds", f"{scheme}://{netloc}{path}"))
        except Exception:
            return True

    async def _fetch_article_body(
        self, url: str
    ) -> tuple[bytes | None, str, int]:
        if self._http_client is None:
            return None, "", 0
        original_scheme = urlparse(url).scheme
        current_url = url
        for _ in range(6):  # 5 redirects + 1 initial
            # SSRF guard the CURRENT url on every hop — a public initial
            # link that 302s to ``http://169.254.169.254`` would otherwise
            # walk straight through.
            current_host = (urlparse(current_url).hostname or "").lower()
            if not current_host or _is_private_or_metadata_host(current_host):
                logger.info(
                    "feeds: refusing redirect to private / metadata host: %s",
                    current_url,
                )
                return None, "", 0
            try:
                response = await self._http_client.get(
                    current_url,
                    headers={
                        "User-Agent": "GilbertFeeds/1.0",
                        "Accept": "text/html, application/xhtml+xml;q=0.9, */*;q=0.5",
                    },
                )
            except Exception:
                return None, "", 0
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location", "")
                if not location:
                    return None, "", response.status_code
                next_url = str(httpx.URL(current_url).join(location))
                if (
                    original_scheme == "https"
                    and urlparse(next_url).scheme == "http"
                ):
                    return None, "", response.status_code
                current_url = next_url
                continue
            mime = (
                response.headers.get("content-type", "").split(";")[0].strip().lower()
            )
            # Pre-check Content-Length to short-circuit oversized bodies
            # before reading them. (TODO v1.x: switch to streaming
            # `client.stream()` so a chunked-encoding adversary without
            # Content-Length can't OOM us — for now `httpx` defaults +
            # the post-read cap below provide a backstop.)
            length = response.headers.get("content-length")
            if length is not None:
                try:
                    if int(length) > 256 * 1024:
                        return None, mime, response.status_code
                except ValueError:
                    pass
            body = response.content
            if len(body) > 256 * 1024:
                # Skip half-articles — search relevance breaks otherwise.
                return None, mime, response.status_code
            return body, mime, response.status_code
        return None, "", 0

    @staticmethod
    def _html_to_text(html: str) -> str:
        out: list[str] = []
        in_tag = False
        for ch in html:
            if ch == "<":
                in_tag = True
                continue
            if ch == ">":
                in_tag = False
                out.append(" ")
                continue
            if not in_tag:
                out.append(ch)
        return " ".join("".join(out).split())

    # ── Retention sweep ──────────────────────────────────────────────

    async def _retention_tick(self) -> None:
        if self._retention_days <= 0:
            return
        cutoff = (_now_utc() - timedelta(days=self._retention_days)).isoformat()
        rows = await self._storage.query(
            Query(
                collection=_FEED_ITEMS_COLLECTION,
                filters=[
                    Filter(field="received_at", op=FilterOp.LT, value=cutoff),
                ],
            )
        )
        removed = 0
        for row in rows:
            row_id = row.get("_id", "")
            if (
                row.get("ingested_to_knowledge")
                and self._knowledge is not None
                and self._feed_doc_backend is not None
            ):
                with contextlib.suppress(Exception):
                    await self._knowledge.remove_document(
                        _doc_id_for(
                            self._feed_doc_backend.source_id,
                            str(row.get("feed_id", "")),
                            str(row.get("item_uid", "")),
                        )
                    )
            await self._storage.delete(_FEED_ITEMS_COLLECTION, row_id)
            removed += 1
        if removed:
            logger.info(
                "Feeds retention: removed %d items older than %d days",
                removed,
                self._retention_days,
            )

    # ── Briefing builder ─────────────────────────────────────────────

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
    ) -> BriefingResult:
        since_dt = since or (_now_utc() - timedelta(hours=24))
        items = await self.get_top_items(
            user_ctx, category=category, since=since_dt, limit=max(1, top_n)
        )

        if not items:
            return BriefingResult(
                spoken="No new news in your subscribed feeds today.",
                headlines=[],
                item_ids=[],
                since=since_dt,
                briefing_id=f"brief_{uuid.uuid4().hex[:12]}",
            )

        # Anti-repetition context.
        recent_briefings = anti_repetition_context or []
        if not recent_briefings:
            state = await self._storage.get(
                _BRIEFING_STATE_COLLECTION, user_ctx.user_id
            )
            if state:
                raw = state.get("recent_briefings") or []
                if isinstance(raw, list):
                    recent_briefings = [str(s) for s in raw][-10:]

        ai = self._ai_capability()
        spoken: str
        headlines: list[BriefingHeadline]
        if ai is None:
            spoken = self._fallback_briefing(items)
            headlines = self._fallback_headlines(items)
        else:
            recent_lines = (
                "\n".join(f"- {b}" for b in recent_briefings[-7:])
                if recent_briefings
                else "(none yet)"
            )
            cap_words = (
                int(max_spoken_seconds * 2.5)
                if max_spoken_seconds > 0
                else 200
            )
            user_msg = (
                "Today's items (id | title | one-line summary | score):\n"
                + "\n".join(
                    f"{it.id} | {it.title} | {it.summary[:200]} | {it.score:.2f}"
                    for it in items
                )
                + "\n\nKeep spoken under "
                + str(cap_words)
                + " words.\n\nRecent briefings — be different today:\n"
                + recent_lines
            )
            try:
                response = await ai.complete_one_shot(
                    messages=[Message(role=MessageRole.USER, content=user_msg)],
                    system_prompt=self._briefing_prompt,
                    profile_name=self._briefing_ai_profile,
                    tools_override=[],
                )
                text = _strip_json_fences(
                    (response.message.content or "").strip()
                )
                payload = json.loads(text)
                spoken = str(payload.get("spoken", "") or "")
                raw_headlines = payload.get("headlines") or []
                headlines = []
                if isinstance(raw_headlines, list):
                    for h in raw_headlines:
                        if not isinstance(h, dict):
                            continue
                        item_id = str(h.get("item_id", "") or "")
                        item_match = next(
                            (it for it in items if it.id == item_id),
                            None,
                        )
                        link = item_match.link if item_match else ""
                        headlines.append(
                            BriefingHeadline(
                                item_id=item_id,
                                title=str(h.get("title", "") or ""),
                                one_liner=str(h.get("one_liner", "") or ""),
                                score=float(h.get("score", 0.0) or 0.0),
                                link=link,
                            )
                        )
                if not spoken:
                    raise ValueError("empty spoken text")
            except Exception:
                logger.warning(
                    "Briefing parse failed — falling back to deterministic format",
                    exc_info=True,
                )
                spoken = self._fallback_briefing(items)
                headlines = self._fallback_headlines(items)

        briefing_id = f"brief_{uuid.uuid4().hex[:12]}"
        item_ids = [it.id for it in items]
        result = BriefingResult(
            spoken=spoken,
            headlines=headlines,
            item_ids=item_ids,
            since=since_dt,
            briefing_id=briefing_id,
        )

        if mark_briefed:
            now_iso = _now_utc().isoformat()
            for it in items:
                row = await self._storage.get(_FEED_ITEMS_COLLECTION, it.id)
                if row is None:
                    continue
                row["briefed_at"] = now_iso
                await self._storage.put(_FEED_ITEMS_COLLECTION, it.id, row)

            # Persist briefing record + anti-repetition state.
            await self._storage.put(
                _BRIEFINGS_COLLECTION,
                briefing_id,
                {
                    "_id": briefing_id,
                    "user_id": user_ctx.user_id,
                    "spoken": spoken,
                    "headlines": [h.to_dict() for h in headlines],
                    "item_ids": item_ids,
                    "since": since_dt.isoformat(),
                    "created_at": _now_utc().isoformat(),
                },
            )
            await self._update_briefing_state(user_ctx.user_id, briefing_id, spoken)

        return result

    @staticmethod
    def _fallback_briefing(items: list[StoredFeedItem]) -> str:
        if not items:
            return "No new news today."
        snippets = [it.title for it in items if it.title]
        return "Top stories: " + "; ".join(snippets[:5])

    @staticmethod
    def _fallback_headlines(items: list[StoredFeedItem]) -> list[BriefingHeadline]:
        return [
            BriefingHeadline(
                item_id=it.id,
                title=it.title,
                one_liner=(it.summary or it.ai_summary)[:140],
                score=float(it.score),
                link=it.link,
            )
            for it in items
        ]

    async def get_briefing(self, briefing_id: str) -> dict[str, Any] | None:
        row = await self._storage.get(_BRIEFINGS_COLLECTION, briefing_id)
        if row is None:
            return None
        return dict(row)

    async def _update_briefing_state(
        self, user_id: str, briefing_id: str, spoken: str
    ) -> None:
        existing = await self._storage.get(_BRIEFING_STATE_COLLECTION, user_id) or {}
        recent = list(existing.get("recent_briefings") or [])
        if spoken:
            recent.append(spoken)
        recent = recent[-10:]
        opt_in = existing.get("briefing_opt_in")
        if opt_in is None:
            opt_in = True  # owner who triggered the briefing — default opt-in
        await self._storage.put(
            _BRIEFING_STATE_COLLECTION,
            user_id,
            {
                "_id": user_id,
                "last_briefed_on": _now_utc().strftime("%Y-%m-%d"),
                "last_briefing_id": briefing_id,
                "recent_briefings": recent,
                "briefing_opt_in": bool(opt_in),
            },
        )

    async def get_briefing_state(self, user_id: str) -> dict[str, Any]:
        row = await self._storage.get(_BRIEFING_STATE_COLLECTION, user_id) or {}
        return dict(row)

    async def set_briefing_opt_in(self, user_id: str, opt_in: bool) -> None:
        existing = await self._storage.get(_BRIEFING_STATE_COLLECTION, user_id) or {}
        existing["_id"] = user_id
        existing["briefing_opt_in"] = bool(opt_in)
        await self._storage.put(_BRIEFING_STATE_COLLECTION, user_id, existing)

    # ── OPML ─────────────────────────────────────────────────────────

    async def import_opml(
        self, opml_text: str, user_ctx: UserContext
    ) -> list[tuple[str, str]]:
        """Import feeds from an OPML document. Returns ``[(url, error|"")]``."""
        import xml.etree.ElementTree as ET

        results: list[tuple[str, str]] = []
        try:
            root = ET.fromstring(opml_text)
        except ET.ParseError as exc:
            return [("", f"OPML parse error: {exc}")]
        for outline in root.iter("outline"):
            xml_url = outline.attrib.get("xmlUrl") or outline.attrib.get("xmlurl")
            if not xml_url:
                continue
            name = outline.attrib.get("title") or outline.attrib.get("text") or ""
            category = outline.attrib.get("category") or ""
            try:
                await self.subscribe(
                    xml_url, user_ctx, name=name, category=category
                )
                results.append((xml_url, ""))
            except Exception as exc:
                results.append((xml_url, str(exc)))
        return results

    async def export_opml(self, user_ctx: UserContext) -> str:
        feeds = await self.list_accessible_feeds(user_ctx)
        outlines: list[str] = []
        for f in feeds:
            outlines.append(
                f'    <outline type="rss" text="{_xml_escape(f.name)}" '
                f'title="{_xml_escape(f.name)}" '
                f'xmlUrl="{_xml_escape(f.url)}" '
                f'category="{_xml_escape(f.category)}"/>'
            )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<opml version="2.0">\n'
            "<head><title>Gilbert feeds</title></head>\n"
            "<body>\n" + "\n".join(outlines) + "\n</body>\n</opml>\n"
        )

    # ── Events ───────────────────────────────────────────────────────

    async def _publish_event(self, event_type: str, data: dict[str, Any]) -> None:
        if self._event_bus is None:
            return
        await self._event_bus.publish(
            Event(event_type=event_type, data=data, source="feeds")
        )

    async def _publish_feed_event(self, event_type: str, feed: Feed) -> None:
        await self._publish_event(
            event_type,
            {
                "feed_id": feed.id,
                "name": feed.name,
                "url": feed.url,
                "owner_user_id": feed.owner_user_id,
            },
        )

    async def _publish_shares_changed(self, feed: Feed) -> None:
        await self._publish_event(
            "feed.subscription.shares.changed",
            {
                "feed_id": feed.id,
                "owner_user_id": feed.owner_user_id,
                "shared_with_users": list(feed.shared_with_users),
                "shared_with_roles": list(feed.shared_with_roles),
            },
        )

    async def _publish_item_received(
        self, feed: Feed, item: StoredFeedItem
    ) -> None:
        await self._publish_event(
            "feed.item.received",
            {
                "feed_id": feed.id,
                "item_id": item.id,
                "title": item.title,
                "link": item.link,
            },
        )

    # ── Tool surface ─────────────────────────────────────────────────

    @property
    def tool_provider_name(self) -> str:
        return "feeds"

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        return [
            ToolDefinition(
                name="news_briefing",
                slash_group="feeds",
                slash_command="briefing",
                # Spec calls for a "today" alias; the slash framework
                # does not expose aliases on ToolDefinition today, so
                # the alias is implemented at the slash-command parser
                # layer (or as a v1.x ToolDefinition.slash_aliases
                # field). Discoverable via /feeds briefing.
                slash_help=(
                    "Today's news briefing: /feeds briefing [top=5] [since=24h]"
                ),
                description=(
                    "Generate the user's news briefing as a single spoken "
                    "paragraph PLUS a structured headline list. Pulls "
                    "top-scored items across feeds the caller can access, "
                    "marks them as briefed, returns spoken text + headlines. "
                    "USE THIS for cross-feed daily summaries: 'what's "
                    "important today?', 'morning briefing', 'what should I "
                    "know?'. Only call once per day per user — the second "
                    "call returns the cached result, does not re-run the AI. "
                    "For per-feed summaries, use summarize_feed."
                ),
                parameters=[
                    ToolParameter(
                        name="top",
                        type=ToolParameterType.INTEGER,
                        description="Number of top items (1..20).",
                        required=False,
                        default=5,
                    ),
                    ToolParameter(
                        name="since",
                        type=ToolParameterType.STRING,
                        description="ISO duration like '24h' or full ISO datetime.",
                        required=False,
                        default="24h",
                    ),
                    ToolParameter(
                        name="category",
                        type=ToolParameterType.STRING,
                        description="Optional category filter.",
                        required=False,
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="search_feeds",
                slash_group="feeds",
                slash_command="search",
                slash_help=(
                    "Search feed items: /feeds search [query=...] "
                    "[unread=true] [feed_id=...] [min_score=0.5]"
                ),
                description=(
                    "Search items across feeds the caller can access. Use "
                    "for 'find articles about X', 'unread on this feed', "
                    "'scores above 0.7 in tech feeds'. Do NOT use for "
                    "'what's new today' — that's news_briefing."
                ),
                parameters=[
                    ToolParameter(
                        name="query",
                        type=ToolParameterType.STRING,
                        description="Free-text query (matches title/summary).",
                        required=False,
                    ),
                    ToolParameter(
                        name="feed_id",
                        type=ToolParameterType.STRING,
                        description="Restrict to one feed.",
                        required=False,
                    ),
                    ToolParameter(
                        name="unread_only",
                        type=ToolParameterType.BOOLEAN,
                        description="Only unread items.",
                        required=False,
                        default=False,
                    ),
                    ToolParameter(
                        name="min_score",
                        type=ToolParameterType.NUMBER,
                        description="Minimum AI importance score.",
                        required=False,
                        default=0.0,
                    ),
                    ToolParameter(
                        name="category",
                        type=ToolParameterType.STRING,
                        description="Restrict to one category.",
                        required=False,
                    ),
                    ToolParameter(
                        name="limit",
                        type=ToolParameterType.INTEGER,
                        description="Max items per page.",
                        required=False,
                        default=20,
                    ),
                    ToolParameter(
                        name="page",
                        type=ToolParameterType.INTEGER,
                        description="Page number (1-based).",
                        required=False,
                        default=1,
                    ),
                ],
                required_role="user",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="summarize_feed",
                slash_group="feeds",
                slash_command="summarize",
                slash_help=(
                    "Summarize a feed or one item: /feeds summarize "
                    "<feed_id> [item_id=...] [count=10]"
                ),
                description=(
                    "Summarize the most recent N items in a feed (default "
                    "10), or summarize one specific item by id. USE THIS "
                    "when the user names a specific feed. Caches the AI "
                    "summary on each item."
                ),
                parameters=[
                    ToolParameter(
                        name="feed_id",
                        type=ToolParameterType.STRING,
                        description="Feed id (partial match resolves).",
                    ),
                    ToolParameter(
                        name="item_id",
                        type=ToolParameterType.STRING,
                        description="Optional single-item id.",
                        required=False,
                    ),
                    ToolParameter(
                        name="count",
                        type=ToolParameterType.INTEGER,
                        description="Number of items to summarize.",
                        required=False,
                        default=10,
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="subscribe_feed",
                slash_group="feeds",
                slash_command="subscribe",
                slash_help=(
                    "Subscribe to a feed: /feeds subscribe <url> "
                    "[name=...] [category=...]"
                ),
                description=(
                    "Probe a feed URL and propose a subscription. Returns "
                    "a confirmation UI block; does NOT persist directly. "
                    "The user clicks Confirm to fire feeds.create."
                ),
                parameters=[
                    ToolParameter(
                        name="url",
                        type=ToolParameterType.STRING,
                        description="Feed URL to subscribe to.",
                    ),
                    ToolParameter(
                        name="name",
                        type=ToolParameterType.STRING,
                        description="Optional display name override.",
                        required=False,
                    ),
                    ToolParameter(
                        name="category",
                        type=ToolParameterType.STRING,
                        description="Optional category tag.",
                        required=False,
                    ),
                    ToolParameter(
                        name="poll_interval_sec",
                        type=ToolParameterType.INTEGER,
                        description="Poll cadence in seconds.",
                        required=False,
                        default=1800,
                    ),
                    ToolParameter(
                        name="confirm",
                        type=ToolParameterType.BOOLEAN,
                        description="Set true to actually persist (after preview).",
                        required=False,
                        default=False,
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="unsubscribe_feed",
                slash_group="feeds",
                slash_command="unsubscribe",
                slash_help="Unsubscribe from a feed: /feeds unsubscribe <feed_id>",
                description=(
                    "Propose unsubscribing from a feed. Returns a "
                    "confirmation UI block; does NOT persist directly."
                ),
                parameters=[
                    ToolParameter(
                        name="feed_id",
                        type=ToolParameterType.STRING,
                        description="Feed id to remove (partial match resolves).",
                    ),
                    ToolParameter(
                        name="confirm",
                        type=ToolParameterType.BOOLEAN,
                        description="Set true to actually delete.",
                        required=False,
                        default=False,
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="list_feeds",
                slash_group="feeds",
                slash_command="list",
                slash_help="List feeds you can access: /feeds list [compact=true]",
                description=(
                    "List every feed the caller can access (owner / admin "
                    "/ shared). Call this first when the user's intent "
                    "doesn't already name a feed."
                ),
                parameters=[
                    ToolParameter(
                        name="compact",
                        type=ToolParameterType.BOOLEAN,
                        description="Compact output (name + unread + error pill).",
                        required=False,
                        default=True,
                    ),
                ],
                required_role="user",
                parallel_safe=True,
            ),
            ToolDefinition(
                name="read_feed_item",
                slash_group="feeds",
                slash_command="read",
                slash_help=(
                    "Read an item: /feeds read <item_id> [mark_read=true]"
                ),
                description=(
                    "Read one feed item — title, link, summary, ai_summary, "
                    "score, and score_reason. By default, marks the item "
                    "as read as a side effect."
                ),
                parameters=[
                    ToolParameter(
                        name="item_id",
                        type=ToolParameterType.STRING,
                        description=(
                            "Item id (also accepts 'latest' or 'latest "
                            "<category>')."
                        ),
                    ),
                    ToolParameter(
                        name="mark_read",
                        type=ToolParameterType.BOOLEAN,
                        description="Mark as read on read.",
                        required=False,
                        default=True,
                    ),
                ],
                required_role="user",
            ),
            ToolDefinition(
                name="recommend_knowledge_ingestion",
                slash_group="feeds",
                slash_command="recommend-knowledge",
                slash_help=(
                    "Suggest which feeds to ingest into knowledge: "
                    "/feeds recommend-knowledge [feed_id=...]"
                ),
                description=(
                    "Analyze the user's feeds and recommend whether "
                    "ingest_to_knowledge should be enabled for each. "
                    "Returns a list of {feed_id, recommendation, "
                    "rationale} — does NOT flip the flag."
                ),
                parameters=[
                    ToolParameter(
                        name="feed_id",
                        type=ToolParameterType.STRING,
                        description="Optional single-feed scope.",
                        required=False,
                    ),
                ],
                required_role="user",
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str | ToolOutput:
        match name:
            case "news_briefing":
                return await self._tool_news_briefing(arguments)
            case "search_feeds":
                return await self._tool_search_feeds(arguments)
            case "summarize_feed":
                return await self._tool_summarize_feed(arguments)
            case "subscribe_feed":
                return await self._tool_subscribe_feed(arguments)
            case "unsubscribe_feed":
                return await self._tool_unsubscribe_feed(arguments)
            case "list_feeds":
                return await self._tool_list_feeds(arguments)
            case "read_feed_item":
                return await self._tool_read_feed_item(arguments)
            case "recommend_knowledge_ingestion":
                return await self._tool_recommend_knowledge(arguments)
            case _:
                raise KeyError(f"Unknown tool: {name}")

    # --- Tool implementations -----------------------------------------

    async def _tool_news_briefing(self, args: dict[str, Any]) -> str | ToolOutput:
        user_ctx = get_current_user()
        # Idempotency — if briefing already fired today, return cached.
        state = await self.get_briefing_state(user_ctx.user_id)
        today = _now_utc().strftime("%Y-%m-%d")
        if state.get("last_briefed_on") == today and state.get("last_briefing_id"):
            cached = await self.get_briefing(str(state.get("last_briefing_id", "")))
            if cached:
                return _briefing_tool_output(cached)
        top_n = int(args.get("top") or 5)
        since_arg = str(args.get("since") or "24h").strip()
        since_dt = _parse_since(since_arg)
        category = str(args.get("category", "") or "")
        result = await self.build_briefing(
            user_ctx,
            top_n=top_n,
            since=since_dt,
            category=category,
            mark_briefed=True,
        )
        cached = await self.get_briefing(result.briefing_id) or {
            "spoken": result.spoken,
            "headlines": [h.to_dict() for h in result.headlines],
            "item_ids": result.item_ids,
        }
        return _briefing_tool_output(cached)

    async def _tool_search_feeds(self, args: dict[str, Any]) -> str:
        user_ctx = get_current_user()
        items = await self.search_items(
            feed_id=str(args.get("feed_id") or "") or None,
            query=str(args.get("query") or ""),
            unread_only=bool(args.get("unread_only") or False),
            min_score=float(args.get("min_score") or 0.0),
            category=str(args.get("category") or ""),
            limit=int(args.get("limit") or 20),
            page=int(args.get("page") or 1),
            user_ctx=user_ctx,
        )
        if not items:
            return "No feed items match."
        lines = [f"{len(items)} item(s):"]
        for it in items:
            lines.append(
                f"  - [{it.id}] {it.title} | score={it.score:.2f} | "
                f"unread={'y' if not it.read else 'n'}"
            )
        return "\n".join(lines)

    async def _tool_summarize_feed(self, args: dict[str, Any]) -> str:
        user_ctx = get_current_user()
        feed_id = str(args.get("feed_id") or "")
        if not feed_id:
            return "feed_id is required. Call /feeds list to see your feeds."
        feed = await self._resolve_feed_id(feed_id, user_ctx)
        if feed is None:
            return f"No accessible feed matches: {feed_id}"
        item_id = str(args.get("item_id") or "")
        count = max(1, int(args.get("count") or 10))
        if item_id:
            item = await self.get_item(item_id)
            if item is None or item.feed_id != feed.id:
                return f"Item {item_id} not found in feed {feed.id}."
            return await self._summarize_one(item)
        items = await self.search_items(
            feed_id=feed.id, limit=count, user_ctx=user_ctx
        )
        if not items:
            return f"No items found in feed {feed.name!r}."
        out: list[str] = []
        for it in items:
            summary = await self._summarize_one(it)
            out.append(f"[{it.id}] {it.title}\n  {summary}")
        return "\n\n".join(out)

    async def _summarize_one(self, item: StoredFeedItem) -> str:
        if item.ai_summary:
            return item.ai_summary
        ai = self._ai_capability()
        if ai is None:
            return item.summary or "(no summary)"
        try:
            response = await ai.complete_one_shot(
                messages=[
                    Message(
                        role=MessageRole.USER,
                        content=(
                            f"Title: {item.title}\nLink: {item.link}\n\n"
                            f"{item.summary}"
                        ),
                    )
                ],
                system_prompt=self._summarization_prompt,
                profile_name=self._summarization_ai_profile,
                tools_override=[],
            )
            text = (response.message.content or "").strip()[:500]
        except Exception:
            text = item.summary[:500]
        if text:
            row = await self._storage.get(_FEED_ITEMS_COLLECTION, item.id)
            if row is not None:
                row["ai_summary"] = text
                await self._storage.put(_FEED_ITEMS_COLLECTION, item.id, row)
        return text

    async def _tool_subscribe_feed(self, args: dict[str, Any]) -> str | ToolOutput:
        user_ctx = get_current_user()
        url = str(args.get("url") or "")
        if not url:
            return "url is required."
        name = str(args.get("name") or "")
        category = str(args.get("category") or "")
        poll_interval = int(args.get("poll_interval_sec") or self._default_poll_interval_sec)
        confirm = bool(args.get("confirm"))

        async def _do() -> str:
            feed = await self.subscribe(
                url, user_ctx, name=name, category=category,
                poll_interval_sec=poll_interval,
            )
            return f"Subscribed to {feed.name!r} (id={feed.id})"

        return await confirm_or_execute(
            confirm=confirm,
            tool_name="subscribe_feed",
            title="Subscribe to feed",
            summary=f"About to subscribe to {url}",
            summary_lines=[
                f"url: {url}",
                f"name: {name or '(auto)'}",
                f"category: {category or '(none)'}",
                f"poll_interval_sec: {poll_interval}",
            ],
            arguments=args,
            execute=_do,
        )

    async def _tool_unsubscribe_feed(self, args: dict[str, Any]) -> str | ToolOutput:
        user_ctx = get_current_user()
        feed_id_input = str(args.get("feed_id") or "")
        if not feed_id_input:
            return "feed_id is required."
        feed = await self._resolve_feed_id(feed_id_input, user_ctx)
        if feed is None:
            return f"No accessible feed matches: {feed_id_input}"
        confirm = bool(args.get("confirm"))

        async def _do() -> str:
            await self.unsubscribe(feed.id, user_ctx)
            return f"Unsubscribed from {feed.name!r}"

        return await confirm_or_execute(
            confirm=confirm,
            tool_name="unsubscribe_feed",
            title="Unsubscribe from feed",
            summary=f"About to unsubscribe from {feed.name!r}",
            summary_lines=[
                f"feed_id: {feed.id}",
                f"name: {feed.name}",
                f"url: {feed.url}",
            ],
            arguments=args,
            execute=_do,
        )

    async def _tool_list_feeds(self, args: dict[str, Any]) -> str:
        user_ctx = get_current_user()
        feeds = await self.list_accessible_feeds(user_ctx)
        if not feeds:
            return "You have no accessible feeds."
        is_admin = self._is_admin(user_ctx)
        compact = bool(args.get("compact", True))
        lines: list[str] = []
        for f in feeds:
            access = determine_feed_access(user_ctx, f, is_admin=is_admin)
            access_str = access.value if access else "none"
            if compact:
                err = " ⚠" if f.last_error else ""
                lines.append(f"- {f.id} {f.name}{err} ({access_str})")
            else:
                lines.append(
                    f"- {f.id} {f.name} | url={f.url} | category={f.category} "
                    f"| poll={f.poll_interval_sec}s | access={access_str} "
                    f"| last_polled={f.last_polled_at or '(never)'} "
                    f"| failures={f.consecutive_failures}"
                )
        return "\n".join(lines)

    async def _tool_read_feed_item(self, args: dict[str, Any]) -> str:
        user_ctx = get_current_user()
        item_id = str(args.get("item_id") or "")
        if not item_id:
            return "item_id is required."
        mark_read = bool(args.get("mark_read", True))
        item = await self._resolve_item_id(item_id, user_ctx)
        if item is None:
            return f"No accessible item matches: {item_id}"
        feed = await self._require_feed(item.feed_id)
        try:
            self._require_access(feed, user_ctx)
        except FeedsPermissionError:
            return "You don't have access to this item."
        if mark_read and not item.read:
            with contextlib.suppress(Exception):
                await self.mark_read(item.id, user_ctx, read=True)
        return json.dumps(
            {
                "id": item.id,
                "feed_id": item.feed_id,
                "title": item.title,
                "link": item.link,
                "summary": item.summary,
                "ai_summary": item.ai_summary,
                "score": item.score,
                "score_reason": item.score_reason,
                "published_at": item.published_at,
            },
            indent=2,
        )

    async def _tool_recommend_knowledge(self, args: dict[str, Any]) -> str:
        user_ctx = get_current_user()
        feed_id = str(args.get("feed_id") or "")
        feeds = (
            [await self._resolve_feed_id(feed_id, user_ctx)] if feed_id
            else await self.list_accessible_feeds(user_ctx)
        )
        feeds = [f for f in feeds if f is not None]
        if not feeds:
            return "No accessible feeds."
        ai = self._ai_capability()
        if ai is None:
            return json.dumps(
                {
                    "recommendations": [
                        {
                            "feed_id": f.id,
                            "recommendation": "disable",
                            "rationale": "AI service not available",
                        }
                        for f in feeds
                    ]
                },
                indent=2,
            )
        feed_summaries: list[dict[str, Any]] = []
        for f in feeds:
            recent = await self.search_items(
                feed_id=f.id, limit=10, user_ctx=user_ctx
            )
            avg_score = (
                sum(it.score for it in recent if it.score >= 0) / max(1, len(recent))
                if recent
                else 0.0
            )
            feed_summaries.append(
                {
                    "feed_id": f.id,
                    "name": f.name,
                    "category": f.category,
                    "current_ingest": f.ingest_to_knowledge,
                    "avg_score": round(avg_score, 2),
                    "items_seen": len(recent),
                }
            )
        try:
            response = await ai.complete_one_shot(
                messages=[
                    Message(
                        role=MessageRole.USER,
                        content=json.dumps(
                            {
                                "user_interests": _sanitize_user_text(self._user_interests),
                                "feeds": feed_summaries,
                            },
                            indent=2,
                        ),
                    )
                ],
                system_prompt=self._knowledge_recommendation_prompt,
                profile_name=self._scoring_ai_profile,
                tools_override=[],
            )
            text = _strip_json_fences((response.message.content or "").strip())
            json.loads(text)  # validate
            return text
        except Exception:
            return json.dumps(
                {
                    "recommendations": [
                        {
                            "feed_id": f.id,
                            "recommendation": "disable",
                            "rationale": "AI parse failed; recommend manual review",
                        }
                        for f in feeds
                    ]
                },
                indent=2,
            )

    async def _resolve_feed_id(
        self, raw: str, user_ctx: UserContext
    ) -> Feed | None:
        feeds = await self.list_accessible_feeds(user_ctx)
        for f in feeds:
            if f.id == raw:
                return f
        # Partial match — by id prefix or name substring.
        for f in feeds:
            if f.id.startswith(raw) or raw.lower() in f.name.lower():
                return f
        return None

    async def _resolve_item_id(
        self, raw: str, user_ctx: UserContext
    ) -> StoredFeedItem | None:
        # 'latest' / 'latest <category>' shortcut.
        if raw.lower().startswith("latest"):
            parts = raw.split(maxsplit=1)
            category = parts[1] if len(parts) > 1 else ""
            tops = await self.get_top_items(
                user_ctx, category=category, limit=1
            )
            return tops[0] if tops else None
        item = await self.get_item(raw)
        if item is not None:
            feed = await self.get_feed(item.feed_id)
            if feed is not None and can_access_feed(
                user_ctx, feed, is_admin=self._is_admin(user_ctx)
            ):
                return item
        # Partial id match.
        items = await self.search_items(
            limit=50, user_ctx=user_ctx
        )
        for it in items:
            if it.id.startswith(raw):
                return it
        return None

    # ── WebSocket RPC handlers ───────────────────────────────────────
    #
    # Spec §14. ACL prefix is permissive (``feeds.: 100``); each handler
    # enforces its own per-feed access via ``can_access_feed`` /
    # ``can_admin_feed``. Pattern mirrors ``inbox.get_ws_handlers`` and
    # ``calendar.get_ws_handlers`` — `conn.user_ctx` carries the
    # authenticated user; mutations go through the same public methods
    # as the AI tools so the side-effect surface (events, scheduler,
    # cascade) stays identical.

    def get_ws_handlers(self) -> dict[str, Any]:
        return {
            # Feeds
            "feeds.list": self._ws_feeds_list,
            "feeds.get": self._ws_feeds_get,
            "feeds.create": self._ws_feeds_create,
            "feeds.update": self._ws_feeds_update,
            "feeds.delete": self._ws_feeds_delete,
            "feeds.test": self._ws_feeds_test,
            "feeds.share_user": self._ws_feeds_share_user,
            "feeds.unshare_user": self._ws_feeds_unshare_user,
            "feeds.share_role": self._ws_feeds_share_role,
            "feeds.unshare_role": self._ws_feeds_unshare_role,
            "feeds.poll_now": self._ws_feeds_poll_now,
            # Items
            "feeds.items.list": self._ws_items_list,
            "feeds.items.get": self._ws_items_get,
            "feeds.items.mark": self._ws_items_mark,
            "feeds.items.delete": self._ws_items_delete,
            "feeds.items.reingest": self._ws_items_reingest,
            # Briefing
            "feeds.briefing.preview": self._ws_briefing_preview,
            "feeds.briefing.run": self._ws_briefing_run,
            "feeds.briefing.get": self._ws_briefing_get,
            # OPML / backends
            "feeds.import_opml": self._ws_import_opml,
            "feeds.export_opml": self._ws_export_opml,
            "feeds.backends.list": self._ws_backends_list,
        }

    @staticmethod
    def _err(frame: dict[str, Any], msg: str, code: int) -> dict[str, Any]:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": msg, "code": code}

    @staticmethod
    def _ok(frame: dict[str, Any], frame_type: str, **payload: Any) -> dict[str, Any]:
        return {"type": frame_type, "ref": frame.get("id"), **payload}

    def _feed_payload(self, feed: Feed, user_ctx: UserContext) -> dict[str, Any]:
        is_admin = self._is_admin(user_ctx)
        access = determine_feed_access(user_ctx, feed, is_admin=is_admin)
        return {
            "id": feed.id,
            "name": feed.name,
            "url": feed.url,
            "backend_name": feed.backend_name,
            "backend_config": dict(feed.backend_config),
            "owner_user_id": feed.owner_user_id,
            "shared_with_users": list(feed.shared_with_users),
            "shared_with_roles": list(feed.shared_with_roles),
            "category": feed.category,
            "importance_weight": feed.importance_weight,
            "ingest_to_knowledge": feed.ingest_to_knowledge,
            "briefing_eligible": feed.briefing_eligible,
            "poll_enabled": feed.poll_enabled,
            "poll_interval_sec": feed.poll_interval_sec,
            "suggested_poll_interval_sec": feed.suggested_poll_interval_sec,
            "effective_poll_interval_sec": feed.effective_poll_interval_sec(),
            "last_polled_at": feed.last_polled_at,
            "last_poll_status_code": feed.last_poll_status_code,
            "last_poll_items_total": feed.last_poll_items_total,
            "last_poll_items_new": feed.last_poll_items_new,
            "last_poll_duration_ms": feed.last_poll_duration_ms,
            "consecutive_failures": feed.consecutive_failures,
            "last_error": feed.last_error,
            "created_at": feed.created_at,
            "access": access.value if access is not None else None,
            "can_admin": can_admin_feed(user_ctx, feed, is_admin=is_admin),
        }

    def _item_payload(self, item: StoredFeedItem) -> dict[str, Any]:
        return {
            "id": item.id,
            "feed_id": item.feed_id,
            "item_uid": item.item_uid,
            "title": item.title,
            "link": item.link,
            "summary": item.summary,
            "ai_summary": item.ai_summary,
            "author": item.author,
            "score": item.score,
            "score_reason": item.score_reason,
            "lazy_score": getattr(item, "lazy_score", False),
            "read": item.read,
            "briefed_at": item.briefed_at,
            "ingested_to_knowledge": item.ingested_to_knowledge,
            "published_at": item.published_at,
            "received_at": item.received_at,
            "enclosure_url": item.enclosure_url,
            "enclosure_mime": item.enclosure_mime,
        }

    async def _unread_count(self, feed_id: str) -> int:
        """Count of unread items on a feed (used for list payload)."""
        rows = await self._storage.query(
            Query(
                collection=_FEED_ITEMS_COLLECTION,
                filters=[
                    Filter(field="feed_id", op=FilterOp.EQ, value=feed_id),
                    Filter(field="read", op=FilterOp.EQ, value=False),
                ],
            )
        )
        return len(rows)

    # ---- Feeds ----

    async def _ws_feeds_list(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        user_ctx = conn.user_ctx
        feeds = await self.list_accessible_feeds(user_ctx)
        out: list[dict[str, Any]] = []
        for f in feeds:
            payload = self._feed_payload(f, user_ctx)
            payload["unread_count"] = await self._unread_count(f.id)
            out.append(payload)
        return self._ok(frame, "feeds.list.result", feeds=out)

    async def _ws_feeds_get(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        feed_id = str(frame.get("feed_id") or "")
        try:
            feed = await self._require_feed(feed_id)
        except FeedNotFoundError:
            return self._err(frame, "Feed not found", 404)
        try:
            self._require_access(feed, conn.user_ctx)
        except FeedsPermissionError:
            return self._err(frame, "Forbidden", 403)
        payload = self._feed_payload(feed, conn.user_ctx)
        payload["unread_count"] = await self._unread_count(feed.id)
        return self._ok(frame, "feeds.get.result", feed=payload)

    async def _ws_feeds_create(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        url = str(frame.get("url") or "")
        if not url:
            return self._err(frame, "url is required", 400)
        backend_name = str(frame.get("backend_name") or "rss_atom")
        name = str(frame.get("name") or "")
        category = str(frame.get("category") or "")
        poll_interval = int(frame.get("poll_interval_sec") or 0)
        try:
            feed = await self.subscribe(
                url=url,
                user_ctx=conn.user_ctx,
                name=name,
                category=category,
                backend_name=backend_name,
                poll_interval_sec=poll_interval,
            )
        except FeedError as exc:
            return self._err(frame, str(exc), 400)
        return self._ok(
            frame,
            "feeds.create.result",
            feed=self._feed_payload(feed, conn.user_ctx),
        )

    async def _ws_feeds_update(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        feed_id = str(frame.get("feed_id") or "")
        updates = frame.get("updates") or {}
        if not isinstance(updates, dict):
            return self._err(frame, "updates must be an object", 400)
        try:
            feed = await self.update_feed(feed_id, updates, conn.user_ctx)
        except FeedsPermissionError as exc:
            return self._err(frame, str(exc), 403)
        except FeedNotFoundError as exc:
            return self._err(frame, str(exc), 404)
        return self._ok(
            frame,
            "feeds.update.result",
            feed=self._feed_payload(feed, conn.user_ctx),
        )

    async def _ws_feeds_delete(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        feed_id = str(frame.get("feed_id") or "")
        try:
            await self.unsubscribe(feed_id, conn.user_ctx)
        except FeedsPermissionError as exc:
            return self._err(frame, str(exc), 403)
        except FeedNotFoundError as exc:
            return self._err(frame, str(exc), 404)
        return self._ok(frame, "feeds.delete.result", status="ok")

    async def _ws_feeds_test(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        url = str(frame.get("url") or "")
        backend_name = str(frame.get("backend_name") or "rss_atom")
        if not url:
            return self._err(frame, "url is required", 400)
        backends = FeedBackend.registered_backends()
        backend_cls = backends.get(backend_name)
        if backend_cls is None:
            return self._err(frame, f"Unknown backend: {backend_name}", 400)
        backend = backend_cls()
        try:
            await backend.initialize({})
            try:
                meta = await backend.probe(url)
            finally:
                await backend.close()
        except FeedError as exc:
            return self._err(frame, str(exc), 400)
        return self._ok(
            frame,
            "feeds.test.result",
            title=meta.title,
            description=meta.description,
            link=meta.link,
        )

    async def _ws_feeds_share_helper(
        self,
        conn: Any,
        frame: dict[str, Any],
        *,
        share: bool,
        share_role: bool,
    ) -> dict[str, Any]:
        feed_id = str(frame.get("feed_id") or "")
        target = str(frame.get("role") or frame.get("user_id") or "")
        result_verb = (
            ("share_role" if share_role else "share_user")
            if share
            else ("unshare_role" if share_role else "unshare_user")
        )
        try:
            method = (
                self.share_role
                if (share and share_role)
                else self.unshare_role
                if (not share and share_role)
                else self.share_user
                if (share and not share_role)
                else self.unshare_user
            )
            feed = await method(feed_id, target, conn.user_ctx)
        except FeedsPermissionError as exc:
            return self._err(frame, str(exc), 403)
        except FeedNotFoundError as exc:
            return self._err(frame, str(exc), 404)
        return self._ok(
            frame,
            f"feeds.{result_verb}.result",
            feed=self._feed_payload(feed, conn.user_ctx),
        )

    async def _ws_feeds_share_user(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        return await self._ws_feeds_share_helper(conn, frame, share=True, share_role=False)

    async def _ws_feeds_unshare_user(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        return await self._ws_feeds_share_helper(conn, frame, share=False, share_role=False)

    async def _ws_feeds_share_role(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        return await self._ws_feeds_share_helper(conn, frame, share=True, share_role=True)

    async def _ws_feeds_unshare_role(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        return await self._ws_feeds_share_helper(conn, frame, share=False, share_role=True)

    async def _ws_feeds_poll_now(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        feed_id = str(frame.get("feed_id") or "")
        try:
            feed = await self._require_feed(feed_id)
        except FeedNotFoundError:
            return self._err(frame, "Feed not found", 404)
        try:
            self._require_admin(feed, conn.user_ctx)
        except FeedsPermissionError as exc:
            return self._err(frame, str(exc), 403)
        runtime = self._runtimes.get(feed.id)
        if runtime is None:
            return self._err(frame, "Feed runtime not started", 409)
        prev_total = feed.last_poll_items_total
        prev_new = feed.last_poll_items_new
        try:
            await self._poll_runtime(runtime)
        except Exception as exc:  # noqa: BLE001
            return self._err(frame, f"Poll failed: {exc}", 500)
        refreshed = await self.get_feed(feed.id)
        if refreshed is None:
            return self._err(frame, "Feed disappeared during poll", 500)
        return self._ok(
            frame,
            "feeds.poll_now.result",
            items_seen=refreshed.last_poll_items_total,
            items_new=refreshed.last_poll_items_new,
            error=refreshed.last_error,
            previous_items_total=prev_total,
            previous_items_new=prev_new,
        )

    # ---- Items ----

    async def _ws_items_list(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        user_ctx = conn.user_ctx
        feed_id_raw = frame.get("feed_id")
        feed_id = str(feed_id_raw) if feed_id_raw else None
        items = await self.search_items(
            feed_id=feed_id,
            query=str(frame.get("query") or ""),
            unread_only=bool(frame.get("unread_only") or False),
            min_score=float(frame.get("min_score") or 0.0),
            category=str(frame.get("category") or ""),
            limit=int(frame.get("limit") or 50),
            page=int(frame.get("page") or 1),
            user_ctx=user_ctx,
        )
        return self._ok(
            frame,
            "feeds.items.list.result",
            items=[self._item_payload(i) for i in items],
            total=len(items),
        )

    async def _ws_items_get(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        item_id = str(frame.get("item_id") or "")
        item = await self.get_item(item_id)
        if item is None:
            return self._err(frame, "Item not found", 404)
        try:
            feed = await self._require_feed(item.feed_id)
            self._require_access(feed, conn.user_ctx)
        except FeedsPermissionError:
            return self._err(frame, "Forbidden", 403)
        except FeedNotFoundError:
            return self._err(frame, "Owning feed not found", 404)
        return self._ok(frame, "feeds.items.get.result", item=self._item_payload(item))

    async def _ws_items_mark(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        item_id = str(frame.get("item_id") or "")
        read = bool(frame.get("read", True))
        try:
            await self.mark_read(item_id, conn.user_ctx, read=read)
        except FeedsPermissionError as exc:
            return self._err(frame, str(exc), 403)
        except FeedNotFoundError as exc:
            return self._err(frame, str(exc), 404)
        return self._ok(frame, "feeds.items.mark.result", status="ok", read=read)

    async def _ws_items_delete(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        item_id = str(frame.get("item_id") or "")
        item = await self.get_item(item_id)
        if item is None:
            return self._err(frame, "Item not found", 404)
        try:
            feed = await self._require_feed(item.feed_id)
            self._require_admin(feed, conn.user_ctx)
        except FeedsPermissionError as exc:
            return self._err(frame, str(exc), 403)
        except FeedNotFoundError as exc:
            return self._err(frame, str(exc), 404)
        # Cascade knowledge entry if previously ingested.
        if item.ingested_to_knowledge and self._knowledge is not None:
            with contextlib.suppress(Exception):
                await self._knowledge.remove_document(
                    _doc_id_for(
                        self._feed_doc_backend.source_id,
                        feed.id,
                        item.item_uid,
                    )
                )
        await self._storage.delete(_FEED_ITEMS_COLLECTION, item.id)
        return self._ok(frame, "feeds.items.delete.result", status="ok")

    async def _ws_items_reingest(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        item_id = str(frame.get("item_id") or "")
        item = await self.get_item(item_id)
        if item is None:
            return self._err(frame, "Item not found", 404)
        try:
            feed = await self._require_feed(item.feed_id)
            self._require_admin(feed, conn.user_ctx)
        except FeedsPermissionError as exc:
            return self._err(frame, str(exc), 403)
        except FeedNotFoundError as exc:
            return self._err(frame, str(exc), 404)
        try:
            await self._ingest_item(feed, item)
        except Exception as exc:  # noqa: BLE001
            return self._err(frame, f"Ingest failed: {exc}", 500)
        return self._ok(frame, "feeds.items.reingest.result", status="ok")

    # ---- Briefing ----

    async def _ws_briefing_preview(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        # Dry-run — does NOT mark briefed and does NOT publish events.
        result = await self.build_briefing(
            conn.user_ctx,
            top_n=int(frame.get("top_n", 5) or 5),
            category=str(frame.get("category") or ""),
            mark_briefed=False,
        )
        return self._ok(
            frame,
            "feeds.briefing.preview.result",
            spoken=result.spoken,
            headlines=[h.to_dict() for h in result.headlines],
            item_ids=result.item_ids,
            since=result.since.isoformat(),
            briefing_id=result.briefing_id,
        )

    async def _ws_briefing_run(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        # Caller may target another user only as admin; otherwise self.
        target_user_id = str(frame.get("user_id") or "") or conn.user_ctx.user_id
        if target_user_id != conn.user_ctx.user_id and not self._is_admin(conn.user_ctx):
            return self._err(frame, "Only admins may run briefings for other users", 403)
        force = bool(frame.get("force") or False)
        # Idempotency: skip-when-already-briefed unless force.
        if not force:
            state = await self.get_briefing_state(target_user_id)
            today = _now_utc().strftime("%Y-%m-%d")
            if state.get("last_briefed_on") == today and state.get("last_briefing_id"):
                cached = await self.get_briefing(str(state.get("last_briefing_id", "")))
                if cached:
                    return self._ok(
                        frame,
                        "feeds.briefing.run.result",
                        briefing_id=str(state.get("last_briefing_id")),
                        spoken=str(cached.get("spoken", "")),
                        headlines=cached.get("headlines") or [],
                        item_ids=cached.get("item_ids") or [],
                        cached=True,
                    )
        # Build a UserContext for the target — admin path can target another.
        if target_user_id == conn.user_ctx.user_id:
            user_ctx = conn.user_ctx
        else:
            user_ctx = UserContext(
                user_id=target_user_id,
                email="",
                display_name=target_user_id,
                roles=frozenset({"user"}),
            )
        result = await self.build_briefing(
            user_ctx,
            top_n=int(frame.get("top_n", 5) or 5),
            category=str(frame.get("category") or ""),
            mark_briefed=True,
        )
        await self._publish_event(
            "feed.briefing.ready",
            {
                "user_id": target_user_id,
                "briefing_id": result.briefing_id,
                "item_count": len(result.item_ids),
                "since": result.since.isoformat(),
            },
        )
        return self._ok(
            frame,
            "feeds.briefing.run.result",
            briefing_id=result.briefing_id,
            spoken=result.spoken,
            headlines=[h.to_dict() for h in result.headlines],
            item_ids=result.item_ids,
            cached=False,
        )

    async def _ws_briefing_get(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        briefing_id = str(frame.get("briefing_id") or "")
        if not briefing_id:
            return self._err(frame, "briefing_id is required", 400)
        cached = await self.get_briefing(briefing_id)
        if cached is None:
            return self._err(frame, "Briefing not found", 404)
        # Per-user gate: only the recipient or an admin may read.
        if (
            cached.get("user_id") != conn.user_ctx.user_id
            and not self._is_admin(conn.user_ctx)
        ):
            return self._err(frame, "Forbidden", 403)
        return self._ok(
            frame,
            "feeds.briefing.get.result",
            briefing_id=briefing_id,
            spoken=str(cached.get("spoken", "")),
            headlines=cached.get("headlines") or [],
            item_ids=cached.get("item_ids") or [],
            since=str(cached.get("since", "")),
        )

    # ---- OPML / backends ----

    async def _ws_import_opml(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        # Admin-only — OPML import is a bulk subscribe and the spec
        # restricts it to operators (§14).
        if not self._is_admin(conn.user_ctx):
            return self._err(frame, "Admin role required", 403)
        opml_text = str(frame.get("opml") or "")
        if not opml_text:
            return self._err(frame, "opml body is required", 400)
        results = await self.import_opml(opml_text, conn.user_ctx)
        return self._ok(
            frame,
            "feeds.import_opml.result",
            results=[{"url": url, "error": err} for url, err in results],
        )

    async def _ws_export_opml(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        if not self._is_admin(conn.user_ctx):
            return self._err(frame, "Admin role required", 403)
        opml = await self.export_opml(conn.user_ctx)
        return self._ok(frame, "feeds.export_opml.result", opml=opml)

    async def _ws_backends_list(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        backends: list[dict[str, Any]] = []
        for name, cls in FeedBackend.registered_backends().items():
            params = [
                p.to_wire_dict(backend_param=True)
                for p in cls.backend_config_params()
            ]
            backends.append({"name": name, "config_params": params})
        return self._ok(frame, "feeds.backends.list.result", backends=backends)


# ── Helpers ───────────────────────────────────────────────────────────


def _xml_escape(value: str) -> str:
    return (
        (value or "")
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _parse_since(arg: str) -> datetime:
    """Accept '24h', '7d', or a full ISO datetime."""
    if not arg:
        return _now_utc() - timedelta(hours=24)
    arg = arg.strip()
    if arg.endswith("h") and arg[:-1].isdigit():
        return _now_utc() - timedelta(hours=int(arg[:-1]))
    if arg.endswith("d") and arg[:-1].isdigit():
        return _now_utc() - timedelta(days=int(arg[:-1]))
    parsed = _parse_iso(arg)
    if parsed is not None:
        return parsed
    return _now_utc() - timedelta(hours=24)


def _briefing_tool_output(cached: dict[str, Any]) -> ToolOutput:
    spoken = str(cached.get("spoken", "") or "")
    headlines_raw = cached.get("headlines") or []
    elements: list[UIElement] = [
        UIElement(type="label", name="spoken", label=spoken),
    ]
    if isinstance(headlines_raw, list):
        for idx, h in enumerate(headlines_raw):
            if not isinstance(h, dict):
                continue
            line = (
                f"{h.get('title', '')} — {h.get('one_liner', '')}"
                f" (score={float(h.get('score', 0.0)):.2f})"
            )
            elements.append(
                UIElement(
                    type="label",
                    name=f"headline_{idx}",
                    label=line,
                )
            )
    block = UIBlock(
        title="Today's briefing",
        elements=elements,
        tool_name="news_briefing",
    )
    return ToolOutput(text=spoken, ui_blocks=[block])


# Make sure FeedsService satisfies FeedsProvider at import time.
# Defensive sanity check — not exposed publicly.
def _check_protocol() -> None:
    svc: FeedsProvider = FeedsService()
    _ = svc
