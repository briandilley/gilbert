import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "@/hooks/useAuth";
import { changePassword, revokeAllSessions } from "@/api/auth";
import {
  type UserMemory,
  clearMemories,
  deleteMemory,
  getMemoryOptOut,
  listMemories,
  setMemoryOptOut,
} from "@/api/account";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { PluginPanelSlot } from "@/components/PluginPanelSlot";

export function AccountPage() {
  const { user } = useAuth();

  return (
    <div className="p-4 sm:p-6 space-y-4 max-w-2xl mx-auto">
      <h1 className="text-xl sm:text-2xl font-semibold">Account</h1>

      <Card>
        <CardHeader>
          <CardTitle>Profile</CardTitle>
        </CardHeader>
        <CardContent className="text-sm space-y-1">
          <div>
            <span className="text-muted-foreground">Name:</span>{" "}
            {user?.display_name || "—"}
          </div>
          <div>
            <span className="text-muted-foreground">Email:</span>{" "}
            {user?.email || "—"}
          </div>
          <div>
            <span className="text-muted-foreground">Sign-in method:</span>{" "}
            {user?.provider || "—"}
          </div>
        </CardContent>
      </Card>

      {user?.has_password && <ChangePasswordCard />}

      <MemoriesCard />

      {/* Per-user plugin panels — anything a plugin declares with
          ``slot="account.extensions"`` mounts here. Core has zero
          knowledge of which plugins (browser credentials, future
          OAuth tokens, etc.) end up in this slot. */}
      <PluginPanelSlot slot="account.extensions" />

      <RevokeAllSessionsCard />
    </div>
  );
}

function ChangePasswordCard() {
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [success, setSuccess] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setSuccess(false);
    if (newPassword !== confirmPassword) {
      setError("New password and confirmation don't match.");
      return;
    }
    setSubmitting(true);
    try {
      await changePassword(oldPassword, newPassword);
      setSuccess(true);
      setOldPassword("");
      setNewPassword("");
      setConfirmPassword("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not change password");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Change password</CardTitle>
        <CardDescription>
          Other devices you're signed in on will be signed out. This device
          stays signed in.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} className="space-y-3">
          {error && (
            <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {error}
            </div>
          )}
          {success && (
            <div className="rounded-md bg-green-500/10 px-3 py-2 text-sm text-green-700 dark:text-green-400">
              Password changed. Other sessions have been signed out.
            </div>
          )}
          <div className="space-y-1.5">
            <Label htmlFor="old-password">Current password</Label>
            <Input
              id="old-password"
              type="password"
              autoComplete="current-password"
              value={oldPassword}
              onChange={(e) => setOldPassword(e.target.value)}
              required
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="new-password">New password</Label>
            <Input
              id="new-password"
              type="password"
              autoComplete="new-password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              minLength={8}
              required
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="confirm-password">Confirm new password</Label>
            <Input
              id="confirm-password"
              type="password"
              autoComplete="new-password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              minLength={8}
              required
            />
          </div>
          <Button type="submit" disabled={submitting}>
            {submitting ? "Saving…" : "Change password"}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

function RevokeAllSessionsCard() {
  const navigate = useNavigate();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  async function handleRevoke() {
    setError("");
    setSubmitting(true);
    try {
      await revokeAllSessions();
      // The server cleared our cookie too; bounce to the login page.
      navigate("/auth/login", { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not sign out");
      setSubmitting(false);
    }
  }

  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle>Sign out everywhere</CardTitle>
          <CardDescription>
            Sign out of every device and browser where this account is
            currently logged in — including this one.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {error && (
            <div className="mb-3 rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {error}
            </div>
          )}
          <Button
            variant="destructive"
            onClick={() => setConfirmOpen(true)}
            disabled={submitting}
          >
            Sign out everywhere
          </Button>
        </CardContent>
      </Card>

      <Dialog open={confirmOpen} onOpenChange={(o) => !submitting && setConfirmOpen(o)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Sign out everywhere?</DialogTitle>
            <DialogDescription>
              You'll be signed out on every device, including this one, and
              taken to the login page.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setConfirmOpen(false)}
              disabled={submitting}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={handleRevoke}
              disabled={submitting}
            >
              {submitting ? "Signing out…" : "Sign out everywhere"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

function MemoriesCard() {
  const [memories, setMemories] = useState<UserMemory[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [optedOut, setOptedOut] = useState<boolean | null>(null);
  const [optOutBusy, setOptOutBusy] = useState(false);
  const [clearOpen, setClearOpen] = useState(false);
  const [clearing, setClearing] = useState(false);

  async function refresh() {
    try {
      const [list, optOut] = await Promise.all([
        listMemories(),
        getMemoryOptOut(),
      ]);
      setMemories(list);
      setOptedOut(optOut.opted_out);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load memories");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  async function handleDelete(memoryId: string) {
    try {
      await deleteMemory(memoryId);
      setMemories((curr) => curr?.filter((m) => m.memory_id !== memoryId) ?? null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not delete memory");
    }
  }

  async function handleClear() {
    setClearing(true);
    try {
      await clearMemories();
      setMemories([]);
      setClearOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not clear memories");
    } finally {
      setClearing(false);
    }
  }

  async function handleToggleOptOut() {
    if (optedOut === null) return;
    setOptOutBusy(true);
    try {
      await setMemoryOptOut(!optedOut);
      setOptedOut(!optedOut);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not update setting");
    } finally {
      setOptOutBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>What Gilbert remembers about you</CardTitle>
        <CardDescription>
          After a chat ends or sits idle, Gilbert may save short notes
          about your preferences and style so future conversations feel
          more natural. You can delete any of these at any time.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {error && (
          <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {error}
          </div>
        )}

        {loading ? (
          <div className="text-sm text-muted-foreground">Loading…</div>
        ) : memories && memories.length > 0 ? (
          <ul className="space-y-2">
            {memories.map((m) => (
              <li
                key={m.memory_id}
                className="rounded-md border bg-muted/30 px-3 py-2 text-sm"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="flex-1 min-w-0">
                    <div className="font-medium">{m.summary}</div>
                    {m.content && m.content !== m.summary && (
                      <div className="text-muted-foreground text-xs mt-0.5">
                        {m.content}
                      </div>
                    )}
                    <div className="text-muted-foreground text-xs mt-1">
                      {m.source === "auto" ? "Auto-captured" : "Saved by you"}
                      {" · "}
                      {new Date(m.updated_at).toLocaleDateString()}
                    </div>
                  </div>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => handleDelete(m.memory_id)}
                  >
                    Delete
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <div className="text-sm text-muted-foreground">
            No memories saved yet.
          </div>
        )}

        <div className="flex flex-wrap gap-2 pt-2">
          {memories && memories.length > 0 && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => setClearOpen(true)}
            >
              Delete all
            </Button>
          )}
          {optedOut !== null && (
            <Button
              variant={optedOut ? "default" : "outline"}
              size="sm"
              onClick={handleToggleOptOut}
              disabled={optOutBusy}
            >
              {optedOut
                ? "Resume auto-capture"
                : "Stop auto-capturing memories"}
            </Button>
          )}
        </div>
      </CardContent>

      <Dialog open={clearOpen} onOpenChange={(o) => !clearing && setClearOpen(o)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete all memories?</DialogTitle>
            <DialogDescription>
              This removes everything Gilbert has remembered about you,
              including notes you saved manually. Auto-capture stays
              {" "}
              {optedOut ? "off" : "on"} unless you change it above.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setClearOpen(false)}
              disabled={clearing}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={handleClear}
              disabled={clearing}
            >
              {clearing ? "Deleting…" : "Delete all"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
