import { useCallback, useState } from "react";
import { Link, useParams, useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeftIcon,
  PlayIcon,
  Trash2Icon,
  PauseIcon,
  ZapIcon,
  MessageSquareIcon,
  CheckCircle2Icon,
} from "lucide-react";
import { useWebSocket } from "@/hooks/useWebSocket";
import { useEventBus } from "@/hooks/useEventBus";
import { useWsApi } from "@/hooks/useWsApi";
import { Button } from "@/components/ui/button";
import type { AgentRun, GoalStatus } from "@/types/agent";

const STATUS_BADGE: Record<GoalStatus, { label: string; cls: string }> = {
  enabled: { label: "Enabled", cls: "bg-green-500/10 text-green-700 dark:text-green-400" },
  disabled: { label: "Disabled", cls: "bg-yellow-500/10 text-yellow-700 dark:text-yellow-400" },
  completed: { label: "Completed", cls: "bg-blue-500/10 text-blue-700 dark:text-blue-400" },
};

const RUN_STATUS_CLS: Record<string, string> = {
  running: "bg-blue-500/10 text-blue-700 dark:text-blue-400",
  completed: "bg-green-500/10 text-green-700 dark:text-green-400",
  failed: "bg-red-500/10 text-red-700 dark:text-red-400",
};

