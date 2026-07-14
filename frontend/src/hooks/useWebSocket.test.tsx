import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WebSocketProvider, useWebSocket } from "./useWebSocket";

// The provider only connects once a user is present. The user object must be
// referentially stable — the connect effect keys on it, and a fresh object per
// render would tear down and reopen the socket on every state change.
const MOCK_USER = { user_id: "u1", name: "Test", role: "admin" };
vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({
    user: MOCK_USER,
    loading: false,
    refresh: () => Promise.resolve(),
    logout: () => Promise.resolve(),
  }),
}));

/** Minimal WebSocket stand-in: records instances so tests can drive
 *  ``onopen`` / ``onmessage`` by hand. */
class MockWebSocket {
  static OPEN = 1;
  static instances: MockWebSocket[] = [];
  readyState = MockWebSocket.OPEN;
  url: string;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((msg: { data: string }) => void) | null = null;
  sent: string[] = [];
  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }
  send(data: string): void {
    this.sent.push(data);
  }
  close(): void {}
}

beforeEach(() => {
  MockWebSocket.instances = [];
  vi.stubGlobal("WebSocket", MockWebSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function wrapper({ children }: { children: ReactNode }) {
  return <WebSocketProvider>{children}</WebSocketProvider>;
}

/** Renders the provider, opens the mock socket, and returns both the hook
 *  result and a helper that injects a server frame through ``onmessage``. */
function setup() {
  const rendered = renderHook(() => useWebSocket(), { wrapper });
  const ws = MockWebSocket.instances.at(-1);
  if (!ws) throw new Error("WebSocketProvider did not open a socket");
  act(() => ws.onopen?.());
  const inject = (frame: Record<string, unknown>): void => {
    act(() => ws.onmessage?.({ data: JSON.stringify(frame) }));
  };
  return { ...rendered, ws, inject };
}

describe("useWebSocket onmessage dispatch", () => {
  it("delivers direct server-pushed frames to exact-type subscribers, raw", () => {
    const { result, inject } = setup();
    const onMafiaState = vi.fn();
    act(() => {
      result.current.subscribe("mafia.state", onMafiaState);
    });

    inject({ type: "mafia.state", game_id: "g1", state: { phase: "day" } });

    expect(onMafiaState).toHaveBeenCalledTimes(1);
    expect(onMafiaState).toHaveBeenCalledWith(
      expect.objectContaining({
        type: "mafia.state",
        game_id: "g1",
        state: { phase: "day" },
      }),
    );
  });

  it("does not deliver direct frames to the '*' wildcard subscriber", () => {
    const { result, inject } = setup();
    const wildcard = vi.fn();
    act(() => {
      result.current.subscribe("*", wildcard);
    });

    inject({ type: "mafia.state", game_id: "g1", state: {} });

    expect(wildcard).not.toHaveBeenCalled();
  });

  it("still dispatches gilbert.event bus events (exact + wildcard), not raw", () => {
    const { result, inject } = setup();
    const onEvent = vi.fn();
    const wildcard = vi.fn();
    act(() => {
      result.current.subscribe("foo.changed", onEvent);
      result.current.subscribe("*", wildcard);
    });

    inject({
      type: "gilbert.event",
      event_type: "foo.changed",
      data: { x: 1 },
      source: "test",
      timestamp: "now",
    });

    const busShape = expect.objectContaining({
      event_type: "foo.changed",
      data: { x: 1 },
      source: "test",
      timestamp: "now",
    });
    expect(onEvent).toHaveBeenCalledTimes(1);
    expect(onEvent).toHaveBeenCalledWith(busShape);
    expect(wildcard).toHaveBeenCalledTimes(1);
    expect(wildcard).toHaveBeenCalledWith(busShape);
    // A bus event never double-fires a subscriber registered for the
    // literal frame type "gilbert.event" via the raw fall-through.
    expect(onEvent.mock.calls[0][0]).not.toHaveProperty("type");
  });

  it("routes RPC replies to the pending promise, not to subscribers", async () => {
    const { result, inject, ws } = setup();
    const onReply = vi.fn();
    let promise: Promise<{ ok: boolean }> = Promise.resolve({ ok: false });
    act(() => {
      result.current.subscribe("mafia.game.resume.result", onReply);
      promise = result.current.rpc<{ ok: boolean }>({ type: "mafia.game.resume" });
    });

    const sentFrame = JSON.parse(ws.sent.at(-1) ?? "{}") as { id: string };
    inject({ type: "mafia.game.resume.result", ref: sentFrame.id, ok: true });

    await expect(promise).resolves.toEqual(expect.objectContaining({ ok: true }));
    expect(onReply).not.toHaveBeenCalled();
  });
});
