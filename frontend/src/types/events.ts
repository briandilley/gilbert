/** Base frame — all WebSocket messages have a type field plus arbitrary payload. */
export interface WsFrame {
  type: string;
  id?: string;
  ref?: string;
  [key: string]: unknown;
}

/** Server → Client: wrapped event from the event bus. */
export interface WsEventFrame extends WsFrame {
  type: "gilbert.event";
  event_type: string;
  data: Record<string, unknown>;
  source: string;
  timestamp: string;
}

/** Server → Client: sent after authentication. */
export interface WsWelcomeFrame extends WsFrame {
  type: "gilbert.welcome";
  user_id: string;
  roles: string[];
  subscriptions: string[];
}

/** Server → Client: heartbeat response. */
export interface WsPongFrame extends WsFrame {
  type: "gilbert.pong";
}

/** Server → Client: error response. */
export interface WsErrorFrame extends WsFrame {
  type: "gilbert.error";
  error: string;
  code: number;
}

/** Legacy alias — components use this for event handler callbacks. */
export interface GilbertEvent {
  event_type: string;
  data: Record<string, unknown>;
  source: string;
  timestamp: string;
}
