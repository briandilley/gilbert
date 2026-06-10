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
      subagent_conversation_id: "sub-conv-1",
      query: "what is X?",
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
      conversationId: "sub-conv-1",
      query: "what is X?",
    });
  });

  it("removes a subagent when completed (card disappears, delivered message takes over)", () => {
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
    // Terminal event removes the run — card disappears, message stands in.
    expect(result.current).toHaveLength(0);
  });

  it("removes a subagent when stopped", () => {
    const { result } = renderHook(() => useActiveSubagents("conv-1"));
    fire("chat.stream.subagent_started", {
      conversation_id: "conv-1",
      subagent_id: "a1",
      agent_type: "general-purpose",
    });
    fire("chat.stream.subagent_stopped", {
      conversation_id: "conv-1",
      subagent_id: "a1",
    });
    expect(result.current).toHaveLength(0);
  });

  it("removes a subagent when failed", () => {
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
    expect(result.current).toHaveLength(0);
  });
});
