import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

export function RpcPermissions() {
  const queryClient = useQueryClient();
  const api = useWsApi();
  const { connected } = useWebSocket();

  const { data, isLoading } = useQuery({
    queryKey: ["rpc-permissions"],
    queryFn: api.listRpcPermissions,
    enabled: connected,
  });

  const setMutation = useMutation({
    mutationFn: (args: { prefix: string; role: string }) =>
      api.setRpcPermission(args.prefix, args.role),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["rpc-permissions"] }),
  });

  const clearMutation = useMutation({
    mutationFn: api.clearRpcPermission,
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["rpc-permissions"] }),
  });

  if (isLoading) return <LoadingSpinner text="Loading RPC rules..." className="p-4" />;

  return (
    <Card>
      <CardContent className="p-0">
        <p className="px-3 py-2 text-xs text-muted-foreground border-b">
          Controls which WebSocket RPC operations each role can call. Longest prefix match wins.
        </p>
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b">
              <th className="px-3 py-2 text-left font-medium">Frame Type</th>
              <th className="px-3 py-2 text-left font-medium">Min Role</th>
              <th className="px-3 py-2 w-16"></th>
            </tr>
          </thead>
          <tbody>
            {data?.rules.map((rule) => (
              <tr key={rule.frame_prefix} className="border-b">
                <td className="px-3 py-2">
                  <code className="text-xs">{rule.frame_prefix}</code>
                  {rule.source === "override" && (
                    <Badge variant="outline" className="text-[10px] ml-2">override</Badge>
                  )}
                </td>
                <td className="px-3 py-2">
                  <Select
                    value={rule.min_role}
                    onValueChange={(v) =>
                      v && setMutation.mutate({ prefix: rule.frame_prefix, role: v })
                    }
                  >
                    <SelectTrigger className="h-7 text-xs w-28">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {data.role_names.map((r) => (
                        <SelectItem key={r} value={r}>{r}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </td>
                <td className="px-3 py-2">
                  {rule.source === "override" && (
                    <Button
                      size="xs"
                      variant="ghost"
                      className="text-muted-foreground"
                      onClick={() => clearMutation.mutate(rule.frame_prefix)}
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
