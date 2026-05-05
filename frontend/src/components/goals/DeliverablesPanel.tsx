/**
 * DeliverablesPanel — right-rail card on the war-room page that lists
 * a goal's deliverables (drafts + finalized + obsolete) and lets a
 * driver/producer mutate them.
 *
 * Backend authority: the WS layer ultimately gates each mutation
 * (producer OR DRIVER for finalize/supersede). The SPA forwards a
 * permissive ``isDriver`` flag and surfaces backend errors inline if a
 * mutation is rejected.
 *
 * Real-time: the panel subscribes to ``goal.deliverable.finalized`` so
 * a finalize fired by another agent shows up without a manual refresh.
 */

import { useCallback, useState } from "react";
import { PlusIcon } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { useAgents } from "@/api/agents";
import {
  useCreateDeliverable,
  useDeliverables,
  useFinalizeDeliverable,
  useSupersedeDeliverable,
} from "@/api/goals";
import { useEventBus } from "@/hooks/useEventBus";
import { Badge } from "@/components/ui/badge";
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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { timeAgo } from "@/lib/timeAgo";
import type {
  Agent,
  Deliverable,
  DeliverableState,
} from "@/types/agent";
import type { GilbertEvent } from "@/types/events";

interface Props {
  goalId: string;
  isDriver: boolean;
  /** When set, the viewer is the producer agent themselves (gives access
   * to actions even without DRIVER role). Currently always undefined
   * from the war-room page since the viewer is a user, not an agent. */
  viewerAgentId?: string;
}

const STATE_BADGE_CLASS: Record<DeliverableState, string> = {
  draft: "bg-muted text-muted-foreground",
  ready: "bg-green-500/15 text-green-600 dark:text-green-400",
  obsolete: "bg-muted text-muted-foreground line-through",
};

const STATE_LABEL: Record<DeliverableState, string> = {
  draft: "Draft",
  ready: "Ready",
  obsolete: "Obsolete",
};

const KIND_HINTS = ["spec", "code", "report", "image"];

