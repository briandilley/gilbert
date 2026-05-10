import type { CameraEventRow, CameraInfo } from "@/types/cameras";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

interface CameraCardProps {
  camera: CameraInfo;
  latestEvent?: CameraEventRow;
}

/** Per-camera summary card — latest snapshot + last detection. */
export function CameraCard({ camera, latestEvent }: CameraCardProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          {camera.name}
          {camera.role_visibility !== "everyone" && (
            <Badge variant="outline" className="text-xs">
              {camera.role_visibility}
            </Badge>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        {latestEvent && latestEvent.has_snapshot ? (
          <img
            src={latestEvent.snapshot_url}
            alt={`${camera.name} latest`}
            className="w-full h-40 object-cover rounded border"
            loading="lazy"
          />
        ) : (
          <div className="w-full h-40 rounded border bg-muted flex items-center justify-center text-sm text-muted-foreground">
            no recent snapshot
          </div>
        )}
        <div className="flex items-center gap-2 flex-wrap text-xs">
          {camera.labels.map((l) => (
            <Badge key={l} variant="secondary">
              {l}
            </Badge>
          ))}
        </div>
        {latestEvent && (
          <p className="text-xs text-muted-foreground">
            Last: {latestEvent.label} at{" "}
            {new Date(latestEvent.started_at).toLocaleTimeString()}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
