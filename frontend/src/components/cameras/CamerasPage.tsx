import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useCamerasApi } from "@/hooks/useCamerasApi";
import { CameraCard } from "./CameraCard";
import { EventList } from "./EventList";
import { MuteDrawer } from "./MuteDrawer";
import { Button } from "@/components/ui/button";
import { Camera as CameraIcon, BellOff } from "lucide-react";

/** Cameras dashboard page.
 *
 * Top section: per-camera grid (latest snapshot + last detection).
 * Below: recent events list across all visible cameras.
 * Sidebar / drawer: mute editor.
 */
export function CamerasPage() {
  const api = useCamerasApi();
  const [muteOpen, setMuteOpen] = useState(false);

  const camerasQuery = useQuery({
    queryKey: ["cameras.list"],
    queryFn: api.listCameras,
  });
  const eventsQuery = useQuery({
    queryKey: ["cameras.events.recent"],
    queryFn: () => api.listEvents({ limit: 50 }),
    refetchInterval: 30_000,
  });

  const cameras = camerasQuery.data ?? [];
  const events = eventsQuery.data ?? [];

  // Index latest event per camera for the grid cards.
  const latestByCamera = new Map<string, (typeof events)[number]>();
  for (const ev of events) {
    if (!latestByCamera.has(ev.camera)) {
      latestByCamera.set(ev.camera, ev);
    }
  }

  return (
    <div className="p-4 sm:p-6 space-y-4">
      <div className="flex items-center gap-2">
        <CameraIcon className="h-5 w-5" />
        <h1 className="text-2xl font-semibold">Cameras</h1>
        <Button
          variant="outline"
          size="sm"
          className="ml-auto"
          onClick={() => setMuteOpen((v) => !v)}
        >
          <BellOff className="h-4 w-4 mr-1" />
          Mutes
        </Button>
      </div>

      {muteOpen && (
        <MuteDrawer cameras={cameras} onClose={() => setMuteOpen(false)} />
      )}

      <section>
        <h2 className="text-sm font-semibold text-muted-foreground mb-2">
          Cameras visible to you
        </h2>
        {camerasQuery.isLoading ? (
          <p className="text-muted-foreground">Loading cameras…</p>
        ) : cameras.length === 0 ? (
          <p className="text-muted-foreground">
            No cameras configured. Enable the ``cameras`` service in
            Settings &rarr; Monitoring.
          </p>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {cameras.map((cam) => (
              <CameraCard
                key={cam.name}
                camera={cam}
                latestEvent={latestByCamera.get(cam.name)}
              />
            ))}
          </div>
        )}
      </section>

      <section>
        <h2 className="text-sm font-semibold text-muted-foreground mb-2">
          Recent events
        </h2>
        <EventList events={events} loading={eventsQuery.isLoading} />
      </section>
    </div>
  );
}
