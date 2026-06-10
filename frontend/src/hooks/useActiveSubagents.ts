import { useCallback, useMemo, useState } from "react";
import { useEventBus } from "@/hooks/useEventBus";
import type { ActiveSubagent, GilbertEvent } from "@/types/events";

/**
 * Tracks subagents spawned during the active conversation's turns, live.
 * Subscribes to the chat.stream.subagent_* events and returns the current
 * list of running subagents. Terminal events (completed/stopped/failed)
 * remove the entry — the delivered message in the conversation stands in.
 * Events for other conversations are ignored.
 */
export function useActiveSubagents(activeConversationId: string | null): ActiveSubagent[] {
  const [byId, setById] = useState<Record<string, ActiveSubagent>>({});

  const onStarted = useCallback(
    (event: GilbertEvent) => {
      const d = event.data as Record<string, unknown>;
      if (d.conversation_id !== activeConversationId) return;
      const id = String(d.subagent_id || "");
      if (!id) return;
      setById((prev) => ({
        ...prev,
        [id]: {
          subagent_id: id,
          agent_type: String(d.agent_type || "agent"),
          status: "running",
          conversationId: String(d.subagent_conversation_id || ""),
          query: String(d.query || ""),
        },
      }));
    },
    [activeConversationId],
  );

  const onTerminal = useCallback(
    (event: GilbertEvent) => {
      const d = event.data as Record<string, unknown>;
      if (d.conversation_id !== activeConversationId) return;
      const id = String(d.subagent_id || "");
      if (!id) return;
      // Remove the run — the delivered message takes over.
      setById((prev) => {
        const { [id]: _, ...rest } = prev;
        return rest;
      });
    },
    [activeConversationId],
  );

  // Memoize handlers so their references are stable across renders.
  const onCompleted = useMemo(() => onTerminal, [onTerminal]);
  const onFailed = useMemo(() => onTerminal, [onTerminal]);
  const onStopped = useMemo(() => onTerminal, [onTerminal]);

  useEventBus("chat.stream.subagent_started", onStarted);
  useEventBus("chat.stream.subagent_completed", onCompleted);
  useEventBus("chat.stream.subagent_failed", onFailed);
  useEventBus("chat.stream.subagent_stopped", onStopped);

  return Object.values(byId);
}
