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

/**
 * Handler for a server-initiated RPC frame (e.g. mcp.bridge.call).
 * Returns the payload that will be shipped back to the server as a
 * reply frame keyed by `ref`. Thrown errors are serialized as
 * `{ok: false, error: <message>}`.
 */
export type ServerRpcHandler = (
  frame: WsFrame,
) => Promise<Record<string, unknown>>;

interface PendingRpc {
  resolve: (data: unknown) => void;
  reject: (error: ApiError) => void;
  timer: ReturnType<typeof setTimeout>;
  /** Original timeout in ms — used when a keepalive event resets the
   *  deadline so we push it out by the same budget we started with. */
  timeoutMs: number;
  /** Conversation this RPC is acting on, if any. Keepalive events
   *  only reset timers whose ``conversationId`` matches the event's
   *  ``conversation_id``. */
  conversationId?: string;
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
  /** Variant of ``rpc`` that also returns the generated frame id
   *  (``ref``) immediately. Use this when the caller needs to hold a
   *  handle on the in-flight RPC — e.g. to send a follow-up frame
   *  that cancels it. The promise resolves the same way as ``rpc``. */
  rpcWithRef: <T = Record<string, unknown>>(
    frame: Omit<WsFrame, "id">,
    timeout?: number,
  ) => { ref: string; promise: Promise<T> };
  /**
   * Register a handler invoked when the server sends a frame of the
   * given type that expects a reply. The reply is sent automatically
   * with `ref` set to the incoming `id`; the handler just returns the
   * payload fields. Returns an unsubscribe function.
   */
  registerServerHandler: (
    type: string,
    handler: ServerRpcHandler,
  ) => () => void;
  connected: boolean;
}

let _nextId = 0;
function nextFrameId(): string {
  return `f_${++_nextId}_${Date.now()}`;
}

const DEFAULT_TIMEOUT = 15_000;
// Ceiling for an AI-driven RPC when there's no observable progress. The
// effective deadline usually doesn't get hit because any ``chat.stream.*``
// or ``chat.tool.*`` event for an in-flight chat-send RPC resets its
// timer (see KEEPALIVE_EVENT_TYPES below). This is the "is the backend
// actually dead" backstop.
const LONG_TIMEOUT = 600_000; // 10 minutes

/** Frame types that need a longer timeout (AI thinking). */
const LONG_TIMEOUT_TYPES = new Set([
  "chat.message.send",
  "chat.form.submit",
  "config.prompt.author",
]);

/** Bus events whose arrival means an AI turn is still progressing.
 *  Any pending long-timeout RPC on the same conversation gets its
 *  deadline pushed back when one of these fires. */
const KEEPALIVE_EVENT_TYPES = new Set([
  "chat.stream.text_delta",
  "chat.stream.round_complete",
  "chat.tool.started",
  "chat.tool.completed",
]);

const defaultValue: WebSocketContextValue = {
  subscribe: () => () => {},
  send: () => "",
  rpc: () => Promise.reject(new ApiError(503, "WebSocket not connected")),
  rpcWithRef: () => ({
    ref: "",
    promise: Promise.reject(new ApiError(503, "WebSocket not connected")),
  }),
  registerServerHandler: () => () => {},
  connected: false,
};

const WebSocketContext = createContext<WebSocketContextValue>(defaultValue);

