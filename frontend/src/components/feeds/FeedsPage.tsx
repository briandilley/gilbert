import { useCallback, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Plus, RefreshCw, RotateCw, Settings } from "lucide-react";
import { FeedSidebar } from "./FeedSidebar";
import { FeedItemList } from "./FeedItemList";
import { FeedEditDrawer } from "./FeedEditDrawer";
import type { Feed } from "@/types/feeds";

/** Multi-feed news page.
 *
 * Layout mirrors the calendar SPA: sidebar of accessible feeds on the
 * left, item list + filter controls on the right. The "All feeds"
 * pseudo-row aggregates items across every accessible feed.
 *
 * URL state is intentionally kept simple (selected feed, search query)
 * since the surface is read-mostly; adding ``?feed=`` / ``?q=``
 * persistence would buy little until people actually reload a lot.
 */
export function FeedsPage() {
  const api = useWsApi();
  const queryClient = useQueryClient();
  const [selectedFeedId, setSelectedFeedId] = useState<string | null>(null);
  const [editFeed, setEditFeed] = useState<Feed | null>(null);
  const [creatingFeed, setCreatingFeed] = useState(false);
  const [query, setQuery] = useState("");
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [minScore, setMinScore] = useState(0);

  const feedsQuery = useQuery({
    queryKey: ["feeds.list"],
    queryFn: () => api.listFeeds(),
  });

  const feeds = feedsQuery.data ?? [];
  const selectedFeed = useMemo(
    () =>
      selectedFeedId
        ? (feeds.find((f) => f.id === selectedFeedId) ?? null)
        : null,
    [feeds, selectedFeedId],
  );

  const itemsQuery = useQuery({
    queryKey: [
      "feeds.items",
      selectedFeedId,
      query,
      unreadOnly,
      minScore,
    ],
    enabled: feeds.length > 0,
    queryFn: () =>
      api.listFeedItems({
        feed_id: selectedFeedId ?? undefined,
        query: query || undefined,
        unread_only: unreadOnly || undefined,
        min_score: minScore || undefined,
        limit: 50,
      }),
  });

  const markMutation = useMutation({
    mutationFn: ({ id, read }: { id: string; read: boolean }) =>
      api.markFeedItem(id, read),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["feeds.items"] }),
  });

  const pollMutation = useMutation({
    mutationFn: (feedId: string) => api.pollFeedNow(feedId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["feeds.list"] });
      queryClient.invalidateQueries({ queryKey: ["feeds.items"] });
    },
  });

  const handleMark = useCallback(
    (itemId: string, read: boolean) => markMutation.mutate({ id: itemId, read }),
    [markMutation],
  );

  return (
    <div className="flex h-full overflow-hidden">
      <FeedSidebar
        feeds={feeds}
        selectedFeedId={selectedFeedId}
        onSelect={setSelectedFeedId}
        onAddFeed={() => setCreatingFeed(true)}
        onEditFeed={(f) => setEditFeed(f)}
        loading={feedsQuery.isLoading}
      />
      <main className="flex-1 overflow-y-auto p-6 space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-semibold">
              {selectedFeed ? selectedFeed.name : "All feeds"}
            </h1>
            {selectedFeed && (
              <p className="text-sm text-muted-foreground">
                {selectedFeed.url}{" "}
                {selectedFeed.last_error && (
                  <Badge variant="destructive" className="ml-2">
                    {selectedFeed.last_error}
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
                queryClient.invalidateQueries({ queryKey: ["feeds.items"] })
              }
              title="Refresh"
            >
              <RefreshCw className="h-4 w-4" />
            </Button>
            {selectedFeed && selectedFeed.can_admin && (
              <>
                <Button
                  variant="outline"
                  size="icon"
                  onClick={() => pollMutation.mutate(selectedFeed.id)}
                  disabled={pollMutation.isPending}
                  title="Poll now"
                >
                  <RotateCw
                    className={`h-4 w-4 ${pollMutation.isPending ? "animate-spin" : ""}`}
                  />
                </Button>
                <Button
                  variant="outline"
                  size="icon"
                  onClick={() => setEditFeed(selectedFeed)}
                  title="Edit feed"
                >
                  <Settings className="h-4 w-4" />
                </Button>
              </>
            )}
            <Button
              onClick={() => setCreatingFeed(true)}
            >
              <Plus className="h-4 w-4 mr-1" /> Subscribe
            </Button>
          </div>
        </div>

        {selectedFeed && pollMutation.data && (
          <Card>
            <CardHeader>
              <CardTitle className="text-sm">Last manual poll</CardTitle>
            </CardHeader>
            <CardContent className="text-sm text-muted-foreground">
              {pollMutation.data.error ? (
                <span className="text-destructive">
                  Poll failed: {pollMutation.data.error}
                </span>
              ) : (
                <>
                  Saw {pollMutation.data.items_seen} item(s),{" "}
                  {pollMutation.data.items_new} new.
                </>
              )}
            </CardContent>
          </Card>
        )}

        <div className="flex items-center gap-2 flex-wrap">
          <Input
            placeholder="Search title / summary…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="max-w-sm"
          />
          <label className="flex items-center gap-1 text-sm">
            <input
              type="checkbox"
              checked={unreadOnly}
              onChange={(e) => setUnreadOnly(e.target.checked)}
            />
            Unread only
          </label>
          <label className="flex items-center gap-1 text-sm">
            Min score
            <Input
              type="number"
              min={0}
              max={1}
              step={0.1}
              value={minScore}
              onChange={(e) => setMinScore(parseFloat(e.target.value) || 0)}
              className="w-20"
            />
          </label>
        </div>

        <FeedItemList
          items={itemsQuery.data?.items ?? []}
          loading={itemsQuery.isLoading}
          onMarkRead={handleMark}
        />
      </main>

      {(editFeed !== null || creatingFeed) && (
        <FeedEditDrawer
          feed={editFeed}
          onClose={() => {
            setEditFeed(null);
            setCreatingFeed(false);
          }}
          onSaved={() => {
            queryClient.invalidateQueries({ queryKey: ["feeds.list"] });
            queryClient.invalidateQueries({ queryKey: ["feeds.items"] });
          }}
        />
      )}
    </div>
  );
}
