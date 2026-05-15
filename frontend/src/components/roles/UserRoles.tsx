import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { KeyRoundIcon, PlusIcon, Trash2Icon, XIcon } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";

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

  const resetPasswordMutation = useMutation({
    mutationFn: ({ userId, password }: { userId: string; password: string }) =>
      api.resetUserPassword(userId, password),
  });

  function toggle(userId: string, currentRoles: string[], role: string) {
    const next = currentRoles.includes(role)
      ? currentRoles.filter((r) => r !== role)
      : [...currentRoles, role];
    mutation.mutate({ userId, roles: next });
  }

  return (
    <div>
      <PageHeader
        eyebrow="SECURITY"
        title="Users"
        description="User accounts and the roles they hold."
        actions={
          data?.allow_user_creation ? (
            <Button size="sm" onClick={() => setShowForm((v) => !v)}>
              {showForm ? <XIcon /> : <PlusIcon />}
              {showForm ? "Cancel" : "Create user"}
            </Button>
          ) : null
        }
      />
      <div className="mx-auto max-w-4xl px-4 py-4 sm:px-6 sm:py-6 space-y-4">
        {isLoading && <LoadingSpinner text="Loading users..." className="p-4" />}

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
          <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b">
                <th className="px-3 py-2 text-left font-medium">User</th>
                <th className="hidden md:table-cell px-3 py-2 text-left font-medium">Username</th>
                <th className="hidden lg:table-cell px-3 py-2 text-left font-medium">Email</th>
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
                  <td className="px-3 py-2 break-words">{u.display_name}</td>
                  <td className="hidden md:table-cell px-3 py-2 text-muted-foreground break-words">{u.username}</td>
                  <td className="hidden lg:table-cell px-3 py-2 text-muted-foreground break-words">{u.email}</td>
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
                  <td className="px-3 py-2 text-center flex gap-1 justify-center">
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 w-7 p-0 text-muted-foreground hover:text-foreground"
                      onClick={() => {
                        const pw = prompt(`New password for "${u.display_name || u.user_id}":`);
                        if (pw) resetPasswordMutation.mutate({ userId: u.user_id, password: pw });
                      }}
                    >
                      <KeyRoundIcon className="h-4 w-4" />
                    </Button>
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
          </div>
        </CardContent>
      </Card>
      </div>
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
