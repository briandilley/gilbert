# Scheduler

Recurring and one-shot timed work. Services register jobs; the
scheduler decides when they fire and dispatches them.

Two layers:

- **`interfaces/cron.py`** — parses a cron expression and answers
  "when does this next fire, in this timezone?". Pure stdlib, no
  Gilbert imports, no I/O.
- **`core/services/scheduler.py`** — owns the job registry, the sleep
  loop, dispatch, persistence, RBAC, and the AI-tool / WebSocket
  surface.

The split matters: all the fiddly calendar reasoning lives in a
dependency-free module that can be tested exhaustively without an event
loop, storage, or a service resolver.

## Everything is a cron expression

There is one schedule kind. `Schedule` carries an `expression` string;
`ScheduleType`, `interval_seconds`, `hour` and `minute` no longer
exist. See [ADR-0022](../adr/0022-one-cron-engine-for-all-schedules.md)
for why, and for the full dialect table.

The legacy constructors are sugar over the dialect:

```python
Schedule.every(90)        # -> "@every 90s"
Schedule.daily_at(3, 30)  # -> "30 3 * * *"
Schedule.hourly_at(15)    # -> "15 * * * *"
Schedule.once_after(45)   # -> "@once+45s"
Schedule.cron("0 9 * * MON-FRI", timezone="America/Los_Angeles")
```

Callers that just want "every 5 minutes" should keep using
`Schedule.every(300)` — it reads better at the call site and compiles
to the same thing.

The expression is parsed in `__post_init__`, so a typo raises
`CronParseError` where the schedule is written rather than at 3am on
the first fire.

## Next-fire calculation

`SchedulerService._next_fire_at()` is the single path:

```
expression.next_after(anchor, tz)
  -> floor at start_at
  -> gate through the daily window
  -> retire (None) if past end_at
  -> add jitter
```

Returning `None` means the job is finished; the loop marks it `DONE`.

A few non-obvious points:

**Windows are filters, not generators.** `window_start_time` /
`window_end_time` reject candidate fires outside the window and advance
to the next window opening. Implementing them as a filter is what lets
them compose with `@every 90s` — a cron field expression cannot state a
sub-minute rate, so "every 90 seconds between 1am and 2am" is only
expressible as expression-plus-window.

**Interval jobs anchor on the previous fire**, not on loop-iteration
time, so drift does not accumulate across long sleeps. After a suspend
the anchor may be far in the past; `next_after` advances in whole
intervals so the *phase* of the schedule survives rather than being
restarted from the moment of waking.

**Comparisons go through UTC.** Subtracting or comparing two aware
datetimes that share a `tzinfo` *object* makes Python skip
`utcoffset()` and compare wall clock. Across a DST transition that
answers the wrong question, so every ordering test in the engine
converts to UTC first. This is easy to "simplify" back into a bug.

## The loop

`_run_job_loop` computes an absolute next-fire **instant**, then sleeps
toward it in slices capped at `_SLEEP_SLICE_SECONDS` (60s). Each slice
re-reads the wall clock.

That chunking is the whole suspend story: a single
`asyncio.sleep(86400)` cannot notice that the laptop was closed for two
hours, so the job fires two hours late. Re-reading each slice bounds
the error to one slice.

Fires are dispatched as **separate tasks** (`_dispatch_fire`), not
awaited inline, so a slow callback never stalls scheduling — which is
also what makes overlap a real question with a real policy. Tasks carry
`contextvars.copy_context()` so request-scoped context does not leak
between concurrent fires.

One-shot jobs are the exception: they are awaited inline. There is no
subsequent fire to protect, and awaiting avoids a race where the loop
marks the job `DONE` while the fire task is still running and then has
its terminal state overwritten by the fire's `IDLE`.

## Policies

**`OverlapPolicy`** — what happens when a fire is due and the previous
one is still in flight. `SKIP` (default) drops it, `QUEUE` serialises
through a per-job lock, `CONCURRENT` runs anyway.

**`CatchUpPolicy`** — what happens to fires missed while the process
was down. `SKIP` (real cron behaviour) loses them, `ONCE` fires a
single make-up run regardless of how many were missed, `BACKFILL`
replays each one, capped at `_MAX_BACKFILL_FIRES`.

Defaults differ per constructor on purpose: `daily_at` / `hourly_at` /
`cron` default to `ONCE`, because a missed daily digest is worth one
catch-up run. `every` and `once_after` default to `SKIP`, because a
10-second poll tick has nothing meaningful to make up.

Catch-up needs to know when the job last fired, which means fire
history has to be persisted. It lives in its own collection,
`scheduler_job_state`, rather than on the `scheduler_jobs` row —
**system jobs are never persisted in `scheduler_jobs`**, so without a
separate store they could never catch up at all.

## Persistence

User jobs (created via `set_timer` / `set_alarm`) persist to
`scheduler_jobs`. System jobs do not — their owning services
re-register them from `start()` on every boot, so persisting them would
just create duplicates.

Rows written before the cron migration have no `expression`. The loader
**skips** them with a warning rather than inventing one: guessing
`@every 60s` for an unconvertible row would turn a one-shot into a job
that fires forever. Migration `0007_scheduler_cron_schedules` is the
supported conversion path.

## Testing

`tests/unit/test_cron.py` covers the engine directly — parsing, the
DOM/DOW OR rule, `L`/`W`/`#`, impossible expressions, and named
`America/Los_Angeles` DST transitions. Because the module has no
dependencies, these run as fast as plain function calls.

`tests/unit/test_scheduler_service.py` covers the service: bounds,
windows, catch-up, overlap, chunked sleep, and the tool surface.

`tests/integration/test_migration_scheduler_cron.py` runs migration
0007 against real SQLite, including a re-run assertion for idempotency.
