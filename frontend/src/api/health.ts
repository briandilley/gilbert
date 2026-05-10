/**
 * Health-service REST API client.
 *
 * Mirrors the per-user account routes under ``/api/health/me/*`` and
 * the per-user audit-log filter at ``/api/health/me/audit-log``.
 * Admin routes live under ``/api/health/admin/*`` and are not
 * exposed here — admin pages call them directly with explicit role
 * gating.
 */

import { apiFetch } from "./client";

export interface HealthLink {
  backend_name: string;
  enabled: boolean;
  last_sync_at: string;
  last_sync_error: string;
  last_delivery_at: string;
  webhook_token_last4: string;
  supports_webhook: boolean;
}

export interface DailySummary {
  user_id: string;
  local_date: string;
  summary_text: string;
  metrics_snapshot: Record<string, number>;
  flags: string[];
  generated_at: string;
}

export interface HealthMetric {
  id: string;
  user_id: string;
  backend: string;
  metric_type: string;
  value: number;
  unit: string;
  recorded_at: string;
  ingested_at: string;
  source_event_id: string;
  extra: Record<string, string>;
}

export interface DeleteAllPreview {
  metric_count: number;
  earliest_recorded_at: string;
  latest_recorded_at: string;
  backends: string[];
  summaries_count: number;
  audit_count: number;
}

export interface AuditRow {
  id: string;
  kind: string;
  actor_user_id: string;
  target_user_id: string;
  accessed_at: string;
  metric_types: string[];
  // Backends touched (populated for ``self_delete_all``; empty for
  // cross-user reads). Spec §4.5 — separate field from metric_types.
  backends?: string[];
  period_start: string;
  period_end: string;
}

export async function listHealthLinks(): Promise<HealthLink[]> {
  const result = await apiFetch<{ items: HealthLink[] }>(
    "/api/health/me/links",
  );
  return result.items ?? [];
}

export async function fetchLatestSummary(): Promise<DailySummary | null> {
  const result = await apiFetch<{ summary: DailySummary | null }>(
    "/api/health/me/summary",
  );
  return result.summary;
}

export async function fetchHealthMetrics(params?: {
  metric_type?: string;
  since?: string;
  until?: string;
}): Promise<HealthMetric[]> {
  const qs = new URLSearchParams();
  if (params?.metric_type) qs.set("metric_type", params.metric_type);
  if (params?.since) qs.set("since", params.since);
  if (params?.until) qs.set("until", params.until);
  const url = `/api/health/me/metrics${qs.toString() ? "?" + qs : ""}`;
  const result = await apiFetch<{ items: HealthMetric[] }>(url);
  return result.items ?? [];
}

export async function previewDeleteAll(): Promise<DeleteAllPreview> {
  return apiFetch<DeleteAllPreview>("/api/health/me/delete-all/preview");
}

export async function executeDeleteAll(): Promise<{
  deleted_metrics: number;
  disconnected_backends: string[];
  upstream_revoke_failures: string[];
}> {
  return apiFetch("/api/health/me/delete-all", {
    method: "POST",
    body: JSON.stringify({ confirm: "DELETE" }),
  });
}

export async function disconnectHealthBackend(
  backend: string,
): Promise<void> {
  await apiFetch(
    `/api/health/me/disconnect/${encodeURIComponent(backend)}`,
    { method: "POST" },
  );
}

export async function listMyAuditLog(): Promise<AuditRow[]> {
  const result = await apiFetch<{ items: AuditRow[] }>(
    "/api/health/me/audit-log",
  );
  return result.items ?? [];
}

