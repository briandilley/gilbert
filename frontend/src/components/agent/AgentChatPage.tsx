import { useState, useEffect, useCallback, useRef } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  PlusIcon,
  PlayIcon,
  CogIcon,
  Loader2Icon,
  WrenchIcon,
  ZapIcon,
  Trash2Icon,
  MenuIcon,
} from "lucide-react";
import { useWebSocket } from "@/hooks/useWebSocket";
import { useEventBus } from "@/hooks/useEventBus";
import { useWsApi } from "@/hooks/useWsApi";
import { MarkdownContent } from "@/components/ui/MarkdownContent";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { CreateGoalDialog } from "@/components/agent/AgentsPage";
import type { Goal, GoalStatus } from "@/types/agent";
import type { GilbertEvent } from "@/types/events";
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
  const [createOpen, setCreateOpen] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);

  // Auto-close the mobile sidebar whenever the selected goal changes
  // (so picking a goal from the drawer immediately shows the chat).
  useEffect(() => {
    if (selectedGoalId) setMobileSidebarOpen(false);
  }, [selectedGoalId]);

  const { data: goals } = useQuery({
    queryKey: ["agent", "goals"],
    queryFn: api.listGoals,
    enabled: connected,
  });

  const { data: profiles } = useQuery({
    queryKey: ["ai-profiles"],
    queryFn: api.listAiProfiles,
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

  const sidebarBody = (
    <>
      <div className="p-3 border-b flex items-center justify-between">
        <span className="font-semibold">Agents</span>
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={() => setCreateOpen(true)}
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
    </>
  );

  return (
    <div className="flex h-full">
      {/* Desktop sidebar — persistent at md+ */}
      <aside className="hidden md:flex w-72 border-r flex-col shrink-0">
        {sidebarBody}
      </aside>

      {/* Mobile sidebar — Sheet drawer */}
      <Sheet open={mobileSidebarOpen} onOpenChange={setMobileSidebarOpen}>
        <SheetContent side="left" className="w-72 p-0 flex flex-col">
          <SheetHeader className="sr-only">
            <SheetTitle>Agents</SheetTitle>
          </SheetHeader>
          {sidebarBody}
        </SheetContent>
      </Sheet>

      {/* Main panel */}
      <main className="flex-1 flex flex-col overflow-hidden min-w-0">
        {selectedGoal ? (
          <GoalChatPanel
            goal={selectedGoal}
            onOpenSidebar={() => setMobileSidebarOpen(true)}
          />
        ) : (
          <div className="flex-1 flex flex-col items-center justify-center text-muted-foreground p-6 text-center gap-3">
            <Button
              variant="outline"
              size="sm"
              className="md:hidden"
              onClick={() => setMobileSidebarOpen(true)}
            >
              <MenuIcon className="size-4 mr-2" /> Open agents
            </Button>
            <span>Select a goal from the sidebar to view its conversation.</span>
          </div>
        )}
      </main>

      <CreateGoalDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        profiles={profiles ?? []}
        onCreated={() => {
          queryClient.invalidateQueries({ queryKey: ["agent"] });
          setCreateOpen(false);
        }}
      />
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

  const handleDelete = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (
      !confirm(
        `Delete goal "${goal.name}"? This removes the goal, all runs, and the conversation.`,
      )
    )
      return;
    await api.deleteGoal(goal.id);
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
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={handleDelete}
          className="h-6 w-6 hover:text-red-600 dark:hover:text-red-400"
          title="Delete goal"
        >
          <Trash2Icon className="size-3.5" />
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
  onOpenSidebar?: () => void;
}

function GoalChatPanel({ goal, onOpenSidebar }: GoalChatPanelProps) {
  const api = useWsApi();
  const queryClient = useQueryClient();
  const [composerText, setComposerText] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);

  // Fetch runs so we can fall back to the most recent run's
  // conversation_id when goal.conversation_id is empty (stateless goals
  // never capture a single conversation on the goal; first-run stateful
  // goals only capture after success). Poll while a run is in progress.
  const { data: runsResp } = useQuery({
    queryKey: ["agent", "runs", goal.id, goal.run_count],
    queryFn: () => api.listAgentRuns(goal.id, 50),
    enabled: !!goal.id,
    refetchInterval: (query) => {
      const data = query.state.data;
      if (!data || !data.ok || !data.runs) return false;
      const hasRunning = data.runs.some((r) => r.status === "running");
      return hasRunning ? 2000 : false;
    },
  });

  const runs = runsResp?.ok && runsResp.runs ? runsResp.runs : [];
  // Most recent run with a conversation_id (runs are returned newest-first)
  const fallbackConvId =
    runs.find((r) => !!r.conversation_id)?.conversation_id ?? "";
  const conversationId = goal.conversation_id || fallbackConvId;
  const hasRunningRun = runs.some((r) => r.status === "running");
  // Show "awaiting input" for the most recent run that has the flag,
  // regardless of run status — the agent's question outlives its
  // chat() turn and stays pending until the user actually replies.
  // Runs are returned newest-first so the first match is the latest.
  const awaitingRun = runs.find((r) => r.awaiting_user_input);
  const awaitingQuestion = awaitingRun?.pending_question || null;

  // ---------------------------------------------------------------------------
  // Streaming turn state — mirrors the ChatPage pattern.
  // A single "in-flight" ChatTurn is built up from stream events, then
  // cleared when turn_complete fires and the persisted version is fetched.
  // ---------------------------------------------------------------------------
  const [streamingTurn, setStreamingTurn] = useState<ChatTurn | null>(null);
  const nextRoundPendingRef = useRef(false);
  const streamingConvIdRef = useRef<string>("");

  // Keep the ref in sync with the active conversation so event handlers
  // can filter correctly without stale closure values.
  useEffect(() => {
    streamingConvIdRef.current = conversationId;
    // Drop any stale streaming turn when the conversation changes.
    setStreamingTurn(null);
    nextRoundPendingRef.current = false;
  }, [conversationId]);

  const updateStreamingTurn = useCallback(
    (mutator: (turn: ChatTurn) => ChatTurn) => {
      setStreamingTurn((prev) => {
        const base: ChatTurn = prev ?? {
          user_message: { content: "", attachments: [] },
          rounds: [],
          final_content: "",
          final_attachments: [],
          incomplete: false,
          streaming: true,
        };
        return mutator(base);
      });
    },
    [],
  );

  const handleTextDelta = useCallback(
    (event: GilbertEvent) => {
      if (event.data.conversation_id !== streamingConvIdRef.current) return;
      const chunk = event.data.text;
      if (typeof chunk !== "string" || !chunk) return;
      const startNewRound = nextRoundPendingRef.current;
      if (startNewRound) {
        nextRoundPendingRef.current = false;
      }
      updateStreamingTurn((turn) => {
        const rounds = [...turn.rounds];
        if (rounds.length === 0 || startNewRound) {
          rounds.push({ reasoning: "", tools: [] });
        }
        const lastIdx = rounds.length - 1;
        rounds[lastIdx] = {
          ...rounds[lastIdx],
          reasoning: rounds[lastIdx].reasoning + chunk,
        };
        return { ...turn, rounds };
      });
    },
    [updateStreamingTurn],
  );

  const handleToolStarted = useCallback(
    (event: GilbertEvent) => {
      if (event.data.conversation_id !== streamingConvIdRef.current) return;
      const toolName = String(event.data.tool_name || "");
      const toolCallId = String(event.data.tool_call_id || "");
      const args = (event.data.arguments as Record<string, unknown>) || {};
      updateStreamingTurn((turn) => {
        const rounds = [...turn.rounds];
        if (rounds.length === 0) {
          rounds.push({ reasoning: "", tools: [] });
        }
        const lastIdx = rounds.length - 1;
        const newTool: ChatRoundTool = {
          tool_call_id: toolCallId,
          tool_name: toolName,
          arguments: args,
          status: "running",
          is_error: false,
        };
        rounds[lastIdx] = {
          ...rounds[lastIdx],
          tools: [...rounds[lastIdx].tools, newTool],
        };
        return { ...turn, rounds };
      });
    },
    [updateStreamingTurn],
  );

  const handleToolCompleted = useCallback(
    (event: GilbertEvent) => {
      if (event.data.conversation_id !== streamingConvIdRef.current) return;
      const toolCallId = String(event.data.tool_call_id || "");
      const isError = Boolean(event.data.is_error);
      const resultPreview =
        typeof event.data.result_preview === "string"
          ? event.data.result_preview
          : "";
      updateStreamingTurn((turn) => {
        const rounds = turn.rounds.map((round) => {
          const tools = round.tools.map((tool) =>
            tool.tool_call_id === toolCallId
              ? {
                  ...tool,
                  status: "done" as const,
                  is_error: isError,
                  result: resultPreview,
                }
              : tool,
          );
          return { ...round, tools };
        });
        return { ...turn, rounds };
      });
    },
    [updateStreamingTurn],
  );

  const handleRoundComplete = useCallback(
    (event: GilbertEvent) => {
      if (event.data.conversation_id !== streamingConvIdRef.current) return;
      nextRoundPendingRef.current = true;
    },
    [],
  );

  const handleTurnComplete = useCallback(
    (event: GilbertEvent) => {
      if (event.data.conversation_id !== streamingConvIdRef.current) return;
      // Storage now has the committed turn. Refetch the persisted conversation
      // and clear the streaming overlay so the user sees the authoritative version.
      nextRoundPendingRef.current = false;
      setStreamingTurn(null);
      queryClient.invalidateQueries({ queryKey: ["agent-conv", conversationId] });
    },
    [conversationId, queryClient],
  );

  useEventBus("chat.stream.text_delta", handleTextDelta);
  useEventBus("chat.stream.round_complete", handleRoundComplete);
  useEventBus("chat.stream.turn_complete", handleTurnComplete);
  useEventBus("chat.tool.started", handleToolStarted);
  useEventBus("chat.tool.completed", handleToolCompleted);

  // Auto-scroll: track whether the user is scrolled to the bottom. If
  // they are, follow new content as it streams in. If they've scrolled
  // up to read older content, leave them alone — don't yank them back.
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const stickToBottomRef = useRef(true);

  const handleScroll = useCallback(() => {
    const el = scrollContainerRef.current;
    if (!el) return;
    // 32px threshold lets the user be "near the bottom" and still
    // count as following along.
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    stickToBottomRef.current = distanceFromBottom < 32;
  }, []);

  const { data: conversation, isLoading } = useQuery<ConversationDetail | null>({
    queryKey: ["agent-conv", conversationId, goal.run_count],
    queryFn: () =>
      conversationId
        ? api.loadConversation(conversationId)
        : Promise.resolve(null),
    enabled: !!conversationId,
    // Events now drive updates; polling not needed.
  });

  // When the conversation content changes (new persisted turns, new
  // streamed deltas, new tool events), keep the scroll glued to the
  // bottom unless the user has scrolled up to read history.
  const turnsCount = conversation?.turns?.length ?? 0;
  const streamingReasoningLen = streamingTurn
    ? streamingTurn.rounds.reduce(
        (sum, r) => sum + (r.reasoning?.length ?? 0),
        0,
      )
    : 0;
  const streamingToolsCount = streamingTurn
    ? streamingTurn.rounds.reduce((sum, r) => sum + r.tools.length, 0)
    : 0;
  useEffect(() => {
    if (!stickToBottomRef.current) return;
    const el = scrollContainerRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [turnsCount, streamingReasoningLen, streamingToolsCount, streamingTurn]);

  // When the user switches to a different goal, snap to the bottom.
  useEffect(() => {
    stickToBottomRef.current = true;
    const el = scrollContainerRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [conversationId]);

  const handleSend = async () => {
    const text = composerText.trim();
    if (!text) return;
    setSending(true);
    setSendError(null);
    try {
      const result = await api.runGoalNow(goal.id, text);
      if (!result.ok) {
        setSendError(result.error || "Failed to send.");
        return;
      }
      setComposerText("");
      queryClient.invalidateQueries({ queryKey: ["agent"] });
      queryClient.invalidateQueries({ queryKey: ["agent-conv"] });
    } catch (e) {
      setSendError(e instanceof Error ? e.message : String(e));
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="border-b px-3 sm:px-4 py-3 flex items-center gap-2 sm:gap-3">
        {onOpenSidebar ? (
          <Button
            variant="ghost"
            size="icon-sm"
            className="md:hidden shrink-0"
            onClick={onOpenSidebar}
            aria-label="Open agents list"
          >
            <MenuIcon className="size-5" />
          </Button>
        ) : null}
        <h1 className="font-semibold text-lg flex-1 truncate min-w-0">
          {goal.name}
        </h1>
        {awaitingQuestion ? (
          <span className="text-xs px-2 py-0.5 rounded bg-amber-500/10 text-amber-700 dark:text-amber-400 shrink-0">
            <span className="hidden sm:inline">⌛ waiting for your input</span>
            <span className="sm:hidden">⌛ waiting</span>
          </span>
        ) : hasRunningRun ? (
          <span className="text-xs text-blue-600 dark:text-blue-400 flex items-center gap-1 shrink-0">
            <Loader2Icon className="size-3 animate-spin" /> running…
          </span>
        ) : null}
        <span className="hidden sm:inline text-xs text-muted-foreground shrink-0">
          {goal.run_count} run{goal.run_count === 1 ? "" : "s"}
        </span>
      </div>

      {/* Messages */}
      <div
        ref={scrollContainerRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto p-3 sm:p-4 space-y-3 sm:space-y-4"
      >
        {!conversationId ? (
          <div className="text-center text-muted-foreground py-12">
            No conversation yet. Send a message below to start.
          </div>
        ) : isLoading ? (
          <div className="text-center text-muted-foreground py-12">
            Loading…
          </div>
        ) : !conversation ||
          !conversation.turns ||
          (conversation.turns.length === 0 && !streamingTurn) ? (
          <div className="text-center text-muted-foreground py-12">
            Conversation is empty.
          </div>
        ) : (
          <>
            {(conversation?.turns ?? []).map((turn, i) => (
              <FlatTurnBlock key={i} turn={turn} />
            ))}
            {streamingTurn ? (
              <FlatTurnBlock
                key="__streaming"
                turn={streamingTurn}
                streaming
              />
            ) : null}
          </>
        )}
      </div>

      {/* Awaiting-input banner */}
      {awaitingQuestion ? (
        <div className="border-t border-amber-200 dark:border-amber-900 bg-amber-50/50 dark:bg-amber-950/20 px-4 py-2 text-sm">
          <span className="font-medium text-amber-800 dark:text-amber-300">
            Agent is asking:
          </span>{" "}
          <span className="text-amber-900 dark:text-amber-200">
            {awaitingQuestion}
          </span>
        </div>
      ) : null}

      {/* Composer */}
      <div className="border-t p-3">
        <div className="flex gap-2 items-end">
          <textarea
            value={composerText}
            onChange={(e) => setComposerText(e.target.value)}
            onKeyDown={(e) => {
              // Enter sends; Shift+Enter inserts a newline.
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                handleSend();
              }
            }}
            placeholder={
              goal.status !== "enabled"
                ? `Goal is ${goal.status} — re-enable to chat`
                : hasRunningRun
                ? "Run in progress — message will be injected mid-run…"
                : "Send a message to the agent (Enter to send, Shift+Enter for newline)…"
            }
            disabled={sending || goal.status !== "enabled"}
            rows={2}
            className="flex-1 min-h-12 max-h-48 rounded-md border bg-transparent px-3 py-2 text-sm shadow-sm resize-y disabled:opacity-50"
          />
          <Button
            type="button"
            onClick={handleSend}
            disabled={
              sending ||
              !composerText.trim() ||
              goal.status !== "enabled"
            }
          >
            {sending ? (
              <Loader2Icon className="size-4 animate-spin" />
            ) : (
              "Send"
            )}
          </Button>
        </div>
        {sendError ? (
          <div className="text-xs text-red-600 mt-1">{sendError}</div>
        ) : null}
      </div>
    </div>
  );
}

interface FlatTurnBlockProps {
  turn: ChatTurn;
  streaming?: boolean;
}

/**
 * Renders one ChatTurn flat — user trigger message, then each
 * intermediate round's reasoning + tool calls, then the final
 * assistant response. No collapsing or grouping.
 *
 * When `streaming` is true a pulsing indicator is shown and a subtle
 * left border marks the turn as in-flight.
 */
function FlatTurnBlock({ turn, streaming }: FlatTurnBlockProps) {
  return (
    <div
      className={`space-y-2${streaming ? " border-l-2 border-blue-400 pl-3" : ""}`}
    >
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
          <MarkdownContent content={turn.final_content} />
        </div>
      )}

      {/* Streaming indicator */}
      {streaming ? (
        <div className="text-xs text-blue-600 dark:text-blue-400 flex items-center gap-1 mt-2">
          <span className="size-1.5 rounded-full bg-blue-500 animate-pulse" />
          streaming…
        </div>
      ) : null}
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
          <div className="text-muted-foreground italic">
            <MarkdownContent content={round.reasoning} />
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
