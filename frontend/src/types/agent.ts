/**
 * Autonomous agent types — mirror Python dataclasses in
 * ``gilbert.interfaces.agent``.
 *
 * Storage rows use ``_id`` (not ``id``) for primary keys — see
 * ``_agent_to_dict`` / ``_run_to_dict`` / ``_commitment_to_dict`` /
 * ``_memory_to_dict`` in ``src/gilbert/core/services/agent.py``. The
 * frontend preserves that shape verbatim; we don't translate
 * ``_id`` → ``id``.
 */

export type AgentStatus = "enabled" | "disabled";
export type MemoryState = "short_term" | "long_term";
export type RunStatus = "running" | "completed" | "failed" | "timed_out";
export type AvatarKind = "emoji" | "icon" | "image";

export interface Agent {
  _id: string;
  owner_user_id: string;
  name: string;
  role_label: string;
  persona: string;
  system_prompt: string;
  procedural_rules: string;
  profile_id: string;
  conversation_id: string;
  status: AgentStatus;
  avatar_kind: AvatarKind;
  avatar_value: string;
  lifetime_cost_usd: number;
  cost_cap_usd: number | null;
  tools_allowed: string[] | null;
  heartbeat_enabled: boolean;
  heartbeat_interval_s: number;
  heartbeat_checklist: string;
  dream_enabled: boolean;
  dream_quiet_hours: string;
  dream_probability: number;
  dream_max_per_night: number;
  created_at: string;
  updated_at: string;
}

export interface AgentMemory {
  _id: string;
  agent_id: string;
  content: string;
  state: MemoryState;
  kind: string;
  tags: string[];
  score: number;
  created_at: string;
  last_used_at: string | null;
}

export interface Commitment {
  _id: string;
  agent_id: string;
  content: string;
  due_at: string;
  created_at: string;
  completed_at: string | null;
  completion_note: string;
}

export interface AgentRun {
  _id: string;
  agent_id: string;
  triggered_by: string;
  trigger_context: Record<string, unknown>;
  started_at: string;
  status: RunStatus;
  conversation_id: string;
  delegation_id: string;
  ended_at: string | null;
  final_message_text: string | null;
  rounds_used: number;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  error: string | null;
  awaiting_user_input: boolean;
  pending_question: string | null;
  pending_actions: Array<{
    id: string;
    kind: string;
    label: string;
    payload?: Record<string, unknown>;
  }>;
}

export interface ToolDescriptor {
  name: string;
  description: string;
  provider: string;
  required_role?: string;
}

export type ToolGroupMap = Record<string, string[]>;

export interface AgentDefaults {
  default_persona?: string;
  default_system_prompt?: string;
  default_procedural_rules?: string;
  default_heartbeat_interval_s?: number;
  default_heartbeat_checklist?: string;
  default_dream_enabled?: boolean;
  default_dream_quiet_hours?: string;
  default_dream_probability?: number;
  default_dream_max_per_night?: number;
  default_profile_id?: string;
  default_avatar_kind?: AvatarKind;
  default_avatar_value?: string;
  default_tools_allowed?: string[] | null;
  tool_groups?: ToolGroupMap;
}

export interface MemoryFilters {
  state?: MemoryState;
  kind?: string;
  tags?: string[];
  q?: string;
  limit?: number;
}

export interface AgentCreatePayload {
  name: string;
  role_label?: string;
  persona?: string;
  system_prompt?: string;
  procedural_rules?: string;
  profile_id?: string;
  avatar_kind?: AvatarKind;
  avatar_value?: string;
  cost_cap_usd?: number | null;
  tools_allowed?: string[] | null;
  heartbeat_enabled?: boolean;
  heartbeat_interval_s?: number;
  heartbeat_checklist?: string;
  dream_enabled?: boolean;
  dream_quiet_hours?: string;
  dream_probability?: number;
  dream_max_per_night?: number;
}

export type AgentUpdatePayload = Partial<AgentCreatePayload> & {
  status?: AgentStatus;
};

// ── Multi-agent goals (Phase 4) ───────────────────────────────────

export type GoalStatus =
  | "new"
  | "in_progress"
  | "blocked"
  | "complete"
  | "cancelled";
export type AssignmentRole = "driver" | "collaborator" | "reviewer";

export interface Goal {
  _id: string;
  owner_user_id: string;
  name: string;
  description: string;
  status: GoalStatus;
  war_room_conversation_id: string;
  cost_cap_usd: number | null;
  lifetime_cost_usd: number;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface GoalAssignment {
  _id: string;
  goal_id: string;
  agent_id: string;
  role: AssignmentRole;
  assigned_at: string;
  assigned_by: string;
  removed_at: string | null;
  handoff_note: string;
}

export interface WarRoomPost {
  author_id: string;
  author_name: string;
  author_kind: "agent" | "user";
  body: string;
  ts: string;
}

export interface GoalSummary {
  goal: Goal;
  assignees: Array<{
    agent_id: string;
    agent_name: string;
    role: AssignmentRole;
  }>;
  recent_posts: WarRoomPost[];
  is_dependency_blocked: boolean;
}
