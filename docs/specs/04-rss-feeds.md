# Feature 04 — RSS / News Feeds Service

**Status:** Draft (specification only — not yet implemented).
**Owner:** Core team.
**Closest analog in the codebase:** `InboxService` (per-user external content
service that polls remote sources, persists items, fans out events, and
exposes AI tools). Re-read `core/services/inbox.py` and the
[Inbox Service memory](../../.claude/memory/memory-inbox-service.md) before
implementing — the service shape, runtime registry, scheduler integration,
sharing model, and event taxonomy here are all deliberate copies of that
design with the email-specific bits replaced by feed-specific bits.

---

## 1. User-facing pitch

Gilbert subscribes to RSS / Atom feeds on behalf of its users, polls them on
a configurable cadence, deduplicates items, scores each one for importance
against a user-tunable AI prompt, and surfaces the top items in a **daily
briefing** that the greeting service can read aloud over speakers in the
morning. Users can ask Gilbert to subscribe to a new feed in chat, ask
"what's important today?", search across their feeds, or have items
ingested into the knowledge service for vector search.

The headline UX moment: 7:00 AM, Gilbert greets you when presence detects
your arrival, and after the personalized greeting it says *"Three stories
worth your attention today: the SEC filed against …, the city council
voted on …, and your favorite open-source project just shipped 2.0."*

## 2. Scope

### In scope (this feature)

- A new core service, `FeedsService`, modeled on `InboxService`.
- A new backend ABC, `FeedBackend`, modeled on `EmailBackend`.
- A built-in concrete backend, `RssAtomFeedBackend`, in
  `src/gilbert/integrations/rss_feeds.py`. The only third-party dep is
  `feedparser`. This passes the `integrations/` tier test laid out in
  §3 ("Note on `integrations/` precedent"): permissive-licensed, small,
  stdlib-only transitive deps, parses an open standard with no vendor
  specifics. **No vendor lock-in lives in core**, so this is the one
  feed backend that stays in `integrations/`.
- Per-user feed subscriptions (`feeds` collection) and per-item state
  (`feed_items` collection), owner-scoped with role/user sharing — exactly
  the same authorization shape as mailboxes.
- Polling scheduler integration: one job per `poll_enabled` feed, default
  cadence 30 minutes, configurable per-feed.
- AI tools: `news_briefing`, `search_feeds`, `summarize_feed`,
  `subscribe_feed`, `unsubscribe_feed`, `list_feeds`, `read_feed_item`,
  `recommend_knowledge_ingestion`. (See §10. `subscribe_feed` and
  `unsubscribe_feed` are confirmation-block tools — they do NOT persist
  directly.)
- Optional knowledge ingestion per-feed (a flag on the `Feed` row).
- Importance scoring per-item via `AISamplingProvider.complete_one_shot`,
  driven by a configurable `ai_prompt=True` system prompt.
- Daily briefing pipeline that emits a `feed.briefing.ready` event for the
  greeting service and TTS to consume.
- Web UI at `/feeds` — feed list, subscription editor, item list, and a
  briefing preview. Settings page integration via the standard
  `Configurable` protocol.

### Future-but-enabled-by-design (NOT specified here)

- A `RedditFeedBackend` plugin under `std-plugins/reddit/`.
- A `HackerNewsFeedBackend` plugin under `std-plugins/hackernews/`.
- A YouTube-channel backend plugin.
- A podcast (RSS-with-audio-enclosures) backend plugin that wires into the
  speaker / music service for "play me the latest episode."
- These are explicitly out of scope for this spec — the `FeedBackend` ABC
  must be designed so they slot in without core changes, but **the
  implementer should not write them in this PR**.

### Explicitly out of scope

- **Newsletter parsing** (substack-style HTML emails). That's already
  inbox-shaped and belongs in `InboxService` if/when it happens.
- **Paywalled-content fetching.** RSS items typically include a
  description / summary; we surface that. Following the link to a
  paywalled article and bypassing the paywall is a separate, ethically
  fraught feature.
- **Comment threads** (Reddit comments, HN comments, blog comments). The
  per-backend plugins may surface top-level items only; comments are out.
- **A full RSS reader UI** (three-pane like NetNewsWire / Feedly). v1
  ships a simple list + detail and a settings page — that's enough to
  configure feeds and inspect what Gilbert saw. A power-user reader is v2
  and not part of this spec.

## 3. Architectural fit

### Layer placement

| Concern | Location | Rationale |
|---|---|---|
| `FeedBackend` ABC | `src/gilbert/interfaces/feeds.py` | Pure abstraction. Imports only stdlib + other interfaces. |
| `Feed`, `FeedItem`, auth helpers, `FeedsProvider` protocol | `src/gilbert/interfaces/feeds.py` | Shared data + capability protocol live in `interfaces/`. |
| `RssAtomFeedBackend` (built-in) | `src/gilbert/integrations/rss_feeds.py` | Vendor-free dep (`feedparser`). Same tier as `LocalAuth` and `LocalDocuments`. |
| `FeedsService` | `src/gilbert/core/services/feeds.py` | Discoverable service. Owns subscriptions, items, scoring, summarization, and `build_briefing` (the briefing-text builder). |
| `FeedBriefingService` (daily fan-out + event publisher) | `src/gilbert/core/services/feed_briefing.py` | Thin scheduler + event publisher. Calls `FeedsProvider.build_briefing` and emits `feed.briefing.ready`. No prompt config, no AI calls of its own. See §13. |
| Synthetic feed-articles `DocumentBackend` | `src/gilbert/integrations/feed_documents.py` | Owned privately by `FeedsService` (NOT registered with `KnowledgeService`). Read-only file backend over `.gilbert/feed-cache/`. |
| Web UI | `frontend/src/components/feeds/` | Core SPA, mirrors `frontend/src/components/inbox/`. |
| Reddit / HN plugins | `std-plugins/reddit/`, `std-plugins/hackernews/` | **Not built in this PR.** |

### Note on `integrations/` precedent (`feedparser` justification)

`feedparser` is the first third-party Python dep introduced into
`src/gilbert/integrations/`. The current residents (`LocalAuth`,
`LocalDocuments`) are zero-dep. The rule the codebase actually enforces
is "vendor-free + small + provider-neutral," which `feedparser` clears:

- **Small.** ~150KB on disk; only stdlib transitive deps (`sgmllib3k`).
- **Permissive license.** BSD.
- **Provider-neutral.** Parses RSS 2.0 / Atom 1.0, both published by
  working groups / standards bodies. There is no "RSS company" to
  couple to.
- **Out-of-the-box value.** A user running Gilbert with no plugins
  enabled can still subscribe to any blog. RSS works on any URL,
  unlike "generic IMAP" which still needs server credentials per
  provider — that's why `EmailBackend` has no in-core implementation.

Anything with provider-specific behavior or heavyweight transitive
deps still belongs in `std-plugins/`. This sentence exists so the next
"my dep is small too" PR has a clear test to pass or fail.

### Capabilities (service registry)

`FeedsService` declares (note: `feed.briefing.ready` is published by
`FeedBriefingService`, not `FeedsService` — `FeedsService` only owns
the briefing *text builder*):

```python
ServiceInfo(
    name="feeds",
    capabilities=frozenset({"feeds", "ai_tools", "ws_handlers"}),
    requires=frozenset({"entity_storage", "scheduler"}),
    optional=frozenset({
        "event_bus",
        "knowledge",          # ingestion target (optional per-feed flag)
        "configuration",
        "access_control",
        "ai_chat",            # importance scoring, summarization, briefing
    }),
    events=frozenset({
        "feed.item.received",
        "feed.item.scored",
        "feed.subscription.created",
        "feed.subscription.updated",
        "feed.subscription.deleted",
        "feed.subscription.shares.changed",
        "feed.subscription.disabled",
        "feed.ingest.throttled",
    }),
    ai_calls=frozenset({"feed_score", "feed_summarize", "feed_briefing"}),
    toggleable=True,
    toggle_description="RSS / news feed polling, scoring, and briefing builder",
)
```

`FeedBriefingService` declares (thin: just the daily schedule + event
publisher, no prompts of its own):

```python
ServiceInfo(
    name="feed_briefing",
    capabilities=frozenset({"feed_briefing"}),
    requires=frozenset({"feeds", "scheduler"}),
    optional=frozenset({"event_bus", "configuration", "speaker_control",
                        "access_control"}),
    events=frozenset({"feed.briefing.ready"}),
    ai_calls=frozenset(),  # build_briefing's AI call is owned by FeedsService
    toggleable=True,
    toggle_description="Morning news briefing fan-out and event publication",
)
```

### Registration in `app.py`

After `InboxService`:

```python
from gilbert.core.services.feeds import FeedsService
from gilbert.core.services.feed_briefing import FeedBriefingService

self.service_manager.register(FeedsService())
self.service_manager.register(FeedBriefingService())
```

The `RssAtomFeedBackend` is registered via a side-effect import inside
`FeedsService.start()`:

```python
try:
    import gilbert.integrations.rss_feeds  # noqa: F401
except ImportError:
    logger.warning("feedparser not installed — RSS feeds disabled")
```

Plugin backends (Reddit, HN, when they exist) register themselves via
their own `plugin.py`'s `setup()` import.

## 4. Data model

Three entity collections, all owned by `FeedsService`. Same shape as the
inbox triple (mailboxes, messages, outbox) — but no outbox-equivalent
because feeds are read-only.

### `feeds` (collection)

The subscription / source. One row per subscribed feed.

| Field | Type | Notes |
|---|---|---|
| `id` | `str` | `feed_<uuid12>`. |
| `name` | `str` | User-given display name. Defaults to the feed's `<title>` when subscribing. |
| `url` | `str` | The feed URL (validated to look like http(s)). |
| `backend_name` | `str` | `"rss_atom"` for the built-in. |
| `backend_config` | `dict[str, Any]` | Backend-specific knobs (e.g. HTTP basic auth on a private feed, etag/cache policy). |
| `owner_user_id` | `str` | Set at creation, immutable. |
| `shared_with_users` | `list[str]` | Identical sharing model to mailboxes. |
| `shared_with_roles` | `list[str]` | Identical. |
| `poll_enabled` | `bool` | Default `True`. |
| `poll_interval_sec` | `int` | Default `1800` (30 min). Min `60`, max `86400`. |
| `category` | `str` | User-supplied free-form tag (e.g. `"tech"`, `"local"`, `"finance"`). Used by the briefing prompt. |
| `importance_weight` | `float` | `0.0–1.0`, default `0.5`. Multiplied into the AI score so users can boost feeds they care more about without having to retune the prompt. |
| `ingest_to_knowledge` | `bool` | Default `False`. When `True`, new items also go to the knowledge service for vector indexing. |
| `briefing_eligible` | `bool` | Default `True`. When `False`, items don't show up in the daily briefing (still searchable). |
| `last_polled_at` | `str` (ISO) | Updated on each successful poll *and* on 304 Not Modified. |
| `last_error` | `str` | Last poll error message (empty on success). Useful for the UI. |
| `consecutive_failures` | `int` | Used by the back-off logic in §6.5. |
| `last_poll_status_code` | `int` | HTTP status of the most recent fetch (0 if never polled). |
| `last_poll_items_total` | `int` | Items in the most recent feed body (post-decode). |
| `last_poll_items_new` | `int` | New (post-dedup) items the most recent poll persisted. |
| `last_poll_duration_ms` | `int` | Wall-clock duration of the most recent poll. |
| `http_cache` | `dict[str, str]` | Backend bookkeeping (etag, last_modified, last_fetch_hash). **Not user-editable.** Distinct from `backend_config`. The UI never renders this. |
| `suggested_poll_interval_sec` | `int` | Latest `<ttl>` / `Cache-Control: max-age` hint from the source. Effective cadence is `max(poll_interval_sec, suggested_poll_interval_sec)` (§6.6). |
| `created_at` | `str` (ISO) | Timestamp. |

Indexes:

- `feeds(owner_user_id)`
- `feeds(poll_enabled)` — used by the boot loader to find runtimes to start.

### `feed_items` (collection)

One row per item we've ever seen in any feed.

| Field | Type | Notes |
|---|---|---|
| `_id` | `str` | Globally unique `<feed_id>__<item_uid>` (double-underscore separator — see §6.4 note; `:` collides with URL-shaped fallback uids). |
| `feed_id` | `str` | The owning feed. |
| `item_uid` | `str` | The feed-defined GUID / id. Used to dedup. |
| `title` | `str` | Item title. |
| `link` | `str` | Canonical URL to the article. |
| `author` | `str` | If supplied. |
| `published_at` | `str` (ISO) | Best-effort from the feed; fallback to "received_at". |
| `updated_at` | `str` (ISO) | `<atom:updated>` if present, else equal to `published_at`. Used by the edit-detection path in §6.4a. |
| `received_at` | `str` (ISO) | When *Gilbert* first saw the item. |
| `summary` | `str` | The feed's `<description>` / `<summary>` field. **Kept as plain text, capped by `max_summary_length` (default 4000 chars).** |
| `ai_summary` | `str` | Optional AI-generated summary (only when `summarize_on_ingest=True` is set on the service or when `summarize_feed` is run on demand). |
| `score` | `float` | AI-assigned importance score, `0.0–1.0`. `-1.0` if not yet scored. |
| `score_reason` | `str` | One-sentence rationale from the scoring AI. |
| `read` | `bool` | Marked-read state. Default `False`. Auto-flipped when `read_feed_item` is called. |
| `briefed_at` | `str` (ISO) | ISO timestamp of the briefing that included this item, or `""` if never briefed. Replaces the boolean `briefed` flag — see §13.1 for the daily-fire vs presence reconciliation. Filtering by "not yet briefed today" becomes a date comparison, which is what the briefing pipeline actually wants. |
| `ingested_to_knowledge` | `bool` | Mirrors success of the optional knowledge call. |
| `enclosure_url` | `str` | First media enclosure URL if any (e.g. podcast MP3). |
| `enclosure_mime` | `str` | Mime type of the enclosure. |

