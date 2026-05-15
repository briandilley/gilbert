import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { PageHeader } from "@/components/layout/PageHeader";
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

  return (
    <div>
      <PageHeader
        eyebrow="SECURITY"
        title="RPC"
        description="Which WebSocket RPC operations each role can call. Longest prefix match wins."
      />
      <div className="mx-auto max-w-4xl px-4 py-4 sm:px-6 sm:py-6">
        {isLoading ? (
          <LoadingSpinner text="Loading RPC rules..." className="p-4" />
        ) : (
      <Card>
      <CardContent className="px-0 py-0">
        <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border">
              <th className="px-3 py-2 text-left font-mono text-[11px] uppercase tracking-[0.08em] text-muted-foreground font-medium">Frame type</th>
              <th className="px-3 py-2 text-left font-mono text-[11px] uppercase tracking-[0.08em] text-muted-foreground font-medium">Min role</th>
              <th className="px-3 py-2 w-16"></th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {data?.rules.map((rule) => (
              <tr key={rule.frame_prefix} className="hover:bg-foreground/[0.025] transition-colors">
                <td className="px-3 py-2">
                  <code className="font-mono text-xs">{rule.frame_prefix}</code>
                  {rule.source === "override" && (
                    <Badge variant="outline" className="ml-2">override</Badge>
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
        </div>
      </CardContent>
    </Card>
        )}
      </div>
    </div>
  );
}
