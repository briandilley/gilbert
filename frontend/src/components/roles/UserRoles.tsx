import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { Card, CardContent } from "@/components/ui/card";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";

export function UserRoles() {
  const queryClient = useQueryClient();
  const api = useWsApi();
  const { connected } = useWebSocket();
  const { data, isLoading } = useQuery({
    queryKey: ["user-roles"],
    queryFn: api.listUserRoles,
    enabled: connected,
  });

  const mutation = useMutation({
    mutationFn: (args: { userId: string; roles: string[] }) =>
      api.setUserRoles(args.userId, args.roles),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["user-roles"] }),
  });

  function toggle(userId: string, currentRoles: string[], role: string) {
    const next = currentRoles.includes(role)
      ? currentRoles.filter((r) => r !== role)
      : [...currentRoles, role];
    mutation.mutate({ userId, roles: next });
  }

  if (isLoading) return <LoadingSpinner text="Loading users..." className="p-4" />;

  return (
    <Card>
      <CardContent className="p-0">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b">
              <th className="px-3 py-2 text-left font-medium">User</th>
              <th className="px-3 py-2 text-left font-medium">Email</th>
              {data?.role_names.map((r) => (
                <th key={r} className="px-3 py-2 text-center font-medium">
                  {r}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data?.users.map((u) => (
              <tr key={u.user_id} className="border-b">
                <td className="px-3 py-2">{u.display_name}</td>
                <td className="px-3 py-2 text-muted-foreground">{u.email}</td>
                {data.role_names.map((r) => (
                  <td key={r} className="px-3 py-2 text-center">
                    <input
                      type="checkbox"
                      checked={u.roles.includes(r)}
                      onChange={() => toggle(u.user_id, u.roles, r)}
                      className="accent-primary"
                    />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}
