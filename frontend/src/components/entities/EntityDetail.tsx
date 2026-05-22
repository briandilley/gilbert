import { useParams, Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { Card, CardContent } from "@/components/ui/card";
import { PageHeader } from "@/components/layout/PageHeader";

export function EntityDetail() {
  const { collection, entityId } = useParams<{
    collection: string;
    entityId: string;
  }>();

  const api = useWsApi();
  const { connected } = useWebSocket();
  const { data, isLoading } = useQuery({
    queryKey: ["entity", collection, entityId],
    queryFn: () => api.getEntity(collection!, entityId!),
    enabled: !!collection && !!entityId && connected,
  });

  return (
    <div>
      <PageHeader
        eyebrow={
          <span className="space-x-1">
            <Link to="/entities" className="hover:text-foreground transition-colors">
              DATA / ENTITIES
            </Link>
            <span className="text-muted-foreground/60">/</span>
            {data && (
              <Link
                to={`/entities/${encodeURIComponent(data.collection)}`}
                className="hover:text-foreground transition-colors"
              >
                {data.collection.toUpperCase()}
              </Link>
            )}
          </span>
        }
        title={
          <code className="font-mono text-xl break-all">
            {data?.entity_id ?? entityId}
          </code>
        }
      />

      <div className="mx-auto max-w-4xl px-4 py-4 sm:px-6 sm:py-6">
        {isLoading ? (
          <div className="text-xs text-muted-foreground">Loading…</div>
        ) : data ? (
          <Card>
            <CardContent className="pt-4">
              <pre className="font-mono text-xs leading-relaxed overflow-x-auto whitespace-pre-wrap break-words">
                {JSON.stringify(data.entity, null, 2)}
              </pre>
            </CardContent>
          </Card>
        ) : null}
      </div>
    </div>
  );
}
