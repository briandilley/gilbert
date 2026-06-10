import { useCallback, useEffect, useState } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { MessageList } from "./MessageList";
import { useWsApi } from "@/hooks/useWsApi";
import { useEventBus } from "@/hooks/useEventBus";
import type { ChatTurn } from "@/types/chat";
import type { GilbertEvent } from "@/types/events";

/**
 * Read-only live view of a subagent's conversation. Opens the subagent's
 * persisted conversation and subscribes to live streaming events scoped to
 * that conversation id only — so scrolling back in the main chat while a
 * subagent runs doesn't mix up the two streams.
 */
export function SubagentLiveViewer({
  open,
  conversationId,
  onClose,
}: {
  open: boolean;
  conversationId: string;
  onClose: () => void;
}) {
  const api = useWsApi();
  const [turns, setTurns] = useState<ChatTurn[]>([]);

  // Load the conversation when the dialog opens.
  // NOTE: `api` is intentionally excluded from the dependency array — it is a
  // stable object whose identity must not be used to trigger re-fetches.
  // Re-run only when open/conversationId changes.
  useEffect(() => {
    if (!open || !conversationId) return;
    let cancelled = false;
    setTurns([]);
    api
      .loadConversation(conversationId)
      .then((detail) => {
        if (!cancelled) setTurns(detail.turns);
      })
      .catch(() => {
        // Best-effort: subagent conv may not exist yet on first open.
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, conversationId]);

  // Live-update: text deltas — only act on events for THIS subagent conv.
  const handleTextDelta = useCallback(
    (event: GilbertEvent) => {
      const d = event.data as Record<string, unknown>;
      if (d.conversation_id !== conversationId) return;
      const text = String(d.text || "");
      if (!text) return;
      setTurns((prev) => {
        // Opened mid-stream with no persisted turn yet? Bootstrap a streaming
        // turn so live text shows instead of being dropped.
        if (prev.length === 0) {
          return [
            {
              user_message: { content: "", attachments: [] },
              rounds: [],
              final_content: text,
              final_attachments: [],
              incomplete: false,
              streaming: true,
            } as ChatTurn,
          ];
        }
        const last = prev[prev.length - 1];
        return [
          ...prev.slice(0, -1),
          { ...last, final_content: last.final_content + text, streaming: true },
        ];
      });
    },
    [conversationId],
  );

  // Round complete — mark streaming done for current turn.
  const handleRoundComplete = useCallback(
    (event: GilbertEvent) => {
      const d = event.data as Record<string, unknown>;
      if (d.conversation_id !== conversationId) return;
      setTurns((prev) => {
        if (prev.length === 0) return prev;
        const last = prev[prev.length - 1];
        return [...prev.slice(0, -1), { ...last, streaming: false }];
      });
    },
    [conversationId],
  );

  useEventBus("chat.stream.text_delta", handleTextDelta);
  useEventBus("chat.stream.round_complete", handleRoundComplete);

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-3xl max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Subagent activity</DialogTitle>
        </DialogHeader>
        <MessageList
          turns={turns}
          uiBlocks={[]}
          isShared={false}
          onBlockSubmit={() => {}}
        />
      </DialogContent>
    </Dialog>
  );
}
