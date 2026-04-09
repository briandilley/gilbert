import { useState, useEffect, useMemo } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";

interface InviteUser {
  user_id: string;
  display_name: string;
}

interface InviteModalProps {
  open: boolean;
  users: InviteUser[];
  existingMemberIds: string[];
  pendingInviteIds: string[];
  currentUserId?: string;
  loading?: boolean;
  onInvite: (invited: InviteUser[], revoked: string[]) => void;
  onCancel: () => void;
}

export function InviteModal({
  open,
  users,
  existingMemberIds,
  pendingInviteIds,
  currentUserId,
  loading,
  onInvite,
  onCancel,
}: InviteModalProps) {
  const [filter, setFilter] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const pendingSet = useMemo(() => new Set(pendingInviteIds), [pendingInviteIds]);

  useEffect(() => {
    if (open) {
      setFilter("");
      setSelected(new Set(pendingInviteIds));
    }
  }, [open, pendingInviteIds]);

  const memberSet = useMemo(() => new Set(existingMemberIds), [existingMemberIds]);

  const filteredUsers = useMemo(() => {
    const lowerFilter = filter.toLowerCase();
    return users.filter(
      (u) =>
        !memberSet.has(u.user_id) &&
        u.user_id !== currentUserId &&
        (u.display_name.toLowerCase().includes(lowerFilter) ||
          u.user_id.toLowerCase().includes(lowerFilter)),
    );
  }, [users, memberSet, currentUserId, filter]);

  function toggleUser(userId: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(userId)) {
        next.delete(userId);
      } else {
        next.add(userId);
      }
      return next;
    });
  }

  function handleSubmit() {
    const newInvites = users.filter(
      (u) => selected.has(u.user_id) && !pendingSet.has(u.user_id),
    );
    const revoked = pendingInviteIds.filter((id) => !selected.has(id));
    onInvite(newInvites, revoked);
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onCancel()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Invite Users</DialogTitle>
        </DialogHeader>
        <Input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter by name..."
          autoFocus
        />
        <ScrollArea className="max-h-64 -mx-1">
          <div className="space-y-0.5 px-1">
            {loading ? (
              <p className="text-sm text-muted-foreground py-4 text-center">
                Loading users...
              </p>
            ) : filteredUsers.length === 0 ? (
              <p className="text-sm text-muted-foreground py-4 text-center">
                {filter ? "No matching users" : "No users available to invite"}
              </p>
            ) : (
              filteredUsers.map((u) => (
                <label
                  key={u.user_id}
                  className="flex items-center gap-2.5 rounded-lg px-2 py-1.5 cursor-pointer hover:bg-accent transition-colors"
                >
                  <input
                    type="checkbox"
                    checked={selected.has(u.user_id)}
                    onChange={() => toggleUser(u.user_id)}
                    className="rounded border-input"
                  />
                  <Avatar className="size-6">
                    <AvatarFallback className="text-[10px]">
                      {u.display_name.charAt(0).toUpperCase()}
                    </AvatarFallback>
                  </Avatar>
                  <span className="text-sm truncate">{u.display_name}</span>
                </label>
              ))
            )}
          </div>
        </ScrollArea>
        <DialogFooter>
          <Button variant="outline" onClick={onCancel}>
            Cancel
          </Button>
          <Button onClick={handleSubmit}>
            Save
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
