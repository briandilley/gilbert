import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CalendarSidebar } from "./CalendarSidebar";
import { WeekAgenda } from "./WeekAgenda";
import { AccountEditDrawer } from "./AccountEditDrawer";
import { CreateEventDrawer } from "./CreateEventDrawer";
import type { CalendarAccount } from "@/types/calendar";
import { Plus, RefreshCw, Settings } from "lucide-react";

/** Multi-account calendar page.
 *
 * Layout: sidebar of accessible accounts on the left, weekly agenda
 * on the right. The sidebar's selected account narrows the agenda; an
 * "All" pseudo-row aggregates across every accessible account.
 */
export function CalendarPage() {
  const api = useWsApi();
  const queryClient = useQueryClient();
  const [selectedAccountId, setSelectedAccountId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [createDefaultDate, setCreateDefaultDate] = useState<Date | null>(null);
  const [editAccount, setEditAccount] = useState<CalendarAccount | null>(null);
  const [creatingAccount, setCreatingAccount] = useState(false);
  const [weekStart, setWeekStart] = useState<Date>(() => {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    d.setDate(d.getDate() - d.getDay()); // Sunday
    return d;
  });

  const accountsQuery = useQuery({
    queryKey: ["calendar.accounts"],
    queryFn: () => api.listCalendarAccounts(),
  });

  const accounts = useMemo(() => accountsQuery.data ?? [], [accountsQuery.data]);
  const selectedAccount = useMemo(
    () =>
      selectedAccountId
        ? accounts.find((a) => a.id === selectedAccountId) ?? null
        : null,
    [accounts, selectedAccountId],
  );

  const weekEnd = useMemo(() => {
    const d = new Date(weekStart);
    d.setDate(d.getDate() + 7);
    return d;
  }, [weekStart]);

  const eventsQuery = useQuery({
    queryKey: [
      "calendar.events",
      selectedAccountId,
      weekStart.toISOString(),
      weekEnd.toISOString(),
    ],
    enabled: accounts.length > 0,
    queryFn: () =>
      api.listCalendarEvents({
        time_min: weekStart.toISOString(),
        time_max: weekEnd.toISOString(),
        account_id: selectedAccountId,
        max_results: 250,
      }),
  });

  const deleteEvent = useMutation({
    mutationFn: ({
      accountId,
      eventId,
    }: {
      accountId: string;
      eventId: string;
    }) => api.deleteCalendarEvent(accountId, eventId, false),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["calendar.events"] }),
  });

  const stepWeek = (delta: number) => {
    const d = new Date(weekStart);
    d.setDate(d.getDate() + delta * 7);
    setWeekStart(d);
  };

  return (
    <div className="flex h-full overflow-hidden">
      <CalendarSidebar
        accounts={accounts}
        selectedAccountId={selectedAccountId}
        onSelect={setSelectedAccountId}
        onAddAccount={() => setCreatingAccount(true)}
        onEditAccount={(a) => setEditAccount(a)}
        loading={accountsQuery.isLoading}
      />
      <main className="flex-1 overflow-y-auto p-6 space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-semibold">
              {selectedAccount ? selectedAccount.name : "All calendars"}
            </h1>
            {selectedAccount && (
              <p className="text-sm text-muted-foreground">
                {selectedAccount.email_address} · {selectedAccount.timezone}{" "}
                {selectedAccount.health !== "ok" && (
                  <Badge variant="destructive" className="ml-2">
                    {selectedAccount.health}
                  </Badge>
                )}
              </p>
            )}
          </div>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="icon"
              onClick={() =>
                queryClient.invalidateQueries({
                  queryKey: ["calendar.events"],
                })
              }
              title="Refresh"
            >
              <RefreshCw className="h-4 w-4" />
            </Button>
            {selectedAccount && selectedAccount.can_admin && (
              <Button
                variant="outline"
                size="icon"
                onClick={() => setEditAccount(selectedAccount)}
                title="Edit account"
              >
                <Settings className="h-4 w-4" />
              </Button>
            )}
            <Button
              onClick={() => {
                setCreateDefaultDate(null);
                setCreateOpen(true);
              }}
              disabled={accounts.length === 0}
            >
              <Plus className="h-4 w-4 mr-1" /> New event
            </Button>
          </div>
        </div>

        {eventsQuery.data?.warnings?.length ? (
          <Card>
            <CardHeader>
              <CardTitle>Warnings</CardTitle>
            </CardHeader>
            <CardContent>
              <ul className="text-sm text-muted-foreground space-y-1">
                {eventsQuery.data.warnings.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            </CardContent>
          </Card>
        ) : null}

        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => stepWeek(-1)}>
            ← Prev week
          </Button>
          <span className="text-sm">
            {weekStart.toLocaleDateString()} —{" "}
            {new Date(weekEnd.getTime() - 1).toLocaleDateString()}
          </span>
          <Button variant="outline" size="sm" onClick={() => stepWeek(1)}>
            Next week →
          </Button>
        </div>

        <WeekAgenda
          weekStart={weekStart}
          events={eventsQuery.data?.events ?? []}
          loading={eventsQuery.isLoading}
          onDeleteEvent={(accountId, eventId) =>
            deleteEvent.mutate({ accountId, eventId })
          }
          canCreateEvent={accounts.length > 0}
          onCreateEvent={(date) => {
            setCreateDefaultDate(date);
            setCreateOpen(true);
          }}
        />
      </main>

      {createOpen && (
        <CreateEventDrawer
          accounts={accounts}
          defaultAccountId={selectedAccountId}
          defaultStartDate={createDefaultDate}
          onClose={() => {
            setCreateOpen(false);
            setCreateDefaultDate(null);
          }}
          onCreated={() =>
            queryClient.invalidateQueries({ queryKey: ["calendar.events"] })
          }
        />
      )}
      {(editAccount !== null || creatingAccount) && (
        <AccountEditDrawer
          account={editAccount}
          onClose={() => {
            setEditAccount(null);
            setCreatingAccount(false);
          }}
          onSaved={() => {
            queryClient.invalidateQueries({ queryKey: ["calendar.accounts"] });
            queryClient.invalidateQueries({ queryKey: ["calendar.events"] });
          }}
        />
      )}
    </div>
  );
}
