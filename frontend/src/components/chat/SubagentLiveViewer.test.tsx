import React from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ConversationDetail } from "@/types/chat";
import type { GilbertEvent } from "@/types/events";

// Mock Dialog components — base-ui portals hang in jsdom; wrap children in divs
vi.mock("@/components/ui/dialog", () => ({
  Dialog: ({ children, open }: { children: React.ReactNode; open?: boolean }) =>
    open ? <div data-testid="dialog">{children}</div> : null,
  DialogContent: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
  DialogHeader: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
  DialogTitle: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
}));

// Use a stable api object to avoid infinite re-render from unstable useWsApi ref
const mockLoadConversation = vi.fn();
const stableApi = { loadConversation: mockLoadConversation };
vi.mock("@/hooks/useWsApi", () => ({
  useWsApi: () => stableApi,
}));

// Capture event-bus handlers so tests can fire events and assert scoping.
const handlers = new Map<string, (e: GilbertEvent) => void>();
vi.mock("@/hooks/useEventBus", () => ({
  useEventBus: (type: string, handler: (e: GilbertEvent) => void) => {
    handlers.set(type, handler);
  },
}));

function fireDelta(conversation_id: string, text: string) {
  act(() => {
    handlers.get("chat.stream.text_delta")?.({
      event_type: "chat.stream.text_delta",
      data: { conversation_id, text },
      source: "ai",
      timestamp: "",
    });
  });
}

// Mock MessageList with minimal output
vi.mock("./MessageList", () => ({
  MessageList: ({ turns }: { turns: Array<{ final_content: string }> }) => (
    <ul data-testid="message-list">
      {turns.map((t, i) => (
        <li key={i}>{t.final_content}</li>
      ))}
    </ul>
  ),
}));

import { SubagentLiveViewer } from "./SubagentLiveViewer";

describe("SubagentLiveViewer", () => {
  it("loads and displays subagent conversation turns", async () => {
    const fakeConv: ConversationDetail = {
      conversation_id: "sub-1",
      title: "Subagent",
      turns: [
        {
          user_message: { content: "go", attachments: [] },
          rounds: [],
          final_content: "working on it...",
          final_attachments: [],
          streaming: false,
          incomplete: false,
          interrupted: false,
        } as unknown as import("@/types/chat").ChatTurn,
      ],
      ui_blocks: [],
      updated_at: "",
      shared: false,
    };
    mockLoadConversation.mockResolvedValue(fakeConv);

    render(
      <SubagentLiveViewer open conversationId="sub-1" onClose={() => {}} />,
    );

    await waitFor(() =>
      expect(screen.getByText(/working on it/i)).toBeInTheDocument(),
    );
  });

  it("streams live text for the watched conversation and ignores others", async () => {
    handlers.clear();
    mockLoadConversation.mockResolvedValue({
      conversation_id: "sub-1",
      title: "Subagent",
      turns: [],
      ui_blocks: [],
      updated_at: "",
      shared: false,
    } as ConversationDetail);

    render(<SubagentLiveViewer open conversationId="sub-1" onClose={() => {}} />);
    await waitFor(() => expect(handlers.has("chat.stream.text_delta")).toBe(true));

    // An event for a DIFFERENT conversation must be ignored.
    fireDelta("other-conv", "leak");
    expect(screen.queryByText(/leak/)).not.toBeInTheDocument();

    // An event for the watched conversation streams in (bootstraps a turn).
    fireDelta("sub-1", "live tokens");
    expect(screen.getByText(/live tokens/)).toBeInTheDocument();
  });
});
