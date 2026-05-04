import { useState, useCallback, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { BellIcon, BellRingIcon, XIcon } from "lucide-react";
import { useWebSocket } from "@/hooks/useWebSocket";
import { useEventBus } from "@/hooks/useEventBus";
import { useWsApi } from "@/hooks/useWsApi";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { Notification as AppNotification } from "@/types/notifications";
import type { GilbertEvent } from "@/types/events";

const URGENCY_COLOR: Record<string, string> = {
  info: "text-muted-foreground",
  normal: "text-blue-500",
  urgent: "text-red-500",
};

/** Play a short attention-getting sound using the WebAudio API.
 *  No external asset needed — synthesised on the fly. */
function playUrgentDing(): void {
  if (typeof window === "undefined") return;
  try {
    const Ctor = (
      window.AudioContext ||
      (window as unknown as { webkitAudioContext: typeof AudioContext })
        .webkitAudioContext
    );
    if (!Ctor) return;
    const ctx = new Ctor();
    const now = ctx.currentTime;
    // Two-tone "ding" — 880Hz for 0.15s, then 1320Hz for 0.15s
    [
      { freq: 880, start: now, dur: 0.15 },
      { freq: 1320, start: now + 0.18, dur: 0.15 },
    ].forEach(({ freq, start, dur }) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.frequency.value = freq;
      osc.type = "sine";
      gain.gain.setValueAtTime(0, start);
      gain.gain.linearRampToValueAtTime(0.18, start + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.001, start + dur);
      osc.connect(gain).connect(ctx.destination);
      osc.start(start);
      osc.stop(start + dur);
    });
    // Close the context after the sound finishes
    setTimeout(() => ctx.close().catch(() => undefined), 800);
  } catch {
    // Audio blocked / unavailable — best effort
  }
}

