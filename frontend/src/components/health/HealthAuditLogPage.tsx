/**
 * /account/health/audit-log — per-user audit log.
 *
 * Closes the loop opened by the cross-user-read notification:
 * the target user sees a "view your audit log" link in the
 * notification, lands here, can see exactly who accessed what and
 * when. Required closure on the privacy-loop story per spec §17.4.
 */

import { useEffect, useState } from "react";
import { type AuditRow, listMyAuditLog } from "@/api/health";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

export function HealthAuditLogPage() {
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string>("");

  useEffect(() => {
    listMyAuditLog()
      .then(setRows)
      .catch((err) => {
        setError(err instanceof Error ? err.message : "Could not load");
      })
      .finally(() => setLoading(false));
  }, []);

  return (
    <div className="container mx-auto py-6 max-w-3xl space-y-4">
      <h1 className="text-2xl font-semibold">Health access log</h1>

      <Card>
        <CardHeader>
          <CardTitle>Who accessed your health data</CardTitle>
          <CardDescription>
            Every cross-user read of your health metrics shows up
            here, sorted most recent first. Self-deletes also show
            up so you can confirm your "delete all" actually fired.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {error && (
            <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {error}
            </div>
          )}
          {loading ? (
            <p className="text-sm text-muted-foreground">Loading…</p>
          ) : rows.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No access events recorded.
            </p>
          ) : (
            <table className="w-full text-sm">
              <thead className="text-left text-muted-foreground border-b">
                <tr>
                  <th className="py-1">When</th>
                  <th className="py-1">Kind</th>
                  <th className="py-1">Actor</th>
                  <th className="py-1">What</th>
                  <th className="py-1">Window</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id} className="border-b last:border-0">
                    <td className="py-1 text-xs">
                      <span title={r.accessed_at}>{r.accessed_at}</span>
                    </td>
                    <td className="py-1">{r.kind}</td>
                    <td className="py-1">
                      <code>{r.actor_user_id}</code>
                    </td>
                    <td className="py-1 text-xs">
                      {r.metric_types.length > 0
                        ? r.metric_types.join(", ")
                        : r.backends && r.backends.length > 0
                          ? `backends: ${r.backends.join(", ")}`
                          : "—"}
                    </td>
                    <td className="py-1 text-xs">
                      {r.period_start && r.period_end
                        ? `${r.period_start} → ${r.period_end}`
                        : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

