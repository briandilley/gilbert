import { useCallback, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ChevronRightIcon } from "lucide-react";
import { useAgentRuns } from "@/api/agents";
import { useEventBus } from "@/hooks/useEventBus";
import { Badge } from "@/components/ui/badge";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { timeAgo } from "@/lib/timeAgo";
import type { AgentRun, RunStatus } from "@/types/agent";

interface Props {
  agentId: string;
}

function formatDuration(startedAt: string, endedAt: string | null): string {
  if (!endedAt) return "—";
  const ms = new Date(endedAt).getTime() - new Date(startedAt).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "—";
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rs = s % 60;
  if (m < 60) return `${m}m ${rs}s`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return `${h}h ${rm}m`;
}

const STATUS_COLOR: Record<RunStatus, string> = {
  running: "bg-blue-500/15 text-blue-600 dark:text-blue-400",
  completed: "bg-green-500/15 text-green-600 dark:text-green-400",
  failed: "bg-destructive/15 text-destructive",
  timed_out: "bg-yellow-500/15 text-yellow-600 dark:text-yellow-400",
};

export function RunsTable({ agentId }: Props) {
  const queryClient = useQueryClient();
  const runsQuery = useAgentRuns(agentId, 100);
  const [openRunId, setOpenRunId] = useState<string | null>(null);
  const [triggerFilter, setTriggerFilter] = useState<string>("all");

  const invalidate = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ["agents", "runs", agentId] });
  }, [queryClient, agentId]);

  // Refresh on every backend lifecycle event for this agent.
  useEventBus("agent.run.started", invalidate);
  useEventBus("agent.run.completed", invalidate);

  const triggers = useMemo(() => {
    const set = new Set<string>();
    for (const r of runsQuery.data ?? []) set.add(r.triggered_by);
    return ["all", ...Array.from(set).sort()];
  }, [runsQuery.data]);

  const visibleRuns = useMemo(() => {
    const all = runsQuery.data ?? [];
    if (triggerFilter === "all") return all;
    return all.filter((r) => r.triggered_by === triggerFilter);
  }, [runsQuery.data, triggerFilter]);

  return (
    <div className="space-y-3">
      <div className="flex items-end gap-3">
        <div className="flex flex-col gap-1">
          <label htmlFor="run-trigger-filter" className="text-xs">
            Triggered by
          </label>
          <select
            id="run-trigger-filter"
            value={triggerFilter}
            onChange={(e) => setTriggerFilter(e.target.value)}
            className="h-8 rounded-md border border-input bg-transparent px-2 text-sm"
          >
            {triggers.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </div>
      </div>

      {runsQuery.isPending && <LoadingSpinner text="Loading runs…" />}

      {runsQuery.isError && (
        <div
          role="alert"
          className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          Failed to load runs.
        </div>
      )}

      {runsQuery.data && visibleRuns.length === 0 && (
        <div className="rounded-md border border-dashed px-4 py-8 text-center text-sm text-muted-foreground">
          No runs yet.
        </div>
      )}

      {visibleRuns.length > 0 && (
        <div className="overflow-x-auto rounded-md border">
          <table className="w-full text-sm">
            <thead className="bg-muted/40 text-left text-xs text-muted-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2 font-medium">Trigger</th>
                <th className="px-3 py-2 font-medium">Started</th>
                <th className="px-3 py-2 font-medium">Duration</th>
                <th className="px-3 py-2 font-medium">Cost</th>
                <th className="px-3 py-2 font-medium">Rounds</th>
                <th className="px-3 py-2 font-medium">Tokens</th>
              </tr>
            </thead>
            <tbody>
              {visibleRuns.map((run) => {
                const open = openRunId === run._id;
                return (
                  <RunRow
                    key={run._id}
                    run={run}
                    open={open}
                    onToggle={() => setOpenRunId(open ? null : run._id)}
                  />
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function RunRow({
  run,
  open,
  onToggle,
}: {
  run: AgentRun;
  open: boolean;
  onToggle: () => void;
}) {
  const tokens = run.tokens_in + run.tokens_out;
  const detail =
    run.status === "failed"
      ? run.error || "(failed without error message)"
      : run.final_message_text || "(no final message)";
  const detailId = `run-detail-${run._id}`;
  return (
    <>
      <tr
        className="cursor-pointer border-t hover:bg-muted/40"
        onClick={onToggle}
      >
        <td className="px-3 py-2 align-middle">
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onToggle();
              }}
              aria-expanded={open}
              aria-controls={detailId}
              className="inline-flex items-center justify-center rounded p-1 hover:bg-muted"
            >
              <ChevronRightIcon
                className={`size-4 transition-transform ${open ? "rotate-90" : ""}`}
                aria-hidden
              />
              <span className="sr-only">
                {open ? "Collapse" : "Expand"} run details
              </span>
            </button>
            <Badge variant="outline" className={STATUS_COLOR[run.status]}>
              {run.status}
            </Badge>
          </div>
        </td>
        <td className="px-3 py-2 text-muted-foreground">{run.triggered_by}</td>
        <td className="px-3 py-2 text-muted-foreground whitespace-nowrap">
          {timeAgo(run.started_at)}
        </td>
        <td className="px-3 py-2 text-muted-foreground whitespace-nowrap">
          {formatDuration(run.started_at, run.ended_at)}
        </td>
        <td className="px-3 py-2 whitespace-nowrap">
          ${run.cost_usd.toFixed(2)}
        </td>
        <td className="px-3 py-2">{run.rounds_used}</td>
        <td className="px-3 py-2 text-muted-foreground whitespace-nowrap">
          {tokens.toLocaleString()}
        </td>
      </tr>
      {open && (
        <tr id={detailId} className="border-t bg-muted/20">
          <td colSpan={7} className="px-3 py-3">
            <div className="text-xs text-muted-foreground mb-1">
              {run.status === "failed" ? "Error" : "Final message"}
            </div>
            <pre className="whitespace-pre-wrap break-words text-sm">
              {detail}
            </pre>
          </td>
        </tr>
      )}
    </>
  );
}
