import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useEventBus } from "./useEventBus";
import type { GilbertEvent } from "@/types/events";

/** A single audio reply pushed by the BrowserSpeakerBackend. */
export interface BrowserAudioClip {
  /** Stable client-side id for React keys. */
  id: string;
  /** HTTP URL the SPA can fetch via an HTMLAudioElement. */
  url: string;
  /** TTS source line or caller-supplied title; rendered above the player. */
  title: string;
  /** 0–100. Applied to ``<audio>.volume`` on autoplay and on each <audio>. */
  volume: number;
  /** Conversation the clip is anchored to. Empty string = no chat context. */
  conversationId: string;
  /** ISO-8601 timestamp from the originating bus event. */
  timestamp: string;
  /** True once the provider has called .play() at least once on this clip. */
  played: boolean;
}

interface BrowserSpeakerContextValue {
  clipsForConversation: (conversationId: string) => BrowserAudioClip[];
  allClips: BrowserAudioClip[];
}

const defaultCtx: BrowserSpeakerContextValue = {
  clipsForConversation: () => [],
  allClips: [],
};

const BrowserSpeakerContext =
  createContext<BrowserSpeakerContextValue>(defaultCtx);

// Cap per-conversation history so a long-running session can't grow
// unbounded. Older clips drop off the front of the list — the user can
// still re-trigger via the AI if they want it again.
const MAX_CLIPS_PER_CONVERSATION = 25;

/**
 * Subscribes to ``speaker.browser.play`` / ``speaker.browser.stop`` events,
 * auto-plays new clips via an HTMLAudioElement, and exposes the history
 * so the chat transcript can render inline audio bubbles.
 *
 * Lives inside ``WebSocketProvider`` because it depends on the WS bus.
 */
export function BrowserSpeakerProvider({ children }: { children: ReactNode }) {
  const [clips, setClips] = useState<BrowserAudioClip[]>([]);
  // Single autoplay element so a new clip preempts the previous one
  // instead of overlapping. Per-clip <audio controls> elements inside
  // each bubble are independent and let the user replay at will.
  const autoplayRef = useRef<HTMLAudioElement | null>(null);

  const handlePlay = useCallback((event: GilbertEvent) => {
    const data = event.data as Record<string, unknown>;
    const url = typeof data.url === "string" ? data.url : "";
    if (!url) return;
    const clip: BrowserAudioClip = {
      id:
        typeof event.timestamp === "string" && event.timestamp.length > 0
          ? `${event.timestamp}-${Math.random().toString(36).slice(2, 8)}`
          : `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      url,
      title: typeof data.title === "string" ? data.title : "",
      volume: clampVolume(data.volume),
      conversationId:
        typeof data.conversation_id === "string" ? data.conversation_id : "",
      timestamp: event.timestamp,
      played: false,
    };

    setClips((prev) => {
      // Trim per-conversation. We keep clips for *all* conversations
      // so navigating away and back still shows recent replies, but
      // each conversation's bucket is bounded.
      const sameConv = prev.filter(
        (c) => c.conversationId === clip.conversationId,
      );
      const otherConv = prev.filter(
        (c) => c.conversationId !== clip.conversationId,
      );
      const trimmedSame = [...sameConv, clip].slice(-MAX_CLIPS_PER_CONVERSATION);
      return [...otherConv, ...trimmedSame];
    });

    // Stop any prior autoplay before starting the new one. The user's
    // per-bubble <audio controls> elements are unaffected — they own
    // their own playback state.
    if (autoplayRef.current) {
      autoplayRef.current.pause();
      autoplayRef.current = null;
    }
    const audio = new Audio(clip.url);
    audio.volume = clip.volume / 100;
    autoplayRef.current = audio;
    audio.play().catch((err) => {
      // Chrome / Safari block autoplay when there's no recent gesture.
      // Failing is fine — the user can hit play on the bubble manually.
      // eslint-disable-next-line no-console
      console.warn("speaker.browser.play: autoplay blocked", err);
    });
  }, []);

  const handleStop = useCallback(() => {
    if (autoplayRef.current) {
      autoplayRef.current.pause();
      autoplayRef.current = null;
    }
  }, []);

  useEventBus("speaker.browser.play", handlePlay);
  useEventBus("speaker.browser.stop", handleStop);

  // Tear down the autoplay element on unmount so a hot-reload doesn't
  // leave dangling audio playing.
  useEffect(() => {
    return () => {
      if (autoplayRef.current) {
        autoplayRef.current.pause();
        autoplayRef.current = null;
      }
    };
  }, []);

  const clipsForConversation = useCallback(
    (conversationId: string) =>
      clips.filter((c) => c.conversationId === conversationId),
    [clips],
  );

  return (
    <BrowserSpeakerContext.Provider
      value={{ clipsForConversation, allClips: clips }}
    >
      {children}
    </BrowserSpeakerContext.Provider>
  );
}

/** Read the audio clips that belong to the given conversation. */
export function useBrowserSpeakerClips(
  conversationId: string,
): BrowserAudioClip[] {
  return useContext(BrowserSpeakerContext).clipsForConversation(conversationId);
}

function clampVolume(raw: unknown): number {
  const n = typeof raw === "number" ? raw : Number(raw);
  if (!Number.isFinite(n)) return 80;
  return Math.max(0, Math.min(100, Math.round(n)));
}
