import { useCallback, useMemo, useState } from "react";
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

  // Memoize the per-status handlers so their references are stable across
  // renders (changing only when activeConversationId changes). Passing a fresh
  // closure to useEventBus every render would churn subscribe/unsubscribe and
  // could drop an event that arrives mid-teardown.
  const onStarted = useMemo(() => upsert("running"), [upsert]);
  const onCompleted = useMemo(() => upsert("completed"), [upsert]);
  const onFailed = useMemo(() => upsert("failed"), [upsert]);
  useEventBus("chat.stream.subagent_started", onStarted);
  useEventBus("chat.stream.subagent_completed", onCompleted);
  useEventBus("chat.stream.subagent_failed", onFailed);

  return Object.values(byId);
}
