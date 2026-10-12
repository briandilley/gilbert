/**
 * AIProfileEditorDialog — create or edit one AI profile.
 *
 * Used by the AI Profiles page and by ``EditProfileLink``, which puts an
 * "Edit profile" link next to every AI profile picker so a profile can be
 * changed in place without leaving the page that uses it.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
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
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export interface ProfileForm {
  name: string;
  description: string;
  tool_mode: string;
  tools: string[];
  tool_roles: Record<string, string>;
  backend: string;
  model: string;
}

function emptyForm(): ProfileForm {
  return {
    name: "",
    description: "",
    tool_mode: "all",
    tools: [],
    tool_roles: {},
    backend: "",
    model: "",
  };
}

interface Props {
  open: boolean;
  /** Profile to edit; ``null`` creates a new one. */
  profileName: string | null;
  onClose: () => void;
  onSaved?: (name: string) => void;
}

export function AIProfileEditorDialog({ open, profileName, onClose, onSaved }: Props) {
  const api = useWsApi();
  const queryClient = useQueryClient();
  const { connected } = useWebSocket();
  const isNew = profileName === null;

  const { data } = useQuery({
    queryKey: ["ai-profiles"],
    queryFn: api.listProfiles,
    enabled: connected && open,
  });
  const { data: modelsData } = useQuery({
    queryKey: ["chat-models"],
    queryFn: api.listModels,
    enabled: connected && open,
  });

  const existing = isNew ? undefined : data?.profiles.find((p) => p.name === profileName);
  const initial: ProfileForm | null = isNew
    ? emptyForm()
    : existing
      ? {
          name: existing.name,
          description: existing.description,
          tool_mode: existing.tool_mode,
          tools: existing.tools,
          tool_roles: existing.tool_roles,
          backend: existing.backend || "",
          model: existing.model || "",
        }
      : null;

  const saveMutation = useMutation({
    mutationFn: api.saveProfile,
    onSuccess: (_r, saved) => {
      queryClient.invalidateQueries({ queryKey: ["ai-profiles"] });
      onSaved?.(saved.name);
      onClose();
    },
  });

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-lg max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{isNew ? "Create profile" : `Edit profile "${profileName}"`}</DialogTitle>
        </DialogHeader>

        {!initial && data && (
          <p className="text-sm text-muted-foreground">No profile named "{profileName}".</p>
        )}
        {open && initial && (
          <EditorBody
            key={profileName ?? "__new__"}
            initial={initial}
            isNew={isNew}
            allToolNames={data?.all_tool_names ?? []}
            backends={modelsData?.backends ?? []}
            saving={saveMutation.isPending}
            error={saveMutation.isError ? (saveMutation.error as Error)?.message || "unknown error" : null}
            onCancel={onClose}
            onSave={(form) => saveMutation.mutate(form)}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}

interface ModelBackend {
  name: string;
  models: Array<{ id: string; name: string }>;
}

function EditorBody({
  initial,
  isNew,
  allToolNames,
  backends,
  saving,
  error,
  onCancel,
  onSave,
}: {
  initial: ProfileForm;
  isNew: boolean;
  allToolNames: string[];
  backends: ModelBackend[];
  saving: boolean;
  error: string | null;
  onCancel: () => void;
  onSave: (form: ProfileForm) => void;
}) {
  const [editing, setEditing] = useState<ProfileForm>(initial);
  const [toolFilter, setToolFilter] = useState("");
  return (
    <>
          <div className="space-y-4">
            <div className="space-y-1.5">
              <Label className="text-xs">Name</Label>
              <Input
                value={editing.name}
                onChange={(e) => setEditing({ ...editing, name: e.target.value })}
                disabled={!isNew}
                placeholder="profile_name"
              />
            </div>

            <div className="space-y-1.5">
              <Label className="text-xs">Description</Label>
              <Textarea
                value={editing.description}
                onChange={(e) => setEditing({ ...editing, description: e.target.value })}
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
                  {backends.map((b) => (
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
                  {backends
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
              <Label className="text-xs">Tool mode</Label>
              <Select
                value={editing.tool_mode}
                onValueChange={(v) => v && setEditing({ ...editing, tool_mode: v })}
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

            {editing.tool_mode !== "all" && allToolNames.length > 0 && (
              <div className="space-y-1.5">
                <Label className="text-xs">Tools to {editing.tool_mode}</Label>
                <Input
                  value={toolFilter}
                  onChange={(e) => setToolFilter(e.target.value)}
                  placeholder="Filter tools..."
                  className="h-7 text-xs"
                />
                <div className="max-h-48 overflow-y-auto border rounded-md p-2 space-y-1">
                  {allToolNames
                    .filter((t) => t.toLowerCase().includes(toolFilter.toLowerCase()))
                    .map((t) => (
                      <label key={t} className="flex items-center gap-2 text-sm cursor-pointer">
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

        {error && <p className="text-sm text-rose-500">Could not save: {error}</p>}
        <DialogFooter>
          <Button variant="outline" onClick={onCancel}>
            Cancel
          </Button>
          <Button
            onClick={() => onSave(editing)}
            disabled={!editing.name.trim() || saving}
          >
            {isNew ? "Create" : "Save"}
          </Button>
        </DialogFooter>
    </>
  );
}
