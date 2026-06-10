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
import { PageHeader } from "@/components/layout/PageHeader";
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
import { PencilIcon, PlusIcon, RotateCcwIcon, Trash2Icon } from "lucide-react";
import type { SubagentTypeDTO } from "@/types/subagent";

interface TypeForm {
  id: string;
  name: string;
  description: string;
  system_prompt: string;
  backend: string;
  model: string;
  temperature: string; // stored as string for input, coerced on save
  max_tokens: string;
  tool_mode: string;
  tools: string[];
  max_rounds: string;
  max_wall_clock_s: string;
  execution_mode: string;
  deliver_as: string;
  enabled: boolean;
  built_in: boolean;
  icon: string;
}

function dtoToForm(t: SubagentTypeDTO): TypeForm {
  return {
    ...t,
    temperature: t.temperature != null ? String(t.temperature) : "",
    max_tokens: t.max_tokens != null ? String(t.max_tokens) : "",
    max_rounds: String(t.max_rounds),
    max_wall_clock_s: t.max_wall_clock_s != null ? String(t.max_wall_clock_s) : "",
  };
}

function formToDto(f: TypeForm): SubagentTypeDTO {
  return {
    id: f.id,
    name: f.name,
    description: f.description,
    system_prompt: f.system_prompt,
    backend: f.backend,
    model: f.model,
    temperature: f.temperature.trim() !== "" ? parseFloat(f.temperature) : null,
    max_tokens: f.max_tokens.trim() !== "" ? parseInt(f.max_tokens, 10) : null,
    tool_mode: f.tool_mode,
    tools: f.tools,
    max_rounds: parseInt(f.max_rounds, 10) || 12,
    max_wall_clock_s: f.max_wall_clock_s.trim() !== "" ? parseFloat(f.max_wall_clock_s) : null,
    execution_mode: f.execution_mode,
    deliver_as: f.deliver_as,
    enabled: f.enabled,
    built_in: f.built_in,
    icon: f.icon,
  };
}

function emptyForm(): TypeForm {
  return {
    id: "",
    name: "",
    description: "",
    system_prompt: "",
    backend: "",
    model: "",
    temperature: "",
    max_tokens: "",
    tool_mode: "all",
    tools: [],
    max_rounds: "12",
    max_wall_clock_s: "300",
    execution_mode: "sync",
    deliver_as: "inline",
    enabled: true,
    built_in: false,
    icon: "",
  };
}

