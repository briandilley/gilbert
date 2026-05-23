// Types mirroring ``src/gilbert/core/services/phone_call.py`` wire shape.
// Keep field names in sync with what the backend serializes.

export type CallStatus =
  | "initiated"
  | "ringing"
  | "connected"
  | "hung_up"
  | "failed";

export interface CallOutcome {
  appointment_booked?: boolean;
  appointment_datetime?: string;
  loaner_confirmed?: boolean;
  service_advisor?: string;
  notes?: string;
  escalated?: boolean;
  escalation_reason?: string;
  hang_up_reason?: string;
  forced_hangup_reason?: string;
  // Open-ended — the brain's ``note`` tool can stash any key.
  [key: string]: unknown;
}

export interface PhoneCallTranscriptTurn {
  who: "us" | "them" | "user_intervention" | "system";
  text: string;
  ts: number; // seconds since call-start
}

export interface PhoneCallIntervention {
  who: "user";
  ts: string; // ISO-8601
  text: string;
}

/** Compact shape returned by ``phone.call.list`` — full transcript is
 *  not included to keep the WS frame small. */
export interface PhoneCallSummary {
  call_id: string;
  user_id: string;
  to_number: string;
  status: CallStatus;
  started_at: string;
  ended_at: string;
  duration_seconds: number;
  brief_preview: string;
  outcome: CallOutcome;
  failure_reason: string;
}

/** Full call record returned by ``phone.call.get``. */
export interface PhoneCallDetail extends PhoneCallSummary {
  brief: string;
  from_number: string;
  callback_number: string;
  transcript: PhoneCallTranscriptTurn[];
  interventions: PhoneCallIntervention[];
}
