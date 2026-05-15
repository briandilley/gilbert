import { useParams, useSearchParams, Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/layout/PageHeader";

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
    return <div className="p-4 sm:p-6 text-muted-foreground">Loading...</div>;
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
    <div>
      <PageHeader
        eyebrow={
          <Link
            to="/entities"
            className="hover:text-foreground transition-colors"
          >
            DATA / ENTITIES
          </Link>
        }
        title={<code className="font-mono text-xl">{data.collection}</code>}
        description={
          <span className="font-mono">
            {data.total} entit{data.total === 1 ? "y" : "ies"}
          </span>
        }
      />

      <div className="mx-auto max-w-4xl px-4 py-4 sm:px-6 sm:py-6 space-y-3">
        <Card>
          <CardContent className="p-0">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border">
                    <th className="px-3 py-2 text-left font-mono text-[11px] uppercase tracking-[0.08em] text-muted-foreground font-medium">
                      id
                    </th>
                    {columns.map((col) => (
                      <th
                        key={col}
                        className="px-3 py-2 text-left font-mono text-[11px] uppercase tracking-[0.08em] text-muted-foreground font-medium cursor-pointer hover:text-foreground select-none"
                        onClick={() => toggleSort(col)}
                      >
                        {col}
                        {currentSort === col && (
                          <span className="ml-1 text-(--signal)">
                            {currentDir === "desc" ? "↓" : "↑"}
                          </span>
                        )}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {data.entities.map((entity) => {
                    const id = entity._id as string;
                    return (
                      <tr key={id} className="hover:bg-foreground/[0.025] transition-colors">
                        <td className="px-3 py-2 font-mono text-xs">
                          <Link
                            to={`/entities/${encodeURIComponent(data.collection)}/${encodeURIComponent(id)}`}
                            className="text-(--signal) hover:underline"
                          >
                            {id.length > 20 ? id.slice(0, 20) + "…" : id}
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
                                  className="text-(--signal) hover:underline font-mono text-xs"
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
          <div className="flex items-center justify-end gap-2">
            <span className="font-mono text-xs text-muted-foreground mr-auto">
              page {data.page} of {data.total_pages}
            </span>
            <Button
              variant="outline"
              size="sm"
              onClick={() => changePage(data.page - 1)}
              disabled={data.page <= 1}
            >
              Previous
            </Button>
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
    </div>
  );
}
