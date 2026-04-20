import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  PlayIcon,
  PauseIcon,
  Trash2Icon,
  ChevronRightIcon,
  ChevronDownIcon,
  AlertCircleIcon,
  RefreshCcwIcon,
} from "lucide-react";
import type { Job, Schedule, ScheduledAction } from "@/types/scheduler";

/** "01:00:30" → "01:00"; empty stays empty. Trims the seconds so the
 *  UI display matches how users enter times (HH:MM) unless seconds
 *  are actually non-zero. */
function compactTime(hhmmss: string): string {
  if (!hhmmss) return "";
  const parts = hhmmss.split(":");
  if (parts.length >= 2 && (parts[2] ?? "00") === "00") {
    return `${parts[0]}:${parts[1]}`;
  }
  return hhmmss;
}

/** "2099-06-01T01:00:00" → "Jun 1 01:00". Falls back to the raw
 *  string if it can't be parsed so we never swallow a value the
 *  backend considers valid. */
function compactDatetime(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const month = d.toLocaleString("en-US", { month: "short" });
  const day = d.getDate();
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${month} ${day} ${hh}:${mm}`;
}

/** Human-readable summary of a schedule, including any bounds/window
 *  suffix so a single glance at the row tells the user when this job
 *  is allowed to fire. */
function formatSchedule(schedule: Schedule): string {
  const base = formatScheduleBase(schedule);
  const suffix = formatScheduleBounds(schedule);
  return suffix ? `${base} ${suffix}` : base;
}

function formatScheduleBase(schedule: Schedule): string {
  switch (schedule.type) {
    case "interval": {
      const s = schedule.interval_seconds;
      if (s < 60) return `every ${s}s`;
      if (s < 3600) return `every ${Math.round(s / 60)}m`;
      if (s < 86400) return `every ${Math.round(s / 3600)}h`;
      return `every ${Math.round(s / 86400)}d`;
    }
    case "daily":
      return `daily at ${String(schedule.hour).padStart(2, "0")}:${String(
        schedule.minute,
      ).padStart(2, "0")}`;
    case "hourly":
      return `hourly at :${String(schedule.minute).padStart(2, "0")}`;
    case "once": {
      const s = schedule.interval_seconds;
      if (s < 60) return `once in ${s}s`;
      if (s < 3600) return `once in ${Math.round(s / 60)}m`;
      return `once in ${Math.round(s / 3600)}h`;
    }
    default:
      return schedule.type;
  }
}

function formatScheduleBounds(schedule: Schedule): string {
  const parts: string[] = [];
  const ws = compactTime(schedule.window_start_time);
  const we = compactTime(schedule.window_end_time);
  if (ws && we) {
    parts.push(`${ws}–${we} daily`);
  }
  const start = compactDatetime(schedule.start_at);
  const end = compactDatetime(schedule.end_at);
  if (start && end) {
    parts.push(`from ${start} to ${end}`);
  } else if (start) {
    parts.push(`from ${start}`);
  } else if (end) {
    parts.push(`until ${end}`);
  }
  return parts.join(" ");
}

/** Short summary of what an action does — fits in a table cell. */
function formatActionSummary(action: ScheduledAction): string {
  if (action.type === "tool") {
    return action.tool || "(no tool)";
  }
  if (action.type === "ai_prompt") {
    const prompt = action.ai_prompt.trim();
    if (!prompt) return "(empty prompt)";
    return prompt.length > 60 ? prompt.slice(0, 57) + "..." : prompt;
  }
  return action.message ? `event: ${action.message.slice(0, 50)}` : "event";
}

function actionBadgeVariant(
  type: ScheduledAction["type"],
): "default" | "secondary" | "outline" {
  switch (type) {
    case "tool":
      return "default";
    case "ai_prompt":
      return "secondary";
    default:
      return "outline";
  }
}

/** Format "2026-04-12T10:55:30.123456+00:00" as "Apr 12, 10:55:30". */
function formatLastRun(iso: string): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    const month = d.toLocaleString("en-US", { month: "short" });
    const day = d.getDate();
    const time = d.toLocaleTimeString("en-US", { hour12: false });
    return `${month} ${day}, ${time}`;
  } catch {
    return iso;
  }
}

export function SchedulerPage() {
  const queryClient = useQueryClient();
  const api = useWsApi();
  const { connected } = useWebSocket();

  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [showSystem, setShowSystem] = useState(true);

  const { data: jobs, isLoading, refetch } = useQuery({
    queryKey: ["scheduler-jobs"],
    queryFn: () => api.listJobs(true),
    enabled: connected,
    refetchInterval: 10_000,
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["scheduler-jobs"] });

  const enableMutation = useMutation({
    mutationFn: api.enableJob,
    onSuccess: invalidate,
  });

  const disableMutation = useMutation({
    mutationFn: api.disableJob,
    onSuccess: invalidate,
  });

  const removeMutation = useMutation({
    mutationFn: api.removeJob,
    onSuccess: invalidate,
  });

  const runNowMutation = useMutation({
    mutationFn: api.runJobNow,
    onSuccess: invalidate,
  });

  function toggleExpand(name: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(name)) {
        next.delete(name);
      } else {
        next.add(name);
      }
      return next;
    });
  }

  if (isLoading) {
    return <LoadingSpinner text="Loading scheduled jobs..." className="p-4" />;
  }

  const filteredJobs = (jobs ?? []).filter(
    (j) => showSystem || j.type === "user",
  );
  const userCount = (jobs ?? []).filter((j) => j.type === "user").length;
  const systemCount = (jobs ?? []).filter((j) => j.type === "system").length;

  return (
    <div className="p-4 sm:p-6 max-w-6xl mx-auto">
      <div className="flex flex-wrap items-start justify-between gap-3 mb-4">
        <div>
          <h1 className="text-xl sm:text-2xl font-semibold">Scheduler</h1>
          <p className="text-sm text-muted-foreground">
            {userCount} user {userCount === 1 ? "job" : "jobs"} · {systemCount}{" "}
            system {systemCount === 1 ? "job" : "jobs"}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant={showSystem ? "secondary" : "outline"}
            size="sm"
            onClick={() => setShowSystem((v) => !v)}
          >
            {showSystem ? "Hide system" : "Show system"}
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => refetch()}
            title="Refresh"
          >
            <RefreshCcwIcon className="size-4" />
          </Button>
        </div>
      </div>

      {filteredJobs.length === 0 ? (
        <Card>
          <CardContent className="p-8 text-center text-muted-foreground">
            No scheduled jobs.
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent className="p-0">
            <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b">
                  <th className="w-8"></th>
                  <th className="px-3 py-2 text-left font-medium">Name</th>
                  <th className="hidden sm:table-cell px-3 py-2 text-left font-medium">Type</th>
                  <th className="hidden md:table-cell px-3 py-2 text-left font-medium">Schedule</th>
                  <th className="hidden lg:table-cell px-3 py-2 text-left font-medium">Action</th>
                  <th className="px-3 py-2 text-left font-medium">State</th>
                  <th className="hidden md:table-cell px-3 py-2 text-left font-medium">Last run</th>
                  <th className="hidden lg:table-cell px-3 py-2 text-left font-medium">Runs</th>
                  <th className="px-3 py-2 w-32"></th>
                </tr>
              </thead>
              <tbody>
                {filteredJobs.map((job) => (
                  <JobRow
                    key={job.name}
                    job={job}
                    expanded={expanded.has(job.name)}
                    onToggleExpand={() => toggleExpand(job.name)}
                    onEnable={() => enableMutation.mutate(job.name)}
                    onDisable={() => disableMutation.mutate(job.name)}
                    onRemove={() => removeMutation.mutate(job.name)}
                    onRunNow={() => runNowMutation.mutate(job.name)}
                    mutating={
                      enableMutation.isPending ||
                      disableMutation.isPending ||
                      removeMutation.isPending ||
                      runNowMutation.isPending
                    }
                  />
                ))}
              </tbody>
            </table>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

interface JobRowProps {
  job: Job;
  expanded: boolean;
  onToggleExpand: () => void;
  onEnable: () => void;
  onDisable: () => void;
  onRemove: () => void;
  onRunNow: () => void;
  mutating: boolean;
}

function JobRow({
  job,
  expanded,
  onToggleExpand,
  onEnable,
  onDisable,
  onRemove,
  onRunNow,
  mutating,
}: JobRowProps) {
  const isSystem = job.type === "system";

  return (
    <>
      <tr
        className="border-b hover:bg-muted/30 cursor-pointer"
        onClick={onToggleExpand}
      >
        <td className="px-2 py-2">
          {expanded ? (
            <ChevronDownIcon className="size-4 text-muted-foreground" />
          ) : (
            <ChevronRightIcon className="size-4 text-muted-foreground" />
          )}
        </td>
        <td className="px-3 py-2 font-medium break-words">{job.name}</td>
        <td className="hidden sm:table-cell px-3 py-2">
          <Badge
            variant={isSystem ? "secondary" : "outline"}
            className="text-xs"
          >
            {job.type}
          </Badge>
        </td>
        <td className="hidden md:table-cell px-3 py-2 text-muted-foreground whitespace-nowrap">
          {formatSchedule(job.schedule)}
        </td>
        <td className="hidden lg:table-cell px-3 py-2">
          <Badge
            variant={actionBadgeVariant(job.action.type)}
            className="text-xs"
          >
            {job.action.type}
          </Badge>
          <span className="ml-2 text-muted-foreground">
            {formatActionSummary(job.action)}
          </span>
        </td>
        <td className="px-3 py-2">
          <JobStateBadge state={job.state} enabled={job.enabled} error={job.last_error} />
        </td>
        <td className="hidden md:table-cell px-3 py-2 text-muted-foreground tabular-nums whitespace-nowrap">
          {formatLastRun(job.last_run)}
        </td>
        <td className="hidden lg:table-cell px-3 py-2 text-muted-foreground tabular-nums">
          {job.run_count}
        </td>
        <td className="px-3 py-2" onClick={(e) => e.stopPropagation()}>
          <div className="flex gap-1 justify-end">
            <Button
              variant="ghost"
              size="icon-xs"
              title="Run now"
              disabled={mutating}
              onClick={onRunNow}
            >
              <PlayIcon className="size-3" />
            </Button>
            {job.enabled ? (
              <Button
                variant="ghost"
                size="icon-xs"
                title="Pause"
                disabled={mutating}
                onClick={onDisable}
              >
                <PauseIcon className="size-3" />
              </Button>
            ) : (
              <Button
                variant="ghost"
                size="icon-xs"
                title="Resume"
                disabled={mutating}
                onClick={onEnable}
              >
                <PlayIcon className="size-3" />
              </Button>
            )}
            <Button
              variant="ghost"
              size="icon-xs"
              className="text-destructive"
              title={
                isSystem
                  ? "System jobs cannot be removed"
                  : "Cancel"
              }
              disabled={isSystem || mutating}
              onClick={onRemove}
            >
              <Trash2Icon className="size-3" />
            </Button>
          </div>
        </td>
      </tr>

      {expanded && (
        <tr className="border-b bg-muted/20">
          <td></td>
          <td colSpan={8} className="px-3 py-3">
            <JobDetails job={job} />
          </td>
        </tr>
      )}
    </>
  );
}

function JobStateBadge({
  state,
  enabled,
  error,
}: {
  state: string;
  enabled: boolean;
  error: string;
}) {
  if (!enabled) {
    return (
      <Badge variant="outline" className="text-xs">
        paused
      </Badge>
    );
  }
  if (error) {
    return (
      <Badge variant="destructive" className="text-xs gap-1">
        <AlertCircleIcon className="size-3" />
        failed
      </Badge>
    );
  }
  if (state === "running") {
    return (
      <Badge variant="default" className="text-xs">
        running
      </Badge>
    );
  }
  return (
    <Badge variant="secondary" className="text-xs">
      {state}
    </Badge>
  );
}

function JobDetails({ job }: { job: Job }) {
  const hasBounds =
    job.schedule.start_at ||
    job.schedule.end_at ||
    job.schedule.window_start_time ||
    job.schedule.window_end_time;

  return (
    <div className="space-y-3 text-xs">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <DetailField label="Owner" value={job.owner || "—"} />
        <DetailField label="Runs" value={String(job.run_count)} />
        <DetailField
          label="Last duration"
          value={
            job.last_duration_seconds
              ? `${job.last_duration_seconds.toFixed(2)}s`
              : "—"
          }
        />
        <DetailField label="State" value={job.state} />
      </div>

      {hasBounds && (
        // Only render the bounds block when at least one bound is set
        // so unbounded jobs (the common case) don't show a row full of
        // em-dashes that adds no information.
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <DetailField
            label="Start at"
            value={compactDatetime(job.schedule.start_at) || "—"}
          />
          <DetailField
            label="End at"
            value={compactDatetime(job.schedule.end_at) || "—"}
          />
          <DetailField
            label="Window start"
            value={compactTime(job.schedule.window_start_time) || "—"}
          />
          <DetailField
            label="Window end"
            value={compactTime(job.schedule.window_end_time) || "—"}
          />
        </div>
      )}

      {job.last_error && (
        <div className="rounded border border-destructive/50 bg-destructive/10 p-2">
          <div className="font-medium text-destructive mb-1">Last error</div>
          <pre className="whitespace-pre-wrap break-words">
            {job.last_error}
          </pre>
        </div>
      )}

      <div>
        <div className="font-medium mb-1">Action payload</div>
        <pre className="rounded bg-muted/50 p-2 overflow-x-auto">
          {JSON.stringify(job.action, null, 2)}
        </pre>
      </div>
    </div>
  );
}

function DetailField({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-muted-foreground uppercase tracking-wider text-[10px]">
        {label}
      </div>
      <div className="font-medium tabular-nums">{value}</div>
    </div>
  );
}
