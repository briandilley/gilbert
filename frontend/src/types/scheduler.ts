/**
 * Scheduler types — mirror the Python dataclasses in
 * gilbert.interfaces.scheduler exactly. The backend sends these as plain
 * JSON (snake_case) and we keep the same field names here.
 */

export type ScheduleKind = "interval" | "daily" | "hourly" | "once";

export type JobState = "pending" | "running" | "idle" | "done" | "failed";

export type ScheduledActionKind = "event" | "tool" | "ai_prompt";

/**
 * A job's schedule — how often it fires.
 * - `interval`: every N seconds
 * - `daily`: once per day at hour:minute
 * - `hourly`: once per hour at minute
 * - `once`: single-shot N seconds from creation
 *
 * Recurring schedules (interval/daily/hourly) can also carry optional
 * bounds that the backend honors at fire time:
 * - `start_at` / `end_at`: ISO-8601 naive-local datetimes delimiting
 *   when the job is allowed to fire. Empty string means unbounded.
 * - `window_start_time` / `window_end_time`: a `HH:MM[:SS]` daily
 *   window that gates interval fires (ignored for daily/hourly/once).
 *   Empty strings mean "no window". Paired: both set or neither.
 */
export interface Schedule {
  type: ScheduleKind;
  interval_seconds: number;
  hour: number;
  minute: number;
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
  schedule: Schedule;
  action: ScheduledAction;
}
