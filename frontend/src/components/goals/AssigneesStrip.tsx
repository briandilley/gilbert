import { useState } from "react";
import { PlusIcon, XIcon } from "lucide-react";
import { useAgents } from "@/api/agents";
import {
  useAssignAgentToGoal,
  useGoalAssignments,
  useUnassignAgent,
} from "@/api/goals";
import { AgentAvatar } from "@/components/agent/AgentAvatar";
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
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { Agent, AssignmentRole } from "@/types/agent";

const ROLE_LABEL: Record<AssignmentRole, string> = {
  driver: "Driver",
  collaborator: "Collaborator",
  reviewer: "Reviewer",
};

const ROLE_PILL_CLASS: Record<AssignmentRole, string> = {
  driver: "bg-blue-500/15 text-blue-600 dark:text-blue-400",
  collaborator: "bg-muted text-muted-foreground",
  reviewer: "bg-purple-500/15 text-purple-600 dark:text-purple-400",
};

interface Props {
  goalId: string;
}

/**
 * Horizontal strip of agent avatars for everyone actively assigned to a
 * goal, plus a "+" button that opens an "add assignee" dialog. Each
 * avatar shows a role chip; an inline X unassigns. Uses
 * ``useGoalAssignments`` for the active list and ``useAgents`` to
 * resolve agent_id -> name/avatar.
 */
export function AssigneesStrip({ goalId }: Props) {
  const [addOpen, setAddOpen] = useState(false);
  const { data: assignments } = useGoalAssignments(goalId, {
    activeOnly: true,
  });
  const { data: agents } = useAgents();
  const unassign = useUnassignAgent();
  const [actionError, setActionError] = useState<string | null>(null);

  const agentById: Record<string, Agent> = {};
  for (const a of agents ?? []) agentById[a._id] = a;

  const assignedIds = new Set(assignments?.map((a) => a.agent_id) ?? []);
  const candidates = (agents ?? []).filter((a) => !assignedIds.has(a._id));

  const handleUnassign = async (agentId: string) => {
    setActionError(null);
    try {
      await unassign.mutateAsync({ goalId, agentId });
    } catch (err) {
      setActionError(
        err instanceof Error ? err.message : "Failed to unassign.",
      );
    }
  };

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        {(assignments ?? []).map((assignment) => {
          const agent = agentById[assignment.agent_id];
          return (
            <div
              key={assignment._id}
              className="flex items-center gap-1.5 rounded-full border bg-muted/30 py-0.5 pr-1 pl-1.5"
            >
              {agent ? (
                <AgentAvatar agent={agent} size="sm" />
              ) : (
                <span
                  className="inline-flex size-6 items-center justify-center rounded-full bg-muted text-xs"
                  aria-hidden
                >
                  ?
                </span>
              )}
              <span className="text-xs font-medium truncate max-w-[8rem]">
                {agent?.name ?? assignment.agent_id}
              </span>
              <Badge
                className={`${ROLE_PILL_CLASS[assignment.role]} text-[10px]`}
                variant="outline"
              >
                {ROLE_LABEL[assignment.role]}
              </Badge>
              <button
                type="button"
                onClick={() => handleUnassign(assignment.agent_id)}
                className="rounded-full p-0.5 text-muted-foreground hover:bg-accent hover:text-foreground"
                title={`Unassign ${agent?.name ?? assignment.agent_id}`}
                aria-label={`Unassign ${agent?.name ?? assignment.agent_id}`}
                disabled={unassign.isPending}
              >
                <XIcon className="size-3" />
              </button>
            </div>
          );
        })}

        <Button
          variant="outline"
          size="sm"
          onClick={() => setAddOpen(true)}
          disabled={candidates.length === 0}
          title={
            candidates.length === 0
              ? "All agents are already assigned"
              : "Add assignee"
          }
        >
          <PlusIcon /> Add assignee
        </Button>
      </div>

      {actionError && (
        <div
          role="alert"
          className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive"
        >
          {actionError}
        </div>
      )}

      <AddAssigneeDialog
        goalId={goalId}
        open={addOpen}
        onOpenChange={setAddOpen}
        candidates={candidates}
      />
    </div>
  );
}

interface AddAssigneeDialogProps {
  goalId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  candidates: Agent[];
}

function AddAssigneeDialog({
  goalId,
  open,
  onOpenChange,
  candidates,
}: AddAssigneeDialogProps) {
  const assign = useAssignAgentToGoal();
  const [agentId, setAgentId] = useState("");
  const [role, setRole] = useState<AssignmentRole>("collaborator");
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setAgentId("");
    setRole("collaborator");
    setError(null);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!agentId) {
      setError("Pick an agent.");
      return;
    }
    setError(null);
    try {
      await assign.mutateAsync({ goalId, agentId, role });
      reset();
      onOpenChange(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to assign.");
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
            <DialogTitle>Add assignee</DialogTitle>
            <DialogDescription>
              Add an agent to this goal as a Collaborator, Reviewer, or
              Driver.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-2">
            <Label>Agent</Label>
            <Select
              value={agentId}
              onValueChange={(v) => setAgentId(v ?? "")}
            >
              <SelectTrigger className="w-full">
                <SelectValue placeholder="Select an agent…" />
              </SelectTrigger>
              <SelectContent>
                {candidates.map((a) => (
                  <SelectItem key={a._id} value={a._id}>
                    {a.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-2">
            <Label>Role</Label>
            <Select
              value={role}
              onValueChange={(v) => setRole(v as AssignmentRole)}
            >
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="driver">Driver</SelectItem>
                <SelectItem value="collaborator">Collaborator</SelectItem>
                <SelectItem value="reviewer">Reviewer</SelectItem>
              </SelectContent>
            </Select>
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
              disabled={assign.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={assign.isPending}>
              {assign.isPending ? "Assigning…" : "Add"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
