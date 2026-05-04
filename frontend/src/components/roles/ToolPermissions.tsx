import { useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { ChevronRightIcon } from "lucide-react";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { Card } from "@/components/ui/card";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

interface Tool {
  tool_name: string;
  provider: string;
  default_role: string;
  effective_role: string;
  has_override: boolean;
}

export function ToolPermissions() {
  const queryClient = useQueryClient();
  const api = useWsApi();
  const { connected } = useWebSocket();
  const { data, isLoading } = useQuery({
    queryKey: ["tool-permissions"],
    queryFn: api.listToolPermissions,
    enabled: connected,
  });

  const setMutation = useMutation({
    mutationFn: (args: { toolName: string; role: string }) =>
      api.setToolRole(args.toolName, args.role),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["tool-permissions"] }),
  });

  const clearMutation = useMutation({
    mutationFn: api.clearToolRole,
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["tool-permissions"] }),
  });

  // Group by provider so each service's tools collapse into a single
  // header row. Providers are sorted alphabetically; within a provider,
  // tools keep the order the backend returned them.
  const groups = useMemo(() => {
    const map = new Map<string, Tool[]>();
    for (const t of (data?.tools ?? []) as Tool[]) {
      const key = t.provider || "(unspecified)";
      const arr = map.get(key) ?? [];
      arr.push(t);
      map.set(key, arr);
    }
    return [...map.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [data?.tools]);

  if (isLoading) return <LoadingSpinner text="Loading tools..." className="p-4" />;

  return (
    <>
      <h1 className="text-xl sm:text-2xl font-semibold text-center mb-4">Tools</h1>
      <div className="space-y-2">
        {groups.map(([provider, tools]) => (
          <ProviderGroup
            key={provider}
            provider={provider}
            tools={tools}
            roleNames={data?.role_names ?? []}
            overrideCount={tools.filter((t) => t.has_override).length}
            onSet={(toolName, role) =>
              setMutation.mutate({ toolName, role })
            }
            onClear={(toolName) => clearMutation.mutate(toolName)}
          />
        ))}
      </div>
    </>
  );
}

function ProviderGroup({
  provider,
  tools,
  roleNames,
  overrideCount,
  onSet,
  onClear,
}: {
  provider: string;
  tools: Tool[];
  roleNames: string[];
  overrideCount: number;
  onSet: (toolName: string, role: string) => void;
  onClear: (toolName: string) => void;
}) {
  // Default closed — the page is dense enough that auto-open would make
  // every tool table render at once. Caller can click to expand any
  // group they care about.
  const [open, setOpen] = useState(false);

  return (
    <Card className="overflow-hidden p-0">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 px-3 py-2 hover:bg-accent transition-colors text-left"
      >
        <ChevronRightIcon
          className={cn("size-4 transition-transform shrink-0", open && "rotate-90")}
        />
        <span className="font-medium">{provider}</span>
        <span className="text-xs text-muted-foreground">
          {tools.length} tool{tools.length === 1 ? "" : "s"}
        </span>
        {overrideCount > 0 && (
          <Badge variant="secondary" className="text-xs ml-auto">
            {overrideCount} override{overrideCount === 1 ? "" : "s"}
          </Badge>
        )}
      </button>
      {open && (
        <div className="overflow-x-auto border-t">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b">
                <th className="px-3 py-2 text-left font-medium">Tool</th>
                <th className="hidden md:table-cell px-3 py-2 text-left font-medium">
                  Default
                </th>
                <th className="px-3 py-2 text-left font-medium">Override</th>
                <th className="px-3 py-2 w-16"></th>
              </tr>
            </thead>
            <tbody>
              {tools.map((tool) => (
                <tr key={tool.tool_name} className="border-b last:border-b-0">
                  <td className="px-3 py-2 break-words">{tool.tool_name}</td>
                  <td className="hidden md:table-cell px-3 py-2">
                    <Badge variant="secondary" className="text-xs">
                      {tool.default_role}
                    </Badge>
                  </td>
                  <td className="px-3 py-2">
                    <Select
                      value={tool.has_override ? tool.effective_role : undefined}
                      onValueChange={(v) => v && onSet(tool.tool_name, v)}
                    >
                      <SelectTrigger className="h-7 text-xs w-28">
                        <SelectValue placeholder="Default" />
                      </SelectTrigger>
                      <SelectContent>
                        {roleNames.map((r) => (
                          <SelectItem key={r} value={r}>
                            {r}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </td>
                  <td className="px-3 py-2">
                    {tool.has_override && (
                      <Button
                        size="xs"
                        variant="ghost"
                        className="text-muted-foreground"
                        onClick={() => onClear(tool.tool_name)}
                      >
                        Reset
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
