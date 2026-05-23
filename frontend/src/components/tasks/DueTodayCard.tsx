import { useQuery } from "@tanstack/react-query";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useWsApi } from "@/hooks/useWsApi";
import { CheckCircle2, ListTodo } from "lucide-react";
import { Link } from "react-router-dom";

/** Dashboard widget — today's tasks (due-today + overdue) across every
 * accessible list.
 *
 * Mounted via ``DashboardPage.tsx`` next to ``UpcomingEventCard`` and
 * ``BriefingCard``. Hidden when there's nothing to show.
 */
export function DueTodayCard() {
  const api = useWsApi();
  const todayQuery = useQuery({
    queryKey: ["tasks.due_today.card"],
    queryFn: () => api.tasksDueToday(),
  });
  const overdueQuery = useQuery({
    queryKey: ["tasks.overdue.card"],
    queryFn: () => api.tasksOverdue(),
  });

  if (todayQuery.isLoading || overdueQuery.isLoading) {
    return null;
  }

  const today = todayQuery.data ?? [];
  const overdue = overdueQuery.data ?? [];
  if (today.length === 0 && overdue.length === 0) {
    return null;
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          <ListTodo className="h-4 w-4" />
          Today's tasks
        </CardTitle>
      </CardHeader>
      <CardContent>
        {overdue.length > 0 && (
          <div className="text-sm text-destructive mb-2">
            {overdue.length} overdue
          </div>
        )}
        <ul className="space-y-1 text-sm">
          {[...today, ...overdue].slice(0, 5).map((t) => (
            <li key={t.id} className="flex items-center gap-2">
              <CheckCircle2 className="h-3 w-3 text-muted-foreground" />
              <span className="truncate">{t.title}</span>
            </li>
          ))}
        </ul>
        {today.length + overdue.length > 5 && (
          <Link
            to="/tasks"
            className="text-xs text-muted-foreground hover:text-foreground mt-2 inline-block"
          >
            +{today.length + overdue.length - 5} more →
          </Link>
        )}
      </CardContent>
    </Card>
  );
}