function timeAgo(iso: string): string {
  const then = new Date(iso).getTime();
  const seconds = Math.floor((Date.now() - then) / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

/**
 * Bell icon with unread badge + dropdown of recent notifications.
 * Mounts in the NavBar's right-side cluster. Subscribes to
 * ``notification.received`` events for live updates and refetches the
 * list on receipt so urgency-driven UI cues stay in sync with storage.
 */
interface UrgentToast {
  id: string;
  message: string;
  goalId?: string;
  source: string;
}

export function NotificationBell() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const api = useWsApi();
  const { connected } = useWebSocket();
  const [open, setOpen] = useState(false);
  const [bellPulse, setBellPulse] = useState(false);
  const [urgentToasts, setUrgentToasts] = useState<UrgentToast[]>([]);

  const { data } = useQuery({
    queryKey: ["notifications", "recent"],
    queryFn: () => api.listNotifications(undefined, 10),
    enabled: connected,
    // Aggressive polling so even if events are missed entirely (filter
    // bug, dropped frame, whatever), urgent notifications surface
    // within a few seconds.
    refetchInterval: 5_000,
  });

  const showToastFor = useCallback(
    (n: {
      id?: string;
      message?: string;
      source?: string;
      source_ref?: { goal_id?: string } | null;
    }) => {
      const toast: UrgentToast = {
        id: n.id || String(Date.now()),
        message: n.message || "Agent needs your attention",
        goalId: n.source_ref?.goal_id,
        source: n.source || "system",
      };
      setUrgentToasts((prev) => {
        // Don't duplicate by id
        if (prev.some((t) => t.id === toast.id)) return prev;
        const next = [...prev, toast];
        return next.slice(-3);
      });
    },
    [],
  );

  // Live update on new notifications via WS event (fast path).
  const handleNotificationEvent = useCallback(
    (event: GilbertEvent) => {
      // Visible debug aid — open browser devtools and you should see
      // these log lines whenever a notification.received event arrives.
      // If you see nothing while a notification is created in the
      // backend, the WS event delivery is broken (visibility filter,
      // reconnect, etc.). If you see them but the UI doesn't react,
      // it's a render bug here.
      // eslint-disable-next-line no-console
      console.debug("[NotificationBell] notification.received", event.data);

      queryClient.invalidateQueries({ queryKey: ["notifications"] });
      const data = (event.data ?? {}) as {
        id?: string;
        urgency?: string;
        message?: string;
        source?: string;
        source_ref?: { goal_id?: string } | null;
      };

      // Bell pulse for any new notification
      setBellPulse(true);
      window.setTimeout(() => setBellPulse(false), 1500);

      if (data.urgency === "urgent") {
        playUrgentDing();
        if (typeof document !== "undefined") {
          const original = document.title;
          document.title = "🔔 " + original;
          window.setTimeout(() => {
            document.title = original;
          }, 8000);
        }
        showToastFor(data);
      }
    },
    [queryClient, showToastFor],
  );
  useEventBus("notification.received", handleNotificationEvent);

  // Backstop: every poll cycle, surface unread URGENT notifications as
  // toasts even if we never received a live event for them. The
  // showToastFor function dedupes by id so the same notification
  // doesn't keep popping up.
  useEffect(() => {
    const items = data?.items ?? [];
    const unreadUrgent = items.filter(
      (n) => n.urgency === "urgent" && !n.read,
    );
    if (unreadUrgent.length === 0) return;
    let dingPlayed = false;
    for (const n of unreadUrgent) {
      // First-time show? Pulse + ding once per polling cycle.
      const wasNew = !urgentToasts.some((t) => t.id === n.id);
      if (wasNew && !dingPlayed) {
        playUrgentDing();
        setBellPulse(true);
        window.setTimeout(() => setBellPulse(false), 1500);
        dingPlayed = true;
      }
      showToastFor(n);
    }
    // We intentionally don't include urgentToasts in deps — that would
    // cause re-runs on every toast change. The dedupe in showToastFor
    // handles repeated calls.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, showToastFor]);

  const dismissToast = useCallback((id: string) => {
    setUrgentToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const onToastClick = useCallback(
    async (toast: UrgentToast) => {
      // Mark read if we have the id
      try {
        await api.markNotificationRead(toast.id);
        queryClient.invalidateQueries({ queryKey: ["notifications"] });
      } catch {
        // best effort
      }
      if (toast.goalId) {
        navigate(`/agents?goal=${toast.goalId}`);
      } else {
        navigate("/notifications");
      }
      dismissToast(toast.id);
    },
    [api, queryClient, navigate, dismissToast],
  );

  // Keep bellPulse cleanup safe across unmounts
  useEffect(() => {
    return () => {
      setBellPulse(false);
    };
  }, []);

  const items = data?.items ?? [];
  const unread = data?.unread_count ?? 0;

  const handleClick = async (n: AppNotification) => {
    if (!n.read) {
      try {
        await api.markNotificationRead(n.id);
        queryClient.invalidateQueries({ queryKey: ["notifications"] });
      } catch {
        // best-effort; the user can still navigate
      }
    }
    // Deep-link if source_ref names a known shape — agent goal_id
    // routes to the agent CHAT page (where you'd answer), not the
    // settings page.
    const ref = n.source_ref ?? null;
    if (ref && typeof ref === "object" && "goal_id" in ref) {
      const goalId = String((ref as { goal_id: string }).goal_id);
      navigate(`/agents?goal=${goalId}`);
    } else {
      navigate("/notifications");
    }
    setOpen(false);
  };

  const handleMarkAllRead = async () => {
    try {
      await api.markAllNotificationsRead();
      queryClient.invalidateQueries({ queryKey: ["notifications"] });
    } catch {
      // ignore
    }
  };

  return (
    <>
      {/* Urgent notification toasts — fixed top-right, persist until
          clicked or dismissed. Multiple stack vertically. */}
      {urgentToasts.length > 0 ? (
        <div className="fixed top-16 right-4 z-50 flex flex-col gap-2 max-w-sm">
          {urgentToasts.map((toast) => (
            <div
              key={toast.id}
              className="rounded-md border border-red-300 dark:border-red-700 bg-red-50 dark:bg-red-950/90 shadow-lg p-3 flex items-start gap-2 animate-in slide-in-from-right"
              role="alert"
            >
              <BellRingIcon className="size-4 text-red-600 dark:text-red-400 shrink-0 mt-0.5 animate-pulse" />
              <button
                type="button"
                onClick={() => onToastClick(toast)}
                className="flex-1 text-left min-w-0"
              >
                <div className="text-xs font-medium text-red-700 dark:text-red-300 mb-0.5 capitalize">
                  {toast.source}
                </div>
                <div className="text-sm text-red-900 dark:text-red-100 break-words">
                  {toast.message}
                </div>
                <div className="text-xs text-red-600 dark:text-red-400 mt-1">
                  Click to respond
                </div>
              </button>
              <button
                type="button"
                onClick={() => dismissToast(toast.id)}
                className="text-red-600 dark:text-red-400 hover:text-red-800 dark:hover:text-red-200 shrink-0"
                aria-label="Dismiss"
              >
                <XIcon className="size-4" />
              </button>
            </div>
          ))}
        </div>
      ) : null}

      <DropdownMenu open={open} onOpenChange={setOpen}>
        <DropdownMenuTrigger
          render={
            <Button
              variant="ghost"
              size="icon-sm"
              className={`relative ${bellPulse ? "animate-bounce" : ""}`}
              aria-label="Notifications"
            />
          }
        >
          {bellPulse ? (
            <BellRingIcon className="size-5 text-red-500" />
          ) : (
            <BellIcon className="size-5" />
          )}
          {unread > 0 ? (
            <span
              className={`absolute -top-1 -right-1 inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-red-500 px-1 text-[10px] font-medium leading-none text-white ${
                bellPulse ? "animate-ping-once" : ""
              }`}
              aria-label={`${unread} unread notifications`}
            >
              {unread > 99 ? "99+" : unread}
            </span>
          ) : null}
        </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-80 p-0">
        <div className="flex items-center justify-between px-3 py-2 border-b">
          <span className="text-sm font-medium">Notifications</span>
          <button
            type="button"
            className="text-xs text-muted-foreground hover:text-foreground disabled:opacity-50"
            onClick={handleMarkAllRead}
            disabled={unread === 0}
          >
            Mark all read
          </button>
        </div>
        <div className="max-h-96 overflow-y-auto">
          {items.length === 0 ? (
            <div className="px-3 py-6 text-center text-sm text-muted-foreground">
              No notifications
            </div>
          ) : (
            items.map((n) => (
              <button
                key={n.id}
                type="button"
                onClick={() => handleClick(n)}
                className={`block w-full text-left px-3 py-2 hover:bg-accent border-b last:border-b-0 ${
                  n.read ? "opacity-60" : ""
                }`}
              >
                <div className="flex items-start gap-2">
                  <BellIcon
                    className={`size-3.5 mt-0.5 shrink-0 ${
                      URGENCY_COLOR[n.urgency] ?? URGENCY_COLOR.normal
                    }`}
                  />
                  <div className="flex-1 min-w-0">
                    <div className="text-sm break-words">{n.message}</div>
                    <div className="text-xs text-muted-foreground mt-0.5 flex items-center gap-2">
                      <span>{n.source}</span>
                      <span>·</span>
                      <span>{timeAgo(n.created_at)}</span>
                    </div>
                  </div>
                </div>
              </button>
            ))
          )}
        </div>
        <div className="px-3 py-2 border-t">
          <button
            type="button"
            className="text-xs text-muted-foreground hover:text-foreground"
            onClick={() => {
              navigate("/notifications");
              setOpen(false);
            }}
          >
            View all
          </button>
        </div>
      </DropdownMenuContent>
      </DropdownMenu>
    </>
  );
}
