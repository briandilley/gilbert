import type { ConversationMember } from "@/types/chat";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { useIsUserOnline } from "@/hooks/usePresence";
import { cn } from "@/lib/utils";

interface MemberPanelProps {
  members: ConversationMember[];
  ownerId?: string;
  currentUserId?: string;
  onKick: (userId: string) => void;
}

export function MemberPanelContent({
  members,
  ownerId,
  currentUserId,
  onKick,
}: MemberPanelProps) {
  const isOwner = currentUserId === ownerId;

  return (
    <div className="p-3">
      <h3 className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground mb-3 px-1">
        Members ({members.length})
      </h3>
      <div className="space-y-1">
        {members.map((m) => (
          <MemberRow
            key={m.user_id}
            member={m}
            isOwnerRow={m.user_id === ownerId}
            isSelf={m.user_id === currentUserId}
            showKick={isOwner && m.user_id !== currentUserId}
            onKick={onKick}
          />
        ))}
      </div>
    </div>
  );
}

/** Single row — broken out so the ``useIsUserOnline`` hook subscribes
 *  per member instead of the whole panel re-rendering on every
 *  online/offline transition. */
function MemberRow({
  member,
  isOwnerRow,
  isSelf,
  showKick,
  onKick,
}: {
  member: ConversationMember;
  isOwnerRow: boolean;
  isSelf: boolean;
  showKick: boolean;
  onKick: (userId: string) => void;
}) {
  const online = useIsUserOnline(member.user_id);
  return (
    <div className="group flex items-center gap-2.5 rounded-lg px-2 py-1.5">
      <span className="relative inline-flex shrink-0">
        <Avatar className="size-6">
          <AvatarFallback className="text-[10px]">
            {member.display_name.charAt(0).toUpperCase()}
          </AvatarFallback>
        </Avatar>
        {/* Online dot — bottom-right of the avatar. ``ring-background``
            cuts a notch so the dot reads cleanly against the avatar
            edge regardless of theme. Aria-label drives screen reader
            output without polluting the visible UI. */}
        <span
          aria-label={online ? `${member.display_name} is online` : undefined}
          className={cn(
            "absolute -right-0.5 -bottom-0.5 size-2 rounded-full ring-2 ring-background transition-colors",
            online ? "bg-emerald-500" : "bg-transparent",
          )}
        />
      </span>
      <span className="flex-1 truncate text-sm">
        {member.display_name}
        {isSelf && (
          <span className="ml-1 text-muted-foreground/60 text-[11px]">
            (you)
          </span>
        )}
      </span>
      {isOwnerRow && (
        <Badge variant="secondary" className="text-[10px] px-1">
          Owner
        </Badge>
      )}
      {showKick && (
        <Button
          variant="ghost"
          size="xs"
          className="hidden text-destructive group-hover:inline-flex"
          onClick={() => onKick(member.user_id)}
        >
          Kick
        </Button>
      )}
    </div>
  );
}
