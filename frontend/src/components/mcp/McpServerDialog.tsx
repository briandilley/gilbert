import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { useAuth } from "@/hooks/useAuth";
import { Button } from "@/components/ui/button";
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
import { Badge } from "@/components/ui/badge";
import { hasRole } from "@/types/auth";
import type {
  McpAuthKind,
  McpScope,
  McpServer,
  McpServerDraft,
  McpToolSpec,
  McpTransport,
} from "@/types/mcp";
import { EditProfileLink } from "@/components/roles/EditProfileLink";

interface McpServerDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Existing server when editing; ``null`` when creating. */
  existing: McpServer | null;
  onSave: (draft: McpServerDraft) => Promise<void>;
}

/** Convert a command array to a shell-ish display string and back. */
function commandToText(cmd: string[]): string {
  return cmd.map((part) => (/\s/.test(part) ? `"${part}"` : part)).join(" ");
}

function parseCommand(text: string): string[] {
  // Minimal shell-style splitter: supports quoted segments, no escapes.
  const parts: string[] = [];
  let buf = "";
  let quote: '"' | "'" | null = null;
  for (const ch of text) {
    if (quote) {
      if (ch === quote) {
        quote = null;
      } else {
        buf += ch;
      }
    } else if (ch === '"' || ch === "'") {
      quote = ch;
    } else if (/\s/.test(ch)) {
      if (buf) {
        parts.push(buf);
        buf = "";
      }
    } else {
      buf += ch;
    }
  }
  if (buf) parts.push(buf);
  return parts;
}

function envToText(env: Record<string, string>): string {
  return Object.entries(env)
    .map(([k, v]) => `${k}=${v}`)
    .join("\n");
}

function parseEnv(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq <= 0) continue;
    const key = line.slice(0, eq).trim();
    const value = line.slice(eq + 1);
    if (key) out[key] = value;
  }
  return out;
}

function slugify(name: string): string {
  const s = name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  if (!s) return "";
  return /^[a-z]/.test(s) ? s : `s-${s}`;
}

const EMPTY_DRAFT: McpServerDraft = {
  name: "",
  slug: "",
  transport: "stdio",
  command: [],
  env: {},
  cwd: null,
  url: null,
  auth: {
    kind: "none",
    bearer_token: "",
    oauth_scopes: [],
    oauth_client_name: "Gilbert",
  },
  enabled: true,
  auto_start: true,
  scope: "private",
  allowed_roles: [],
  allowed_users: [],
  tool_cache_ttl_seconds: 300,
  allow_sampling: false,
  sampling_profile: "mcp_sampling",
  sampling_budget_tokens: 10000,
  sampling_budget_window_seconds: 3600,
};

