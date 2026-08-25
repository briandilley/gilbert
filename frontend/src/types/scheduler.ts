/**
 * Scheduler types — mirror the Python dataclasses in
 * gilbert.interfaces.scheduler exactly. The backend sends these as plain
 * JSON (snake_case) and we keep the same field names here.
 */

export type JobState = "pending" | "running" | "idle" | "done" | "failed";

export type ScheduledActionKind = "event" | "tool" | "ai_prompt";

/** What to do about fires missed while Gilbert was down. */
export type CatchUpPolicy = "skip" | "once" | "backfill";

/** What to do when a fire is due while the previous one still runs. */
export type OverlapPolicy = "skip" | "queue" | "concurrent";

/**
 * A job's schedule. Every schedule is a single cron expression — see
 * `gilbert.interfaces.cron` for the dialect (5- and 6-field forms, the
 * Quartz `L`/`W`/`#` day specifiers, `@daily`-style macros, `@every
 * 90s` for arbitrary intervals, and `@reboot` / `@once+45s` for
 * one-shots).
 *
 * `description` is rendered by the backend, which already has the
 * parsed expression — that is deliberate, so there is no cron parser in
 * the frontend. Display `description`; show `expression` verbatim when
 * the user wants the underlying detail.
 *
 * Optional bounds the backend honors at fire time:
 * - `start_at` / `end_at`: ISO-8601 naive-local datetimes delimiting
 *   when the job is allowed to fire. Empty string means unbounded.
 * - `window_start_time` / `window_end_time`: a `HH:MM[:SS]` daily
 *   window filtering candidate fires. Empty strings mean "no window".
 *   Paired: both set or neither.
 */
export interface Schedule {
  expression: string;
  description: string;
  /** IANA timezone name. Empty string means the host's local zone. */
  timezone: string;
  catch_up: CatchUpPolicy;
  overlap: OverlapPolicy;
  start_at: string;
  end_at: string;
  window_start_time: string;
  window_end_time: string;
}

/**
 * What a job does when it fires:
 * - `event`: publishes a `timer.fired` / `alarm.fired` event with `message`
 * - `tool`: invokes `tool` with `tool_arguments`
 * - `ai_prompt`: runs `ai_prompt` through the AI service (rate-limited)
 */
export interface ScheduledAction {
  type: ScheduledActionKind;
  tool: string;
  tool_arguments: Record<string, unknown>;
  ai_prompt: string;
  message: string;
}

/** Serialized JobInfo as returned by scheduler.job.list / scheduler.job.get. */
export interface Job {
  name: string;
  /** "system" jobs are registered in-memory by core services and cannot be
   *  removed; "user" jobs are created via set_timer/set_alarm and persisted. */
  type: "system" | "user";
  state: JobState;
  enabled: boolean;
  owner: string;
  run_count: number;
  last_run: string;
  last_duration_seconds: number;
  last_error: string;
  /** ISO-8601 timestamp of the next scheduled fire; empty when retired
   *  or disabled. */
  next_run_at: string;
  schedule: Schedule;
  action: ScheduledAction;
}
