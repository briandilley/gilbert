import { useCallback, useEffect, useState } from "react";
import { useWebSocket } from "./useWebSocket";

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
  setEnabled: (next: boolean) => Promise<void>;
} {
  const { rpc, connected } = useWebSocket();
  const [enabled, setEnabledState] = useState(false);
  const [ready, setReady] = useState(false);

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
        const reply = await rpc<{ value?: unknown }>({
          type: "users.prefs.get",
          key: "speaker.browser_echo",
          default: false,
        } as Record<string, unknown>);
        if (cancelled) return;
        setEnabledState(reply?.value === true);
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

  return { enabled, ready, setEnabled };
}
