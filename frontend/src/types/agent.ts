/**
 * Autonomous agent types — mirror Python dataclasses in
 * gilbert.interfaces.agent.
 */

export type GoalStatus = "enabled" | "disabled" | "completed";
export type RunStatus = "running" | "completed" | "failed";
export type TriggerType = "time" | "event" | null;

export interface TriggerConfig {
  // TIME shape:
  kind?: "interval" | "daily_at" | "hourly_at";
  seconds?: number;
  hour?: number;
  minute?: number;
  // EVENT shape:
  event_type?: string;        // legacy: single event subscription
  event_types?: string[];     // new: multi-event subscription
  filter?: {
    field: string;
    op: "eq" | "neq" | "in" | "contains";
    value: unknown;
  };
}

export interface Goal {
  id: string;
  owner_user_id: string;
  name: string;
  instruction: string;
  profile_id: string;
  status: GoalStatus;
  created_at: string;
  updated_at: string;
  trigger_type: TriggerType | string | null;
  trigger_config: TriggerConfig | null;
  conversation_id: string;
  last_run_at: string | null;
  last_run_status: RunStatus | null;
  run_count: number;
  completed_at: string | null;
  completed_reason: string | null;
  stateless: boolean;
}

export interface AgentRun {
  id: string;
  goal_id: string;
  triggered_by: string;
  started_at: string;
  ended_at: string | null;
  status: RunStatus;
  conversation_id: string;
  final_message_text: string | null;
  rounds_used: number;
  tokens_in: number;
  tokens_out: number;
  error: string | null;
  complete_goal_called: boolean;
  complete_reason: string | null;
}

export interface GoalCreatePayload {
  name: string;
  instruction: string;
  profile_id: string;
  trigger_type?: "time" | "event" | "";
  trigger_config?: TriggerConfig;
  stateless?: boolean;
}

export interface GoalUpdatePayload {
  name?: string;
  instruction?: string;
  profile_id?: string;
  status?: GoalStatus;
  trigger_type?: "time" | "event" | "";
  trigger_config?: TriggerConfig;
  stateless?: boolean;
}