`tags` was considered and **dropped from v1**: free-form AI-suggested
tags lead to tag-explosion (every poll mints novel strings, the search
filter UI dies). If a tag vocabulary is needed later, it'll be a
fixed/curated set fed back into the prompt — out of scope for v1.

Indexes:

- `feed_items(feed_id, received_at)` — listing newest-first per feed.
- `feed_items(feed_id, item_uid)` — dedup lookup.
- `feed_items(read)` — unread queries.
- `feed_items(score)` — top-N for briefing.
- `feed_items(briefed_at)` — briefing-eligibility filter (empty string == "never briefed").

### Storage rule (privacy / size)

**Never persist the full article body.** We store `title + link + summary +
ai_summary + score`. Full content only flows through the system if the user
has flipped `ingest_to_knowledge=True` on a feed, in which case
`FeedsService` hands the bytes to `KnowledgeService.index_document` (see
§9) and immediately discards them locally.

## 5. `FeedBackend` interface

```python
# src/gilbert/interfaces/feeds.py

@dataclass(frozen=True)
class FeedItem:
    """A single item parsed out of a feed."""

    item_uid: str            # feed-defined unique id (guid, atom:id, link as fallback)
    title: str
    link: str
    summary: str             # plain-text summary, no HTML
    author: str = ""
    published_at: datetime | None = None
    enclosure_url: str = ""
    enclosure_mime: str = ""
    raw_metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class FeedMeta:
    """Metadata about the feed itself, returned by ``probe()``."""

    title: str
    description: str
    link: str                # the feed's homepage link, NOT the feed URL
    language: str = ""
    icon_url: str = ""


class FeedBackend(ABC):
    """Abstract feed backend — fetch source.

    The backend is consulted only during ``probe`` (subscribe-time
    metadata fetch) and ``poll`` (periodic item listing). All reads
    after that go through entity storage.

    Concrete backends MUST be deterministic about ``item_uid``: the
    same item must always produce the same uid, or dedup breaks.
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

        Used at subscription time to validate the URL and pre-fill the
        ``Feed.name`` field. Raises ``FeedError`` (defined in this module)
        on any failure — ``FeedsService.subscribe()`` catches and surfaces.
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

        ``since`` is advisory — if the protocol supports conditional GETs
        (RSS does, via ``If-Modified-Since`` and etags) the backend
        SHOULD use it to avoid wasted bytes. The service does the actual
        dedup against entity storage regardless of what the backend
        returns.

        ``http_cache`` is the per-feed bookkeeping dict the service
        round-trips: backends consult ``http_cache.get("etag")`` /
        ``http_cache.get("last_modified")`` to issue conditional GETs
        and return the updated values via ``PollResult.http_cache``.
        Backends MUST NOT touch ``Feed.backend_config`` — that's
        user-supplied settings and a UI save would clobber bookkeeping.
        """
        ...


@dataclass(frozen=True)
class PollResult:
    """What ``FeedBackend.poll`` returns."""

    items: list[FeedItem]
    http_cache: dict[str, str] = field(default_factory=dict)
    suggested_min_interval_sec: int = 0
    """Hint from ``<ttl>`` and/or ``Cache-Control: max-age`` (whichever is
    larger). The service stores it on ``Feed.suggested_poll_interval_sec``
    and uses ``max(feed.poll_interval_sec, suggested)`` as the effective
    cadence. ``0`` means "no hint."""

    not_modified: bool = False
    """True when the source returned 304 Not Modified — ``items`` will be
    empty. The service treats this as success: bumps ``last_polled_at``,
    does NOT bump ``consecutive_failures``."""

    status_code: int = 0
    """HTTP status code (or 0 for non-HTTP backends). Persisted on the
    Feed row for observability."""
```

### Authorization helpers (mirror `interfaces/inbox.py`)

```python
def can_access_feed(user_ctx: UserContext, feed: Feed,
                    *, is_admin: bool = False) -> bool: ...

def can_admin_feed(user_ctx: UserContext, feed: Feed,
                   *, is_admin: bool = False) -> bool: ...

def determine_feed_access(user_ctx: UserContext, feed: Feed,
                          *, is_admin: bool = False) -> FeedAccess | None: ...


class FeedAccess(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    SHARED_USER = "shared_user"
    SHARED_ROLE = "shared_role"
```

Same precedence as mailboxes (owner > admin > shared_user > shared_role).
Sharing = full read access; only owner / admin can edit settings or
delete the subscription.

### `FeedsProvider` capability protocol

```python
@runtime_checkable
class FeedsProvider(Protocol):
    """What plugins / other services consume.

    Resolved via ``resolver.get_capability("feeds")`` and
    ``isinstance``-checked against this protocol — never against the
    concrete ``FeedsService`` class.
    """

    async def subscribe(
        self,
        url: str,
        user_ctx: UserContext,
        *,
        name: str = "",
        category: str = "",
        backend_name: str = "rss_atom",
    ) -> Feed: ...

    async def unsubscribe(self, feed_id: str, user_ctx: UserContext) -> None: ...

    async def list_accessible_feeds(self, user_ctx: UserContext) -> list[Feed]: ...

    async def get_feed(self, feed_id: str) -> Feed | None: ...

    async def search_items(
        self,
        feed_id: str | None = None,
        query: str = "",
        unread_only: bool = False,
        min_score: float = 0.0,
        limit: int = 50,
    ) -> list[FeedItem]: ...

    async def get_top_items(
        self,
        user_ctx: UserContext,
        *,
        category: str = "",
        since: datetime | None = None,
        limit: int = 5,
    ) -> list[FeedItem]: ...

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
```

`build_briefing` lives on `FeedsProvider` (not on a separate
`BriefingProvider`). Rationale: the only callers are `GreetingService`
and `FeedBriefingService`, and the implementation reads from feed
storage, applies feed-scoped policy (`briefing_eligible`, `score`,
`briefed_at`), and uses prompt config that already lives on
`FeedsService`. A second protocol for one method on the same service
the caller already resolved by name was overengineering — see
"Revision Log — Round 2."

`BriefingResult` is the two-artifact return shape (§8.3):

```python
@dataclass(frozen=True)
class BriefingResult:
    spoken: str             # ~60s flowing-paragraph TTS-shaped text
    headlines: list[BriefingHeadline]
    item_ids: list[str]     # order matches headlines
    since: datetime
    briefing_id: str        # opaque id; clients fetch full text via feeds.briefing.get


@dataclass(frozen=True)
class BriefingHeadline:
    item_id: str
    title: str
    one_liner: str
    score: float
    link: str
```

`FeedBriefingService` consumes only `FeedsProvider` — it does not
import `FeedsService` directly and does not own a parallel protocol.

## 6. `FeedsService` runtime

### 6.1 Lifecycle (mirror of `InboxService`)

`FeedsService` keeps a `dict[feed_id, _FeedRuntime]` of per-feed runtimes.
Each runtime owns one `FeedBackend` instance and one scheduler job
named `feeds-poll-{feed_id}`.

```python
@dataclass
class _FeedRuntime:
    feed: Feed
    backend: FeedBackend
    poll_job_name: str = ""
```

`start()`:

1. Resolve required capabilities (`entity_storage`, `scheduler`),
   ensure indexes, load global config (max_summary_length, etc.).
2. Resolve optional capabilities (`event_bus`, `knowledge`,
   `configuration`, `access_control`, `ai_chat`).
3. Schedule a one-shot `feeds-boot` system job that calls
   `_boot_runtimes` — backend `initialize()` may hit the network so we
   defer it off `start()`'s critical path, exactly like inbox.
4. No "shared tick" equivalent — feeds doesn't have an outbox. (If
   §11 ends up adding scoring batching, that goes in
   `_score_tick`, also a system job.)

`stop()`: cancel every poll job, close every backend, clear the runtime
registry.

### 6.2 Restart-on-update field set

`update_feed()` only restarts the per-feed runtime when one of these
fields changes:

- `url`
- `backend_name`
- `backend_config`
- `poll_enabled`
- `poll_interval_sec`

`name`, `category`, `importance_weight`, `ingest_to_knowledge`,
`briefing_eligible`, and share lists do **not** restart the runtime —
they're read at use-time, not at runtime-start time.

### 6.3 Subscribe flow

`FeedsService.subscribe(url, user_ctx, ...)`:

1. Look up the backend class from the registry by `backend_name`.
2. Construct one and call `backend.probe(url)` to validate the URL and
   pre-fill `Feed.name` if the caller didn't supply one.
3. Persist the `Feed` row with `owner_user_id = user_ctx.user_id`.
4. If `poll_enabled` (default), call `_start_runtime(feed)` so polling
   begins immediately.
5. Publish `feed.subscription.created`.
6. Kick off a **first poll** with a small staggered delay — set
   `Schedule.once_after(jitter)` for `feeds-firstpoll-{feed_id}` where
   `jitter` is `random.uniform(0, max_first_poll_jitter_sec)` (default
   30s). For OPML import or any bulk-subscribe path, the service
   spreads first polls across the jitter window so 50 simultaneous
   subscriptions don't fan out into 50 simultaneous fetches. All
   `_poll_runtime` calls — first, scheduled, manual — go through the
   service-wide `_poll_semaphore = asyncio.Semaphore(max_concurrent_polls)`
   regardless. See §6.7.

### 6.4 Polling

The pipeline is: poll → dedup-and-persist → emit `feed.item.received`
with `score=-1.0` immediately → enqueue scoring → enqueue ingestion.
Scoring and ingestion are **decoupled** from the poll loop via async
queues, so a slow AI provider can never block the next poll dispatch.

```python
async def _poll_runtime(self, runtime: _FeedRuntime) -> None:
    feed = runtime.feed
    async with self._poll_semaphore:        # see §6.7
        t0 = time.perf_counter()
        try:
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
        await self._mark_polled_ok(feed, result, items_total=0, items_new=0,
                                   duration_ms=_ms(t0))
        return

    new_items, edited_items = await self._dedup_and_persist(feed, result.items)
    await self._mark_polled_ok(
        feed, result,
        items_total=len(result.items),
        items_new=len(new_items),
        duration_ms=_ms(t0),
    )

    for item in new_items:
        await self._publish_item_received(feed, item)   # score = -1.0
        await self._score_queue.put((feed, item))
        await self._ingest_queue.put((feed, item))

    # Edited items: do not re-emit, do not re-score, do not re-ingest.
    # See §6.4a.
```

#### Dedup and edit detection

