import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

export function CollectionACLs() {
  const queryClient = useQueryClient();
  const api = useWsApi();
  const { connected } = useWebSocket();
  const { data, isLoading } = useQuery({
    queryKey: ["collection-acls"],
    queryFn: api.listCollectionACLs,
    enabled: connected,
  });

  const setMutation = useMutation({
    mutationFn: (args: {
      collection: string;
      readRole: string;
      writeRole: string;
    }) => api.setCollectionACL(args.collection, args.readRole, args.writeRole),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["collection-acls"] }),
  });

  const clearMutation = useMutation({
    mutationFn: api.clearCollectionACL,
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["collection-acls"] }),
  });

  if (isLoading) return <LoadingSpinner text="Loading collections..." className="p-4" />;

  return (
    <Card>
      <CardContent className="p-0">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b">
              <th className="px-3 py-2 text-left font-medium">Collection</th>
              <th className="px-3 py-2 text-left font-medium">Read Role</th>
              <th className="px-3 py-2 text-left font-medium">Write Role</th>
              <th className="px-3 py-2 w-16"></th>
            </tr>
          </thead>
          <tbody>
            {data?.collections.map((col) => (
              <tr key={col.collection} className="border-b">
                <td className="px-3 py-2">{col.collection}</td>
                <td className="px-3 py-2">
                  <Select
                    value={col.read_role}
                    onValueChange={(v) =>
                      v &&
                      setMutation.mutate({
                        collection: col.collection,
                        readRole: v,
                        writeRole: col.write_role,
                      })
                    }
                  >
                    <SelectTrigger className="h-7 text-xs w-28">
                      <SelectValue />
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
                  <Select
                    value={col.write_role}
                    onValueChange={(v) =>
                      v &&
                      setMutation.mutate({
                        collection: col.collection,
                        readRole: col.read_role,
                        writeRole: v,
                      })
                    }
                  >
                    <SelectTrigger className="h-7 text-xs w-28">
                      <SelectValue />
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
                  {col.has_custom && (
                    <Button
                      size="xs"
                      variant="ghost"
                      className="text-muted-foreground"
                      onClick={() => clearMutation.mutate(col.collection)}
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
