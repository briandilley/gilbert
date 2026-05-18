import { useCallback, useEffect, useState } from "react";
import { useWebSocket } from "./useWebSocket";
import type { SpeakerSystemInfo } from "@/types/speaker";

/**
 * Per-user preference toggle for "also play speaker output in my browser tab."
 *
 * Reads + writes via the generic ``users.prefs.{get,set}`` RPCs, keyed on
 * ``speaker.browser_echo``. The pref is self-only — the RPC handler reads
 * the connection's authenticated user id and never trusts a frame field
 * for identity, so this hook doesn't need to send one either.
 *
 * Returns ``[enabled, toggle, ready]`` — ``ready`` flips true once the
 * initial read completes so the UI can render the actual toggle state
 * instead of flickering through "off → real value". Hook is cheap to
 * mount anywhere in the chat surface; the read happens once per session.
 */
export function useBrowserEchoPref(): {
  enabled: boolean;
  ready: boolean;
  primaryBackend: string;
  /** True when the primary speaker backend is itself ``browser`` —
   *  in that config the echo toggle is a no-op (the service-side
   *  gate short-circuits to avoid double-play) and the UI should
   *  reflect that rather than pretending the toggle does anything. */
  redundantWithPrimary: boolean;
  setEnabled: (next: boolean) => Promise<void>;
} {
  const { rpc, connected } = useWebSocket();
  const [enabled, setEnabledState] = useState(false);
  const [ready, setReady] = useState(false);
  const [primaryBackend, setPrimaryBackend] = useState<string>("");
  const [activeBackends, setActiveBackends] = useState<string[]>([]);

  useEffect(() => {
    let cancelled = false;
    if (!connected) {
      // Reset readiness if the socket drops so a reconnect re-reads.
      setReady(false);
      return () => {
        cancelled = true;
      };
    }
    (async () => {
      try {
        const [prefReply, infoReply] = await Promise.all([
          rpc<{ value?: unknown }>({
            type: "users.prefs.get",
            key: "speaker.browser_echo",
            default: false,
          } as Record<string, unknown>),
          rpc<SpeakerSystemInfo>({
            type: "speaker.info",
          } as Record<string, unknown>),
        ]);
        if (cancelled) return;
        setEnabledState(prefReply?.value === true);
        setPrimaryBackend(
          typeof infoReply?.primary_backend === "string"
            ? infoReply.primary_backend
            : "",
        );
        setActiveBackends(
          Array.isArray(infoReply?.active_backends)
            ? infoReply.active_backends
            : [],
        );
      } catch (err) {
        // Connection blip / unauthenticated session — leave the toggle
        // in its default "off" state and let the user re-toggle once
        // the connection recovers.
        // eslint-disable-next-line no-console
        console.debug("browser-echo pref read failed", err);
      } finally {
        if (!cancelled) setReady(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [connected, rpc]);

  const setEnabled = useCallback(
    async (next: boolean) => {
      // Optimistic — flip the UI immediately, revert on RPC error so
      // the user sees they're back where they started.
      const prev = enabled;
      setEnabledState(next);
      try {
        await rpc({
          type: "users.prefs.set",
          key: "speaker.browser_echo",
          value: next,
        } as Record<string, unknown>);
      } catch (err) {
        // eslint-disable-next-line no-console
        console.warn("browser-echo pref write failed; reverting", err);
        setEnabledState(prev);
        throw err;
      }
    },
    [enabled, rpc],
  );

  // The echo toggle is redundant when browser is the *only* active
  // backend — flipping it would be a no-op because the service-side
  // gate short-circuits to avoid double-play.
  const browserOnlyActive =
    primaryBackend === "browser" && activeBackends.length === 1;

  return {
    enabled,
    ready,
    primaryBackend,
    redundantWithPrimary: browserOnlyActive,
    setEnabled,
  };
}
