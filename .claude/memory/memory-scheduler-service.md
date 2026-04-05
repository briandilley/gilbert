# Scheduler Service

## Summary
Manages recurring and one-shot timed tasks. Supports system jobs (registered by services like doorbell polling) and user jobs (timers/alarms set via AI chat).

## Details

### Interface
- `src/gilbert/interfaces/scheduler.py` — `Schedule` (every/daily/hourly/once factories), `JobInfo`, `JobState`, `JobCallback`

### Service
- `src/gilbert/core/services/scheduler.py` — `SchedulerService`
- Always registered (core service, not optional)
- Capabilities: `scheduler`, `ai_tools`
- System jobs: registered by other services, cannot be removed by users
- User jobs: created/cancelled via AI tools, publish events when fired

### Job Lifecycle
- States: pending → running → idle (recurring) or done/failed (one-shot)
- Each job runs in its own asyncio task via `_run_job_loop()`
- `_next_delay()` calculates sleep time based on schedule type
- Jobs can be enabled/disabled, run immediately via `run_now()`

### AI Tools
- `list_timers` — list all jobs (system + user)
- `set_timer` — one-shot timer, fires `timer.fired` event
- `set_alarm` — recurring alarm (interval/daily/hourly), fires `alarm.fired` event
- `cancel_timer` — remove a user timer/alarm

## Related
- `src/gilbert/core/services/doorbell.py` — registers system timer for ring polling
- `tests/unit/test_scheduler_service.py` — 18 tests
