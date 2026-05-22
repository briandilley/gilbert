import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { useAuth } from "@/hooks/useAuth";
import { hasRole } from "@/types/auth";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  PlayIcon,
  SquareIcon,
  PlusIcon,
  PencilIcon,
  Trash2Icon,
  AlertCircleIcon,
  RefreshCcwIcon,
  KeyRoundIcon,
  ChevronDownIcon,
  ChevronRightIcon,
} from "lucide-react";
import { McpServerDialog } from "./McpServerDialog";
import { McpResourcePanel } from "./McpResourcePanel";
import { McpPromptPanel } from "./McpPromptPanel";
import type { McpServer, McpServerDraft } from "@/types/mcp";

function scopeBadgeVariant(
  scope: McpServer["scope"],
): "neutral" | "outline" | "active" {
  switch (scope) {
    case "public":
      return "active";
    case "shared":
      return "neutral";
    default:
      return "outline";
  }
}

function formatStatus(server: McpServer): {
  label: string;
  tone: "ok" | "warn" | "idle";
} {
  if (!server.enabled) return { label: "disabled", tone: "idle" };
  if (server.connected) {
    return { label: `running · ${server.tool_count} tools`, tone: "ok" };
  }
  if (server.needs_oauth) {
    return { label: "sign-in required", tone: "warn" };
  }
  if (server.retry_count > 0) {
    return {
      label: `reconnecting · try ${server.retry_count}`,
      tone: "warn",
    };
  }
  if (server.last_error) return { label: "error", tone: "warn" };
  return { label: "stopped", tone: "idle" };
}

