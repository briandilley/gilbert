import { useState, useEffect, useCallback } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  PlusIcon,
  PlayIcon,
  CogIcon,
  Loader2Icon,
  WrenchIcon,
  ZapIcon,
} from "lucide-react";
import { useWebSocket } from "@/hooks/useWebSocket";
import { useEventBus } from "@/hooks/useEventBus";
import { useWsApi } from "@/hooks/useWsApi";
import { Button } from "@/components/ui/button";
import type { Goal, GoalStatus } from "@/types/agent";
import type {
  ConversationDetail,
  ChatTurn,
  ChatRound,
  ChatRoundTool,
} from "@/types/chat";

const STATUS_COLOR: Record<GoalStatus, string> = {
  enabled: "bg-green-500",
  disabled: "bg-yellow-500",
  completed: "bg-blue-500",
};

/**
 * Top-level agent page: sidebar of user's goals (with run/pause/settings),
 * main panel renders the selected goal's conversation flat — every
 * turn and tool round in chronological order, no rollups.
 */
export function AgentChatPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const api = useWsApi();
  const queryClient = useQueryClient();
  const { connected } = useWebSocket();

  const selectedGoalId = searchParams.get("goal") || "";

  const { data: goals } = useQuery({
    queryKey: ["agent", "goals"],
    queryFn: api.listGoals,
    enabled: connected,
  });

  const refresh = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ["agent"] });
    queryClient.invalidateQueries({ queryKey: ["agent-conv"] });
  }, [queryClient]);

  useEventBus("agent.run.started", refresh);
  useEventBus("agent.run.completed", refresh);

  // Auto-select first goal when nothing is selected and goals exist
  useEffect(() => {
    if (!selectedGoalId && goals && goals.length > 0) {
      setSearchParams({ goal: goals[0].id }, { replace: true });
    }
  }, [selectedGoalId, goals, setSearchParams]);

  const selectedGoal = goals?.find((g) => g.id === selectedGoalId) ?? null;

  return (
    <div className="flex h-full">
      {/* Sidebar */}
      <aside className="w-72 border-r flex flex-col shrink-0">
        <div className="p-3 border-b flex items-center justify-between">
          <span className="font-semibold">Agents</span>
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={() => navigate("/agents/list")}
            title="New goal"
          >
            <PlusIcon className="size-4" />
          </Button>
        </div>
        <div className="flex-1 overflow-y-auto">
          {!goals || goals.length === 0 ? (
            <div className="p-4 text-sm text-muted-foreground text-center">
              No goals yet. Click + to create one.
            </div>
          ) : (
            goals.map((g) => (
              <GoalSidebarRow
                key={g.id}
                goal={g}
                selected={g.id === selectedGoalId}
                onSelect={() => setSearchParams({ goal: g.id })}
              />
            ))
          )}
        </div>
      </aside>

      {/* Main panel */}
      <main className="flex-1 flex flex-col overflow-hidden">
        {selectedGoal ? (
          <GoalChatPanel goal={selectedGoal} />
        ) : (
          <div className="flex-1 flex items-center justify-center text-muted-foreground">
            Select a goal from the sidebar to view its conversation.
          </div>
        )}
      </main>
    </div>
  );
}

interface GoalSidebarRowProps {
  goal: Goal;
  selected: boolean;
  onSelect: () => void;
}

function GoalSidebarRow({ goal, selected, onSelect }: GoalSidebarRowProps) {
  const navigate = useNavigate();
  const api = useWsApi();
  const queryClient = useQueryClient();
  const [running, setRunning] = useState(false);

  const handleRun = async (e: React.MouseEvent) => {
    e.stopPropagation();
    setRunning(true);
    try {
      await api.runGoalNow(goal.id);
      queryClient.invalidateQueries({ queryKey: ["agent"] });
    } finally {
      setRunning(false);
    }
  };

  const handleReenable = async (e: React.MouseEvent) => {
    e.stopPropagation();
    await api.updateGoal(goal.id, { status: "enabled" });
    queryClient.invalidateQueries({ queryKey: ["agent"] });
  };

  return (
    <button
      type="button"
      onClick={onSelect}
      className={`w-full text-left px-3 py-2 border-b hover:bg-accent/40 ${
        selected ? "bg-accent" : ""
      }`}
    >
      <div className="flex items-center gap-2">
        <span
          className={`size-2 rounded-full shrink-0 ${STATUS_COLOR[goal.status]}`}
          aria-label={goal.status}
        />
        <span className="font-medium text-sm flex-1 truncate">{goal.name}</span>
        {goal.status === "completed" ? (
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={handleReenable}
            className="h-6 w-6"
            title="Re-enable (clears completion)"
          >
            <ZapIcon className="size-3.5" />
          </Button>
        ) : (
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={handleRun}
            disabled={running || goal.status !== "enabled"}
            className="h-6 w-6"
            title={
              goal.status === "disabled"
                ? "Goal is disabled — open settings to re-enable"
                : "Run now"
            }
          >
            {running ? (
              <Loader2Icon className="size-3.5 animate-spin" />
            ) : (
              <PlayIcon className="size-3.5" />
            )}
          </Button>
        )}
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={(e) => {
            e.stopPropagation();
            navigate(`/agents/${goal.id}`);
          }}
          className="h-6 w-6"
          title="Settings"
        >
          <CogIcon className="size-3.5" />
        </Button>
      </div>
      <div className="text-xs text-muted-foreground mt-1 ml-4 truncate">
        {goal.run_count} run{goal.run_count === 1 ? "" : "s"}
        {goal.last_run_status ? ` · last: ${goal.last_run_status}` : ""}
      </div>
    </button>
  );
}

