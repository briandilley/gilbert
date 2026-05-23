import { useQuery } from "@tanstack/react-query";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useWsApi } from "@/hooks/useWsApi";
import { Calendar, MapPin } from "lucide-react";

/** Dashboard widget — the next event across every accessible account.
 *
 * Mounted via ``DashboardPage.tsx`` and gated on a ConfigParam
 * (``calendar.show_dashboard_card``) at the page level.
 */
export function UpcomingEventCard() {
  const api = useWsApi();
  const now = new Date();
  const horizon = new Date(now.getTime() + 72 * 60 * 60 * 1000);

  const eventsQuery = useQuery({
    queryKey: ["calendar.upcoming-card"],
    queryFn: () =>
      api.listCalendarEvents({
        time_min: now.toISOString(),
        time_max: horizon.toISOString(),
        max_results: 1,
      }),
  });

  const events = eventsQuery.data?.events ?? [];
  const next = events.find((e) => new Date(e.start) >= now);

  if (eventsQuery.isLoading) {
    return null;
  }
  if (!next) {
    return null;
  }

  const start = new Date(next.start);
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          <Calendar className="h-4 w-4" />
          Up next
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="font-medium">{next.title}</div>
        <div className="text-sm text-muted-foreground">
          {start.toLocaleString(undefined, {
            weekday: "short",
            month: "short",
            day: "numeric",
            hour: "numeric",
            minute: "2-digit",
          })}
        </div>
        {next.location && (
          <div className="text-xs text-muted-foreground flex items-center gap-1 mt-1">
            <MapPin className="h-3 w-3" />
            {next.location}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