export function McpServerDialog({
  open,
  onOpenChange,
  existing,
  onSave,
}: McpServerDialogProps) {
  const api = useWsApi();
  const { user } = useAuth();
  const isAdmin = hasRole(user, "admin");

  const [draft, setDraft] = useState<McpServerDraft>(EMPTY_DRAFT);
  const [commandText, setCommandText] = useState("");
  const [envText, setEnvText] = useState("");
  const [saving, setSaving] = useState(false);
  const [testResult, setTestResult] = useState<
    { tools: McpToolSpec[] } | { error: string } | null
  >(null);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Directory lookups for the shared-scope allow-lists. Only fetched
  // when the dialog opens so offline / non-admin users don't pay for
  // them on every page render.
  const { data: users } = useQuery({
    queryKey: ["mcp-users"],
    queryFn: api.listChatUsers,
    enabled: open && isAdmin,
  });
  const { data: rolesData } = useQuery({
    queryKey: ["mcp-roles"],
    queryFn: api.listRoles,
    enabled: open && isAdmin,
  });
  const availableRoles = rolesData?.roles ?? [];

  // Populate the form when opening, reset state between uses.
  useEffect(() => {
    if (!open) return;
    setError(null);
    setTestResult(null);
    if (existing) {
      const d: McpServerDraft = {
        id: existing.id,
        name: existing.name,
        slug: existing.slug,
        transport: existing.transport,
        command: existing.command,
        env: existing.env,
        cwd: existing.cwd,
        url: existing.url,
        auth: existing.auth,
        enabled: existing.enabled,
        auto_start: existing.auto_start,
        scope: existing.scope,
        allowed_roles: existing.allowed_roles,
        allowed_users: existing.allowed_users,
        tool_cache_ttl_seconds: existing.tool_cache_ttl_seconds,
        allow_sampling: existing.allow_sampling,
        sampling_profile: existing.sampling_profile || "mcp_sampling",
        sampling_budget_tokens: existing.sampling_budget_tokens || 10000,
        sampling_budget_window_seconds:
          existing.sampling_budget_window_seconds || 3600,
      };
      setDraft(d);
      setCommandText(commandToText(d.command));
      setEnvText(envToText(d.env));
    } else {
      setDraft(EMPTY_DRAFT);
      setCommandText("");
      setEnvText("");
    }
  }, [open, existing]);

  const slugAutoFills = useMemo(
    () => !existing && draft.slug === slugify(draft.name.slice(0, -1)),
    [existing, draft.slug, draft.name],
  );

  const updateDraft = (patch: Partial<McpServerDraft>) => {
    setDraft((d) => ({ ...d, ...patch }));
  };

  const onNameChange = (value: string) => {
    setDraft((d) => {
      const nextSlug =
        !existing && (d.slug === "" || d.slug === slugify(d.name))
          ? slugify(value)
          : d.slug;
      return { ...d, name: value, slug: nextSlug };
    });
  };

  const collectDraft = (): McpServerDraft => ({
    ...draft,
    command: parseCommand(commandText),
    env: parseEnv(envText),
  });

  const runTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const tools = await api.testMcpServer(collectDraft());
      setTestResult({ tools });
    } catch (e) {
      setTestResult({ error: String(e) });
    } finally {
      setTesting(false);
    }
  };

  const runSave = async () => {
    setSaving(true);
    setError(null);
    try {
      await onSave(collectDraft());
      onOpenChange(false);
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  };

  const scopeOptions: { value: McpScope; label: string; disabled: boolean }[] =
    [
      { value: "private", label: "Private (only me)", disabled: false },
      {
        value: "shared",
        label: "Shared (specific roles/users)",
        disabled: !isAdmin,
      },
      { value: "public", label: "Public (everyone)", disabled: !isAdmin },
    ];

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>
            {existing ? `Edit ${existing.name}` : "Add MCP Server"}
          </DialogTitle>
        </DialogHeader>

        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label htmlFor="mcp-name">Name</Label>
              <Input
                id="mcp-name"
                value={draft.name}
                onChange={(e) => onNameChange(e.target.value)}
                placeholder="Weather"
              />
            </div>
            <div>
              <Label htmlFor="mcp-slug">Slug</Label>
              <Input
                id="mcp-slug"
                value={draft.slug}
                onChange={(e) => updateDraft({ slug: e.target.value })}
                placeholder="weather"
                disabled={!!existing}
              />
              {slugAutoFills && (
                <p className="text-xs text-muted-foreground mt-1">
                  Auto-derived from name
                </p>
              )}
            </div>
          </div>

          <div>
            <Label htmlFor="mcp-transport">Transport</Label>
            <Select
              value={draft.transport}
              onValueChange={(v) => {
                if (v) updateDraft({ transport: v as McpTransport });
              }}
            >
              <SelectTrigger id="mcp-transport">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="stdio">
                  Stdio (local subprocess)
                </SelectItem>
                <SelectItem value="http">
                  HTTP (Streamable HTTP)
                </SelectItem>
                <SelectItem value="sse">
                  SSE (Server-Sent Events)
                </SelectItem>
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground mt-1">
              Stdio spawns a subprocess and speaks MCP over stdin/stdout.
              HTTP and SSE connect to a remote MCP server over the network.
            </p>
          </div>

          {draft.transport === "stdio" ? (
            <>
              <div>
                <Label htmlFor="mcp-command">Command</Label>
                <Input
                  id="mcp-command"
                  value={commandText}
                  onChange={(e) => setCommandText(e.target.value)}
                  placeholder="npx -y @modelcontextprotocol/server-weather"
                />
                <p className="text-xs text-muted-foreground mt-1">
                  First token is the executable; remaining tokens are
                  arguments. Quote with <code>"</code> for values
                  containing spaces.
                </p>
              </div>

              <div>
                <Label htmlFor="mcp-env">
                  Environment (KEY=VALUE per line)
                </Label>
                <Textarea
                  id="mcp-env"
                  value={envText}
                  onChange={(e) => setEnvText(e.target.value)}
                  placeholder="API_KEY=sk-..."
                  rows={4}
                  className="font-mono text-sm"
                />
                <p className="text-xs text-muted-foreground mt-1">
                  Values shown as <code>****</code> are masked secrets —
                  leave them unchanged to keep the stored value.
                </p>
              </div>

              <div>
                <Label htmlFor="mcp-cwd">
                  Working directory (optional)
                </Label>
                <Input
                  id="mcp-cwd"
                  value={draft.cwd ?? ""}
                  onChange={(e) =>
                    updateDraft({ cwd: e.target.value || null })
                  }
                  placeholder="/path/to/dir"
                />
              </div>
            </>
          ) : (
            <>
              <div>
                <Label htmlFor="mcp-url">Server URL</Label>
                <Input
                  id="mcp-url"
                  type="url"
                  value={draft.url ?? ""}
                  onChange={(e) =>
                    updateDraft({ url: e.target.value || null })
                  }
                  placeholder="https://example.com/mcp"
                />
              </div>

              <div>
                <Label htmlFor="mcp-auth-kind">Authentication</Label>
                <Select
                  value={draft.auth.kind}
                  onValueChange={(v) => {
                    if (!v) return;
                    updateDraft({
                      auth: { ...draft.auth, kind: v as McpAuthKind },
                    });
                  }}
                >
                  <SelectTrigger id="mcp-auth-kind">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="none">None (open server)</SelectItem>
                    <SelectItem value="bearer">Bearer token</SelectItem>
                    <SelectItem value="oauth">OAuth 2.1</SelectItem>
                  </SelectContent>
                </Select>
                {draft.auth.kind === "oauth" && (
                  <p className="text-xs text-muted-foreground mt-1">
                    Save the server first, then click{" "}
                    <strong>Sign in</strong> on the row to authenticate.
                    Gilbert will open the server's sign-in page in a new
                    tab and handle the callback automatically.
                  </p>
                )}
              </div>

              {draft.auth.kind === "bearer" && (
                <div>
                  <Label htmlFor="mcp-bearer">Bearer token</Label>
                  <Input
                    id="mcp-bearer"
                    type="password"
                    value={draft.auth.bearer_token}
                    onChange={(e) =>
                      updateDraft({
                        auth: {
                          ...draft.auth,
                          bearer_token: e.target.value,
                        },
                      })
                    }
                    placeholder="sk-..."
                  />
                  <p className="text-xs text-muted-foreground mt-1">
                    Sent as <code>Authorization: Bearer &lt;token&gt;</code>.
                    Shown as <code>****</code> in list views; leave it
                    unchanged to keep the stored token.
                  </p>
                </div>
              )}
            </>
          )}

          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label htmlFor="mcp-scope">Visibility</Label>
              <Select
                value={draft.scope}
                onValueChange={(v) => {
                  if (v) updateDraft({ scope: v as McpScope });
                }}
              >
                <SelectTrigger id="mcp-scope">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {scopeOptions.map((opt) => (
                    <SelectItem
                      key={opt.value}
                      value={opt.value}
                      disabled={opt.disabled}
                    >
                      {opt.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground mt-1">
                {isAdmin
                  ? "If a user can see this server, they can use its tools."
                  : "Only admins can create shared or public servers. " +
                    "If a user can see this server, they can use its tools."}
              </p>
            </div>
            <div>
              <Label htmlFor="mcp-ttl">Tool cache TTL (seconds)</Label>
              <Input
                id="mcp-ttl"
                type="number"
                min={1}
                value={draft.tool_cache_ttl_seconds}
                onChange={(e) =>
                  updateDraft({
                    tool_cache_ttl_seconds: Number(e.target.value) || 300,
                  })
                }
              />
            </div>
          </div>

          {draft.scope === "shared" && (
            <div className="space-y-3 rounded-md border p-3 bg-muted/30">
              <div>
                <Label>Allowed roles</Label>
                <div className="flex flex-wrap gap-1 mt-1">
                  {availableRoles.map((role) => {
                    const on = draft.allowed_roles.includes(role.name);
                    return (
                      <Badge
                        key={role.name}
                        variant={on ? "default" : "outline"}
                        className="cursor-pointer select-none"
                        onClick={() =>
                          updateDraft({
                            allowed_roles: on
                              ? draft.allowed_roles.filter(
                                  (r) => r !== role.name,
                                )
                              : [...draft.allowed_roles, role.name],
                          })
                        }
                      >
                        {role.name}
                      </Badge>
                    );
                  })}
                </div>
              </div>
              <div>
                <Label>Allowed users</Label>
                <div className="flex flex-wrap gap-1 mt-1">
                  {(users ?? []).map((u) => {
                    const on = draft.allowed_users.includes(u.user_id);
                    return (
                      <Badge
                        key={u.user_id}
                        variant={on ? "default" : "outline"}
                        className="cursor-pointer select-none"
                        onClick={() =>
                          updateDraft({
                            allowed_users: on
                              ? draft.allowed_users.filter(
                                  (id) => id !== u.user_id,
                                )
                              : [...draft.allowed_users, u.user_id],
                          })
                        }
                      >
                        {u.display_name || u.user_id}
                      </Badge>
                    );
                  })}
                </div>
                <p className="text-xs text-muted-foreground mt-1">
                  Either allow-list grants access. Must have at least one
                  entry across both lists.
                </p>
              </div>
            </div>
          )}

          {isAdmin && draft.transport !== "stdio" && (
            <div className="space-y-3 rounded-md border p-3 bg-amber-500/5 border-amber-500/30">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="text-sm font-medium">
                    Sampling (server-initiated AI calls)
                  </div>
                  <p className="text-xs text-muted-foreground mt-1">
                    Allow this MCP server to ask Gilbert to run LLM calls
                    on its behalf. Off by default. Admin-only. Consumes
                    AI budget — use with servers you trust.
                  </p>
                </div>
                <label className="flex items-center gap-2 text-sm shrink-0">
                  <input
                    type="checkbox"
                    checked={draft.allow_sampling}
                    onChange={(e) =>
                      updateDraft({ allow_sampling: e.target.checked })
                    }
                  />
                  Enabled
                </label>
              </div>
              {draft.allow_sampling && (
                <div className="grid grid-cols-2 gap-3">
                  <div className="col-span-2">
                    <Label htmlFor="mcp-sampling-profile">
                      AI profile
                    </Label>
                    <Input
                      id="mcp-sampling-profile"
                      value={draft.sampling_profile}
                      onChange={(e) =>
                        updateDraft({ sampling_profile: e.target.value })
                      }
                      placeholder="mcp_sampling"
                    />
                    <EditProfileLink profileName={draft.sampling_profile} />
                    <p className="text-xs text-muted-foreground mt-1">
                      Name of an AI context profile. The built-in{" "}
                      <code>mcp_sampling</code> profile has no tools —
                      pick a different one only if you really want the
                      remote server to reach your tools.
                    </p>
                  </div>
                  <div>
                    <Label htmlFor="mcp-sampling-budget">
                      Token budget
                    </Label>
                    <Input
                      id="mcp-sampling-budget"
                      type="number"
                      min={1}
                      value={draft.sampling_budget_tokens}
                      onChange={(e) =>
                        updateDraft({
                          sampling_budget_tokens:
                            Number(e.target.value) || 0,
                        })
                      }
                    />
                  </div>
                  <div>
                    <Label htmlFor="mcp-sampling-window">
                      Window (seconds)
                    </Label>
                    <Input
                      id="mcp-sampling-window"
                      type="number"
                      min={1}
                      value={draft.sampling_budget_window_seconds}
                      onChange={(e) =>
                        updateDraft({
                          sampling_budget_window_seconds:
                            Number(e.target.value) || 0,
                        })
                      }
                    />
                  </div>
                </div>
              )}
            </div>
          )}

          <div className="flex gap-4">
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={draft.enabled}
                onChange={(e) => updateDraft({ enabled: e.target.checked })}
              />
              Enabled
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={draft.auto_start}
                onChange={(e) =>
                  updateDraft({ auto_start: e.target.checked })
                }
              />
              Auto-start on boot
            </label>
          </div>

          {error && (
            <div className="rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
              {error}
            </div>
          )}

          {testResult && "error" in testResult && (
            <div className="rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
              Test failed: {testResult.error}
            </div>
          )}

          {testResult && "tools" in testResult && (
            <div className="rounded-md border p-3 text-sm">
              <div className="font-medium mb-1">
                Server responded with {testResult.tools.length} tool
                {testResult.tools.length === 1 ? "" : "s"}:
              </div>
              <ul className="list-disc pl-5 space-y-0.5">
                {testResult.tools.map((t) => (
                  <li key={t.name}>
                    <code>{t.name}</code>
                    {t.description ? ` — ${t.description}` : ""}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            onClick={runTest}
            disabled={testing || !commandText.trim()}
          >
            {testing ? "Testing..." : "Test connection"}
          </Button>
          <Button
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={saving}
          >
            Cancel
          </Button>
          <Button onClick={runSave} disabled={saving}>
            {saving ? "Saving..." : existing ? "Save changes" : "Create"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
