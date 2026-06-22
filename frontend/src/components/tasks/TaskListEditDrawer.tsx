import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { ConfigField } from "@/components/settings/ConfigField";
import { isParamVisible } from "@/lib/configVisibility";
import { useWsApi } from "@/hooks/useWsApi";
import type { TaskList } from "@/types/tasks";
import { AlertTriangle, Trash2 } from "lucide-react";

interface Props {
  /** If null the drawer is in "create" mode. */
  list: TaskList | null;
  onClose: () => void;
  onSaved: () => void;
}

/** Create / edit / delete a TaskList in a drawer.
 *
 * The backend dropdown is populated from ``tasks.backends.list``;
 * backend-specific config_params are rendered as plain text fields
 * (the rich ConfigField component is deferred to the settings UI's
 * existing wiring).
 */
export function TaskListEditDrawer({ list, onClose, onSaved }: Props) {
  const api = useWsApi();
  const isCreate = list === null;

  const backendsQuery = useQuery({
    queryKey: ["tasks.backends"],
    queryFn: () => api.listTaskBackends(),
  });
  const backends = backendsQuery.data ?? [];

  const [name, setName] = useState(list?.name ?? "");
  const [backendName, setBackendName] = useState(
    list?.backend_name ?? "local",
  );
  const [backendConfig, setBackendConfig] = useState<Record<string, unknown>>(
    list?.backend_config ?? {},
  );
  const [pollEnabled, setPollEnabled] = useState(list?.poll_enabled ?? true);
  const [pollInterval, setPollInterval] = useState(
    list?.poll_interval_sec ?? 300,
  );
  const [isDefault, setIsDefault] = useState(list?.is_default ?? false);
  const [forceDelete, setForceDelete] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [backendActionResult, setBackendActionResult] = useState<string | null>(null);
  const [backendActionRunning, setBackendActionRunning] = useState<string>("");

  useEffect(() => {
    if (!list) return;
    setName(list.name);
    setBackendName(list.backend_name);
    setBackendConfig(list.backend_config);
    setPollEnabled(list.poll_enabled);
    setPollInterval(list.poll_interval_sec);
    setIsDefault(list.is_default);
  }, [list]);

  const selectedBackend = backends.find((b) => b.name === backendName);

  const runBackendAction = async (key: string) => {
    setBackendActionResult(null);
    setBackendActionRunning(key);
    try {
      const response = await api.invokeConfigAction("tasks", key, {
        backend: backendName,
        config: backendConfig,
      });
      const result = response.result;
      const persistRaw = (result.data ?? {})["persist"];
      if (persistRaw && typeof persistRaw === "object") {
        setBackendConfig((prev) => ({
          ...prev,
          ...(persistRaw as Record<string, unknown>),
        }));
      }
      if (result.open_url) {
        window.open(result.open_url, "_blank", "noopener,noreferrer");
      }
      setBackendActionResult(result.message || result.status);
    } catch (e) {
      setBackendActionResult((e as Error).message || "Backend action failed");
    } finally {
      setBackendActionRunning("");
    }
  };

  const create = useMutation({
    mutationFn: () =>
      api.createTaskList({
        name,
        backend_name: backendName,
        backend_config: backendConfig,
        poll_enabled: pollEnabled,
        poll_interval_sec: pollInterval,
        is_default: isDefault,
      }),
    onSuccess: () => {
      onSaved();
      onClose();
    },
    onError: (e: Error) => setError(e.message || "Failed to create list"),
  });

  const update = useMutation({
    mutationFn: () =>
      api.updateTaskList(list!.id, {
        name,
        backend_name: backendName,
        backend_config: backendConfig,
        poll_enabled: pollEnabled,
        poll_interval_sec: pollInterval,
        is_default: isDefault,
      }),
    onSuccess: () => {
      onSaved();
      onClose();
    },
    onError: (e: Error) => setError(e.message || "Failed to update list"),
  });

  const remove = useMutation({
    mutationFn: () => api.deleteTaskList(list!.id, forceDelete),
    onSuccess: () => {
      onSaved();
      onClose();
    },
    onError: (e: Error) => setError(e.message || "Failed to delete list"),
  });

  const test = useMutation({
    mutationFn: () => api.testTaskListConnection(list!.id),
  });

  const submit = () => {
    setError(null);
    if (!name) {
      setError("Name is required");
      return;
    }
    if (isCreate) {
      create.mutate();
    } else {
      update.mutate();
    }
  };

  return (
    <Sheet open onOpenChange={(open) => (!open ? onClose() : null)}>
      <SheetContent className="w-full sm:max-w-lg overflow-y-auto">
        <SheetHeader>
          <SheetTitle>
            {isCreate ? "New task list" : `Edit ${list!.name}`}
          </SheetTitle>
        </SheetHeader>

        <div className="space-y-4 mt-4">
          <div>
            <Label htmlFor="list-name">Name</Label>
            <Input
              id="list-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Personal, Work, Shopping…"
            />
          </div>

          <div>
            <Label htmlFor="list-backend">Backend</Label>
            <select
              id="list-backend"
              value={backendName}
              onChange={(e) => setBackendName(e.target.value)}
              className="w-full border rounded-md px-3 py-2 text-sm bg-background"
              disabled={!isCreate}
            >
              {backends.map((b) => (
                <option key={b.name} value={b.name}>
                  {b.name}
                </option>
              ))}
            </select>
          </div>

          {selectedBackend && selectedBackend.config_params.length > 0 && (
            <div className="space-y-3 border rounded-md p-3 bg-muted/30">
              <div className="text-xs font-semibold text-muted-foreground">
                Backend configuration
              </div>
              {selectedBackend.config_params
                .filter((p) => isParamVisible(p, backendConfig))
                .map((p) => (
                  <ConfigField
                    key={p.key}
                    param={p}
                    value={backendConfig[p.key] ?? p.default}
                    onChange={(key, value) =>
                      setBackendConfig({ ...backendConfig, [key]: value })
                    }
                  />
                ))}
              {(selectedBackend.actions ?? []).filter((a) => !a.hidden).length > 0 && (
                <div className="flex flex-wrap gap-2 pt-1">
                  {(selectedBackend.actions ?? [])
                    .filter((a) => !a.hidden)
                    .map((a) => (
                      <Button
                        key={a.key}
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={() => runBackendAction(a.key)}
                        disabled={backendActionRunning === a.key}
                      >
                        {backendActionRunning === a.key ? "Running..." : a.label}
                      </Button>
                    ))}
                </div>
              )}
              {backendActionResult && (
                <p className="text-xs text-muted-foreground">{backendActionResult}</p>
              )}
            </div>
          )}

          <Separator />

          <div className="grid grid-cols-2 gap-2">
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={pollEnabled}
                onChange={(e) => setPollEnabled(e.target.checked)}
              />
              Poll enabled
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={isDefault}
                onChange={(e) => setIsDefault(e.target.checked)}
              />
              Default list
            </label>
          </div>

          <div>
            <Label htmlFor="list-poll-interval">
              Poll interval (seconds)
            </Label>
            <Input
              id="list-poll-interval"
              type="number"
              min={60}
              value={pollInterval}
              onChange={(e) =>
                setPollInterval(parseInt(e.target.value, 10) || 300)
              }
            />
          </div>

          {!isCreate && list!.degraded_since && (
            <div className="flex items-center gap-2 text-sm text-amber-600 bg-amber-50 dark:bg-amber-950 px-3 py-2 rounded-md">
              <AlertTriangle className="h-4 w-4" />
              <span>
                Connection issues since {list!.degraded_since}.{" "}
                {list!.last_error}
              </span>
            </div>
          )}

          {!isCreate && (
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => test.mutate()}
                disabled={test.isPending}
              >
                Test connection
              </Button>
              {test.data && (
                <span
                  className={`text-sm ${
                    test.data.ok ? "text-green-600" : "text-destructive"
                  }`}
                >
                  {test.data.ok ? "OK" : test.data.error}
                </span>
              )}
            </div>
          )}

          {error && (
            <div className="text-sm text-destructive flex items-center gap-1">
              <AlertTriangle className="h-4 w-4" />
              {error}
            </div>
          )}

          <div className="flex justify-between pt-4">
            <div>
              {!isCreate && list!.can_admin && (
                <div className="flex items-center gap-2">
                  <label className="flex items-center gap-1 text-xs">
                    <input
                      type="checkbox"
                      checked={forceDelete}
                      onChange={(e) => setForceDelete(e.target.checked)}
                    />
                    Force (drop open tasks)
                  </label>
                  <Button
                    variant="destructive"
                    size="sm"
                    onClick={() => {
                      if (
                        window.confirm(
                          `Delete list "${list!.name}"?` +
                            (forceDelete
                              ? " All tasks in it will be dropped."
                              : ""),
                        )
                      ) {
                        remove.mutate();
                      }
                    }}
                  >
                    <Trash2 className="h-4 w-4 mr-1" /> Delete
                  </Button>
                </div>
              )}
            </div>
            <div className="flex gap-2">
              <Button variant="outline" onClick={onClose}>
                Cancel
              </Button>
              <Button
                onClick={submit}
                disabled={create.isPending || update.isPending}
              >
                {isCreate ? "Create" : "Save"}
              </Button>
            </div>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}
