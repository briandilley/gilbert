import { useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { BellIcon, Trash2Icon, CheckIcon } from "lucide-react";
import { useEventBus } from "@/hooks/useEventBus";
import { useWebSocket } from "@/hooks/useWebSocket";
import { useWsApi } from "@/hooks/useWsApi";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/layout/PageHeader";
import { cn } from "@/lib/utils";
import type { Notification as AppNotification } from "@/types/notifications";

// Urgency → semantic foreground. Functional only (info / normal / urgent).
const URGENCY_COLOR: Record<string, string> = {
  info: "text-muted-foreground",
  normal: "text-info",
  urgent: "text-destructive",
};

function timeAgo(iso: string): string {
  const then = new Date(iso).getTime();
  const seconds = Math.floor((Date.now() - then) / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function NotificationsPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const api = useWsApi();
  const { connected } = useWebSocket();

  const { data, isLoading } = useQuery({
    queryKey: ["notifications", "all"],
    queryFn: () => api.listNotifications(undefined, 500),
    enabled: connected,
  });

  const handleNotificationEvent = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ["notifications"] });
  }, [queryClient]);
  useEventBus("notification.received", handleNotificationEvent);

  const items = data?.items ?? [];
  const unread = data?.unread_count ?? 0;

  const handleMarkRead = async (n: AppNotification) => {
    if (n.read) return;
    await api.markNotificationRead(n.id);
    queryClient.invalidateQueries({ queryKey: ["notifications"] });
  };

  const handleDelete = async (n: AppNotification) => {
    await api.deleteNotification(n.id);
    queryClient.invalidateQueries({ queryKey: ["notifications"] });
  };

  const handleMarkAllRead = async () => {
    await api.markAllNotificationsRead();
    queryClient.invalidateQueries({ queryKey: ["notifications"] });
  };

  const handleClick = (n: AppNotification) => {
    // Notification deep-links to agent/goal views were removed with the
    // agent/goal subsystem; just mark read.
    handleMarkRead(n);
  };

  return (
    <div>
      <PageHeader
        eyebrow="ALERTS"
        title="Notifications"
        description={
          unread === 0
            ? "Up to date."
            : `${unread} unread.`
        }
        actions={
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => navigate("/account/notifications")}
            >
              Delivery routes
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={handleMarkAllRead}
              disabled={unread === 0}
            >
              <CheckIcon />
              Mark all read
            </Button>
          </div>
        }
      />

      <div className="mx-auto max-w-3xl px-4 py-4 sm:px-6 sm:py-6">
        {isLoading ? (
          <div className="text-center text-xs text-muted-foreground py-8">
            Loading…
          </div>
        ) : items.length === 0 ? (
          <div className="flex flex-col items-center justify-center gap-2 rounded-md border border-dashed border-border py-16 text-center">
            <p className="font-mono text-[11px] uppercase tracking-[0.08em] text-muted-foreground">
              No notifications
            </p>
            <p className="text-sm text-muted-foreground">
              You're caught up.
            </p>
          </div>
        ) : (
          <div className="rounded-md border border-border divide-y divide-border">
            {items.map((n) => (
              <div
                key={n.id}
                className={cn(
                  "flex items-start gap-3 px-3 py-2.5 transition-colors",
                  "hover:bg-foreground/[0.025]",
                  n.read && "opacity-60",
                )}
              >
                <BellIcon
                  className={cn(
                    "size-3.5 mt-1 shrink-0",
                    URGENCY_COLOR[n.urgency] ?? URGENCY_COLOR.normal,
                  )}
                />
                <button
                  type="button"
                  onClick={() => handleClick(n)}
                  className="flex-1 text-left min-w-0"
                >
                  <div className="text-sm break-words">{n.message}</div>
                  <div className="text-xs text-muted-foreground mt-0.5 flex items-center gap-1.5 flex-wrap font-mono">
                    <span>{n.source}</span>
                    <span className="text-muted-foreground/50">·</span>
                    <span>{n.urgency}</span>
                    <span className="text-muted-foreground/50">·</span>
                    <span>{timeAgo(n.created_at)}</span>
                    {n.read && n.read_at ? (
                      <>
                        <span className="text-muted-foreground/50">·</span>
                        <span>read {timeAgo(n.read_at)}</span>
                      </>
                    ) : null}
                  </div>
                </button>
                <div className="flex items-center gap-1 shrink-0">
                  {!n.read ? (
                    <Button
                      variant="ghost"
                      size="icon-xs"
                      onClick={() => handleMarkRead(n)}
                      title="Mark as read"
                    >
                      <CheckIcon />
                    </Button>
                  ) : null}
                  <Button
                    variant="ghost"
                    size="icon-xs"
                    onClick={() => handleDelete(n)}
                    title="Delete"
                  >
                    <Trash2Icon />
                  </Button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
