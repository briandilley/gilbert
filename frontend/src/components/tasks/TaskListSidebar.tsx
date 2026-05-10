import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import type { TaskList } from "@/types/tasks";
import { ListTodo, Plus, Settings, Star } from "lucide-react";

interface Props {
  lists: TaskList[];
  selectedListId: string | null;
  onSelect: (id: string | null) => void;
  onAddList: () => void;
  onEditList: (list: TaskList) => void;
  loading?: boolean;
}

/** Sidebar listing accessible task lists plus an "All tasks" pseudo-row.
 *
 * Mirrors ``FeedSidebar`` / ``CalendarSidebar`` for visual consistency.
 * Lists with a non-empty ``last_error`` get a destructive badge so the
 * operator notices broken polls without drilling in. The default list
 * gets a star icon so the AI's resolution choice is transparent.
 */
export function TaskListSidebar({
  lists,
  selectedListId,
  onSelect,
  onAddList,
  onEditList,
  loading,
}: Props) {
  return (
    <aside className="w-64 border-r overflow-y-auto bg-muted/20">
      <div className="p-4">
        <h2 className="text-lg font-semibold mb-3">Task lists</h2>
        <Button variant="outline" size="sm" className="w-full" onClick={onAddList}>
          <Plus className="h-4 w-4 mr-1" /> New list
        </Button>
      </div>
      <nav className="px-2 pb-4 space-y-1">
        <button
          onClick={() => onSelect(null)}
          className={`w-full flex items-center gap-2 px-3 py-2 rounded-md text-sm text-left ${
            selectedListId === null
              ? "bg-accent text-accent-foreground"
              : "hover:bg-accent/50"
          }`}
        >
          <ListTodo className="h-4 w-4" />
          <span className="flex-1">All tasks</span>
        </button>
        {loading && (
          <div className="px-3 py-2 text-xs text-muted-foreground">Loading…</div>
        )}
        {lists.map((tl) => (
          <div key={tl.id} className="flex items-center gap-1">
            <button
              onClick={() => onSelect(tl.id)}
              className={`flex-1 flex items-center gap-2 px-3 py-2 rounded-md text-sm text-left ${
                selectedListId === tl.id
                  ? "bg-accent text-accent-foreground"
                  : "hover:bg-accent/50"
              }`}
            >
              <ListTodo className="h-4 w-4" />
              <div className="flex-1 min-w-0">
                <div className="truncate flex items-center gap-1">
                  {tl.name}
                  {tl.is_default && (
                    <Star className="h-3 w-3 fill-yellow-400 text-yellow-400" />
                  )}
                </div>
                <div className="text-xs text-muted-foreground truncate">
                  {tl.backend_name}
                </div>
              </div>
              {tl.last_error && (
                <Badge variant="destructive" className="text-xs">
                  !
                </Badge>
              )}
            </button>
            {tl.can_admin && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  onEditList(tl);
                }}
                className="p-1 rounded hover:bg-accent/50"
                title="Edit list"
              >
                <Settings className="h-3 w-3" />
              </button>
            )}
          </div>
        ))}
        {!loading && lists.length === 0 && (
          <div className="px-3 py-4 text-sm text-muted-foreground">
            No lists yet. Click "New list" to create one.
          </div>
        )}
      </nav>
    </aside>
  );
}

