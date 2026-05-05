import { useState } from "react";
import { useUpdateGoalStatus } from "@/api/goals";
import { GoalCard } from "./GoalCard";
import type { Goal, GoalStatus } from "@/types/agent";

const COLUMNS: Array<{ status: GoalStatus; label: string }> = [
  { status: "new", label: "New" },
  { status: "in_progress", label: "In progress" },
  { status: "blocked", label: "Blocked" },
  { status: "complete", label: "Complete" },
  { status: "cancelled", label: "Cancelled" },
];

const DRAG_KEY = "application/x-gilbert-goal-id";

interface Props {
  goals: Goal[];
}

/**
 * Five-column kanban for the goal list. Drag-and-drop between columns
 * uses the native HTML5 drag API (no new deps): ``GoalCard``'s
 * ``onDragStart`` stamps the goal id, the column's ``onDragOver``
 * preventDefaults to opt in as a drop target, and ``onDrop`` reads the
 * goal id and fires ``goals.update_status``.
 */
export function GoalKanban({ goals }: Props) {
  const updateStatus = useUpdateGoalStatus();
  const [hoverColumn, setHoverColumn] = useState<GoalStatus | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const handleDragStart = (
    e: React.DragEvent<HTMLElement>,
    goal: Goal,
  ) => {
    e.dataTransfer.setData(DRAG_KEY, goal._id);
    e.dataTransfer.effectAllowed = "move";
  };

  const handleDrop = async (
    e: React.DragEvent<HTMLDivElement>,
    target: GoalStatus,
  ) => {
    e.preventDefault();
    setHoverColumn(null);
    const goalId = e.dataTransfer.getData(DRAG_KEY);
    if (!goalId) return;
    const goal = goals.find((g) => g._id === goalId);
    if (!goal || goal.status === target) return;
    setActionError(null);
    try {
      await updateStatus.mutateAsync({ goalId, status: target });
    } catch (err) {
      setActionError(
        err instanceof Error ? err.message : "Failed to update status.",
      );
    }
  };

  return (
    <div className="space-y-3">
      {actionError && (
        <div
          role="alert"
          className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          {actionError}
        </div>
      )}
      <div className="grid grid-cols-1 gap-3 md:grid-cols-3 xl:grid-cols-5">
        {COLUMNS.map(({ status, label }) => {
          const columnGoals = goals.filter((g) => g.status === status);
          const isHover = hoverColumn === status;
          return (
            <div
              key={status}
              className={`flex min-h-[24rem] flex-col rounded-xl border bg-muted/30 p-2 transition-colors ${
                isHover ? "border-primary bg-primary/10" : ""
              }`}
              onDragOver={(e) => {
                e.preventDefault();
                e.dataTransfer.dropEffect = "move";
                setHoverColumn(status);
              }}
              onDragLeave={() => setHoverColumn(null)}
              onDrop={(e) => handleDrop(e, status)}
            >
              <div className="mb-2 flex items-center justify-between px-1">
                <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  {label}
                </h2>
                <span className="text-xs text-muted-foreground">
                  {columnGoals.length}
                </span>
              </div>
              <div className="flex flex-1 flex-col gap-2">
                {columnGoals.map((g) => (
                  <GoalCard
                    key={g._id}
                    goal={g}
                    onDragStart={handleDragStart}
                  />
                ))}
                {columnGoals.length === 0 && (
                  <div className="rounded-md border border-dashed p-4 text-center text-xs text-muted-foreground">
                    No goals
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
