import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { useWsApi } from "@/hooks/useWsApi";
import { Newspaper, Play, RotateCw } from "lucide-react";

/** Dashboard card showing today's news briefing if available, with a
 * "Run now" button that builds one if today's hasn't fired yet.
 *
 * Hidden when no feeds exist (empty briefing → no value showing the
 * card). Hidden gracefully on RPC errors so a misconfigured feeds
 * service doesn't blow up the dashboard.
 */
export function BriefingCard() {
  const api = useWsApi();
  const queryClient = useQueryClient();
  const [expanded, setExpanded] = useState(false);

  const previewQuery = useQuery({
    queryKey: ["feeds.briefing.preview"],
    queryFn: () => api.previewBriefing({ top_n: 5 }),
    retry: false,
  });

  const runMutation = useMutation({
    mutationFn: () => api.runBriefing({ top_n: 5 }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["feeds.briefing.preview"] });
    },
  });

  const briefing = previewQuery.data;
  const hasContent = useMemo(
    () => Boolean(briefing && briefing.headlines.length > 0),
    [briefing],
  );

  if (previewQuery.isLoading) {
    return null;
  }
  if (previewQuery.isError) {
    // Likely no feeds capability or briefing not available — silently hide.
    return null;
  }
  if (!hasContent) {
    return null;
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          <Newspaper className="h-4 w-4" />
          Today's briefing
          <Button
            variant="ghost"
            size="sm"
            className="ml-auto h-7"
            onClick={() => runMutation.mutate()}
            disabled={runMutation.isPending}
            title="Build a fresh briefing"
          >
            {runMutation.isPending ? (
              <RotateCw className="h-3 w-3 animate-spin" />
            ) : (
              <Play className="h-3 w-3" />
            )}
            <span className="ml-1 text-xs">Run</span>
          </Button>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        <ul className="space-y-1">
          {briefing!.headlines.slice(0, 5).map((h) => (
            <li key={h.item_id} className="flex items-start gap-2">
              <Badge variant="secondary" className="text-xs">
                {h.score.toFixed(2)}
              </Badge>
              <div className="flex-1 min-w-0">
                {h.link ? (
                  <a
                    href={h.link}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="font-medium hover:underline"
                  >
                    {h.title}
                  </a>
                ) : (
                  <span className="font-medium">{h.title}</span>
                )}
                {h.one_liner && (
                  <p className="text-sm text-muted-foreground line-clamp-2">
                    {h.one_liner}
                  </p>
                )}
              </div>
            </li>
          ))}
        </ul>
        {briefing!.spoken && (
          <div>
            <Button
              variant="ghost"
              size="sm"
              className="h-6 text-xs"
              onClick={() => setExpanded((v) => !v)}
            >
              {expanded ? "Hide spoken text" : "Read aloud"}
            </Button>
            {expanded && (
              <p className="text-sm text-muted-foreground mt-1 italic">
                {briefing!.spoken}
              </p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
