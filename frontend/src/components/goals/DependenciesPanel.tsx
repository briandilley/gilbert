/**
 * DependenciesPanel — right-rail card on the war-room page that lists
 * outgoing (this goal depends on …) and incoming (… depends on this
 * goal) dependency rows. Drivers can add/remove outgoing deps; the
 * backend ultimately gates each mutation.
 *
 * Each dep row points at a goal by id; we resolve names via
 * ``useGoals()`` so deleted/cross-owner goals fall back to the raw id.
 */

import { useMemo, useState } from "react";
import { CheckIcon, CircleIcon, PlusIcon, XIcon } from "lucide-react";
import { Link } from "react-router-dom";
import {
  useAddDependency,
  useDependencies,
  useGoals,
  useRemoveDependency,
} from "@/api/goals";
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { Goal, GoalDependency } from "@/types/agent";

interface Props {
  goalId: string;
  isDriver: boolean;
}

export function DependenciesPanel({ goalId, isDriver }: Props) {
  const outgoingQuery = useDependencies(goalId, undefined);
  const incomingQuery = useDependencies(undefined, goalId);
  const { data: goals } = useGoals();
  const [addOpen, setAddOpen] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const goalById = useMemo<Record<string, Goal>>(() => {
    const m: Record<string, Goal> = {};
    for (const g of goals ?? []) m[g._id] = g;
    return m;
  }, [goals]);

  const remove = useRemoveDependency();
  const handleRemove = async (depId: string) => {
    setActionError(null);
    try {
      await remove.mutateAsync(depId);
    } catch (err) {
      setActionError(
        err instanceof Error ? err.message : "Failed to remove dependency.",
      );
    }
  };

  return (
    <Card size="sm">
      <CardContent>
        <div className="flex items-center justify-between gap-2">
          <h3 className="text-sm font-semibold">Dependencies</h3>
          {isDriver && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => setAddOpen(true)}
            >
              <PlusIcon /> Add
            </Button>
          )}
        </div>

        {actionError && (
          <div
            role="alert"
            className="mt-2 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive"
          >
            {actionError}
          </div>
        )}

        <div className="mt-3 space-y-3">
          <Section
            title="Depends on"
            query={outgoingQuery}
            empty="No outgoing dependencies."
            renderRow={(dep) => (
              <DependencyRow
                key={dep._id}
                dep={dep}
                otherGoal={goalById[dep.source_goal_id]}
                otherGoalId={dep.source_goal_id}
                canRemove={isDriver}
                isPending={remove.isPending}
                onRemove={() => handleRemove(dep._id)}
              />
            )}
          />

          <Section
            title="Depended on by"
            query={incomingQuery}
            empty="No incoming dependencies."
            renderRow={(dep) => (
              <DependencyRow
                key={dep._id}
                dep={dep}
                otherGoal={goalById[dep.dependent_goal_id]}
                otherGoalId={dep.dependent_goal_id}
                canRemove={false}
                isPending={false}
                onRemove={() => {}}
              />
            )}
          />
        </div>

        <AddDependencyDialog
          dependentGoalId={goalId}
          open={addOpen}
          onOpenChange={setAddOpen}
          goals={goals ?? []}
        />
      </CardContent>
    </Card>
  );
}

interface SectionProps {
  title: string;
  query: ReturnType<typeof useDependencies>;
  empty: string;
  renderRow: (dep: GoalDependency) => React.ReactNode;
}

function Section({ title, query, empty, renderRow }: SectionProps) {
  return (
    <div>
      <div className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        {title}
      </div>
      {query.isPending && (
        <div className="mt-1">
          <LoadingSpinner text="Loading…" />
        </div>
      )}
      {query.isError && (
        <div
          role="alert"
          className="mt-1 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive"
        >
          Failed to load dependencies.
        </div>
      )}
      {query.data && query.data.length === 0 && (
        <div className="mt-1 text-xs text-muted-foreground italic">
          {empty}
        </div>
      )}
      {query.data && query.data.length > 0 && (
        <ul className="mt-1 space-y-1.5">{query.data.map(renderRow)}</ul>
      )}
    </div>
  );
}

