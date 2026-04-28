import { useEffect, useLayoutEffect, useRef } from "react";
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

// How close to the bottom (in CSS pixels) we consider the user
// "anchored". Inside this window we keep auto-scrolling to the new
// bottom; outside it we leave the scroll position alone so we don't
// yank a reader who's scrolled up to look at history.
const ANCHOR_THRESHOLD_PX = 80;

export function MessageList({
  turns,
  uiBlocks,
  isShared,
  currentUserId,
  onBlockSubmit,
}: MessageListProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const innerRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  // Tracks "is the user currently parked at the bottom?". Held in a
  // ref (not state) because the scroll/resize handlers need the live
  // value without triggering re-renders.
  const isAnchoredRef = useRef(true);

  // Scroll listener — recompute anchor status on every scroll. Cheap;
  // ``passive`` keeps it off the input pipeline.
  useEffect(() => {
    const c = containerRef.current;
    if (!c) return;
    const handler = () => {
      const distance = c.scrollHeight - c.scrollTop - c.clientHeight;
      isAnchoredRef.current = distance < ANCHOR_THRESHOLD_PX;
    };
    c.addEventListener("scroll", handler, { passive: true });
    return () => c.removeEventListener("scroll", handler);
  }, []);

  // Snap to bottom whenever turns/blocks change. ``useLayoutEffect``
  // runs synchronously after DOM layout but before paint, so the
  // scroll position is computed against the just-rendered DOM rather
  // than a stale one. ``behavior: "auto"`` jumps instantly — smooth
  // scroll undershoots when content (e.g. UI block forms) is still
  // expanding while the animation is mid-flight.
  useLayoutEffect(() => {
    if (!isAnchoredRef.current) return;
    bottomRef.current?.scrollIntoView({ behavior: "auto", block: "end" });
  }, [turns, uiBlocks]);

  // Late layout shifts — async image loads, UI block controls that
  // take a tick to lay out their inputs, custom fonts settling — all
  // happen after the prop-change effect has already fired. Watch the
  // inner content for size changes and re-pin to the bottom while the
  // user is still anchored.
  useEffect(() => {
    const inner = innerRef.current;
    if (!inner) return;
    const ro = new ResizeObserver(() => {
      if (isAnchoredRef.current) {
        bottomRef.current?.scrollIntoView({ behavior: "auto", block: "end" });
      }
    });
    ro.observe(inner);
    return () => ro.disconnect();
  }, []);

  // UI blocks are anchored by ``response_index`` which the backend
  // sets to the 0-based turn index (== count of user messages minus
  // one at the time the block was produced). Since we render one
  // ``TurnBubble`` per turn, response_index maps 1:1 onto the turn
  // array index — no extra bookkeeping required.
  const visibleBlocks = uiBlocks.filter(
    (block) =>
      (!block.for_user || block.for_user === currentUserId) &&
      block.exclude_user !== currentUserId,
  );

  const blocksByTurnIndex = new Map<number, UIBlock[]>();
  const unanchored: UIBlock[] = [];

  for (const block of visibleBlocks) {
    if (
      block.response_index != null &&
      block.response_index >= 0 &&
      block.response_index < turns.length
    ) {
      const turnIdx = block.response_index;
      const list = blocksByTurnIndex.get(turnIdx) ?? [];
      list.push(block);
      blocksByTurnIndex.set(turnIdx, list);
      continue;
    }
    unanchored.push(block);
  }

  return (
    <div
      ref={containerRef}
      className="flex-1 overflow-y-auto overflow-x-hidden overscroll-contain"
    >
      <div ref={innerRef} className="space-y-6 px-3 py-4 sm:px-4">
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
