import { useCallback, useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { useAuth } from "@/hooks/useAuth";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { MailboxSidebar } from "./MailboxSidebar";
import { MailboxEditor } from "./MailboxEditor";
import { MessageList } from "./MessageList";
import { MessageDetailDialog } from "./MessageDetailDialog";
import { OutboxPanel } from "./OutboxPanel";
import type { InboxMailbox, InboxMessage } from "@/types/inbox";
import { SettingsIcon } from "lucide-react";

/** Multi-mailbox inbox page.
 *
 * Layout:
 *   ┌──────────┬────────────────────────────────┐
 *   │ Sidebar  │ Header (name · email · edit)    │
 *   │ Mine     │ Outbox panel (if active drafts) │
 *   │ Shared   │ Search + message list           │
 *   │ All      │                                 │
 *   └──────────┴────────────────────────────────┘
 *
 * URL state:
 *   ?mbx=<id>     — selected mailbox (persisted so reloads land in the same place)
 *   ?msg=<id>     — opened message dialog
 *   ?sender=...   — sender filter
 *   ?subject=...  — subject filter
 *
 * Event subscriptions:
 *   inbox.mailbox.*          → refetch mailboxes
 *   inbox.mailbox.shares.changed → refetch (access set may change)
 *   auth.user.roles.changed  → refetch if the event targets the current user
 *     (role membership changes can open/close access to role-shared mailboxes)
 *   inbox.message.received   → refetch messages + stats for the affected mailbox
 *   inbox.outbox.*           → refetch outbox for the affected mailbox
 */
export function InboxPage() {
  const api = useWsApi();
  const { connected, subscribe } = useWebSocket();
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const [editorOpen, setEditorOpen] = useState(false);
  const [editorMailbox, setEditorMailbox] = useState<InboxMailbox | null>(null);

  const isAdmin = useMemo(
    () => Boolean(user?.roles?.includes("admin")),
    [user],
  );

  // URL state
  const selectedId = searchParams.get("mbx");
  const selectedMessageId = searchParams.get("msg");
  const sender = searchParams.get("sender") || "";
  const subject = searchParams.get("subject") || "";

  const updateParam = useCallback(
    (key: string, value: string | null) => {
      const p = new URLSearchParams(searchParams);
      if (value) p.set(key, value);
      else p.delete(key);
      setSearchParams(p, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  // ---- Data ----

  const { data: mailboxes = [] } = useQuery({
    queryKey: ["inbox-mailboxes"],
    queryFn: api.listMailboxes,
    enabled: connected,
  });

  const selectedMailbox = useMemo(
    () => mailboxes.find((m) => m.id === selectedId) ?? null,
    [mailboxes, selectedId],
  );

  // Default-select the first mailbox if nothing is selected and we have any.
  useEffect(() => {
    if (!selectedId && mailboxes.length > 0) {
      updateParam("mbx", mailboxes[0].id);
    }
  }, [mailboxes, selectedId, updateParam]);

  const { data: stats } = useQuery({
    queryKey: ["inbox-stats", selectedId],
    queryFn: () => api.inboxStats(selectedId ?? undefined),
    enabled: connected,
  });

  // ---- Event subscriptions ----

  useEffect(() => {
    const invalidateMailboxes = () =>
      queryClient.invalidateQueries({ queryKey: ["inbox-mailboxes"] });

    const invalidateOutbox = () =>
      queryClient.invalidateQueries({ queryKey: ["inbox-outbox"] });

    const invalidateMessages = () => {
      queryClient.invalidateQueries({ queryKey: ["inbox-messages"] });
      queryClient.invalidateQueries({ queryKey: ["inbox-stats"] });
    };

    const unsubs: Array<() => void> = [];
    unsubs.push(subscribe("inbox.mailbox.created", invalidateMailboxes));
    unsubs.push(subscribe("inbox.mailbox.updated", invalidateMailboxes));
    unsubs.push(subscribe("inbox.mailbox.deleted", invalidateMailboxes));
    unsubs.push(
      subscribe("inbox.mailbox.shares.changed", invalidateMailboxes),
    );
    unsubs.push(
      subscribe("auth.user.roles.changed", (evt) => {
        // Role changes can open/close access to role-shared mailboxes.
        // Only refetch when the affected user is the current user —
        // other users' role changes don't affect what we can see.
        if (evt.data?.user_id === user?.user_id) {
          invalidateMailboxes();
        }
      }),
    );
    unsubs.push(subscribe("inbox.message.received", invalidateMessages));
    unsubs.push(subscribe("inbox.outbox.sent", invalidateOutbox));
    unsubs.push(subscribe("inbox.outbox.failed", invalidateOutbox));

    return () => {
      unsubs.forEach((u) => u());
    };
  }, [subscribe, queryClient, user?.user_id]);

  // ---- Handlers ----

  const handleSelectMailbox = (id: string) => {
    updateParam("mbx", id);
    updateParam("msg", null);
    updateParam("sender", null);
    updateParam("subject", null);
  };

  const handleSelectMessage = (msg: InboxMessage) => {
    updateParam("msg", msg.message_id);
  };

  const handleCloseDetail = () => updateParam("msg", null);

  const handleOpenEditor = (mb: InboxMailbox | null) => {
    setEditorMailbox(mb);
    setEditorOpen(true);
  };

  return (
    <div className="flex h-[calc(100vh-3.5rem)] flex-col sm:flex-row">
      <MailboxSidebar
        mailboxes={mailboxes}
        selectedId={selectedId}
        onSelect={handleSelectMailbox}
        onCreate={() => handleOpenEditor(null)}
        isAdmin={isAdmin}
      />

      <main className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-5xl space-y-4 p-4 sm:p-6">
          {mailboxes.length === 0 ? (
            <EmptyState onCreate={() => handleOpenEditor(null)} />
          ) : (
            <>
              <MailboxHeader
                mailbox={selectedMailbox}
                stats={stats ?? null}
                onEdit={() => handleOpenEditor(selectedMailbox)}
              />

              <OutboxPanel mailboxId={selectedId} />

              <MessageList
                mailboxId={selectedId}
                sender={sender}
                subject={subject}
                onSenderChange={(v) => updateParam("sender", v || null)}
                onSubjectChange={(v) => updateParam("subject", v || null)}
                selectedMessageId={selectedMessageId}
                onSelectMessage={handleSelectMessage}
              />
            </>
          )}
        </div>
      </main>

      <MessageDetailDialog
        messageId={selectedMessageId}
        mailboxId={selectedId}
        onClose={handleCloseDetail}
      />

      <MailboxEditor
        mailbox={editorMailbox}
        open={editorOpen}
        onOpenChange={setEditorOpen}
      />
    </div>
  );
}

function MailboxHeader({
  mailbox, stats, onEdit,
}: {
  mailbox: InboxMailbox | null;
  stats: { total: number; inbound: number } | null;
  onEdit: () => void;
}) {
  if (!mailbox) {
    return (
      <div className="flex items-center gap-3">
        <h1 className="text-xl font-semibold sm:text-2xl">Inbox</h1>
        <span className="text-xs text-muted-foreground">Select a mailbox</span>
      </div>
    );
  }
  return (
    <div className="flex flex-wrap items-center gap-3">
      <h1 className="text-xl font-semibold sm:text-2xl">{mailbox.name}</h1>
      <span className="text-sm text-muted-foreground">{mailbox.email_address}</span>
      {stats && (
        <Badge variant="secondary">{stats.total} messages</Badge>
      )}
      {mailbox.access && mailbox.access !== "owner" && (
        <Badge variant="outline" className="capitalize">
          {mailbox.access.replace("_", " ")}
        </Badge>
      )}
      {mailbox.can_admin && (
        <Button
          variant="ghost"
          size="sm"
          className="ml-auto"
          onClick={onEdit}
        >
          <SettingsIcon className="size-3.5 mr-1.5" />
          Settings
        </Button>
      )}
    </div>
  );
}

function EmptyState({ onCreate }: { onCreate: () => void }) {
  return (
    <div className="flex flex-col items-center justify-center gap-4 rounded-lg border border-dashed py-20 text-center">
      <h2 className="text-lg font-semibold">No mailboxes yet</h2>
      <p className="max-w-md text-sm text-muted-foreground">
        Gilbert's inbox is multi-mailbox — every mailbox is owned by a user and
        can be shared with others by user or role. Create your first mailbox to
        start syncing mail.
      </p>
      <Button onClick={onCreate}>Create mailbox</Button>
    </div>
  );
}
