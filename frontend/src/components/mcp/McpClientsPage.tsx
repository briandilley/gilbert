import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  PlusIcon,
  Trash2Icon,
  KeyRoundIcon,
  PowerIcon,
  PowerOffIcon,
  CopyIcon,
  CheckIcon,
  RefreshCcwIcon,
  AlertTriangleIcon,
} from "lucide-react";
import type { McpServerClient, McpServerClientDraft } from "@/types/mcp";

/**
 * Admin-only page for managing MCP client registrations — the bearer
 * tokens external MCP-aware agents (Claude Desktop, Cursor, etc.)
 * use to authenticate to Gilbert's ``/api/mcp`` HTTP endpoint.
 *
 * Non-admins who reach this URL directly will see empty lists +
 * forbidden-error toasts from the RPC handlers; the nav card is
 * already filtered out for them at the dashboard layer.
 */
export function McpClientsPage() {
  const api = useWsApi();
  const queryClient = useQueryClient();
  const { connected } = useWebSocket();

  const { data: clients, isLoading, refetch } = useQuery({
    queryKey: ["mcp-clients"],
    queryFn: api.listMcpClients,
    enabled: connected,
    refetchInterval: 30_000,
  });

  const [createOpen, setCreateOpen] = useState(false);
  const [revealState, setRevealState] = useState<{
    client: McpServerClient;
    token: string;
  } | null>(null);

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["mcp-clients"] });

  const createMutation = useMutation({
    mutationFn: api.createMcpClient,
    onSuccess: (result) => {
      setCreateOpen(false);
      setRevealState({ client: result.client, token: result.token });
      invalidate();
    },
  });

  const rotateMutation = useMutation({
    mutationFn: api.rotateMcpClientToken,
    onSuccess: (result) => {
      setRevealState({ client: result.client, token: result.token });
      invalidate();
    },
  });

  const toggleMutation = useMutation({
    mutationFn: ({ id, active }: { id: string; active: boolean }) =>
      api.updateMcpClient(id, { active }),
    onSuccess: invalidate,
  });

  const deleteMutation = useMutation({
    mutationFn: api.deleteMcpClient,
    onSuccess: invalidate,
  });

  const handleDelete = (client: McpServerClient) => {
    if (
      !confirm(
        `Delete MCP client "${client.name}"? This revokes its token ` +
          `and disconnects any active sessions.`,
      )
    ) {
      return;
    }
    deleteMutation.mutate(client.id);
  };

  const handleRotate = (client: McpServerClient) => {
    if (
      !confirm(
        `Rotate "${client.name}"'s token? The old token stops working ` +
          `immediately. The new token will be shown once.`,
      )
    ) {
      return;
    }
    rotateMutation.mutate(client.id);
  };

  if (isLoading) {
    return <LoadingSpinner text="Loading MCP clients..." className="p-8" />;
  }

  const rows = clients ?? [];

  return (
    <div className="p-4 sm:p-6 max-w-5xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-semibold">MCP Clients</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Bearer tokens for external MCP-aware agents (Claude Desktop,
            Cursor, etc.) that connect <em>to</em> Gilbert's{" "}
            <code>/api/mcp</code> endpoint. Each client acts as the
            owner user's identity and sees tools filtered by the
            selected AI profile.
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={() => refetch()}>
            <RefreshCcwIcon className="size-4 mr-1" />
            Refresh
          </Button>
          <Button size="sm" onClick={() => setCreateOpen(true)}>
            <PlusIcon className="size-4 mr-1" />
            Add Client
          </Button>
        </div>
      </div>

      {rows.length === 0 ? (
        <Card>
          <CardContent className="py-12 text-center text-muted-foreground">
            <p>No MCP clients registered.</p>
            <p className="text-sm mt-2">
              Click <strong>Add Client</strong> to issue the first
              bearer token.
            </p>
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-2">
          {rows.map((client) => (
            <ClientRow
              key={client.id}
              client={client}
              onDelete={handleDelete}
              onRotate={handleRotate}
              onToggle={(c) =>
                toggleMutation.mutate({ id: c.id, active: !c.active })
              }
            />
          ))}
        </div>
      )}

      <CreateClientDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onSubmit={(draft) => createMutation.mutateAsync(draft)}
      />

      {revealState && (
        <TokenRevealDialog
          client={revealState.client}
          token={revealState.token}
          onClose={() => setRevealState(null)}
        />
      )}
    </div>
  );
}

// ── Row ───────────────────────────────────────────────────────────────


interface ClientRowProps {
  client: McpServerClient;
  onDelete: (client: McpServerClient) => void;
  onRotate: (client: McpServerClient) => void;
  onToggle: (client: McpServerClient) => void;
}

