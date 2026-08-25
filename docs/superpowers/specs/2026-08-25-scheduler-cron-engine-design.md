# Scheduler: one cron engine for every schedule

**Date:** 2026-08-25
**Status:** Approved for implementation

## Problem

`gilbert.interfaces.scheduler.Schedule` carries four independent schedule
kinds — `INTERVAL`, `DAILY`, `HOURLY`, `ONCE` — each with its own branch in
`SchedulerService._next_delay()`. The arrangement has four defects:

1. **No cron.** Anything cron expresses beyond "daily at H:M" or "hourly at
   :M" is inexpressible. "First Monday of the month", "weekdays at 9",
   "every 15 minutes between 9 and 5" all require a bespoke job.
2. **Naive-local arithmetic.** All time math runs on `datetime.now()` with no
   timezone. A `daily_at(2, 30)` job either fires twice or not at all on DST
   boundaries.
3. **Long sleeps.** A daily job issues a single `asyncio.sleep(86400)`. Host
   suspend or a clock adjustment makes it fire late by exactly the lost
   interval, with no correction.
4. **No missed-fire handling, no overlap policy, no visibility.** A fire
   missed during downtime is lost silently; a slow callback can overlap its
   successor; and `JobInfo` exposes no next-fire time, so neither the SPA nor
   `list_timers` can say when a job actually runs next.

## Decision

Collapse all four kinds into a **single cron engine**. `Schedule` carries one
`expression` string; every schedule in the system — including sub-minute
polling and one-shot startup jobs — is a cron expression in a dialect wide
enough to express them.

The four existing constructors survive as thin compilers to that dialect, so
none of the 44 existing call sites change.

### Supported dialect

| Feature | Syntax |
|---|---|
| 5-field POSIX | `30 3 * * *` |
| 6-field with seconds | `*/30 * * * * *` |
| Ranges, lists, steps | `1-5`, `1,3,5`, `*/15`, `0-30/5` |
| Names | `JAN`–`DEC`, `SUN`–`SAT` |
| Quartz day extensions | `L`, `W`, `#` (`FRI#3`, `L-3`, `15W`), `?` |
| Standard macros | `@yearly` `@annually` `@monthly` `@weekly` `@daily` `@midnight` `@hourly` |
| Interval macro | `@every 90s`, `@every 1h30m` |
| Startup / one-shot | `@reboot`, `@once`, `@once+45s` |

The 6-field seconds form is standard in Quartz, Spring Scheduler and Go's
`robfig/cron`; `@every` is standard in `robfig/cron`; `@reboot` is standard
Vixie cron. Only the `@once+<delay>` form is a Gilbert extension, and it is
built on the established `@reboot` macro (`@reboot` == `@once+0s`).

**Day-of-month / day-of-week OR rule.** When *both* the DOM and DOW fields are
restricted (neither is `*` or `?`), a day matches if *either* field matches —
classic Vixie semantics. When only one is restricted, only that one applies.
Getting this wrong is the single most common source of silent incompatibility
between cron engines, so it is specified here and tested directly.

### DST semantics

Written down because engines disagree and the disagreement is invisible until
it costs someone a missed fire:

- **Spring forward**, hour-anchored job at a local time that does not exist
  (`30 2 * * *` on the skip day): fires **once**, at the transition instant.
- **Fall back**, hour-anchored job at an ambiguous local time that occurs
  twice: fires on the **first** occurrence only, never both.
- **Wildcard-hour** expressions (`*/15 * * * *`) follow real elapsed time
  across both transitions — no skips, no repeats.
- **`@every`** is a pure duration, so it is timezone-independent and
  DST-immune by construction.

### Why not a third-party parser

`croniter` has a large surface and a history of DST bugs; `cronsim` is clean
but deliberately omits the Quartz `L`/`W`/`#` extensions. Both would still
need the next-fire result post-processed against Gilbert's `start_at`/`end_at`
bounds, window gating, catch-up policy and suspend-resilient sleep — so the
hard part gets written either way. A stdlib-only module in `interfaces/`
satisfies the layer rule that `interfaces/` depends on nothing but the
standard library, and keeps one coherent implementation.

## Components

### `src/gilbert/interfaces/cron.py` (new)

Pure stdlib. Depends on nothing in Gilbert.

```python
class CronKind(StrEnum):
    FIELDS = "fields"     # 5- or 6-field expression
    EVERY = "every"       # @every <duration>
    ONCE = "once"         # @once[+delay] / @reboot

@dataclass(frozen=True)
class CronExpression:
    raw: str
    kind: CronKind
    # FIELDS: pre-expanded match sets
    seconds: frozenset[int]
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]
    dom_restricted: bool
    dow_restricted: bool
    last_day_of_month: bool          # L in DOM
    last_dom_offset: int | None      # L-3
    last_dow: int | None             # 5L  = last Friday
    nth_dow: tuple[int, int] | None  # FRI#3
    nearest_weekday: int | None      # 15W
    # EVERY / ONCE
    every_seconds: float
    once_delay_seconds: float

    def next_after(self, after: datetime, tz: tzinfo) -> datetime | None: ...
    def describe(self) -> str: ...

def parse(expression: str) -> CronExpression: ...   # raises CronParseError
```

`next_after` walks fields coarse-to-fine (month → day → hour → minute →
second), advancing and resetting lower fields rather than scanning
minute-by-minute — a yearly expression must not cost millions of iterations.
The walk is bounded at a 5-year horizon and returns `None` for expressions
that can never match (`0 0 30 2 *`).

`describe()` returns a human string ("every day at 3:30 AM") so the SPA never
needs a TypeScript cron parser.

### `src/gilbert/interfaces/scheduler.py` (rewritten `Schedule`)

`ScheduleType`, `interval_seconds`, `hour` and `minute` are removed.

