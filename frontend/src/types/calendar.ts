import type { ConfigParamMeta } from "@/types/config";

// ── Account ─────────────────────────────────────────────────────

export type CalendarAccess = "owner" | "admin" | "shared_user" | "shared_role";

export interface CalendarAccount {
  id: string;
  name: string;
  email_address: string;
  backend_name: string;
  backend_config: Record<string, unknown>;
  calendar_id: string;
  timezone: string;
  working_hours_start_hour: number;
  working_hours_end_hour: number;
  owner_user_id: string;
  shared_with_users: string[];
  shared_with_roles: string[];
  poll_enabled: boolean;
  poll_interval_sec: number;
  upcoming_event_lookahead_minutes: number;
  health: string;
  last_error: string;
  last_error_at: string;
  created_at: string;
  /** How the current user has access; null if none. */
  access: CalendarAccess | null;
  can_admin: boolean;
}

export interface CalendarBackendInfo {
  name: string;
  display_name: string;
  config_params: ConfigParamMeta[];
}

// ── Event ────────────────────────────────────────────────────────

export type EventStatus = "confirmed" | "tentative" | "cancelled";
export type EventVisibility = "default" | "public" | "private";
export type AttendeeResponseStatus =
  | "needsAction"
  | "accepted"
  | "declined"
  | "tentative";

export interface CalendarAttendee {
  email: string;
  name: string;
  response_status: AttendeeResponseStatus;
  is_organizer: boolean;
  is_self: boolean;
}

export interface CalendarEvent {
  event_id: string;
  calendar_id: string;
  account_id: string;
  title: string;
  start: string; // ISO 8601
  end: string;
  etag: string;
  all_day: boolean;
  description: string;
  location: string;
  organizer_email: string;
  attendees: CalendarAttendee[];
  visibility: EventVisibility;
  status: EventStatus;
  transparency: string;
  html_link: string;
  recurring_event_id: string | null;
}

export interface FreeBusyBlock {
  calendar_id: string;
  start: string;
  end: string;
}

export interface FreeSlot {
  start: string;
  end: string;
  slot_duration_minutes: number;
  requested_duration_minutes: number;
}

export interface EventDraft {
  title: string;
  start: string;
  end?: string;
  duration_minutes?: number;
  description?: string;
  location?: string;
  attendees?: string[]; // emails
  all_day?: boolean;
  send_invites?: boolean;
}

