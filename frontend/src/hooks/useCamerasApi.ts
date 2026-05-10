/**
 * Camera-specific WS RPC wrappers.
 *
 * Lives outside the giant ``useWsApi`` because the camera surface is
 * additive and self-contained — keeping it in its own hook means a
 * future plugin-only camera UI can ship the same client code without
 * touching core's main API hook.
 */

import { useCallback, useMemo } from "react";
import { useWebSocket } from "./useWebSocket";
import type { CameraEventRow, CameraInfo, CameraMute } from "@/types/cameras";

interface ListEventsArgs {
  camera?: string;
  label?: string;
  since?: string;
  until?: string;
  limit?: number;
  offset?: number;
}

interface SetMuteArgs {
  camera?: string;
  label?: string;
  until_ms?: number;
}

export function useCamerasApi() {
  const { rpc } = useWebSocket();

  const listCameras = useCallback(async (): Promise<CameraInfo[]> => {
    const r = await rpc<{ cameras: CameraInfo[] }>({
      type: "cameras.list",
    });
    return r.cameras ?? [];
  }, [rpc]);

  const getCamera = useCallback(
    async (name: string): Promise<CameraInfo | null> => {
      const r = await rpc<{ camera: CameraInfo | null }>({
        type: "cameras.get",
        name,
      });
      return r.camera ?? null;
    },
    [rpc],
  );

  const listEvents = useCallback(
    async (args: ListEventsArgs): Promise<CameraEventRow[]> => {
      const r = await rpc<{ events: CameraEventRow[] }>({
        type: "cameras.events.list",
        ...args,
      });
      return r.events ?? [];
    },
    [rpc],
  );

  const getEvent = useCallback(
    async (event_id: string): Promise<CameraEventRow | null> => {
      const r = await rpc<{ event: CameraEventRow | null }>({
        type: "cameras.events.get",
        event_id,
      });
      return r.event ?? null;
    },
    [rpc],
  );

  const getSnapshot = useCallback(
    async (
      event_id: string,
    ): Promise<{ data: string; media_type: string }> => {
      const r = await rpc<{ data: string; media_type: string }>({
        type: "cameras.snapshots.get",
        event_id,
      });
      return r;
    },
    [rpc],
  );

  const listMutes = useCallback(async (): Promise<CameraMute[]> => {
    const r = await rpc<{ mutes: CameraMute[] }>({
      type: "cameras.mutes.list",
    });
    return r.mutes ?? [];
  }, [rpc]);

  const setMute = useCallback(
    async (args: SetMuteArgs): Promise<CameraMute> => {
      const r = await rpc<{ mute: CameraMute }>({
        type: "cameras.mutes.set",
        ...args,
      });
      return r.mute;
    },
    [rpc],
  );

  const clearMute = useCallback(
    async (camera: string, label: string): Promise<void> => {
      await rpc({
        type: "cameras.mutes.clear",
        camera,
        label,
      });
    },
    [rpc],
  );

  const testConnection = useCallback(async (): Promise<{
    status: string;
    message: string;
  }> => {
    return rpc({ type: "cameras.test_connection" });
  }, [rpc]);

  return useMemo(
    () => ({
      listCameras,
      getCamera,
      listEvents,
      getEvent,
      getSnapshot,
      listMutes,
      setMute,
      clearMute,
      testConnection,
    }),
    [
      listCameras,
      getCamera,
      listEvents,
      getEvent,
      getSnapshot,
      listMutes,
      setMute,
      clearMute,
      testConnection,
    ],
  );
}