export function DeliverablesPanel({ goalId, isDriver, viewerAgentId }: Props) {
  const qc = useQueryClient();
  const deliverablesQuery = useDeliverables(goalId);
  const { data: agents } = useAgents();
  const [createOpen, setCreateOpen] = useState(false);
  const [supersedeTarget, setSupersedeTarget] = useState<Deliverable | null>(
    null,
  );
  const [actionError, setActionError] = useState<string | null>(null);

  // Subscribe to deliverable.finalized for live refresh.
  const onFinalized = useCallback(
    (event: GilbertEvent) => {
      if (event.data.goal_id === goalId) {
        qc.invalidateQueries({
          queryKey: ["goals", "deliverables", goalId],
        });
      }
    },
    [goalId, qc],
  );
  useEventBus("goal.deliverable.finalized", onFinalized);

  const agentById: Record<string, Agent> = {};
  for (const a of agents ?? []) agentById[a._id] = a;

  const sorted = [...(deliverablesQuery.data ?? [])].sort(
    (a, b) =>
      new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
  );

  const finalize = useFinalizeDeliverable();

  const handleFinalize = async (deliverable: Deliverable) => {
    setActionError(null);
    try {
      await finalize.mutateAsync(deliverable._id);
    } catch (err) {
      setActionError(
        err instanceof Error ? err.message : "Failed to finalize.",
      );
    }
  };

  return (
    <Card size="sm">
      <CardContent>
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-semibold">Deliverables</h3>
            <Badge variant="secondary" className="text-[10px]">
              {sorted.length}
            </Badge>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setCreateOpen(true)}
          >
            <PlusIcon /> New
          </Button>
        </div>

        {actionError && (
          <div
            role="alert"
            className="mt-2 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive"
          >
            {actionError}
          </div>
        )}

        {deliverablesQuery.isPending && (
          <div className="mt-3">
            <LoadingSpinner text="Loading deliverables…" />
          </div>
        )}

        {deliverablesQuery.isError && (
          <div
            role="alert"
            className="mt-2 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive"
          >
            Failed to load deliverables.
          </div>
        )}

        {deliverablesQuery.data && sorted.length === 0 && (
          <div className="mt-3 rounded-md border border-dashed px-3 py-4 text-center text-xs text-muted-foreground">
            No deliverables yet.
          </div>
        )}

        {sorted.length > 0 && (
          <ul className="mt-3 space-y-2">
            {sorted.map((deliverable) => {
              const producer = agentById[deliverable.produced_by_agent_id];
              const canAct =
                isDriver ||
                (viewerAgentId !== undefined &&
                  deliverable.produced_by_agent_id === viewerAgentId);
              const isObsolete = deliverable.state === "obsolete";
              return (
                <li
                  key={deliverable._id}
                  className={`rounded-md border bg-muted/20 px-2.5 py-2 ${
                    isObsolete ? "opacity-60" : ""
                  }`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span
                          className={`text-sm font-medium truncate ${
                            isObsolete ? "line-through" : ""
                          }`}
                        >
                          {deliverable.name}
                        </span>
                        <Badge
                          variant="outline"
                          className={`${STATE_BADGE_CLASS[deliverable.state]} text-[10px]`}
                        >
                          {STATE_LABEL[deliverable.state]}
                        </Badge>
                      </div>
                      <div className="text-[11px] text-muted-foreground">
                        {deliverable.kind || "—"}
                      </div>
                      <div className="text-[11px] text-muted-foreground">
                        {producer?.name ?? deliverable.produced_by_agent_id}
                        {" · "}
                        {timeAgo(deliverable.created_at)}
                      </div>
                      {deliverable.content_ref && (
                        <div
                          className="mt-1 text-[11px] text-muted-foreground truncate"
                          title={deliverable.content_ref}
                        >
                          {deliverable.content_ref}
                        </div>
                      )}
                    </div>
                  </div>

                  {canAct && !isObsolete && (
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {deliverable.state === "draft" && (
                        <Button
                          variant="outline"
                          size="xs"
                          disabled={finalize.isPending}
                          onClick={() => handleFinalize(deliverable)}
                        >
                          Finalize
                        </Button>
                      )}
                      {(deliverable.state === "draft" ||
                        deliverable.state === "ready") && (
                        <Button
                          variant="outline"
                          size="xs"
                          onClick={() => setSupersedeTarget(deliverable)}
                        >
                          Supersede
                        </Button>
                      )}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}

        <CreateDeliverableDialog
          goalId={goalId}
          open={createOpen}
          onOpenChange={setCreateOpen}
        />

        <SupersedeDeliverableDialog
          deliverable={supersedeTarget}
          onClose={() => setSupersedeTarget(null)}
        />
      </CardContent>
    </Card>
  );
}

interface CreateDialogProps {
  goalId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

function CreateDeliverableDialog({
  goalId,
  open,
  onOpenChange,
}: CreateDialogProps) {
  const create = useCreateDeliverable();
  const [name, setName] = useState("");
  const [kind, setKind] = useState("");
  const [contentRef, setContentRef] = useState("");
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setName("");
    setKind("");
    setContentRef("");
    setError(null);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) {
      setError("Name is required.");
      return;
    }
    if (!kind.trim()) {
      setError("Kind is required.");
      return;
    }
    setError(null);
    try {
      await create.mutateAsync({
        goalId,
        name: name.trim(),
        kind: kind.trim(),
        ...(contentRef.trim() ? { contentRef: contentRef.trim() } : {}),
      });
      reset();
      onOpenChange(false);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to create deliverable.",
      );
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
            <DialogTitle>New deliverable</DialogTitle>
            <DialogDescription>
              Register an artifact this goal produces. Finalizing it later
              wakes any goals that depend on a deliverable with this name.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-2">
            <Label htmlFor="deliverable-name">Name</Label>
            <Input
              id="deliverable-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. api-spec"
              autoFocus
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor="deliverable-kind">Kind</Label>
            <Input
              id="deliverable-kind"
              list="deliverable-kind-options"
              value={kind}
              onChange={(e) => setKind(e.target.value)}
              placeholder="spec / code / report / image"
            />
            <datalist id="deliverable-kind-options">
              {KIND_HINTS.map((k) => (
                <option key={k} value={k} />
              ))}
            </datalist>
          </div>

          <div className="space-y-2">
            <Label htmlFor="deliverable-content-ref">
              Content ref (optional)
            </Label>
            <Input
              id="deliverable-content-ref"
              value={contentRef}
              onChange={(e) => setContentRef(e.target.value)}
              placeholder="path or URL"
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
              disabled={create.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={create.isPending}>
              {create.isPending ? "Creating…" : "Create"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

interface SupersedeDialogProps {
  deliverable: Deliverable | null;
  onClose: () => void;
}

function SupersedeDeliverableDialog({
  deliverable,
  onClose,
}: SupersedeDialogProps) {
  const supersede = useSupersedeDeliverable();
  const [newContentRef, setNewContentRef] = useState("");
  const [finalize, setFinalize] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setNewContentRef("");
    setFinalize(false);
    setError(null);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!deliverable) return;
    if (!newContentRef.trim()) {
      setError("New content ref is required.");
      return;
    }
    setError(null);
    try {
      await supersede.mutateAsync({
        deliverableId: deliverable._id,
        newContentRef: newContentRef.trim(),
        finalize,
      });
      reset();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to supersede.");
    }
  };

  return (
    <Dialog
      open={deliverable !== null}
      onOpenChange={(o) => {
        if (!o) {
          reset();
          onClose();
        }
      }}
    >
      <DialogContent>
        <form onSubmit={handleSubmit} className="space-y-4">
          <DialogHeader>
            <DialogTitle>
              Supersede {deliverable ? `'${deliverable.name}'` : "deliverable"}
            </DialogTitle>
            <DialogDescription>
              Marks the current deliverable obsolete and creates a new one
              under the same name. Optionally finalize the new one
              immediately.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-2">
            <Label htmlFor="supersede-content-ref">New content ref</Label>
            <Input
              id="supersede-content-ref"
              value={newContentRef}
              onChange={(e) => setNewContentRef(e.target.value)}
              placeholder="path or URL"
              autoFocus
            />
          </div>

          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={finalize}
              onChange={(e) => setFinalize(e.target.checked)}
            />
            Finalize the new deliverable immediately
          </label>

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
              onClick={onClose}
              disabled={supersede.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={supersede.isPending}>
              {supersede.isPending ? "Superseding…" : "Supersede"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
