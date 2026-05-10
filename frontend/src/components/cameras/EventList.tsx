import type { CameraEventRow } from "@/types/cameras";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

interface EventListProps {
  events: CameraEventRow[];
  loading?: boolean;
  onSelect?: (event: CameraEventRow) => void;
}

/** Recent-events list used by the cameras page and the dashboard card.
 *
 * Each row is keyed by ``event_id`` (Frigate's globally-unique event
 * identifier). Thumbnails fetch via the proxy route
 * ``/api/cameras/events/<id>/snapshot.jpg`` so the browser never sees
 * the raw Frigate URL or auth token.
 */
export function EventList({ events, loading, onSelect }: EventListProps) {
  if (loading) {
    return (
      <Card>
        <CardContent className="p-4 text-muted-foreground">Loading…</CardContent>
      </Card>
    );
  }
  if (events.length === 0) {
    return (
      <Card>
        <CardContent className="p-4 text-muted-foreground">
          No camera events.
        </CardContent>
      </Card>
    );
  }
  return (
    <div className="space-y-2">
      {events.map((ev) => (
        <Card
          key={ev.event_id}
          className="cursor-pointer hover:bg-accent"
          onClick={() => onSelect?.(ev)}
        >
          <CardContent className="p-3 flex gap-3 items-start">
            {ev.has_snapshot ? (
              <img
                src={ev.snapshot_url}
                alt={`${ev.camera} ${ev.label}`}
                className="w-32 h-20 object-cover rounded border"
                loading="lazy"
              />
            ) : (
              <div className="w-32 h-20 rounded border bg-muted flex items-center justify-center text-xs text-muted-foreground">
                no snapshot
              </div>
            )}
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 mb-1">
                <span className="font-medium">{ev.camera}</span>
                <Badge variant="outline">{ev.label}</Badge>
                {ev.sub_label && (
                  <Badge variant="secondary">{ev.sub_label}</Badge>
                )}
                <span className="text-xs text-muted-foreground ml-auto">
                  {new Date(ev.started_at).toLocaleString()}
                </span>
              </div>
              {ev.vision_text && (
                <p className="text-sm text-muted-foreground italic">
                  {ev.vision_text}
                </p>
              )}
              <div className="flex gap-2 mt-1 text-xs">
                <span>score {(ev.score ?? 0).toFixed(2)}</span>
                {ev.has_clip && ev.clip_url && (
                  <a
                    href={ev.clip_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-blue-500 hover:underline"
                    onClick={(e) => e.stopPropagation()}
                  >
                    clip
                  </a>
                )}
              </div>
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
