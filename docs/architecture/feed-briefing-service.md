# Feed Briefing Service

## Summary
Thin scheduler + event publisher for the daily news briefing fan-out.
Owns NO AI calls and NO prompts — calls
`FeedsProvider.build_briefing` (defined in `interfaces/feeds.py`) for
the actual brief construction. The fan-out strategy is **presence-
first, daily-fire-as-fallback**: a 7 AM presence-driven greeting
takes priority; the daily fallback fires
`presence_grace_minutes` later for users who didn't get the
greeting.

## Details

### Why a separate service from `FeedsService`

`FeedsService` owns the briefing TEXT BUILDER (the AI call + prompt
config + persistence). `FeedBriefingService` owns the SCHEDULE +
FAN-OUT POLICY (who gets briefed, when, what event fires). Splitting
keeps `FeedsService` focused on the per-feed lifecycle and lets
operators toggle the briefing schedule independently of feeds.

`FeedBriefingService` consumes only `FeedsProvider` — it does NOT
import `FeedsService` directly and does NOT own a parallel
`BriefingProvider` protocol (intentionally absent per Round 2).

### Daily-fire vs. presence — race resolution

The original spec was silent on this; the product review correctly
flagged it as a "daily fire eats the items the killer feature needs"
bug.

**Resolved policy: presence-first, daily-fire is the fallback.**

`start()` registers a `Schedule.daily_at(briefing_hour + grace,
minute)` system job named `feed-briefing-fallback`, where `grace` is
`presence_grace_minutes` (default 90 min, so default fire at 8:30
AM if `briefing_hour=7`). The fallback iterates every user with
at least one accessible briefing-eligible feed and:

1. Reads `feed_briefing_state.last_briefed_on` (date-only).
2. If today's date is already in `last_briefed_on`, **skip** —
   the greeting flow already fired.
3. Otherwise, call `FeedsProvider.build_briefing(user_ctx,
   mark_briefed=True)`, publish `feed.briefing.ready`.

`GreetingService` calls `FeedsProvider.build_briefing(user_ctx,
top_n=3, mark_briefed=True)` when presence fires inside the greeting
window AND `include_briefing=True` AND today's `last_briefed_on !=
today`. Whichever path runs first sets `last_briefed_on`; the other
becomes a no-op for that user that day.

### Per-user opt-in for role-shared briefings

If a user only gains feed access via *role share* (a "team" role with
a shared feed), default that user's `briefing_opt_in` to `False` so
a 10-person team sharing a feed doesn't trigger 10 unwanted morning
briefings. Users who own at least one feed default to `True`. The
flag lives on `feed_briefing_state.briefing_opt_in`.

### Event payload — privacy

`feed.briefing.ready` event data:
`{user_id, briefing_id, item_count, since}`. **Deliberately does
NOT contain `spoken_text`** — briefings can include sensitive items
(SOX-regulated user's financial filing, internal company feed).
Consumers RPC-fetch the spoken text via `feeds.briefing.get` if they
need it. Narrows the privacy posture so briefing content stays out
of WS event logs and server logs.

The WS fanout layer restricts `feed.briefing.ready` delivery to the
recipient `user_id` only (analogous to how notification events
work).

### System / shop briefing

When `system_briefing_enabled=True` AND `system_briefing_user_id` is
set, the fallback ALSO calls `speaker_svc.announce(spoken,
speaker_names=announce_speakers or None)` once for that user — the
shop's blanket morning briefing on the shop speakers. Distinct from
per-user fan-out. Renamed from the original spec's `auto_announce`
because that name conflated "did we wake up the speakers" with "did
we generate a per-user briefing."

### Configuration (`feed_briefing` namespace)

| Key | Default | Notes |
|---|---|---|
| `enabled` | `False` | Off by default — opt-in feature. |
| `briefing_hour` | `7` | Hour-of-day for the daily fire. **Single-tenant time zone in v1**. |
| `briefing_minute` | `0` | Minute of `briefing_hour`. |
| `timezone` | `"UTC"` | Server-side IANA timezone. |
| `briefing_top_n` | `5` | Items per briefing. |
| `briefing_since_hours` | `24` | Look-back window. |
| `presence_grace_minutes` | `90` | Fallback fires `briefing_hour + this`. |
| `system_briefing_enabled` | `False` | Single shared shop briefing. |
| `system_briefing_user_id` | `""` | User_id whose feeds drive the shop briefing. |
| `announce_speakers` | `[]` | Speakers for the shop briefing. |

No prompts of its own — all three feed prompts live on
`FeedsService`.

### Time zone — single global v1

v1 is single-time-zone. Households / shared deployments where
everyone is in one zone are fine; multi-zone deployments get the
briefing at server-zone-7-AM regardless. v1.x adds per-user
`briefing_hour` / `timezone` overrides plus a 15-min briefing-tick
that decides which users are due.

### User enumeration

Walks the `feeds` collection (with SYSTEM context), unions
`{owner_user_id} ∪ shared_with_users` plus role-resolved members
(via the optional `users` capability — best-effort; missing user
service yields false negatives over false positives). Cache **in
memory only** for the duration of the daily run; rebuild next daily
fire so the projection can't drift.

## Related
- [Feeds Service](feeds-service.md) — owns `build_briefing` + prompts
- [Greeting Service](greeting-service.md) — calls `build_briefing` from presence path
- [Capability Protocols](capability-protocols.md) — `FeedsProvider`
- `src/gilbert/core/services/feed_briefing.py`
- `tests/unit/test_feed_briefing_service.py`
