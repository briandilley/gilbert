import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import { PencilIcon, PlusIcon, Trash2Icon } from "lucide-react";

interface ProfileForm {
  name: string;
  description: string;
  tool_mode: string;
  tools: string[];
  tool_roles: Record<string, string>;
  backend: string;
  model: string;
}

export function AIProfiles() {
  const queryClient = useQueryClient();
  const api = useWsApi();
  const { connected } = useWebSocket();
  const { data, isLoading } = useQuery({
    queryKey: ["ai-profiles"],
    queryFn: api.listProfiles,
    enabled: connected,
  });

  const { data: modelsData } = useQuery({
    queryKey: ["chat-models"],
    queryFn: api.listModels,
    enabled: connected,
  });

  const [editing, setEditing] = useState<ProfileForm | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [toolFilter, setToolFilter] = useState("");

  const saveMutation = useMutation({
    mutationFn: api.saveProfile,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["ai-profiles"] });
      setEditing(null);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: api.deleteProfile,
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["ai-profiles"] }),
  });

  function openNew() {
    setIsNew(true);
    setToolFilter("");
    setEditing({
      name: "",
      description: "",
      tool_mode: "all",
      tools: [],
      tool_roles: {},
      backend: "",
      model: "",
    });
  }

  function openEdit(profile: {
    name: string;
    description: string;
    tool_mode: string;
    tools: string[];
    tool_roles: Record<string, string>;
    backend?: string;
    model?: string;
  }) {
    setIsNew(false);
    setToolFilter("");
    setEditing({
      ...profile,
      backend: profile.backend || "",
      model: profile.model || "",
    });
  }

  if (isLoading) return <LoadingSpinner text="Loading profiles..." className="p-4" />;

  return (
    <>
      <h1 className="text-xl sm:text-2xl font-semibold text-center mb-4">AI Profiles</h1>
      <div className="flex justify-end mb-4">
        <Button size="sm" onClick={openNew}>
          <PlusIcon className="size-4 mr-1" />
          New Profile
        </Button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {data?.profiles.map((profile) => (
          <Card key={profile.name}>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm flex items-center gap-2">
                <span className="flex-1">{profile.name}</span>
                <Badge variant="secondary" className="text-xs">
                  {profile.tool_mode}
                </Badge>
                {(profile.backend || profile.model) && (
                  <Badge variant="outline" className="text-xs">
                    {profile.model || profile.backend || "default"}
                  </Badge>
                )}
                <Button
                  variant="ghost"
                  size="icon-xs"
                  onClick={() => openEdit(profile)}
                >
                  <PencilIcon className="size-3" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon-xs"
                  className="text-destructive"
                  onClick={() => deleteMutation.mutate(profile.name)}
                >
                  <Trash2Icon className="size-3" />
                </Button>
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              {profile.description && (
                <p className="text-muted-foreground">{profile.description}</p>
              )}
              {profile.tools.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {profile.tools.map((t) => (
                    <Badge key={t} variant="outline" className="text-[10px]">
                      {t}
                    </Badge>
                  ))}
                </div>
              )}
              {profile.assigned_calls.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {profile.assigned_calls.map((c) => (
                    <Badge key={c} variant="secondary" className="text-[10px]">
                      {c}
                    </Badge>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        ))}
      </div>

      {/* Edit / Create modal */}
      <Dialog
        open={editing !== null}
        onOpenChange={(open) => !open && setEditing(null)}
      >
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>
              {isNew ? "Create Profile" : `Edit "${editing?.name}"`}
            </DialogTitle>
          </DialogHeader>

          {editing && (
            <div className="space-y-4">
              <div className="space-y-1.5">
                <Label className="text-xs">Name</Label>
                <Input
                  value={editing.name}
                  onChange={(e) =>
                    setEditing({ ...editing, name: e.target.value })
                  }
                  disabled={!isNew}
                  placeholder="profile_name"
                />
              </div>

              <div className="space-y-1.5">
                <Label className="text-xs">Description</Label>
                <Textarea
                  value={editing.description}
                  onChange={(e) =>
                    setEditing({ ...editing, description: e.target.value })
                  }
                  rows={2}
                  placeholder="What this profile is for..."
                />
              </div>

              <div className="space-y-1.5">
                <Label className="text-xs">Backend</Label>
                <Select
                  value={editing.backend || "__default__"}
                  onValueChange={(v) => {
                    if (!v) return;
                    const newBackend = v === "__default__" ? "" : v;
                    setEditing({ ...editing, backend: newBackend, model: "" });
                  }}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__default__">Default (first available)</SelectItem>
                    {(modelsData?.backends ?? []).map((b) => (
                      <SelectItem key={b.name} value={b.name}>
                        {b.name.charAt(0).toUpperCase() + b.name.slice(1)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div className="space-y-1.5">
                <Label className="text-xs">Model</Label>
                <Select
                  value={editing.model || "__default__"}
                  onValueChange={(v) => {
                    if (!v) return;
                    setEditing({ ...editing, model: v === "__default__" ? "" : v });
                  }}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__default__">Default (backend decides)</SelectItem>
                    {(modelsData?.backends ?? [])
                      .filter((b) => !editing.backend || b.name === editing.backend)
                      .flatMap((b) =>
                        b.models.map((m) => (
                          <SelectItem key={m.id} value={m.id}>
                            {editing.backend ? m.name : `${b.name}: ${m.name}`}
                          </SelectItem>
                        )),
                      )}
                  </SelectContent>
                </Select>
              </div>

              <div className="space-y-1.5">
                <Label className="text-xs">Tool Mode</Label>
                <Select
                  value={editing.tool_mode}
                  onValueChange={(v) =>
                    v && setEditing({ ...editing, tool_mode: v })
                  }
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="all">All tools</SelectItem>
                    <SelectItem value="include">Include list</SelectItem>
                    <SelectItem value="exclude">Exclude list</SelectItem>
                  </SelectContent>
                </Select>
              </div>

              {editing.tool_mode !== "all" && data?.all_tool_names && (
                <div className="space-y-1.5">
                  <Label className="text-xs">
                    Tools to {editing.tool_mode}
                  </Label>
                  <Input
                    value={toolFilter}
                    onChange={(e) => setToolFilter(e.target.value)}
                    placeholder="Filter tools..."
                    className="h-7 text-xs"
                  />
                  <div className="max-h-48 overflow-y-auto border rounded-md p-2 space-y-1">
                    {data.all_tool_names
                      .filter((t) =>
                        t.toLowerCase().includes(toolFilter.toLowerCase()),
                      )
                      .map((t) => (
                        <label
                          key={t}
                          className="flex items-center gap-2 text-sm cursor-pointer"
                        >
                          <input
                            type="checkbox"
                            checked={editing.tools.includes(t)}
                            onChange={(e) => {
                              const next = e.target.checked
                                ? [...editing.tools, t]
                                : editing.tools.filter((x) => x !== t);
                              setEditing({ ...editing, tools: next });
                            }}
                            className="accent-primary"
                          />
                          {t}
                        </label>
                      ))}
                  </div>
                </div>
              )}
            </div>
          )}

          <DialogFooter>
            <Button variant="outline" onClick={() => setEditing(null)}>
              Cancel
            </Button>
            <Button
              onClick={() => editing && saveMutation.mutate(editing)}
              disabled={!editing?.name.trim()}
            >
              {isNew ? "Create" : "Save"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
