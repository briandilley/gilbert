import type { ConfigParamMeta } from "@/types/config";

// ── Feed ─────────────────────────────────────────────────────────────

export type FeedAccess = "owner" | "admin" | "shared_user" | "shared_role";

export interface Feed {
  id: string;
  name: string;
  url: string;
  backend_name: string;
  backend_config: Record<string, unknown>;
  owner_user_id: string;
  shared_with_users: string[];
  shared_with_roles: string[];
  category: string;
  importance_weight: number;
  ingest_to_knowledge: boolean;
  briefing_eligible: boolean;
  poll_enabled: boolean;
  poll_interval_sec: number;
  suggested_poll_interval_sec: number;
  effective_poll_interval_sec: number;
  last_polled_at: string;
  last_poll_status_code: number;
  last_poll_items_total: number;
  last_poll_items_new: number;
  last_poll_duration_ms: number;
  consecutive_failures: number;
  last_error: string;
  created_at: string;
  /** Per-list payload extension. */
  unread_count?: number;
  /** How the current user has access; null if none. */
  access: FeedAccess | null;
  can_admin: boolean;
}

export interface FeedBackendInfo {
  name: string;
  config_params: ConfigParamMeta[];
}

// ── Item ─────────────────────────────────────────────────────────────

export interface FeedItem {
  id: string;
  feed_id: string;
  item_uid: string;
  title: string;
  link: string;
  summary: string;
  ai_summary: string;
  author: string;
  /** -1.0 = unscored (lazy_score=true means waiting for backlog drain). */
  score: number;
  score_reason: string;
  lazy_score: boolean;
  read: boolean;
  briefed_at: string;
  ingested_to_knowledge: boolean;
  published_at: string;
  received_at: string;
  enclosure_url: string;
  enclosure_mime: string;
}

// ── Briefing ────────────────────────────────────────────────────────

export interface BriefingHeadline {
  item_id: string;
  title: string;
  one_liner: string;
  score: number;
  link: string;
}

export interface BriefingPayload {
  briefing_id: string;
  spoken: string;
  headlines: BriefingHeadline[];
  item_ids: string[];
  since: string;
  cached?: boolean;
}

export interface PollNowResult {
  items_seen: number;
  items_new: number;
  error: string;
  previous_items_total: number;
  previous_items_new: number;
}

export interface OpmlImportResult {
  url: string;
  error: string;
}
