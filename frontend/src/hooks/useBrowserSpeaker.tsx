import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useWebSocket } from "@/hooks/useWebSocket";
import type { GilbertEvent } from "@/types/events";

const STORAGE_KEY = "browser_speaker.enabled";
const HISTORY_LIMIT = 10;

export interface PlayItem {
  id: string;
  url: string;
  title: string;
  volume: number; // 0-100
  receivedAt: number;
}

interface BrowserSpeakerStore {
  enabled: boolean;
  history: PlayItem[];
  lastPlayed: PlayItem | null;
  isPlaying: boolean;
  setEnabled: (v: boolean) => void;
  replay: (id: string) => void;
  clearHistory: () => void;
}

const BrowserSpeakerContext = createContext<BrowserSpeakerStore | null>(null);

function readPersistedEnabled(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "true";
  } catch {
    return false;
  }
}

function persistEnabled(v: boolean): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, v ? "true" : "false");
  } catch {
    // Storage may be unavailable; ignore.
  }
}

export function BrowserSpeakerProvider({ children }: { children: ReactNode }) {
  const [enabled, setEnabledState] = useState<boolean>(readPersistedEnabled);
  const [history, setHistory] = useState<PlayItem[]>([]);
  const [lastPlayed, setLastPlayed] = useState<PlayItem | null>(null);
  const [isPlaying, setIsPlaying] = useState(false);
  const { connected, rpc, subscribe } = useWebSocket();

  // Singleton <audio> element kept across renders.
  const audioRef = useRef<HTMLAudioElement | null>(null);
  if (audioRef.current === null && typeof document !== "undefined") {
    const el = document.createElement("audio");
    el.preload = "metadata";
    el.style.display = "none";
    document.body.appendChild(el);
    audioRef.current = el;
    el.addEventListener("ended", () => setIsPlaying(false));
    el.addEventListener("pause", () => setIsPlaying(false));
  }

  // Activation sync — fire activate/deactivate to the server based on
  // [enabled, connected]. Re-activates on reconnect if enabled is true.
  const lastSyncedRef = useRef<boolean>(false);
  useEffect(() => {
    const want = enabled && connected;
    if (want === lastSyncedRef.current) return;
    lastSyncedRef.current = want;
    let cancelled = false;
    (async () => {
      try {
        if (want) {
          await rpc({ type: "browser_speaker.activate" });
        } else {
          await rpc({ type: "browser_speaker.deactivate" });
        }
      } catch {
        // Treat as transient; next state change retries.
        // Reset so a future change re-tries properly.
        if (!cancelled) lastSyncedRef.current = !want;
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [enabled, connected, rpc]);

  // Event subscription — append to history, autoplay if enabled.
  useEffect(() => {
    const handler = (event: GilbertEvent) => {
      const data = event.data as Record<string, unknown>;
      const url = typeof data.url === "string" ? data.url : "";
      if (!url) return;
      const item: PlayItem = {
        id:
          typeof event.timestamp === "string" && event.timestamp.length > 0
            ? `${event.timestamp}-${Math.random().toString(36).slice(2, 8)}`
            : `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        url,
        title: typeof data.title === "string" ? data.title : "",
        volume: clampVolume(data.volume),
        receivedAt: Date.now(),
      };
      setHistory((prev) => [item, ...prev].slice(0, HISTORY_LIMIT));
      setLastPlayed(item);
      if (enabled && audioRef.current) {
        const el = audioRef.current;
        el.src = url;
        el.volume = Math.max(0, Math.min(1, item.volume / 100));
        setIsPlaying(true);
        el.play().catch(() => setIsPlaying(false));
      }
    };
    return subscribe("speaker.browser.play", handler);
  }, [enabled, subscribe]);

  const setEnabled = useCallback((v: boolean) => {
    persistEnabled(v);
    setEnabledState(v);
    if (!v && audioRef.current) {
      audioRef.current.pause();
      setIsPlaying(false);
    }
  }, []);

  const replay = useCallback(
    (id: string) => {
      const el = audioRef.current;
      if (!el) return;
      const item = history.find((h) => h.id === id);
      if (!item) return;
      el.src = item.url;
      el.volume = Math.max(0, Math.min(1, item.volume / 100));
      setIsPlaying(true);
      el.play().catch(() => setIsPlaying(false));
    },
    [history],
  );

  const clearHistory = useCallback(() => setHistory([]), []);

  // Cleanup the singleton audio element on unmount.
  useEffect(() => {
    return () => {
      const el = audioRef.current;
      if (el) {
        el.pause();
        el.remove();
        audioRef.current = null;
      }
    };
  }, []);

  const store = useMemo<BrowserSpeakerStore>(
    () => ({
      enabled,
      history,
      lastPlayed,
      isPlaying,
      setEnabled,
      replay,
      clearHistory,
    }),
    [enabled, history, lastPlayed, isPlaying, setEnabled, replay, clearHistory],
  );

  return (
    <BrowserSpeakerContext.Provider value={store}>
      {children}
    </BrowserSpeakerContext.Provider>
  );
}

export function useBrowserSpeaker(): BrowserSpeakerStore {
  const ctx = useContext(BrowserSpeakerContext);
  if (ctx === null) {
    throw new Error("useBrowserSpeaker must be used inside <BrowserSpeakerProvider>");
  }
  return ctx;
}

/**
 * Transitional stub — removed after BrowserAudioBubbles is deleted (Hdr-Task 9).
 * Returns an empty array typed as PlayItem[] so BrowserAudioBubbles still compiles.
 */
export function useBrowserSpeakerClips(_conversationId: string): PlayItem[] {
  return [];
}

function clampVolume(raw: unknown): number {
  const n = typeof raw === "number" ? raw : Number(raw);
  if (!Number.isFinite(n)) return 80;
  return Math.max(0, Math.min(100, Math.round(n)));
}
