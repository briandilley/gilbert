/**
 * AgentDetailPage — full detail view for a single agent.
 *
 * Layout:
 *  - Back link to ``/agents``.
 *  - Header card: avatar, name, role_label, status pill, action buttons
 *    (Run now / Disable-Enable / Delete) and a plugin toolbar slot
 *    (``agent.detail.toolbar``).
 *  - Tabs: Chat | Settings | Memory | Commitments | Runs, plus
 *    plugin-contributed extra tabs via ``agent.detail.settings.tabs``.
 *  - Active tab is driven by the ``?tab=`` URL search param so
 *    deep-links work; default is ``chat``.
 *
 * Real-time:
 *  - ``agent.updated`` → invalidate ``["agents", "detail", agentId]``.
 *  - ``agent.deleted`` → if matching agent_id, navigate back to ``/agents``.
 *  - ``agent.run.started`` / ``agent.run.completed`` → invalidate runs +
 *    detail (lifetime cost accrues).
 *
 * Note: the Chat tab intentionally does NOT embed the chat composer +
 * turn rendering. Wiring those into the agent detail is a Phase 1B+
 * polish item — for now we either show a "no conversation yet"
 * placeholder, or link out to ``/chat`` for the existing
 * ``conversation_id``.
 */

import { useCallback, useState } from "react";
import {
  Link,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import {
  useAgent,
  useDeleteAgent,
  useRunAgentNow,
  useSetAgentStatus,
} from "@/api/agents";
import { ApiError } from "@/api/client";
import { useEventBus } from "@/hooks/useEventBus";
import { AgentAvatar } from "./AgentAvatar";
import { AgentEditForm } from "./AgentEditForm";
import { CommitmentsList } from "./CommitmentsList";
import { MemoryBrowser } from "./MemoryBrowser";
import { RunsTable } from "./RunsTable";
import { PluginPanelSlot } from "@/components/PluginPanelSlot";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
import type { GilbertEvent } from "@/types/events";

const TAB_VALUES = ["chat", "settings", "memory", "commitments", "runs"] as const;
type TabValue = (typeof TAB_VALUES)[number];

function isTabValue(s: string | null): s is TabValue {
  return s !== null && (TAB_VALUES as readonly string[]).includes(s);
}

export function AgentDetailPage() {
  const params = useParams<{ agentId: string }>();
  const agentId = params.agentId ?? "";
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();

  const tabParam = searchParams.get("tab");
  const activeTab: TabValue = isTabValue(tabParam) ? tabParam : "chat";
  const setActiveTab = (next: string) => {
    const sp = new URLSearchParams(searchParams);
    sp.set("tab", next);
    setSearchParams(sp, { replace: true });
  };

  const agentQuery = useAgent(agentId);
  const runNow = useRunAgentNow();
  const setStatus = useSetAgentStatus();
  const deleteAgent = useDeleteAgent();

  const [confirmDeleteOpen, setConfirmDeleteOpen] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  // ── Real-time event subscriptions ─────────────────────────────────

  const onAgentUpdated = useCallback(
    (event: GilbertEvent) => {
      if (event.data.agent_id === agentId) {
        qc.invalidateQueries({ queryKey: ["agents", "detail", agentId] });
      }
    },
    [agentId, qc],
  );

  const onAgentDeleted = useCallback(
    (event: GilbertEvent) => {
      if (event.data.agent_id === agentId) {
        navigate("/agents");
      }
    },
    [agentId, navigate],
  );

  const onRunChanged = useCallback(
    (event: GilbertEvent) => {
      if (event.data.agent_id === agentId) {
        qc.invalidateQueries({ queryKey: ["agents", "runs", agentId] });
        qc.invalidateQueries({ queryKey: ["agents", "detail", agentId] });
      }
    },
    [agentId, qc],
  );

  useEventBus("agent.updated", onAgentUpdated);
  useEventBus("agent.deleted", onAgentDeleted);
  useEventBus("agent.run.started", onRunChanged);
  useEventBus("agent.run.completed", onRunChanged);

  // ── Loading / error states ────────────────────────────────────────

  if (agentQuery.isPending) {
    return (
      <div className="p-6">
        <BackLink />
        <div className="mt-4">
          <LoadingSpinner text="Loading agent…" />
        </div>
      </div>
    );
  }

  if (agentQuery.isError) {
    const err = agentQuery.error;
    const isNotFound = err instanceof ApiError && err.status === 404;
    return (
      <div className="p-6">
        <BackLink />
        <div
          role="alert"
          className="mt-4 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          {isNotFound ? (
            <>
              Agent not found.{" "}
              <Link to="/agents" className="underline">
                Back to agents
              </Link>
              .
            </>
          ) : (
            <>
              Failed to load agent:{" "}
              {err instanceof Error ? err.message : "unknown error"}
            </>
          )}
        </div>
      </div>
    );
  }

  const agent = agentQuery.data;
  const isEnabled = agent.status === "enabled";

  // ── Action handlers ───────────────────────────────────────────────

  const handleRunNow = async () => {
    setActionError(null);
    try {
      await runNow.mutateAsync({ agentId, userMessage: undefined });
    } catch (e) {
      setActionError(
        e instanceof Error ? e.message : "Failed to start a run.",
      );
    }
  };

  const handleToggleStatus = async () => {
    setActionError(null);
    try {
      await setStatus.mutateAsync({
        agentId,
        status: isEnabled ? "disabled" : "enabled",
      });
    } catch (e) {
      setActionError(
        e instanceof Error ? e.message : "Failed to change status.",
      );
    }
  };

  const handleDelete = async () => {
    setActionError(null);
    try {
      await deleteAgent.mutateAsync(agentId);
      setConfirmDeleteOpen(false);
      navigate("/agents");
    } catch (e) {
      setActionError(
        e instanceof Error ? e.message : "Failed to delete agent.",
      );
    }
  };

  const isDeletePending = deleteAgent.isPending;

  // ── Render ───────────────────────────────────────────────────────

  return (
    <div className="p-6 space-y-4">
      <BackLink />

      {/* Header card */}
      <div className="rounded-md border bg-card p-4">
        <div className="flex items-start gap-4">
          <AgentAvatar agent={agent} size="md" />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-lg font-semibold truncate">{agent.name}</h1>
              <Badge
                className={
                  isEnabled
                    ? "bg-green-500/15 text-green-600 dark:text-green-400"
                    : "bg-yellow-500/15 text-yellow-600 dark:text-yellow-400"
                }
                variant="outline"
              >
                {agent.status}
              </Badge>
            </div>
            {agent.role_label && (
              <div className="text-sm text-muted-foreground truncate">
                {agent.role_label}
              </div>
            )}
          </div>
          <div className="flex flex-col items-end gap-2">
            <div className="flex flex-wrap justify-end gap-2">
              <Button
                onClick={handleRunNow}
                disabled={runNow.isPending || !isEnabled}
                title={
                  !isEnabled
                    ? "Enable the agent before running."
                    : undefined
                }
              >
                {runNow.isPending ? (
                  <span className="inline-flex items-center gap-2">
                    <span className="h-3 w-3 animate-spin rounded-full border-2 border-current border-t-transparent" />
                    Running…
                  </span>
                ) : (
                  "Run now"
                )}
              </Button>
              <Button
                variant="outline"
                onClick={handleToggleStatus}
                disabled={setStatus.isPending}
              >
                {setStatus.isPending
                  ? "Saving…"
                  : isEnabled
                    ? "Disable"
                    : "Enable"}
              </Button>
              <Button
                variant="destructive"
                onClick={() => setConfirmDeleteOpen(true)}
                disabled={isDeletePending}
              >
                Delete
              </Button>
            </div>
            <PluginPanelSlot slot="agent.detail.toolbar" />
          </div>
        </div>

        {actionError && (
          <div
            role="alert"
            className="mt-3 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
          >
            {actionError}
          </div>
        )}
      </div>

      {/* Tabs */}
      <Tabs value={activeTab} onValueChange={(v) => setActiveTab(String(v))}>
        <TabsList>
          <TabsTrigger value="chat">Chat</TabsTrigger>
          <TabsTrigger value="settings">Settings</TabsTrigger>
          <TabsTrigger value="memory">Memory</TabsTrigger>
          <TabsTrigger value="commitments">Commitments</TabsTrigger>
          <TabsTrigger value="runs">Runs</TabsTrigger>
        </TabsList>
        {/* Plugin-contributed extra tabs go alongside the built-ins. */}
        <PluginPanelSlot slot="agent.detail.settings.tabs" />

        <TabsContent value="chat">
          {/*
           * Phase 1B: full chat composer/turn rendering inside this
           * tab is deferred. We either show a placeholder if the agent
           * has never run (no ``conversation_id`` yet) or link to the
           * main chat page for the existing conversation.
           */}
          {agent.conversation_id === "" ? (
            <div className="border rounded-md p-4 text-sm text-muted-foreground">
              No conversation yet — click "Run now" above to start one.
            </div>
          ) : (
            <div className="border rounded-md p-4 text-sm">
              <p className="text-muted-foreground">
                Personal conversation:{" "}
                <code className="font-mono">{agent.conversation_id}</code>
              </p>
              <Link
                to={`/chat?conversation=${encodeURIComponent(agent.conversation_id)}`}
                className="text-blue-600 hover:underline"
              >
                Open in Chat ↗
              </Link>
            </div>
          )}
        </TabsContent>

        <TabsContent value="settings">
          <AgentEditForm mode="edit" agent={agent} />
        </TabsContent>

        <TabsContent value="memory">
          <MemoryBrowser agentId={agent._id} />
        </TabsContent>

        <TabsContent value="commitments">
          <CommitmentsList agentId={agent._id} />
        </TabsContent>

        <TabsContent value="runs">
          <RunsTable agentId={agent._id} />
        </TabsContent>
      </Tabs>

      {/* Delete confirmation */}
      <Dialog
        open={confirmDeleteOpen}
        onOpenChange={(o) => !isDeletePending && setConfirmDeleteOpen(o)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete agent?</DialogTitle>
            <DialogDescription>
              This permanently deletes <strong>{agent.name}</strong> along with
              its memory, commitments, and run history. This cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setConfirmDeleteOpen(false)}
              disabled={isDeletePending}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={handleDelete}
              disabled={isDeletePending}
            >
              {isDeletePending ? "Deleting…" : "Delete agent"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function BackLink() {
  return (
    <Link
      to="/agents"
      className="text-sm text-muted-foreground hover:text-foreground hover:underline"
    >
      ← Agents
    </Link>
  );
}
