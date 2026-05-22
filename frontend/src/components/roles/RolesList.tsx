import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import { PlusIcon, Trash2Icon } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";

export function RolesList() {
  const queryClient = useQueryClient();
  const api = useWsApi();
  const { connected } = useWebSocket();
  const { data, isLoading } = useQuery({
    queryKey: ["roles"],
    queryFn: api.listRoles,
    enabled: connected,
  });

  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState("");
  const [newLevel, setNewLevel] = useState("100");
  const [newDesc, setNewDesc] = useState("");

  const createMutation = useMutation({
    mutationFn: () => api.createRole(newName, Number(newLevel), newDesc),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["roles"] });
      setShowCreate(false);
      setNewName("");
      setNewLevel("100");
      setNewDesc("");
    },
  });

  const deleteMutation = useMutation({
    mutationFn: api.deleteRole,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["roles"] }),
  });

  return (
    <div>
      <PageHeader
        eyebrow="SECURITY"
        title="Roles"
        description="Named privilege levels that user accounts hold. Each role has a numeric level; checks compare against ``required_role`` declared by the resource."
        actions={
          <Button size="sm" onClick={() => setShowCreate(true)}>
            <PlusIcon />
            New role
          </Button>
        }
      />

      <div className="mx-auto max-w-4xl px-4 py-4 sm:px-6 sm:py-6">
        {isLoading ? (
          <LoadingSpinner text="Loading roles..." className="p-4" />
        ) : (
          <Card>
            <CardContent className="px-0 py-0">
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-border">
                      <th className="px-3 py-2 text-left font-mono text-[11px] uppercase tracking-[0.08em] text-muted-foreground font-medium">
                        Role
                      </th>
                      <th className="px-3 py-2 text-left font-mono text-[11px] uppercase tracking-[0.08em] text-muted-foreground font-medium">
                        Level
                      </th>
                      <th className="hidden sm:table-cell px-3 py-2 text-left font-mono text-[11px] uppercase tracking-[0.08em] text-muted-foreground font-medium">
                        Type
                      </th>
                      <th className="hidden md:table-cell px-3 py-2 text-left font-mono text-[11px] uppercase tracking-[0.08em] text-muted-foreground font-medium">
                        Description
                      </th>
                      <th className="px-3 py-2 w-16"></th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {data?.roles.map((role) => (
                      <tr key={role.name}>
                        <td className="px-3 py-2 font-medium break-words">
                          {role.name}
                        </td>
                        <td className="px-3 py-2 font-mono">{role.level}</td>
                        <td className="hidden sm:table-cell px-3 py-2">
                          <Badge variant={role.builtin ? "neutral" : "outline"}>
                            {role.builtin ? "built-in" : "custom"}
                          </Badge>
                        </td>
                        <td className="hidden md:table-cell px-3 py-2 text-muted-foreground">
                          {role.description}
                        </td>
                        <td className="px-3 py-2 text-right">
                          {!role.builtin && (
                            <Button
                              variant="ghost"
                              size="icon-xs"
                              className="text-destructive"
                              onClick={() => deleteMutation.mutate(role.name)}
                            >
                              <Trash2Icon />
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

    {/* Create Role modal */}
      <Dialog open={showCreate} onOpenChange={setShowCreate}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>Create Role</DialogTitle>
          </DialogHeader>

          <div className="space-y-4">
            <div className="space-y-1.5">
              <Label className="text-xs">Name</Label>
              <Input
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                placeholder="role_name"
              />
            </div>
            <div className="space-y-1.5">
              <Label className="text-xs">Level (1-199)</Label>
              <Input
                type="number"
                value={newLevel}
                onChange={(e) => setNewLevel(e.target.value)}
                min={1}
                max={199}
              />
            </div>
            <div className="space-y-1.5">
              <Label className="text-xs">Description</Label>
              <Input
                value={newDesc}
                onChange={(e) => setNewDesc(e.target.value)}
                placeholder="Optional description"
              />
            </div>
          </div>

          <DialogFooter>
            <Button variant="outline" size="sm" onClick={() => setShowCreate(false)}>
              Cancel
            </Button>
            <Button
              size="sm"
              onClick={() => createMutation.mutate()}
              disabled={!newName.trim()}
            >
              Create
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