export function McpPage() {
  const api = useWsApi();
  const queryClient = useQueryClient();
  const { connected } = useWebSocket();
  const { user } = useAuth();
  const isAdmin = hasRole(user, "admin");

  const { data: servers, isLoading, refetch } = useQuery({
    queryKey: ["mcp-servers"],
    queryFn: api.listMcpServers,
    enabled: connected,
    refetchInterval: 15_000,
  });

  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<McpServer | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const toggleExpanded = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["mcp-servers"] });

  const saveMutation = useMutation({
    mutationFn: async (draft: McpServerDraft) => {
      if (draft.id) {
        await api.updateMcpServer(draft);
      } else {
        await api.createMcpServer(draft);
      }
    },
    onSuccess: invalidate,
  });

  const startMutation = useMutation({
    mutationFn: api.startMcpServer,
    onSuccess: invalidate,
  });
  const stopMutation = useMutation({
    mutationFn: api.stopMcpServer,
    onSuccess: invalidate,
  });
  const deleteMutation = useMutation({
    mutationFn: api.deleteMcpServer,
    onSuccess: invalidate,
  });

  const signInMutation = useMutation({
    mutationFn: async (serverId: string) => {
      // Kick off the OAuth flow on the server side; the RPC waits up
      // to ~30s for the SDK to produce the authorization URL (covers
      // OIDC discovery + dynamic client registration).
      const { authorization_url } = await api.startMcpOAuth(serverId);
      // Opening in a new tab lets the user complete auth without
      // losing the Gilbert tab. The callback route auto-closes the
      // popup on success; we rely on the 15 s refetch interval to
      // surface the newly-connected state here.
      window.open(authorization_url, "_blank", "noopener,noreferrer");
    },
    onSettled: invalidate,
  });

  const openNew = () => {
    setEditing(null);
    setDialogOpen(true);
  };
  const openEdit = (server: McpServer) => {
    setEditing(server);
    setDialogOpen(true);
  };
  const handleSave = async (draft: McpServerDraft) => {
    // Throw so the dialog can surface errors inline instead of closing.
    await saveMutation.mutateAsync(draft);
  };
  const handleDelete = (server: McpServer) => {
    if (!confirm(`Delete MCP server "${server.name}"? This cannot be undone.`))
      return;
    deleteMutation.mutate(server.id);
  };

  if (isLoading) {
    return (
      <div>
        <PageHeader eyebrow="MCP" title="Servers" />
        <LoadingSpinner text="Loading MCP servers..." className="p-8" />
      </div>
    );
  }

  const rows = servers ?? [];
  const mine = rows.filter((s) => s.owner_id === user?.user_id);
  const shared = rows.filter(
    (s) => s.owner_id !== user?.user_id && s.scope !== "private",
  );

  return (
    <div>
      <PageHeader
        eyebrow="MCP"
        title="Servers"
        description="Federate tools from external Model Context Protocol servers into Gilbert's AI. Private servers are visible only to you; shared and public servers are managed by admins."
        actions={
          <>
            <Button variant="outline" size="sm" onClick={() => refetch()}>
              <RefreshCcwIcon />
              Refresh
            </Button>
            <Button size="sm" onClick={openNew}>
              <PlusIcon />
              Add server
            </Button>
          </>
        }
      />

      <div className="mx-auto max-w-5xl px-4 py-4 sm:px-6 sm:py-6">
        {rows.length === 0 ? (
          <div className="flex flex-col items-center justify-center gap-3 rounded-md border border-dashed border-border py-16 text-center">
            <p className="font-mono text-[11px] uppercase tracking-[0.08em] text-muted-foreground">
              No MCP servers
            </p>
            <p className="max-w-md text-sm text-muted-foreground">
              Add a server to federate its tools into the AI. Servers can
              be private (only you), shared with a role or user, or public.
            </p>
            <Button size="sm" onClick={openNew}>
              <PlusIcon />
              Add server
            </Button>
          </div>
        ) : (
          <div className="space-y-6">
            <ServerSection
              title="My servers"
              servers={mine}
              emptyHint="You haven't created any MCP servers yet."
              canEdit={() => true}
              onEdit={openEdit}
              onStart={(s) => startMutation.mutate(s.id)}
              onStop={(s) => stopMutation.mutate(s.id)}
              onDelete={handleDelete}
              onSignIn={(s) => signInMutation.mutate(s.id)}
              expanded={expanded}
              onToggleExpanded={toggleExpanded}
            />
            {shared.length > 0 && (
              <ServerSection
                title={isAdmin ? "Shared & public" : "Available to you"}
                servers={shared}
                emptyHint=""
                canEdit={() => isAdmin}
                onEdit={openEdit}
                onStart={(s) => startMutation.mutate(s.id)}
                onStop={(s) => stopMutation.mutate(s.id)}
                onDelete={handleDelete}
                onSignIn={(s) => signInMutation.mutate(s.id)}
                expanded={expanded}
                onToggleExpanded={toggleExpanded}
              />
            )}
          </div>
        )}
      </div>

      <McpServerDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        existing={editing}
        onSave={handleSave}
      />
    </div>
  );
}

interface ServerSectionProps {
  title: string;
  servers: McpServer[];
  emptyHint: string;
  canEdit: (server: McpServer) => boolean;
  onEdit: (server: McpServer) => void;
  onStart: (server: McpServer) => void;
  onStop: (server: McpServer) => void;
  onDelete: (server: McpServer) => void;
  onSignIn: (server: McpServer) => void;
  expanded: Set<string>;
  onToggleExpanded: (id: string) => void;
}

