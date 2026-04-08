import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import type { DocumentNode } from "@/types/documents";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

export function DocumentsPage() {
  const api = useWsApi();
  const { connected } = useWebSocket();
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);

  const { data, isLoading } = useQuery({
    queryKey: ["documents"],
    queryFn: api.listDocuments,
    enabled: connected,
  });

  const {
    data: searchResults,
    refetch: doSearch,
    isFetching: isSearching,
  } = useQuery({
    queryKey: ["document-search", query],
    queryFn: () => api.searchDocuments(query),
    enabled: false,
  });

  async function handleSearch(e: React.FormEvent) {
    e.preventDefault();
    if (!query.trim()) return;
    setSearching(true);
    await doSearch();
    setSearching(false);
  }

  if (isLoading) {
    return <div className="p-6 text-muted-foreground">Loading documents...</div>;
  }

  return (
    <div className="p-6 space-y-6 max-w-4xl mx-auto">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold text-center">Documents</h1>
        <form onSubmit={handleSearch} className="flex gap-2">
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search documents..."
            className="w-64"
          />
          <Button type="submit" disabled={isSearching}>
            Search
          </Button>
        </form>
      </div>

      {searching && searchResults?.results ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">
              Search Results ({searchResults.results.length})
            </CardTitle>
          </CardHeader>
          <CardContent>
            {searchResults.results.length === 0 ? (
              <p className="text-sm text-muted-foreground">No results found.</p>
            ) : (
              <div className="space-y-3">
                {searchResults.results.map((r, i) => (
                  <div key={i} className="border-b pb-2 last:border-0">
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-sm">
                        {r.document_name}
                      </span>
                      <Badge variant="secondary" className="text-xs">
                        {Math.round(r.relevance * 100)}%
                      </Badge>
                      {r.doc_type && (
                        <Badge variant="outline" className="text-xs">
                          {r.doc_type}
                        </Badge>
                      )}
                    </div>
                    <p className="text-xs text-muted-foreground mt-1 line-clamp-2">
                      {r.chunk_text}
                    </p>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      ) : (
        data?.sources.map((source) => (
          <Card key={source.source_id}>
            <CardHeader>
              <CardTitle className="text-sm">{source.source_name}</CardTitle>
            </CardHeader>
            <CardContent>
              <DocumentTree nodes={source.tree} sourceId={source.source_id} />
            </CardContent>
          </Card>
        ))
      )}
    </div>
  );
}

function DocumentTree({
  nodes,
  sourceId,
}: {
  nodes: DocumentNode[];
  sourceId: string;
}) {
  return (
    <div className="space-y-0.5">
      {nodes.map((node) => (
        <DocumentTreeNode key={node.path} node={node} sourceId={sourceId} />
      ))}
    </div>
  );
}

function DocumentTreeNode({
  node,
  sourceId,
}: {
  node: DocumentNode;
  sourceId: string;
}) {
  const [open, setOpen] = useState(false);

  if (node.is_folder) {
    return (
      <div>
        <button
          onClick={() => setOpen(!open)}
          className="flex items-center gap-1 text-sm hover:text-foreground text-muted-foreground py-0.5 w-full text-left"
        >
          <span className="text-xs">{open ? "▾" : "▸"}</span>
          <span>{node.name}</span>
        </button>
        {open && node.children && (
          <div className="ml-4">
            <DocumentTree nodes={node.children} sourceId={sourceId} />
          </div>
        )}
      </div>
    );
  }

  return (
    <a
      href={
        node.external_url ||
        `/documents/serve/${sourceId}/${encodeURIComponent(node.path)}`
      }
      target="_blank"
      rel="noopener noreferrer"
      className="flex items-center gap-2 text-sm py-0.5 ml-3 hover:text-foreground text-muted-foreground"
    >
      <span>{node.name}</span>
      {node.size !== undefined && (
        <span className="text-xs text-muted-foreground">
          ({formatSize(node.size)})
        </span>
      )}
    </a>
  );
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
