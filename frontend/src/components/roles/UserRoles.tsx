import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { PlusIcon, Trash2Icon, XIcon } from "lucide-react";

export function UserRoles() {
  const queryClient = useQueryClient();
  const api = useWsApi();
  const { connected } = useWebSocket();
  const [showForm, setShowForm] = useState(false);

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

  const deleteMutation = useMutation({
    mutationFn: (userId: string) => api.deleteUser(userId),
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
    <div className="space-y-4">
      {data?.allow_user_creation && (
        <div className="flex justify-end">
          <Button size="sm" onClick={() => setShowForm((v) => !v)}>
            {showForm ? <XIcon className="h-4 w-4 mr-1" /> : <PlusIcon className="h-4 w-4 mr-1" />}
            {showForm ? "Cancel" : "Create User"}
          </Button>
        </div>
      )}

      {showForm && (
        <CreateUserForm
          onCreated={() => {
            setShowForm(false);
            queryClient.invalidateQueries({ queryKey: ["user-roles"] });
          }}
        />
      )}

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
                <th className="px-3 py-2 w-10" />
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
                  <td className="px-3 py-2 text-center">
                    {u.user_id !== "root" && (
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-7 w-7 p-0 text-muted-foreground hover:text-destructive"
                        onClick={() => {
                          if (confirm(`Delete user "${u.display_name || u.user_id}"?`)) {
                            deleteMutation.mutate(u.user_id);
                          }
                        }}
                      >
                        <Trash2Icon className="h-4 w-4" />
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardContent>
      </Card>
    </div>
  );
}

function CreateUserForm({ onCreated }: { onCreated: () => void }) {
  const api = useWsApi();
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");

  const mutation = useMutation({
    mutationFn: () =>
      api.createUser({
        username,
        password,
        email: email || undefined,
        display_name: displayName || undefined,
      }),
    onSuccess: () => onCreated(),
    onError: (err: Error) => setError(err.message),
  });

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (!username.trim() || !password) {
      setError("Username and password are required");
      return;
    }
    mutation.mutate();
  }

  return (
    <Card>
      <CardContent className="p-4">
        <form onSubmit={handleSubmit} className="space-y-3">
          <h3 className="text-sm font-medium">Create New User</h3>

          {error && (
            <p className="text-sm text-destructive">{error}</p>
          )}

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <Input
              placeholder="Username *"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoFocus
            />
            <Input
              placeholder="Display Name"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
            />
            <Input
              type="email"
              placeholder="Email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
            <Input
              type="password"
              placeholder="Password *"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>

          <div className="flex justify-end">
            <Button type="submit" size="sm" disabled={mutation.isPending}>
              {mutation.isPending ? "Creating..." : "Create"}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
