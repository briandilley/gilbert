import { useParams, Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

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

  if (isLoading) {
    return <div className="p-6 text-muted-foreground">Loading...</div>;
  }

  if (!data) return null;

  return (
    <div className="p-6 space-y-4 max-w-4xl mx-auto">
      <div className="flex items-center gap-2 text-sm">
        <Link to="/entities" className="text-muted-foreground hover:text-foreground">
          Entities
        </Link>
        <span className="text-muted-foreground">/</span>
        <Link
          to={`/entities/${encodeURIComponent(data.collection)}`}
          className="text-muted-foreground hover:text-foreground"
        >
          {data.collection}
        </Link>
        <span className="text-muted-foreground">/</span>
        <span className="font-medium">{data.entity_id}</span>
      </div>

      <Card>
        <CardContent className="pt-6">
          <pre className="text-sm overflow-x-auto whitespace-pre-wrap">
            {JSON.stringify(data.entity, null, 2)}
          </pre>
        </CardContent>
      </Card>
    </div>
  );
}
