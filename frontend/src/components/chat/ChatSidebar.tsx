import { ScrollArea } from "@/components/ui/scroll-area";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { cn } from "@/lib/utils";
import type { ConversationSummary } from "@/types/chat";
import { MailIcon, MessageSquareIcon, UsersRoundIcon } from "lucide-react";

interface ChatSidebarProps {
  conversations: ConversationSummary[];
  activeId: string | null;
  currentUserId?: string;
  onSelect: (id: string) => void;
  onSelectInvite: (id: string) => void;
  onJoinRoom: (id: string) => void;
  onLeaveRoom: (id: string) => void;
  onRename: (id: string) => void;
  onDelete: (id: string) => void;
}

export function ChatSidebarContent({
  conversations,
  activeId,
  currentUserId,
  onSelect,
  onSelectInvite,
  onJoinRoom,
  onLeaveRoom,
  onRename,
  onDelete,
}: ChatSidebarProps) {
  const shared = conversations.filter((c) => c.shared);
  const personal = conversations.filter((c) => !c.shared);

  return (
    <ScrollArea className="h-full w-full">
      <div className="p-3 space-y-1">
        {/* Rooms section */}
        <div className="mb-3">
          <div className="flex items-center gap-1.5 px-2 mb-2">
            <UsersRoundIcon className="size-3.5 text-muted-foreground" />
            <h3 className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
              Rooms
            </h3>
          </div>
          {shared.length === 0 ? (
            <p className="px-2 text-xs text-muted-foreground/60">No rooms</p>
          ) : (
            shared.map((conv) => {
              const isMember = conv.is_member !== false;
              const isInvited = conv.is_invited === true;
              return (
                <div
                  key={conv.conversation_id}
                  className={cn(
                    "group flex items-center gap-2 rounded-lg px-2.5 py-1.5 text-sm cursor-pointer transition-colors hover:bg-accent min-w-0",
                    activeId === conv.conversation_id && "bg-accent",
                    isInvited && "bg-primary/5",
                  )}
                  onClick={() =>
                    isInvited
                      ? onSelectInvite(conv.conversation_id)
                      : isMember
                        ? onSelect(conv.conversation_id)
                        : onJoinRoom(conv.conversation_id)
                  }
                >
                  {isInvited && (
                    <MailIcon className="size-3.5 text-primary shrink-0" />
                  )}
                  <span className="flex-1 truncate">{conv.title}</span>
                  {conv.member_count !== undefined && !isInvited && (
                    <Badge variant="secondary" className="text-[10px] px-1.5">
                      {conv.member_count}
                    </Badge>
                  )}
                  {isInvited && (
                    <Badge variant="default" className="text-[10px]">
                      Invited
                    </Badge>
                  )}
                  {!isMember && !isInvited && (
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
            })
          )}
        </div>

        <Separator />

        {/* Chats section */}
        <div className="mt-3">
          <div className="flex items-center gap-1.5 px-2 mb-2">
            <MessageSquareIcon className="size-3.5 text-muted-foreground" />
            <h3 className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
              Chats
            </h3>
          </div>
          {personal.length === 0 ? (
            <p className="px-2 text-xs text-muted-foreground/60">No chats yet</p>
          ) : (
            personal.map((conv) => (
              <div
                key={conv.conversation_id}
                className={cn(
                  "group flex items-center gap-2 rounded-lg px-2.5 py-1.5 text-sm cursor-pointer transition-colors hover:bg-accent min-w-0",
                  activeId === conv.conversation_id && "bg-accent",
                )}
                onClick={() => onSelect(conv.conversation_id)}
              >
                <span className="flex-1 truncate">{conv.title}</span>
                <span className="hidden group-hover:inline-flex gap-1 shrink-0">
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
            ))
          )}
        </div>
      </div>
    </ScrollArea>
  );
}
