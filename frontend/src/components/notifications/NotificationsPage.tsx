import { useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { BellIcon, Trash2Icon, CheckIcon } from "lucide-react";
import { useEventBus } from "@/hooks/useEventBus";
import { useWebSocket } from "@/hooks/useWebSocket";
import { useWsApi } from "@/hooks/useWsApi";
import { Button } from "@/components/ui/button";
import type { Notification as AppNotification } from "@/types/notifications";

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
    const ref = n.source_ref ?? null;
    if (ref && typeof ref === "object" && "goal_id" in ref) {
      navigate(`/agents/${(ref as { goal_id: string }).goal_id}`);
    }
    handleMarkRead(n);
  };

  return (
    <div className="container mx-auto py-6 max-w-3xl">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-2xl font-semibold">Notifications</h1>
        <Button
          variant="outline"
          size="sm"
          onClick={handleMarkAllRead}
          disabled={unread === 0}
        >
          <CheckIcon className="size-4 mr-1" />
          Mark all read ({unread})
        </Button>
      </div>

      {isLoading ? (
        <div className="text-center text-muted-foreground py-8">Loading…</div>
      ) : items.length === 0 ? (
        <div className="text-center text-muted-foreground py-8">
          No notifications.
        </div>
      ) : (
        <div className="rounded-md border divide-y">
          {items.map((n) => (
            <div
              key={n.id}
              className={`flex items-start gap-3 px-4 py-3 hover:bg-accent/50 ${
                n.read ? "opacity-60" : ""
              }`}
            >
              <BellIcon
                className={`size-4 mt-1 shrink-0 ${
                  URGENCY_COLOR[n.urgency] ?? URGENCY_COLOR.normal
                }`}
              />
              <button
                type="button"
                onClick={() => handleClick(n)}
                className="flex-1 text-left min-w-0"
              >
                <div className="text-sm break-words">{n.message}</div>
                <div className="text-xs text-muted-foreground mt-1 flex items-center gap-2 flex-wrap">
                  <span>{n.source}</span>
                  <span>·</span>
                  <span>{n.urgency}</span>
                  <span>·</span>
                  <span>{timeAgo(n.created_at)}</span>
                  {n.read && n.read_at ? (
                    <>
                      <span>·</span>
                      <span>read {timeAgo(n.read_at)}</span>
                    </>
                  ) : null}
                </div>
              </button>
              <div className="flex items-center gap-1 shrink-0">
                {!n.read ? (
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    onClick={() => handleMarkRead(n)}
                    title="Mark as read"
                  >
                    <CheckIcon className="size-4" />
                  </Button>
                ) : null}
                <Button
                  variant="ghost"
                  size="icon-sm"
                  onClick={() => handleDelete(n)}
                  title="Delete"
                >
                  <Trash2Icon className="size-4" />
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
