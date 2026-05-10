// Camera-related TypeScript types — mirrors the WS RPC payloads from
// CameraEventService.

export interface CameraInfo {
  name: string;
  labels: string[];
  zones: string[];
  role_visibility: "everyone" | "user" | "admin" | string;
  has_audio: boolean;
}

export interface CameraEventRow {
  event_id: string;
  camera: string;
  label: string;
  sub_label: string;
  score: number;
  phase: "active" | "ended" | string;
  started_at: number;
  ended_at: number;
  duration_seconds: number;
  zones: string[];
  snapshot_url: string;
  clip_url: string;
  has_snapshot: boolean;
  has_clip: boolean;
  source_backend: string;
  vision_text: string;
  required_role: string;
}

export interface CameraMute {
  camera: string;
  label: string;
  until_ms: number;
  set_by?: string;
  set_at_ms?: number;
}
