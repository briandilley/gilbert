import { useCallback, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { PlusIcon, TargetIcon } from "lucide-react";
import { useAgents } from "@/api/agents";
import { useCreateGoal, useGoals } from "@/api/goals";
import { useEventBus } from "@/hooks/useEventBus";
import { Button } from "@/components/ui/button";
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
import { Textarea } from "@/components/ui/textarea";
import { GoalKanban } from "./GoalKanban";

export function GoalsListPage() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { data: goals, isPending, isError } = useGoals();
  const [dialogOpen, setDialogOpen] = useState(false);

  const invalidate = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ["goals", "list"] });
  }, [queryClient]);

  // Live updates — goals come and go from any agent / session.
  useEventBus("goal.created", invalidate);
  useEventBus("goal.updated", invalidate);
  useEventBus("goal.status.changed", invalidate);
  useEventBus("goal.assignment.changed", invalidate);

  return (
    <div className="p-4 sm:p-6 space-y-4 max-w-7xl mx-auto">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <TargetIcon className="size-5 text-muted-foreground" />
          <h1 className="text-xl sm:text-2xl font-semibold">Goals</h1>
        </div>
        <Button onClick={() => setDialogOpen(true)}>
          <PlusIcon /> New goal
        </Button>
      </div>

      {isPending && <LoadingSpinner text="Loading goals…" />}

      {isError && (
        <div
          role="alert"
          className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          Failed to load goals.
        </div>
      )}

      {!isPending && !isError && goals && goals.length === 0 && (
        <div className="rounded-md border border-dashed px-4 py-8 text-center text-sm text-muted-foreground">
          No goals yet — click "New goal" to create one.
        </div>
      )}

      {!isPending && !isError && goals && goals.length > 0 && (
        <GoalKanban goals={goals} />
      )}

      <NewGoalDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        onCreated={(goalId) => {
          setDialogOpen(false);
          navigate(`/goals/${encodeURIComponent(goalId)}`);
        }}
      />
    </div>
  );
}

interface NewGoalDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: (goalId: string) => void;
}

/**
 * "New goal" dialog — name + description + multi-select assignee
 * checkboxes. Submitting calls ``goals.create`` with the chosen peer
 * names (first becomes DRIVER on the backend).
 */
function NewGoalDialog({ open, onOpenChange, onCreated }: NewGoalDialogProps) {
  const { data: agents } = useAgents();
  const createGoal = useCreateGoal();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [assigneeNames, setAssigneeNames] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setName("");
    setDescription("");
    setAssigneeNames([]);
    setError(null);
  };

  const toggleAssignee = (agentName: string) => {
    setAssigneeNames((prev) =>
      prev.includes(agentName)
        ? prev.filter((n) => n !== agentName)
        : [...prev, agentName],
    );
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) {
      setError("Name is required.");
      return;
    }
    setError(null);
    try {
      const goal = await createGoal.mutateAsync({
        name: name.trim(),
        description: description.trim(),
        ...(assigneeNames.length > 0 ? { assign_to: assigneeNames } : {}),
      });
      reset();
      onCreated(goal._id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create goal.");
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
            <DialogTitle>New goal</DialogTitle>
            <DialogDescription>
              Create a goal and optionally assign one or more agents. The
              first assignee becomes DRIVER.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-2">
            <Label htmlFor="goal-name">Name</Label>
            <Input
              id="goal-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoFocus
              required
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor="goal-description">Description</Label>
            <Textarea
              id="goal-description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={4}
            />
          </div>

          <div className="space-y-2">
            <Label>Assignees</Label>
            {agents === undefined && (
              <div className="text-xs text-muted-foreground">
                Loading agents…
              </div>
            )}
            {agents && agents.length === 0 && (
              <div className="text-xs text-muted-foreground">
                No agents available — create one first.
              </div>
            )}
            {agents && agents.length > 0 && (
              <div className="max-h-40 space-y-1 overflow-y-auto rounded-md border p-2">
                {agents.map((a) => (
                  <label
                    key={a._id}
                    className="flex cursor-pointer items-center gap-2 rounded px-1 py-0.5 text-sm hover:bg-accent"
                  >
                    <input
                      type="checkbox"
                      checked={assigneeNames.includes(a.name)}
                      onChange={() => toggleAssignee(a.name)}
                    />
                    <span className="truncate">{a.name}</span>
                    {a.role_label && (
                      <span className="text-xs text-muted-foreground truncate">
                        — {a.role_label}
                      </span>
                    )}
                  </label>
                ))}
              </div>
            )}
            {assigneeNames.length > 0 && (
              <div className="text-xs text-muted-foreground">
                First assignee ({assigneeNames[0]}) will be DRIVER.
              </div>
            )}
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
              disabled={createGoal.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={createGoal.isPending}>
              {createGoal.isPending ? "Creating…" : "Create goal"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