`feed_item._id = f"{feed.id}__{item.item_uid}"` (double-underscore so
URL-shaped fallback uids — see §6.4b — don't collide on `:`).

Dedup is **not** short-circuited on first known item. Some Atom feeds
sort by `<atom:updated>` rather than reverse-chronological by
`published`, so the "stop on first known" optimization corrupts dedup
on those feeds. Instead: iterate every item up to `max_items`, do an
`await storage.exists(_FEED_ITEMS_COLLECTION, _id)` per item, and let
the storage backend's index handle the cost. At `max_items=100`, this
is fine.

#### 6.4a Item updates / edit detection

Many blogs publish a draft and refine the title or body within minutes
to days; Atom feeds explicitly carry `<atom:updated>` for this case.

Policy: **first-write-wins on `score`, `briefed_at`, and `read`. Title
and summary are mutable.** Concretely, `_dedup_and_persist` returns two
lists:

- `new_items` — items not previously seen.
- `edited_items` — items whose `_id` already exists AND whose incoming
  `updated_at` is strictly newer than the stored `updated_at` AND
  whose `title` or `summary` differs.

For edited items, update only `title`, `summary`, and `updated_at`.
**Never** re-emit `feed.item.received`, never re-enqueue scoring,
never bump `briefed_at`. The user already saw this story; we just
keep the displayed fields fresh.

#### 6.4b `item_uid` derivation in `RssAtomFeedBackend`

1. Feed-supplied `<guid>` / `<id>` if non-empty.
2. Otherwise `<link>` (with whitespace stripped, lowercased host).
3. Otherwise SHA1 hash of `(<title> + <published_at_iso>)`.

This MUST be deterministic — the dedup key is computed on every poll,
so a non-deterministic uid means duplicate items every poll.

#### 6.4c Async scoring queue

```python
self._score_queue: asyncio.Queue[tuple[Feed, FeedItem]] = asyncio.Queue(maxsize=10000)
self._score_workers: list[asyncio.Task] = []
self._score_semaphore: asyncio.Semaphore   # bounds concurrent AI calls
```

`start()` spawns `max_concurrent_scoring` worker tasks (default `4`,
configurable). Each worker:

1. Awaits an item off the queue.
2. Acquires `_score_semaphore`.
3. Calls `_score_item(feed, item)` — `complete_one_shot` with
   `tools_override=[]`.
4. Persists `score`, `score_reason` back to the row.
5. Emits `feed.item.scored` with `{feed_id, item_id, score, score_reason}`.

`stop()` calls `await self._score_queue.join()` then cancels the
workers. The integration test `test_score_queue_drains_on_stop`
proves this. If the queue is full at `put()` time (chatty feed +
slow AI), drop with a WARNING log and a `feeds_score_drops_total`
counter — the next poll cycle re-encounters new items naturally
through the dedup-on-startup retry path (a `feeds-rescore-tick`
system job sweeps `score == -1.0` items older than 1h and re-enqueues
them, capped at `max_concurrent_scoring * 10` per tick).

`score_on_ingest=False` simply doesn't call `_score_queue.put`. Tools
that need a score on demand (`search_feeds` with `min_score`,
`build_briefing`) use a synchronous fallback that scores the few
items they actually need, bypassing the queue.

#### 6.4d Async ingestion queue

Same shape as the scoring queue. Default `max_concurrent_ingestion=2`
(network-bound, lower default — a body fetch is heavier than a
classification). See §9 for the per-item ingestion logic and the
`ingest_max_items_per_day_per_user` cap.

#### 6.4e First-sync AI cap

When `_boot_runtimes` runs at startup, the `_score_queue` is empty;
once polls fan out, **a fresh install with 20 feeds × ~50 backlog
items each = 1000 scoring calls** queued in seconds. To prevent the
"first day burns the AI budget" experience:

- `initial_score_cap: int = 50` config knob (default 50). The first
  `_boot_runtimes` pass enqueues up to 50 items total across all
  feeds; the rest are persisted with `score=-1.0` and a
  `lazy_score=True` flag picked up the first time a tool reads them.
- After the cap is hit, log at INFO with the count and continue
  polling — the cap only restricts *scoring*, not item collection.

This promotes one of the original §23 open questions to a hard
requirement.

### 6.5 Error handling, back-off, and graceful give-up

When `backend.poll(...)` raises:

1. Set `feed.last_error = str(exc)`, increment `feed.consecutive_failures`.
2. If `consecutive_failures >= 3`, widen the effective poll cadence:

   ```python
   effective_interval = min(86400, base_interval * 2 ** (consecutive_failures - 2))
   ```

   Implementation: `scheduler.remove_job(poll_job_name)` and re-add
   with `effective_interval`. On success, reset `consecutive_failures`
   to `0` and restore the original interval. The remove/re-add window
   is briefly raceable; the test
   `test_backoff_restore_does_not_lose_job` proves a concurrent
   `_poll_runtime` doesn't slip through during the swap (we hold
   `_poll_locks[feed_id]` across the swap).
3. Log at `WARNING`. Don't crash, don't unsubscribe — broken feeds are
   common (transient 503s, intermittent SSL errors, etc.).
4. Surface `last_error`, `consecutive_failures`, and
   `last_poll_status_code` in the WS list RPC so the UI can show an
   error pill on the feed row.
5. **Graceful give-up.** When `consecutive_failures >= 20`, transition
   the feed to `poll_enabled=False`, cancel the scheduler job, emit
   `feed.subscription.disabled` with `{feed_id, reason, last_error}`,
   and surface the disabled state in the UI so the user can re-enable.
   This prevents a forgotten / dead feed from accumulating logspam
   forever.

On HTTP 304 Not Modified (returned via `PollResult.not_modified=True`),
**do not** bump `consecutive_failures`, **do** bump `last_polled_at`,
and **do** persist the round-tripped `http_cache` headers.

### 6.6 Respecting source-supplied cadence (`<ttl>` / `Cache-Control`)

Many feeds publish a polite-poll hint via `<ttl>` (RSS) or
`Cache-Control: max-age` (HTTP). Hammering a feed every 30 minutes
when it says `ttl=720` (12 hours) is rude and gets us rate-limited or
403'd. The backend's `poll()` returns
`PollResult.suggested_min_interval_sec` taken from
`max(<ttl>*60, max-age)`, the service stores it on
`Feed.suggested_poll_interval_sec`, and the **effective cadence** for
re-scheduling is:

```python
effective = max(feed.poll_interval_sec, feed.suggested_poll_interval_sec)
```

The UI surfaces both values so users see when their configured
cadence is being overridden by the source's hint. Users can lower
their configured cadence below the suggestion; the source wins.

### 6.7 Concurrency caps (service-wide semaphores)

Three operational knobs, all `ConfigParam`s on `FeedsService`:

| Key | Default | Purpose |
|---|---|---|
| `max_concurrent_polls` | `8` | Service-wide poll fan-out cap. ALL `_poll_runtime` paths (boot, scheduled, `feeds.poll_now`) acquire `_poll_semaphore` first. The scheduler does not bound concurrency for you. |
| `max_concurrent_scoring` | `4` | Bounds AI scoring workers (§6.4c). |
| `max_concurrent_ingestion` | `2` | Bounds knowledge-body fetches (§9). Heaviest of the three; lowest default. |
| `max_first_poll_jitter_sec` | `30` | First-poll stagger window (§6.3). |

### 6.8 ContextVar / concurrency rules

This service is a singleton. It holds:

- `_runtimes: dict[feed_id, _FeedRuntime]` — keyed by feed_id, fine.
- `_cached_feeds: list[Feed]` — set in `_refresh_cache()` only, read
  via the `cached_feeds` property. Same pattern as
  `InboxService.cached_mailboxes`.

Per-request state (active user, active conversation) is read from
`gilbert.core.context.get_current_user()` and `get_current_conversation_id()`
at the top of each handler. **No `_current_*` attributes on `self`.**

Tool methods receive the caller's identity via `_user_id` / `_user_roles`
/ `_conversation_id` injected by the AI service into `tc.arguments`,
just like every other tool in Gilbert — see
`InboxService._tool_send` for the canonical pattern (and
`memory-multi-user-isolation.md`).

## 7. `RssAtomFeedBackend` (built-in)

`src/gilbert/integrations/rss_feeds.py`:

```python
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.feeds import FeedBackend, FeedError, FeedItem, FeedMeta
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)


class RssAtomFeedBackend(FeedBackend):
    """RSS 2.0 / Atom 1.0 feed backend backed by ``feedparser``."""

    backend_name = "rss_atom"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="user_agent",
                type=ToolParameterType.STRING,
                description=(
                    "User-Agent string sent on feed requests. "
                    "Some hosts rate-limit the default Python UA."
                ),
                default="GilbertFeeds/1.0 (+https://github.com/briandilley/gilbert)",
            ),
            ConfigParam(
                key="basic_auth_user",
                type=ToolParameterType.STRING,
                description="HTTP Basic auth username (optional, for private feeds).",
                default="",
            ),
            ConfigParam(
                key="basic_auth_password",
                type=ToolParameterType.STRING,
                description="HTTP Basic auth password (optional).",
                default="",
                sensitive=True,
            ),
            ConfigParam(
                key="request_timeout_sec",
                type=ToolParameterType.INTEGER,
                description="HTTP timeout per request (seconds).",
                default=15,
            ),
            ConfigParam(
                key="max_response_bytes",
                type=ToolParameterType.INTEGER,
                description=(
                    "Hard cap on feed response body size. Raises "
                    "FeedError if exceeded; counts as a failure."
                ),
                default=10 * 1024 * 1024,  # 10 MiB
            ),
        ]

    def __init__(self) -> None:
        self._client: Any = None  # httpx.AsyncClient — created in initialize()
        self._user_agent = ""
        self._basic_auth: tuple[str, str] | None = None
        self._timeout = 15
        self._max_response_bytes = 10 * 1024 * 1024

    async def initialize(self, config: dict[str, Any] | None = None) -> None: ...

    async def close(self) -> None: ...

    async def probe(self, url: str) -> FeedMeta: ...
    async def poll(self, url: str, *, since: datetime | None = None,
                    max_items: int = 100,
                    http_cache: dict[str, str] | None = None) -> PollResult: ...
```

Implementation notes:

- All `feedparser` calls are CPU/IO-blocking — wrap with
  `asyncio.to_thread`.
- The `httpx.AsyncClient` MUST be constructed in `initialize()` (so
  resource lifetime tracks the runtime), not `__init__`. `close()`
  closes the client.
- The `initialize` signature accepts an injectable client for tests:
  `async def initialize(self, config=None, *, http_client=None)`. Tests
  pass `httpx.AsyncClient(transport=httpx.MockTransport(...))` so we
  never hit the network in CI. (Per CLAUDE.md "don't mock the thing
  you're testing" — we don't mock `feedparser`; we mock the HTTP
  transport.)
- For the actual HTTP fetch, use `httpx.AsyncClient.stream("GET", url)`
  with these required headers:
  - `User-Agent` from backend config.
  - `Accept: application/atom+xml, application/rss+xml, application/xml;q=0.9, */*;q=0.8`.
  - `Accept-Encoding: gzip, deflate` (httpx handles transparently).
  - `If-None-Match: <etag>` if `http_cache.get("etag")`.
  - `If-Modified-Since: <last_modified>` if `http_cache.get("last_modified")`.
- **Body size cap.** Stream the response and abort if `Content-Length`
  exceeds `max_response_bytes` OR if streamed bytes exceed it. Raise
  `FeedError("response too large")`. Counts as a failure.
- **Decoding safety.** Reject `Content-Encoding` chains the backend
  can't decode in bounded memory: gzip/deflate OK; `br`, `zstd`, or
  unknown encodings fail closed with `FeedError`.
- `feedparser.parse(bytes)` then runs on the fetched bytes via
  `asyncio.to_thread`. Don't use `feedparser.parse(url)` — it does
  blocking I/O.
- Honor HTTP 304 Not Modified: when the source returns 304, return
  `PollResult(items=[], not_modified=True, http_cache=existing,
  status_code=304)`. The service uses this to mark `last_polled_at`
  without bumping `consecutive_failures`.
- On 200 OK, derive `suggested_min_interval_sec` from `<ttl>` (RSS,
  in minutes — multiply by 60) and `Cache-Control: max-age` (in
  seconds). Use `max(...)` of whichever are present. `0` if neither.
- Reject obviously-not-a-URL inputs in `probe` (`urlparse(url).scheme
  not in {"http", "https"}` → raise `FeedError`).
- Hard-cap follow-redirects at 5; reject https → http downgrade.
- `FeedError` is defined in `interfaces/feeds.py` and raised by both
  the backend and service-level code so callers can catch one type.

`pyproject.toml` core deps gain a single line: `feedparser>=6.0.10`.

## 8. AI integrations (all configurable prompts)

Per the **AI Prompts Are Always Configurable** rule
([memory](../../.claude/memory/memory-ai-prompts-configurable.md)),
every system prompt below MUST be exposed as a
`ConfigParam(multiline=True, ai_prompt=True)` on **`FeedsService`**
with the bundled string as `default`. (Per the architect-review fix:
all three prompts live on `FeedsService`, the owning service.
`FeedBriefingService` has no prompts of its own.)

### 8.1 Importance scoring (`feed_score`)

Driven by the async scoring queue in §6.4c, not from inside the poll
loop. Each worker calls:

```python
ai_svc.complete_one_shot(
    messages=[Message(role=MessageRole.USER, content=user_msg)],
    system_prompt=self._scoring_prompt,   # NOT _DEFAULT_SCORING_PROMPT
    profile_name=self._scoring_ai_profile,
    tools_override=[],                    # MANDATORY — see voice rules
)
```

`user_msg` includes feed name, category, item title, link, summary,
and the user's free-form "what I care about" preamble pulled from the
feeds service config (`user_interests`). Output is parsed as JSON:

```json
{ "score": 0.0-1.0, "reason": "one sentence" }
```

`tags` was considered and dropped (see §4 data model). If the model
returns a `tags` field, the parser ignores it.

The default scoring prompt (the bundled `_DEFAULT_SCORING_PROMPT`
constant in `feeds.py`) instructs the model to rate
relevance / novelty / importance and to return ONLY JSON. The prompt
description must explicitly say "Output JSON only — wrap in no other
text" because the parser is deliberately strict.

**Tolerant JSON parser.** Cheap models occasionally wrap JSON in
\`\`\`json fences. The parser strips a single leading
`^\`\`\`(?:json)?\n` and trailing `\n\`\`\`$` before
`json.loads`. That single regex saves ~5% of scoring failures across
cheap profiles. Anything beyond that fence-strip is treated as
malformed and falls into the failure path below.

**No identity layer.** `complete_one_shot` does NOT prepend
soul/identity prompts (verified — only `chat()` does that). Scoring is
classification, not personality. Don't change that.

Failures: if scoring raises or the JSON is malformed, set
`item.score = -1.0` (sentinel for "not scored"), log at WARNING, and
move on. The `feeds-rescore-tick` system job (every 30 min) sweeps
items with `score == -1.0 AND received_at > now - 24h` and re-enqueues
them, capped at `max_concurrent_scoring * 10` per tick.

The final stored score is `min(1.0, score * feed.importance_weight)`
— this is the only place `importance_weight` is consumed. (Setting
`importance_weight = 0` effectively "mutes" a feed without disabling it.)

