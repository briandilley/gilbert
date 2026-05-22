import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { Card, CardContent, CardEyebrow, CardHeader } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { PageHeader } from "@/components/layout/PageHeader";

export function EntitiesPage() {
  const api = useWsApi();
  const { connected } = useWebSocket();
  const { data, isLoading } = useQuery({
    queryKey: ["entity-collections"],
    queryFn: api.listCollections,
    enabled: connected,
  });

  return (
    <div>
      <PageHeader
        eyebrow="DATA"
        title="Entity browser"
        description="Every persisted collection in the entity store, grouped by namespace."
      />
      <div className="mx-auto max-w-4xl px-4 py-4 sm:px-6 sm:py-6 space-y-3">
        {isLoading ? (
          <div className="text-xs text-muted-foreground">Loading…</div>
        ) : (
          data?.groups.map((group) => (
            <Card key={group.namespace}>
              <CardHeader>
                <CardEyebrow>{group.namespace}</CardEyebrow>
              </CardHeader>
              <CardContent>
                <div className="divide-y divide-border">
                  {group.collections.map((col) => (
                    <Link
                      key={col.name}
                      to={`/entities/${encodeURIComponent(col.name)}`}
                      className="group flex items-center justify-between gap-3 py-2 text-sm transition-colors hover:text-foreground"
                    >
                      <span className="truncate font-medium">
                        {col.short_name || col.name}
                      </span>
                      <Badge variant="neutral">{col.count}</Badge>
                    </Link>
                  ))}
                </div>
              </CardContent>
            </Card>
          ))
        )}
      </div>
    </div>
  );
}
