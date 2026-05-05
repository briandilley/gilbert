/**
 * WarRoomPage — read-only war-room view for a single goal.
 *
 * Layout:
 *  - Back link to ``/goals``.
 *  - Header card: name, description, status pill, lifetime cost,
 *    "Status" dropdown (DRIVER mutates via ``goals.update_status``),
 *    "Handoff" button (opens dialog).
 *  - AssigneesStrip below the header.
 *  - Two-column body: scrollable post list + a right rail with
 *    DeliverablesPanel + DependenciesPanel (Phase 5, wired below).
 *
 * Composer: Phase 4 ships a READ-ONLY war room. Posting flows through
 * ``goal_post`` from an assigned agent. A human composer is a
 * follow-up.
 *
 * Real-time:
 *  - ``goal.updated`` and ``goal.status.changed`` invalidate the
 *    summary + detail.
 *  - ``goal.assignment.changed`` invalidates assignments + summary.
 *  - ``goal.deliverable.finalized`` is handled inside DeliverablesPanel.
 *  - We don't subscribe to a per-message "new post" event yet; the
 *    posts query auto-refreshes when React Query re-validates.
 */

import { useCallback, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { useAgents } from "@/api/agents";
import {
  useDeleteGoal,
  useGoalPosts,
  useGoalSummary,
  useHandoffGoal,
  useUpdateGoalStatus,
} from "@/api/goals";
import { ApiError } from "@/api/client";
import { useEventBus } from "@/hooks/useEventBus";
import { AgentAvatar } from "@/components/agent/AgentAvatar";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { timeAgo } from "@/lib/timeAgo";
import type {
  Agent,
  AssignmentRole,
  GoalStatus,
  GoalSummary,
  WarRoomPost,
} from "@/types/agent";
import { AssigneesStrip } from "./AssigneesStrip";
import { DeliverablesPanel } from "./DeliverablesPanel";
import { DependenciesPanel } from "./DependenciesPanel";
import { GoalStatusPill } from "./GoalCard";
import type { GilbertEvent } from "@/types/events";

const STATUS_OPTIONS: Array<{ value: GoalStatus; label: string }> = [
  { value: "new", label: "New" },
  { value: "in_progress", label: "In progress" },
  { value: "blocked", label: "Blocked" },
  { value: "complete", label: "Complete" },
  { value: "cancelled", label: "Cancelled" },
];

export function WarRoomPage() {
  const params = useParams<{ goalId: string }>();
  const goalId = params.goalId ?? "";
  const qc = useQueryClient();
  const navigate = useNavigate();

  const summaryQuery = useGoalSummary(goalId);
  const postsQuery = useGoalPosts(goalId, 100);
  const deleteGoal = useDeleteGoal();

  const [actionError, setActionError] = useState<string | null>(null);
  const [handoffOpen, setHandoffOpen] = useState(false);
  const [confirmDeleteOpen, setConfirmDeleteOpen] = useState(false);

  const handleDelete = async () => {
    setActionError(null);
    try {
      await deleteGoal.mutateAsync({ goalId });
      setConfirmDeleteOpen(false);
      navigate("/goals");
    } catch (e) {
      setActionError(
        e instanceof Error ? e.message : "Failed to delete goal.",
      );
    }
  };

  // ── Real-time event subscriptions ─────────────────────────────────

  const onGoalChanged = useCallback(
    (event: GilbertEvent) => {
      if (event.data.goal_id === goalId) {
        qc.invalidateQueries({ queryKey: ["goals", "summary", goalId] });
        qc.invalidateQueries({ queryKey: ["goals", "detail", goalId] });
        qc.invalidateQueries({ queryKey: ["goals", "posts", goalId] });
      }
    },
    [goalId, qc],
  );

  const onAssignmentChanged = useCallback(
    (event: GilbertEvent) => {
      if (event.data.goal_id === goalId) {
        qc.invalidateQueries({
          queryKey: ["goals", "assignments", goalId],
        });
        qc.invalidateQueries({ queryKey: ["goals", "summary", goalId] });
      }
    },
    [goalId, qc],
  );

  useEventBus("goal.updated", onGoalChanged);
  useEventBus("goal.status.changed", onGoalChanged);
  useEventBus("goal.assignment.changed", onAssignmentChanged);

  // ── Loading / error states ────────────────────────────────────────

  if (summaryQuery.isPending) {
    return (
      <div className="p-6">
        <BackLink />
        <div className="mt-4">
          <LoadingSpinner text="Loading goal…" />
        </div>
      </div>
    );
  }

  if (summaryQuery.isError) {
    const err = summaryQuery.error;
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
              Goal not found.{" "}
              <Link to="/goals" className="underline">
                Back to goals
              </Link>
              .
            </>
          ) : (
            <>
              Failed to load goal:{" "}
              {err instanceof Error ? err.message : "unknown error"}
            </>
          )}
        </div>
      </div>
    );
  }

  const summary = summaryQuery.data;
  const goal = summary.goal;

  return (
    <div className="p-4 sm:p-6 space-y-4 max-w-7xl mx-auto">
      <BackLink />

      {/* Header card */}
      <Card>
        <CardContent>
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="text-lg font-semibold truncate">{goal.name}</h1>
                <GoalStatusPill status={goal.status} />
              </div>
              {goal.description && (
                <p className="mt-1 text-sm text-muted-foreground whitespace-pre-wrap">
                  {goal.description}
                </p>
              )}
              <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
                <span>
                  Cost: ${goal.lifetime_cost_usd.toFixed(2)}
                  {goal.cost_cap_usd != null && (
                    <> / ${goal.cost_cap_usd.toFixed(2)}</>
                  )}
                </span>
                <span>Created {timeAgo(goal.created_at)}</span>
                {goal.completed_at && (
                  <span>Completed {timeAgo(goal.completed_at)}</span>
                )}
              </div>
            </div>
            <div className="flex flex-wrap gap-2">
              <StatusSelector
                goalId={goal._id}
                status={goal.status}
                onError={setActionError}
              />
              <Button
                variant="outline"
                onClick={() => setHandoffOpen(true)}
                disabled={summary.assignees.length < 2}
                title={
                  summary.assignees.length < 2
                    ? "Need at least two assignees to hand off"
                    : undefined
                }
              >
                Handoff
              </Button>
              <Button
                variant="outline"
                onClick={() => setConfirmDeleteOpen(true)}
                disabled={deleteGoal.isPending}
              >
                Delete
              </Button>
            </div>
          </div>

          <div className="mt-4">
            <AssigneesStrip goalId={goal._id} />
          </div>

          {actionError && (
            <div
              role="alert"
              className="mt-3 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
            >
              {actionError}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Body: posts + right rail */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[1fr_18rem]">
        <Card>
          <CardContent>
            <h2 className="text-sm font-semibold mb-2">War room</h2>
            {/*
             * Phase 4 ships a read-only war room. Posting flows through
             * the ``goal_post`` agent tool; a human composer is a
             * follow-up.
             */}
            {postsQuery.isPending && (
              <LoadingSpinner text="Loading posts…" />
            )}
            {postsQuery.isError && (
              <div
                role="alert"
                className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
              >
                Failed to load war-room posts.
              </div>
            )}
            {postsQuery.data && postsQuery.data.length === 0 && (
              <div className="rounded-md border border-dashed px-4 py-6 text-center text-sm text-muted-foreground">
                No posts yet.
              </div>
            )}
            {postsQuery.data && postsQuery.data.length > 0 && (
              <PostsList posts={postsQuery.data} />
            )}
          </CardContent>
        </Card>

        <div className="space-y-3">
          {/*
           * The viewer here is a user, not an agent. The backend gates
           * each mutation (producer OR DRIVER for finalize/supersede;
           * caller owner for dependency add/remove), so we forward a
           * permissive ``isDriver={true}`` and surface backend errors
           * inline. Future polish can compute true ownership/role.
           */}
          <DeliverablesPanel goalId={goal._id} isDriver={true} />
          <DependenciesPanel goalId={goal._id} isDriver={true} />
        </div>
      </div>

      <HandoffDialog
        open={handoffOpen}
        onOpenChange={setHandoffOpen}
        summary={summary}
        onCompleted={() => {
          setHandoffOpen(false);
          qc.invalidateQueries({ queryKey: ["goals", "summary", goal._id] });
          qc.invalidateQueries({
            queryKey: ["goals", "assignments", goal._id],
          });
        }}
      />

      <Dialog
        open={confirmDeleteOpen}
        onOpenChange={(o) => !deleteGoal.isPending && setConfirmDeleteOpen(o)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete goal?</DialogTitle>
            <DialogDescription>
              This permanently deletes <strong>{goal.name}</strong> along with
              its war-room conversation, assignments, deliverables, and
              dependency edges. This cannot be undone — use Cancelled status
              instead if you want to keep history.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setConfirmDeleteOpen(false)}
              disabled={deleteGoal.isPending}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={handleDelete}
              disabled={deleteGoal.isPending}
            >
              {deleteGoal.isPending ? "Deleting…" : "Delete goal"}
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
      to="/goals"
      className="text-sm text-muted-foreground hover:text-foreground hover:underline"
    >
      ← Goals
    </Link>
  );
}

function StatusSelector({
  goalId,
  status,
  onError,
}: {
  goalId: string;
  status: GoalStatus;
  onError: (msg: string | null) => void;
}) {
  const update = useUpdateGoalStatus();
  return (
    <Select
      value={status}
      onValueChange={async (v) => {
        const next = v as GoalStatus;
        if (next === status) return;
        onError(null);
        try {
          await update.mutateAsync({ goalId, status: next });
        } catch (err) {
          onError(
            err instanceof Error ? err.message : "Failed to change status.",
          );
        }
      }}
    >
      <SelectTrigger className="w-40">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {STATUS_OPTIONS.map((s) => (
          <SelectItem key={s.value} value={s.value}>
            {s.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

function PostsList({ posts }: { posts: WarRoomPost[] }) {
  const { data: agents } = useAgents();
  const agentByName: Record<string, Agent> = {};
  for (const a of agents ?? []) agentByName[a.name] = a;

  // Render newest at the bottom — server returns chronological, but
  // be defensive in case it changes.
  const ordered = [...posts].sort(
    (a, b) => new Date(a.ts).getTime() - new Date(b.ts).getTime(),
  );

  return (
    <ol className="space-y-3 max-h-[60vh] overflow-y-auto pr-2">
      {ordered.map((p, i) => {
        const agent =
          p.author_kind === "agent" ? agentByName[p.author_name] : undefined;
        return (
          <li
            key={`${p.ts}-${i}`}
            className="flex items-start gap-2 border-b border-border/50 pb-2 last:border-b-0 last:pb-0"
          >
            {agent ? (
              <AgentAvatar agent={agent} size="xs" />
            ) : (
              <span
                className="inline-flex size-4 items-center justify-center rounded-full bg-muted text-[10px]"
                aria-hidden
              >
                {p.author_kind === "user" ? "U" : "?"}
              </span>
            )}
            <div className="min-w-0 flex-1">
              <div className="flex items-baseline gap-2">
                <span className="text-xs font-medium truncate">
                  {p.author_name}
                </span>
                <span className="text-[11px] text-muted-foreground">
                  {timeAgo(p.ts)}
                </span>
              </div>
              <div className="mt-0.5 text-sm whitespace-pre-wrap">{p.body}</div>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

interface HandoffDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  summary: GoalSummary;
  onCompleted: () => void;
}

function HandoffDialog({
  open,
  onOpenChange,
  summary,
  onCompleted,
}: HandoffDialogProps) {
  const handoff = useHandoffGoal();
  const driver = summary.assignees.find((a) => a.role === "driver");
  const others = summary.assignees.filter((a) => a.role !== "driver");

  const [toAgentId, setToAgentId] = useState("");
  const [newRoleForFrom, setNewRoleForFrom] =
    useState<AssignmentRole>("collaborator");
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setToAgentId("");
    setNewRoleForFrom("collaborator");
    setNote("");
    setError(null);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!driver) {
      setError("No current driver to hand off from.");
      return;
    }
    if (!toAgentId) {
      setError("Pick the new driver.");
      return;
    }
    setError(null);
    try {
      await handoff.mutateAsync({
        goalId: summary.goal._id,
        fromAgentId: driver.agent_id,
        toAgentId,
        newRoleForFrom,
        ...(note.trim() ? { note: note.trim() } : {}),
      });
      reset();
      onCompleted();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Handoff failed.");
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (!o) reset();
        onOpenChange(o);
      }}
    >
      <DialogContent>
        <form onSubmit={handleSubmit} className="space-y-4">
          <DialogHeader>
            <DialogTitle>Handoff DRIVER role</DialogTitle>
            <DialogDescription>
              Transfer the DRIVER role to another assignee. The current
              driver becomes a collaborator (or a role you choose), and
              the note is recorded on both assignment rows.
            </DialogDescription>
          </DialogHeader>

          <div className="text-xs text-muted-foreground">
            Current driver: <strong>{driver?.agent_name ?? "(none)"}</strong>
          </div>

          <div className="space-y-2">
            <Label>New driver</Label>
            <Select
              value={toAgentId}
              onValueChange={(v) => setToAgentId(v ?? "")}
            >
              <SelectTrigger className="w-full">
                <SelectValue placeholder="Pick an assignee…" />
              </SelectTrigger>
              <SelectContent>
                {others.map((a) => (
                  <SelectItem key={a.agent_id} value={a.agent_id}>
                    {a.agent_name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {others.length === 0 && (
              <div className="text-xs text-muted-foreground">
                No other assignees — add one from the strip first.
              </div>
            )}
          </div>

          <div className="space-y-2">
            <Label>Role for previous driver</Label>
            <Select
              value={newRoleForFrom}
              onValueChange={(v) => setNewRoleForFrom(v as AssignmentRole)}
            >
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="collaborator">Collaborator</SelectItem>
                <SelectItem value="reviewer">Reviewer</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-2">
            <Label htmlFor="handoff-note">Note (optional)</Label>
            <Textarea
              id="handoff-note"
              rows={3}
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="Why are you handing off?"
            />
          </div>

          {error && (
            <div
              role="alert"
              className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive"
            >
              {error}
            </div>
          )}

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={handoff.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={handoff.isPending}>
              {handoff.isPending ? "Handing off…" : "Handoff"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
