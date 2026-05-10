import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCamerasApi } from "@/hooks/useCamerasApi";
import type { CameraInfo } from "@/types/cameras";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Trash2 } from "lucide-react";

interface MuteDrawerProps {
  cameras: CameraInfo[];
  onClose?: () => void;
}

/** Mute editor — list active mutes, add a new mute, clear an existing
 * mute. Pure UI for the underlying ``cameras.mutes.*`` RPCs.
 */
export function MuteDrawer({ cameras, onClose }: MuteDrawerProps) {
  const api = useCamerasApi();
  const queryClient = useQueryClient();
  const mutesQuery = useQuery({
    queryKey: ["cameras.mutes"],
    queryFn: api.listMutes,
  });
  const [camera, setCamera] = useState<string>("");
  const [label, setLabel] = useState<string>("");
  const [hours, setHours] = useState<number>(8);

  const setMutation = useMutation({
    mutationFn: () =>
      api.setMute({
        camera,
        label,
        until_ms: Date.now() + hours * 3600 * 1000,
      }),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["cameras.mutes"] }),
  });

  const clearMutation = useMutation({
    mutationFn: ({ camera, label }: { camera: string; label: string }) =>
      api.clearMute(camera || "", label || ""),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["cameras.mutes"] }),
  });

  const mutes = mutesQuery.data ?? [];

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Camera mutes</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid grid-cols-3 gap-2 items-end">
          <div>
            <label className="text-xs text-muted-foreground">Camera</label>
            <select
              value={camera}
              onChange={(e) => setCamera(e.target.value)}
              className="w-full border rounded px-2 py-1 text-sm bg-background"
            >
              <option value="">all cameras</option>
              {cameras.map((c) => (
                <option key={c.name} value={c.name}>
                  {c.name}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="text-xs text-muted-foreground">Label</label>
            <Input
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="all labels"
            />
          </div>
          <div>
            <label className="text-xs text-muted-foreground">Hours</label>
            <Input
              type="number"
              min={1}
              max={48}
              value={hours}
              onChange={(e) => setHours(Number(e.target.value) || 8)}
            />
          </div>
        </div>
        <Button
          onClick={() => setMutation.mutate()}
          disabled={setMutation.isPending}
        >
          {setMutation.isPending ? "Muting…" : "Mute"}
        </Button>

        <div className="border-t pt-3 space-y-2">
          {mutes.length === 0 ? (
            <p className="text-sm text-muted-foreground">No active mutes.</p>
          ) : (
            mutes.map((m, idx) => (
              <div
                key={`${m.camera}.${m.label}.${idx}`}
                className="flex items-center gap-2 text-sm"
              >
                <span>
                  {m.camera || "all"} / {m.label || "all"} until{" "}
                  {m.until_ms
                    ? new Date(m.until_ms).toLocaleString()
                    : "no end"}
                </span>
                <Button
                  variant="ghost"
                  size="sm"
                  className="ml-auto h-7"
                  onClick={() =>
                    clearMutation.mutate({
                      camera: m.camera,
                      label: m.label,
                    })
                  }
                >
                  <Trash2 className="h-3 w-3" />
                </Button>
              </div>
            ))
          )}
        </div>
        {onClose && (
          <Button variant="outline" onClick={onClose}>
            Close
          </Button>
        )}
      </CardContent>
    </Card>
  );
}
