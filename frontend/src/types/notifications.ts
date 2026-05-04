/**
 * Notification types — mirror Python dataclasses in
 * gilbert.interfaces.notifications. Backend sends snake_case JSON; we
 * keep matching field names here.
 */

export type NotificationUrgency = "info" | "normal" | "urgent";

export interface Notification {
  id: string;
  user_id: string;
  source: string;
  message: string;
  urgency: NotificationUrgency;
  created_at: string; // ISO-8601
  read: boolean;
  read_at: string | null;
  source_ref: Record<string, unknown> | null;
}

export interface NotificationListResult {
  items: Notification[];
  unread_count: number;
}
