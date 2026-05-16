import { useBrowserSpeakerClips } from "@/hooks/useBrowserSpeaker";
import { Volume2Icon } from "lucide-react";
import { useEffect, useRef } from "react";

interface BrowserAudioBubblesProps {
  conversationId: string;
}

/**
 * Renders BrowserSpeakerBackend audio clips inline at the tail of the
 * chat transcript. Each clip gets its own ``<audio controls>`` so the
 * user can replay, scrub, or pause independently of the auto-play
 * that already fired when the event arrived.
 */
export function BrowserAudioBubbles({
  conversationId,
}: BrowserAudioBubblesProps) {
  const clips = useBrowserSpeakerClips(conversationId);
  if (!conversationId || clips.length === 0) return null;
  return (
    <div className="space-y-3">
      {clips.map((clip) => (
        <AudioBubble
          key={clip.id}
          url={clip.url}
          title={clip.title}
          volume={clip.volume}
        />
      ))}
    </div>
  );
}

interface AudioBubbleProps {
  url: string;
  title: string;
  volume: number;
}

function AudioBubble({ url, title, volume }: AudioBubbleProps) {
  const audioRef = useRef<HTMLAudioElement>(null);

  // Apply the requested volume once metadata is available. We don't
  // override the user's manual volume adjustments after that — the
  // ``<audio>`` element keeps its own state from then on.
  useEffect(() => {
    const el = audioRef.current;
    if (!el) return;
    const apply = () => {
      el.volume = Math.max(0, Math.min(1, volume / 100));
    };
    if (el.readyState >= 1) {
      apply();
    } else {
      el.addEventListener("loadedmetadata", apply, { once: true });
      return () => el.removeEventListener("loadedmetadata", apply);
    }
  }, [volume]);

  return (
    <div className="mx-auto max-w-md rounded-md border border-border bg-muted/30 p-3">
      <div className="mb-2 flex items-center gap-2 text-xs text-muted-foreground">
        <Volume2Icon className="size-3.5" />
        <span className="truncate">{title || "Voice reply"}</span>
      </div>
      <audio
        ref={audioRef}
        controls
        preload="metadata"
        src={url}
        className="w-full"
      />
    </div>
  );
}