export function Subagents() {
  const queryClient = useQueryClient();
  const api = useWsApi();
  const { connected } = useWebSocket();

  const { data, isLoading } = useQuery({
    queryKey: ["subagent-types"],
    queryFn: api.listSubagentTypes,
    enabled: connected,
  });

  const { data: modelsData } = useQuery({
    queryKey: ["chat-models"],
    queryFn: api.listModels,
    enabled: connected,
  });

  const [editing, setEditing] = useState<TypeForm | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [toolFilter, setToolFilter] = useState("");

  const saveMutation = useMutation({
    mutationFn: (f: TypeForm) => api.saveSubagentType(formToDto(f)),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["subagent-types"] });
      setEditing(null);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: api.deleteSubagentType,
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["subagent-types"] }),
  });

  const resetMutation = useMutation({
    mutationFn: api.resetSubagentType,
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["subagent-types"] }),
  });

  function openNew() {
    setIsNew(true);
    setToolFilter("");
    setEditing(emptyForm());
  }

  function openEdit(t: SubagentTypeDTO) {
    setIsNew(false);
    setToolFilter("");
    setEditing(dtoToForm(t));
  }

  return (
    <div>
      <PageHeader
        eyebrow="SECURITY"
        title="Subagent types"
        description="Self-contained agent definitions: model, tools, budget, prompt, and execution mode. Built-in types can be edited and reset; custom types can be deleted."
        actions={
          <Button size="sm" onClick={openNew}>
            <PlusIcon />
            New type
          </Button>
        }
      />
      <div className="mx-auto max-w-4xl px-4 py-4 sm:px-6 sm:py-6">
        {isLoading && <LoadingSpinner text="Loading types..." className="p-4" />}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {data?.types.map((t) => (
            <Card key={t.id}>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm flex items-center gap-2">
                  <span className="flex-1">{t.name}</span>
                  <Badge variant="secondary" className="text-xs">
                    {t.execution_mode}
                  </Badge>
                  <Badge variant="secondary" className="text-xs">
                    {t.tool_mode}
                  </Badge>
                  {(t.backend || t.model) && (
                    <Badge variant="outline" className="text-xs">
                      {t.model || t.backend || "default"}
                    </Badge>
                  )}
                  {!t.enabled && (
                    <Badge variant="outline" className="text-xs text-muted-foreground">
                      disabled
                    </Badge>
                  )}
                  <Button
                    variant="ghost"
                    size="icon-xs"
                    onClick={() => openEdit(t)}
                  >
                    <PencilIcon className="size-3" />
                  </Button>
                  {t.built_in ? (
                    <Button
                      variant="ghost"
                      size="icon-xs"
                      title="Reset to defaults"
                      onClick={() => resetMutation.mutate(t.id)}
                    >
                      <RotateCcwIcon className="size-3" />
                    </Button>
                  ) : (
                    <Button
                      variant="ghost"
                      size="icon-xs"
                      className="text-destructive"
                      onClick={() => deleteMutation.mutate(t.id)}
                    >
                      <Trash2Icon className="size-3" />
                    </Button>
                  )}
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-2 text-sm">
                {t.description && (
                  <p className="text-muted-foreground">{t.description}</p>
                )}
                {t.tools.length > 0 && (
                  <div className="flex flex-wrap gap-1">
                    {t.tools.map((tool) => (
                      <Badge key={tool} variant="outline" className="text-[10px]">
                        {tool}
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
          <DialogContent className="max-w-2xl max-h-[90vh] overflow-y-auto">
            <DialogHeader>
              <DialogTitle>
                {isNew ? "Create subagent type" : `Edit "${editing?.name}"`}
              </DialogTitle>
            </DialogHeader>

            {editing && (
              <div className="space-y-4">
                <div className="space-y-1.5">
                  <Label className="text-xs">ID</Label>
                  <Input
                    value={editing.id}
                    onChange={(e) =>
                      setEditing({ ...editing, id: e.target.value })
                    }
                    disabled={!isNew}
                    placeholder="my-agent-type"
                  />
                </div>

                <div className="space-y-1.5">
                  <Label className="text-xs">Name</Label>
                  <Input
                    value={editing.name}
                    onChange={(e) =>
                      setEditing({ ...editing, name: e.target.value })
                    }
                    placeholder="My Agent Type"
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
                    placeholder="When to use this agent type..."
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

                <div className="grid grid-cols-2 gap-4">
                  <div className="space-y-1.5">
                    <Label className="text-xs">Temperature</Label>
                    <Input
                      type="number"
                      step="0.1"
                      min="0"
                      max="2"
                      value={editing.temperature}
                      onChange={(e) =>
                        setEditing({ ...editing, temperature: e.target.value })
                      }
                      placeholder="(default)"
                    />
                  </div>
                  <div className="space-y-1.5">
                    <Label className="text-xs">Max tokens</Label>
                    <Input
                      type="number"
                      step="1"
                      min="1"
                      value={editing.max_tokens}
                      onChange={(e) =>
                        setEditing({ ...editing, max_tokens: e.target.value })
                      }
                      placeholder="(default)"
                    />
                  </div>
                </div>

                <div className="space-y-1.5">
                  <Label className="text-xs">Tool mode</Label>
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
                        .filter((tool) =>
                          tool.toLowerCase().includes(toolFilter.toLowerCase()),
                        )
                        .map((tool) => (
                          <label
                            key={tool}
                            className="flex items-center gap-2 text-sm cursor-pointer"
                          >
                            <input
                              type="checkbox"
                              checked={editing.tools.includes(tool)}
                              onChange={(e) => {
                                const next = e.target.checked
                                  ? [...editing.tools, tool]
                                  : editing.tools.filter((x) => x !== tool);
                                setEditing({ ...editing, tools: next });
                              }}
                              className="accent-primary"
                            />
                            {tool}
                          </label>
                        ))}
                    </div>
                  </div>
                )}

                <div className="grid grid-cols-2 gap-4">
                  <div className="space-y-1.5">
                    <Label className="text-xs">Max rounds</Label>
                    <Input
                      type="number"
                      step="1"
                      min="1"
                      value={editing.max_rounds}
                      onChange={(e) =>
                        setEditing({ ...editing, max_rounds: e.target.value })
                      }
                    />
                  </div>
                  <div className="space-y-1.5">
                    <Label className="text-xs">Max wall clock (seconds)</Label>
                    <Input
                      type="number"
                      step="1"
                      min="0"
                      value={editing.max_wall_clock_s}
                      onChange={(e) =>
                        setEditing({ ...editing, max_wall_clock_s: e.target.value })
                      }
                      placeholder="(none)"
                    />
                  </div>
                </div>

                <div className="space-y-1.5">
                  <Label className="text-xs">Execution mode</Label>
                  <Select
                    value={editing.execution_mode}
                    onValueChange={(v) =>
                      v && setEditing({ ...editing, execution_mode: v })
                    }
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="sync">Sync (return result inline)</SelectItem>
                      <SelectItem value="background">Background (detach, notify when done)</SelectItem>
                    </SelectContent>
                  </Select>
                </div>

                <div className="space-y-1.5">
                  <Label className="text-xs">Deliver as</Label>
                  <Select
                    value={editing.deliver_as}
                    onValueChange={(v) =>
                      v && setEditing({ ...editing, deliver_as: v })
                    }
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="inline">Inline (in the chat message)</SelectItem>
                      <SelectItem value="report_file">Report file (attach as Markdown)</SelectItem>
                    </SelectContent>
                  </Select>
                </div>

                <div className="flex items-center gap-2">
                  <input
                    id="type-enabled"
                    type="checkbox"
                    checked={editing.enabled}
                    onChange={(e) =>
                      setEditing({ ...editing, enabled: e.target.checked })
                    }
                    className="accent-primary"
                  />
                  <Label htmlFor="type-enabled" className="text-xs cursor-pointer">
                    Enabled (appears in spawn_agent type list)
                  </Label>
                </div>

                <div className="space-y-1.5">
                  <Label className="text-xs">System prompt</Label>
                  <Textarea
                    value={editing.system_prompt}
                    onChange={(e) =>
                      setEditing({ ...editing, system_prompt: e.target.value })
                    }
                    rows={10}
                    className="font-mono text-xs"
                    placeholder="The agent's system prompt..."
                  />
                </div>
              </div>
            )}

            {saveMutation.isError && (
              <p className="text-sm text-rose-400">
                Couldn't save:{" "}
                {(saveMutation.error as Error)?.message || "unknown error"}
              </p>
            )}
            <DialogFooter>
              <Button variant="outline" onClick={() => setEditing(null)}>
                Cancel
              </Button>
              <Button
                onClick={() => editing && saveMutation.mutate(editing)}
                disabled={!editing?.name.trim() || !editing?.id.trim()}
              >
                {isNew ? "Create" : "Save"}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </div>
    </div>
  );
}
