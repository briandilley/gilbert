import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import type { CalendarAccount } from "@/types/calendar";
import { Calendar, Plus, Settings } from "lucide-react";

interface Props {
  accounts: CalendarAccount[];
  selectedAccountId: string | null;
  onSelect: (id: string | null) => void;
  onAddAccount: () => void;
  onEditAccount: (account: CalendarAccount) => void;
  loading?: boolean;
}

/** Sidebar listing accessible calendar accounts plus an "All" pseudo-row. */
export function CalendarSidebar({
  accounts,
  selectedAccountId,
  onSelect,
  onAddAccount,
  onEditAccount,
  loading,
}: Props) {
  return (
    <aside className="w-64 border-r overflow-y-auto bg-muted/20">
      <div className="p-4">
        <h2 className="text-lg font-semibold mb-3">Calendars</h2>
        <Button variant="outline" size="sm" className="w-full" onClick={onAddAccount}>
          <Plus className="h-4 w-4 mr-1" /> Add account
        </Button>
      </div>
      <nav className="px-2 pb-4 space-y-1">
        <button
          onClick={() => onSelect(null)}
          className={`w-full flex items-center gap-2 px-3 py-2 rounded-md text-sm text-left ${
            selectedAccountId === null ? "bg-accent text-accent-foreground" : "hover:bg-accent/50"
          }`}
        >
          <Calendar className="h-4 w-4" />
          <span>All calendars</span>
        </button>
        {loading && (
          <div className="px-3 py-2 text-xs text-muted-foreground">Loading…</div>
        )}
        {accounts.map((a) => (
          <div key={a.id} className="flex items-center gap-1">
            <button
              onClick={() => onSelect(a.id)}
              className={`flex-1 flex items-center gap-2 px-3 py-2 rounded-md text-sm text-left ${
                selectedAccountId === a.id
                  ? "bg-accent text-accent-foreground"
                  : "hover:bg-accent/50"
              }`}
            >
              <Calendar className="h-4 w-4" />
              <div className="flex-1 min-w-0">
                <div className="truncate">{a.name}</div>
                <div className="text-xs text-muted-foreground truncate">
                  {a.email_address}
                </div>
              </div>
              {a.health !== "ok" && (
                <Badge variant="destructive" className="text-xs">!</Badge>
              )}
            </button>
            {a.can_admin && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  onEditAccount(a);
                }}
                className="p-1 rounded hover:bg-accent/50"
                title="Edit account"
              >
                <Settings className="h-3 w-3" />
              </button>
            )}
          </div>
        ))}
        {!loading && accounts.length === 0 && (
          <div className="px-3 py-4 text-sm text-muted-foreground">
            No calendar accounts yet. Click "Add account" to get started.
          </div>
        )}
      </nav>
    </aside>
  );
}

