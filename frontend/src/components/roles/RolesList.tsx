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

  if (isLoading) return <LoadingSpinner text="Loading roles..." className="p-4" />;

  return (
    <>
      <div className="flex justify-end mb-4">
        <Button size="sm" onClick={() => setShowCreate(true)}>
          <PlusIcon className="size-4 mr-1" />
          New Role
        </Button>
      </div>

      <Card>
        <CardContent className="p-0">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b">
                <th className="px-3 py-2 text-left font-medium">Role</th>
                <th className="px-3 py-2 text-left font-medium">Level</th>
                <th className="px-3 py-2 text-left font-medium">Type</th>
                <th className="px-3 py-2 text-left font-medium">Description</th>
                <th className="px-3 py-2 w-16"></th>
              </tr>
            </thead>
            <tbody>
              {data?.roles.map((role) => (
                <tr key={role.name} className="border-b">
                  <td className="px-3 py-2 font-medium">{role.name}</td>
                  <td className="px-3 py-2">{role.level}</td>
                  <td className="px-3 py-2">
                    <Badge
                      variant={role.builtin ? "secondary" : "outline"}
                      className="text-xs"
                    >
                      {role.builtin ? "Built-in" : "Custom"}
                    </Badge>
                  </td>
                  <td className="px-3 py-2 text-muted-foreground">
                    {role.description}
                  </td>
                  <td className="px-3 py-2">
                    {!role.builtin && (
                      <Button
                        variant="ghost"
                        size="icon-xs"
                        className="text-destructive"
                        onClick={() => deleteMutation.mutate(role.name)}
                      >
                        <Trash2Icon className="size-3" />
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardContent>
      </Card>

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
            <Button variant="outline" onClick={() => setShowCreate(false)}>
              Cancel
            </Button>
            <Button
              onClick={() => createMutation.mutate()}
              disabled={!newName.trim()}
            >
              Create
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
