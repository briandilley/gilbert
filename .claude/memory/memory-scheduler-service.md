# Scheduler Service

## Summary
Manages recurring and one-shot timed tasks. Supports system jobs (registered by services) and user jobs (timers/alarms set via AI chat). All periodic work in Gilbert must go through this service. Recurring alarms can be bounded by absolute start/end times and/or a daily time-of-day window.

## Details

### Interface
- `src/gilbert/interfaces/scheduler.py` — `Schedule`, `JobInfo`, `JobState`, `JobCallback`, `ScheduledAction`, `ActionStep`
- `Schedule` carries the base type (INTERVAL/DAILY/HOURLY/ONCE) plus four optional bounds:
  - `start_at: datetime | None` — first fire cannot happen before this (naive-local)
  - `end_at: datetime | None` — job retires to DONE once the next fire would land past this
  - `window_start_time: time | None` and `window_end_time: time | None` — daily time-of-day window (INTERVAL only; paired; same-day; overnight windows not supported)

### Service
- `src/gilbert/core/services/scheduler.py` — `SchedulerService`
- Always registered (core service, not optional)
- Capabilities: `scheduler`, `ai_tools`, `ws_handlers`
- System jobs: registered by other services, cannot be removed — only paused/resumed by admins
- User jobs: created/cancelled via AI tools, publish events when fired
- Timer ownership: user jobs track owner, non-admins can only cancel their own

### Job Lifecycle
- States: pending → running → idle (recurring) or done/failed (one-shot / past end_at)
- Each job runs in its own asyncio task via `_run_job_loop()`
- `_next_delay(schedule, last_fire_at)` returns seconds until the next fire, or `None` if the job is retired (ONCE already fired, or next fire is past `end_at`). The loop transitions to `DONE` and exits when it returns `None`.
- For INTERVAL with a window, `_clamp_to_daily_window` pushes the computed natural-next-fire to the next valid in-window point (today if unreached, else tomorrow's start).
- Jobs can be enabled/disabled, run immediately via `run_now()`

### Loading persisted jobs on startup
- ONCE timers whose `fire_at` is in the past are dropped.
- Recurring jobs whose `end_at` is in the past are also dropped (mirror of the ONCE cleanup).
- Both cleanups remove the storage record so a restart doesn't re-register-then-immediately-retire the same dead job.

### AI Tools
- `list_timers` (everyone) — lists all jobs with schedule type, bounds, window times, state, action
- `set_timer` (user) — one-shot timer, fires `timer.fired` event, tracks owner
- `set_alarm` (user) — recurring alarm. Accepts `type` (interval/daily/hourly), `interval_seconds`, `hour`, `minute`, plus `start_at`, `end_at`, `window_start_time`, `window_end_time` for bounded or windowed runs. Validates: end after start, window pair set together, window end after start (no overnight), window only on interval alarms.
- `cancel_timer` (user) — remove own timer; admins can cancel any; system timers cannot be cancelled
- `pause_timer` (admin) — disable a timer/alarm (system or user)
- `resume_timer` (admin) — re-enable a paused timer/alarm

### Services Using the Scheduler
- `PresenceService` → `presence-poll` (every 30s)
- `DoorbellService` → `doorbell-poll` (every 5s)
- `KnowledgeService` → `knowledge-sync` (every 300s)

### Design notes / gotchas
- Time handling is naive-local throughout. `_parse_optional_iso_datetime` converts tz-aware ISO input to local-naive; the WS serializer and persistence write ISO strings without timezone markers.
- The window applies DAILY and the start/end are ABSOLUTE — they're orthogonal. Combining them expresses "every minute between 1am-2am daily until next Sunday" with one alarm.
- `start_at`/`end_at`/window apply to INTERVAL/DAILY/HOURLY, NOT ONCE. For ONCE, the `seconds` parameter is the entire schedule and bounds are ignored at `_next_delay` level.

## Related
- `tests/unit/test_scheduler_service.py` — 101 tests incl. bounds/window semantics, tool validation, persistence round-trip, expired-bounds cleanup
- `src/gilbert/interfaces/scheduler.py` — `Schedule` dataclass with the four new fields and factory `start_at`/`end_at`/window kwargs
