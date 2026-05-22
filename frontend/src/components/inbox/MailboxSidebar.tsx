import { InboxIcon, PlusIcon, UsersIcon, ShieldIcon, GlobeIcon } from "lucide-react";
import type { InboxMailbox, MailboxAccess } from "@/types/inbox";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

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

/**
 * Mailbox list rendered into the global SideNav via usePageSidebar.
 *
 * Layout matches the contextual side-nav vocabulary — eyebrow-styled
 * group labels (uppercase mono), 32px list rows, signal-color
 * accent bar on the active row.
 */
export function MailboxSidebar({
  mailboxes, selectedId, onSelect, onCreate, isAdmin,
}: MailboxSidebarProps) {
  const groups = groupMailboxes(mailboxes, isAdmin);

  return (
    <div className="flex h-full w-full flex-col">
      <div className="flex items-center justify-between gap-2 px-4 py-3 border-b border-border">
        <h2 className="text-[15px] font-semibold tracking-[-0.01em]">
          Mailboxes
        </h2>
        <Button
          variant="ghost"
          size="icon-xs"
          title="Add mailbox"
          onClick={onCreate}
        >
          <PlusIcon />
        </Button>
      </div>

      <div className="flex-1 overflow-y-auto py-2">
        {mailboxes.length === 0 && (
          <div className="px-4 py-6 text-center">
            <p className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-muted-foreground">
              No mailboxes
            </p>
            <p className="mt-1.5 text-xs text-muted-foreground">
              Click the + above to add one.
            </p>
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
    </div>
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
    <div className="mb-3 last:mb-0">
      <div className="px-4 pb-1 font-mono text-[11px] uppercase tracking-[0.08em] font-medium text-muted-foreground">
        {label}
      </div>
      <ul className="px-2">
        {mailboxes.map((m) => {
          const Icon = m.access ? ACCESS_ICON[m.access] : InboxIcon;
          const active = m.id === selectedId;
          return (
            <li key={m.id}>
              <button
                type="button"
                onClick={() => onSelect(m.id)}
                className={cn(
                  "group relative flex w-full items-center gap-2",
                  "h-8 px-2 rounded-md text-sm leading-none text-left",
                  "transition-[background-color,color] duration-(--duration-fast) ease-(--ease-out)",
                  active
                    ? "bg-foreground/8 text-foreground font-medium"
                    : "text-foreground/80 hover:bg-foreground/5 hover:text-foreground",
                )}
              >
                {active && (
                  <span
                    aria-hidden
                    className="absolute left-0 top-1.5 bottom-1.5 w-[2px] rounded-r-full bg-(--signal)"
                  />
                )}
                <Icon className="size-3.5 shrink-0 text-muted-foreground" />
                <span className="min-w-0 flex-1 truncate">{m.name}</span>
                {m.can_admin && (
                  <Badge variant="neutral" className="text-[9.5px]">
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
