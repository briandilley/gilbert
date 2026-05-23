import { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { useWsApi } from "@/hooks/useWsApi";
import type { Task, TaskList } from "@/types/tasks";
import { Trash2 } from "lucide-react";

interface Props {
  /** If null the drawer is in "create" mode. */
  task: Task | null;
  /** Default list_id used in create mode. */
  defaultListId: string;
  /** Lists available for the list picker. */
  lists: TaskList[];
  onClose: () => void;
  onSaved: () => void;
}

const PRIORITY_OPTIONS: { value: number; label: string }[] = [
  { value: 0, label: "None" },
  { value: 1, label: "Low" },
  { value: 2, label: "Medium" },
  { value: 3, label: "High" },
  { value: 4, label: "Urgent" },
];

/** Create / edit / delete a task in a drawer.
 *
 * Soft-delete by default — admin force-delete is reserved for a
 * dedicated tooling path (the AI tool / WS RPC accepts ``force=true``).
 * The form asks the user once before performing the soft-delete to
 * mirror the AI tool's UIBlock confirm shape.
 */
export function TaskEditDrawer({
  task,
  defaultListId,
  lists,
  onClose,
  onSaved,
}: Props) {
  const api = useWsApi();
  const isCreate = task === null;

  const [listId, setListId] = useState(task?.list_id ?? defaultListId);
  const [title, setTitle] = useState(task?.title ?? "");
  const [notes, setNotes] = useState(task?.notes ?? "");
  const [dueAt, setDueAt] = useState(task?.due_at ?? "");
  const [dueAtTz, setDueAtTz] = useState(task?.due_at_tz ?? "");
  const [priority, setPriority] = useState(task?.priority ?? 0);
  const [tagsInput, setTagsInput] = useState(task?.tags.join(", ") ?? "");
  const [project, setProject] = useState(task?.project ?? "");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!task) return;
    setListId(task.list_id);
    setTitle(task.title);
    setNotes(task.notes);
    setDueAt(task.due_at);
    setDueAtTz(task.due_at_tz);
    setPriority(task.priority);
    setTagsInput(task.tags.join(", "));
    setProject(task.project);
  }, [task]);

  const tags = tagsInput
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);

  const create = useMutation({
    mutationFn: () =>
      api.addTask({
        list_id: listId,
        title,
        notes,
        due_at: dueAt,
        due_at_tz: dueAtTz,
        priority,
        tags,
        project,
      }),
    onSuccess: () => {
      onSaved();
      onClose();
    },
    onError: (e: Error) => setError(e.message || "Failed to add task"),
  });

  const update = useMutation({
    mutationFn: () =>
      api.updateTask(task!.id, {
        title,
        notes,
        due_at: dueAt,
        due_at_tz: dueAtTz,
        priority,
        tags,
        project,
      }),
    onSuccess: () => {
      onSaved();
      onClose();
    },
    onError: (e: Error) => setError(e.message || "Failed to update task"),
  });

  const remove = useMutation({
    mutationFn: () => api.deleteTask(task!.id, false),
    onSuccess: () => {
      onSaved();
      onClose();
    },
    onError: (e: Error) => setError(e.message || "Failed to delete task"),
  });

  const submit = () => {
    setError(null);
    if (!title) {
      setError("Title is required");
      return;
    }
    if (!listId) {
      setError("List is required");
      return;
    }
    if (isCreate) {
      create.mutate();
    } else {
      update.mutate();
    }
  };

  return (
    <Sheet open onOpenChange={(open) => (!open ? onClose() : null)}>
      <SheetContent className="w-full sm:max-w-md overflow-y-auto">
        <SheetHeader>
          <SheetTitle>{isCreate ? "Add task" : "Edit task"}</SheetTitle>
        </SheetHeader>

        <div className="space-y-4 mt-4">
          <div>
            <Label htmlFor="task-title">Title</Label>
            <Input
              id="task-title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="What needs doing?"
            />
          </div>

          {isCreate && (
            <div>
              <Label htmlFor="task-list">List</Label>
              <select
                id="task-list"
                value={listId}
                onChange={(e) => setListId(e.target.value)}
                className="w-full border rounded-md px-3 py-2 text-sm bg-background"
              >
                {lists.map((tl) => (
                  <option key={tl.id} value={tl.id}>
                    {tl.name}
                    {tl.is_default ? " (default)" : ""}
                  </option>
                ))}
              </select>
            </div>
          )}

          <div>
            <Label htmlFor="task-notes">Notes</Label>
            <textarea
              id="task-notes"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              rows={4}
              className="w-full border rounded-md px-3 py-2 text-sm bg-background"
            />
          </div>

          <div className="grid grid-cols-2 gap-2">
            <div>
              <Label htmlFor="task-due">Due (ISO)</Label>
              <Input
                id="task-due"
                value={dueAt}
                onChange={(e) => setDueAt(e.target.value)}
                placeholder="2026-05-09T17:00:00Z"
              />
            </div>
            <div>
              <Label htmlFor="task-tz">TZ (IANA)</Label>
              <Input
                id="task-tz"
                value={dueAtTz}
                onChange={(e) => setDueAtTz(e.target.value)}
                placeholder="America/Los_Angeles"
              />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-2">
            <div>
              <Label htmlFor="task-priority">Priority</Label>
              <select
                id="task-priority"
                value={priority}
                onChange={(e) => setPriority(parseInt(e.target.value))}
                className="w-full border rounded-md px-3 py-2 text-sm bg-background"
              >
                {PRIORITY_OPTIONS.map((p) => (
                  <option key={p.value} value={p.value}>
                    {p.label}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <Label htmlFor="task-project">Project</Label>
              <Input
                id="task-project"
                value={project}
                onChange={(e) => setProject(e.target.value)}
              />
            </div>
          </div>

          <div>
            <Label htmlFor="task-tags">Tags (comma-separated)</Label>
            <Input
              id="task-tags"
              value={tagsInput}
              onChange={(e) => setTagsInput(e.target.value)}
              placeholder="shopping, phone-call"
            />
          </div>

          {error && (
            <div className="text-sm text-destructive">{error}</div>
          )}

          <div className="flex justify-between pt-4">
            {!isCreate && (
              <>
                {!confirmDelete ? (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => setConfirmDelete(true)}
                    className="text-destructive"
                  >
                    <Trash2 className="h-4 w-4 mr-1" /> Delete
                  </Button>
                ) : (
                  <div className="flex gap-2">
                    <Button
                      variant="destructive"
                      size="sm"
                      onClick={() => remove.mutate()}
                      disabled={remove.isPending}
                    >
                      Confirm delete
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setConfirmDelete(false)}
                    >
                      Cancel
                    </Button>
                  </div>
                )}
              </>
            )}
            <div className="flex gap-2 ml-auto">
              <Button variant="outline" onClick={onClose}>
                Close
              </Button>
              <Button
                onClick={submit}
                disabled={create.isPending || update.isPending}
              >
                {isCreate ? "Add" : "Save"}
              </Button>
            </div>
          </div>

          <Separator />
          <div className="text-xs text-muted-foreground">
            Soft-delete is recoverable until retention elapses (default
            90 days). Admin tooling can hard-delete via the WS RPC if
            needed.
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}

