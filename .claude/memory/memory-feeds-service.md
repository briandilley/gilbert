# Feeds Service

## Summary
Multi-feed RSS / Atom service mirroring `InboxService`. Each feed
subscription is owned by a user and can be shared with users / roles;
the service runs one `FeedBackend` instance + one
`feeds-poll-{feed_id}` scheduler job per `poll_enabled` feed. Items
are persisted in `feed_items` (tagged with `feed_id`); scoring and
optional knowledge ingestion happen on bounded async worker pools so a
slow AI provider can never block the next poll. The `build_briefing`
method is the AI-driven daily briefing builder — owned here because
the prompt belongs where the AI call is made.

## Details

### Data model

Five entity collections owned by `FeedsService`:

| Collection | Key fields |
|---|---|
| `feeds` | `id`, `name`, `url`, `backend_name`, `backend_config`, `owner_user_id`, `shared_with_users`, `shared_with_roles`, `poll_enabled`, `poll_interval_sec`, `category`, `importance_weight`, `ingest_to_knowledge`, `briefing_eligible`, `last_polled_at`, `last_error`, `consecutive_failures`, `last_poll_status_code`, `last_poll_items_total`, `last_poll_items_new`, `last_poll_duration_ms`, `http_cache`, `suggested_poll_interval_sec`, `created_at` |
| `feed_items` | `_id = "{feed_id}__{item_uid}"` (double-underscore so URL-shaped fallback uids don't collide on `:`), `feed_id`, `item_uid`, `title`, `link`, `summary`, `author`, `published_at`, `updated_at`, `received_at`, `ai_summary`, `score` (`-1.0` until scored), `score_reason`, `read`, `briefed_at` (timestamp, `""` when never), `ingested_to_knowledge`, `enclosure_url`, `enclosure_mime`, `lazy_score` |
| `feed_briefings` | `_id = "brief_<uuid12>"`, `user_id`, `spoken`, `headlines`, `item_ids`, `since`, `created_at` |
| `feed_briefing_state` | `_id = user_id`, `last_briefed_on` (date), `last_briefing_id`, `recent_briefings` (last-10 spoken texts, FIFO trimmed), `briefing_opt_in` |
| `feed_ingest_daily` | `_id = "{user_id}:{YYYY-MM-DD}"`, `count` — per-user ingestion budget |

Indexes: `feeds(owner_user_id)`, `feeds(poll_enabled)`,
`feed_items(feed_id, received_at)`, `feed_items(feed_id, item_uid)`,
`feed_items(read)`, `feed_items(score)`, `feed_items(briefed_at)`.

**Storage rule:** never persist the full article body. The service
stores `title + link + summary + ai_summary + score`. Full content
flows through the system only when the per-feed
`ingest_to_knowledge=True` flag is set, in which case the bytes are
handed to `KnowledgeProvider.index_document` and immediately
discarded locally (cached files under `.gilbert/feed-cache/` are for
observability only; the synthetic backend never lists them).

### Authorization

Single rule, in `interfaces/feeds.py`:

- `can_access_feed(user_ctx, feed, *, is_admin)` — admin OR owner OR
  user in `shared_with_users` OR any role overlap with
  `shared_with_roles`. Grants read + briefing-eligibility + tool
  visibility.
- `can_admin_feed(user_ctx, feed, *, is_admin)` — admin OR owner only.
  Gates settings, share edits, unsubscribe.
- `determine_feed_access(user_ctx, feed, *, is_admin)` — returns the
  `FeedAccess` tag (`owner`/`admin`/`shared_user`/`shared_role`) for
  UI grouping. Owner > admin > shared_user > shared_role.

### Backend ABC + built-in

`FeedBackend` ABC in `interfaces/feeds.py` follows the universal
backend pattern (`__init_subclass__` registry, `backend_name`,
`backend_config_params()`, `initialize` / `close`, `probe`, `poll`).
The single built-in concrete backend is
`RssAtomFeedBackend` in `src/gilbert/integrations/rss_feeds.py` —
the only feed backend that lives in `integrations/`. The "may live
in core integrations/" test (vendor-free + small + provider-neutral)
is documented in spec §3 so the next "but my dep is small too" PR
has a clear test to pass or fail.

`feedparser>=6.0.10` is the only new core dep. Provider-specific
backends (Reddit, HackerNews, YouTube, podcasts) belong in
`std-plugins/` — explicitly out of scope for v1 but the ABC was
designed to slot them in without core changes.

### HTTP politeness contract

Implemented inside `RssAtomFeedBackend._fetch`:

- `User-Agent` from backend config (default
  `"GilbertFeeds/1.0 (+https://github.com/briandilley/gilbert)"`).
- `Accept: application/atom+xml, application/rss+xml,
  application/xml;q=0.9, */*;q=0.8`.
- `Accept-Encoding: gzip, deflate` (httpx decompresses transparently;
  `br` / `zstd` / unknown encodings fail closed with `FeedError`).
- Conditional GET: `If-None-Match` (etag), `If-Modified-Since`. The
  service round-trips `Feed.http_cache` via `PollResult.http_cache`.
  **Backends MUST NOT touch `Feed.backend_config`** — that's
  user-supplied settings and a UI save would clobber bookkeeping.
- Body-size cap (`max_response_bytes`, default 10 MiB) — raises
  `FeedTooLargeError` when exceeded.
- Hard cap of 5 redirects; reject `https → http` downgrade.
- `<ttl>` (RSS, in minutes × 60) and `Cache-Control: max-age` are
  surfaced via `PollResult.suggested_min_interval_sec`. The service's
  effective cadence is `max(feed.poll_interval_sec, suggested)`.

### Runtime lifecycle

`FeedsService` keeps `_runtimes: dict[feed_id, _FeedRuntime]`. Each
runtime owns one `FeedBackend` instance + one
`feeds-poll-{feed_id}` scheduler job. On `start()`:

1. Side-effect import `gilbert.integrations.rss_feeds` so the
   built-in registers with the registry.
2. Resolve required (`entity_storage`, `scheduler`) and optional
   (`event_bus`, `knowledge`, `configuration`, `access_control`,
   `ai_chat`) capabilities.
3. Spawn the synthetic `feed_articles` `DocumentBackend` privately
   (NOT registered with `KnowledgeService`).
4. Build the article-fetch `httpx.AsyncClient`.
5. Spawn `max_concurrent_scoring` score workers and
   `max_concurrent_ingestion` ingest workers.
6. Schedule `feeds-boot` (one-shot, fires `_boot_runtimes` off the
   start critical path), `feeds-rescore-tick` (every 30 min), and
   `feeds-retention-tick` (daily).

`_start_runtime` applies a **mandatory cold-start jitter** of
`random.uniform(0, min(poll_interval_sec, 120))` on the first fire so
N runtimes don't synchronously hit their backends on Gilbert restart.

`update_feed` restarts the runtime only when one of `url`,
`backend_name`, `backend_config`, `poll_enabled`, or
`poll_interval_sec` changes. `name`, `category`,
`importance_weight`, `ingest_to_knowledge`, `briefing_eligible`,
share lists do NOT trigger a restart — read at use-time.

### Polling

`_poll_runtime` flow per fire (per-feed `_poll_locks` lock + service-
wide `_poll_semaphore`):

1. Backend `poll(url, since=last_polled_at, max_items, http_cache)`.
2. `not_modified=True` → bump `last_polled_at`, do NOT bump
   `consecutive_failures`.
3. Otherwise dedup-and-persist via `_dedup_and_persist` — does NOT
   short-circuit on first known item (Atom feeds with
   `<atom:updated>` ordering would corrupt dedup).
4. For each new item: publish `feed.item.received` immediately
   (`score=-1.0`), enqueue scoring (if `score_on_ingest`), enqueue
   ingestion (if `feed.ingest_to_knowledge`).
5. **Edited items** (existing `_id` + newer `updated_at` + `title` /
   `summary` differ) — update only `title`, `summary`, `updated_at`.
   **Never** re-emit `feed.item.received`, never re-enqueue scoring,
   never bump `briefed_at` (first-write-wins on those).

### Error handling, back-off, give-up

- `consecutive_failures >= 3` widens cadence:
  `min(86400, base * 2 ** (consecutive_failures - 2))`.
- `consecutive_failures >= 20` flips `poll_enabled=False`, stops the
  runtime, emits `feed.subscription.disabled`. Prevents a forgotten
  / dead feed from accumulating logspam forever.
- HTTP 304 = success, no failure bump; 401/403 raises `FeedAuthError`
  (failure); 404 raises `FeedNotFoundError` (failure).

### Async scoring queue

`_score_queue: asyncio.Queue` (maxsize 10000). `max_concurrent_scoring`
worker tasks (default 4) drain the queue. Each worker calls
`_score_item` which:

- `complete_one_shot` with `system_prompt=self._scoring_prompt` and
  `tools_override=[]` (mandatory — prevents the recursion bug from
  `memory-ai-context-profiles.md`).
- Parses JSON; **tolerates a single leading
  `^```(?:json)?\n` and trailing `\n```$` fence** before
  `json.loads` (saves ~5% of cheap-profile failures per spec §8.1).
- Stored score is `min(1.0, raw_score * feed.importance_weight)`.
- On parse failure: `score=-1.0`, `lazy_score` survives, logged at
  WARNING.
- On queue-full: drop with WARNING + `feeds_score_drops_total`
  counter increments.

`feeds-rescore-tick` (every 30 min) sweeps `score == -1.0 AND
received_at > now - 24h` items and re-enqueues them, capped at
`max_concurrent_scoring * 10` per tick.

`initial_score_cap=50` (default) bounds first-sync AI fan-out — items
beyond the cap stay at `score=-1.0` until lazy-scored on tool read.

### Knowledge ingestion

Per-item flow when `feed.ingest_to_knowledge=True`:

1. Acquire `_ingest_semaphore`.
2. Per-user-per-day cap (`ingest_max_items_per_day_per_user`,
   default 200). Above cap → emit `feed.ingest.throttled`, skip.
3. SSRF / politeness checks: same eTLD+1 host (or refuse `localhost`/
   private hosts), `robots.txt` honored when `respect_robots_txt`
   (cached 1h).
4. Fetch body — 10s timeout, 256 KB cap, max 5 redirects, reject
   `https → http`.
5. Content-Type guard (HTML / XHTML only).
6. Paywall heuristic — strip HTML to text, skip if < 1 KB or matches
   bundled paywall regex.
7. Cache bytes to `.gilbert/feed-cache/<feed_id>/<safe_uid>.html`
   (observability only — the synthetic backend's `list_documents`
   returns `[]`).
8. `KnowledgeProvider.index_document(synthetic_backend, meta)` —
   never via `KnowledgeService._sync_backend` (would re-walk every
   sync).

The synthetic `feed_articles` `DocumentBackend` lives at
`src/gilbert/integrations/feed_documents.py`. It IS registered in
`DocumentBackend._registry` (so future plugins can reference the
source_id), but the **single instance** is owned PRIVATELY by
`FeedsService` — never registered with `KnowledgeService._backends`.
`list_documents()` returns `[]` defensively.

Cascade: retention purge AND unsubscribe call
`KnowledgeProvider.remove_document(document_id)` for any item with
`ingested_to_knowledge=True`.

### Briefing builder (`build_briefing`)

Lives on `FeedsService` (NOT a separate `BriefingProvider`). Per
spec §5 / Round 2 architect: `build_briefing` reads from feed
storage, applies feed-scoped policy (`briefing_eligible`, `score`,
`briefed_at`), and uses prompt config that already lives on
`FeedsService` — a second protocol for one method on the same
service the caller already resolved by name was overengineering.

Flow:

1. Resolve every feed `user_ctx` can access.
2. Filter to `briefing_eligible=True`, optionally one category.
3. Pull items where `briefed_at == "" AND received_at >= since AND
   score >= 0`, sorted by recency-decayed score
   (`score * exp(-age_hours/24)`).
4. Top `top_n`.
5. **Single AI call** returning JSON with both `spoken` (TTS-shaped
   paragraph, ~200 words / `max_spoken_seconds * 2.5`) and
   `headlines` (clickable list). `tools_override=[]` — mandatory.
6. Anti-repetition: pull `feed_briefing_state.recent_briefings`
   (last 10) and pass into the user message.
7. On parse failure: deterministic fallback ("Top stories: …").
8. If `mark_briefed=True`: stamp `briefed_at` on chosen items,
   persist briefing record, append `spoken` to `recent_briefings`,
   set `last_briefed_on=today`.

### AI tools (8)

Slash group `feeds`:

| Name | Slash | Notes |
|---|---|---|
| `news_briefing` | `/feeds briefing` | Two-artifact output. Today-cached short-circuit. |
| `search_feeds` | `/feeds search` | Paginated. `parallel_safe`. |
| `summarize_feed` | `/feeds summarize` | Caches `ai_summary`. |
| `subscribe_feed` | `/feeds subscribe` | UIBlock confirm — does NOT persist directly. |
| `unsubscribe_feed` | `/feeds unsubscribe` | UIBlock confirm — does NOT persist. |
| `list_feeds` | `/feeds list` | Compact by default. `parallel_safe`. |
| `read_feed_item` | `/feeds read` | Auto-mark-read. Accepts `latest` / `latest <category>`. |
| `recommend_knowledge_ingestion` | `/feeds recommend-knowledge` | Returns recommendations; does NOT flip flag. |

### Configuration (`feeds` namespace)

Service-level: `enabled`, `max_items_per_poll`, `max_summary_length`,
`default_poll_interval_sec`, `summarize_on_ingest`, `score_on_ingest`,
`initial_score_cap`, `max_concurrent_polls`, `max_concurrent_scoring`,
`max_concurrent_ingestion`, `max_first_poll_jitter_sec`,
`retention_days`, `ingest_max_items_per_day_per_user`,
`respect_robots_txt`, `scoring_ai_profile`,
`summarization_ai_profile`, `briefing_ai_profile`, `user_interests`
(sanitized before concat).

**Three `ai_prompt=True` ConfigParams** — `scoring_prompt`,
`summarization_prompt`, `briefing_prompt` — plus a
`knowledge_recommendation_prompt`. All four cached on
`self._<name>_prompt` in `on_config_changed`; falsy override falls
back to the bundled `_DEFAULT_*_PROMPT` constant.

### Events published

All carry `feed_id` (and `item_id` for item events):

- `feed.item.received` (no `score` — scoring is async)
- `feed.item.scored`
- `feed.subscription.created` / `.updated` / `.deleted`
- `feed.subscription.shares.changed`
- `feed.subscription.disabled` (auto-disabled at 20 failures)
- `feed.ingest.throttled`

`acl.py` puts `"feed."` at level 100 (user). The WS layer's
per-feed-access filter applies on top, modeled on inbox.

### OPML

`import_opml` walks `<outline>` entries and calls `subscribe` per
entry; returns `[(url, ""|error)]`. `export_opml` returns OPML 2.0
text for every accessible feed.

## Related
- [Inbox Service](memory-inbox-service.md) — closest analog
- [Feed Briefing Service](memory-feed-briefing-service.md) — daily fan-out
- [Knowledge Service](memory-knowledge-service.md) — `KnowledgeProvider` consumer
- [AI Prompts Are Always Configurable](memory-ai-prompts-configurable.md)
- [Backend Pattern](memory-backend-pattern.md)
- [Capability Protocols](memory-capability-protocols.md) — `FeedsProvider`
- `src/gilbert/interfaces/feeds.py` — backend ABC, dataclasses, helpers, FeedsProvider
- `src/gilbert/integrations/rss_feeds.py` — built-in `RssAtomFeedBackend`
- `src/gilbert/integrations/feed_documents.py` — synthetic `feed_articles` backend (privately owned)
- `src/gilbert/core/services/feeds.py` — service implementation
- `tests/unit/test_rss_atom_backend.py`, `tests/unit/test_feeds_service.py`

