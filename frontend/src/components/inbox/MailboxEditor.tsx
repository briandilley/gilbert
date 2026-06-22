import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ConfigField } from "@/components/settings/ConfigField";
import { isParamVisible } from "@/lib/configVisibility";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import type { InboxMailbox, EmailBackendInfo } from "@/types/inbox";
import type { UserRoleAssignment } from "@/types/roles";
import { Trash2Icon, CheckIcon, AlertTriangleIcon, SearchIcon } from "lucide-react";

interface MailboxEditorProps {
  /** If null, editor is in "create" mode. */
  mailbox: InboxMailbox | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/** Mailbox create/edit drawer.
 *
 * Renders the generic fields (name, email, poll settings), then a
 * backend selector that drives a dynamically-rendered block of
 * backend-specific ConfigField inputs. Owner/admin-only controls
 * (sharing, delete) appear when the caller has ``can_admin``.
 */
export function MailboxEditor({
  mailbox, open, onOpenChange,
}: MailboxEditorProps) {
  const api = useWsApi();
  const queryClient = useQueryClient();
  const isCreate = mailbox === null;

  // Form state
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [backendName, setBackendName] = useState("");
  const [backendConfig, setBackendConfig] = useState<Record<string, unknown>>({});
  const [pollEnabled, setPollEnabled] = useState(true);
  const [pollInterval, setPollInterval] = useState(60);

  // Test connection feedback
  const [testResult, setTestResult] = useState<
    { ok: boolean; error: string } | null
  >(null);
  const [backendActionResult, setBackendActionResult] = useState<string | null>(null);
  const [backendActionRunning, setBackendActionRunning] = useState<string>("");

  // Reset form whenever the drawer opens with a new target
  useEffect(() => {
    if (!open) return;
    if (mailbox) {
      setName(mailbox.name);
      setEmail(mailbox.email_address);
      setBackendName(mailbox.backend_name);
      setBackendConfig(mailbox.backend_config || {});
      setPollEnabled(mailbox.poll_enabled);
      setPollInterval(mailbox.poll_interval_sec);
    } else {
      setName("");
      setEmail("");
      setBackendName("");
      setBackendConfig({});
      setPollEnabled(true);
      setPollInterval(60);
    }
    setTestResult(null);
  }, [mailbox, open]);

  const { connected } = useWebSocket();

  // Available backends (for select + config param schema)
  const { data: backends = [] } = useQuery<EmailBackendInfo[]>({
    queryKey: ["email-backends"],
    queryFn: api.listEmailBackends,
    enabled: open,
  });

  // Users + role names for the sharing picker (edit-mode only).
  // Lists every user in Gilbert + every named role; the picker filters
  // them in-memory.
  const { data: userRoles } = useQuery({
    queryKey: ["roles-user-list"],
    queryFn: api.listUserRoles,
    enabled: open && connected && !!mailbox && (mailbox?.can_admin ?? false),
  });
  const allUsers: UserRoleAssignment[] = userRoles?.users ?? [];
  const allRoles: string[] = userRoles?.role_names ?? [];

  const activeBackend = useMemo(
    () => backends.find((b) => b.name === backendName),
    [backends, backendName],
  );

  // ---- Mutations ----

  const createMutation = useMutation({
    mutationFn: () =>
      api.createMailbox({
        name,
        email_address: email,
        backend_name: backendName,
        backend_config: backendConfig,
        poll_enabled: pollEnabled,
        poll_interval_sec: pollInterval,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["inbox-mailboxes"] });
      onOpenChange(false);
    },
  });