function fmtDuration(start: string, end: string | null): string {
  if (!end) return "running";
  const ms = new Date(end).getTime() - new Date(start).getTime();
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${Math.round(ms / 100) / 10}s`;
  return `${Math.round(ms / 60000)}m`;
}

function fmtTimestamp(iso: string): string {
  return new Date(iso).toLocaleString();
}

export function AgentDetailPage() {
  const { goalId } = useParams<{ goalId: string }>();
  const navigate = useNavigate();
  const api = useWsApi();
  const queryClient = useQueryClient();
  const { connected } = useWebSocket();
  const [running, setRunning] = useState(false);

  const { data: goalResp, isLoading: goalLoading } = useQuery({
    queryKey: ["agent", "goal", goalId],
    queryFn: () => (goalId ? api.getGoal(goalId) : Promise.resolve(null)),
    enabled: connected && !!goalId,
  });

  const { data: runsResp } = useQuery({
    queryKey: ["agent", "runs", goalId],
    queryFn: () => (goalId ? api.listAgentRuns(goalId) : Promise.resolve(null)),
    enabled: connected && !!goalId,
  });

  const refresh = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ["agent"] });
  }, [queryClient]);

  useEventBus("agent.run.completed", refresh);
  useEventBus("agent.run.started", refresh);

  if (!goalId) return null;
  if (goalLoading) {
    return <div className="container mx-auto py-6 max-w-3xl text-muted-foreground">Loading…</div>;
  }
  if (!goalResp || !goalResp.ok || !goalResp.goal) {
    return (
      <div className="container mx-auto py-6 max-w-3xl">
        <Button variant="ghost" size="sm" onClick={() => navigate("/agents")}>
          <ArrowLeftIcon className="size-4 mr-1" /> Back
        </Button>
        <div className="text-center text-muted-foreground py-12">
          Goal not found.
        </div>
      </div>
    );
  }

  const goal = goalResp.goal;
  const runs: AgentRun[] = runsResp?.ok && runsResp.runs ? runsResp.runs : [];
  const badge = STATUS_BADGE[goal.status];

  const handleRunNow = async () => {
    setRunning(true);
    try {
      await api.runGoalNow(goal.id);
      refresh();
    } finally {
      setRunning(false);
    }
  };

  const handleToggle = async () => {
    if (goal.status === "completed") return;
    const next: GoalStatus = goal.status === "enabled" ? "disabled" : "enabled";
    await api.updateGoal(goal.id, { status: next });
    refresh();
  };

  const handleDelete = async () => {
    if (!confirm("Delete this goal? Its run history will also be deleted.")) return;
    await api.deleteGoal(goal.id);
    navigate("/agents");
  };

  return (
    <div className="container mx-auto py-6 max-w-4xl">
      <Button
        variant="ghost"
        size="sm"
        onClick={() => navigate("/agents")}
        className="mb-3"
      >
        <ArrowLeftIcon className="size-4 mr-1" /> Agents
      </Button>

      <div className="flex items-start justify-between gap-4 mb-4">
        <div className="min-w-0">
          <h1 className="text-2xl font-semibold flex items-center gap-2 flex-wrap">
            {goal.name}
            <span className={`inline-flex items-center gap-1 rounded px-2 py-0.5 text-xs ${badge.cls}`}>
              {badge.label}
            </span>
          </h1>
          <div className="text-sm text-muted-foreground mt-1">
            Profile: <code className="font-mono">{goal.profile_id}</code>
            {" · "}
            {goal.run_count} run{goal.run_count === 1 ? "" : "s"}
          </div>
          {goal.completed_reason ? (
            <div className="mt-2 text-sm flex items-start gap-2 text-blue-700 dark:text-blue-400">
              <CheckCircle2Icon className="size-4 mt-0.5 shrink-0" />
              <span>Completed: {goal.completed_reason}</span>
            </div>
          ) : null}
        </div>

        <div className="flex items-center gap-1 shrink-0">
          <Button
            variant="default"
            size="sm"
            onClick={handleRunNow}
            disabled={running || goal.status !== "enabled"}
          >
            <PlayIcon className="size-4 mr-1" />
            {running ? "Running…" : "Run now"}
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={handleToggle}
            disabled={goal.status === "completed"}
          >
            {goal.status === "enabled" ? (
              <>
                <PauseIcon className="size-4 mr-1" /> Disable
              </>
            ) : (
              <>
                <ZapIcon className="size-4 mr-1" /> Enable
              </>
            )}
          </Button>
          <Button variant="ghost" size="icon-sm" onClick={handleDelete} title="Delete">
            <Trash2Icon className="size-4" />
          </Button>
        </div>
      </div>

      <div className="rounded-md border p-4 mb-6">
        <div className="text-sm font-medium mb-2">Instruction</div>
        <pre className="whitespace-pre-wrap text-sm font-sans text-muted-foreground">
          {goal.instruction}
        </pre>
      </div>

      {goal.conversation_id ? (
        <div className="mb-6">
          <Link
            to={`/chat?conversation=${goal.conversation_id}`}
            className="inline-flex items-center gap-2 text-sm text-blue-600 hover:underline"
          >
            <MessageSquareIcon className="size-4" />
            Open agent activity in chat
          </Link>
        </div>
      ) : (
        <div className="mb-6 text-sm text-muted-foreground">
          No conversation yet — run the goal at least once to start one.
        </div>
      )}

      <h2 className="text-lg font-semibold mb-2">Runs</h2>
      {runs.length === 0 ? (
        <div className="rounded-md border text-center text-muted-foreground py-8">
          No runs yet.
        </div>
      ) : (
        <div className="rounded-md border divide-y">
          {runs.map((r) => (
            <div key={r.id} className="px-4 py-3 flex items-start gap-3">
              <span
                className={`inline-flex items-center rounded px-2 py-0.5 text-xs ${
                  RUN_STATUS_CLS[r.status] ?? RUN_STATUS_CLS.failed
                }`}
              >
                {r.status}
              </span>
              <div className="flex-1 min-w-0">
                <div className="text-sm break-words">
                  {r.final_message_text ?? (r.error ? `Error: ${r.error}` : "(no output)")}
                </div>
                <div className="text-xs text-muted-foreground mt-1 flex items-center gap-3 flex-wrap">
                  <span>{fmtTimestamp(r.started_at)}</span>
                  <span>·</span>
                  <span>{fmtDuration(r.started_at, r.ended_at)}</span>
                  <span>·</span>
                  <span>triggered by {r.triggered_by}</span>
                  {r.tokens_in + r.tokens_out > 0 ? (
                    <>
                      <span>·</span>
                      <span>{r.tokens_in + r.tokens_out} tokens</span>
                    </>
                  ) : null}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
