import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

export function EntitiesPage() {
  const api = useWsApi();
  const { connected } = useWebSocket();
  const { data, isLoading } = useQuery({
    queryKey: ["entity-collections"],
    queryFn: api.listCollections,
    enabled: connected,
  });

  if (isLoading) {
    return <div className="p-6 text-muted-foreground">Loading...</div>;
  }

  return (
    <div className="p-6 space-y-6 max-w-4xl mx-auto">
      <h1 className="text-2xl font-semibold text-center">Entity Browser</h1>

      {data?.groups.map((group) => (
        <Card key={group.namespace}>
          <CardHeader>
            <CardTitle className="text-sm">{group.namespace}</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-1">
              {group.collections.map((col) => (
                <Link
                  key={col.name}
                  to={`/entities/${encodeURIComponent(col.name)}`}
                  className="flex items-center justify-between rounded-md px-3 py-2 hover:bg-accent text-sm"
                >
                  <span>{col.short_name || col.name}</span>
                  <Badge variant="secondary">{col.count}</Badge>
                </Link>
              ))}
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
