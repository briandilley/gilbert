# Health Service

## Summary
Multi-backend personal health-metrics service with PHI-adjacent privacy
posture: owner-only reads enforced in the service, cross-user reads
gated behind a dedicated `health-admin` role and audit-logged + target-
user-notified, two-step right-to-delete wizard, hash-at-rest webhook
tokens, generic OAuth callback, code-driven (not AI-driven) flag
vocabulary, and structured-snapshot greeting integration. Three v1
backends: `apple-health` (push via iOS Shortcut), `withings` (OAuth
pull), `hk-webhook` (generic catch-all push).

## Details

### Architecture

- `interfaces/health.py` — `HealthBackend` ABC + registry, `HealthProvider`
  capability protocol, `HealthLinkProvider` capability protocol (for
  the web nav's "should /health render?" gate, no raw storage poking),
  `StorageAwareHealthBackend` protocol (for OAuth backends that need
  raw storage access), `HealthMetric` / `HealthAggregate` /
  `DailySummary` / `GreetingBrief` / `LinkStartResult` /
  `LinkCompleteResult` dataclasses, `MetricType` / `MetricUnit` /
  `AggregatePeriod` / `AggregatorKind` enums, error taxonomy
  (`HealthBackendAuthError`, `HealthBackendRateLimitError`,
  `HealthBackendTransientError`, `HealthBackendNotFoundError`), pure
  `can_read_metrics` / `can_mutate_metrics` auth helpers,
  `parse_metric_payload` shared parser with `extra`-whitelist caps,
  `METRIC_TYPE_HUMAN_NAMES` mapping + `metric_types_human_summary`
  helper for cross-user-read notifications, `HEALTH_ADMIN_ROLE`
  constant. Imports nothing outside `interfaces/` + stdlib.
- `core/services/health.py` — `HealthService` (singleton). Discovers
  backends via `HealthBackend.registered_backends()` + side-effect
  imports inside std-plugins.
- `web/routes/health.py` — `/webhook/health/{token}` (path-isolated
  from `/api`), `/api/health/me/*`, `/api/health/admin/*`, the generic
  OAuth callback `/api/health/me/oauth/{backend}/callback`.
- `std-plugins/apple-health/` — push backend with HealthKit identifier
  mapping, prebuilt iOS Shortcut path.
- `std-plugins/withings/` — OAuth pull backend (sleep, weight, BP, HR).
- `std-plugins/hk-webhook/` — generic catch-all webhook backend.

### Privacy posture

- **Owner-only by default.** Even built-in `admin` cannot read another
  user's metrics — `health-admin` is a SEPARATE seeded role at level
  0, never auto-granted. Operators grant it manually via
  `/roles/users`.
- **Cross-user reads** (admin holding `health-admin`) persist a
  `health_audit` row, fire `health.access.audit` on the bus, AND call
  `NotificationProvider.notify_user(user_id=target, source="health",
  urgency="normal")`. NotificationProvider absent: WARN log, audit
  row + bus event remain the durable record.
- **Mutations are always owner-only** — even `health-admin` cannot
  inject or delete on behalf of another user. SYSTEM bypasses for
  scheduler / cascade work.
- **AI tools NEVER accept `user_id` from the model** — every tool reads
  `_user_id` from the injected arguments, missing `_user_id` is a
  `PermissionError` (no silent fallback to `get_current_user()`).
- **Webhook tokens are hash-at-rest** (`SHA-256`); the raw token is
  shown to the user once on rotation and never persisted. The route
  uses `hmac.compare_digest` for confirmation.
- **OAuth tokens are PLAINTEXT in v1** (Withings refresh + access
  tokens). Documented gap; v2 adds Fernet sealed to the OS keychain.
  Service emits a startup WARN if any OAuth backend is registered AND
  bind != 127.0.0.1 AND no TLS-fronting tunnel.
- **`acl_collections` rows seeded** at start for `health_metrics`,
  `health_links`, `health_daily_summaries`, `health_audit`,
  `health_oauth_state` so the entities page never silently exposes
  private data.
- **Log-redaction filter in `core/logging.py`** masks values for the
  keys `code`, `state`, `Authorization`, `webhook_url`, `oauth_*`,
  plus generic `token`/`secret`/`password`. Bearer tokens in
  Authorization headers are masked before generic kv patterns run, so
  `Authorization: Bearer <secret>` collapses cleanly to
  `Authorization: Bearer [redacted]`. Installed once on the root
  logger (singleton `RedactingFilter`) so every handler — console,
  file, AI log — emits redacted records.

### Backends

- `apple-health` (push) — translates HealthKit identifiers to
  `MetricType` via a fixed map; `extra` whitelist allows `device` +
  `source_app` only.
- `withings` (pull, OAuth) — satisfies `StorageAwareHealthBackend` so
  the service injects the raw storage + `gilbert.public_base_url`
  before `initialize`. Token refresh on 401, retries the request once.
  Disconnect revokes upstream BEFORE local cleanup; failure logs
  WARN but does not block local cleanup.
- `hk-webhook` (push) — generic. NO `extra` whitelist (every key in
  the payload's `extra` dict is silently stripped).

### Schema

- `health_metrics(_id, user_id, backend, metric_type, value, unit,
  recorded_at, ingested_at, source_event_id, extra)` — indexes:
  `(user_id, metric_type, recorded_at)`,
  `(user_id, recorded_at)`,
  `(user_id, backend, source_event_id)`,
  `(user_id, backend, metric_type, recorded_at)` (dedup fallback).
- `health_links(_id=user_id/backend, user_id, backend_name,
  webhook_token_hash UNIQUE, webhook_token_last4, oauth_*,
  last_sync_at, last_sync_error, last_delivery_at, enabled, ...)`.
- `health_daily_summaries(_id=user_id/YYYY-MM-DD, user_id, local_date,
  summary_text, metrics_snapshot, flags, generated_at)`.
- `health_audit(_id, kind, actor_user_id, target_user_id, accessed_at,
  metric_types, backends, period_start, period_end, request_id)` —
  `read=admin/write=admin` ACL so attackers gaining user-level access
  can't tamper with the audit trail through the entities page.
  `metric_types` is `list[MetricType]` and is empty for
  `self_delete_all` rows; `backends` carries the `list[str]` of
  backend names involved in the delete (only populated for
  `self_delete_all`).
- `health_oauth_state(_id=state, user_id, backend_name, created_at,
  expires_at, consumed_at)` — server-side state binding, 10-minute
  TTL, one-shot consume. `consume_oauth_state` is serialized per-state
  via an in-memory `asyncio.Lock` so two concurrent callbacks for the
  same state cannot both observe `consumed_at == None` and both
  succeed (the lock holds across the read-then-write critical
  section, including the TTL check).

### Events

- `health.metric.received` — fired ONCE per newly-persisted insert.
  Duplicates skip the publish to defeat replay-flood amplification.
- `health.metric.deleted` — `scope="user-deleted" | "retention" |
  "backend-disconnect"`.
- `health.daily.summary` — daily-summary tick output.
- `health.link.connected` / `health.link.disconnected`.
- `health.access.audit` — cross-user read or self-delete audit row.

`health.` prefix at level 100 in `interfaces/acl.py`. The per-event
`can_see_health_event` filter in `web/ws_protocol.py` narrows
delivery: metrics + summaries + links → owner only; audit events →
actor + target + admin.

### WS RPCs

- `health.links.list` — list caller's connected backends.
- `health.summary.latest` — latest persisted DailySummary.
- `health.metrics.read` — read metrics in a window.
- `health.delete_all.preview` — preview counts for the two-step
  delete dialog.

### Multi-user / concurrency

- Singleton service. Per-`(user_id, backend)` `asyncio.Lock` keyed
  dict serializes the dedup-then-write path so concurrent webhook
  deliveries for the same user-backend can't trample each other.
  Different users / different backends fan out.
- Withings `_call` retries on 401 are also serialized per-user via
  `WithingsBackend._refresh_locks[user_id]`. After acquiring the
  lock, the backend re-reads the link row to absorb the case where
  another caller already refreshed (avoids "second refresh fails
  because the first invalidated the prior refresh token" cascading
  to consecutive-auth-failure-driven auto-disable).
- Per-user write cap (default 100k/day) enforced in `ingest_metrics`
  regardless of source — defends against a buggy Shortcut posting
  every minute.
- Per-token + per-IP webhook buckets, LRU-capped at 10k entries.
- Scheduler loops use `_run_per_user(user_ids, work, *, concurrency,
  label, tz_by_user=...)` — bounded `asyncio.Semaphore` + per-task
  `contextvars.copy_context()` so the per-task `set_current_user`
  doesn't leak. Actor stays `UserContext.SYSTEM` with
  `metadata["target_user_id"]` carrying the target so audit logs
  distinguish "system did X for user Y" from "user Y did X."
  `_system_acting_for(user_id, *, tz=None)` populates `tz` on the
  SYSTEM context so `_compute_and_persist_summary` reads it via
  `get_current_user().tz` instead of re-querying `users_svc`.

### Configurable prompts

- `summary_prompt` (`ConfigParam(multiline=True, ai_prompt=true)`).
- `trend_prompt` (`ConfigParam(multiline=True, ai_prompt=true)`).
- `__init__` sets `self._summary_prompt = _DEFAULT_SUMMARY_PROMPT` /
  `self._trend_prompt = _DEFAULT_TREND_PROMPT`.
- `on_config_changed` recomputes via `str(config.get(key, "") or "") or
  _DEFAULT_*` so empty overrides re-resolve to bundled defaults.
- Call sites read `self._summary_prompt` / `self._trend_prompt` —
  never the constants. Tests assert this against a sentinel override.
- The bundled prompts forbid the words `concerning`, `abnormal`,
  `warning`, `risk`, `noteworthy`, `should` — non-clinical guarantee
  with a deterministic prompt-text regression test.

### Right-to-delete

Two-step wizard:
1. `GET /api/health/me/delete-all/preview` returns counts
   (metric_count, summaries_count, audit_count, backends).
2. `POST /api/health/me/delete-all` requires `confirm: "DELETE"`
   (case-sensitive literal). Cascade:
   1. For each linked backend: call `backend.disconnect(user_id)`
      which revokes upstream BEFORE local link row deletion.
      Failures log WARN; local cleanup proceeds anyway.
   2. `delete_query` every `health_metrics`, `health_daily_summaries`,
      `health_links` row for the user.
   3. Persist a `health_audit` row with `kind="self_delete_all"`,
      `actor=target=user_id`. SURVIVES the cascade.
   4. Publish `health.metric.deleted` with `scope="user-deleted"`.

The AI tool `health_delete_my_data` returns the two-step UIBlock via
`confirm_or_execute` — the model can never one-shot the delete; it
must render the block and wait for the user's literal `"DELETE"`.

### `auth.user.deleted` cascade

UserService publishes `auth.user.deleted` (`{user_id, deleted_at}`)
after `delete_user` completes. HealthService subscribes via the bus
and runs the same delete cascade as a self-delete but with
`actor_kind="cascade"` and NO audit row (the user no longer exists).

### Open Questions

- v2: OAuth-token encryption at rest (Fernet sealed to OS keychain).
- v2: Step-up auth for cross-user reads (sudo-style fresh auth).
- v2: Greeting model with automation tools (the §1 marketing line
  "I dimmed the meeting reminders" needs the greeting model to
  invoke automation tools).
- v2: Withings outgoing webhooks (avoid the 6h pull cadence).
- v2: Garmin / Oura / Fitbit additional pull backends (interface
  ready, no core changes needed).
- v2: Body-HMAC on Apple Health webhook deliveries (HTTPS-only is
  the v1 mitigation).

## Related
- `src/gilbert/interfaces/health.py` — backend ABC, dataclasses,
  protocols, helpers.
- `src/gilbert/core/services/health.py` — HealthService.
- `src/gilbert/web/routes/health.py` — webhook + per-user + admin +
  generic OAuth callback routes.
- `std-plugins/apple-health/` — Apple Health (HealthKit) push backend.
- `std-plugins/withings/` — Withings OAuth pull backend.
- `std-plugins/hk-webhook/` — generic catch-all push backend.
- [Multi-User Isolation](memory-multi-user-isolation.md) — singleton
  + concurrent users + per-user keyed state.
- [Backend Pattern](memory-backend-pattern.md) — the universal
  registry pattern.
- [Capability Protocols](memory-capability-protocols.md) — how
  greeting + proposals consume `HealthProvider`.
- [User & Authentication System](memory-user-auth-system.md) —
  `auth.user.deleted` event publisher.
- [AI Prompts Are Always Configurable](memory-ai-prompts-configurable.md)
  — the `summary_prompt` / `trend_prompt` configurability rule.
- [UI Blocks](memory-ui-blocks.md) — `confirm_or_execute` two-step
  delete wizard.