### 8.2 Per-item summarization (`feed_summarize`)

Optional; off by default. Two ways to trigger:

- Service-level config `summarize_on_ingest: bool` (default `False`).
  When `True`, every new item gets summarized as part of poll fan-out.
- AI tool `summarize_feed` (see §10) for ad-hoc.

Same `complete_one_shot` shape; output is plain text capped to 500
chars. Stored in `feed_items.ai_summary`.

### 8.3 Daily briefing (`feed_briefing`) — owned by `FeedsService`

Method: `FeedsProvider.build_briefing(user_ctx, *, top_n=5, since,
category="", max_spoken_seconds=0, mark_briefed=True,
anti_repetition_context=None) -> BriefingResult`.

1. Resolve every feed `user_ctx` can access via
   `list_accessible_feeds`.
2. Filter to `briefing_eligible=True` feeds; if `category != ""`,
   filter to that category.
3. Pull items where `briefed_at == "" AND received_at >= since AND
   score >= 0`, sorted by **recency-decayed score**:

   ```python
   age_hours = (now - received_at).total_seconds() / 3600
   effective_score = score * math.exp(-age_hours / 24)
   ```

   Already filtered to the `since` window; the decay just keeps
   fresher items winning within it.
4. Take top `top_n`.
5. **Two-artifact AI call.** One `complete_one_shot` returns a JSON
   object with both the spoken paragraph and the headline list:

   ```python
   ai_svc.complete_one_shot(
       messages=[Message(role=MessageRole.USER, content=user_msg)],
       system_prompt=self._briefing_prompt,
       profile_name=self._briefing_ai_profile,
       tools_override=[],   # MANDATORY — prevents the "Sonos audio-clip
                            # loop" recursion bug from memory-ai-context-profiles
   )
   ```

   Expected JSON:

   ```json
   {
     "spoken": "Three stories worth your attention today: ...",
     "headlines": [
       {"item_id": "...", "title": "...", "one_liner": "...", "score": 0.82},
       ...
     ]
   }
   ```

   Same fenced-JSON tolerance as §8.1. On parse failure, fall back to
   a non-AI deterministic format ("Top stories: title 1; title 2; …")
   and log at WARNING. **Never** drop the briefing entirely on parse
   failure — TTS still gets *something* worth speaking.
6. **Anti-repetition.** Pull the user's last 10 briefings'
   `spoken` field from a new `feed_briefing_state` collection
   (`user_id` -> last-10 `recent_briefings: list[str]`,
   trimmed FIFO). Pass them to the AI in the user message as
   "yesterday's briefings — be different today." This keeps the voice
   varied across days.
7. If `mark_briefed=True`, set `briefed_at = now.isoformat()` on the
   chosen items. **`mark_briefed=False` is critical** for
   `feeds.briefing.preview` (§14) and for the presence-driven
   greeting flow (§13.1) so a daily fire never eats the items
   greeting will use.
8. Persist a `briefing_id` (`brief_<uuid12>`) and the chosen
   `headlines` to a `briefings` collection so the WS RPC
   `feeds.briefing.get(briefing_id)` can fetch full text without
   sending it through the WS event payload (privacy — see §12).
9. Append the `spoken` text to the user's `recent_briefings` list
   (cap at 10).

`FeedBriefingService` calls this with `mark_briefed=True` for the
daily fan-out. `GreetingService` calls this with `mark_briefed=True`
for the presence-driven path. The presence-vs-daily-fire race is
resolved in §13.1 by making the daily fire conditional on a
`presence_window_elapsed` heuristic — only one of them fires per user
per day.

The default briefing prompt MUST instruct the model to:
- Write a single flowing paragraph suitable for TTS, NOT bullets.
- **Vary tone across days** (witty, warm, dramatic, deadpan, poetic,
  nerdy) — match the persona/voice convention used by
  `GreetingService` (`memory-soul-identity.md`). The user has heard
  yesterday's briefing — be different today.
- Mention items by name with brief context.
- Keep `spoken` under 200 words (or under
  `max_spoken_seconds * 2.5 ≈ words` if `max_spoken_seconds > 0`).
- Return JSON only with `spoken` and `headlines` fields; no prose
  outside the JSON.

The `briefing_prompt` `ConfigParam` (`multiline=True, ai_prompt=True`)
lives on **`FeedsService`** (the owning service that runs the AI
call). `FeedBriefingService` does not own this config knob.

#### Event publication

`FeedBriefingService` (the only daily-fire publisher) emits:

```python
Event(
    event_type="feed.briefing.ready",
    data={
        "user_id": user_ctx.user_id,
        "briefing_id": result.briefing_id,
        "item_count": len(result.item_ids),
        "since": since.isoformat(),
    },
    source="feed_briefing",
)
```

**The event payload deliberately does NOT contain `spoken_text`.**
Briefings can include sensitive items (a SOX-regulated user's
financial filing, an internal company feed). Consumers RPC-fetch the
spoken text via `feeds.briefing.get(briefing_id)` if they need it.
This narrows the privacy posture (the event is logged in WS logs and
server logs; only the briefing-id is exposed there).

A separate consumer (the greeting service, an MQTT publisher, a TTS
hook) subscribes to the event. **`FeedBriefingService` does not call
`speaker_svc.announce` itself by default.** Same briefing text could
be sent to a phone, written to a file for a printed morning paper, or
delivered via Slack — driven purely by who's subscribed.

## 9. Knowledge ingestion

Driven by the async ingestion queue (§6.4d) — never blocks the poll
loop. Per-item flow when `feed.ingest_to_knowledge=True`:

1. Acquire `_ingest_semaphore` (`max_concurrent_ingestion`, default 2).
2. Check the per-user-per-day cap
   (`ingest_max_items_per_day_per_user`, default 200). If exceeded,
   emit `feed.ingest.throttled` with `{user_id, feed_id, current_count,
   cap}` so the UI can show a "you've used your budget" banner. Skip
   this item; rely on retention to drop it from the queue rather than
   carrying it forever.
3. Look up the optional `knowledge` capability via the resolver and
   `isinstance`-check `KnowledgeProvider`. If absent or not started,
   skip silently (don't error — the user may have toggled the flag
   while the knowledge plugin was down).
4. **Politeness check.** Consult `robots.txt` for `item.link`'s host
   using `urllib.robotparser`. Cache the parsed file for 1 hour. If
   `User-Agent` is disallowed, log INFO and skip. (Add config
   `respect_robots_txt: bool = True` so power users can opt out for
   their own private sites.)