function ClientRow({ client, onDelete, onRotate, onToggle }: ClientRowProps) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="font-medium">{client.name}</span>
              <Badge variant={client.active ? "default" : "outline"}>
                {client.active ? "active" : "inactive"}
              </Badge>
              <Badge variant="secondary" className="font-mono text-xs">
                {client.token_prefix}…
              </Badge>
            </div>
            {client.description && (
              <div className="mt-1 text-sm text-muted-foreground">
                {client.description}
              </div>
            )}
            <div className="mt-2 grid grid-cols-2 sm:grid-cols-4 gap-x-4 gap-y-1 text-xs text-muted-foreground">
              <div>
                <span className="font-medium">Owner:</span>{" "}
                <code>{client.owner_user_id}</code>
              </div>
              <div>
                <span className="font-medium">Profile:</span>{" "}
                <code>{client.ai_profile}</code>
              </div>
              <div>
                <span className="font-medium">Last used:</span>{" "}
                {formatTimestamp(client.last_used_at) || "never"}
              </div>
              <div>
                <span className="font-medium">Last IP:</span>{" "}
                {client.last_ip || "—"}
              </div>
            </div>
          </div>
          <div className="flex gap-1 shrink-0">
            <Button
              variant="ghost"
              size="icon-sm"
              title={client.active ? "Deactivate" : "Activate"}
              onClick={() => onToggle(client)}
            >
              {client.active ? (
                <PowerOffIcon className="size-4" />
              ) : (
                <PowerIcon className="size-4" />
              )}
            </Button>
            <Button
              variant="ghost"
              size="icon-sm"
              title="Rotate token"
              onClick={() => onRotate(client)}
            >
              <KeyRoundIcon className="size-4" />
            </Button>
            <Button
              variant="ghost"
              size="icon-sm"
              title="Delete"
              onClick={() => onDelete(client)}
            >
              <Trash2Icon className="size-4" />
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

// ── Create dialog ──────────────────────────────────────────────────────


interface CreateClientDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSubmit: (draft: McpServerClientDraft) => Promise<unknown>;
}

