import { Link } from "react-router-dom";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { timeAgo } from "@/lib/timeAgo";
import type { Goal, GoalStatus } from "@/types/agent";

const STATUS_LABEL: Record<GoalStatus, string> = {
  new: "New",
  in_progress: "In progress",
  blocked: "Blocked",
  complete: "Complete",
  cancelled: "Cancelled",
};

const STATUS_PILL_CLASS: Record<GoalStatus, string> = {
  new: "bg-muted text-muted-foreground",
  in_progress: "bg-blue-500/15 text-blue-600 dark:text-blue-400",
  blocked: "bg-yellow-500/15 text-yellow-600 dark:text-yellow-400",
  complete: "bg-green-500/15 text-green-600 dark:text-green-400",
  cancelled: "bg-muted text-muted-foreground line-through",
};

export function GoalStatusPill({ status }: { status: GoalStatus }) {
  return (
    <Badge className={STATUS_PILL_CLASS[status]} variant="outline">
      {STATUS_LABEL[status]}
    </Badge>
  );
}

interface Props {
  goal: Goal;
  /**
   * Drag-start handler — the parent kanban wires this so the column
   * drop handler can read ``goal_id`` from ``e.dataTransfer``.
   */
  onDragStart?: (e: React.DragEvent<HTMLElement>, goal: Goal) => void;
}

/**
 * Compact card summarising a goal — used by the kanban columns.
 *
 * The body is intentionally light: name + status + lifetime cost +
 * created-at relative time. We deliberately do NOT call
 * ``useGoalAssignments`` per card, since the kanban can render dozens
 * of cards and each query is its own WS RPC roundtrip. The detailed
 * assignee strip lives on the war-room page (``WarRoomPage``).
 *
 * Deliverables / dependency-blocked badges ship in Phase 5; for now
 * we render an em-dash placeholder so the layout doesn't shift later.
 */
export function GoalCard({ goal, onDragStart }: Props) {
  const formattedCost = `$${goal.lifetime_cost_usd.toFixed(2)}`;
  return (
    <Link
      to={`/goals/${encodeURIComponent(goal._id)}`}
      className="block transition-opacity hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-xl"
      draggable={Boolean(onDragStart)}
      onDragStart={onDragStart ? (e) => onDragStart(e, goal) : undefined}
    >
      <Card size="sm">
        <CardContent>
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0 flex-1">
              <div className="font-medium truncate">{goal.name}</div>
              {goal.description && (
                <div className="mt-0.5 text-xs text-muted-foreground line-clamp-2">
                  {goal.description}
                </div>
              )}
            </div>
            <GoalStatusPill status={goal.status} />
          </div>
          <div className="mt-2 flex items-center justify-between text-xs text-muted-foreground">
            <span>Cost: {formattedCost}</span>
            <span>{timeAgo(goal.created_at)}</span>
          </div>
          <div className="mt-1 text-xs text-muted-foreground">
            Deliverables: — · Deps: —
          </div>
        </CardContent>
      </Card>
    </Link>
  );
}
