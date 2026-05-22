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
  kind: string; // "" for generic, "chat_speech" for read-aloud clips
  conversationId: string;
}

interface BrowserSpeakerStore {
  enabled: boolean;
  history: PlayItem[];
  lastPlayed: PlayItem | null;
  /** Item currently loaded into the <audio> element. Stays set while
   *  paused so the popover Now-Playing controls can resume it; cleared
   *  by ``stop()`` and naturally-ended one-shot clips. Long-running
   *  streams have no ``ended`` event so they only clear on stop. */
  currentItem: PlayItem | null;
  isPlaying: boolean;
  /** True when the <audio> element has a source loaded but is
   *  paused — i.e. resume() will pick up where it left off. */
  isPaused: boolean;
  setEnabled: (v: boolean) => void;
  replay: (id: string) => void;
  clearHistory: () => void;
  pause: () => void;
  resume: () => void;
  /** Stop playback AND clear the audio source so the Now-Playing
   *  controls disappear. For internet radio streams this is the only
   *  way to actually release the connection — Pause keeps the stream
   *  buffering on most browsers. */
  stop: () => void;
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
  const [currentItem, setCurrentItem] = useState<PlayItem | null>(null);
  const [isPlaying, setIsPlaying] = useState(false);
  const [isPaused, setIsPaused] = useState(false);
  const { connected, rpc, subscribe } = useWebSocket();

  // Singleton <audio> element kept across renders. The ``play`` /
  // ``pause`` / ``ended`` listeners keep ``isPlaying`` / ``isPaused``
  // in sync no matter who triggers the state change — the SPA via
  // pause/resume/stop, the backend via ``speaker.browser.play`` events,
  // or the user pressing the OS-level media key.
  const audioRef = useRef<HTMLAudioElement | null>(null);
  if (audioRef.current === null && typeof document !== "undefined") {
    const el = document.createElement("audio");
    el.preload = "metadata";
    el.style.display = "none";
    document.body.appendChild(el);
    audioRef.current = el;
    el.addEventListener("play", () => {
      setIsPlaying(true);
      setIsPaused(false);
    });
    el.addEventListener("pause", () => {
      setIsPlaying(false);
      // Distinguish "ended" from "user-paused": ``ended`` fires
      // ``pause`` too, but with ``el.ended === true``. For
      // long-running streams ``ended`` never fires, so a manual pause
      // here means we can resume from the current buffer position.
      setIsPaused(!el.ended && !!el.src);
    });
    el.addEventListener("ended", () => {
      setIsPlaying(false);
      setIsPaused(false);
    });
  }

  // Activation sync — fire activate/deactivate to the server based on
  // [enabled, connected]. Re-activates on reconnect if enabled is true.
  // The backend rejects activations from an unauthenticated connection
  // (race: SPA fires activate the instant ``connected`` flips, but the
  // WS may not have completed its auth handshake yet, so ``user_id``
  // is briefly empty). The handler returns ``{status: "error", ...}``
  // rather than throwing, so we explicitly inspect ``status`` here and
  // leave ``lastSyncedRef`` un-flipped on failure — the next state
  // change (typically the auth-completion re-render) re-fires and
  // lands cleanly.
  const lastSyncedRef = useRef<boolean>(false);
  useEffect(() => {
    const want = enabled && connected;
    if (want === lastSyncedRef.current) return;
    lastSyncedRef.current = want;
    let cancelled = false;
    (async () => {
      try {
        const result = want
          ? await rpc<{ status?: string }>({
              type: "browser_speaker.activate",
            })
          : await rpc<{ status?: string }>({
              type: "browser_speaker.deactivate",
            });
        if (!cancelled && result?.status === "error") {
          lastSyncedRef.current = !want;
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
        kind: typeof data.kind === "string" ? data.kind : "",
        conversationId:
          typeof data.conversation_id === "string" ? data.conversation_id : "",
      };
      if (item.kind !== "chat_speech") {
        setHistory((prev) => [item, ...prev].slice(0, HISTORY_LIMIT));
      }
      setLastPlayed(item);
      if (enabled && audioRef.current) {
        const el = audioRef.current;
        // Belt-and-suspenders: explicitly stop any in-flight clip before
        // swapping src so the interrupt is deterministic across browsers.
        if (!el.paused) el.pause();
        el.src = url;
        el.volume = Math.max(0, Math.min(1, item.volume / 100));
        setCurrentItem(item);
        setIsPlaying(true);
        setIsPaused(false);
        el.play().catch(() => {
          setIsPlaying(false);
          setIsPaused(false);
        });
      }
    };
    return subscribe("speaker.browser.play", handler);
  }, [enabled, subscribe]);

  // The browser backend publishes ``speaker.browser.stop`` when a
  // ``stop_speakers`` call hits the user's browser target. Mirror it
  // in the SPA so the <audio> element actually stops — otherwise a
  // long-running stream (e.g. an internet radio station) keeps playing
  // after the user pressed stop in the UI. Also clears the loaded
  // source so the Now-Playing controls disappear.
  useEffect(() => {
    const handler = () => {
      const el = audioRef.current;
      if (el) {
        el.pause();
        el.removeAttribute("src");
        try {
          el.load();
        } catch {
          // load() can throw in obscure browser states; the pause
          // above is what actually matters for stopping audio.
        }
      }
      setIsPlaying(false);
      setIsPaused(false);
      setCurrentItem(null);
    };
    return subscribe("speaker.browser.stop", handler);
  }, [subscribe]);

  const setEnabled = useCallback((v: boolean) => {
    persistEnabled(v);
    setEnabledState(v);
    if (!v && audioRef.current) {
      audioRef.current.pause();
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
      setCurrentItem(item);
      el.play().catch(() => {
        setIsPlaying(false);
        setIsPaused(false);
      });
    },
    [history],
  );

  const clearHistory = useCallback(() => setHistory([]), []);

  const pause = useCallback(() => {
    const el = audioRef.current;
    if (el && !el.paused) {
      el.pause();
    }
  }, []);

  const resume = useCallback(() => {
    const el = audioRef.current;
    if (!el || !el.src || !el.paused) return;
    el.play().catch(() => {
      setIsPlaying(false);
      setIsPaused(false);
    });
  }, []);

  const stop = useCallback(() => {
    const el = audioRef.current;
    if (el) {
      el.pause();
      // Streams keep the underlying connection open until the source
      // is unset — pausing alone isn't enough to fully release a
      // long-running radio stream. Clearing src + load() is the
      // documented way to drop the network handle.
      el.removeAttribute("src");
      try {
        el.load();
      } catch {
        // See speaker.browser.stop handler — load() may throw.
      }
    }
    setIsPlaying(false);
    setIsPaused(false);
    setCurrentItem(null);
  }, []);

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
      currentItem,
      isPlaying,
      isPaused,
      setEnabled,
      replay,
      clearHistory,
      pause,
      resume,
      stop,
    }),
    [
      enabled,
      history,
      lastPlayed,
      currentItem,
      isPlaying,
      isPaused,
      setEnabled,
      replay,
      clearHistory,
      pause,
      resume,
      stop,
    ],
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

function clampVolume(raw: unknown): number {
  const n = typeof raw === "number" ? raw : Number(raw);
  if (!Number.isFinite(n)) return 80;
  return Math.max(0, Math.min(100, Math.round(n)));
}