function ServerSection({
  title,
  servers,
  emptyHint,
  canEdit,
  onEdit,
  onStart,
  onStop,
  onDelete,
  onSignIn,
  expanded,
  onToggleExpanded,
}: ServerSectionProps) {
  if (servers.length === 0 && !emptyHint) return null;
  return (
    <div>
      <h2 className="font-mono text-[11px] uppercase tracking-[0.08em] font-medium text-muted-foreground mb-2">
        {title}
      </h2>
      {servers.length === 0 ? (
        <Card>
          <CardContent className="py-6 text-center text-sm text-muted-foreground">
            {emptyHint}
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-2">
          {servers.map((server) => (
            <ServerRow
              key={server.id}
              server={server}
              canEdit={canEdit(server)}
              onEdit={onEdit}
              onStart={onStart}
              onStop={onStop}
              onDelete={onDelete}
              onSignIn={onSignIn}
              isExpanded={expanded.has(server.id)}
              onToggleExpanded={() => onToggleExpanded(server.id)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

interface ServerRowProps {
  server: McpServer;
  canEdit: boolean;
  onEdit: (server: McpServer) => void;
  onStart: (server: McpServer) => void;
  onStop: (server: McpServer) => void;
  onDelete: (server: McpServer) => void;
  onSignIn: (server: McpServer) => void;
  isExpanded: boolean;
  onToggleExpanded: () => void;
}

function ServerRow({
  server,
  canEdit,
  onEdit,
  onStart,
  onStop,
  onDelete,
  onSignIn,
  isExpanded,
  onToggleExpanded,
}: ServerRowProps) {
  const status = formatStatus(server);
  return (
    <Card>
      <CardContent className="p-4">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 flex-wrap">
              <button
                type="button"
                className="text-muted-foreground hover:text-foreground transition-colors"
                onClick={onToggleExpanded}
                aria-label={isExpanded ? "Collapse details" : "Expand details"}
              >
                {isExpanded ? (
                  <ChevronDownIcon className="size-4" />
                ) : (
                  <ChevronRightIcon className="size-4" />
                )}
              </button>
              <span className="font-medium">{server.name}</span>
              <code className="text-xs text-muted-foreground">
                /mcp.{server.slug}
              </code>
              <Badge variant={scopeBadgeVariant(server.scope)}>
                {server.scope}
              </Badge>
              <Badge variant={statusToVariant(status.tone)} dot>{status.label}</Badge>
            </div>
            {server.scope === "shared" && (
              <div className="mt-1 text-xs text-muted-foreground">
                Shared with{" "}
                {server.allowed_roles.length > 0 && (
                  <>roles: {server.allowed_roles.join(", ")}</>
                )}
                {server.allowed_roles.length > 0 &&
                  server.allowed_users.length > 0 &&
                  "; "}
                {server.allowed_users.length > 0 && (
                  <>users: {server.allowed_users.join(", ")}</>
                )}
              </div>
            )}
            <div className="mt-1 text-xs text-muted-foreground font-mono truncate">
              {server.command.join(" ") || "(no command)"}
            </div>
            {server.last_error && (
              <div className="mt-1 text-xs text-destructive flex items-center gap-1">
                <AlertCircleIcon className="size-3" />
                {server.last_error}
              </div>
            )}
          </div>
          {canEdit && (
            <div className="flex gap-1 shrink-0">
              {server.needs_oauth && (
                <Button
                  variant="default"
                  size="sm"
                  onClick={() => onSignIn(server)}
                >
                  <KeyRoundIcon className="size-4 mr-1" />
                  Sign in
                </Button>
              )}
              {server.connected ? (
                <Button
                  variant="ghost"
                  size="icon-sm"
                  title="Stop"
                  onClick={() => onStop(server)}
                >
                  <SquareIcon className="size-4" />
                </Button>
              ) : !server.needs_oauth ? (
                <Button
                  variant="ghost"
                  size="icon-sm"
                  title="Start"
                  onClick={() => onStart(server)}
                >
                  <PlayIcon className="size-4" />
                </Button>
              ) : null}
              <Button
                variant="ghost"
                size="icon-sm"
                title="Edit"
                onClick={() => onEdit(server)}
              >
                <PencilIcon className="size-4" />
              </Button>
              <Button
                variant="ghost"
                size="icon-sm"
                title="Delete"
                onClick={() => onDelete(server)}
              >
                <Trash2Icon className="size-4" />
              </Button>
            </div>
          )}
        </div>
        {isExpanded && (
          <div className="mt-4 pt-4 border-t space-y-4">
            <McpResourcePanel server={server} />
            <McpPromptPanel server={server} />
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/** Map the McpStatus tone strings onto the design-system Badge
 *  state variants. The dot prop carries the semantic color. */
function statusToVariant(tone: "ok" | "warn" | "idle"): "success" | "error" | "off" {
  if (tone === "ok") return "success";
  if (tone === "warn") return "error";
  return "off";
}
