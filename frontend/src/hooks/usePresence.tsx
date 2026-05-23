// Session-presence: which user_ids currently have ≥1 WebSocket
// connection open to Gilbert. Powers the green online dot on user
// avatars in shared rooms.
//
// Design:
// - One ``PresenceProvider`` at the app root owns the live set.
// - On WS connect (and reconnect) we call ``chat.presence.online_users``
//   to grab the snapshot — events alone aren't enough because users
//   already online before our tab opened wouldn't show up otherwise.
// - We listen for ``chat.user.online`` / ``chat.user.offline`` to keep
//   the set fresh as users come and go.
// - ``useIsUserOnline(userId)`` is the leaf hook avatars consume; it
//   triggers a re-render only when that specific user_id transitions,
//   not on every other user's events.

import {
  createContext,
  ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { useWebSocket } from "./useWebSocket";
import type { GilbertEvent } from "@/types/events";

interface PresenceContextValue {
  /** All currently-online user_ids. Stable reference per snapshot — do
   *  NOT mutate; treat as immutable. */
  onlineUserIds: ReadonlySet<string>;
}

const PresenceContext = createContext<PresenceContextValue>({
  onlineUserIds: new Set(),
});

interface OnlineUsersResult {
  user_ids?: string[];
}

export function PresenceProvider({ children }: { children: ReactNode }) {
  const { connected, rpc, subscribe } = useWebSocket();
  const [onlineUserIds, setOnlineUserIds] = useState<ReadonlySet<string>>(
    () => new Set(),
  );

  // Fetch the snapshot whenever the WS connects (initial load + after
  // any reconnect). On a stale set after reconnect we'd otherwise show
  // people offline who are actually online — they came online while
  // our tab was disconnected so we missed their event.
  useEffect(() => {
    if (!connected) return;
    let cancelled = false;
    rpc<OnlineUsersResult>({ type: "chat.presence.online_users" })
      .then((res) => {
        if (cancelled) return;
        setOnlineUserIds(new Set(res.user_ids ?? []));
      })
      .catch(() => {
        // Snapshot is best-effort; the event subscriptions will fill
        // in transitions even if the initial fetch fails. The set
        // stays empty (or stale from a previous connect) which means
        // some avatars won't show the dot until their owner triggers
        // an event — acceptable degradation.
      });
    return () => {
      cancelled = true;
    };
  }, [connected, rpc]);

  // Maintain the set through online/offline transitions. Using
  // ``setOnlineUserIds(prev => ...)`` so we never trigger a re-render
  // when the set didn't actually change (e.g. duplicate event from
  // the same edge transition).
  useEffect(() => {
    const onOnline = (event: GilbertEvent) => {
      const uid = (event.data as { user_id?: string }).user_id;
      if (!uid) return;
      setOnlineUserIds((prev) => {
        if (prev.has(uid)) return prev;
        const next = new Set(prev);
        next.add(uid);
        return next;
      });
    };
    const onOffline = (event: GilbertEvent) => {
      const uid = (event.data as { user_id?: string }).user_id;
      if (!uid) return;
      setOnlineUserIds((prev) => {
        if (!prev.has(uid)) return prev;
        const next = new Set(prev);
        next.delete(uid);
        return next;
      });
    };
    const unsubOnline = subscribe("chat.user.online", onOnline);
    const unsubOffline = subscribe("chat.user.offline", onOffline);
    return () => {
      unsubOnline();
      unsubOffline();
    };
  }, [subscribe]);

  const value = useMemo(() => ({ onlineUserIds }), [onlineUserIds]);

  return (
    <PresenceContext.Provider value={value}>
      {children}
    </PresenceContext.Provider>
  );
}

/**
 * Returns true if the given user_id currently has at least one
 * WebSocket connection to Gilbert. ``""`` always returns false.
 *
 * Re-renders only when that *specific* user transitions — implemented
 * via the ``ReadonlySet`` identity check; the parent provider sets a
 * new identity only when the membership actually changed for SOME
 * user. (A finer-grained per-user subscription would be possible but
 * the set is small enough that the broad invalidation is fine.)
 */
export function useIsUserOnline(userId: string): boolean {
  const { onlineUserIds } = useContext(PresenceContext);
  return userId ? onlineUserIds.has(userId) : false;
}

/**
 * Returns the whole set when a caller needs to iterate (e.g.
 * MemberPanel rendering N avatars with a dot each). Prefer
 * ``useIsUserOnline`` for single-row checks so re-renders stay scoped.
 */
export function useOnlineUserIds(): ReadonlySet<string> {
  return useContext(PresenceContext).onlineUserIds;
}
