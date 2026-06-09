import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { GilbertEvent } from "@/types/events";
import { useActiveSubagents } from "./useActiveSubagents";

// Capture event-bus handlers so tests can fire events synchronously.
const handlers = new Map<string, (e: GilbertEvent) => void>();
vi.mock("@/hooks/useEventBus", () => ({
  useEventBus: (type: string, handler: (e: GilbertEvent) => void) => {
    handlers.set(type, handler);
  },
}));

beforeEach(() => {
  handlers.clear();
});

function fire(event_type: string, data: Record<string, unknown>) {
  act(() => {
    handlers.get(event_type)?.({ event_type, data, source: "subagent", timestamp: "" });
  });
}

describe("useActiveSubagents", () => {
  it("tracks running subagents for the active conversation, ignores others", () => {
    const { result } = renderHook(() => useActiveSubagents("conv-1"));
    expect(result.current).toEqual([]);

    fire("chat.stream.subagent_started", {
      conversation_id: "conv-1",
      subagent_id: "a1",
      agent_type: "general-purpose",
    });
    fire("chat.stream.subagent_started", {
      conversation_id: "other",
      subagent_id: "b1",
      agent_type: "general-purpose",
    });

    expect(result.current).toHaveLength(1);
    expect(result.current[0]).toMatchObject({
      subagent_id: "a1",
      agent_type: "general-purpose",
      status: "running",
    });
  });

  it("marks a subagent completed", () => {
    const { result } = renderHook(() => useActiveSubagents("conv-1"));
    fire("chat.stream.subagent_started", {
      conversation_id: "conv-1",
      subagent_id: "a1",
      agent_type: "general-purpose",
    });
    fire("chat.stream.subagent_completed", {
      conversation_id: "conv-1",
      subagent_id: "a1",
      agent_type: "general-purpose",
    });
    expect(result.current[0].status).toBe("completed");
  });

  it("marks a subagent failed with a reason", () => {
    const { result } = renderHook(() => useActiveSubagents("conv-1"));
    fire("chat.stream.subagent_started", {
      conversation_id: "conv-1",
      subagent_id: "a1",
      agent_type: "general-purpose",
    });
    fire("chat.stream.subagent_failed", {
      conversation_id: "conv-1",
      subagent_id: "a1",
      agent_type: "general-purpose",
      reason: "boom",
    });
    expect(result.current[0].status).toBe("failed");
    expect(result.current[0].reason).toBe("boom");
  });
});
