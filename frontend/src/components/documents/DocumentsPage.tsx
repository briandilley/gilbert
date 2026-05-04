import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { FolderIcon, FolderOpenIcon, FileTextIcon, ChevronRightIcon } from "lucide-react";
import { PluginPanelSlot } from "@/components/PluginPanelSlot";

export function DocumentsPage() {
  const api = useWsApi();
  const { connected } = useWebSocket();
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);

  const { data: sources, isLoading } = useQuery({
    queryKey: ["document-sources"],
    queryFn: api.listDocumentSources,
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
    return (
      <div className="flex items-center justify-center p-12">
        <LoadingSpinner text="Loading sources..." />
      </div>
    );
  }

  return (
    <div className="p-4 sm:p-6 space-y-4 sm:space-y-6 max-w-4xl mx-auto">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <h1 className="text-xl sm:text-2xl font-semibold">Documents</h1>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-2 sm:shrink-0">
          {/* Toolbar slot — plugins drop 'Import from <source>' or
              'Sync now' buttons here. */}
          <PluginPanelSlot slot="documents.toolbar" />
          <form onSubmit={handleSearch} className="flex gap-2">
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search documents..."
              className="flex-1 sm:w-64 sm:flex-none"
            />
            <Button type="submit" disabled={isSearching}>
              Search
            </Button>
          </form>
        </div>
      </div>

      {searching && searchResults ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">
              Search Results ({searchResults.length})
            </CardTitle>
          </CardHeader>
          <CardContent>
            {searchResults.length === 0 ? (
              <p className="text-sm text-muted-foreground">No results found.</p>
            ) : (
              <div className="space-y-3">
                {searchResults.map((r, i) => (
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
        sources?.map((source) => (
          <Card key={source.source_id}>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm flex items-center gap-2">
                <FolderIcon className="size-4" />
                {source.source_name}
              </CardTitle>
            </CardHeader>
            <CardContent>
              <FolderBrowser sourceId={source.source_id} />
            </CardContent>
          </Card>
        ))
      )}
    </div>
  );
}

/** Lazy-loading folder browser for a single document source. */
function FolderBrowser({ sourceId }: { sourceId: string }) {
  const api = useWsApi();
  const { connected } = useWebSocket();

  const { data: children, isLoading } = useQuery({
    queryKey: ["doc-browse", sourceId, ""],
    queryFn: () => api.browseDocuments(sourceId),
    enabled: connected,
  });

  if (isLoading) return <LoadingSpinner text="Loading..." className="py-2" />;

  if (!children || children.length === 0) {
    return <p className="text-sm text-muted-foreground">No documents.</p>;
  }

  return (
    <div className="space-y-0.5">
      {children.map((child) =>
        child.is_folder ? (
          <LazyFolder key={child.path} sourceId={sourceId} path={child.path} name={child.name} />
        ) : (
          <FileRow key={child.path} sourceId={sourceId} item={child} />
        ),
      )}
    </div>
  );
}

/** A folder row that loads its children on click. */
function LazyFolder({
  sourceId,
  path,
  name,
}: {
  sourceId: string;
  path: string;
  name: string;
}) {
  const api = useWsApi();
  const { connected } = useWebSocket();
  const [open, setOpen] = useState(false);

  const { data: children, isLoading } = useQuery({
    queryKey: ["doc-browse", sourceId, path],
    queryFn: () => api.browseDocuments(sourceId, path),
    enabled: open && connected,
  });

  return (
    <div>
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 text-sm hover:text-foreground text-muted-foreground py-1 w-full text-left"
      >
        {open ? (
          <FolderOpenIcon className="size-4 shrink-0 text-amber-500" />
        ) : (
          <FolderIcon className="size-4 shrink-0 text-amber-500" />
        )}
        <span className="flex-1">{name}</span>
        <ChevronRightIcon
          className={`size-3.5 text-muted-foreground transition-transform ${open ? "rotate-90" : ""}`}
        />
      </button>
      {open && (
        <div className="ml-5 border-l pl-3">
          {isLoading ? (
            <LoadingSpinner className="py-1" />
          ) : !children || children.length === 0 ? (
            <p className="text-xs text-muted-foreground py-1">Empty folder</p>
          ) : (
            <div className="space-y-0.5">
              {children.map((child) =>
                child.is_folder ? (
                  <LazyFolder
                    key={child.path}
                    sourceId={sourceId}
                    path={child.path}
                    name={child.name}
                  />
                ) : (
                  <FileRow key={child.path} sourceId={sourceId} item={child} />
                ),
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

interface FileItem {
  name: string;
  path: string;
  is_folder: boolean;
  size?: number;
  modified?: string;
  type?: string;
  external_url?: string;
}

function FileRow({ sourceId, item }: { sourceId: string; item: FileItem }) {
  const href =
    item.external_url ||
    `/documents/serve/${sourceId}/${encodeURIComponent(item.path)}`;

  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="flex items-center gap-1.5 text-sm py-1 hover:text-foreground text-muted-foreground group"
    >
      <FileTextIcon className="size-4 shrink-0" />
      <span className="flex-1 truncate group-hover:underline">{item.name}</span>
      {item.size != null && item.size > 0 && (
        <span className="text-xs text-muted-foreground shrink-0">
          {formatSize(item.size)}
        </span>
      )}
      {item.modified && (
        <span className="text-xs text-muted-foreground shrink-0">
          {formatDate(item.modified)}
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

function formatDate(iso: string): string {
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  } catch {
    return "";
  }
}
