# ADR-0022: One cron engine for all schedules

**Status:** Accepted
**Date:** 2026-08-25

## Context

`Schedule` carried four independent kinds — `INTERVAL`, `DAILY`,
`HOURLY`, `ONCE` — each with its own branch in the scheduler's
next-fire calculation. Four consequences followed:

- Anything cron can express beyond "daily at H:M" or "hourly at :M" —
  weekdays only, the third Friday, every 15 minutes between 9 and 5 —
  required a bespoke job with its own timing logic.
- Time arithmetic ran on naive `datetime.now()`. A `daily_at(2, 30)`
  job fires twice or not at all on a DST boundary.
- A daily job slept for up to 86400 seconds in one call, so a host
  suspend made it fire late by exactly the suspended duration.
- There was no missed-fire policy, no overlap policy, and no
  `next_run_at`, so a fire lost to downtime was lost silently and no
  caller could ask when a job runs next.

Fixing these four branch-by-branch would have meant implementing DST
handling and drift correction three times.

## Decision

**Every schedule is a single cron expression evaluated by one engine**
in `gilbert.interfaces.cron`.

`ScheduleType` and the per-kind rate fields (`interval_seconds`,
`hour`, `minute`) are removed. `Schedule` carries one `expression`
string plus timezone, bounds, and policies. The four legacy
constructors are retained as compilers to the dialect, so all 44
existing call sites are unchanged and every one of them gains
DST-correct, suspend-resilient behaviour without being edited.

### Dialect

Standard cron cannot express two things Gilbert genuinely needs:
sub-minute rates (10- and 30-second poll ticks) and one-shot startup
jobs. Both are covered by established extensions rather than
invention:

| Feature | Syntax | Precedent |
|---|---|---|
| 5-field POSIX | `30 3 * * *` | Vixie |
| 6-field with seconds | `*/30 * * * * *` | Quartz, Spring, robfig/cron |
| Ranges, lists, steps | `1-5`, `1,3,5`, `*/15`, `0-30/5` | Vixie |
| Names | `JAN`–`DEC`, `SUN`–`SAT` | Vixie |
| Day extensions | `L`, `W`, `#`, `?` | Quartz |
| Macros | `@yearly` … `@hourly` | Vixie |
| Interval | `@every 90s`, `@every 1h30m` | robfig/cron |
| Startup / one-shot | `@reboot`, `@once`, `@once+45s` | `@reboot` is Vixie |

Only `@once+<delay>` is a Gilbert addition, generalising `@reboot`
(which is exactly `@once+0s`).

### Specified semantics

Two behaviours differ silently between cron implementations, so they
are fixed here and tested directly rather than left to the parser:

**Day-of-month / day-of-week is an OR.** When both fields are
restricted (neither `*` nor `?`), a day matches if *either* matches.
When only one is restricted, only that one applies.

**DST.** Field expressions evaluate against local wall clock in the
job's timezone, then localise:

- A nonexistent local time on the spring-forward day fires **once**, at
  the transition instant.
- An ambiguous local time on the fall-back day fires on the **first**
  occurrence only.
- Wildcard-hour expressions follow real elapsed time across both.
- `@every` is a pure duration — timezone-independent, DST-immune.

## Alternatives considered

**Add `CRON` as a fifth kind, leave the others alone.** Smallest
change, but `daily`/`hourly` keep their naive-local arithmetic, so the
DST bug persists for every existing caller and only newly-written cron
jobs are correct. Rejected: it fixes the feature gap without fixing the
correctness gap.

**Use `croniter`.** Large surface, recurring history of DST bugs, and
the first core dependency added purely for scheduling.

**Use `cronsim`.** Small, MIT, DST-aware, no transitive deps — the
strongest third-party candidate. Rejected because it deliberately omits
the Quartz `L`/`W`/`#` extensions, so "everything cron supports" would
stop at the POSIX feature set.

Either third-party option would still need its result post-processed
against Gilbert's `start_at`/`end_at` bounds, window gating, catch-up
policy, and chunked sleep — the hard part gets written regardless. A
stdlib-only module also satisfies the rule that `interfaces/` depends on
nothing outside the standard library.

## Consequences

- One next-fire implementation. A timing fix lands everywhere at once.
- Every existing daily/hourly job became DST-correct with no edits.
- `interfaces/cron.py` is code we own and must maintain, including the
  Quartz extensions and the DST edge cases. This is deliberate: those
  edge cases are exactly where third-party engines disagree, and the
  test suite pins our behaviour.
- Catch-up requires fire history, so the scheduler now writes a
  `scheduler_job_state` row after each fire. It is separate from
  `scheduler_jobs` because system jobs are never persisted there and
  would otherwise be unable to catch up.
- Fires are dispatched as tasks rather than awaited inline, which is
  what makes `OverlapPolicy` meaningful. One-shot jobs are still
  awaited inline so their terminal state is not overwritten.
- Persisted rows required migration `0007_scheduler_cron_schedules`.
  The loader deliberately **skips** a row with no `expression` rather
  than guessing one — inventing `@every 60s` for an unconvertible row
  would silently turn a one-shot into a job that fires forever.
