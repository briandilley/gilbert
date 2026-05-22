import { Volume2Icon, VolumeXIcon } from "lucide-react";
import { useChatSpeech } from "@/hooks/useChatSpeech";
import { cn } from "@/lib/utils";

interface Props {
  conversationId: string | null;
  className?: string;
}

export function ChatSpeechToggle({ conversationId, className }: Props) {
  const { enabled, isSpeaking, toggle } = useChatSpeech(conversationId);
  const disabled = !conversationId;

  const Icon = enabled ? Volume2Icon : VolumeXIcon;
  const title = enabled ? "Stop reading replies aloud" : "Read replies aloud";

  return (
    <button
      type="button"
      onClick={() => void toggle()}
      disabled={disabled}
      title={title}
      aria-label={title}
      aria-pressed={enabled}
      className={cn(
        "inline-flex h-7 w-7 items-center justify-center rounded transition-colors",
        "hover:bg-foreground/10 disabled:opacity-40 disabled:cursor-not-allowed",
        enabled ? "text-(--signal)" : "text-foreground/60",
        isSpeaking && "animate-pulse",
        className,
      )}
    >
      <Icon className="h-4 w-4" />
    </button>
  );
}
