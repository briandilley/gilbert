import { act, renderHook, waitFor } from "@testing-library/react";
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

// Mock useWsApi to control listSubagents responses.
let mockListSubagents: (convId: string) => Promise<{ runs: Array<{ subagent_id: string; agent_type: string; query: string; conversation_id: string; status: string }> }>;

vi.mock("@/hooks/useWsApi", () => ({
  useWsApi: () => ({
    listSubagents: (convId: string) => mockListSubagents(convId),
  }),
}));

beforeEach(() => {
  handlers.clear();
  // Default: return no runs.
  mockListSubagents = () => Promise.resolve({ runs: [] });
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

  it("re-seeds running runs from listSubagents when activeConversationId changes", async () => {
    mockListSubagents = (convId) => {
      if (convId !== "conv-reseed") return Promise.resolve({ runs: [] });
      return Promise.resolve({
        runs: [
          {
            subagent_id: "seeded-1",
            agent_type: "deep-research",
            query: "something",
            conversation_id: "sub-conv-seeded",
            status: "running",
          },
        ],
      });
    };

    const { result, rerender } = renderHook(
      ({ id }: { id: string | null }) => useActiveSubagents(id),
      { initialProps: { id: null as string | null } },
    );

    rerender({ id: "conv-reseed" });

    await waitFor(() => {
      expect(result.current).toHaveLength(1);
    });

    expect(result.current[0]).toMatchObject({
      subagent_id: "seeded-1",
      agent_type: "deep-research",
      query: "something",
      conversationId: "sub-conv-seeded",
      status: "running",
    });
  });

  it("ignores stale listSubagents response when conversation changes before RPC returns", async () => {
    let resolveStale!: (v: { runs: Array<{ subagent_id: string; agent_type: string; query: string; conversation_id: string; status: string }> }) => void;

    mockListSubagents = (convId) => {
      if (convId === "stale-conv") {
        return new Promise((res) => { resolveStale = res; });
      }
      return Promise.resolve({ runs: [] });
    };

    const { result, rerender } = renderHook(
      ({ id }: { id: string | null }) => useActiveSubagents(id),
      { initialProps: { id: null as string | null } },
    );

    rerender({ id: "stale-conv" });
    // Switch away before the RPC resolves.
    rerender({ id: "new-conv" });

    // Now resolve the stale RPC with a run.
    act(() => {
      resolveStale({
        runs: [{ subagent_id: "stale-run", agent_type: "dr", query: "q", conversation_id: "c", status: "running" }],
      });
    });

    await act(async () => {
      await Promise.resolve();
    });

    // Stale result must not be merged in.
    expect(result.current).toHaveLength(0);
  });
});
