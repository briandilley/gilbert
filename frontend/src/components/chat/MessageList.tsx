import { useEffect, useRef } from "react";
import { TurnBubble } from "./TurnBubble";
import type { ChatTurn } from "@/types/chat";
import type { UIBlock } from "@/types/ui";
import { UIBlockRenderer } from "@/components/ui/UIBlockRenderer";

interface MessageListProps {
  turns: ChatTurn[];
  uiBlocks: UIBlock[];
  isShared: boolean;
  currentUserId?: string;
  onBlockSubmit: (blockId: string, values: Record<string, unknown>) => void;
}

export function MessageList({
  turns,
  uiBlocks,
  isShared,
  currentUserId,
  onBlockSubmit,
}: MessageListProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, uiBlocks]);

  // UI blocks are anchored by ``response_index`` which is the count of
  // visible (non-empty) assistant rows in the conversation. Map each
  // index onto a turn — turns without a final answer never get a
  // visible assistant row, so they don't consume a response_index.
  const visibleBlocks = uiBlocks.filter(
    (block) =>
      (!block.for_user || block.for_user === currentUserId) &&
      block.exclude_user !== currentUserId,
  );

  const blocksByTurnIndex = new Map<number, UIBlock[]>();
  const unanchored: UIBlock[] = [];

  // Walk turns in order, advancing a "visible assistant" counter for
  // every turn that has a final answer (== a visible bubble).
  let visibleAssistantIndex = 0;
  const assistantToTurnIndex = new Map<number, number>();
  for (let i = 0; i < turns.length; i++) {
    if (turns[i].final_content) {
      assistantToTurnIndex.set(visibleAssistantIndex, i);
      visibleAssistantIndex++;
    }
  }

  for (const block of visibleBlocks) {
    if (block.response_index != null) {
      const turnIdx = assistantToTurnIndex.get(block.response_index);
      if (turnIdx != null) {
        const list = blocksByTurnIndex.get(turnIdx) ?? [];
        list.push(block);
        blocksByTurnIndex.set(turnIdx, list);
        continue;
      }
    }
    unanchored.push(block);
  }

  return (
    <div
      ref={containerRef}
      className="flex-1 overflow-y-auto overflow-x-hidden overscroll-contain"
    >
      <div className="space-y-6 px-3 py-4 sm:px-4">
        {turns.map((turn, i) => (
          <div key={i}>
            <TurnBubble
              turn={turn}
              isShared={isShared}
              currentUserId={currentUserId}
            />
            {blocksByTurnIndex.get(i)?.map((block) => (
              <div key={block.block_id} className="max-w-md mx-auto mt-4">
                <UIBlockRenderer block={block} onSubmit={onBlockSubmit} />
              </div>
            ))}
          </div>
        ))}

        {unanchored.map((block) => (
          <div key={block.block_id} className="max-w-md mx-auto">
            <UIBlockRenderer block={block} onSubmit={onBlockSubmit} />
          </div>
        ))}

        <div ref={bottomRef} />
      </div>
    </div>
  );
}
