import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useCamerasApi } from "@/hooks/useCamerasApi";
import { useEventBus } from "@/hooks/useEventBus";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Camera as CameraIcon } from "lucide-react";
import type { CameraEventRow } from "@/types/cameras";

/** Dashboard card — shows the five most recent camera events with
 * thumbnails, role-filtered server-side. Subscribes to
 * ``camera.event.detected`` so new arrivals appear without a poll.
 *
 * Hidden when no events exist (no signal to show, no cost to render).
 */
export function RecentDetectionCard() {
  const api = useCamerasApi();
  const recentQuery = useQuery({
    queryKey: ["cameras.events.dashboard"],
    queryFn: () => api.listEvents({ limit: 5 }),
  });

  const [recent, setRecent] = useState<CameraEventRow[]>([]);

  useEffect(() => {
    if (recentQuery.data) {
      setRecent(recentQuery.data);
    }
  }, [recentQuery.data]);

  // Live updates via the existing event-bus hook.
  useEventBus("camera.event.detected", (ev) => {
    const data = ev.data as Partial<CameraEventRow>;
    if (!data?.event_id) return;
    setRecent((prev) => {
      // Drop dup, prepend, cap.
      const next = [
        data as CameraEventRow,
        ...prev.filter((p) => p.event_id !== data.event_id),
      ];
      return next.slice(0, 5);
    });
  });

  if (recentQuery.isError || (recent.length === 0 && !recentQuery.isLoading)) {
    return null;
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          <CameraIcon className="h-4 w-4" />
          Recent camera events
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        {recent.map((ev) => (
          <div key={ev.event_id} className="flex items-start gap-2">
            {ev.has_snapshot ? (
              <img
                src={ev.snapshot_url}
                alt=""
                className="w-20 h-12 object-cover rounded border"
                loading="lazy"
              />
            ) : (
              <div className="w-20 h-12 rounded border bg-muted" />
            )}
            <div className="flex-1 min-w-0 text-sm">
              <div className="flex items-center gap-1">
                <span className="font-medium truncate">{ev.camera}</span>
                <Badge variant="outline" className="text-xs">
                  {ev.label}
                </Badge>
                <span className="text-xs text-muted-foreground ml-auto">
                  {ev.started_at
                    ? new Date(ev.started_at).toLocaleTimeString()
                    : ""}
                </span>
              </div>
              {ev.vision_text && (
                <p className="text-xs text-muted-foreground truncate italic">
                  {ev.vision_text}
                </p>
              )}
            </div>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
