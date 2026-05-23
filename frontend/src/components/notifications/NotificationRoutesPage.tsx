import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useWebSocket } from "@/hooks/useWebSocket";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/**
 * /account/notifications — per-user push-notification routes UI.
 *
 * Purely backend-driven:
 *   - The list of available providers comes from ``push.backends.list``.
 *   - The fields rendered for each provider come from that backend's
 *     ``destination_params``. Adding a new push plugin requires zero
 *     edits to this file.
 *   - The dynamic source filter list comes from ``push.sources.list``
 *     (per-user distinct ``Notification.source`` values from the last
 *     30 days).
 *
 * The empty-state hero quick-setup-ntfy flow is the v1 product priority
 * — a brand new user clicks one button, scans a QR with the ntfy app,
 * and a test notification lands on their phone within 60 seconds.
 */

interface ConfigParamMeta {
  key: string;
  type: string;
  description: string;
  default: unknown;
  sensitive: boolean;
  multiline: boolean;
  choices: string[] | null;
}

interface BackendActionMeta {
  key: string;
  label: string;
  description: string;
}

interface BackendMeta {
  name: string;
  label: string;
  destination_params: ConfigParamMeta[];
  actions: BackendActionMeta[];
  enabled: boolean;
  runtime_data: Record<string, unknown>;
}

interface RouteRow {
  _id: string;
  user_id: string;
  label: string;
  backend_name: string;
  destination_data: Record<string, unknown>;
  enabled: boolean;
  urgency_floor: "info" | "normal" | "urgent";
  source_allow: string[];
  source_deny: string[];
  quiet_hours_start: string | null;
  quiet_hours_end: string | null;
  quiet_hours_timezone: string | null;
  last_delivered_at: string | null;
  created_at: string;
  updated_at: string;
}

