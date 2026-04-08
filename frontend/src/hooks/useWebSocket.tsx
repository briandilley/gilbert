import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useCallback,
  useState,
  type ReactNode,
} from "react";
import type { GilbertEvent, WsFrame } from "@/types/events";
import { useAuth } from "./useAuth";

type EventHandler = (event: GilbertEvent) => void;

interface WebSocketContextValue {
  /** Subscribe to events by event_type (from gilbert.event frames). */
  subscribe: (eventType: string, handler: EventHandler) => () => void;
  /** Send a typed frame to the server. Returns the frame id for correlation. */
  send: (frame: WsFrame) => string;
  connected: boolean;
}

let _nextId = 0;
function nextFrameId(): string {
  return `f_${++_nextId}_${Date.now()}`;
}

const defaultValue: WebSocketContextValue = {
  subscribe: () => () => {},
  send: () => "",
  connected: false,
};

const WebSocketContext = createContext<WebSocketContextValue>(defaultValue);

export function WebSocketProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const [connected, setConnected] = useState(false);
  const handlersRef = useRef<Map<string, Set<EventHandler>>>(new Map());
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeout = useRef<ReturnType<typeof setTimeout>>(undefined);
  const pingInterval = useRef<ReturnType<typeof setInterval>>(undefined);

  const subscribe = useCallback(
    (eventType: string, handler: EventHandler) => {
      if (!handlersRef.current.has(eventType)) {
        handlersRef.current.set(eventType, new Set());
      }
      handlersRef.current.get(eventType)!.add(handler);
      return () => {
        handlersRef.current.get(eventType)?.delete(handler);
      };
    },
    [],
  );

  const send = useCallback((frame: WsFrame): string => {
    const id = frame.id || nextFrameId();
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ ...frame, id }));
    }
    return id;
  }, []);

  useEffect(() => {
    if (!user) return;

    let disposed = false;
    let backoff = 1000;

    function connect() {
      if (disposed) return;

      const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(`${proto}//${window.location.host}/ws/events`);
      wsRef.current = ws;

      ws.onopen = () => {
        setConnected(true);
        backoff = 1000;
        // Start heartbeat
        clearInterval(pingInterval.current);
        pingInterval.current = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "gilbert.ping" }));
          }
        }, 30000);
      };

      ws.onclose = () => {
        setConnected(false);
        wsRef.current = null;
        clearInterval(pingInterval.current);
        if (!disposed) {
          reconnectTimeout.current = setTimeout(connect, backoff);
          backoff = Math.min(backoff * 2, 30000);
        }
      };

      ws.onmessage = (msg) => {
        try {
          const frame = JSON.parse(msg.data);
          const type: string = frame.type || "";

          if (type === "gilbert.event") {
            // Dispatch bus events to subscribed handlers
            const event: GilbertEvent = {
              event_type: frame.event_type,
              data: frame.data || {},
              source: frame.source || "",
              timestamp: frame.timestamp || "",
            };
            handlersRef.current
              .get(event.event_type)
              ?.forEach((h) => h(event));
            handlersRef.current.get("*")?.forEach((h) => h(event));
          }
          // gilbert.welcome, gilbert.pong, etc. — no action needed for now
          // RPC results (chat.message.send.result, etc.) are handled by
          // promise-based callers if we add that later
        } catch {
          // ignore parse errors
        }
      };
    }

    connect();

    return () => {
      disposed = true;
      clearTimeout(reconnectTimeout.current);
      clearInterval(pingInterval.current);
      wsRef.current?.close();
    };
  }, [user]);

  return (
    <WebSocketContext.Provider value={{ subscribe, send, connected }}>
      {children}
    </WebSocketContext.Provider>
  );
}

export function useWebSocket(): WebSocketContextValue {
  return useContext(WebSocketContext);
}