export function WebSocketProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const [connected, setConnected] = useState(false);
  const handlersRef = useRef<Map<string, Set<EventHandler>>>(new Map());
  const pendingRef = useRef<Map<string, PendingRpc>>(new Map());
  const serverHandlersRef = useRef<Map<string, ServerRpcHandler>>(new Map());
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

  const rpcWithRef = useCallback(
    <T = Record<string, unknown>>(
      frame: Omit<WsFrame, "id">,
      timeout?: number,
    ): { ref: string; promise: Promise<T> } => {
      const id = nextFrameId();
      const promise = new Promise<T>((resolve, reject) => {
        const ms =
          timeout ?? (LONG_TIMEOUT_TYPES.has(frame.type as string) ? LONG_TIMEOUT : DEFAULT_TIMEOUT);

        const makeTimer = (): ReturnType<typeof setTimeout> =>
          setTimeout(() => {
            pendingRef.current.delete(id);
            reject(new ApiError(408, "RPC timeout"));
          }, ms);

        // Extract conversation_id from chat frames so keepalive events
        // on the same conversation can reset this pending RPC's timer.
        const convId =
          typeof (frame as Record<string, unknown>).conversation_id === "string"
            ? ((frame as Record<string, unknown>).conversation_id as string)
            : undefined;

        pendingRef.current.set(id, {
          resolve: resolve as (data: unknown) => void,
          reject,
          timer: makeTimer(),
          timeoutMs: ms,
          conversationId: convId,
        });

        // If WS is open, send immediately. Otherwise the frame will be
        // sent when the connection opens (or timeout will fire).
        const ws = wsRef.current;
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ ...frame, id }));
        } else {
          // Queue: check periodically until connected or timeout
          const check = setInterval(() => {
            const w = wsRef.current;
            if (w && w.readyState === WebSocket.OPEN) {
              clearInterval(check);
              w.send(JSON.stringify({ ...frame, id }));
            }
            // If pending was already removed (timeout/cleanup), stop checking
            if (!pendingRef.current.has(id)) {
              clearInterval(check);
            }
          }, 100);
        }
      });
      return { ref: id, promise };
    },
    [],
  );

  const rpc = useCallback(
    <T = Record<string, unknown>>(
      frame: Omit<WsFrame, "id">,
      timeout?: number,
    ): Promise<T> => rpcWithRef<T>(frame, timeout).promise,
    [rpcWithRef],
  );

  const registerServerHandler = useCallback(
    (type: string, handler: ServerRpcHandler) => {
      serverHandlersRef.current.set(type, handler);
      return () => {
        if (serverHandlersRef.current.get(type) === handler) {
          serverHandlersRef.current.delete(type);
        }
      };
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
        // Only reject pending RPCs if the provider is unmounting.
        // During reconnects, pending RPCs will timeout naturally.
        if (disposed) {
          rejectAllPending(new ApiError(503, "Connection closed"));
        }
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

          // Server-initiated RPC: the server asked us to do something
          // and expects a reply frame keyed by our id. Dispatch to a
          // registered handler asynchronously so slow handlers don't
          // stall the message pump.
          const serverHandler = serverHandlersRef.current.get(type);
          if (serverHandler && frame.id) {
            const frameId: string = frame.id;
            (async () => {
              let payload: Record<string, unknown>;
              try {
                payload = await serverHandler(frame);
              } catch (err) {
                const message =
                  err instanceof Error ? err.message : String(err);
                payload = { ok: false, error: message };
              }
              const reply: Record<string, unknown> = {
                type: `${type}.result`,
                ref: frameId,
                ...payload,
              };
              const w = wsRef.current;
              if (w && w.readyState === WebSocket.OPEN) {
                w.send(JSON.stringify(reply));
              }
            })();
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

            // Keepalive: any stream/tool progress event for a
            // conversation resets the deadline on any in-flight RPC
            // for the same conversation. This lets a legitimately
            // long agentic turn (big code gen + multiple tool rounds)
            // run past the 10-minute backstop without falsely timing
            // out, as long as the backend is actively reporting
            // progress. If progress stops for the full budget, the
            // backstop kicks in.
            if (KEEPALIVE_EVENT_TYPES.has(event.event_type)) {
              const eventConvId =
                typeof event.data.conversation_id === "string"
                  ? event.data.conversation_id
                  : undefined;
              if (eventConvId) {
                for (const [pendingId, pending] of pendingRef.current) {
                  if (pending.conversationId !== eventConvId) continue;
                  clearTimeout(pending.timer);
                  pending.timer = setTimeout(() => {
                    pendingRef.current.delete(pendingId);
                    pending.reject(new ApiError(408, "RPC timeout"));
                  }, pending.timeoutMs);
                }
              }
            }

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
    <WebSocketContext.Provider
      value={{ subscribe, send, rpc, rpcWithRef, registerServerHandler, connected }}
    >
      {children}
    </WebSocketContext.Provider>
  );
}

export function useWebSocket(): WebSocketContextValue {
  return useContext(WebSocketContext);
}
