# Open Questions Across All 8 Specs

These need a human decision before or during implementation. Not bugs — design forks the spec authors deliberately left open.

---

## Decisions Locked Before Spec PR (2026-05-09)

The cross-cutting and load-bearing decisions have been resolved by the project owner. Implementer agents must read this section first and apply the choices below as if they were in the spec body.

### Cross-cutting

- **Encryption-at-rest for backend secrets:** **DEFER.** Keep v1 plaintext for OAuth tokens, service-account JSON, API keys, etc. (Same shape as existing Gmail backend.) Document the gap in `std-plugins/README.md` once the first new plugin in this initiative ships. A separate "secrets at rest" PR will follow this initiative; do not block calendar / Plex / Withings on it. Affects: #01-calendar §Open Questions #8, #07-media-library §23, #08-health §19 #1.

- **`UserContext.tz` typed field:** **PROMOTE IN FEATURE 03's PR.** Feature 03 (notification fan-out) is the first feature that needs per-user tz for quiet-hours math, so its PR includes the precursor: add `tz: str | None` (IANA, validated) to `UserContext` in `src/gilbert/interfaces/auth.py`, expose it on the user profile UI, and seed `null` for existing users. Features 05 (tasks) and 08 (health) inherit the typed field on their respective branches; they should drop their `metadata["tz"]` workarounds and read `user_ctx.tz` directly. Affects: #03 §Open Questions #4, #05 §21 #4, #08 (DST daily-summary).