function detectBrowserTz(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

export function NotificationRoutesPage() {
  const navigate = useNavigate();
  const { rpc, connected } = useWebSocket();
  const [routes, setRoutes] = useState<RouteRow[] | null>(null);
  const [backends, setBackends] = useState<BackendMeta[]>([]);
  const [knownSources, setKnownSources] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState<RouteRow | null>(null);
  const [showAdd, setShowAdd] = useState(false);

  const reload = useCallback(async () => {
    if (!connected) return;
    setLoading(true);
    try {
      const [routesResp, backendsResp, sourcesResp] = await Promise.all([
        rpc<{ ok: boolean; routes: RouteRow[] }>({ type: "push.routes.list" }),
        rpc<{ ok: boolean; backends: BackendMeta[] }>({
          type: "push.backends.list",
        }),
        rpc<{ ok: boolean; sources: string[] }>({ type: "push.sources.list" }),
      ]);
      if (routesResp.ok) setRoutes(routesResp.routes ?? []);
      if (backendsResp.ok) setBackends(backendsResp.backends ?? []);
      if (sourcesResp.ok) setKnownSources(sourcesResp.sources ?? []);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load routes");
    } finally {
      setLoading(false);
    }
  }, [connected, rpc]);

  useEffect(() => {
    reload();
  }, [reload]);

  const handleToggleEnabled = async (route: RouteRow) => {
    try {
      await rpc({
        type: "push.routes.update",
        route_id: route._id,
        enabled: !route.enabled,
      });
      reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not update route");
    }
  };

  const handleDelete = async (route: RouteRow) => {
    if (
      !confirm(`Delete route ${JSON.stringify(route.label)}? This can't be undone.`)
    ) {
      return;
    }
    try {
      await rpc({ type: "push.routes.delete", route_id: route._id });
      reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not delete route");
    }
  };

  const handleSendTest = async (route: RouteRow) => {
    try {
      const result = await rpc<{
        ok: boolean;
        status: string;
        message: string;
      }>({
        type: "push.routes.test",
        route_id: route._id,
      });
      if (result.ok) {
        alert("Test sent. Check your device.");
      } else if (result.status === "debounced") {
        alert(result.message);
      } else {
        alert(`Test failed: ${result.message}`);
      }
    } catch (err) {
      alert(err instanceof Error ? err.message : "Test failed");
    }
  };

  const ntfyAvailable = backends.some((b) => b.name === "ntfy" && b.enabled);

  return (
    <div className="p-4 sm:p-6 space-y-4 max-w-3xl mx-auto">
      <div className="flex items-center justify-between gap-3">
        <h1 className="text-xl sm:text-2xl font-semibold">
          My Notification Routes
        </h1>
        <Button variant="ghost" size="sm" onClick={() => navigate("/notifications")}>
          ← In-app notifications
        </Button>
      </div>

      {error && (
        <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      {loading && !routes ? (
        <div className="text-sm text-muted-foreground">Loading…</div>
      ) : (routes?.length ?? 0) === 0 ? (
        <EmptyStateHero
          ntfyAvailable={ntfyAvailable}
          onAddRoute={() => setShowAdd(true)}
          onQuickSetupNtfy={async () => {
            const topic = `gilbert-${Math.random()
              .toString(36)
              .slice(2, 10)}`;
            try {
              await rpc({
                type: "push.routes.create",
                backend_name: "ntfy",
                label: "Phone via ntfy",
                destination_data: { topic, server: "" },
                urgency_floor: "normal",
              });
              reload();
              alert(
                `ntfy route created with topic ${topic}. Subscribe to it in the ntfy app and click "Send test" on the new route.`,
              );
            } catch (err) {
              setError(
                err instanceof Error ? err.message : "Could not create route",
              );
            }
          }}
        />
      ) : (
        <div className="space-y-3">
          {routes!.map((route) => (
            <RouteCard
              key={route._id}
              route={route}
              onToggle={() => handleToggleEnabled(route)}
              onTest={() => handleSendTest(route)}
              onDelete={() => handleDelete(route)}
              onEdit={() => setEditing(route)}
            />
          ))}
        </div>
      )}

      {(showAdd || (routes?.length ?? 0) > 0) && (
        <div className="flex justify-end">
          <Button onClick={() => setShowAdd(true)}>+ Add Route</Button>
        </div>
      )}

      {(showAdd || editing) && (
        <RouteForm
          backends={backends}
          knownSources={knownSources}
          existing={editing}
          onClose={() => {
            setShowAdd(false);
            setEditing(null);
          }}
          onSaved={() => {
            setShowAdd(false);
            setEditing(null);
            reload();
          }}
        />
      )}
    </div>
  );
}

function EmptyStateHero({
  ntfyAvailable,
  onQuickSetupNtfy,
  onAddRoute,
}: {
  ntfyAvailable: boolean;
  onQuickSetupNtfy: () => void;
  onAddRoute: () => void;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>You don't have any notification routes yet</CardTitle>
        <CardDescription>
          Gilbert can deliver important notifications to your phone or chat
          even when you're not at this tab. The fastest path is ntfy — free,
          no signup, just a QR code and the ntfy app.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap gap-2">
          {ntfyAvailable && (
            <Button onClick={onQuickSetupNtfy}>
              ► Quick setup: ntfy on my phone
            </Button>
          )}
          <Button variant="outline" onClick={onAddRoute}>
            Other options…
          </Button>
        </div>
        <div className="text-xs text-muted-foreground">
          Other supported providers: Pushover (one-time-paid mobile app),
          Discord channel webhooks, Telegram bots. Each shows up here once
          your admin has configured the matching plugin.
        </div>
      </CardContent>
    </Card>
  );
}

function RouteCard({
  route,
  onToggle,
  onTest,
  onDelete,
  onEdit,
}: {
  route: RouteRow;
  onToggle: () => void;
  onTest: () => void;
  onDelete: () => void;
  onEdit: () => void;
}) {
  const lastDelivered = route.last_delivered_at
    ? new Date(route.last_delivered_at).toLocaleString()
    : "never";
  const urgencyLabel = {
    info: "Info or higher",
    normal: "Normal or higher",
    urgent: "Urgent only",
  }[route.urgency_floor];
  const sourceClause = route.source_allow.length
    ? `only: ${route.source_allow.join(", ")}`
    : route.source_deny.length
      ? `not: ${route.source_deny.join(", ")}`
      : "all";
  const quiet = route.quiet_hours_start
    ? `${route.quiet_hours_start}–${route.quiet_hours_end ?? "?"}`
    : "off";
  return (
    <Card className={route.enabled ? "" : "opacity-60"}>
      <CardHeader className="flex-row items-start justify-between gap-3">
        <div>
          <CardTitle className="text-base">{route.label}</CardTitle>
          <CardDescription>
            via <span className="font-mono">{route.backend_name}</span>
          </CardDescription>
        </div>
        <div className="flex items-center gap-2">
          <Button size="sm" variant="ghost" onClick={onToggle}>
            {route.enabled ? "Disable" : "Enable"}
          </Button>
          <Button size="sm" variant="ghost" onClick={onEdit}>
            Edit
          </Button>
          <Button size="sm" variant="ghost" onClick={onTest}>
            Test
          </Button>
          <Button size="sm" variant="ghost" onClick={onDelete}>
            ×
          </Button>
        </div>
      </CardHeader>
      <CardContent className="text-xs text-muted-foreground space-y-0.5">
        <div>Send when urgency is at least: {urgencyLabel}</div>
        <div>Sources: {sourceClause}</div>
        <div>
          Quiet hours: {quiet}
          {route.quiet_hours_timezone
            ? ` (${route.quiet_hours_timezone})`
            : ""}
        </div>
        <div>Last delivered: {lastDelivered}</div>
      </CardContent>
    </Card>
  );
}

function RouteForm({
  backends,
  knownSources,
  existing,
  onClose,
  onSaved,
}: {
  backends: BackendMeta[];
  knownSources: string[];
  existing: RouteRow | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { rpc } = useWebSocket();
  const [backendName, setBackendName] = useState<string>(
    existing?.backend_name ?? backends[0]?.name ?? "",
  );
  const [label, setLabel] = useState<string>(existing?.label ?? "");
  const [destData, setDestData] = useState<Record<string, string>>(
    existing
      ? Object.fromEntries(
          Object.entries(existing.destination_data || {}).map(([k, v]) => [
            k,
            String(v ?? ""),
          ]),
        )
      : {},
  );
  const [urgencyFloor, setUrgencyFloor] = useState<RouteRow["urgency_floor"]>(
    existing?.urgency_floor ?? "normal",
  );
  const [sourceAllow, setSourceAllow] = useState<string>(
    (existing?.source_allow ?? []).join(", "),
  );
  const [sourceDeny, setSourceDeny] = useState<string>(
    (existing?.source_deny ?? []).join(", "),
  );
  const [quietStart, setQuietStart] = useState<string>(
    existing?.quiet_hours_start ?? "",
  );
  const [quietEnd, setQuietEnd] = useState<string>(
    existing?.quiet_hours_end ?? "",
  );
  const [quietTz, setQuietTz] = useState<string>(
    existing?.quiet_hours_timezone ?? "",
  );
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState("");
  const [testToast, setTestToast] = useState("");

  const browserTz = useMemo(detectBrowserTz, []);
  const selectedBackend = backends.find((b) => b.name === backendName);

  // When changing backend, reset the destination_data fields to that
  // backend's known keys.
  useEffect(() => {
    if (!selectedBackend) return;
    setDestData((prev) => {
      const next: Record<string, string> = {};
      for (const p of selectedBackend.destination_params) {
        next[p.key] = prev[p.key] ?? String(p.default ?? "");
      }
      return next;
    });
  }, [selectedBackend]);

  const splitCsv = (s: string): string[] =>
    s
      .split(",")
      .map((x) => x.trim())
      .filter(Boolean);

  const handleTestUnsaved = async () => {
    setTestToast("");
    try {
      const result = await rpc<{
        ok: boolean;
        status: string;
        message: string;
      }>({
        type: "push.routes.test_unsaved",
        backend_name: backendName,
        destination_data: destData,
      });
      if (result.ok) {
        setTestToast("Test sent. Check your device.");
      } else {
        setTestToast(`Test failed: ${result.message}`);
      }
    } catch (err) {
      setTestToast(err instanceof Error ? err.message : "Test failed");
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setFormError("");
    setSubmitting(true);
    try {
      const payload: Record<string, unknown> = {
        backend_name: backendName,
        label,
        destination_data: destData,
        urgency_floor: urgencyFloor,
        source_allow: splitCsv(sourceAllow),
        source_deny: splitCsv(sourceDeny),
        quiet_hours_start: quietStart || null,
        quiet_hours_end: quietEnd || null,
        quiet_hours_timezone: quietTz || null,
      };
      if (existing) {
        await rpc({ type: "push.routes.update", route_id: existing._id, ...payload });
      } else {
        await rpc({ type: "push.routes.create", ...payload });
      }
      onSaved();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "Could not save");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>{existing ? "Edit route" : "Add route"}</CardTitle>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} className="space-y-3">
          {formError && (
            <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {formError}
            </div>
          )}
          {testToast && (
            <div className="rounded-md bg-muted/40 px-3 py-2 text-sm">
              {testToast}
            </div>
          )}
          <div className="space-y-1.5">
            <Label htmlFor="rf-backend">Provider</Label>
            <select
              id="rf-backend"
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
              value={backendName}
              onChange={(e) => setBackendName(e.target.value)}
              disabled={!!existing}
            >
              {backends.map((b) => (
                <option key={b.name} value={b.name} disabled={!b.enabled}>
                  {b.label}
                  {b.name === "ntfy" ? "  · Recommended" : ""}
                  {!b.enabled ? "  · disabled" : ""}
                </option>
              ))}
            </select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="rf-label">Label</Label>
            <Input
              id="rf-label"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              required
            />
          </div>
          {selectedBackend?.destination_params.map((p) => (
            <div key={p.key} className="space-y-1.5">
              <Label htmlFor={`rf-dest-${p.key}`}>{p.key}</Label>
              <Input
                id={`rf-dest-${p.key}`}
                type={p.sensitive ? "password" : "text"}
                placeholder={p.description}
                value={destData[p.key] ?? ""}
                onChange={(e) =>
                  setDestData({ ...destData, [p.key]: e.target.value })
                }
              />
              <p className="text-xs text-muted-foreground">{p.description}</p>
            </div>
          ))}
          <div className="space-y-1.5">
            <Label htmlFor="rf-urgency">Send when urgency is at least</Label>
            <select
              id="rf-urgency"
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
              value={urgencyFloor}
              onChange={(e) =>
                setUrgencyFloor(e.target.value as RouteRow["urgency_floor"])
              }
            >
              <option value="info">Info</option>
              <option value="normal">Normal</option>
              <option value="urgent">Urgent</option>
            </select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="rf-allow">Only deliver from these sources</Label>
            <Input
              id="rf-allow"
              placeholder="comma-separated; empty = all"
              value={sourceAllow}
              onChange={(e) => setSourceAllow(e.target.value)}
            />
            {knownSources.length > 0 && (
              <p className="text-xs text-muted-foreground">
                Recent sources: {knownSources.join(", ")}
              </p>
            )}
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="rf-deny">Never deliver from these sources</Label>
            <Input
              id="rf-deny"
              placeholder="comma-separated; empty = none"
              value={sourceDeny}
              onChange={(e) => setSourceDeny(e.target.value)}
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label htmlFor="rf-q-start">Quiet hours start</Label>
              <Input
                id="rf-q-start"
                placeholder="22:00"
                value={quietStart}
                onChange={(e) => setQuietStart(e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="rf-q-end">Quiet hours end</Label>
              <Input
                id="rf-q-end"
                placeholder="07:00"
                value={quietEnd}
                onChange={(e) => setQuietEnd(e.target.value)}
              />
            </div>
          </div>
          <div>
            <button
              type="button"
              className="text-xs text-muted-foreground underline"
              onClick={() => setShowAdvanced((v) => !v)}
            >
              {showAdvanced ? "Hide" : "Show"} advanced
            </button>
          </div>
          {showAdvanced && (
            <div className="space-y-1.5">
              <Label htmlFor="rf-tz">Quiet-hours timezone (IANA)</Label>
              <Input
                id="rf-tz"
                placeholder={`e.g. ${browserTz}`}
                value={quietTz}
                onChange={(e) => setQuietTz(e.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                Leave empty to use your account timezone (or the server's
                if you haven't set one).
              </p>
            </div>
          )}
          <div className="flex flex-wrap gap-2 pt-2">
            <Button type="submit" disabled={submitting}>
              {submitting ? "Saving…" : "Save"}
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={submitting}
              onClick={handleTestUnsaved}
            >
              Send test
            </Button>
            <Button type="button" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

