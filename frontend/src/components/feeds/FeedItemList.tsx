import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import type { FeedItem } from "@/types/feeds";
import { ExternalLink } from "lucide-react";

interface Props {
  items: FeedItem[];
  loading?: boolean;
  onMarkRead: (itemId: string, read: boolean) => void;
}

/** Item rows for the selected feed (or aggregated across feeds when
 * "All feeds" is selected). Sorted by ``received_at`` descending by the
 * server. Score badge is dimmed when ``lazy_score=true`` so the user
 * sees that a backlog drain is pending instead of a real ignore.
 */
export function FeedItemList({ items, loading, onMarkRead }: Props) {
  if (loading) {
    return <div className="text-sm text-muted-foreground p-4">Loading…</div>;
  }
  if (!items.length) {
    return (
      <div className="text-sm text-muted-foreground p-4">
        No items match — try lowering the score floor or showing read items.
      </div>
    );
  }
  return (
    <ul className="divide-y rounded-md border">
      {items.map((it) => {
        const scoreLabel =
          it.score < 0
            ? "—"
            : it.score.toFixed(2);
        const dim = it.read ? "opacity-60" : "";
        return (
          <li key={it.id} className={`p-3 flex flex-col gap-1 ${dim}`}>
            <div className="flex items-start gap-2">
              <Badge
                variant={it.score >= 0.5 ? "default" : "secondary"}
                title={
                  it.lazy_score
                    ? "Awaiting AI scoring (backlog)"
                    : it.score_reason
                }
                className={it.lazy_score ? "opacity-60" : ""}
              >
                {scoreLabel}
              </Badge>
              <div className="flex-1 min-w-0">
                <div className="font-medium leading-tight">
                  {it.title || "(untitled)"}
                </div>
                {it.summary && (
                  <div className="text-sm text-muted-foreground line-clamp-2">
                    {it.summary}
                  </div>
                )}
              </div>
              {it.link && (
                <a
                  href={it.link}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="p-1 rounded hover:bg-accent"
                  title="Open original"
                >
                  <ExternalLink className="h-4 w-4" />
                </a>
              )}
            </div>
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <span>{it.received_at?.slice(0, 10)}</span>
              {it.author && <span>· {it.author}</span>}
              {it.briefed_at && <span>· briefed</span>}
              {it.ingested_to_knowledge && <span>· in knowledge</span>}
              <Button
                variant="ghost"
                size="sm"
                className="ml-auto h-6 text-xs"
                onClick={() => onMarkRead(it.id, !it.read)}
              >
                {it.read ? "Mark unread" : "Mark read"}
              </Button>
            </div>
          </li>
        );
      })}
    </ul>
  );
}
