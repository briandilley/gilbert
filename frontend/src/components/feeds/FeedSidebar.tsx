import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import type { Feed } from "@/types/feeds";
import { Plus, Rss, Settings } from "lucide-react";

interface Props {
  feeds: Feed[];
  selectedFeedId: string | null;
  onSelect: (id: string | null) => void;
  onAddFeed: () => void;
  onEditFeed: (feed: Feed) => void;
  loading?: boolean;
}

/** Sidebar listing accessible feeds plus an "All" pseudo-row.
 *
 * Mirrors ``CalendarSidebar`` so the two pages have a consistent
 * navigation feel. Feeds with a non-empty ``last_error`` get a red "!"
 * badge so the operator notices broken polls without drilling in.
 */
export function FeedSidebar({
  feeds,
  selectedFeedId,
  onSelect,
  onAddFeed,
  onEditFeed,
  loading,
}: Props) {
  const totalUnread = feeds.reduce((sum, f) => sum + (f.unread_count ?? 0), 0);
  return (
    <aside className="w-64 border-r overflow-y-auto bg-muted/20">
      <div className="p-4">
        <h2 className="text-lg font-semibold mb-3">Feeds</h2>
        <Button variant="outline" size="sm" className="w-full" onClick={onAddFeed}>
          <Plus className="h-4 w-4 mr-1" /> Add feed
        </Button>
      </div>
      <nav className="px-2 pb-4 space-y-1">
        <button
          onClick={() => onSelect(null)}
          className={`w-full flex items-center gap-2 px-3 py-2 rounded-md text-sm text-left ${
            selectedFeedId === null ? "bg-accent text-accent-foreground" : "hover:bg-accent/50"
          }`}
        >
          <Rss className="h-4 w-4" />
          <span className="flex-1">All feeds</span>
          {totalUnread > 0 && (
            <Badge variant="secondary" className="text-xs">
              {totalUnread}
            </Badge>
          )}
        </button>
        {loading && (
          <div className="px-3 py-2 text-xs text-muted-foreground">Loading…</div>
        )}
        {feeds.map((f) => (
          <div key={f.id} className="flex items-center gap-1">
            <button
              onClick={() => onSelect(f.id)}
              className={`flex-1 flex items-center gap-2 px-3 py-2 rounded-md text-sm text-left ${
                selectedFeedId === f.id
                  ? "bg-accent text-accent-foreground"
                  : "hover:bg-accent/50"
              }`}
            >
              <Rss className="h-4 w-4" />
              <div className="flex-1 min-w-0">
                <div className="truncate">{f.name}</div>
                {f.category && (
                  <div className="text-xs text-muted-foreground truncate">
                    {f.category}
                  </div>
                )}
              </div>
              {(f.unread_count ?? 0) > 0 && (
                <Badge variant="secondary" className="text-xs">
                  {f.unread_count}
                </Badge>
              )}
              {f.last_error && (
                <Badge variant="destructive" className="text-xs">
                  !
                </Badge>
              )}
            </button>
            {f.can_admin && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  onEditFeed(f);
                }}
                className="p-1 rounded hover:bg-accent/50"
                title="Edit feed"
              >
                <Settings className="h-3 w-3" />
              </button>
            )}
          </div>
        ))}
        {!loading && feeds.length === 0 && (
          <div className="px-3 py-4 text-sm text-muted-foreground">
            No feeds yet. Click "Add feed" to subscribe to one.
          </div>
        )}
      </nav>
    </aside>
  );
}