interface DependencyRowProps {
  dep: GoalDependency;
  otherGoal: Goal | undefined;
  otherGoalId: string;
  canRemove: boolean;
  isPending: boolean;
  onRemove: () => void;
}

function DependencyRow({
  dep,
  otherGoal,
  otherGoalId,
  canRemove,
  isPending,
  onRemove,
}: DependencyRowProps) {
  const satisfied = dep.satisfied_at !== null;
  return (
    <li className="flex items-start gap-2 rounded-md border bg-muted/20 px-2.5 py-1.5">
      <span
        className={`mt-0.5 ${
          satisfied
            ? "text-green-600 dark:text-green-400"
            : "text-muted-foreground"
        }`}
        title={satisfied ? "Satisfied" : "Pending"}
        aria-label={satisfied ? "Satisfied" : "Pending"}
      >
        {satisfied ? (
          <CheckIcon className="size-3.5" />
        ) : (
          <CircleIcon className="size-3.5" />
        )}
      </span>
      <div className="min-w-0 flex-1">
        <Link
          to={`/goals/${otherGoalId}`}
          className="text-sm font-medium hover:underline truncate block"
          title={otherGoal?.name ?? otherGoalId}
        >
          {otherGoal?.name ?? otherGoalId}
        </Link>
        <div className="text-[11px] text-muted-foreground">
          requires{" "}
          <span className="font-mono">{dep.required_deliverable_name}</span>
        </div>
      </div>
      {canRemove && (
        <button
          type="button"
          onClick={onRemove}
          disabled={isPending}
          className="rounded-full p-0.5 text-muted-foreground hover:bg-accent hover:text-foreground"
          aria-label="Remove dependency"
          title="Remove dependency"
        >
          <XIcon className="size-3.5" />
        </button>
      )}
    </li>
  );
}

interface AddDialogProps {
  dependentGoalId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  goals: Goal[];
}

function AddDependencyDialog({
  dependentGoalId,
  open,
  onOpenChange,
  goals,
}: AddDialogProps) {
  const add = useAddDependency();
  const [sourceGoalId, setSourceGoalId] = useState("");
  const [requiredName, setRequiredName] = useState("");
  const [error, setError] = useState<string | null>(null);

  const candidates = goals.filter((g) => g._id !== dependentGoalId);

  const reset = () => {
    setSourceGoalId("");
    setRequiredName("");
    setError(null);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!sourceGoalId) {
      setError("Pick a source goal.");
      return;
    }
    if (!requiredName.trim()) {
      setError("Required deliverable name is required.");
      return;
    }
    setError(null);
    try {
      await add.mutateAsync({
        dependentGoalId,
        sourceGoalId,
        requiredDeliverableName: requiredName.trim(),
      });
      reset();
      onOpenChange(false);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to add dependency.",
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
            <DialogTitle>Add dependency</DialogTitle>
            <DialogDescription>
              This goal will be marked dependency-blocked until the source
              goal finalizes a deliverable with the given name.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-2">
            <Label>Source goal</Label>
            <Select
              value={sourceGoalId}
              onValueChange={(v) => setSourceGoalId(v ?? "")}
            >
              <SelectTrigger className="w-full">
                <SelectValue placeholder="Pick a goal…" />
              </SelectTrigger>
              <SelectContent>
                {candidates.map((g) => (
                  <SelectItem key={g._id} value={g._id}>
                    {g.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {candidates.length === 0 && (
              <div className="text-xs text-muted-foreground">
                No other goals to depend on.
              </div>
            )}
          </div>

          <div className="space-y-2">
            <Label htmlFor="dep-required-name">Required deliverable name</Label>
            <Input
              id="dep-required-name"
              value={requiredName}
              onChange={(e) => setRequiredName(e.target.value)}
              placeholder="e.g. api-spec"
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
              disabled={add.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={add.isPending}>
              {add.isPending ? "Adding…" : "Add"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
