import type { ConfigParamMeta } from "@/types/config";

// ── Mailbox ─────────────────────────────────────────────────────

export type MailboxAccess = "owner" | "admin" | "shared_user" | "shared_role";

export interface InboxMailbox {
  id: string;
  name: string;
  email_address: string;
  backend_name: string;
  backend_config: Record<string, unknown>;
  owner_user_id: string;
  shared_with_users: string[];
  shared_with_roles: string[];
  poll_enabled: boolean;
  poll_interval_sec: number;
  created_at: string;
  /** How the current user has access; null if none (shouldn't happen in listings). */
  access: MailboxAccess | null;
  can_admin: boolean;
}

export interface EmailBackendInfo {
  name: string;
  config_params: ConfigParamMeta[];
}

// ── Stats ───────────────────────────────────────────────────────

export interface InboxStats {
  total: number;
  inbound: number;
}

// ── Messages ────────────────────────────────────────────────────

export interface InboxMessage {
  mailbox_id: string;
  message_id: string;
  thread_id?: string;
  date: string;
  sender_email: string;
  sender_name: string;
  subject: string;
  snippet: string;
  is_inbound: boolean;
}

export interface EmailAddress {
  email: string;
  name: string;
}

export interface MessageDetail extends InboxMessage {
  to: EmailAddress[];
  cc: EmailAddress[];
  body_text?: string;
  body_html?: string;
  in_reply_to?: string;
}

// ── Outbox ──────────────────────────────────────────────────────

export type OutboxStatus =
  | "pending"
  | "sending"
  | "sent"
  | "failed"
  | "cancelled";

export interface OutboxEntry {
  id: string;
  mailbox_id: string;
  status: OutboxStatus;
  send_at: string;
  created_by_user_id: string;
  created_at: string;
  sent_at: string | null;
  error: string | null;
  retry_count: number;
  subject: string;
  to: string[];
}
