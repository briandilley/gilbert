import { useParams, useSearchParams, Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";

export function CollectionDetail() {
  const { collection } = useParams<{ collection: string }>();
  const [searchParams, setSearchParams] = useSearchParams();

  const api = useWsApi();
  const { connected } = useWebSocket();
  const { data, isLoading } = useQuery({
    queryKey: ["entity-collection", collection, searchParams.toString()],
    queryFn: () => api.queryCollection(collection!, {
      page: Number(searchParams.get("page") || 1),
      sort: searchParams.get("sort") || undefined,
      order: searchParams.get("order") || undefined,
    }),
    enabled: !!collection && connected,
  });

  if (isLoading) {
    return <div className="p-6 text-muted-foreground">Loading...</div>;
  }

  if (!data) return null;

  function changePage(page: number) {
    const params = new URLSearchParams(searchParams);
    params.set("page", String(page));
    setSearchParams(params);
  }

  function toggleSort(field: string) {
    const params = new URLSearchParams(searchParams);
    const currentSort = params.get("sort");
    const currentDir = params.get("dir");
    if (currentSort === field && currentDir !== "desc") {
      params.set("sort", field);
      params.set("dir", "desc");
    } else if (currentSort === field && currentDir === "desc") {
      params.delete("sort");
      params.delete("dir");
    } else {
      params.set("sort", field);
      params.delete("dir");
    }
    params.set("page", "1");
    setSearchParams(params);
  }

  const columns = data.display_columns ?? data.sortable_fields.slice(0, 6);
  const currentSort = searchParams.get("sort");
  const currentDir = searchParams.get("dir");

  return (
    <div className="p-6 space-y-4 max-w-4xl mx-auto">
      <div className="flex items-center gap-2">
        <Link to="/entities" className="text-muted-foreground hover:text-foreground text-sm">
          Entities
        </Link>
        <span className="text-muted-foreground">/</span>
        <h1 className="text-lg font-semibold">{data.collection}</h1>
        <span className="text-sm text-muted-foreground">
          ({data.total} entities)
        </span>
      </div>

      <Card>
        <CardContent className="p-0">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b">
                  <th className="px-3 py-2 text-left font-medium">ID</th>
                  {columns.map((col) => (
                    <th
                      key={col}
                      className="px-3 py-2 text-left font-medium cursor-pointer hover:text-foreground"
                      onClick={() => toggleSort(col)}
                    >
                      {col}
                      {currentSort === col && (
                        <span className="ml-1">
                          {currentDir === "desc" ? "↓" : "↑"}
                        </span>
                      )}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.entities.map((entity) => {
                  const id = entity._id as string;
                  return (
                    <tr key={id} className="border-b hover:bg-accent/50">
                      <td className="px-3 py-2">
                        <Link
                          to={`/entities/${encodeURIComponent(data.collection)}/${encodeURIComponent(id)}`}
                          className="text-primary hover:underline"
                        >
                          {id.length > 20 ? id.slice(0, 20) + "..." : id}
                        </Link>
                      </td>
                      {columns.map((col) => {
                        const val = entity[col];
                        const fkTarget = data.fk_map[col];
                        const display =
                          val === null || val === undefined
                            ? ""
                            : typeof val === "object"
                              ? JSON.stringify(val).slice(0, 50)
                              : String(val).slice(0, 50);
                        return (
                          <td key={col} className="px-3 py-2 truncate max-w-48">
                            {fkTarget && val ? (
                              <Link
                                to={`/entities/${encodeURIComponent(fkTarget)}/${encodeURIComponent(String(val))}`}
                                className="text-primary hover:underline"
                              >
                                {display}
                              </Link>
                            ) : (
                              display
                            )}
                          </td>
                        );
                      })}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>

      {data.total_pages > 1 && (
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => changePage(data.page - 1)}
            disabled={data.page <= 1}
          >
            Previous
          </Button>
          <span className="text-sm text-muted-foreground">
            Page {data.page} of {data.total_pages}
          </span>
          <Button
            variant="outline"
            size="sm"
            onClick={() => changePage(data.page + 1)}
            disabled={data.page >= data.total_pages}
          >
            Next
          </Button>
        </div>
      )}
    </div>
  );
}
