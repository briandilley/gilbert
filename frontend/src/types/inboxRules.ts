/** Inbox rules — mirrors ``gilbert.interfaces.inbox_rules.InboxRule``. */

export interface InboxRule {
  id: string;
  name: string;
  enabled: boolean;
  dry_run: boolean;
  mailbox_ids: string[];
  sender_contains: string[];
  subject_contains: string[];
  match_condition: string;
  match_profile: string;
  action_instructions: string;
  action_profile: string;
  announce: boolean;
  speakers: string[];
  active_days: number[];
  start_hour: number;
  end_hour: number;
  timezone: string;
  max_age_hours: number;
  dedupe_window_hours: number;
  created_at: string;
  updated_at: string;
}

export interface InboxRuleRun {
  _id: string;
  rule_id: string;
  rule_name: string;
  message_id: string;
  mailbox_id: string;
  subject: string;
  sender: string;
  received_at: string;
  created_at: string;
  updated_at: string;
  status: string;
  reason: string;
  dedupe_key: string;
  output: string;
  error: string;
  attempts: number;
  conversation_id: string;
}

export interface InboxRuleOptions {
  profiles: Array<{ name: string; tool_mode: string; tools: string[] }>;
  tools: Array<{ name: string; description: string }>;
  speakers: string[];
  mailboxes: Array<{ id: string; name: string }>;
}
