import { useCallback, useEffect, useRef, useState } from "react";
import { useEventBus } from "@/hooks/useEventBus";
import { useWsApi } from "@/hooks/useWsApi";
import type { ActiveSubagent, GilbertEvent } from "@/types/events";

/**
 * Tracks subagents spawned during the active conversation's turns, live.
 * Subscribes to the chat.stream.subagent_* events and returns the current
 * list of running subagents. Terminal events (completed/stopped/failed)
 * remove the entry — the delivered message in the conversation stands in.
 * Events for other conversations are ignored.
 *
 * On conversation open (``activeConversationId`` changes), the hook re-seeds
 * running subagent cards from the ``subagent.list`` RPC so they survive
 * navigation away from and back to the conversation.
 */
export function useActiveSubagents(activeConversationId: string | null): ActiveSubagent[] {
  const [byId, setById] = useState<Record<string, ActiveSubagent>>({});
  const api = useWsApi();
  // Track the latest conversation id to guard against stale RPC responses.
  const latestConvRef = useRef<string | null>(null);

  // Re-seed running cards whenever the active conversation changes.
  useEffect(() => {
    latestConvRef.current = activeConversationId;
    // Clear immediately on EVERY navigation so another conversation's cards
    // (added via live events) can't linger into this one.
    setById({});
    if (!activeConversationId) return;
    const convId = activeConversationId;
    api.listSubagents(convId).then((result) => {
      // Ignore stale responses if the conversation changed while awaiting.
      if (latestConvRef.current !== convId) return;
      const next: Record<string, ActiveSubagent> = {};
      for (const run of result.runs) {
        if (run.status !== "running") continue;
        next[run.subagent_id] = {
          subagent_id: run.subagent_id,
          agent_type: run.agent_type,
          status: "running",
          conversationId: run.conversation_id,
          query: run.query,
        };
      }
      setById(next);
    }).catch(() => {
      // Best-effort — a failed RPC just means no re-seeding this navigation.
    });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeConversationId]);

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

  // ``onTerminal`` is already a stable useCallback ref — reuse it directly for
  // every terminal status (completed/failed/stopped all drop the run).
  useEventBus("chat.stream.subagent_started", onStarted);
  useEventBus("chat.stream.subagent_completed", onTerminal);
  useEventBus("chat.stream.subagent_failed", onTerminal);
  useEventBus("chat.stream.subagent_stopped", onTerminal);

  return Object.values(byId);
}
