import type { ConfigActionMeta, ConfigParamMeta } from "@/types/config";

// ── TaskList ─────────────────────────────────────────────────────────

export type ListAccess =
  | "owner"
  | "admin"
  | "shared_user"
  | "shared_role";

export interface TaskList {
  id: string;
  name: string;
  backend_name: string;
  backend_config: Record<string, unknown>;
  owner_user_id: string;
  shared_with_users: string[];
  shared_with_roles: string[];
  poll_enabled: boolean;
  poll_interval_sec: number;
  is_default: boolean;
  created_at: string;
  last_sync_at: string;
  degraded_since: string;
  last_error: string;
  /** How the current user has access; null if none. */
  access: ListAccess | null;
  can_admin: boolean;
}

export interface TaskBackendInfo {
  name: string;
  config_params: ConfigParamMeta[];
  actions?: ConfigActionMeta[];
}

// ── Task ────────────────────────────────────────────────────────────

export type TaskStatus = "open" | "done" | "cancelled";

export type SyncStatus =
  | "synced"
  | "pending_push"
  | "push_failed"
  | "pending_delete";

export interface Task {
  id: string;
  list_id: string;
  title: string;
  notes: string;
  due_at: string;
  due_at_tz: string;
  completed_at: string;
  status: TaskStatus;
  /** 0 = none, 1 = low, 2 = medium, 3 = high, 4 = urgent. */
  priority: number;
  tags: string[];
  project: string;
  created_at: string;
  updated_at: string;
  /** Resolved backend name for this task's list. */
  backend: string;
  sync_status: SyncStatus;
  last_push_error: string;
}