  const updateMutation = useMutation({
    mutationFn: () =>
      api.updateMailbox(mailbox!.id, {
        name,
        email_address: email,
        backend_name: backendName,
        backend_config: backendConfig,
        poll_enabled: pollEnabled,
        poll_interval_sec: pollInterval,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["inbox-mailboxes"] });
      onOpenChange(false);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: () => api.deleteMailbox(mailbox!.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["inbox-mailboxes"] });
      onOpenChange(false);
    },
  });

  const testMutation = useMutation({
    mutationFn: () => api.testMailboxConnection(mailbox!.id),
    onSuccess: (data) => setTestResult(data),
  });

  const shareUser = useMutation({
    mutationFn: (userId: string) => api.shareMailboxUser(mailbox!.id, userId),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["inbox-mailboxes"] }),
  });

  const unshareUser = useMutation({
    mutationFn: (userId: string) => api.unshareMailboxUser(mailbox!.id, userId),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["inbox-mailboxes"] }),
  });

  const shareRole = useMutation({
    mutationFn: (role: string) => api.shareMailboxRole(mailbox!.id, role),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["inbox-mailboxes"] }),
  });

  const unshareRole = useMutation({
    mutationFn: (role: string) => api.unshareMailboxRole(mailbox!.id, role),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["inbox-mailboxes"] }),
  });

  const toggleUser = (userId: string, currentlyShared: boolean) => {
    if (currentlyShared) {
      unshareUser.mutate(userId);
    } else {
      shareUser.mutate(userId);
    }
  };

  const toggleRole = (role: string, currentlyShared: boolean) => {
    if (currentlyShared) {
      unshareRole.mutate(role);
    } else {
      shareRole.mutate(role);
    }
  };

  const handleBackendConfigChange = (key: string, value: unknown) => {
    setBackendConfig((prev) => ({ ...prev, [key]: value }));
  };

  const runBackendAction = async (key: string) => {
    setBackendActionResult(null);
    setBackendActionRunning(key);
    try {
      const response = await api.invokeConfigAction("inbox", key, {
        backend: backendName,
        config: {
          ...backendConfig,
          email_address: email,
        },
      });
      const result = response.result;
      const persistRaw = (result.data ?? {})["persist"];
      if (persistRaw && typeof persistRaw === "object") {
        setBackendConfig((prev) => ({
          ...prev,
          ...(persistRaw as Record<string, unknown>),
        }));
      }
      if (result.open_url) {
        window.open(result.open_url, "_blank", "noopener,noreferrer");
      }
      setBackendActionResult(result.message || result.status);
    } catch (e) {
      setBackendActionResult((e as Error).message || "Backend action failed");
    } finally {
      setBackendActionRunning("");
    }
  };

  const canSave = Boolean(name.trim() && backendName);
  const canAdmin = mailbox?.can_admin ?? true; // create mode is always "admin"

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="flex h-full w-full flex-col gap-0 overflow-hidden sm:!max-w-xl">
        <SheetHeader>
          <SheetTitle>{isCreate ? "New Mailbox" : `Edit ${mailbox?.name}`}</SheetTitle>
        </SheetHeader>

        <div className="flex-1 space-y-5 overflow-y-auto px-4 pb-4">
          {/* Generic mailbox fields */}
          <section className="space-y-3">
            <div className="space-y-1.5">
              <Label htmlFor="mbx-name">Name</Label>
              <Input
                id="mbx-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Work"
                disabled={!canAdmin}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="mbx-email">Email address</Label>
              <Input
                id="mbx-email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
                disabled={!canAdmin}
              />
              <p className="text-xs text-muted-foreground">
                Gilbert compares this address to incoming messages to tell
                outbound vs inbound. Use the account you're authenticating as.
              </p>
            </div>
          </section>

          <Separator />

          {/* Backend selection + dynamic backend params */}
          <section className="space-y-3">
            <div className="space-y-1.5">
              <Label>Backend</Label>
              <Select
                value={backendName}
                onValueChange={(v) => setBackendName(v ?? "")}
                disabled={!canAdmin}
              >
                <SelectTrigger className="w-full">
                  <SelectValue placeholder="Select an email backend..." />
                </SelectTrigger>
                <SelectContent>
                  {backends.map((b) => (
                    <SelectItem key={b.name} value={b.name}>
                      {b.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {activeBackend && activeBackend.config_params.length > 0 && (
              <div className="space-y-3 rounded-md border bg-muted/30 p-3">
                <div className="text-xs font-medium text-muted-foreground">
                  {activeBackend.name} settings
                </div>
                {activeBackend.config_params
                  .filter((p) => isParamVisible(p, backendConfig))
                  .map((p) => (
                    <ConfigField
                      key={p.key}
                      param={p}
                      value={backendConfig[p.key] ?? p.default}
                      onChange={handleBackendConfigChange}
                    />
                  ))}
                {(activeBackend.actions ?? []).filter((a) => !a.hidden).length > 0 && (
                  <div className="flex flex-wrap gap-2 pt-1">
                    {(activeBackend.actions ?? [])
                      .filter((a) => !a.hidden)
                      .map((a) => (
                        <Button
                          key={a.key}
                          type="button"
                          variant="outline"
                          size="sm"
                          onClick={() => runBackendAction(a.key)}
                          disabled={backendActionRunning === a.key}
                        >
                          {a.key === "test_connection" ? (
                            <CheckIcon className="h-4 w-4 mr-1" />
                          ) : (
                            <SearchIcon className="h-4 w-4 mr-1" />
                          )}
                          {backendActionRunning === a.key ? "Running..." : a.label}
                        </Button>
                      ))}
                  </div>
                )}
                {backendActionResult && (
                  <p className="text-xs text-muted-foreground">{backendActionResult}</p>
                )}
              </div>
            )}
          </section>

          <Separator />

          {/* Polling */}
          <section className="space-y-3">
            <div className="flex items-center gap-2">
              <input
                id="mbx-poll"
                type="checkbox"
                className="h-4 w-4 accent-primary"
                checked={pollEnabled}
                onChange={(e) => setPollEnabled(e.target.checked)}
                disabled={!canAdmin}
              />
              <Label htmlFor="mbx-poll">Poll this mailbox for new mail</Label>
            </div>
            {pollEnabled && (
              <div className="space-y-1.5">
                <Label htmlFor="mbx-interval">Poll interval (seconds)</Label>
                <Input
                  id="mbx-interval"
                  type="number"
                  min="10"
                  value={pollInterval}
                  onChange={(e) => setPollInterval(parseInt(e.target.value, 10) || 60)}
                  disabled={!canAdmin}
                />
              </div>
            )}
          </section>

          {/* Sharing (edit mode, admin only) */}
          {!isCreate && canAdmin && mailbox && (
            <>
              <Separator />
              <section className="space-y-3">
                <div className="text-xs font-medium text-muted-foreground">
                  Sharing
                </div>
                <p className="text-xs text-muted-foreground">
                  Shared users have full read/send access to this mailbox, but
                  cannot edit settings or sharing.
                </p>

                <CheckboxPicker
                  label="Users"
                  options={allUsers.map((u) => ({
                    value: u.user_id,
                    label: u.display_name || u.username || u.user_id,
                    sublabel: u.email,
                  }))}
                  selected={mailbox.shared_with_users}
                  onToggle={toggleUser}
                  emptyText="No users found."
                />

                <CheckboxPicker
                  label="Roles"
                  options={allRoles.map((r) => ({ value: r, label: r }))}
                  selected={mailbox.shared_with_roles}
                  onToggle={toggleRole}
                  emptyText="No roles found."
                />
              </section>
            </>
          )}

          {/* Test connection + delete (edit mode) */}
          {!isCreate && canAdmin && (
            <>
              <Separator />
              <section className="space-y-3">
                <div className="flex items-center gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => testMutation.mutate()}
                    disabled={testMutation.isPending}
                  >
                    Test connection
                  </Button>
                  {testResult && (
                    <Badge
                      variant={testResult.ok ? "default" : "destructive"}
                      className="gap-1"
                    >
                      {testResult.ok ? (
                        <CheckIcon className="size-3" />
                      ) : (
                        <AlertTriangleIcon className="size-3" />
                      )}
                      {testResult.ok ? "OK" : testResult.error || "Failed"}
                    </Badge>
                  )}
                </div>

                <Button
                  variant="destructive"
                  size="sm"
                  onClick={() => {
                    if (
                      confirm(
                        "Delete this mailbox? Messages and outbox history will be removed. " +
                          "Pending outbox drafts must be cancelled first.",
                      )
                    ) {
                      deleteMutation.mutate();
                    }
                  }}
                  disabled={deleteMutation.isPending}
                >
                  <Trash2Icon className="size-3.5 mr-1.5" />
                  Delete mailbox
                </Button>
                {deleteMutation.error && (
                  <p className="text-xs text-destructive">
                    {(deleteMutation.error as Error).message}
                  </p>
                )}
              </section>
            </>
          )}
        </div>

        {canAdmin && (
          <div className="flex items-center justify-end gap-2 border-t bg-background px-4 py-3">
            <Button variant="ghost" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button
              onClick={() => (isCreate ? createMutation.mutate() : updateMutation.mutate())}
              disabled={!canSave || createMutation.isPending || updateMutation.isPending}
            >
              {isCreate ? "Create mailbox" : "Save changes"}
            </Button>
          </div>
        )}
      </SheetContent>
    </Sheet>
  );
}

interface CheckboxPickerOption {
  value: string;
  label: string;
  sublabel?: string;
}

/** A filterable list of checkboxes for picking which users / roles a
 * mailbox is shared with. Filter matches against label and sublabel
 * case-insensitively; selected items always remain visible regardless
 * of the filter so the user can uncheck them. */
function CheckboxPicker({
  label,
  options,
  selected,
  onToggle,
  emptyText,
}: {
  label: string;
  options: CheckboxPickerOption[];
  selected: string[];
  onToggle: (value: string, currentlyShared: boolean) => void;
  emptyText: string;
}) {
  const [filter, setFilter] = useState("");
  const selectedSet = useMemo(() => new Set(selected), [selected]);

  const visible = useMemo(() => {
    const f = filter.trim().toLowerCase();
    if (!f) return options;
    return options.filter((o) => {
      if (selectedSet.has(o.value)) return true; // never hide checked items
      return (
        o.label.toLowerCase().includes(f) ||
        (o.sublabel?.toLowerCase().includes(f) ?? false) ||
        o.value.toLowerCase().includes(f)
      );
    });
  }, [filter, options, selectedSet]);

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between">
        <Label className="text-xs">{label}</Label>
        <span className="text-[10px] text-muted-foreground">
          {selected.length} selected
        </span>
      </div>
      <div className="relative">
        <SearchIcon className="absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder={`Filter ${label.toLowerCase()}...`}
          className="pl-7 text-sm"
        />
      </div>
      <div className="max-h-48 overflow-y-auto rounded-md border bg-background">
        {visible.length === 0 ? (
          <div className="px-3 py-4 text-center text-xs text-muted-foreground">
            {options.length === 0 ? emptyText : "No matches."}
          </div>
        ) : (
          <ul className="divide-y">
            {visible.map((opt) => {
              const checked = selectedSet.has(opt.value);
              return (
                <li key={opt.value}>
                  <label
                    className="flex cursor-pointer items-center gap-2 px-3 py-2 hover:bg-accent/40"
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => onToggle(opt.value, checked)}
                      className="h-4 w-4 rounded border-input accent-primary"
                    />
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm">{opt.label}</div>
                      {opt.sublabel && (
                        <div className="truncate text-[11px] text-muted-foreground">
                          {opt.sublabel}
                        </div>
                      )}
                    </div>
                  </label>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