```python
class CatchUpPolicy(StrEnum):
    SKIP = "skip"          # real cron behavior; the fire is lost
    ONCE = "once"          # one catch-up fire on startup, then resume
    BACKFILL = "backfill"  # replay every missed occurrence

class OverlapPolicy(StrEnum):
    SKIP = "skip"              # default: drop the fire if one is running
    QUEUE = "queue"            # await the in-flight fire, then run
    CONCURRENT = "concurrent"  # run anyway

@dataclass
class Schedule:
    expression: str
    timezone: str = ""                       # IANA name; "" = host local
    start_at: datetime | None = None
    end_at: datetime | None = None
    window_start_time: time | None = None
    window_end_time: time | None = None
    catch_up: CatchUpPolicy = CatchUpPolicy.SKIP
    overlap: OverlapPolicy = OverlapPolicy.SKIP
    jitter_seconds: float = 0.0
```

Compatibility constructors, unchanged signatures:

| Constructor | Compiles to |
|---|---|
| `Schedule.every(90)` | `@every 90s` |
| `Schedule.daily_at(3, 30)` | `30 3 * * *` |
| `Schedule.hourly_at(15)` | `15 * * * *` |
| `Schedule.once_after(45)` | `@once+45s` |
| `Schedule.cron(expr, tz=...)` | *(new)* verbatim |

`window_start_time` / `window_end_time` are retained as a **filter** on
candidate fires rather than a generator. As a filter they compose with every
expression kind, which is what makes `@every 90s` windowable — cron fields
alone cannot express a sub-minute rate inside a time-of-day window.

Catch-up defaults are set per constructor rather than uniformly: `daily_at` /
`hourly_at` / `cron` default to `ONCE` (a missed daily digest is worth one
catch-up run), while `every` defaults to `SKIP` (a 10-second poll tick has
nothing meaningful to catch up on) and `once_after` to `SKIP`.

### `src/gilbert/core/services/scheduler.py` (engine)

One next-fire path replaces the four-branch `_next_delay`:

```
expression.next_after(anchor, tz)
  → floor at start_at
  → gate through daily window
  → retire (None) if past end_at
  → add jitter
```

- **Chunked sleep.** `_sleep_until(target)` sleeps in bounded slices (60s cap),
  re-reading wall clock each slice. A host suspend or clock jump is detected on
  the next slice and corrected, instead of firing late by the lost interval.
- **Overlap.** `_Job.is_running` guards `_execute_job` per `OverlapPolicy`.
- **Catch-up.** Requires persisted fire history, which does not exist today.
  Adds a `scheduler_job_state` collection (`{id: <job name>, last_fire_at}`),
  written best-effort after each fire. This is deliberately separate from
  `scheduler_jobs` because **system jobs are never persisted there** — the
  separate collection is what lets a system job catch up at all.
- **Visibility.** `JobInfo` gains `next_run_at: str` and `description: str`,
  both serialized to WS frames and `list_timers`.

### `src/gilbert/migrations/0007_scheduler_cron_schedules.py` (new)

Rewrites persisted `scheduler_jobs` rows onto the new shape:

| Old row | New `expression` |
|---|---|
| `schedule_type=interval`, `interval_seconds=N` | `@every Ns` |
| `schedule_type=daily`, `hour=H`, `minute=M` | `M H * * *` |
| `schedule_type=hourly`, `minute=M` | `M * * * *` |
| `schedule_type=once`, `interval_seconds=N` | `@once+Ns` (existing `fire_at` preserved) |

`start_at`, `end_at`, `window_start_time`, `window_end_time` and `owner` carry
over untouched; `timezone`, `catch_up` and `overlap` are seeded with defaults.

Idempotent per the runner's re-execute-on-crash contract: a row that already
has a non-empty `expression` is skipped, so a crash mid-run replays safely.

### Frontend

`frontend/src/types/scheduler.ts` — `Schedule` gains `expression`, `timezone`,
`catch_up`, `overlap`, `description`; drops `type`, `interval_seconds`,
`hour`, `minute`. `Job` gains `next_run_at`.

`frontend/src/components/scheduler/SchedulerPage.tsx` — `formatScheduleBase()`
is replaced by rendering the backend-supplied `description`, with the raw
`expression` shown in the detail panel. No cron parsing in TypeScript.

### Documentation

- `docs/architecture/scheduler.md` — new subsystem walkthrough (none exists).
- `docs/adr/0022-one-cron-engine-for-all-schedules.md` — records the
  one-engine decision, the dialect, the DOM/DOW rule and the DST semantics.
- `src/gilbert/CONTEXT.md` — glossary entries for *job*, *expression*,
  *catch-up*, *fire*.

## Testing

- `tests/unit/test_cron.py` — table-driven across parsing (every dialect row
  above), the DOM-OR-DOW rule, `L`/`W`/`#`, impossible expressions, and
  explicit `America/Los_Angeles` spring-forward and fall-back transition cases
  asserting the semantics stated above.
- `tests/unit/test_scheduler_service.py` — extended for the single engine,
  catch-up policies, overlap policies, chunked sleep under a simulated clock
  jump, and `next_run_at` reporting.
- Migration tested against a real test SQLite database, per the project rule
  that database tests do not mock the DB. Includes a re-run assertion for
  idempotency.

## Risk

`Schedule` is consumed by 44 call sites and every field except the bounds is
being removed. The compatibility constructors absorb construction, but any
code *reading* `schedule.type` or `schedule.interval_seconds` breaks. Known
readers: the scheduler service itself, `_serialize_job`, `_tool_list_timers`,
`_persist_job`, `_load_persisted_jobs`, and the SPA. First implementation step
is a repo-wide sweep for direct field reads, so these surface at edit time
rather than at runtime.
