import { useMemo } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import type { CalendarEvent } from "@/types/calendar";
import { localDateKey } from "./datetime";
import { ExternalLink, Plus, Trash2 } from "lucide-react";

interface Props {
  weekStart: Date;
  events: CalendarEvent[];
  loading?: boolean;
  canCreateEvent: boolean;
  onDeleteEvent: (accountId: string, eventId: string) => void;
  onCreateEvent: (date: Date) => void;
}

/** Group events by day-of-week and render them as cards. */
export function WeekAgenda({
  weekStart,
  events,
  loading,
  canCreateEvent,
  onDeleteEvent,
  onCreateEvent,
}: Props) {
  const days = useMemo(() => {
    const out: { label: string; date: Date; events: CalendarEvent[] }[] = [];
    for (let i = 0; i < 7; i++) {
      const d = new Date(weekStart);
      d.setDate(d.getDate() + i);
      const label = d.toLocaleDateString(undefined, {
        weekday: "long",
        month: "short",
        day: "numeric",
      });
      const dayKey = localDateKey(d);
      const dayEvents = events.filter((e) => localDateKey(e.start) === dayKey);
      out.push({ label, date: d, events: dayEvents });
    }
    return out;
  }, [weekStart, events]);

  if (loading) {
    return <div className="text-sm text-muted-foreground">Loading events…</div>;
  }

  return (
    <div className="space-y-4">
      {days.map((day) => (
        <Card key={day.label}>
          <CardHeader className="flex flex-row items-center justify-between gap-3">
            <CardTitle className="text-base">{day.label}</CardTitle>
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={() => onCreateEvent(day.date)}
              disabled={!canCreateEvent}
              title={`Create event on ${day.label}`}
            >
              <Plus className="h-4 w-4" />
            </Button>
          </CardHeader>
          <CardContent className="space-y-2">
            {day.events.length === 0 ? (
              <p className="text-sm text-muted-foreground italic">No events</p>
            ) : (
              day.events.map((evt) => (
                <div
                  key={`${evt.account_id}:${evt.event_id}`}
                  className="flex items-start justify-between rounded border p-2"
                >
                  <div className="flex-1 min-w-0">
                    <div className="font-medium">{evt.title}</div>
                    <div className="text-xs text-muted-foreground">
                      {evt.all_day
                        ? "All day"
                        : `${formatTime(evt.start)} — ${formatTime(evt.end)}`}
                      {evt.location ? ` · ${evt.location}` : ""}
                    </div>
                    {evt.attendees.length > 0 && (
                      <div className="text-xs text-muted-foreground mt-1 truncate">
                        Attendees: {evt.attendees.map((a) => a.email).join(", ")}
                      </div>
                    )}
                    {evt.status !== "confirmed" && (
                      <Badge className="mt-1" variant="secondary">
                        {evt.status}
                      </Badge>
                    )}
                  </div>
                  <div className="flex flex-shrink-0 gap-1">
                    {evt.html_link && (
                      <a
                        href={evt.html_link}
                        target="_blank"
                        rel="noreferrer noopener"
                      >
                        <Button variant="ghost" size="icon" title="Open in provider">
                          <ExternalLink className="h-4 w-4" />
                        </Button>
                      </a>
                    )}
                    <Button
                      variant="ghost"
                      size="icon"
                      onClick={() => {
                        if (
                          window.confirm(
                            `Delete "${evt.title}"?`,
                          )
                        ) {
                          onDeleteEvent(evt.account_id, evt.event_id);
                        }
                      }}
                      title="Delete"
                    >
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </div>
                </div>
              ))
            )}
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

function formatTime(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleTimeString(undefined, {
    hour: "numeric",
    minute: "2-digit",
  });
}
