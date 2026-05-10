/**
 * MediaLibraryUserMappings — Settings panel for the core media_library
 * service. Per spec §13: one table per configured backend, one row per
 * Gilbert user, dropdown of backend users, plus a per-backend health
 * banner.
 *
 * Uses the standard ``config.action.invoke`` envelope (no new RPC) to
 * call the service-level ConfigActions exposed by MediaLibraryService:
 * ``list_gilbert_users``, ``list_user_mappings``, ``list_backend_users``,
 * ``set_user_mapping``, ``unlink_user_mapping``, ``list_backend_health``,
 * ``test_backend``.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { useEventBus } from "@/hooks/useEventBus";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { RefreshCwIcon, Trash2Icon } from "lucide-react";

interface GilbertUser {
  user_id: string;
  display_name: string;
  email: string;
}

interface BackendUser {
  id: string;
  username: string;
  display_name: string;
}

interface UserMapping {
  gilbert_user_id: string;
  backend_name: string;
  backend_user_id: string;
  backend_username: string;
}

interface BackendHealth {
  backend_name: string;
  status: string;
  last_error: string;
  last_error_at: number;
  last_success_at: number;
}

const NAMESPACE = "media_library";

export function MediaLibraryUserMappings() {
  const api = useWsApi();
  const { connected } = useWebSocket();
  const queryClient = useQueryClient();

  // Gilbert users
  const { data: usersData } = useQuery({
    queryKey: ["media_library", "gilbert_users"],
    queryFn: () => api.invokeConfigAction(NAMESPACE, "list_gilbert_users"),
    enabled: connected,
  });
  const gilbertUsers: GilbertUser[] =
    (usersData?.result?.data?.users as GilbertUser[]) ?? [];

  // Existing mappings
  const { data: mappingsData } = useQuery({
    queryKey: ["media_library", "mappings"],
    queryFn: () => api.invokeConfigAction(NAMESPACE, "list_user_mappings"),
    enabled: connected,
  });
  const mappings: UserMapping[] =
    (mappingsData?.result?.data?.mappings as UserMapping[]) ?? [];

  // Backend health — subscribed to ``media.backend.health_changed``
  // for live updates, with a 30s poll fallback in case the bus drops.
  // Spec §13.2.
  const { data: healthData } = useQuery({
    queryKey: ["media_library", "health"],
    queryFn: () => api.invokeConfigAction(NAMESPACE, "list_backend_health"),
    enabled: connected,
    refetchInterval: 30_000,
  });
  const healthRows: BackendHealth[] =
    (healthData?.result?.data?.health as BackendHealth[]) ?? [];

  // Configured backends == those that show up in healthRows.
  const backends = healthRows.map((h) => h.backend_name);

  const refreshAll = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ["media_library"] });
  }, [queryClient]);

  const refreshHealth = useCallback(() => {
    queryClient.invalidateQueries({
      queryKey: ["media_library", "health"],
    });
  }, [queryClient]);

  // Push: bus event triggers an immediate health refetch so the
  // banner flips green/red without waiting for the 30s poll cycle.
  useEventBus("media.backend.health_changed", refreshHealth);

  if (backends.length === 0) {
    return (
      <div className="rounded-md border p-4 sm:p-6 text-sm text-muted-foreground">
        Enable a media library backend (Plex / Jellyfin) above to manage
        per-user mappings.
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <BackendHealthBanner rows={healthRows} onRefresh={refreshAll} />
      {backends.map((backend) => (
        <BackendMappingsTable
          key={backend}
          backend={backend}
          gilbertUsers={gilbertUsers}
          mappings={mappings.filter((m) => m.backend_name === backend)}
          onChanged={refreshAll}
        />
      ))}
    </div>
  );
}


// ── Health banner ───────────────────────────────────────────────────


function BackendHealthBanner({
  rows,
  onRefresh,
}: {
  rows: BackendHealth[];
  onRefresh: () => void;
}) {
  return (
    <div className="rounded-md border p-3">
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-sm font-semibold">Backend health</h3>
        <Button variant="ghost" size="sm" onClick={onRefresh}>
          <RefreshCwIcon className="size-3 mr-1" /> Refresh
        </Button>
      </div>
      <ul className="text-sm space-y-1">
        {rows.map((row) => (
          <li
            key={row.backend_name}
            className="flex items-center gap-2"
            data-testid={`media-library-health-${row.backend_name}`}
          >
            <span
              className={
                row.status === "healthy"
                  ? "size-2.5 rounded-full bg-green-500"
                  : row.status === "degraded"
                    ? "size-2.5 rounded-full bg-yellow-500"
                    : "size-2.5 rounded-full bg-red-500"
              }
              aria-hidden
            />
            <span className="font-medium">{row.backend_name}</span>
            <span className="text-muted-foreground">
              — {row.status}
              {row.status !== "healthy" && row.last_error
                ? ` (${row.last_error})`
                : ""}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}


// ── Per-backend mapping table ───────────────────────────────────────


function BackendMappingsTable({
  backend,
  gilbertUsers,
  mappings,
  onChanged,
}: {
  backend: string;
  gilbertUsers: GilbertUser[];
  mappings: UserMapping[];
  onChanged: () => void;
}) {
  const api = useWsApi();
  const { connected } = useWebSocket();

  const { data: backendUsersData } = useQuery({
    queryKey: ["media_library", "backend_users", backend],
    queryFn: () =>
      api.invokeConfigAction(NAMESPACE, "list_backend_users", { backend }),
    enabled: connected,
  });
  const backendUsers: BackendUser[] =
    (backendUsersData?.result?.data?.users as BackendUser[]) ?? [];

  // pending edits keyed by gilbert user id (the in-memory dropdown
  // selection before the user clicks "Save all").
  const [pending, setPending] = useState<Record<string, string>>({});
  // local UI state for messages.
  const [saving, setSaving] = useState<boolean>(false);
  const [errorFor, setErrorFor] = useState<string | null>(null);

  useEffect(() => {
    setPending({});
    setErrorFor(null);
  }, [backend]);

  const mappingFor = (gid: string): UserMapping | undefined =>
    mappings.find((m) => m.gilbert_user_id === gid);

  const onSelect = (gid: string, backendUserId: string) => {
    setPending((prev) => ({ ...prev, [gid]: backendUserId }));
  };

  const dirtyEntries = useMemo(
    () =>
      Object.entries(pending).filter(([gid, buid]) => {
        const current = mappingFor(gid);
        return buid && buid !== current?.backend_user_id;
      }),
    [pending, mappings],
  );

  const onSaveAll = async () => {
    if (dirtyEntries.length === 0) return;
    setSaving(true);
    setErrorFor(null);
    try {
      // Spec §13.1: batch all dirty rows in one click. Issue all RPCs
      // in parallel and surface the first failure (the rest stay
      // dirty so the user can retry without losing their selections).
      const results = await Promise.all(
        dirtyEntries.map(async ([gid, buid]) => {
          const buser = backendUsers.find((u) => u.id === buid);
          const response = await api.invokeConfigAction(
            NAMESPACE,
            "set_user_mapping",
            {
              gilbert_user_id: gid,
              backend,
              backend_user_id: buid,
              backend_username: buser?.username || "",
            },
          );
          return { gid, buid, response };
        }),
      );
      const failures = results.filter(
        (r) => r.response.result.status !== "ok",
      );
      const succeededGids = new Set(
        results
          .filter((r) => r.response.result.status === "ok")
          .map((r) => r.gid),
      );
      // Drop the saved rows from the pending map; keep any failures.
      setPending((prev) => {
        const next: Record<string, string> = {};
        for (const [gid, buid] of Object.entries(prev)) {
          if (!succeededGids.has(gid)) next[gid] = buid;
        }
        return next;
      });
      if (failures.length > 0) {
        const first = failures[0].response.result.message || "Save failed";
        setErrorFor(
          failures.length === 1
            ? first
            : `${first} (+${failures.length - 1} more)`,
        );
      }
      onChanged();
    } finally {
      setSaving(false);
    }
  };

  const onUnlink = async (gid: string) => {
    if (!confirm(`Unlink this user from ${backend}?`)) return;
    const response = await api.invokeConfigAction(
      NAMESPACE,
      "unlink_user_mapping",
      { gilbert_user_id: gid, backend },
    );
    if (response.result.status === "ok") {
      onChanged();
    } else {
      setErrorFor(response.result.message || "Unlink failed");
    }
  };

  const onTest = async (_gid: string) => {
    // Test a single user mapping by making a no-op backend call —
    // we re-use ``test_backend`` for simplicity in v1; per-user
    // probes can land later.
    const response = await api.invokeConfigAction(
      NAMESPACE,
      "test_backend",
      { backend },
    );
    alert(
      response.result.status === "ok"
        ? `OK: ${response.result.message}`
        : `FAILED: ${response.result.message}`,
    );
  };

  return (
    <div className="rounded-md border p-3">
      <h3 className="text-sm font-semibold mb-3 capitalize">
        {backend}
      </h3>
      {errorFor && (
        <div className="mb-2 text-xs text-red-500">{errorFor}</div>
      )}
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs text-muted-foreground">
            <th className="py-1 pr-4">Gilbert user</th>
            <th className="py-1 pr-4">{backend} user</th>
            <th className="py-1 pr-2 w-32">Actions</th>
          </tr>
        </thead>
        <tbody>
          {gilbertUsers.map((gu) => {
            const mapping = mappingFor(gu.user_id);
            const selected = pending[gu.user_id] ?? mapping?.backend_user_id ?? "";
            const dirty =
              !!pending[gu.user_id] &&
              pending[gu.user_id] !== mapping?.backend_user_id;
            return (
              <tr key={gu.user_id} className="border-t">
                <td className="py-2 pr-4">
                  <div className="font-medium">{gu.display_name}</div>
                  <div className="text-xs text-muted-foreground">
                    {gu.email || gu.user_id}
                  </div>
                </td>
                <td className="py-2 pr-4">
                  <Select
                    value={selected}
                    onValueChange={(v: string | null) => onSelect(gu.user_id, v ?? "")}
                  >
                    <SelectTrigger
                      className={
                        dirty ? "w-64 ring-2 ring-amber-400" : "w-64"
                      }
                    >
                      <SelectValue placeholder={`Choose ${backend}…`} />
                    </SelectTrigger>
                    <SelectContent>
                      {backendUsers.map((bu) => (
                        <SelectItem key={bu.id} value={bu.id}>
                          {bu.username}
                          {bu.display_name && bu.display_name !== bu.username
                            ? ` — ${bu.display_name}`
                            : ""}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </td>
                <td className="py-2 pr-2">
                  <div className="flex gap-1">
                    {mapping && !dirty && (
                      <>
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => onTest(gu.user_id)}
                        >
                          Test
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => onUnlink(gu.user_id)}
                        >
                          <Trash2Icon className="size-3" />
                        </Button>
                      </>
                    )}
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {/* Spec §13.1: one Save-all button at the bottom commits every
          pending edit in a single batch. Disabled when no edits are
          dirty so the click is unambiguous. */}
      <div className="mt-3 flex items-center justify-end gap-3">
        {dirtyEntries.length > 0 && (
          <span className="text-xs text-muted-foreground">
            {dirtyEntries.length} pending
          </span>
        )}
        <Button
          size="sm"
          onClick={onSaveAll}
          disabled={saving || dirtyEntries.length === 0}
        >
          {saving ? "Saving…" : "Save all"}
        </Button>
      </div>
    </div>
  );
}
