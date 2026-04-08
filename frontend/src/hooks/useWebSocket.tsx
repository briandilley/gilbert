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
import { ApiError } from "@/api/client";
import { useAuth } from "./useAuth";

type EventHandler = (event: GilbertEvent) => void;

interface PendingRpc {
  resolve: (data: unknown) => void;
  reject: (error: ApiError) => void;
  timer: ReturnType<typeof setTimeout>;
}

interface WebSocketContextValue {
  /** Subscribe to events by event_type (from gilbert.event frames). */
  subscribe: (eventType: string, handler: EventHandler) => () => void;
  /** Send a typed frame (fire-and-forget). */
  send: (frame: WsFrame) => string;
  /** Send a frame and await the response (promise-based RPC). */
  rpc: <T = Record<string, unknown>>(
    frame: Omit<WsFrame, "id">,
    timeout?: number,
  ) => Promise<T>;
  connected: boolean;
}

let _nextId = 0;
function nextFrameId(): string {
  return `f_${++_nextId}_${Date.now()}`;
}

const DEFAULT_TIMEOUT = 15_000;
const LONG_TIMEOUT = 120_000; // for AI operations

/** Frame types that need a longer timeout (AI thinking). */
const LONG_TIMEOUT_TYPES = new Set([
  "chat.message.send",
  "chat.form.submit",
]);

const defaultValue: WebSocketContextValue = {
  subscribe: () => () => {},
  send: () => "",
  rpc: () => Promise.reject(new ApiError(503, "WebSocket not connected")),
  connected: false,
};

const WebSocketContext = createContext<WebSocketContextValue>(defaultValue);

export function WebSocketProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const [connected, setConnected] = useState(false);
  const handlersRef = useRef<Map<string, Set<EventHandler>>>(new Map());
  const pendingRef = useRef<Map<string, PendingRpc>>(new Map());
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

  const rpc = useCallback(
    <T = Record<string, unknown>>(
      frame: Omit<WsFrame, "id">,
      timeout?: number,
    ): Promise<T> => {
      return new Promise<T>((resolve, reject) => {
        const ws = wsRef.current;
        if (!ws || ws.readyState !== WebSocket.OPEN) {
          reject(new ApiError(503, "WebSocket not connected"));
          return;
        }

        const id = nextFrameId();
        const ms =
          timeout ?? (LONG_TIMEOUT_TYPES.has(frame.type) ? LONG_TIMEOUT : DEFAULT_TIMEOUT);

        const timer = setTimeout(() => {
          pendingRef.current.delete(id);
          reject(new ApiError(408, "RPC timeout"));
        }, ms);

        pendingRef.current.set(id, {
          resolve: resolve as (data: unknown) => void,
          reject,
          timer,
        });

        ws.send(JSON.stringify({ ...frame, id }));
      });
    },
    [],
  );

  const rejectAllPending = useCallback((error: ApiError) => {
    for (const [id, pending] of pendingRef.current) {
      clearTimeout(pending.timer);
      pending.reject(error);
    }
    pendingRef.current.clear();
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
        rejectAllPending(new ApiError(503, "Connection lost"));
        if (!disposed) {
          reconnectTimeout.current = setTimeout(connect, backoff);
          backoff = Math.min(backoff * 2, 30000);
        }
      };

      ws.onmessage = (msg) => {
        try {
          const frame = JSON.parse(msg.data);
          const type: string = frame.type || "";

          // Route RPC responses to pending promises
          if (frame.ref && pendingRef.current.has(frame.ref)) {
            const pending = pendingRef.current.get(frame.ref)!;
            pendingRef.current.delete(frame.ref);
            clearTimeout(pending.timer);

            if (type === "gilbert.error") {
              pending.reject(new ApiError(frame.code || 500, frame.error || "Unknown error"));
            } else {
              // Strip protocol fields, pass the payload
              const { type: _t, ref: _r, ...payload } = frame;
              pending.resolve(payload);
            }
            return;
          }

          // Dispatch bus events
          if (type === "gilbert.event") {
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
      rejectAllPending(new ApiError(503, "Provider unmounted"));
      wsRef.current?.close();
    };
  }, [user, rejectAllPending]);

  return (
    <WebSocketContext.Provider value={{ subscribe, send, rpc, connected }}>
      {children}
    </WebSocketContext.Provider>
  );
}

export function useWebSocket(): WebSocketContextValue {
  return useContext(WebSocketContext);
}
