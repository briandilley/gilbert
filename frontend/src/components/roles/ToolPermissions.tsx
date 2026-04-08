import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { Card, CardContent } from "@/components/ui/card";
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

  if (isLoading) return <LoadingSpinner text="Loading tools..." className="p-4" />;

  return (
    <Card>
      <CardContent className="p-0">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b">
              <th className="px-3 py-2 text-left font-medium">Provider</th>
              <th className="px-3 py-2 text-left font-medium">Tool</th>
              <th className="px-3 py-2 text-left font-medium">Default</th>
              <th className="px-3 py-2 text-left font-medium">Override</th>
              <th className="px-3 py-2 w-16"></th>
            </tr>
          </thead>
          <tbody>
            {data?.tools.map((tool) => (
              <tr key={tool.tool_name} className="border-b">
                <td className="px-3 py-2 text-muted-foreground">
                  {tool.provider}
                </td>
                <td className="px-3 py-2">{tool.tool_name}</td>
                <td className="px-3 py-2">
                  <Badge variant="secondary" className="text-xs">
                    {tool.default_role}
                  </Badge>
                </td>
                <td className="px-3 py-2">
                  <Select
                    value={tool.has_override ? tool.effective_role : undefined}
                    onValueChange={(v) =>
                      v &&
                      setMutation.mutate({ toolName: tool.tool_name, role: v })
                    }
                  >
                    <SelectTrigger className="h-7 text-xs w-28">
                      <SelectValue placeholder="Default" />
                    </SelectTrigger>
                    <SelectContent>
                      {data.role_names.map((r) => (
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
                      onClick={() => clearMutation.mutate(tool.tool_name)}
                    >
                      Reset
                    </Button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}
