import { MarkdownContent } from "@/components/ui/MarkdownContent";
import type { ChatMessage } from "@/types/chat";
import { cn } from "@/lib/utils";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";

interface MessageBubbleProps {
  message: ChatMessage;
  isShared: boolean;
  currentUserId?: string;
}

export function MessageBubble({
  message,
  isShared,
  currentUserId,
}: MessageBubbleProps) {
  const isOwnMessage = !isShared || !message.author_id || message.author_id === currentUserId;
  const isUser = message.role === "user" && isOwnMessage;
  const isAssistant = message.role === "assistant";

  let authorLabel = "";
  if (isAssistant) {
    authorLabel = "Gilbert";
  } else if (isShared && message.author_name) {
    authorLabel = isOwnMessage ? "You" : message.author_name;
  } else if (message.role === "user") {
    authorLabel = "You";
  }

  // Strip [Name]: prefix from shared room messages — stored for AI context
  // but shouldn't display since we show author_name separately
  let displayContent = message.content;
  if (isShared && message.role === "user") {
    displayContent = displayContent.replace(/^\[.*?\]:\s*/, "");
  }

  const initials = isAssistant
    ? "G"
    : (message.author_name || "You").charAt(0).toUpperCase();

  return (
    <div
      className={cn(
        "flex gap-2.5 max-w-3xl mx-auto",
        isUser ? "flex-row-reverse" : "flex-row",
      )}
    >
      <Avatar className="size-7 shrink-0 mt-0.5">
        <AvatarFallback
          className={cn(
            "text-[11px]",
            isAssistant && "bg-primary text-primary-foreground",
          )}
        >
          {initials}
        </AvatarFallback>
      </Avatar>

      <div
        className={cn(
          "flex flex-col gap-0.5 min-w-0",
          isUser ? "items-end" : "items-start",
        )}
      >
        <span className="text-[11px] text-muted-foreground px-0.5">
          {authorLabel}
        </span>
        <div
          className={cn(
            "rounded-2xl px-3.5 py-2 text-sm leading-relaxed",
            isUser
              ? "bg-primary text-primary-foreground rounded-tr-sm"
              : "bg-muted rounded-tl-sm",
          )}
        >
          {isAssistant ? (
            <MarkdownContent content={message.content} />
          ) : (
            <p className="whitespace-pre-wrap break-words">{displayContent}</p>
          )}
        </div>
      </div>
    </div>
  );
}
