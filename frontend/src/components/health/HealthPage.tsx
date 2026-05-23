/**
 * /health page — per-user metrics view.
 *
 * Shows:
 * - Today's summary (latest persisted ``DailySummary``)
 * - Connected sources panel (delegates to plugin-shipped panels via
 *   ``account.extensions`` slot — same way the Account page does it)
 * - A simple list of recent metrics (last 7 days)
 * - Right-to-delete entry point at the bottom (preview-then-DELETE
 *   confirm dialog)
 *
 * Per spec §17.2 v1 ships a basic "latest + last 7 days" table. Real
 * graphs are v2.
 */

import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  type DailySummary,
  type DeleteAllPreview,
  type HealthMetric,
  executeDeleteAll,
  fetchHealthMetrics,
  fetchLatestSummary,
  listHealthLinks,
  previewDeleteAll,
  type HealthLink,
} from "@/api/health";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export function HealthPage() {
  const navigate = useNavigate();
  const [summary, setSummary] = useState<DailySummary | null>(null);
  const [metrics, setMetrics] = useState<HealthMetric[]>([]);
  const [links, setLinks] = useState<HealthLink[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string>("");
  const [deleteOpen, setDeleteOpen] = useState<boolean>(false);
  const [preview, setPreview] = useState<DeleteAllPreview | null>(null);
  const [confirmText, setConfirmText] = useState<string>("");
  const [deleting, setDeleting] = useState<boolean>(false);

  useEffect(() => {
    Promise.all([
      fetchLatestSummary(),
      fetchHealthMetrics({ since: weekAgoIso() }),
      listHealthLinks(),
    ])
      .then(([s, m, l]) => {
        setSummary(s);
        setMetrics(m);
        setLinks(l);
      })
      .catch((err) => {
        setError(
          err instanceof Error ? err.message : "Could not load health data",
        );
      })
      .finally(() => setLoading(false));
  }, []);

  const byMetricType = useMemo(() => {
    const map: Record<string, HealthMetric[]> = {};
    for (const m of metrics) {
      (map[m.metric_type] ?? (map[m.metric_type] = [])).push(m);
    }
    for (const list of Object.values(map)) {
      list.sort((a, b) => b.recorded_at.localeCompare(a.recorded_at));
    }
    return map;
  }, [metrics]);

  const handleOpenDelete = async () => {
    setError("");
    try {
      const p = await previewDeleteAll();
      setPreview(p);
      setConfirmText("");
      setDeleteOpen(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not preview delete");
    }
  };

  const handleConfirmDelete = async () => {
    if (confirmText !== "DELETE") return;
    setDeleting(true);
    try {
      await executeDeleteAll();
      setDeleteOpen(false);
      // Reload everything from scratch.
      const [s, m, l] = await Promise.all([
        fetchLatestSummary(),
        fetchHealthMetrics({ since: weekAgoIso() }),
        listHealthLinks(),
      ]);
      setSummary(s);
      setMetrics(m);
      setLinks(l);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not delete");
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div className="container mx-auto py-6 max-w-3xl space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Health</h1>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => navigate("/account/health/audit-log")}
        >
          View audit log
        </Button>
      </div>

      {error && (
        <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Today's summary</CardTitle>
          <CardDescription>
            Generated each morning from yesterday's metrics — non-clinical
            by design.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {loading ? (
            <p className="text-sm text-muted-foreground">Loading…</p>
          ) : summary ? (
            <>
              <p className="text-sm">{summary.summary_text || "(empty)"}</p>
              {summary.flags.length > 0 && (
                <p className="text-xs text-muted-foreground mt-2">
                  Flags: {summary.flags.join(", ")}
                </p>
              )}
            </>
          ) : (
            <p className="text-sm text-muted-foreground">
              No summary yet. Connect a backend to start tracking.
            </p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Connected sources</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          {links.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No backends connected. Visit{" "}
              <button
                className="underline"
                onClick={() => navigate("/account")}
              >
                Account
              </button>{" "}
              to connect Apple Health, Withings, or the generic
              webhook.
            </p>
          ) : (
            <ul className="space-y-1 text-sm">
              {links.map((l) => (
                <li key={l.backend_name} className="flex justify-between">
                  <span>{l.backend_name}</span>
                  <span className="text-muted-foreground text-xs">
                    {l.enabled ? "enabled" : "disabled"}
                    {l.last_delivery_at &&
                      ` — last delivery ${l.last_delivery_at}`}
                    {l.last_sync_at && ` — last sync ${l.last_sync_at}`}
                    {l.last_sync_error && (
                      <span className="text-destructive">
                        {" "}
                        — {l.last_sync_error}
                      </span>
                    )}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Recent metrics (last 7 days)</CardTitle>
        </CardHeader>
        <CardContent>
          {Object.keys(byMetricType).length === 0 ? (
            <p className="text-sm text-muted-foreground">No metrics yet.</p>
          ) : (
            <table className="w-full text-sm">
              <thead className="text-left text-muted-foreground border-b">
                <tr>
                  <th className="py-1">Metric</th>
                  <th className="py-1">Latest</th>
                  <th className="py-1">When</th>
                  <th className="py-1">Count</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(byMetricType).map(([type, list]) => (
                  <tr key={type} className="border-b last:border-0">
                    <td className="py-1">{type}</td>
                    <td className="py-1">
                      {list[0].value} {list[0].unit}
                    </td>
                    <td className="py-1 text-xs text-muted-foreground">
                      {list[0].recorded_at}
                    </td>
                    <td className="py-1 text-xs">{list.length}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-destructive">
            Right to delete
          </CardTitle>
          <CardDescription>
            Erase every metric, summary, and backend link Gilbert holds
            for you. This is a two-step preview-then-confirm flow — the
            actual delete only fires when you type DELETE explicitly.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button variant="destructive" onClick={handleOpenDelete}>
            Delete all my health data
          </Button>
        </CardContent>
      </Card>

      <Dialog
        open={deleteOpen}
        onOpenChange={(o) => !deleting && setDeleteOpen(o)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete all your health data?</DialogTitle>
            <DialogDescription>
              This will:
              <ul className="list-disc pl-5 mt-2">
                <li>
                  Delete{" "}
                  <strong>{preview?.metric_count ?? 0}</strong> metric
                  reading(s) (
                  {preview?.earliest_recorded_at ?? ""} →{" "}
                  {preview?.latest_recorded_at ?? ""}).
                </li>
                <li>
                  Delete{" "}
                  <strong>{preview?.summaries_count ?? 0}</strong>{" "}
                  daily summaries.
                </li>
                <li>
                  Revoke and remove your connected backends:{" "}
                  <strong>
                    {(preview?.backends ?? []).join(", ") || "none"}
                  </strong>
                  .
                </li>
              </ul>
              <p className="mt-2">
                <strong>Withings retention disclosure:</strong> we'll
                revoke Gilbert's Withings access and delete every
                measurement we cached locally. Withings continues to
                retain the data on your behalf — to delete it from
                Withings, use Withings's own account-deletion flow.
              </p>
              <p className="mt-2">
                Your audit log of who accessed your data is{" "}
                <strong>not</strong> deleted — it's a permanent record
                of access.
              </p>
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <Label htmlFor="delete-confirm">
              Type the literal word DELETE to proceed:
            </Label>
            <Input
              id="delete-confirm"
              value={confirmText}
              onChange={(e) => setConfirmText(e.target.value)}
              placeholder="DELETE"
              autoComplete="off"
            />
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setDeleteOpen(false)}
              disabled={deleting}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={confirmText !== "DELETE" || deleting}
              onClick={handleConfirmDelete}
            >
              {deleting ? "Deleting…" : "Delete everything"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function weekAgoIso(): string {
  const t = new Date();
  t.setDate(t.getDate() - 7);
  return t.toISOString();
}

