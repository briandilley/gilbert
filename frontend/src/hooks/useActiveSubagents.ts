import { useCallback, useState } from "react";
import { useEventBus } from "@/hooks/useEventBus";
import type { ActiveSubagent, GilbertEvent, SubagentStatus } from "@/types/events";

/**
 * Tracks subagents spawned during the active conversation's turns, live.
 * Subscribes to the chat.stream.subagent_* events and returns the current
 * list (running first by arrival). Events for other conversations are ignored.
 */
export function useActiveSubagents(activeConversationId: string | null): ActiveSubagent[] {
  const [byId, setById] = useState<Record<string, ActiveSubagent>>({});

  const upsert = useCallback(
    (status: SubagentStatus) => (event: GilbertEvent) => {
      const d = event.data as Record<string, unknown>;
      if (d.conversation_id !== activeConversationId) return;
      const id = String(d.subagent_id || "");
      if (!id) return;
      setById((prev) => ({
        ...prev,
        [id]: {
          subagent_id: id,
          agent_type: String(d.agent_type || "agent"),
          status,
          reason: typeof d.reason === "string" ? d.reason : prev[id]?.reason,
        },
      }));
    },
    [activeConversationId],
  );

  useEventBus("chat.stream.subagent_started", upsert("running"));
  useEventBus("chat.stream.subagent_completed", upsert("completed"));
  useEventBus("chat.stream.subagent_failed", upsert("failed"));

  return Object.values(byId);
}