interface GoalChatPanelProps {
  goal: Goal;
}

function GoalChatPanel({ goal }: GoalChatPanelProps) {
  const api = useWsApi();

  // Fetch runs so we can fall back to the most recent run's
  // conversation_id when goal.conversation_id is empty (stateless goals
  // never capture a single conversation on the goal; first-run stateful
  // goals only capture after success).
  const { data: runsResp } = useQuery({
    queryKey: ["agent", "runs", goal.id, goal.run_count],
    queryFn: () => api.listAgentRuns(goal.id, 50),
    enabled: !!goal.id,
  });

  const runs = runsResp?.ok && runsResp.runs ? runsResp.runs : [];
  // Most recent run with a conversation_id (runs are returned newest-first)
  const fallbackConvId =
    runs.find((r) => !!r.conversation_id)?.conversation_id ?? "";
  const conversationId = goal.conversation_id || fallbackConvId;

  const { data: conversation, isLoading } = useQuery<ConversationDetail | null>({
    queryKey: ["agent-conv", conversationId, goal.run_count],
    queryFn: () =>
      conversationId
        ? api.loadConversation(conversationId)
        : Promise.resolve(null),
    enabled: !!conversationId,
  });

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="border-b px-4 py-3 flex items-center gap-3">
        <h1 className="font-semibold text-lg flex-1">{goal.name}</h1>
        <span className="text-xs text-muted-foreground">
          {goal.run_count} run{goal.run_count === 1 ? "" : "s"}
        </span>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {!conversationId ? (
          <div className="text-center text-muted-foreground py-12">
            No conversation yet. Hit Run on the sidebar to start.
          </div>
        ) : isLoading ? (
          <div className="text-center text-muted-foreground py-12">
            Loading…
          </div>
        ) : !conversation ||
          !conversation.turns ||
          conversation.turns.length === 0 ? (
          <div className="text-center text-muted-foreground py-12">
            Conversation is empty.
          </div>
        ) : (
          conversation.turns.map((turn, i) => (
            <FlatTurnBlock key={i} turn={turn} />
          ))
        )}
      </div>
    </div>
  );
}

interface FlatTurnBlockProps {
  turn: ChatTurn;
}

/**
 * Renders one ChatTurn flat — user trigger message, then each
 * intermediate round's reasoning + tool calls, then the final
 * assistant response. No collapsing or grouping.
 */
function FlatTurnBlock({ turn }: FlatTurnBlockProps) {
  return (
    <div className="space-y-2">
      {/* User / trigger message */}
      {turn.user_message?.content && (
        <div className="rounded-md bg-blue-50 dark:bg-blue-950/30 border border-blue-200 dark:border-blue-900 px-3 py-2">
          <div className="text-xs font-medium text-blue-700 dark:text-blue-400 mb-1">
            User / trigger
          </div>
          <div className="text-sm whitespace-pre-wrap break-words">
            {turn.user_message.content}
          </div>
        </div>
      )}

      {/* Intermediate rounds */}
      {(turn.rounds ?? []).map((round, ri) => (
        <FlatRoundBlock key={ri} round={round} />
      ))}

      {/* Final assistant response */}
      {turn.final_content && (
        <div className="px-3 py-2 border-l-2 border-muted">
          <div className="text-xs font-medium text-muted-foreground mb-1">
            assistant
          </div>
          <div className="text-sm whitespace-pre-wrap break-words">
            {turn.final_content}
          </div>
        </div>
      )}
    </div>
  );
}

interface FlatRoundBlockProps {
  round: ChatRound;
}

function FlatRoundBlock({ round }: FlatRoundBlockProps) {
  return (
    <div className="space-y-1 pl-2">
      {/* Reasoning prose */}
      {round.reasoning && (
        <div className="px-3 py-2">
          <div className="text-xs font-medium text-muted-foreground mb-1">
            reasoning
          </div>
          <div className="text-sm whitespace-pre-wrap break-words text-muted-foreground italic">
            {round.reasoning}
          </div>
        </div>
      )}

      {/* Tool calls in this round */}
      {(round.tools ?? []).map((tool, ti) => (
        <FlatToolBlock key={ti} tool={tool} />
      ))}
    </div>
  );
}

interface FlatToolBlockProps {
  tool: ChatRoundTool;
}

function FlatToolBlock({ tool }: FlatToolBlockProps) {
  return (
    <div
      className={`rounded-md border px-3 py-2 ${
        tool.is_error
          ? "bg-red-50 dark:bg-red-950/30 border-red-200 dark:border-red-900"
          : "bg-amber-50 dark:bg-amber-950/30 border-amber-200 dark:border-amber-900"
      }`}
    >
      <div
        className={`text-xs font-medium mb-1 flex items-center gap-1 ${
          tool.is_error
            ? "text-red-700 dark:text-red-400"
            : "text-amber-700 dark:text-amber-400"
        }`}
      >
        <WrenchIcon className="size-3" />
        {tool.tool_name}
        {tool.is_error ? " (error)" : ""}
      </div>

      {tool.arguments && Object.keys(tool.arguments).length > 0 && (
        <div className="text-xs font-mono whitespace-pre-wrap break-words bg-black/5 dark:bg-white/5 p-1 rounded mb-1 max-h-40 overflow-y-auto">
          {JSON.stringify(tool.arguments, null, 2)}
        </div>
      )}

      {tool.result && (
        <div className="text-xs font-mono whitespace-pre-wrap break-words max-h-48 overflow-y-auto opacity-80">
          {tool.result}
        </div>
      )}
    </div>
  );
}
