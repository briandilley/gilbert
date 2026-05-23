import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { Task } from "@/types/tasks";
import { CheckCircle2, Circle, Clock, Pencil } from "lucide-react";

interface Props {
  task: Task;
  onComplete: (taskId: string) => void;
  onEdit: (task: Task) => void;
}

const PRIORITY_LABELS: Record<number, string> = {
  0: "",
  1: "low",
  2: "medium",
  3: "high",
  4: "urgent",
};

const PRIORITY_VARIANT: Record<number, "default" | "secondary" | "destructive" | "outline"> = {
  0: "outline",
  1: "outline",
  2: "secondary",
  3: "secondary",
  4: "destructive",
};

/** Single-task row. Inline complete toggle + edit drawer trigger.
 *
 * Sync status (pending_push / push_failed) surfaces as a small clock
 * icon so the user knows the row is in flight. Soft-deleted rows are
 * filtered upstream by ``listTasks``.
 */
export function TaskRow({ task, onComplete, onEdit }: Props) {
  const [completing, setCompleting] = useState(false);
  const isDone = task.status === "done";
  const isCancelled = task.status === "cancelled";

  const handleComplete = async () => {
    if (completing || isDone) return;
    setCompleting(true);
    try {
      onComplete(task.id);
    } finally {
      setCompleting(false);
    }
  };

  const dueLabel = task.due_at
    ? new Date(task.due_at).toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      })
    : "";

  return (
    <div
      className={`flex items-center gap-3 px-3 py-2 rounded-md border ${
        isDone || isCancelled
          ? "opacity-60 bg-muted/30"
          : "hover:bg-accent/30"
      }`}
    >
      <button
        onClick={handleComplete}
        disabled={completing || isDone || isCancelled}
        className="flex-shrink-0"
        title={isDone ? "Already complete" : "Mark complete"}
      >
        {isDone ? (
          <CheckCircle2 className="h-5 w-5 text-green-500" />
        ) : (
          <Circle className="h-5 w-5 text-muted-foreground hover:text-foreground" />
        )}
      </button>
      <div className="flex-1 min-w-0">
        <div
          className={`text-sm ${isDone || isCancelled ? "line-through" : ""}`}
        >
          {task.title}
        </div>
        {(task.notes || task.tags.length > 0 || task.project) && (
          <div className="text-xs text-muted-foreground truncate">
            {task.project && <span>{task.project} · </span>}
            {task.tags.length > 0 && (
              <span>{task.tags.map((t) => `#${t}`).join(" ")}</span>
            )}
            {task.notes && task.notes.length > 0 && (
              <span> · {task.notes.split("\n")[0].slice(0, 80)}</span>
            )}
          </div>
        )}
      </div>
      {task.priority > 0 && (
        <Badge variant={PRIORITY_VARIANT[task.priority] ?? "outline"}>
          {PRIORITY_LABELS[task.priority]}
        </Badge>
      )}
      {dueLabel && (
        <span className="text-xs text-muted-foreground whitespace-nowrap">
          {dueLabel}
        </span>
      )}
      {task.sync_status === "pending_push" && (
        <Clock
          className="h-3 w-3 text-amber-500"
          aria-label="Syncing"
        />
      )}
      {task.sync_status === "push_failed" && (
        <Badge variant="destructive" className="text-xs">
          push failed
        </Badge>
      )}
      <Button
        variant="ghost"
        size="icon"
        onClick={() => onEdit(task)}
        title="Edit"
      >
        <Pencil className="h-3 w-3" />
      </Button>
    </div>
  );
}