- **UIBlock preview/confirm helper for mutating tools:** **PER-FEATURE, SHARE A HELPER.** Feature 01 (calendar) extracts a shared `confirm_or_execute()` (or similarly-named) helper into `src/gilbert/core/services/_ui_blocks.py` (or wherever the existing UIBlock helpers live). Subsequent features (#06 `mute_camera_alerts`, #08 health-delete wizard, future mutating tools) reuse it. **Do NOT retrofit existing inbox / music / etc. tools in this initiative** — that is a separate UX pass. Affects: #01 §Open Questions #3, #06 §18 #6.

### Spec-local high-impact (using spec author / reviewer recommendations)

- **#02 weather — per-user severe-alert polling:** v2. v1 polls only the service-default `home_location`.
- **#02 weather — admin-on-behalf prefs:** v2. `/weather set_home` always writes the caller's own row in v1.
- **#03 notifications — presence-gated delivery:** v2. v1 default is "always deliver, filter by route rules."
- **#05 tasks — inbox-AI add_task on non-Gilbert sender with default-list resolution failure:** **fail loud** in the email reply (per spec recommendation).
- **#06 frigate — `VisionBackend.model_name`:** **defer.** Take the cheap path; payload omits `vision_model`. Do not touch existing vision backends in this PR.
- **#06 frigate — re-add `identify_visitors` v2:** Decision deferred to a follow-up PR. v1 ships deterministic `who_was_seen` + `latest_clips` only. If you reach for the LLM-correlated version during implementation, STOP and flag the human.
- **#07 media — N:M Gilbert↔backend user mapping:** v2. v1 enforces 1:1 via unique index.
- **#08 health — step-up auth on `health-admin`:** v2. v1 grants `health-admin` as a one-time role assignment. **Flag this for the human if a regulated-deployment scenario comes up during implementation.**
- **#08 health — server-side prompt post-filter ("no diagnosis" enforcement):** **trust users.** v1 ships the bundled prompt; users editing it own the consequences (their own data, their own prompt).

### Medium-impact and low-impact items below

For everything else, implementer agents may use spec author defaults silently. If a defaulted answer materially changes a tool surface, AI prompt, or user-visible behavior in an unexpected way, flag the human via `AskUserQuestion`.

---

## High-impact (block or substantially change a spec if decided differently)

### #01-calendar
- **Q: Standardize the preview/confirm UIBlock pattern across every mutating AI tool, or keep it calendar-only?** Calendar ships preview+confirm for `create/update/delete_event`. Inbox today fires invites/notifications without confirmation, which has the same blast-radius problem. Reviewer recommendation is to hoist a shared helper. Cross-feature decision the calendar spec can't resolve unilaterally. Spec section: §Open Questions / Risks #3.
- **Q: Is encryption-at-rest for `backend_config` (service-account JSON, OAuth tokens) being addressed at the storage layer in a separate effort?** Calendar, Gmail, Withings, Plex etc. all store secrets plaintext in SQLite today. If yes, the spec defers; if no, std-plugins README documents the gap. Same question recurs in #07 (Plex `account_token`), #08 (Withings OAuth tokens). Spec sections: #01 §Open Questions #8, #07 §23 (Plex), #08 §19 #1.

### #02-weather
- **Q: Is per-user severe-alert polling a v2 priority?** v1 polls only the service-default `home_location`; users with a per-user `home_location` different from the admin home do NOT get alerts for their location. Cost is N polls/interval where N = unique configured user locations. Spec section: §Open Questions / Future #1.
- **Q: Should an admin be able to set another user's `home_location` (admin-on-behalf prefs)?** v1 says no — `/weather set_home` always writes the caller's own row. Follow-up shape would be `set_user_home_location(user_id, query)` admin-only. Spec section: §Open Questions / Future #2.

### #03-notification-fanout
- **Q: Does the user/auth profile schema already expose a `users.<user_id>.timezone` field?** This spec depends on it for quiet-hour tz fall-through. If missing, this spec inherits the responsibility to add it (small change in `interfaces/auth.py` + the user profile UI). Implementer needs confirmation on PR-1 kickoff. Spec section: §Open questions #4.
- **Q: Should presence-gated delivery ship in v1?** Request hinted at "skip delivery when user is here." v1 says no — easier semantics, "always deliver, filter by route rules" is what users expect. v2 hook is `route.deliver_when: always | when_offline | when_no_active_session`; the no-active-session variant requires a new `WsSessionInformation` capability protocol. Spec section: §Open questions #1.

### #05-tasks
- **Q: When inbox-AI tries to add a task on behalf of a non-Gilbert sender and default-list resolution fails, should we fail loudly (visible in the email reply) or silently skip the `add_task` and reply normally?** Spec recommends fail-loudly so the user can see and act. Affects every email-driven task creation flow. Spec section: §21 Genuinely open #1.
- **Q: Promote `UserContext.tz` to a typed field on `UserContext`, or keep using `UserContext.metadata["tz"]`?** Cross-cutting change affecting every service. Tasks spec uses metadata for now but flags it. Same downstream concern raised in #03 (quiet-hours tz) and #08 (DST daily-summary). Spec section: §21 Genuinely open #4.

### #06-frigate-cameras
- **Q: Add `model_name: str` to `VisionBackend` (and update `local_vision`, `anthropic_vision`, etc. + `VisionService`) in a wider PR before camera v1, or accept the "cheap path" (no `model_name`, payload omits `vision_model`)?** Spec currently picks the cheap path. Wider PR touches every existing vision implementation. Spec section: §18 #2.
- **Q: Ship `identify_visitors` (LLM-correlated face matches + vision prose + presence) in v2, or never re-add it?** v1 ships deterministic `who_was_seen` + `latest_clips`. The earlier `who_was_at` produced confidently wrong identifications and was dropped. A v2 version would need strict unknown-surfacing in the prompt. Decision shapes whether the camera AI surface ever grows beyond deterministic counts. Spec section: §18 #1.

### #07-media-library
- **Q: Allow N:M Gilbert-user → backend-user mapping (one Gilbert user → many Plex Home users) in v2, or keep 1:1 forever?** v1 enforces 1:1 via a unique index. Relaxing requires a primary-flag or fan-out-and-merge semantic before the index can be loosened. Affects "household merged view" use cases. Spec section: §23 #7.

### #08-health
- **Q: Does the cross-user `health-admin` role need step-up auth (sudo-style fresh re-authentication on each access)?** v1 makes `health-admin` a one-time grant. PHI-style flows in regulated industries usually require fresh re-auth per access. Decision affects whether Gilbert can be deployed in regulated contexts. Spec section: §19 #10.
- **Q: Does the bundled "no diagnosis / no treatment" prompt language need a server-side post-filter that flags forbidden words and re-prompts, or do we trust user-owned prompt edits?** v1 trusts users (their own data, their own prompt). Revisit if the system is ever shared across users with elevated medical-context risk. Spec section: §19 #7.

### #09-google-oauth
- **Q: Should the Gilbert project publish its own Google-verified OAuth client so end users do zero GCP setup?** v1 requires each operator to create their own GCP project / service account / OAuth client (the in-app wizard makes this a guided ~5-minute task). A maintainer-owned verified client removes all GCP setup for end users but imposes: Google's 100-user cap until verified; full OAuth verification plus an annual CASA security assessment for the restricted Gmail/Drive/Calendar scopes; and the project becoming Google's registered data controller. Pure governance/maintainer decision the spec cannot resolve. Spec section: §Open Questions / Risks #2 (b).
- **Q: Cross-spec — does #09's `shared_service_account` model conflict with #01-calendar's "Multi-calendar aggregation per account" non-goal?** #01 pins one `CalendarAccount` → one calendar id; #09's shared SA fronts many users' shared calendars. Recommended resolution (#09 §Open Questions #1): keep #01's invariant — one account row per shared calendar, the SA key reused across rows, the backend must `calendarList().insert` the shared id and must not implicitly aggregate. Needs the #01 author to land the doc note; sequencing: #09's resolver merges before #01's backend. Spec section: §Open Questions / Risks #1 (a).

## Medium-impact (affect scope or UX, not architecture)

### #01-calendar
- **Q: Add richer per-attendee visibility-status indicators in the SPA for `find_free_time` cross-user free-busy?** Google may return `errors:[{reason: "insufficientPermissions"}]` for a target email; visibility varies dramatically with the other party's sharing settings. v1 surfaces warnings in the tool result string only. Spec section: §Open Questions / Risks #9.
- **Q: Confirm tentative-event semantics: `find_free_time` treats tentative as busy; `get_schedule`/`next_event` show them with `status: "tentative"`?** Differential semantics are intentional and pinned in the algorithm description, but worth product confirmation before lock-in. Spec section: §Open Questions / Risks #10.
- **Q: When a follow-up PR adds a "summarize my day" helper, does soul/identity carry enough flavor or do we want a calendar-specific tone prompt?** Spec author leans toward soul/identity; flagged for the follow-up PR's design. Spec section: §Open Questions / Risks #11.

### #02-weather
- **Q: Add a v1 consumer of the `weather.digest` event in this PR, or ship the event with no consumer for future "morning summary announcement"?** Currently no v1 consumer is wired. Spec section: §Open Questions / Future #10.
- **Q: Confirm the dispatcher accepts registering both `slash_group=weather + slash_command=now` AND a top-level `slash_command=weather` alias for the same `current_weather` tool?** Muscle-memory shortcut. Implementation detail but needs verification. Spec section: §Open Questions / Future #11.
- **Q: Add an `ai_visible` flag on `ToolDefinition` (preferred), or use a different mechanism (e.g. registering write tools under a separate provider that doesn't declare `ai_tools`) for slash-only tools like `set_home_location` / `set_units`?** Spec's preferred path is the named flag. Pivot if review prefers another mechanism. Spec section: §Open Questions / Future #12.
- **Q: Final naming pass — `home_location` (admin/service default) vs `user_prefs.{user_id}.location` (per-user) — keep both terms, or unify?** Spec uses both consistently but flags for cleanup at implementation. Spec section: §Open Questions / Future #3.

### #03-notification-fanout
- **Q: Add a "Quick setup: default routes by source" UI template (e.g. all `agent` notifications to phone, all `inbox` to email)?** v1's `source_allow`/`source_deny` covers the capability; this is purely an ergonomics question about the UI. Spec section: §Open questions #2.
- **Q: Add a `notify_role` mechanism for "send all `urgent` to every admin" (broadcast notifications)?** Currently `notify_user` targets one user. This is a separate feature outside per-user fan-out scope but a natural v2 addition. Spec section: §Open questions #3.

### #04-rss-feeds
- **Q: Add per-user `briefing_hour` and `timezone` overrides plus a 15-min briefing-tick scheduler in v1.x?** v1 is single global timezone — multi-zone households / shared deployments get server-zone-7-AM. Tracked here so the data shape doesn't ossify. Spec section: §23 Open / deferred.
- **Q: Add `mute_feed(feed_id, days=7)` and a "feed health" view (which feeds the user has read 0 items from in the last 30 days, with one-click cleanup)?** "Drown in feeds" anti-pattern tooling considered but not included in v1. Spec section: §23 Open / deferred.
- **Q: Should the `briefing_max_seconds` parameter actually constrain AI output length via post-hoc truncation, or stay a model-side hint?** Currently passed as a hint; enforcement is the model's responsibility. Spec section: §23 Open / deferred.

### #05-tasks
- **Q: Add a `task.overdue` periodic event in v1.1 for already-past tasks the user hasn't seen yet?** v1 ships `due_soon` (forward-looking only). Spec section: §21 Genuinely open #3.
- **Q: For the future Todoist webhook integration, add `on_external_change(list_id)` on `TasksService` (callback-via-resolver), or a different shape?** Sketched as a future ABC extension. Direction needs confirmation before v1.2 work begins. Spec section: §21 Genuinely open #2.

### #06-frigate-cameras
- **Q: Default `vision_text_retention_days` to `0` (no separate scrub — vision_text expires with the row) or `7` (matches global default but as a separate field)?** AI-generated prose like "a man in a blue jacket carrying a brown box" is more sensitive than bare detection metadata. Spec section: §18 #4.
- **Q: Confirm `mute_camera_alerts` ships with a UIBlock confirmation by default, or pivot to fire-and-forget with an `undo` slash command?** v1 mirrors calendar-mutation tone with confirm-by-default. For volume-style mutes the confirm is friction. Spec section: §18 #6.
- **Q: Confirm the MQTT broker onboarding hint ("if you don't already have a broker, point this at Frigate's bundled mosquitto") is the intended story, vs. recommending users run a separate broker?** Affects docs in root README + std-plugins README. Spec section: §18 #3.

### #07-media-library
- **Q: Schedule the v2 `get_snapshot` switch (workspace-reference attachments instead of inline base64 in conversation row) now, or defer indefinitely?** v1 inline base64 is bounded (1 MB raw, pre-scaled to 720p) but the conversation row carries the bytes forever. Camera spec has the same shape question. Spec section: §23 #?? (snapshot bytes — actually #06 §18 #5 — both specs share the question).
- **Q: Move per-user `preferred_genres` for `recommend_next` from household-level to per-user via a new `media_library_user_preferences` collection in v2?** v1 is household-level. Defer until usage data justifies it. Spec section: §23 #6.
- **Q: Switch `recently_added` polling to per-user (N polls instead of 1) for households with strict library-isolation needs?** v1 emits events with `library_section` and expects subscribers to re-filter. Defer until a real household reports the leak. Spec section: §23 #11.
- **Q: Use Jellyfin's user-scoped api-keys (10.9+) once they're stable, replacing admin-token + UserId-query-param fan-out?** v1 audit trail logs all per-user queries as the admin user. Spec section: §23 #8.
- **Q: Add a Gilbert-side poster-URL proxy (`GET /media/proxy/<backend>/<id>/poster`) to remove the Plex-token-in-query-string leak entirely?** v1 hands raw upstream URLs to the SPA, with export-time redaction. Spec section: §23 #9.

### #08-health
- **Q: Add Withings webhook subscriptions to replace 6-hour polling once the security model is validated end-to-end?** v1 uses pull-sync. Spec section: §19 #5.
- **Q: Add Garmin / Oura / Fitbit backends in a follow-up?** All three have OAuth APIs but Garmin requires application approval and Oura's rate-limits are tighter. All fit the existing interface without changes. Spec section: §19 #6.
- **Q: Add per-user retention policy (delete-my-pre-2024-weight) on the user profile, replacing global `retention_days`?** v1 covers the global case. Spec section: §19 #3.

### #09-google-oauth
- **Q: What refresh-token longevity posture should the wizard mandate?** A "Testing" OAuth client issues 7-day refresh tokens (breaks an always-on assistant after a week) for non-Workspace accounts; "In production / unverified" issues non-expiring tokens but caps at 100 users and shows an unverified-app interstitial. Spec recommends the wizard mandate "In production" publishing status and explicitly warn about the 7-day trap, baked into the bundled wizard copy. Confirm before wizard copy is finalized. Spec section: §Open Questions / Risks #3 (c).
- **Q: Document (and accept for v1) that Drive write operations are unsupported under `shared_service_account`?** A bare service account has 0 bytes of My-Drive quota. Current Knowledge usage is read-only ingestion (unaffected); `upload_document`/`delete_document` would be SA-limited under shared mode. Spec recommends documenting the read-only-under-SA caveat in `std-plugins/README.md` and leaving write paths for the delegated/oauth modes. Spec section: §Open Questions / Risks #4 (d).
- **Q: Accept that free-account Gmail requires the `oauth_bot` path (no shared-SA option) for v1?** Gmail has no "share inbox with a service account" primitive, so on a free account the only mailbox path is one OAuth-linked ordinary Gmail account. Spec recommends accepting this and stating it plainly in the wizard and README. No engineering path around it (Google API constraint). Spec section: §Open Questions / Risks #5 (e).

## Low-impact (can be deferred to v1.1; spec author flagged "out of scope but worth knowing")

### #01-calendar
- **Q: Add `recurrence_scope: "instance" | "future" | "all"` parameter on `update_event`/`delete_event` in v2?** ABC currently treats series as read-only; real users will eventually want "delete this and all following." Spec section: §Open Questions / Risks #1.
- **Q: Promote working-hours from a single start/end pair to per-day-of-week (Friday-half-day, weekend-on-call) in v2?** Spec section: §Open Questions / Risks #2.
- **Q: Switch from polling to Google Calendar push webhooks?** Lower-latency but requires a public HTTPS endpoint plus a watch-channel renewal job. 5-min poll is "good enough" for v1. Spec section: §Open Questions / Risks #4.
- **Q: Add optimistic concurrency (`version: int` field) on the `CalendarAccount` row?** Last-write-wins is the entity-storage default. Acceptable for v1 (shared-account admin churn is low). Spec section: §Open Questions / Risks #6.
- **Q: Add timezone auto-detection from the calendar's primary timezone via the same probe used for `calendar_id`?** Would require sequencing the probe ahead of `create`, conflicting with the simplified probe-after-save flow. Default of "UTC" plus validation on write is the v1 floor. Spec section: §Revision Log Round 2 — Deferred [eng.nit.3 partial].

### #02-weather
- **Q: When a presence backend grows lat/lon (e.g. a phone-GPS backend), wire up presence-derived weather location?** Designed-in but not wired up. Not blocking. Spec section: §Open Questions / Future #4.
- **Q: Grow weather backends into a multi-backend aggregator (e.g. Open-Meteo for forecast + NWS for alerts simultaneously)?** Cache key already includes `backend_name` so this is non-breaking. Spec section: §Open Questions / Future #6.
- **Q: Add a `weather_history` tool (Open-Meteo Historical Weather API)?** Not in scope for v1. Spec section: §Open Questions / Future #7.
- **Q: Migrate the in-memory weather cache to `entity_storage` once the first rate-limited backend lands?** Plugin hot-installs trigger restarts that wipe the cache and would re-hit a throttled API. Spec section: §Open Questions / Future #8.
- **Q: Publish per-user weather digests for each user with a configured location, in addition to the service-default location digest?** Spec section: §Open Questions / Future #9.
- **Q: Re-introduce a `digest_timezone` ConfigParam if a tz-aware DAILY scheduler primitive lands?** Currently documented as "scheduler's underlying behavior — naive-local, no catch-up." Spec section: §Open Questions / Future #13.

### #03-notification-fanout
- **Q: Confirm the v1.1 outbox row shape (audit log + at-least-once dedup token) — already promoted from v2 to v1.1 mandatory?** Already a known follow-up; included so the human knows it's locked in. Spec section: §Open questions — Resolved in this revision.

### #04-rss-feeds
- **Q: Add bulk re-ingest of cached feed articles to knowledge after retuning the chunker?** v1 only exposes single-item re-ingest via `feeds.items.reingest`. Spec section: §23 Open / deferred.
- **Q: If tags are reintroduced for scoring output, constrain them to a fixed/curated vocabulary fed back into the prompt?** Free-form AI tags lead to tag-explosion; v1 dropped tags entirely. Spec section: §23 Open / deferred.
- **Q: Schedule any of the `FeedBackend` plugins (Reddit, HackerNews, YouTube, podcasts)?** ABC was designed to slot them in without core changes. Out of scope for this PR. Spec section: §23 Open / deferred.

### #05-tasks
- (no low-impact items — all task open questions are medium or high.)

### #06-frigate-cameras
- (resolved items in §18 "Closed (decided)" excluded.)

### #07-media-library
- **Q: Add adaptive backoff to `poll_recently_added` (currently only `poll_now_playing` adapts)?** Recently-added churn is more uniform, value unclear. Revisit if polling cost becomes painful. Spec section: §23 #12.
- **Q: Add proactive Plex per-user-token cache invalidation, or keep the v1 lazy-on-401 path?** No proactive expiry in v1. Spec section: §23 #1.
- **Q: Add webhook/SSE-based now-playing for v2 (Plex Pass `/library/sections/onWebhook` + Jellyfin `/socket` SSE) so pause/resume that polling can't reliably detect emits real events?** v1 ships polling. Spec section: §23 #5.
- **Q: Tune the `recommend_next` candidate-set construction (continue_watching + recently_added + unwatched preferred-genres, capped at 30) empirically?** Implementer discretion. Spec section: §23 #3.
- **Q: Allow casting *to a music speaker* via `play_on` for `music_track` (overlaps `MusicService`)?** Out of scope for v1; `search_media` default-excludes `MUSIC_*` and tool descriptions draw the seam. Spec section: §23 #4.
- **Q: Extend `MediaLibraryProvider` Protocol with mutations if notifications/agents need write access?** v1 exposes read-only methods. Spec leans toward "consumers take a hard dependency on the concrete service rather than extend the Protocol." Spec section: §23 #10.
- **Q: Confirm Jellyfin clients with `SupportsRemoteControl=false` are included with a flag (current v1 behavior) vs. excluded?** Flagged on `MediaClient` so the AI doesn't try `play_on` against them. Spec section: §23 #2.

### #08-health
- **Q: Add a bulk-insert path for Apple Health Shortcut deliveries that exceed `webhook_max_metrics_per_delivery` (default 1000)?** Currently returns 400. Future work if real-world usage exceeds the cap. Spec section: §19 #2.
- **Q: Anchor daily-summary boundary on device-local time (from each metric's `recorded_at` offset) instead of profile TZ for users who move timezones during a day?** v1 anchors on profile TZ; DST is correct but mid-day TZ moves aren't. Spec section: §19 #4.
- **Q: Add body-HMAC (`X-Hk-Signature` header over the body) to Apple Health webhook deliveries?** v1's webhook security is "the URL is the secret." A second per-user secret would defeat captured-token replay over plain HTTP. Threading through the iOS Shortcut is fragile. HTTPS-everywhere is the v1 mitigation. Spec section: §19 #9.
- **Q: Schedule "greeting model with automation tools" + "proposals current-schedule context" follow-up features so the §1 motivating examples ("I dimmed the meeting reminders", "21:30 instead of 22:30") actually work?** Both are separate features against greeting / proposals services. Spec section: §19 #11–12.
- **Q: Add a mobile push path for the daily summary?** Out of scope until Gilbert has a mobile notification path generally. Spec section: §19 #8.

### #09-google-oauth
- **Q: Add per-user OAuth (each Gilbert user links their own Google account) as a v1.1 opt-in?** v1 is operator-scoped (one shared SA or one OAuth-bot account per backend). Per-user OAuth does not change the resolver contract — it adds a second resolution scope keyed by `owner_user_id`. Deferred until a multi-user household requests it. Spec section: §Scope / Out of scope.
- **Q: Include the one thin web-redirect callback route in v1, or defer it (keeping v1 100% inside the std-plugins submodule)?** Loopback (default) + manual-paste need zero `src/gilbert/` change; the web-redirect convenience needs one thin route mirroring `auth.py`. Spec recommends including it (user asked for "support both"); deferring to v1.1 keeps v1 submodule-contained. Spec section: §Open Questions / Risks #6 (f).
- **Q: Wizard backend as per-backend `ConfigAction`s (v1) vs. a plugin `Service`; and add an admin `/google status` slash command later?** v1 uses the existing ConfigAction mechanism (no new Service/ACL) and a panel Status tab (no slash command — no per-tool hide-from-AI flag exists). A plugin `Service` (with `slash_namespace`, not advertising `ai_tools`) + `/google status` is the documented fallback. Spec section: §Open Questions / Risks #7 (g).
- **Q: Ship headless manual-paste linking in the wizard for hosts with neither a reachable loopback browser nor a tunnel?** v1 includes manual-paste as the third linking option (the shape Sonos/Spotify already uses). Listed so the human knows it's intentionally in v1, not deferred. Spec section: §Open Questions / Risks #8.
