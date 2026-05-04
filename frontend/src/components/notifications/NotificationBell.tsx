import { useState, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { BellIcon } from "lucide-react";
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
export function NotificationBell() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const api = useWsApi();
  const { connected } = useWebSocket();
  const [open, setOpen] = useState(false);

  const { data } = useQuery({
    queryKey: ["notifications", "recent"],
    queryFn: () => api.listNotifications(undefined, 10),
    enabled: connected,
    refetchInterval: 60_000, // light polling backup if events miss
  });

  // Live update on new notifications
  const handleNotificationEvent = useCallback((event: GilbertEvent) => {
    queryClient.invalidateQueries({ queryKey: ["notifications"] });
    const urgency = (event.data as { urgency?: string } | undefined)?.urgency;
    if (urgency === "urgent" && typeof window !== "undefined") {
      // Visual: flash the document title briefly
      const original = document.title;
      document.title = "🔔 " + original;
      window.setTimeout(() => {
        document.title = original;
      }, 4000);
    }
  }, [queryClient]);
  useEventBus("notification.received", handleNotificationEvent);

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
    // Deep-link if source_ref names a known shape
    const ref = n.source_ref ?? null;
    if (ref && typeof ref === "object" && "goal_id" in ref) {
      const goalId = String((ref as { goal_id: string }).goal_id);
      navigate(`/agents/${goalId}`);
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
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger
        render={
          <Button
            variant="ghost"
            size="icon-sm"
            className="relative"
            aria-label="Notifications"
          />
        }
      >
        <BellIcon className="size-5" />
        {unread > 0 ? (
          <span
            className="absolute -top-1 -right-1 inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-red-500 px-1 text-[10px] font-medium leading-none text-white"
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
  );
}
