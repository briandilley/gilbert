import { ScrollArea } from "@/components/ui/scroll-area";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { ConversationSummary } from "@/types/chat";

interface ChatSidebarProps {
  conversations: ConversationSummary[];
  activeId: string | null;
  currentUserId?: string;
  onSelect: (id: string) => void;
  onJoinRoom: (id: string) => void;
  onLeaveRoom: (id: string) => void;
  onRename: (id: string) => void;
  onDelete: (id: string) => void;
}

/**
 * Sidebar conversation list — rendered both inline (desktop)
 * and inside a Sheet (mobile).
 */
export function ChatSidebarContent({
  conversations,
  activeId,
  currentUserId,
  onSelect,
  onJoinRoom,
  onLeaveRoom,
  onRename,
  onDelete,
}: ChatSidebarProps) {
  const shared = conversations.filter((c) => c.shared);
  const personal = conversations.filter((c) => !c.shared);

  return (
    <ScrollArea className="h-full">
      {shared.length > 0 && (
        <div className="px-3 pt-3 pb-2">
          <h3 className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground mb-1.5 px-2">
            Rooms
          </h3>
          {shared.map((conv) => {
            const isMember = conv.is_member !== false;
            return (
              <div
                key={conv.conversation_id}
                className={cn(
                  "group flex items-center gap-2 rounded-lg px-2.5 py-2 text-sm cursor-pointer transition-colors hover:bg-accent min-w-0",
                  activeId === conv.conversation_id && "bg-accent",
                )}
                onClick={() =>
                  isMember
                    ? onSelect(conv.conversation_id)
                    : onJoinRoom(conv.conversation_id)
                }
              >
                <span className="flex-1 truncate">{conv.title}</span>
                {conv.member_count !== undefined && (
                  <Badge variant="secondary" className="text-[10px] px-1.5">
                    {conv.member_count}
                  </Badge>
                )}
                {!isMember && (
                  <Badge variant="outline" className="text-[10px]">
                    Join
                  </Badge>
                )}
                {isMember && (
                  <button
                    className="hidden text-muted-foreground hover:text-destructive group-hover:inline text-xs"
                    onClick={(e) => {
                      e.stopPropagation();
                      onLeaveRoom(conv.conversation_id);
                    }}
                  >
                    Leave
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}

      {personal.length > 0 && (
        <div className="px-3 pt-3 pb-2">
          <h3 className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground mb-1.5 px-2">
            Chats
          </h3>
          {personal.map((conv) => (
            <div
              key={conv.conversation_id}
              className={cn(
                "group flex items-center gap-2 rounded-lg px-2.5 py-2 text-sm cursor-pointer transition-colors hover:bg-accent min-w-0",
                activeId === conv.conversation_id && "bg-accent",
              )}
              onClick={() => onSelect(conv.conversation_id)}
            >
              <span className="flex-1 truncate">{conv.title}</span>
              <span className="hidden group-hover:inline-flex gap-1">
                <button
                  className="text-muted-foreground hover:text-foreground text-xs"
                  onClick={(e) => {
                    e.stopPropagation();
                    onRename(conv.conversation_id);
                  }}
                >
                  Rename
                </button>
                <button
                  className="text-muted-foreground hover:text-destructive text-xs"
                  onClick={(e) => {
                    e.stopPropagation();
                    onDelete(conv.conversation_id);
                  }}
                >
                  Delete
                </button>
              </span>
            </div>
          ))}
        </div>
      )}

      {shared.length === 0 && personal.length === 0 && (
        <div className="flex items-center justify-center h-32 text-sm text-muted-foreground">
          No conversations yet
        </div>
      )}
    </ScrollArea>
  );
}