function CreateClientDialog({
  open,
  onOpenChange,
  onSubmit,
}: CreateClientDialogProps) {
  const api = useWsApi();

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [ownerUserId, setOwnerUserId] = useState("");
  const [aiProfile, setAiProfile] = useState("standard");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { data: users } = useQuery({
    queryKey: ["chat-users"],
    queryFn: api.listChatUsers,
    enabled: open,
  });

  const { data: profilesData } = useQuery({
    queryKey: ["ai-profiles"],
    queryFn: api.listProfiles,
    enabled: open,
  });

  const profiles = profilesData?.profiles ?? [];
  const selectedProfile = profiles.find((p) => p.name === aiProfile);

  useEffect(() => {
    if (open) {
      setName("");
      setDescription("");
      setOwnerUserId("");
      setAiProfile("standard");
      setError(null);
    }
  }, [open]);

  const { data: preview, isFetching: previewLoading } = useQuery({
    queryKey: ["mcp-client-preview", ownerUserId, aiProfile],
    queryFn: () => api.previewMcpClientTools(ownerUserId, aiProfile),
    enabled: open && !!ownerUserId && !!aiProfile,
  });

  const submit = async () => {
    if (!name.trim()) {
      setError("Name is required");
      return;
    }
    if (!ownerUserId.trim()) {
      setError("Owner is required");
      return;
    }
    if (!aiProfile.trim()) {
      setError("AI profile is required");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await onSubmit({
        name: name.trim(),
        description: description.trim(),
        owner_user_id: ownerUserId,
        ai_profile: aiProfile,
      });
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Register MCP client</DialogTitle>
        </DialogHeader>

        <div className="space-y-4">
          <div>
            <Label htmlFor="mcp-client-name">Name</Label>
            <Input
              id="mcp-client-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Claude Desktop (work laptop)"
            />
          </div>

          <div>
            <Label htmlFor="mcp-client-description">
              Description (optional)
            </Label>
            <Textarea
              id="mcp-client-description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="What will use this token?"
              rows={2}
            />
          </div>

          <div>
            <Label htmlFor="mcp-client-owner">Owner user</Label>
            <Select
              value={ownerUserId}
              onValueChange={(v) => {
                if (v) setOwnerUserId(v);
              }}
            >
              <SelectTrigger id="mcp-client-owner">
                <SelectValue placeholder="Pick the identity this client acts as" />
              </SelectTrigger>
              <SelectContent>
                {(users ?? []).map((u) => (
                  <SelectItem key={u.user_id} value={u.user_id}>
                    {u.display_name || u.user_id}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground mt-1">
              Every tool call the client makes runs under this user's
              identity and RBAC. Choose carefully — admin-owned clients
              can reach admin-level tools.
            </p>
          </div>

          <div>
            <Label htmlFor="mcp-client-profile">AI profile</Label>
            <Select
              value={aiProfile}
              onValueChange={(v) => {
                if (v) setAiProfile(v);
              }}
            >
              <SelectTrigger id="mcp-client-profile">
                <SelectValue placeholder="Pick an AI profile" />
              </SelectTrigger>
              <SelectContent>
                {profiles.map((p) => (
                  <SelectItem key={p.name} value={p.name}>
                    <div className="flex items-center gap-2">
                      <code className="text-xs">{p.name}</code>
                      <ProfileModeBadge mode={p.tool_mode} />
                    </div>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {selectedProfile && (
              <p className="text-xs text-muted-foreground mt-1">
                {selectedProfile.description ||
                  "No description for this profile."}
              </p>
            )}
            {selectedProfile?.tool_mode === "all" && (
              <div className="mt-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-xs flex gap-2">
                <AlertTriangleIcon className="size-4 shrink-0 text-amber-600" />
                <span>
                  This profile exposes <strong>every</strong> tool the
                  owner can see. For untrusted MCP clients, create a
                  narrower profile in Roles → AI Profiles first.
                </span>
              </div>
            )}
          </div>

          {ownerUserId && aiProfile && (
            <div className="rounded-md border bg-muted/30 p-3">
              <div className="flex items-center justify-between gap-2">
                <div className="text-sm font-medium">
                  This client will see
                </div>
                <Badge variant="secondary">
                  {previewLoading && !preview
                    ? "…"
                    : `${preview?.tool_count ?? 0} tool${
                        (preview?.tool_count ?? 0) === 1 ? "" : "s"
                      }`}
                </Badge>
              </div>
              {preview && preview.tools.length > 0 && (
                <div className="mt-2 max-h-32 overflow-y-auto text-xs font-mono text-muted-foreground space-y-0.5">
                  {preview.tools.map((t) => (
                    <div key={t.name} className="truncate" title={t.description}>
                      {t.name}
                    </div>
                  ))}
                </div>
              )}
              {preview && preview.tools.length === 0 && !previewLoading && (
                <p className="mt-2 text-xs text-muted-foreground">
                  No tools — this client can authenticate but can't
                  call anything. Add tools to the profile in Roles →
                  AI Profiles.
                </p>
              )}
            </div>
          )}

          {error && (
            <div className="rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
              {error}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={saving}
          >
            Cancel
          </Button>
          <Button onClick={submit} disabled={saving}>
            {saving ? "Creating..." : "Create & reveal token"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ProfileModeBadge({
  mode,
}: {
  mode: "all" | "include" | "exclude";
}) {
  if (mode === "all") {
    return (
      <Badge variant="destructive" className="text-[10px] h-4 px-1">
        all tools
      </Badge>
    );
  }
  if (mode === "exclude") {
    return (
      <Badge variant="outline" className="text-[10px] h-4 px-1">
        exclude
      </Badge>
    );
  }
  return (
    <Badge variant="secondary" className="text-[10px] h-4 px-1">
      allowlist
    </Badge>
  );
}

// ── One-shot token reveal ─────────────────────────────────────────────


interface TokenRevealDialogProps {
  client: McpServerClient;
  token: string;
  onClose: () => void;
}

function TokenRevealDialog({
  client,
  token,
  onClose,
}: TokenRevealDialogProps) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(token);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard API can fail in insecure contexts — the user can
      // still select the visible token text and copy manually.
    }
  };

  return (
    <Dialog
      open={true}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Token for {client.name}</DialogTitle>
        </DialogHeader>

        <div className="space-y-4">
          <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm flex gap-2">
            <AlertTriangleIcon className="size-5 shrink-0 text-amber-600" />
            <div>
              <p className="font-medium">Save this token now.</p>
              <p className="text-muted-foreground mt-1">
                Gilbert only stores a hash — this is the one and only
                time you can copy the plaintext. Rotating the token
                issues a new one and invalidates this one.
              </p>
            </div>
          </div>

          <div className="relative">
            <Input
              value={token}
              readOnly
              className="font-mono text-xs pr-24"
              onFocus={(e) => e.target.select()}
            />
            <Button
              size="sm"
              variant="outline"
              className="absolute right-1 top-1 h-7"
              onClick={copy}
            >
              {copied ? (
                <>
                  <CheckIcon className="size-3 mr-1" />
                  Copied
                </>
              ) : (
                <>
                  <CopyIcon className="size-3 mr-1" />
                  Copy
                </>
              )}
            </Button>
          </div>

          <div className="text-xs text-muted-foreground">
            <p className="font-medium mb-1">How to use it:</p>
            <p>
              Configure the client to send{" "}
              <code>Authorization: Bearer &lt;token&gt;</code> when
              connecting to this Gilbert instance's{" "}
              <code>/api/mcp</code> endpoint. The client will act as{" "}
              <code>{client.owner_user_id}</code>.
            </p>
          </div>
        </div>

        <DialogFooter>
          <Button onClick={onClose}>Done</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ── Helpers ───────────────────────────────────────────────────────────


function formatTimestamp(iso: string | null): string {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    const now = new Date();
    const diffMs = now.getTime() - d.getTime();
    const diffMin = Math.floor(diffMs / 60_000);
    if (diffMin < 1) return "just now";
    if (diffMin < 60) return `${diffMin}m ago`;
    const diffHr = Math.floor(diffMin / 60);
    if (diffHr < 24) return `${diffHr}h ago`;
    const diffDay = Math.floor(diffHr / 24);
    if (diffDay < 7) return `${diffDay}d ago`;
    return d.toLocaleDateString();
  } catch {
    return iso;
  }
}
