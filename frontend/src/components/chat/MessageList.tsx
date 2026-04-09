import { useEffect, useRef } from "react";
import { MessageBubble } from "./MessageBubble";
import type { ChatMessage } from "@/types/chat";
import type { UIBlock } from "@/types/ui";
import { UIBlockRenderer } from "@/components/ui/UIBlockRenderer";

interface MessageListProps {
  messages: ChatMessage[];
  uiBlocks: UIBlock[];
  isShared: boolean;
  currentUserId?: string;
  onBlockSubmit: (blockId: string, values: Record<string, unknown>) => void;
}

export function MessageList({
  messages,
  uiBlocks,
  isShared,
  currentUserId,
  onBlockSubmit,
}: MessageListProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, uiBlocks]);

  return (
    <div ref={containerRef} className="flex-1 overflow-y-auto overscroll-contain">
      <div className="space-y-4 px-3 py-4 sm:px-4">
        {messages.map((msg, i) => (
          <MessageBubble
            key={i}
            message={msg}
            isShared={isShared}
            currentUserId={currentUserId}
          />
        ))}

        {uiBlocks
          .filter((block) =>
            (!block.for_user || block.for_user === currentUserId) &&
            block.exclude_user !== currentUserId
          )
          .map((block) => (
            <div key={block.block_id} className="max-w-md mx-auto">
              <UIBlockRenderer block={block} onSubmit={onBlockSubmit} />
            </div>
          ))}

        <div ref={bottomRef} />
      </div>
    </div>
  );
}
