/**
 * BrowserCredentialsPanel — list, add, edit, delete browser-plugin
 * credentials. Per CLAUDE.md, passwords are entered through plain
 * <Input type="password"> fields, never via JSON paste.
 */

import { useCallback, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  PlusIcon,
  Trash2Icon,
  PencilIcon,
  KeyRoundIcon,
  MonitorIcon,
} from "lucide-react";
import type {
  BrowserCredential,
  BrowserCredentialDraft,
} from "@/types/browser";
import { BrowserVncSessionDialog } from "./BrowserVncSessionDialog";

const EMPTY_DRAFT: BrowserCredentialDraft = {
  site: "",
  label: "",
  username: "",
  password: "",
  login_url: "",
};

export function BrowserCredentialsPanel() {
  const api = useWsApi();
  const { connected } = useWebSocket();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState<BrowserCredentialDraft | null>(null);
  const [vncCredentialId, setVncCredentialId] = useState<string | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["browser", "credentials"],
    queryFn: api.listBrowserCredentials,
    enabled: connected,
  });
  const credentials: BrowserCredential[] = data?.credentials ?? [];

  const refresh = useCallback(
    () => queryClient.invalidateQueries({ queryKey: ["browser"] }),
    [queryClient],
  );

  const handleSave = useCallback(
    async (draft: BrowserCredentialDraft) => {
      await api.saveBrowserCredential(draft);
      await refresh();
      setEditing(null);
    },
    [api, refresh],
  );

  const handleDelete = useCallback(
    async (cred: BrowserCredential) => {
      if (
        !confirm(
          `Delete credential for ${cred.site} (${cred.username || cred.label})?`,
        )
      ) {
        return;
      }
      await api.deleteBrowserCredential(cred.id);
      await refresh();
    },
    [api, refresh],
  );

  return (
    <div className="rounded-md border p-4 sm:p-6">
      <div className="flex items-start justify-between gap-3 mb-4">
        <div>
          <h2 className="text-lg font-semibold flex items-center gap-2">
            <KeyRoundIcon className="size-4" /> Saved logins
          </h2>
          <p className="text-sm text-muted-foreground mt-1">
            Used by the agent's <code>browser_login</code> tool. Passwords
            are encrypted at rest with a per-installation key.
          </p>
        </div>
        <Button size="sm" onClick={() => setEditing({ ...EMPTY_DRAFT })}>
          <PlusIcon className="size-4 mr-1" /> Add
        </Button>
      </div>

      {isLoading ? (
        <div className="text-sm text-muted-foreground py-6 text-center">
          Loading…
        </div>
      ) : credentials.length === 0 ? (
        <div className="text-sm text-muted-foreground py-6 text-center">
          No credentials saved yet.
        </div>
      ) : (
        <div className="rounded-md border divide-y">
          {credentials.map((cred) => (
            <div
              key={cred.id}
              className="flex items-center gap-3 px-3 py-2 text-sm"
            >
              <div className="flex-1 min-w-0">
                <div className="font-medium truncate">
                  {cred.label || cred.site}
                </div>
                <div className="text-xs text-muted-foreground truncate">
                  {cred.site} · {cred.username || "(no username)"}
                </div>
              </div>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setVncCredentialId(cred.id)}
                title="Log in interactively via VNC"
              >
                <MonitorIcon className="size-4" />
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() =>
                  setEditing({
                    credential_id: cred.id,
                    site: cred.site,
                    label: cred.label,
                    username: cred.username,
                    password: "",
                    login_url: cred.login_url,
                  })
                }
                title="Edit"
              >
                <PencilIcon className="size-4" />
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => handleDelete(cred)}
                title="Delete"
              >
                <Trash2Icon className="size-4" />
              </Button>
            </div>
          ))}
        </div>
      )}

      {editing && (
        <CredentialDialog
          draft={editing}
          isUpdate={!!editing.credential_id}
          onClose={() => setEditing(null)}
          onSave={handleSave}
        />
      )}
      {vncCredentialId && (
        <BrowserVncSessionDialog
          credentialId={vncCredentialId}
          onClose={() => {
            setVncCredentialId(null);
            refresh();
          }}
        />
      )}
    </div>
  );
}

function CredentialDialog({
  draft,
  isUpdate,
  onClose,
  onSave,
}: {
  draft: BrowserCredentialDraft;
  isUpdate: boolean;
  onClose: () => void;
  onSave: (draft: BrowserCredentialDraft) => Promise<void>;
}) {
  const [form, setForm] = useState<BrowserCredentialDraft>(draft);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async () => {
    setBusy(true);
    setError(null);
    try {
      await onSave(form);
    } catch (e) {
      setError((e as Error).message ?? "Save failed");
      setBusy(false);
    }
  };

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>
            {isUpdate ? "Edit credential" : "Add credential"}
          </DialogTitle>
        </DialogHeader>

        <div className="space-y-3">
          <div className="space-y-1">
            <Label htmlFor="site">Site (domain)</Label>
            <Input
              id="site"
              value={form.site}
              onChange={(e) => setForm({ ...form, site: e.target.value })}
              placeholder="example.com"
              autoFocus
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="label">Label</Label>
            <Input
              id="label"
              value={form.label}
              onChange={(e) => setForm({ ...form, label: e.target.value })}
              placeholder="Main account"
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="login_url">Login URL</Label>
            <Input
              id="login_url"
              value={form.login_url}
              onChange={(e) => setForm({ ...form, login_url: e.target.value })}
              placeholder="https://example.com/login"
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="username">Username / email</Label>
            <Input
              id="username"
              value={form.username}
              onChange={(e) => setForm({ ...form, username: e.target.value })}
              autoComplete="off"
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="password">
              Password{" "}
              {isUpdate ? (
                <span className="text-xs text-muted-foreground">
                  (leave blank to keep)
                </span>
              ) : null}
            </Label>
            <Input
              id="password"
              type="password"
              value={form.password ?? ""}
              onChange={(e) => setForm({ ...form, password: e.target.value })}
              autoComplete="new-password"
            />
          </div>

          <details className="rounded border p-2">
            <summary className="text-xs cursor-pointer text-muted-foreground">
              Advanced — login form selectors (optional)
            </summary>
            <div className="space-y-2 mt-2">
              <div className="space-y-1">
                <Label htmlFor="user_sel">Username selector</Label>
                <Input
                  id="user_sel"
                  value={form.username_selector ?? ""}
                  onChange={(e) =>
                    setForm({ ...form, username_selector: e.target.value })
                  }
                  placeholder="#email"
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="pw_sel">Password selector</Label>
                <Input
                  id="pw_sel"
                  value={form.password_selector ?? ""}
                  onChange={(e) =>
                    setForm({ ...form, password_selector: e.target.value })
                  }
                  placeholder="#password"
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="submit_sel">Submit selector</Label>
                <Input
                  id="submit_sel"
                  value={form.submit_selector ?? ""}
                  onChange={(e) =>
                    setForm({ ...form, submit_selector: e.target.value })
                  }
                  placeholder="button[type=submit]"
                />
              </div>
            </div>
          </details>
        </div>

        {error ? (
          <div className="text-sm text-red-600">{error}</div>
        ) : null}

        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button
            onClick={handleSubmit}
            disabled={busy || !form.site || !form.username}
          >
            {busy ? "Saving…" : "Save"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
