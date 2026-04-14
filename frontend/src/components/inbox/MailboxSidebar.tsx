import { InboxIcon, PlusIcon, UsersIcon, ShieldIcon, GlobeIcon } from "lucide-react";
import type { InboxMailbox, MailboxAccess } from "@/types/inbox";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";

interface MailboxSidebarProps {
  mailboxes: InboxMailbox[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onCreate: () => void;
  isAdmin: boolean;
}

/** Groups a flat list of mailboxes into Mine / Shared with me / All.
 *
 * The backend returns an ``access`` tag on every mailbox (owner / admin /
 * shared_user / shared_role). We group by that tag:
 *   - owner  → "Mine"
 *   - shared_* → "Shared with me"
 *   - admin (and not owner) → "All" (admins only)
 */
function groupMailboxes(
  mailboxes: InboxMailbox[], isAdmin: boolean,
): Record<string, InboxMailbox[]> {
  const groups: Record<string, InboxMailbox[]> = {
    mine: [],
    shared: [],
    all: [],
  };
  for (const m of mailboxes) {
    if (m.access === "owner") {
      groups.mine.push(m);
    } else if (m.access === "shared_user" || m.access === "shared_role") {
      groups.shared.push(m);
    } else if (m.access === "admin" && isAdmin) {
      groups.all.push(m);
    }
  }
  return groups;
}

const ACCESS_ICON: Record<MailboxAccess, typeof UsersIcon> = {
  owner: InboxIcon,
  shared_user: UsersIcon,
  shared_role: ShieldIcon,
  admin: GlobeIcon,
};

export function MailboxSidebar({
  mailboxes, selectedId, onSelect, onCreate, isAdmin,
}: MailboxSidebarProps) {
  const groups = groupMailboxes(mailboxes, isAdmin);

  return (
    <aside className="flex h-full w-full shrink-0 flex-col border-r bg-muted/20 sm:w-64">
      <div className="flex items-center justify-between px-3 py-3 border-b">
        <h2 className="text-sm font-semibold">Mailboxes</h2>
        <Button
          variant="ghost"
          size="icon-sm"
          title="Add mailbox"
          onClick={onCreate}
        >
          <PlusIcon className="size-4" />
        </Button>
      </div>

      <div className="flex-1 overflow-y-auto py-2">
        {mailboxes.length === 0 && (
          <div className="px-3 py-4 text-xs text-muted-foreground">
            No mailboxes yet.
            <br />
            Click <PlusIcon className="inline size-3" /> to add one.
          </div>
        )}

        {groups.mine.length > 0 && (
          <SidebarGroup
            label="Mine"
            mailboxes={groups.mine}
            selectedId={selectedId}
            onSelect={onSelect}
          />
        )}

        {groups.shared.length > 0 && (
          <SidebarGroup
            label="Shared with me"
            mailboxes={groups.shared}
            selectedId={selectedId}
            onSelect={onSelect}
          />
        )}

        {isAdmin && groups.all.length > 0 && (
          <SidebarGroup
            label="All (admin)"
            mailboxes={groups.all}
            selectedId={selectedId}
            onSelect={onSelect}
          />
        )}
      </div>
    </aside>
  );
}

function SidebarGroup({
  label, mailboxes, selectedId, onSelect,
}: {
  label: string;
  mailboxes: InboxMailbox[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <div className="mb-3">
      <div className="px-3 pb-1 text-[10px] uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
      <ul>
        {mailboxes.map((m) => {
          const Icon = m.access ? ACCESS_ICON[m.access] : InboxIcon;
          const active = m.id === selectedId;
          return (
            <li key={m.id}>
              <button
                type="button"
                onClick={() => onSelect(m.id)}
                className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm transition-colors ${
                  active
                    ? "bg-accent text-foreground"
                    : "hover:bg-accent/50 text-foreground/80"
                }`}
              >
                <Icon className="size-4 shrink-0 text-muted-foreground" />
                <span className="min-w-0 flex-1 truncate">{m.name}</span>
                {m.can_admin && (
                  <Badge variant="outline" className="h-4 px-1 text-[9px]">
                    admin
                  </Badge>
                )}
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
