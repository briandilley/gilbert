import { useCallback, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Plus, RefreshCw, RotateCw, Settings } from "lucide-react";
import { TaskListSidebar } from "./TaskListSidebar";
import { TaskRow } from "./TaskRow";
import { TaskEditDrawer } from "./TaskEditDrawer";
import { TaskListEditDrawer } from "./TaskListEditDrawer";
import type { Task, TaskList } from "@/types/tasks";

/** Multi-list tasks page.
 *
 * Layout mirrors the calendar / feeds SPAs: sidebar of accessible
 * lists on the left, task list + filter controls on the right. The
 * "All tasks" pseudo-row aggregates rows across every accessible list.
 */
export function TasksPage() {
  const api = useWsApi();
  const queryClient = useQueryClient();
  const [selectedListId, setSelectedListId] = useState<string | null>(null);
  const [editTask, setEditTask] = useState<Task | null>(null);
  const [creatingTask, setCreatingTask] = useState(false);
  const [editList, setEditList] = useState<TaskList | null>(null);
  const [creatingList, setCreatingList] = useState(false);
  const [statusFilter, setStatusFilter] = useState<string>("open");
  const [tagFilter, setTagFilter] = useState("");

  const listsQuery = useQuery({
    queryKey: ["tasks.lists"],
    queryFn: () => api.listTaskLists(),
  });

  const lists = listsQuery.data ?? [];
  const selectedList = useMemo(
    () =>
      selectedListId
        ? (lists.find((tl) => tl.id === selectedListId) ?? null)
        : null,
    [lists, selectedListId],
  );

  const tasksQuery = useQuery({
    queryKey: ["tasks.list", selectedListId, statusFilter, tagFilter],
    enabled: lists.length > 0,
    queryFn: () =>
      api.listTasks({
        list_id: selectedListId ?? undefined,
        status: statusFilter,
        tag: tagFilter || undefined,
        limit: 200,
      }),
  });

  const completeMutation = useMutation({
    mutationFn: (taskId: string) => api.completeTask(taskId),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["tasks.list"] }),
  });

  const refreshMutation = useMutation({
    mutationFn: (listId: string) => api.refreshTaskList(listId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tasks.lists"] });
      queryClient.invalidateQueries({ queryKey: ["tasks.list"] });
    },
  });

  const handleComplete = useCallback(
    (taskId: string) => completeMutation.mutate(taskId),
    [completeMutation],
  );

  const defaultListId =
    selectedListId ??
    lists.find((tl) => tl.is_default)?.id ??
    lists[0]?.id ??
    "";

  return (
    <div className="flex h-full overflow-hidden">
      <TaskListSidebar
        lists={lists}
        selectedListId={selectedListId}
        onSelect={setSelectedListId}
        onAddList={() => setCreatingList(true)}
        onEditList={(tl) => setEditList(tl)}
        loading={listsQuery.isLoading}
      />
      <main className="flex-1 overflow-y-auto p-6 space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-semibold">
              {selectedList ? selectedList.name : "All tasks"}
            </h1>
            {selectedList && (
              <p className="text-sm text-muted-foreground">
                {selectedList.backend_name}
                {selectedList.last_error && (
                  <Badge variant="destructive" className="ml-2">
                    {selectedList.last_error}
                  </Badge>
                )}
              </p>
            )}
          </div>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="icon"
              onClick={() =>
                queryClient.invalidateQueries({ queryKey: ["tasks.list"] })
              }
              title="Refresh"
            >
              <RefreshCw className="h-4 w-4" />
            </Button>
            {selectedList && selectedList.can_admin && (
              <>
                <Button
                  variant="outline"
                  size="icon"
                  onClick={() => refreshMutation.mutate(selectedList.id)}
                  disabled={refreshMutation.isPending}
                  title="Sync now"
                >
                  <RotateCw
                    className={`h-4 w-4 ${
                      refreshMutation.isPending ? "animate-spin" : ""
                    }`}
                  />
                </Button>
                <Button
                  variant="outline"
                  size="icon"
                  onClick={() => setEditList(selectedList)}
                  title="Edit list"
                >
                  <Settings className="h-4 w-4" />
                </Button>
              </>
            )}
            <Button
              onClick={() => setCreatingTask(true)}
              disabled={lists.length === 0}
            >
              <Plus className="h-4 w-4 mr-1" /> Add task
            </Button>
          </div>
        </div>

        <div className="flex items-center gap-2 flex-wrap">
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="border rounded-md px-2 py-1 text-sm bg-background"
          >
            <option value="open">Open</option>
            <option value="done">Done</option>
            <option value="cancelled">Cancelled</option>
            <option value="all">All</option>
          </select>
          <Input
            placeholder="Filter by tag…"
            value={tagFilter}
            onChange={(e) => setTagFilter(e.target.value)}
            className="max-w-xs"
          />
        </div>

        <div className="space-y-2">
          {tasksQuery.isLoading && (
            <div className="text-sm text-muted-foreground">Loading…</div>
          )}
          {tasksQuery.data?.length === 0 && (
            <div className="text-sm text-muted-foreground">
              No tasks match the current filters.
            </div>
          )}
          {tasksQuery.data?.map((task) => (
            <TaskRow
              key={task.id}
              task={task}
              onComplete={handleComplete}
              onEdit={(t) => setEditTask(t)}
            />
          ))}
        </div>
      </main>

      {(editTask !== null || creatingTask) && (
        <TaskEditDrawer
          task={editTask}
          defaultListId={defaultListId}
          lists={lists}
          onClose={() => {
            setEditTask(null);
            setCreatingTask(false);
          }}
          onSaved={() => {
            queryClient.invalidateQueries({ queryKey: ["tasks.lists"] });
            queryClient.invalidateQueries({ queryKey: ["tasks.list"] });
          }}
        />
      )}

      {(editList !== null || creatingList) && (
        <TaskListEditDrawer
          list={editList}
          onClose={() => {
            setEditList(null);
            setCreatingList(false);
          }}
          onSaved={() => {
            queryClient.invalidateQueries({ queryKey: ["tasks.lists"] });
            queryClient.invalidateQueries({ queryKey: ["tasks.list"] });
          }}
        />
      )}
    </div>
  );
}