5. **Fetch the article body** from `item.link` using `FeedsService`'s
   own `httpx.AsyncClient`:
   - 10-second timeout.
   - 256 KB body cap. If exceeded, skip + log INFO (do NOT
     index half-articles — search relevance breaks).
   - At most 5 redirects.
   - **Reject https → http downgrade.**
   - Reject if final-host eTLD+1 differs from feed-host eTLD+1 by more
     than one hop (mild SSRF guard — a hijacked feed pointing to
     `localhost` shouldn't make us GET `localhost`).
6. **Content-Type guard.** Allow only `text/html` and
   `application/xhtml+xml`. PDFs, images, and binaries are skipped
   with INFO log. (PDF support could be a future enhancement that
   routes to a different ingestion path.)
7. **Paywall heuristic.** Strip HTML to plain text. If extracted text
   is < 1 KB or matches common paywall phrases ("subscribe to read",
   "create a free account", etc. — kept as a bundled regex list),
   skip with INFO log. Otherwise the user's vector search ends up
   surfacing paywall stubs.
8. Cache the bytes to `.gilbert/feed-cache/<feed_id>/<item_uid>.html`
   for observability and debugging.
9. **Hand the bytes to `KnowledgeProvider.index_document(backend,
   meta)`** using the synthetic `feed_articles` `DocumentBackend`
   instance. **The synthetic backend is NOT registered in
   `KnowledgeProvider.backends`.** `FeedsService` owns the instance
   privately; the periodic `knowledge-sync` loop never sees it.
   That's deliberate — feeds are push-on-receive, not pull-on-sync,
   and `KnowledgeService.list_documents` would otherwise re-walk
   `.gilbert/feed-cache/` on every sync and double-handle dedup.
10. Set `feed_item.ingested_to_knowledge = True` on success. On
    failure, leave it `False` and log at WARNING.

**Implementation detail.** The synthetic "feed_articles"
`DocumentBackend` lives at `src/gilbert/integrations/feed_documents.py`
and reads files from `.gilbert/feed-cache/`. `backend_name =
"feed_articles"` registers the class in `DocumentBackend._registry`,
and the **single instance** owned by `FeedsService` sets
`source_id = "feed_articles"`. The class's `list_documents()` returns
`[]` so even if a future contributor accidentally registers it with
`KnowledgeService`, the sync loop is a no-op.

**On re-index reproducibility.** Bytes are cached primarily for
observability and debugging. There's no auto re-index path: since the
synthetic backend isn't in `KnowledgeProvider.backends`, the periodic
`knowledge-sync` doesn't iterate it. Manual re-ingest of a single
item is exposed via the WS RPC `feeds.items.reingest` (admin / owner
only — see §14). Bulk re-ingest is a v1.x feature.

**Cascade on retention / unsubscribe.** When `retention_days` purges
old items (§11), or when a feed is unsubscribed, the matching cached
files are deleted **and** their knowledge entries are removed via
`KnowledgeProvider.remove_document(document_id)` (the new method
introduced in §18.1).

## 10. AI tools (all on `FeedsService`)

Every tool sets `slash_group="feeds"` so they collapse under one
top-level slash namespace. Every tool sets a `slash_help` and uses
`required_role="user"`. None of the read tools need to be marked
`parallel_safe=False`. **Write tools (`subscribe_feed`,
`unsubscribe_feed`) return UI confirmation blocks via `ToolOutput`
and do NOT persist directly** — the user clicks Confirm in the UI,
which fires the matching WS RPC. Without that, AI write tools are a
footgun (URL hallucination, typos, mistaken site).

The full v1 tool surface, with disambiguation guidance baked into
each description so the model picks the right one:

### `news_briefing` (renamed from `daily_briefing` for grep-ability)

```
slash_group: feeds
slash_command: briefing
slash_aliases: ["today"]   # /feeds today as well as /feeds briefing
slash_help: Today's news briefing: /feeds briefing [top=5] [since=24h]
description: Generate the user's news briefing as a single spoken paragraph
    PLUS a structured headline list. Pulls top-scored items across feeds
    the caller can access, marks them as briefed, returns the spoken text
    and headlines.
    USE THIS for cross-feed daily summaries: "what's important today?",
    "morning briefing", "what should I know?". Marks items as briefed —
    only call once per day per user (calling twice returns the cached
    result, does not re-run the AI). For per-feed summaries, use
    summarize_feed instead.
parameters:
  - top: int, default 5, range 1..20
  - since: str (ISO duration like "24h" or full ISO datetime), default "24h"
  - category: str (optional filter to one category), default ""
required_role: user
```

Returns a `ToolOutput` whose `text` is the spoken paragraph and whose
`ui_blocks` is a structured list block of headlines (each with
`item_id`, `title`, `link`, `one_liner`, `score`). The chat UI renders
the headline list as a clickable list — that's the
"Gilbert-read-me-three-things-while-I-made-coffee" delight payoff.

**Idempotency.** If the caller's user already has a briefing today
(checked via `feed_briefing_state.last_briefed_on`), return the
cached `BriefingResult` rather than spending another AI call. A
deliberate force-rescore goes through `feeds.briefing.run` with an
`force=True` flag in the WS RPC.

### `search_feeds`

```
slash_group: feeds
slash_command: search
slash_help: Search feed items: /feeds search [query=...] [unread=true]
            [feed_id=...] [min_score=0.5] [limit=20] [page=1]
description: Search items across feeds the caller can access.
    USE THIS for "find me articles about X", "unread on this feed",
    "scores above 0.7 in tech feeds." Do NOT use for "what's new today" —
    that's news_briefing. With no filters set, returns the most-recent N
    items.
parameters:
  - query: str
  - feed_id: str
  - unread_only: bool, default false
  - min_score: float, default 0.0
  - category: str
  - limit: int, default 20
  - page: int, default 1   # paginated for power-user / slash use
required_role: user
```

### `summarize_feed`

```
slash_group: feeds
slash_command: summarize
slash_help: Summarize a feed or one item: /feeds summarize <feed_id>
            [item_id=...] [count=10]
description: Summarize the most recent N items in a feed (default 10),
    or summarize one specific item by id.
    USE THIS when the user names a specific feed or asks "what's new on X?".
    Does NOT mark items briefed (those are different concepts). Caches the
    AI summary on each item so future calls are cheap.
parameters:
  - feed_id: str (required, accepts partial-match resolution — e.g.
              "tech" matches the feed whose name contains "tech")
  - item_id: str (optional, summarize one)
  - count: int, default 10
required_role: user
```

### `subscribe_feed` — confirmation-block tool, does NOT persist

```
slash_group: feeds
slash_command: subscribe
slash_aliases: ["add"]
slash_help: Subscribe to a feed: /feeds subscribe <url> [name=...]
            [category=...] [dry_run=true]
description: Probe a feed URL and propose a subscription. Returns a
    ToolOutput with a confirmation ui_block ("Subscribe to TechCrunch?
    Category: tech. Poll every 30m." with Confirm/Cancel buttons). Does
    NOT persist; the user clicks Confirm to fire feeds.create. With
    dry_run=true, returns probe results only without proposing
    subscription.
parameters:
  - url: str (required)
  - name: str
  - category: str
  - poll_interval_sec: int, default 1800
  - dry_run: bool, default false
required_role: user
```

### `unsubscribe_feed` — confirmation-block tool, does NOT persist

```
slash_group: feeds
slash_command: unsubscribe
slash_aliases: ["remove"]
slash_help: Unsubscribe from a feed: /feeds unsubscribe <feed_id>
description: Propose unsubscribing from a feed. Returns a ToolOutput
    with a confirmation ui_block ("Unsubscribe from TechCrunch? You will
    lose 247 stored items." with Confirm/Cancel). Does NOT persist; the
    user clicks Confirm to fire feeds.delete.
parameters:
  - feed_id: str (required, accepts partial-match resolution)
required_role: user
```

### `list_feeds`

```
slash_group: feeds
slash_command: list
slash_help: List feeds you can access: /feeds list [compact=true]
description: List every feed the caller can access (owner / admin /
    shared). Call this first when the user's intent doesn't already
    name a feed.
parameters:
  - compact: bool, default true   # /feeds list compact-by-default — name + unread + error pill
                                  # detailed shows all columns
required_role: user
```

### `read_feed_item` (auto-mark-read)

```
slash_group: feeds
slash_command: read
slash_help: Read an item: /feeds read <item_id> [mark_read=true]
description: Read one feed item — title, link, summary, ai_summary, score,
    and score_reason. By default, marks the item as read as a side effect
    (mark_read=true). Pass mark_read=false to peek without marking.
    Power-user "mark unread" is a web-UI action; the AI never needs to
    mark unread.
parameters:
  - item_id: str (required, accepts partial-match resolution; also
              accepts "latest" or "latest <category>" — e.g.
              "/feeds read latest tech")
  - mark_read: bool, default true
required_role: user
```

There is no separate `mark_feed_item` tool. The previous spec had one;
it was folded into `read_feed_item`'s `mark_read` parameter per
product review.

### `recommend_knowledge_ingestion`

```
slash_group: feeds
slash_command: recommend-knowledge
slash_help: Suggest which feeds to ingest into knowledge: /feeds
            recommend-knowledge [feed_id=...]
description: Analyze the user's feeds and recommend whether
    ingest_to_knowledge should be enabled for each. Considers: how often
    the user reads or cites items from this feed, average score of recent
    items, the user's stated interests, and whether the feed produces
    deep-content articles vs. headline links. Returns a list of
    {feed_id, recommendation, rationale} — does NOT flip the flag.
    The user accepts the recommendation via UI confirmation.
parameters:
  - feed_id: str (optional; if absent, evaluates all accessible feeds)
required_role: user
```

This addresses the "30 feeds, only 3 ingest_to_knowledge=True, user
doesn't know which to flip" problem flagged by product review.

### Tools NOT exposed via slash

None for v1. Every tool has a sensible CLI shape, so every tool gets a
`slash_command`. (See the "Slash Command Violations" section of the
[Architecture Violation Checklist](../../.claude/memory/memory-architecture-checklist.md).)

### Profile inclusion

All tools are included in the default profiles (`light`, `standard`,
`advanced`) per the standard "all tools default to all profiles"
convention (`memory-ai-context-profiles.md`). Including
`subscribe_feed` / `unsubscribe_feed` in `light` is acceptable
**only because** they are confirmation-block tools that don't persist
without a user click. Without that gate, they'd be a footgun in the
cheap profile.

## 11. Configuration surface

Two `Configurable` sections — one per service.

### `feeds` (FeedsService) — owns ALL three AI prompts

| Key | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `True` | Service master switch (toggleable). |
| `max_items_per_poll` | int | `100` | Limits per-feed work. |
| `max_summary_length` | int | `4000` | Truncate stored summary text. |
| `default_poll_interval_sec` | int | `1800` | Default for new subscriptions. |
| `summarize_on_ingest` | bool | `False` | If `True`, run `feed_summarize` on every new item. |
| `score_on_ingest` | bool | `True` | If `False`, scoring runs only when the briefing or search asks for it. |
| `initial_score_cap` | int | `50` | Max scoring calls fanned out from `_boot_runtimes` first-sync. Items beyond the cap stay at `score=-1.0` until lazy-scored on read. Hard requirement, not an open question. |
| `max_concurrent_polls` | int | `8` | Service-wide poll-fan-out cap (§6.7). |
| `max_concurrent_scoring` | int | `4` | Bounds AI scoring workers (§6.4c). |
| `max_concurrent_ingestion` | int | `2` | Bounds knowledge body fetches (§9). |
| `max_first_poll_jitter_sec` | int | `30` | First-poll stagger window (§6.3). |
| `retention_days` | int | `90` | Hard-delete items older than this; `0` = keep forever. Daily `feeds-retention-tick` system job enforces. |
| `ingest_max_items_per_day_per_user` | int | `200` | Per-user ingestion cap. Above this, emit `feed.ingest.throttled` and skip. |
| `respect_robots_txt` | bool | `True` | Honor robots.txt for `link` body fetches in §9. |
| `scoring_ai_profile` | str | `"light"` | `choices_from="ai_profiles"`. Short structured classification call. |
| `summarization_ai_profile` | str | `"light"` | `choices_from="ai_profiles"`. |
| `briefing_ai_profile` | str | `"medium"` | `choices_from="ai_profiles"`. **Higher than light** — a coherent 200-word paragraph with varied tone is harder than a JSON classification, and this is the user-facing voice; cheap models sound flat. |
| `user_interests` | str | `""` | Multiline, **NOT** `ai_prompt=True`. Free-form description of "what I care about" the user can write — fed into the scoring prompt as user content, not system prompt. **Sanitized** before concat: leading/trailing whitespace stripped, embedded backticks neutralized — mitigates prompt-injection-via-config. |
| `scoring_prompt` | str | (bundled default) | `multiline=True, ai_prompt=True`. The system prompt for the importance-scoring AI call. |
| `summarization_prompt` | str | (bundled default) | `multiline=True, ai_prompt=True`. |
| `briefing_prompt` | str | (bundled default) | `multiline=True, ai_prompt=True`. The briefing system prompt. **Lives here, not on `FeedBriefingService`** — owning service runs the AI call. |

### `feed_briefing` (FeedBriefingService) — schedule + fan-out only

| Key | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `False` | Off by default — opt-in feature. |
| `briefing_hour` | int | `7` | Hour-of-day to auto-generate the briefing. **Single-tenant time zone** in v1 — see §13.1 known limitation. |
| `briefing_minute` | int | `0` | |
| `timezone` | str | `"UTC"` | Server-side timezone applied to `briefing_hour`. |
| `briefing_top_n` | int | `5` | Items per briefing. |
| `briefing_since_hours` | int | `24` | Look-back window. |
| `presence_grace_minutes` | int | `90` | If presence-driven greeting hasn't fired by `briefing_hour + presence_grace_minutes`, the daily fan-out fires for that user as the fallback (§13.1). |
| `system_briefing_enabled` | bool | `False` | Enable a **single shared "shop" briefing** voiced over speakers regardless of who's home. Distinct from per-user fan-out; gated by this flag because it's a shop-wide policy, not a per-user preference. |
| `system_briefing_user_id` | str | `""` | When `system_briefing_enabled=True`, the user_id whose accessible feeds drive the shop briefing. |
| `announce_speakers` | list[str] | `[]` | `choices_from="speakers"`, consumed when `system_briefing_enabled=True`. |

`config_category` for both services: `"News & Information"` (a new
category — fine; the Settings UI auto-groups by category string).

### Bootstrap YAML

Mirror `inbox`'s zero-bootstrap approach: nothing in `gilbert.yaml`.
All feeds config lives in entity storage. The toggle in Settings is
the canonical way to enable feed briefing.

## 12. Events

All published events carry the relevant `feed_id` (and `item_id` for
item events) so listeners can filter without hitting storage.

| Event | Data fields | Visibility (acl.py) |
|---|---|---|
| `feed.item.received` | `feed_id`, `item_id`, `title`, `link` (no `score` — scoring is async; consumers wanting score subscribe to `feed.item.scored`) | user (100) |
| `feed.item.scored` | `feed_id`, `item_id`, `score`, `score_reason` | user (100) |
| `feed.subscription.created` | `feed_id`, `name`, `url`, `owner_user_id` | user (100) |
| `feed.subscription.updated` | `feed_id`, `name`, `owner_user_id` | user (100) |
| `feed.subscription.deleted` | `feed_id`, `owner_user_id` | user (100) |
| `feed.subscription.shares.changed` | `feed_id`, `owner_user_id`, `shared_with_users`, `shared_with_roles` | user (100) |
| `feed.subscription.disabled` | `feed_id`, `owner_user_id`, `reason`, `last_error` (auto-disable from §6.5 graceful-give-up threshold) | user (100) |
| `feed.ingest.throttled` | `user_id`, `feed_id`, `current_count`, `cap` | user (100) |
| `feed.briefing.ready` | `user_id`, `briefing_id`, `item_count`, `since` (**no `spoken_text`** — fetched via `feeds.briefing.get` to keep the WS event log free of potentially-sensitive briefing content) | user (100), filtered to recipient `user_id` only |

Add to `interfaces/acl.py`:

```python
DEFAULT_EVENT_VISIBILITY = {
    ...,
    "feed.": 100,
}
```

The WS fanout layer also adds a per-event feed-access filter on top of
the level check, modeled on `can_see_inbox_event` — the frontend
keeps a cache of accessible feed_ids (invalidated on
`feed.subscription.shares.changed` and `auth.user.roles.changed`) and
drops events for feeds the user can't access. **`feed.briefing.ready`
is fanned out only to the recipient `user_id`** via a dedicated filter,
not to every user — analogous to how notification events work.

## 13. `FeedBriefingService` daily schedule

### 13.1 Daily-fire vs. presence-driven greeting — race resolution

The pitch in §1 is a **presence-driven** flow: 7 AM, presence detects
arrival, greeting service splices in the briefing. But a naive daily
`Schedule.daily(hour=7)` job would race the presence-driven path —
whichever fires first marks the items briefed and the other comes up
empty. The original spec was silent on this; the product review
correctly flagged it as a "daily fire eats the items the killer
feature needs" bug.

**Resolved policy: presence-first, daily-fire is the fallback.**

`FeedBriefingService.start()` registers a `Schedule.daily(hour=H+grace,
minute=M, timezone=tz)` system job named `feed-briefing-fallback`,
where `grace` is `presence_grace_minutes` (default 90 min, so default
fire at 8:30 AM if `briefing_hour=7`). The fallback job iterates
**every user with at least one accessible `briefing_eligible` feed**
and, for each user:

1. Reads `feed_briefing_state.last_briefed_on` (date-only).
2. **If today's date is already in `last_briefed_on`, skip** — the
   greeting flow already fired for this user today.
3. Otherwise, call `FeedsProvider.build_briefing(user_ctx,
   mark_briefed=True)`, persist `last_briefed_on=today`, emit
   `feed.briefing.ready`.

Meanwhile, `GreetingService` (§16) calls
`FeedsProvider.build_briefing(user_ctx, top_n=3, mark_briefed=True)`
when presence fires inside the greeting window AND
`include_briefing=True` AND today's `last_briefed_on != today`.
Whichever path runs first sets `last_briefed_on`; the other becomes a
no-op for that user that day.

This means:

- Normal day: presence fires at 7:15 AM, greeting includes briefing,
  `last_briefed_on=today`. The 8:30 AM fallback skips this user.
- User works from home / presence misfires: the 8:30 AM fallback
  fires the briefing as a generic event (no greeting wrapper), the
  user sees it via the dashboard "Briefing" card and an
  optional notification. Beats silence.
- User on vacation: greeting never fires, fallback fires once at
  8:30 AM, gets quietly piled up with a "you have unread briefings"
  surface (§15).

**`feed_briefing_state` collection** — one row per user:

| Field | Type | Notes |
|---|---|---|
| `_id` | str | `user_id` |
| `last_briefed_on` | str | ISO date `YYYY-MM-DD` or empty |
| `last_briefing_id` | str | last briefing's `briefing_id` |
| `recent_briefings` | list[str] | last-10 spoken-text strings, FIFO trimmed (§8.3 anti-repetition) |

### 13.2 Time zone — single global v1, per-user v1.x

Spec is multi-user but the daily fire uses a single
`Schedule.daily(hour=briefing_hour, timezone=timezone)`. **v1 is
single-time-zone** — known limitation. Households where everyone
lives in one zone are fine; deployments with users in multiple zones
get the briefing at server-zone-7-AM regardless. v1.x adds per-user
`briefing_hour` / `timezone` overrides plus a 15-min "briefing tick"
that decides which users are due — out of scope here. Recorded in §23.

### 13.3 User enumeration

To enumerate users we walk the `feeds` collection and union
`{owner_user_id} ∪ shared_with_users` plus role-resolved members
(via `AccessControlProvider`). Cache the union **in memory only** for
the duration of the daily run; rebuild next daily fire. Do NOT
persist a "users-with-feeds" projection — easy to drift.

**Per-user opt-in for role-shared briefings.** If a user only gains
feed access via *role share* (a "team" role with a shared feed),
default that user's `briefing_opt_in` to `False` so a 10-person team
sharing a feed doesn't trigger 10 unwanted morning briefings. Users
who own at least one feed get `briefing_opt_in=True` by default. The
flag lives on a per-user `feed_briefing_state` field.

### 13.4 System / shop briefing

When `system_briefing_enabled=True` (§11) AND `system_briefing_user_id`
is set, the fallback job ALSO calls
`speaker_svc.announce(spoken, speaker_names=announce_speakers or None)`
once for that user — the shop's blanket morning briefing on the shop
speakers. Distinct from per-user fan-out: this is a single
shop-policy announcement, not per-user TTS. (Naming `auto_announce`
the previous spec used was muddy — renamed to `system_briefing_*`
because the previous flag conflated "did we wake up the speakers"
with "did we generate a per-user briefing.")

For per-user TTS routing to a specific device, leave the system
flags off and let `GreetingService` handle it — that's how the inbox
events are wired today.

`SchedulerProvider` change is implicit (no API change needed —
`Schedule.daily` already supports `hour/minute/timezone`).

## 14. WebSocket RPCs

Same prefix scheme as inbox. All RPCs default to user level (100); the
handler enforces per-feed access via `can_access_feed` /
`can_admin_feed`.

```
feeds.list                    -> list of accessible feeds with last_polled_at, last_error,
                                 last_poll_status_code, last_poll_items_total/new/duration_ms,
                                 unread_count, suggested_poll_interval_sec
feeds.get                     -> one feed
feeds.create                  -> subscribe (probe + persist + start runtime). Called by the
                                 confirmation-block UI after subscribe_feed tool runs.
feeds.update                  -> mutate; restarts runtime if needed (per §6.2 field set)
feeds.delete                  -> unsubscribe; cascades feed_items AND removes their entries
                                 from KnowledgeProvider via remove_document
feeds.test                    -> probe URL without creating
feeds.share_user / unshare_user / share_role / unshare_role
feeds.poll_now                -> force a poll (admin or owner). Returns synchronously with
                                 {items_seen, items_new, error} so the UI can show "Polled
                                 0 new" feedback.
feeds.items.list              -> filter by feed_id, unread_only, min_score, query, limit, page
feeds.items.get               -> one item
feeds.items.mark              -> mark read/unread
feeds.items.delete            -> hard delete one item (owner / admin only); cascades
                                 KnowledgeProvider.remove_document if previously ingested
feeds.items.reingest          -> manual re-ingest of one cached item to knowledge (admin / owner)
feeds.backends.list           -> registered FeedBackend list with backend_config_params() schemas
feeds.briefing.preview        -> dry-run: build_briefing(mark_briefed=False); does NOT publish
                                 feed.briefing.ready; returns {spoken, headlines}
feeds.briefing.run            -> build_briefing(mark_briefed=True), persists, publishes
                                 feed.briefing.ready (admin or self). Accepts force=True to
                                 bypass the today-already-briefed cache.
feeds.briefing.get            -> {briefing_id} -> {spoken, headlines, item_ids, since}; this
                                 is how WS clients fetch full briefing text after the lean
                                 feed.briefing.ready event arrives
feeds.import_opml             -> upload OPML, parse <outline> entries, call subscribe() for each,
                                 return [(url, ok|error)] result list
feeds.export_opml             -> dump all accessible feeds as an OPML document
```

Add to `interfaces/acl.py`:

```python
DEFAULT_RPC_PERMISSIONS = {
    ...,
    "feeds.": 100,
}
```

Handler-level enforcement supplies the per-resource access check on top.

## 15. Web UI (`/feeds`)

Same shape as `/inbox`. Mirror these files into
`frontend/src/components/feeds/`:

- `FeedsPage.tsx` — top-level page with a feed sidebar (Owned / Shared
  / All-for-admins), main pane with item list, and detail panel.
- `FeedSidebar.tsx` — feed list grouped by access type, with inline
  poll-error pill when `last_error` is non-empty.
- `FeedEditor.tsx` — drawer for create / edit, including the
  category, importance_weight slider, ingest-to-knowledge toggle, and
  briefing-eligible toggle. Backend-config fields are rendered
  dynamically from `feeds.backends.list` using the shared `ConfigField`
  component.
- `ItemList.tsx` — sortable by `received_at`, `score`, or `published_at`.
- `ItemDetailDialog.tsx` — title, link (opens in new tab), summary,
  ai_summary, score, score_reason, tags, and a "mark read" toggle.
- `BriefingPreview.tsx` — settings-page widget that runs
  `feeds.briefing.preview` and renders **two layers**: (1) a bulleted
  list of headlines (title link, one_liner, score badge) for visual
  skimming, (2) the full spoken text in a collapsible "Read aloud"
  section. The dashboard reader wants to scan; the TTS recipient
  wants prose. Both surfaces are real. Has a "Run now" button that
  calls `feeds.briefing.run` and a "Skip today" button that flips
  `last_briefed_on=today` without running the AI.
- `BriefingNotification.tsx` — when `feed.briefing.ready` fires for the
  current user and the dashboard hasn't been opened, show a toast
  ("You have a briefing"). Click to expand into `BriefingPreview`.
  Without this, presence-misfire = silently lost briefing.

The page is mounted at `/feeds` via a top-level core route (NOT a
`Plugin.ui_routes()` registration — feeds is a core feature, not a
plugin). Add a dashboard card "Feeds" with a `briefing_eligible
unread_count` summary and a "Briefing" card showing today's spoken
text if available.

Add a `<PluginPanelSlot slot="feeds.toolbar" />` at the top of
`FeedsPage` so future plugins can drop e.g. a "Reddit subscriptions"
button without touching core. (Slots cost nothing when empty —
per [Plugin UI Extensions checklist](../../.claude/memory/memory-architecture-checklist.md).)

## 16. Greeting integration

`GreetingService` already announces a personalized greeting when a
`presence.arrived` event fires inside the configured greeting window.
The integration with feeds happens in **`GreetingService`**, which
makes the policy decision about when to splice the briefing into the
announcement.

Concrete change in `GreetingService`:

1. New optional config field `include_briefing: bool` (default
   `False`).
2. New optional config field `briefing_max_seconds: int` (default
   `60`) — soft cap on the briefing length, passed as a hint into
   the briefing AI call.
3. When `include_briefing=True` AND today's `last_briefed_on != today`
   for this user (§13.1), after building the personalized greeting
   text, call `feeds.build_briefing(user_ctx, top_n=3,
   max_spoken_seconds=briefing_max_seconds, mark_briefed=True)` and
   read `result.spoken`. Concatenate `greeting + " " + result.spoken`
   and pass to `speaker_svc.announce`.
4. `FeedsService` is resolved via `resolver.get_capability("feeds")`
   and an `isinstance` check against `FeedsProvider` (the protocol
   defined in `interfaces/feeds.py`). **No `BriefingProvider`** —
   that protocol was dropped in revision (one method on the same
   service the caller already resolved by name was overengineering).

If `feeds` capability is absent or the service is disabled,
`include_briefing=True` is a no-op and the greeting goes out without
the briefing — degrade gracefully, exactly like
`GreetingService._announce` already does when `speaker_control` is
missing.

This means the killer-feature flow is reachable end-to-end with **just
the existing greeting service plus the new feeds + feed_briefing
services**. No new wiring in `app.py`, no plugin required.

## 17. Layer / dependency rules — review

Cross-checking against the
[Architecture Violation Checklist](../../.claude/memory/memory-architecture-checklist.md):

- **`interfaces/feeds.py`** imports only stdlib, other `interfaces/`
  modules (`auth.UserContext`, `configuration.ConfigParam`,
  `events.Event` is NOT needed here), and dataclass types. **No imports
  from `core/`, `integrations/`, `web/`, or `storage/`.**
- **`integrations/rss_feeds.py`** imports `interfaces/feeds.py`,
  `interfaces/configuration.py`, `interfaces/tools.py` (for
  `ToolParameterType`), `httpx`, `feedparser`. **No imports from
  `core/services/`, `core/`, or other integrations.**
- **`integrations/feed_documents.py`** (the synthetic
  `DocumentBackend` for ingestion) imports
  `interfaces/knowledge.py` only. It implements a read-only
  backend that reads files from `.gilbert/feed-cache/...`.
- **`core/services/feeds.py`** imports
  `interfaces/feeds.py`, `interfaces/storage.py`,
  `interfaces/scheduler.py`, `interfaces/events.py`,
  `interfaces/auth.py`, `interfaces/ai.py` (for
  `AISamplingProvider`, `Message`, `MessageRole`), and a side-effect
  `import gilbert.integrations.rss_feeds  # noqa: F401`. It does NOT
  import `RssAtomFeedBackend` directly — it goes through
  `FeedBackend.registered_backends().get("rss_atom")`.
- **`core/services/feed_briefing.py`** imports
  `interfaces/feeds.py` (for `FeedsProvider` only — no
  `BriefingProvider` exists), `interfaces/speaker.py` (for
  `SpeakerProvider`, used only in the §13.4 system-briefing path),
  the same set of interface modules, and never imports `FeedsService`.
- **Plugins** (future Reddit/HN backends) will live in
  `std-plugins/<name>/` and import only `gilbert.interfaces.feeds`,
  `gilbert.interfaces.configuration`, and `gilbert.interfaces.tools`.

### Capability protocols — every cross-service interaction

Every place where one service of this feature calls another:

| Caller | Callee capability | Protocol checked |
|---|---|---|
| `FeedsService` | `entity_storage` | `StorageProvider` |
| `FeedsService` | `scheduler` | `SchedulerProvider` |
| `FeedsService` | `event_bus` | `EventBusProvider` |
| `FeedsService` | `configuration` | `ConfigurationReader` |
| `FeedsService` | `access_control` | `AccessControlProvider` |
| `FeedsService` | `ai_chat` | `AISamplingProvider` |
| `FeedsService` | `knowledge` | `KnowledgeProvider` (introduce in `interfaces/knowledge.py` — see §18) |
| `FeedBriefingService` | `feeds` | `FeedsProvider` (only protocol it consumes from feeds) |
| `FeedBriefingService` | `event_bus` | `EventBusProvider` |
| `FeedBriefingService` | `speaker_control` | `SpeakerProvider` (existing — `src/gilbert/interfaces/speaker.py`; used only by §13.4 system-briefing path) |
| `GreetingService` | `feeds` | `FeedsProvider` (consumes `build_briefing`; no separate briefing protocol) |

**No `isinstance` against concrete service classes anywhere.**

### Hardcoded prompts

**All three prompts live on `FeedsService`** (scoring, summarization,
briefing) — the owning service that runs the AI call. All three are
`ConfigParam(multiline=True, ai_prompt=True)` with the bundled string
as `default`, cached on `self._scoring_prompt` /
`self._summarization_prompt` / `self._briefing_prompt` in
`on_config_changed`, and consumed only via `self.<attr>` at the call
site. Empty-string overrides fall back to the constant per the
documented pattern. `FeedBriefingService` owns no prompts of its own.

### Multi-user isolation

Per-request identity comes from `gilbert.core.context` ContextVars
(set by the WS dispatch and AI tool dispatch). Per-target locks
(if needed for serializing concurrent polls of the same feed — the
scheduler should already prevent overlapping fires of the same job, so
this is unlikely) live in
`_poll_locks: dict[feed_id, asyncio.Lock]`. **No `_current_*`
attributes on `self`.**

## 18. Required upstream changes

Two minor changes to existing files:

### 18.1 New protocol: `KnowledgeProvider`

Currently `KnowledgeService` is consumed by `InboxService` via
`getattr(self._knowledge, "backends", {})` style duck-typing
(`core/services/inbox.py` line 104 — `self._knowledge: Any`; lines
1483/1489 — `self._knowledge.backends.items()`). That's a pre-existing
duck-typing violation flagged by the architecture checklist. As part
of this feature, introduce a proper protocol in
`interfaces/knowledge.py`:

```python
@runtime_checkable
class KnowledgeProvider(Protocol):
    async def index_document(
        self,
        backend: DocumentBackend,
        meta: DocumentMeta,
    ) -> int: ...

    async def remove_document(self, document_id: str) -> bool: ...

    async def resolve_document(
        self,
        source_id: str,
        path: str,
    ) -> DocumentMeta | None: ...

    def get_backend(self, source_id: str) -> DocumentBackend | None: ...

    @property
    def backends(self) -> dict[str, DocumentBackend]: ...
```

Why each method:

- `index_document` — both `InboxService` and `FeedsService` need it.
- `remove_document` — `FeedsService` needs it for retention purges
  and unsubscribe cascade (§9, §11). `InboxService` doesn't today,
  but designing the protocol once is cheaper than twice.
- `resolve_document`, `get_backend` — already public on
  `KnowledgeService` (lines 75, 78); add to the protocol now so
  `InboxService` can stop duck-typing as part of this PR.
- `backends` (read-only property) — used by `InboxService` to
  enumerate accessible source_ids.

`KnowledgeService` already implements all four. The PR adds the
`@runtime_checkable Protocol`, switches `InboxService._knowledge`
from `Any` to `KnowledgeProvider | None`, and adds the
`isinstance` check at resolve time in both services. **`memory-knowledge-service.md`
and `memory-inbox-service.md` MUST be updated in the same PR**
(§22 calls this out — make sure it's actually done).

### 18.2 ACL defaults

Add `"feed.": 100` to `DEFAULT_EVENT_VISIBILITY` and `"feeds.": 100`
to `DEFAULT_RPC_PERMISSIONS` in `interfaces/acl.py`.

### 18.3 None for the scheduler

`Schedule.every(seconds=...)`, `Schedule.daily(hour=..., minute=...,
timezone=...)`, and `Schedule.once_after(seconds=...)` are sufficient
— no scheduler API changes needed.

## 19. Tests

All under `tests/unit/test_feeds_service.py`,
`tests/unit/test_feed_briefing_service.py`, and
`tests/unit/test_rss_atom_backend.py`. Database tests use a real
test SQLite DB per CLAUDE.md.

### `test_rss_atom_backend.py` (no mocks of feedparser, no network)

Tests inject `httpx.AsyncClient(transport=httpx.MockTransport(...))`
into `initialize()` so canned fixtures answer canned URLs. Tests
**MUST NOT hit the network**. We do not mock `feedparser` itself —
use real fixture bytes against the real parser.

- `test_probe_returns_meta_for_known_atom_fixture` — feed an Atom
  XML fixture as a fake HTTP response, assert title/description/link.
- `test_poll_returns_items_in_published_order`.
- `test_poll_dedup_by_guid` — same fixture polled twice yields the
  same `item_uid`s.
- `test_poll_handles_missing_guid_falls_back_to_link`.
- `test_poll_handles_missing_link_falls_back_to_hash_of_title_and_date`.
- `test_poll_returns_empty_on_304_not_modified` and
  `test_poll_round_trips_etag_and_last_modified_via_http_cache`.
- `test_probe_rejects_non_http_url`.
- `test_basic_auth_header_sent` — assert the HTTP fixture saw the
  expected `Authorization: Basic ...` header.
- `test_required_headers_sent` — User-Agent, Accept, Accept-Encoding.
- `test_response_size_cap_aborts_large_body` — fixture > 10 MiB
  raises `FeedError`.
- `test_unknown_content_encoding_fails_closed` — `Content-Encoding: br`
  → `FeedError`.
- `test_https_to_http_redirect_rejected`.
- `test_redirect_chain_capped_at_five`.
- `test_atom_feed_with_id_only_no_link`.
- `test_rss_feed_with_neither_guid_nor_link_uses_hash_fallback`.
- `test_malformed_xml_handled_gracefully` — `feedparser` accepts much
  garbage; assert we don't crash.
- `test_ttl_clamps_effective_interval_upward`.
- `test_two_items_sharing_a_guid_dont_double_persist` (vendor-bug
  fixture).
- `test_published_in_the_future_clock_skew`.
- `test_empty_feed_no_items_no_error`.

Use small XML fixtures checked into `tests/fixtures/feeds/`.

### `test_feeds_service.py`

- `test_subscribe_creates_feed_and_starts_runtime` — using a fake
  `FeedBackend` that records calls.
- `test_subscribe_probes_url_first_and_uses_returned_name`.
- `test_unsubscribe_cascades_feed_items`.
- `test_unsubscribe_calls_remove_document_for_ingested_items`.
- `test_poll_persists_only_new_items_dedup_by_uid`.
- `test_poll_does_not_short_circuit_on_first_known_item` — fixture
  with `<atom:updated>` ordering breaks reverse-chrono assumption.
- `test_poll_records_error_and_increments_consecutive_failures`.
- `test_poll_resets_consecutive_failures_on_success`.
- `test_poll_backs_off_after_three_failures`.
- `test_backoff_restore_does_not_lose_job` — concurrent poll during
  remove/re-add swap is correctly serialized by `_poll_locks`.
- `test_graceful_giveup_disables_at_20_consecutive_failures` — emits
  `feed.subscription.disabled`.
- `test_304_does_not_bump_consecutive_failures_does_bump_last_polled_at`.
- `test_http_cache_round_trips_etag_and_last_modified` —
  `Feed.http_cache` is updated, `Feed.backend_config` is untouched.
- `test_ttl_widens_effective_interval_upward`.
- `test_first_poll_jitter_staggers_bulk_subscribe` — 50 subscribes
  fan first polls across the jitter window (not 50 simultaneous fetches).
- `test_max_concurrent_polls_semaphore_bounds_parallelism`.
- `test_score_on_ingest_calls_ai_with_configurable_prompt` — set
  `scoring_prompt = "ZZZ"`, assert the captured `system_prompt`
  argument starts with `"ZZZ"`.
- `test_score_on_ingest_passes_tools_override_empty`.
- `test_score_queue_drains_on_stop` — graceful shutdown doesn't lose
  scoring work or hang waiting for the AI.
- `test_score_queue_full_drops_with_warning` — counter increments.
- `test_score_parser_strips_json_fences` — `\`\`\`json ... \`\`\``
  wrapped output is parsed.
- `test_score_parser_failure_sets_minus_one`.
- `test_rescore_tick_resweeps_minus_one_items_within_24h_window`.
- `test_initial_score_cap_caps_first_sync_scoring_calls`.
- `test_score_caps_at_importance_weight`.
- `test_summarize_on_ingest_disabled_by_default`.
- `test_edit_detection_updates_title_summary_only_leaves_score_briefed_alone`.
- `test_edit_does_not_re_emit_feed_item_received`.
- `test_authorization_owner_admin_shared_user_shared_role` — full
  matrix mirroring the inbox tests.
- `test_share_unshare_user_role_publishes_event`.
- `test_search_items_filters_by_min_score_and_unread`.
- `test_search_items_pagination`.
- `test_ingest_to_knowledge_calls_index_document_directly` — fake
  `KnowledgeProvider` records the call; assert synthetic backend is
  NOT in `provider.backends` (private to `FeedsService`).
- `test_ingest_skipped_when_knowledge_capability_absent`.
- `test_ingest_respects_robots_txt_when_enabled`.
- `test_ingest_skips_paywall_stubs`.
- `test_ingest_skips_non_html_content_types`.
- `test_ingest_per_user_per_day_cap_emits_throttled_event`.
- `test_ingest_rejects_https_to_http_downgrade`.
- `test_retention_tick_deletes_old_items_and_calls_remove_document`.
- `test_user_interests_sanitized_no_backtick_injection`.
- `test_tools_inbox_mailboxes_pattern_for_feeds` — `list_feeds` tool
  for a non-admin user only returns shared/owned feeds.
- `test_subscribe_feed_tool_returns_confirmation_block_does_not_persist`.
- `test_unsubscribe_feed_tool_returns_confirmation_block_does_not_persist`.
- `test_news_briefing_tool_returns_cached_when_already_briefed_today`.
- `test_recommend_knowledge_ingestion_tool_returns_recommendations_does_not_flip`.
- `test_concurrent_polls_of_same_feed_serialize` — two `_poll_runtime`
  calls in flight at once don't double-persist (asyncio.gather pair).
- `test_opml_import_calls_subscribe_per_outline`.
- `test_opml_export_round_trip`.

### `test_feeds_service_briefing.py` (build_briefing tests live with FeedsService)

- `test_build_briefing_pulls_top_n_by_score`.
- `test_build_briefing_filters_briefing_eligible_only`.
- `test_build_briefing_filters_by_briefed_at_empty`.
- `test_build_briefing_recency_decay_prefers_fresher_items_within_window`.
- `test_build_briefing_returns_two_artifacts_spoken_and_headlines`.
- `test_build_briefing_with_mark_briefed_false_does_not_set_briefed_at`.
- `test_build_briefing_uses_configurable_prompt_on_feeds_service`.
- `test_build_briefing_passes_tools_override_empty` — prevents the
  Sonos audio-clip-loop recursion bug.
- `test_build_briefing_appends_to_recent_briefings_capped_at_10`.
- `test_build_briefing_anti_repetition_passes_recent_briefings_to_user_msg`.
- `test_build_briefing_falls_back_to_deterministic_format_on_parse_failure`.
- `test_feeds_provider_protocol_includes_build_briefing`.

### `test_feed_briefing_service.py` (schedule + fan-out only)

- `test_daily_fallback_skips_users_already_briefed_today`.
- `test_daily_fallback_fires_for_users_who_missed_presence`.
- `test_briefing_ready_event_carries_briefing_id_not_spoken_text`.
- `test_briefing_ready_event_filtered_to_recipient_user_id_only`.
- `test_role_shared_users_default_to_briefing_opt_in_false`.
- `test_system_briefing_calls_speaker_announce_for_configured_user`.
- `test_system_briefing_no_op_when_flag_disabled`.
- `test_feed_briefing_service_consumes_only_feeds_provider_no_briefing_provider_exists`.

### Greeting integration tests

In `tests/unit/test_greeting_service.py` (extending the existing
file):

- `test_greeting_includes_briefing_when_flag_set_and_feeds_capable`.
- `test_greeting_skips_briefing_when_flag_off`.
- `test_greeting_skips_briefing_when_feeds_capability_absent` —
  `FeedsProvider` not registered; greeting still goes out without it.
- `test_greeting_skips_briefing_when_already_briefed_today` —
  `last_briefed_on=today` short-circuits the build_briefing call.
- `test_greeting_does_not_import_briefing_provider` — assert the
  `BriefingProvider` symbol does not exist in `interfaces/feeds.py`
  (regression test for the round-2 spec change).

## 20. Observability

### Per-feed (persisted on `Feed` row, no log scrape needed)

- `last_polled_at`, `last_error`, `consecutive_failures`,
  `last_poll_status_code`, `last_poll_items_total`,
  `last_poll_items_new`, `last_poll_duration_ms`,
  `suggested_poll_interval_sec`. The UI renders a "feed health"
  sparkline / pill from these fields.

### Logging

- Each poll logs at INFO: feed_id, items_total, items_new,
  duration_ms, status_code.
- Each scoring call logs at DEBUG: item_id, score, duration_ms.
- Each briefing logs at INFO: user_id, item_count, duration_ms.
- ChromaDB indexing failures (when `ingest_to_knowledge=True`) log
  at WARNING with the doc_id and the exception.
- 304 Not Modified logs at DEBUG (high-volume, low-signal).

### Service-level metrics (in-memory counters, exposed via
`feeds.metrics` WS RPC for the dashboard)

- `feeds_poll_total{status=ok|304|error}` — counter.
- `feeds_score_queue_depth` — gauge.
- `feeds_score_failures_total` — counter.
- `feeds_score_drops_total` — counter (queue-full drops).
- `feeds_ingest_total{outcome=ok|skip_paywall|skip_robots|skip_cap|error}` — counter.
- `feeds_briefings_built_total` — counter.

When `feeds_score_queue_depth` stays above
`max_concurrent_scoring * 25` for >5 min, log at ERROR with a
runbook-pointer-style message — the AI provider has gone slow and
queue is growing.

### Usage service

The standard usage service ([Usage Service
memory](../../.claude/memory/memory-usage-service.md)) records the
AI calls automatically because they go through
`AISamplingProvider.complete_one_shot` — no per-feature plumbing
needed for AI cost.

## 21. Migration / rollout

Since this is a green-field service:

1. Land the spec.
2. Land `interfaces/feeds.py`, `integrations/rss_feeds.py`,
   `interfaces/knowledge.py` (new `KnowledgeProvider` protocol),
   `integrations/feed_documents.py`, with tests.
3. Land `core/services/feeds.py`, `core/services/feed_briefing.py`,
   register both in `app.py`, with tests.
4. Land WS RPCs and the web UI.
5. Land the `GreetingService` integration and update its tests.
6. Update the root `README.md` "Bundled features" table to include
   "RSS / news feeds (`feeds`, `feed_briefing`)".
7. Update `std-plugins/README.md` only when the Reddit / HN backends
   actually ship — out of scope for this PR.

No data migrations needed (new collections only). The service is
toggleable and disabled by default for `feed_briefing`; `feeds` itself
defaults to enabled but is harmless without subscriptions.

## 22. Memory updates required at land time

After implementing, add or update memories per the
[CLAUDE.md memory rules](../../CLAUDE.md):

- **New memory:** `memory-feeds-service.md` covering the `FeedsService`
  + `FeedBackend` ABC, registry, scheduler integration, async scoring
  / ingestion queues, the per-feed runtime pattern, and the
  `build_briefing` text builder.
- **New memory:** `memory-feed-briefing-service.md` covering the
  fan-out + event publication role and the
  presence-vs-fallback resolution (§13.1).
- **Update:** `memory-greeting-service.md` (create if missing) — add
  the `include_briefing` flag, how it consumes `FeedsProvider` (NOT a
  separate `BriefingProvider`), and the today-already-briefed
  short-circuit.
- **Update:** `memory-knowledge-service.md` to mention the new
  `KnowledgeProvider` protocol (`index_document`, `remove_document`,
  `resolve_document`, `get_backend`, `backends` property).
- **Update:** `memory-inbox-service.md` to note the duck-typing
  cleanup once `InboxService` switches to `KnowledgeProvider`.
- **Update:** `memory-capability-protocols.md` — add `FeedsProvider`
  and `KnowledgeProvider` to the protocol table. **Do NOT add
  `BriefingProvider`** — that protocol is intentionally absent.
- **Update:** `MEMORIES.md` index for every new file.

## 23. Open questions / explicit non-decisions

### Resolved in round 2 (no longer open)

- **Per-feed scoring vs. user-level scoring.** Decided: v1 scores at
  the *item* level using a *service-wide* prompt + a free-form
  `user_interests` config field (concatenated into the user message,
  NOT the system prompt). Per-user scoring requires per-(user, item)
  score rows which 10x's storage — out of scope. Sharing semantics
  already work fine — the briefing pipeline filters by
  `briefing_eligible` and per-user accessibility, not by per-user score.
- **Feed item retention.** Decided: ship v1 with `retention_days=90`
  default and a `feeds-retention-tick` daily job. `0` = keep forever.
  Cascade includes `KnowledgeProvider.remove_document`. (Was punted
  to "v1.1 if storage growth becomes a problem"; engineering review
  correctly pushed back — easier to ship on day 1 than retrofit.)
- **Throttling AI calls.** Decided: real architectural decision (queue
  + workers + service-wide semaphores), not a hidden semaphore. See
  §6.4c, §6.4d, §6.7. `initial_score_cap=50` covers the
  first-sync-burns-the-budget case.

### Open / deferred

- **Per-user `briefing_hour` and `timezone`.** v1 is single global
  time zone. Multi-zone households / shared deployments get
  server-zone-7-AM. v1.x adds per-user overrides plus a 15-min
  briefing-tick. Tracked here so the data shape doesn't ossify.
- **Bulk re-ingest of cached feed articles to knowledge.** v1 only
  exposes single-item re-ingest via the `feeds.items.reingest` WS
  RPC. Bulk re-index of a feed (e.g., after retuning the
  `KnowledgeService` chunker) is v1.x.
- **"Drown in feeds" anti-patterns.** Tools we considered but did
  NOT include in v1, flagged for v1.x:
  - `mute_feed(feed_id, days=7)` — temporary mute without unsubscribe.
  - "Feed health" view — which feeds the user has read 0 items from
    in the last 30 days, with a one-click "Cleanup" suggestion.
- **Tag vocabulary for scoring output.** Tags were dropped from v1
  scoring output entirely (free-form tags = tag explosion). If
  tags are added later, they MUST come from a fixed/curated
  vocabulary the prompt can constrain to.
- **`FeedBackend` plugins (Reddit, HackerNews, YouTube, podcasts).**
  Out of scope for this PR; the ABC was designed to slot them in
  without core changes.
- **Per-user `briefing_max_seconds` actually constraining the AI
  output length.** The spec passes `max_spoken_seconds` as a hint;
  enforcement is the model's responsibility. If post-hoc validation
  is needed (e.g., truncation when the model overshoots), that's a
  v1.x refinement.

---

**End of spec.** Implementer: read this start-to-finish, then re-read
the inbox service and the AI-prompts-configurable memory before
opening `feeds.py`. The trick to nailing it is recognizing how much of
this is "inbox with the email parts swapped out" — lean into that;
deviation should require justification.

---

## Revision Log — Round 2

Three independent reviewers (architect, product, engineering) raised
overlapping issues. Round-2 changes, organized by reviewer concern:

### Architect review — applied

- **Dropped `BriefingProvider`** as overengineering (§5, §16, §17).
  `build_briefing` is a single method that reads from feed storage,
  applies feed-scoped policy, and uses the briefing prompt — all of
  which already belong on `FeedsService`. Moved `build_briefing` onto
  `FeedsProvider`. Greeting integration consumes `FeedsProvider`, not
  a parallel briefing protocol. `FeedBriefingService` survives only
  as a daily fan-out + event publisher (no AI calls of its own, no
  prompt config of its own).
- **Synthetic `feed_articles` `DocumentBackend` is owned privately by
  `FeedsService`** and is **NOT** registered with `KnowledgeService`
  (§9, §17). `FeedsService` calls `KnowledgeProvider.index_document`
  directly with the synthetic backend instance it owns. The
  periodic knowledge-sync loop never sees feed articles — feeds are
  push-on-receive, not pull-on-sync. `list_documents()` returns `[]`
  defensively.
- **Verified `SpeakerProvider` exists** at
  `src/gilbert/interfaces/speaker.py` (used by `GreetingService`,
  `MusicService`, `RoastService`, `DoorbellService`,
  `AudioOutputService`). No new protocol needed; the spec's reference
  was correct.
- **Justified `feedparser` in `integrations/`** with the
  small + permissive + provider-neutral test (§3 "Note on
  `integrations/` precedent"), so the next "but my dep is small too"
  PR has a clear test.
- **Audited `KnowledgeProvider` shape** and added `remove_document`,
  `resolve_document`, `get_backend` (§18.1). All four methods already
  exist on `KnowledgeService` — the protocol is wrapping the existing
  public surface, not designing new API.
- **Briefing prompt moved to `FeedsService`** (§8.3, §11) — the
  service that runs the AI call owns the prompt.
- Briefing AI profile bumped to `"medium"` default (§11) — a
  user-facing varied-tone paragraph is harder than a JSON
  classification.

### Product review — applied

- **Resolved daily-fire vs. presence-greeting race** (§13.1) —
  presence-first, fallback fires `presence_grace_minutes` after
  `briefing_hour`. `briefed_at` (timestamp) replaced the boolean
  `briefed` field; `feed_briefing_state.last_briefed_on` (date-only)
  short-circuits both paths from running on the same day. The daily
  fire never eats items the greeting flow needs.
- **Two-artifact split** (§8.3, §10 `news_briefing`) — single
  `complete_one_shot` returns `{spoken, headlines}`. TTS gets prose;
  dashboard / chat gets a clickable headline list. Closed the
  "wall of summaries on a screen" gap.
- **Anti-repetition** (§8.3) — `feed_briefing_state.recent_briefings`
  (last 10) fed into the user message so the briefing voice varies
  across days.
- **`subscribe_feed` is a confirmation-block tool** (§10) — returns
  a `ToolOutput` with `ui_blocks`, does NOT persist. UI Confirm
  fires `feeds.create`. Same shape for new `unsubscribe_feed`.
- **Folded `mark_feed_item` into `read_feed_item`** (§10) — auto-mark
  read with `mark_read: bool = True`. Power-user mark-unread is
  web-UI only.
- **Added `unsubscribe_feed` AI tool** (§10).
- **Added `recommend_knowledge_ingestion`** (§10) so the
  ingest-to-knowledge flag is actionable, not buried in the editor.
- **Briefing event payload narrowed** (§12) — no `spoken_text` on
  the event, just `briefing_id`. Consumers RPC-fetch via
  `feeds.briefing.get`. Privacy: briefing content stays out of WS
  logs.
- **`tools_override=[]` on the briefing AI call** (§8.3) — prevents
  the Sonos audio-clip-loop recursion bug from
  `memory-ai-context-profiles.md`.
- **`news_briefing` cached today-already-briefed result** (§10) —
  spamming the tool can't burn AI budget.
- **`tags` dropped from v1** (§4, §8.1) — free-form AI tags lead to
  tag-explosion; out of scope.
- **`/feeds today` and `/feeds add` aliases** (§10) for discoverability.
- **`/feeds list` defaults to compact format** (§10).
- **`read_feed_item` accepts partial-id and `latest [category]`** (§10).
- **`search_feeds` paginated** (§10).
- **`first_sync` AI cap promoted to hard requirement** (§6.4e, §11)
  — `initial_score_cap=50`.
- **System briefing renamed and clarified** (§13.4, §11) — replaced
  the muddy `auto_announce` flag with `system_briefing_enabled` /
  `system_briefing_user_id` / `announce_speakers`. "Did we wake up
  the speakers" and "did we generate a per-user briefing" are now
  separate decisions.
- **Per-user `briefing_opt_in`** (§13.3) — role-shared-only users
  default to off, so a 10-person team sharing a feed doesn't get 10
  unwanted briefings.
- **`BriefingNotification` UI surface** (§15) — toast for unread
  briefings when presence misfires.

### Engineering review — applied

- **HTTP politeness contract** specified in full (§7) — required
  outbound headers (UA, Accept, Accept-Encoding, If-None-Match,
  If-Modified-Since), `max_response_bytes` cap (10 MiB default), 304
  handling, encoding allow-list (gzip/deflate only).
- **`Feed.http_cache` field** added (§4) — separate from
  `backend_config` so a UI save can never clobber bookkeeping.
  Backend round-trips it via `PollResult`.
- **Async scoring queue** (§6.4c) — scoring decoupled from poll loop.
  Bounded workers, semaphore, `feed.item.scored` event for late
  arrival, `feeds-rescore-tick` for retry, queue-full drops counter.
  `feed.item.received` no longer carries score; `is_high_signal`
  field dropped.
- **Async ingestion queue** (§6.4d) with separate concurrency cap.
- **Item-update / edit detection** (§6.4a) — `updated_at` field on
  `feed_items`; first-write-wins on score/briefed/read; title and
  summary mutable; never re-emit `feed.item.received`.
- **No short-circuit on first-known-item** (§6.4) — Atom feeds with
  `<atom:updated>` ordering would corrupt dedup. Iterate up to
  `max_items` and let storage handle dedup cost.
- **RSS `<ttl>` and `Cache-Control: max-age` respected** (§6.7) —
  `PollResult.suggested_min_interval_sec`; effective cadence is
  `max(configured, suggested)`.
- **First-poll-storm bound** (§6.3) — jitter on subscribe + service-
  wide `_poll_semaphore` for ALL `_poll_runtime` paths.
- **Concurrency caps promoted to `ConfigParam`s** (§6.7, §11) —
  `max_concurrent_polls`, `max_concurrent_scoring`,
  `max_concurrent_ingestion`, `max_first_poll_jitter_sec`.
- **Graceful give-up at 20 consecutive failures** (§6.5) — auto
  `poll_enabled=False`, emit `feed.subscription.disabled`.
- **Back-off formula stated explicitly** (§6.5).
- **Retention shipped in v1** (§11, §23) — `retention_days=90`
  default; daily `feeds-retention-tick` job.
- **`KnowledgeProvider.remove_document`** added (§18.1) for
  retention and unsubscribe cascade.
- **Knowledge ingestion hardened** (§9) — `robots.txt` honored,
  Content-Type guard (HTML only), paywall heuristic, redirect cap
  (5), https→http downgrade rejected, eTLD+1 SSRF guard,
  per-user-per-day cap, `feed.ingest.throttled` event.
- **Recency decay in briefing** (§8.3) — within the `since` window,
  fresher items win.
- **OPML import / export** added (§14) — RSS-reader-equivalent
  import/export so users can adopt Gilbert without retyping URLs.
- **JSON-fence-tolerant scoring parser** (§8.1).
- **`user_interests` sanitized** (§11) — prompt-injection mitigation.
- **`tools_override=[]` on briefing call** explicitly stated (§8.3).
- **Test plan expanded** (§19) — HTTP mock transport injection (no
  network in CI), edit-detection, retention cascade, 304 handling,
  paywall skip, robots.txt, per-day cap, queue drain on stop, etc.
- **Observability gauges + counters** (§20) — feed-row health fields
  for the UI sparkline; service-level counters for poll/score/ingest;
  alerting threshold for runaway score queue.
- **`feeds.poll_now` returns sync result** (§14).
- **`feeds.briefing.preview` does NOT mark briefed and does NOT
  publish event** (§14, §8.3 `mark_briefed=False`).
- **`_id` separator changed from `:` to `__`** (§4) — `:` collides
  with URL-shaped fallback `item_uid`s.
- **Time-zone limitation acknowledged** (§13.2) — single global v1.

### Conflicts resolved

- **Architect vs. product on `briefing_prompt` location.** Architect
  said move to `FeedsService` (owning service); product was silent on
  location. Applied architect's call — both `summarization_prompt`
  and `briefing_prompt` now live on `FeedsService` because that's
  where the AI calls are made.
- **Architect vs. engineering on synthetic backend registration.**
  Both reviewers reached the same conclusion (don't register;
  `FeedsService` owns the instance privately). No conflict; applied.
- **Product wanted `daily_briefing` renamed to discoverable verb;
  engineering wanted `news_briefing` for grep-ability.** Picked
  `news_briefing` as the canonical with `slash_aliases=["today"]`
  for discoverability — both concerns satisfied.
- **Product floated dropping `auto_announce` entirely; engineering
  was silent.** Kept the capability but renamed to
  `system_briefing_*` so the policy is teachable.

### Items NOT applied (with rationale)

- Product's "drop tags entirely from v1": **applied** (was uncertain
  in original review — confirmed dropped).
- Engineering's `last_build_date` on `FeedMeta`: **deferred** — nice
  but cosmetic; can be added in v1.x without schema migration.
- Architect's "if `SpeakerProvider` doesn't exist, drop
  `auto_announce`": moot — the protocol exists.
- Product's per-feed mute (`mute_feed`) tool and "feed health"
  cleanup view: **moved to §23 deferred** — useful but not v1
  blockers.
