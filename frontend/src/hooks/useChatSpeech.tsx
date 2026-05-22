import { useCallback, useEffect, useMemo, useState } from "react";
import { useWebSocket } from "@/hooks/useWebSocket";
import { useBrowserSpeaker } from "@/hooks/useBrowserSpeaker";
import type { GilbertEvent } from "@/types/events";

interface ChatSpeechStore {
  /** Per-(user, conv) read-aloud preference, mirrored from the server. */
  enabled: boolean;
  /** True while a chat-speech clip is actively playing for THIS conv. */
  isSpeaking: boolean;
  /** Toggle the preference; auto-activates the browser speaker on enable. */
  toggle: () => Promise<void>;
}

export function useChatSpeech(conversationId: string | null): ChatSpeechStore {
  const { connected, rpc, subscribe } = useWebSocket();
  const browser = useBrowserSpeaker();
  const [enabled, setEnabled] = useState(false);

  // Hydrate on mount / when conversation changes.
  useEffect(() => {
    if (!conversationId || !connected) {
      setEnabled(false);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const res = (await rpc({
          type: "chat.read_aloud.get",
          conversation_id: conversationId,
        })) as { enabled?: boolean };
        if (!cancelled) setEnabled(Boolean(res?.enabled));
      } catch {
        if (!cancelled) setEnabled(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [conversationId, connected, rpc]);

  // Listen for server-side changes (other tabs of the same user).
  useEffect(() => {
    if (!conversationId) return;
    return subscribe("chat.read_aloud.changed", (event: GilbertEvent) => {
      const data = event.data as Record<string, unknown>;
      if (data.conversation_id !== conversationId) return;
      setEnabled(Boolean(data.enabled));
    });
  }, [conversationId, subscribe]);

  const toggle = useCallback(async () => {
    if (!conversationId) return;
    const next = !enabled;
    if (next && !browser.enabled) {
      // Auto-activate the browser speaker so the audio has somewhere
      // to play. setEnabled fires the activate WS frame.
      browser.setEnabled(true);
    }
    setEnabled(next); // optimistic
    try {
      await rpc({
        type: "chat.read_aloud.set",
        conversation_id: conversationId,
        enabled: next,
      });
    } catch {
      setEnabled(!next); // rollback
    }
  }, [conversationId, enabled, browser, rpc]);

  const isSpeaking = useMemo(() => {
    if (!enabled || !browser.isPlaying) return false;
    const last = browser.lastPlayed;
    if (!last) return false;
    return (
      last.kind === "chat_speech" && last.conversationId === conversationId
    );
  }, [enabled, browser.isPlaying, browser.lastPlayed, conversationId]);

  return { enabled, isSpeaking, toggle };
}
