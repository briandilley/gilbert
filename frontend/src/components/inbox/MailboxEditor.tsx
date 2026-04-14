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
import { useWsApi } from "@/hooks/useWsApi";
import type { InboxMailbox, EmailBackendInfo } from "@/types/inbox";
import { PlusIcon, XIcon, Trash2Icon, CheckIcon, AlertTriangleIcon } from "lucide-react";

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

  // Sharing state (edit-mode only)
  const [newUserShare, setNewUserShare] = useState("");
  const [newRoleShare, setNewRoleShare] = useState("");

  // Test connection feedback
  const [testResult, setTestResult] = useState<
    { ok: boolean; error: string } | null
  >(null);

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

  // Available backends (for select + config param schema)
  const { data: backends = [] } = useQuery<EmailBackendInfo[]>({
    queryKey: ["email-backends"],
    queryFn: api.listEmailBackends,
    enabled: open,
  });

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
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["inbox-mailboxes"] });
      setNewUserShare("");
    },
  });

  const unshareUser = useMutation({
    mutationFn: (userId: string) => api.unshareMailboxUser(mailbox!.id, userId),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["inbox-mailboxes"] }),
  });

  const shareRole = useMutation({
    mutationFn: (role: string) => api.shareMailboxRole(mailbox!.id, role),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["inbox-mailboxes"] });
      setNewRoleShare("");
    },
  });

  const unshareRole = useMutation({
    mutationFn: (role: string) => api.unshareMailboxRole(mailbox!.id, role),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["inbox-mailboxes"] }),
  });

  const handleBackendConfigChange = (key: string, value: unknown) => {
    setBackendConfig((prev) => ({ ...prev, [key]: value }));
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
                {activeBackend.config_params.map((p) => (
                  <ConfigField
                    key={p.key}
                    param={p}
                    value={backendConfig[p.key] ?? p.default}
                    onChange={handleBackendConfigChange}
                  />
                ))}
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

                <SharePanel
                  label="Users"
                  items={mailbox.shared_with_users}
                  input={newUserShare}
                  setInput={setNewUserShare}
                  onAdd={(v) => shareUser.mutate(v)}
                  onRemove={(v) => unshareUser.mutate(v)}
                  placeholder="user_id"
                />
                <SharePanel
                  label="Roles"
                  items={mailbox.shared_with_roles}
                  input={newRoleShare}
                  setInput={setNewRoleShare}
                  onAdd={(v) => shareRole.mutate(v)}
                  onRemove={(v) => unshareRole.mutate(v)}
                  placeholder="role name"
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

function SharePanel({
  label, items, input, setInput, onAdd, onRemove, placeholder,
}: {
  label: string;
  items: string[];
  input: string;
  setInput: (v: string) => void;
  onAdd: (v: string) => void;
  onRemove: (v: string) => void;
  placeholder: string;
}) {
  const submit = () => {
    const v = input.trim();
    if (v) onAdd(v);
  };
  return (
    <div className="space-y-1.5">
      <Label className="text-xs">{label}</Label>
      {items.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {items.map((v) => (
            <span
              key={v}
              className="inline-flex items-center gap-1 rounded-md bg-muted px-2 py-0.5 text-xs"
            >
              {v}
              <button
                type="button"
                onClick={() => onRemove(v)}
                className="text-muted-foreground hover:text-foreground"
              >
                <XIcon className="size-3" />
              </button>
            </span>
          ))}
        </div>
      )}
      <div className="flex gap-1.5">
        <Input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              submit();
            }
          }}
          placeholder={placeholder}
          className="text-sm"
        />
        <Button variant="outline" size="sm" onClick={submit} disabled={!input.trim()}>
          <PlusIcon className="size-3.5" />
        </Button>
      </div>
    </div>
  );
}
