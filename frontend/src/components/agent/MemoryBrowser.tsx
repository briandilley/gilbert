import { useMemo, useState } from "react";
import {
  useAgentMemories,
  useSetMemoryState,
} from "@/api/agents";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { timeAgo } from "@/lib/timeAgo";
import type { AgentMemory, MemoryFilters, MemoryState } from "@/types/agent";

const KIND_OPTIONS = ["any", "fact", "preference", "decision", "daily", "dream"];

interface Props {
  agentId: string;
}

function truncate(text: string, n: number): string {
  if (text.length <= n) return text;
  return `${text.slice(0, n - 1).trimEnd()}…`;
}

type StateFilter = "all" | MemoryState;

export function MemoryBrowser({ agentId }: Props) {
  const [stateFilter, setStateFilter] = useState<StateFilter>("all");
  const [kindFilter, setKindFilter] = useState<string>("any");
  const [tagsInput, setTagsInput] = useState<string>("");
  const [query, setQuery] = useState<string>("");

  const filters = useMemo<MemoryFilters>(() => {
    const f: MemoryFilters = {};
    if (stateFilter !== "all") f.state = stateFilter;
    if (kindFilter !== "any") f.kind = kindFilter;
    const tags = tagsInput
      .split(",")
      .map((t) => t.trim())
      .filter((t) => t.length > 0);
    if (tags.length > 0) f.tags = tags;
    if (query.trim()) f.q = query.trim();
    return f;
  }, [stateFilter, kindFilter, tagsInput, query]);

  const memoriesQuery = useAgentMemories(agentId, filters);
  const setMemoryState = useSetMemoryState();

  const handleFlip = (mem: AgentMemory) => {
    const next: MemoryState =
      mem.state === "short_term" ? "long_term" : "short_term";
    setMemoryState.mutate({ memoryId: mem._id, state: next });
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1">
          <Label className="text-xs">State</Label>
          <div className="flex rounded-md border overflow-hidden">
            {(["all", "short_term", "long_term"] as StateFilter[]).map((s) => (
              <button
                type="button"
                key={s}
                onClick={() => setStateFilter(s)}
                className={`px-2.5 py-1 text-xs ${
                  stateFilter === s
                    ? "bg-primary text-primary-foreground"
                    : "hover:bg-muted"
                }`}
              >
                {s === "all"
                  ? "All"
                  : s === "short_term"
                    ? "Short term"
                    : "Long term"}
              </button>
            ))}
          </div>
        </div>

        <div className="flex flex-col gap-1">
          <Label htmlFor="memory-kind" className="text-xs">
            Kind
          </Label>
          <select
            id="memory-kind"
            value={kindFilter}
            onChange={(e) => setKindFilter(e.target.value)}
            className="h-8 rounded-md border border-input bg-transparent px-2 text-sm"
          >
            {KIND_OPTIONS.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </div>

        <div className="flex flex-col gap-1 flex-1 min-w-[200px]">
          <Label htmlFor="memory-tags" className="text-xs">
            Tags (comma-separated)
          </Label>
          <Input
            id="memory-tags"
            value={tagsInput}
            onChange={(e) => setTagsInput(e.target.value)}
            placeholder="e.g. work, urgent"
          />
        </div>

        <div className="flex flex-col gap-1 flex-1 min-w-[200px]">
          <Label htmlFor="memory-query" className="text-xs">
            Search
          </Label>
          <Input
            id="memory-query"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Full-text search…"
          />
        </div>
      </div>

      {memoriesQuery.isPending && <LoadingSpinner text="Loading memories…" />}

      {memoriesQuery.isError && (
        <div
          role="alert"
          className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          Failed to load memories.
        </div>
      )}

      {setMemoryState.isError && (
        <div
          role="alert"
          className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          {setMemoryState.error instanceof Error
            ? setMemoryState.error.message
            : "Failed to update memory state."}
        </div>
      )}

      {memoriesQuery.data && memoriesQuery.data.length === 0 && (
        <div className="rounded-md border border-dashed px-4 py-8 text-center text-sm text-muted-foreground">
          No memories match these filters.
        </div>
      )}

      {memoriesQuery.data && memoriesQuery.data.length > 0 && (
        <div className="overflow-x-auto rounded-md border">
          <table className="w-full text-sm">
            <thead className="bg-muted/40 text-left text-xs text-muted-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">Content</th>
                <th className="px-3 py-2 font-medium">State</th>
                <th className="px-3 py-2 font-medium">Kind</th>
                <th className="px-3 py-2 font-medium">Tags</th>
                <th className="px-3 py-2 font-medium">Created</th>
                <th className="px-3 py-2 font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {memoriesQuery.data.map((mem) => (
                <tr key={mem._id} className="border-t align-top">
                  <td className="px-3 py-2">
                    <span title={mem.content}>{truncate(mem.content, 80)}</span>
                  </td>
                  <td className="px-3 py-2">
                    <Badge
                      variant="outline"
                      className={
                        mem.state === "long_term"
                          ? "bg-blue-500/15 text-blue-600 dark:text-blue-400"
                          : "bg-muted"
                      }
                    >
                      {mem.state}
                    </Badge>
                  </td>
                  <td className="px-3 py-2 text-muted-foreground">
                    {mem.kind || "—"}
                  </td>
                  <td className="px-3 py-2">
                    {mem.tags.length === 0 ? (
                      <span className="text-muted-foreground">—</span>
                    ) : (
                      <div className="flex flex-wrap gap-1">
                        {mem.tags.map((t) => (
                          <Badge key={t} variant="secondary">
                            {t}
                          </Badge>
                        ))}
                      </div>
                    )}
                  </td>
                  <td className="px-3 py-2 text-muted-foreground whitespace-nowrap">
                    {timeAgo(mem.created_at)}
                  </td>
                  <td className="px-3 py-2">
                    <Button
                      variant="outline"
                      size="xs"
                      disabled={setMemoryState.isPending}
                      onClick={() => handleFlip(mem)}
                    >
                      {mem.state === "short_term" ? "Promote" : "Demote"}
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
