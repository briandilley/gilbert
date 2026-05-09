import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
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
import type { CalendarAccount } from "@/types/calendar";
import { Trash2Icon, AlertTriangleIcon } from "lucide-react";

interface Props {
  /** If null the drawer is in "create" mode. */
  account: CalendarAccount | null;
  onClose: () => void;
  onSaved: () => void;
}

/** Calendar account create/edit drawer.
 *
 * Two-phase create flow per the spec — credentials are saved first
 * with ``poll_enabled=false``, then the user clicks "Probe calendars"
 * to populate the calendar_id dropdown via
 * ``calendar.accounts.probe_calendars``, picks the right one, and
 * flips ``poll_enabled`` on.
 */
export function AccountEditDrawer({ account, onClose, onSaved }: Props) {
  const api = useWsApi();
  const isCreate = account === null;

  const backendsQuery = useQuery({
    queryKey: ["calendar.backends"],
    queryFn: () => api.listCalendarBackends(),
  });

  const [name, setName] = useState(account?.name ?? "");
  const [emailAddress, setEmailAddress] = useState(account?.email_address ?? "");
  const [backendName, setBackendName] = useState(
    account?.backend_name ?? "google_calendar",
  );
  const [backendConfig, setBackendConfig] = useState<Record<string, unknown>>(
    account?.backend_config ?? {},
  );
  const [calendarId, setCalendarId] = useState(account?.calendar_id ?? "primary");
  const [timezone, setTimezone] = useState(account?.timezone ?? "UTC");
  const [whStart, setWhStart] = useState(account?.working_hours_start_hour ?? 9);
  const [whEnd, setWhEnd] = useState(account?.working_hours_end_hour ?? 18);
  const [pollEnabled, setPollEnabled] = useState(account?.poll_enabled ?? false);
  const [pollInterval, setPollInterval] = useState(account?.poll_interval_sec ?? 300);
  const [probedCalendars, setProbedCalendars] = useState<
    { id: string; name: string; timezone: string; primary: boolean }[]
  >([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!account) return;
    setName(account.name);
    setEmailAddress(account.email_address);
    setBackendName(account.backend_name);
    setBackendConfig(account.backend_config);
    setCalendarId(account.calendar_id);
    setTimezone(account.timezone);
    setWhStart(account.working_hours_start_hour);
    setWhEnd(account.working_hours_end_hour);
    setPollEnabled(account.poll_enabled);
    setPollInterval(account.poll_interval_sec);

    // Sensitive ConfigParam values come back from the WS layer as
    // ``********``. Admins can see the live values via a separate
    // call (which logs an audit line server-side). Without this,
    // saving the form would clobber the real secret with the mask.
    if (account.can_admin) {
      api
        .revealCalendarBackendConfig(account.id)
        .then((cfg) => setBackendConfig(cfg))
        .catch(() => {
          /* Non-admins or transient failures: keep the masked values. */
        });
    }
  }, [account, api]);

  const selectedBackend = useMemo(
    () => backendsQuery.data?.find((b) => b.name === backendName),
    [backendsQuery.data, backendName],
  );

  const create = useMutation({
    mutationFn: () =>
      api.createCalendarAccount({
        name,
        email_address: emailAddress,
        backend_name: backendName,
        backend_config: backendConfig,
        calendar_id: calendarId,
        timezone,
        working_hours_start_hour: whStart,
        working_hours_end_hour: whEnd,
        poll_enabled: false, // probe-after-save flow
        poll_interval_sec: pollInterval,
      }),
    onSuccess: () => {
      onSaved();
    },
    onError: (e: Error) => setError(e.message || "Failed to create account"),
  });

  const update = useMutation({
    mutationFn: () =>
      api.updateCalendarAccount(account!.id, {
        name,
        email_address: emailAddress,
        backend_name: backendName,
        backend_config: backendConfig,
        calendar_id: calendarId,
        timezone,
        working_hours_start_hour: whStart,
        working_hours_end_hour: whEnd,
        poll_enabled: pollEnabled,
        poll_interval_sec: pollInterval,
      }),
    onSuccess: () => {
      onSaved();
      onClose();
    },
    onError: (e: Error) => setError(e.message || "Failed to update account"),
  });

  const deleteAccount = useMutation({
    mutationFn: () => api.deleteCalendarAccount(account!.id),
    onSuccess: () => {
      onSaved();
      onClose();
    },
  });

  const probe = useMutation({
    mutationFn: () => api.probeCalendarsForAccount(account!.id),
    onSuccess: (calendars) => {
      setProbedCalendars(calendars);
      // Default to the primary calendar if user hasn't picked one yet.
      const primary = calendars.find((c) => c.primary);
      if (primary && (!calendarId || calendarId === "primary")) {
        setCalendarId(primary.id);
      }
    },
    onError: (e: Error) =>
      setError(e.message || "Failed to probe calendars"),
  });

  const submit = () => {
    setError(null);
    if (!name || !emailAddress || !backendName) {
      setError("Name, email, and backend are required");
      return;
    }
    if (isCreate) {
      create.mutate();
    } else {
      update.mutate();
    }
  };

  return (
    <Sheet open onOpenChange={(open) => (!open ? onClose() : null)}>
      <SheetContent className="sm:max-w-lg overflow-y-auto">
        <SheetHeader>
          <SheetTitle>
            {isCreate ? "Add calendar account" : `Edit ${account!.name}`}
          </SheetTitle>
        </SheetHeader>

        <div className="space-y-4 py-4">
          <div className="space-y-2">
            <Label>Name</Label>
            <Input value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="space-y-2">
            <Label>Email address</Label>
            <Input
              value={emailAddress}
              onChange={(e) => setEmailAddress(e.target.value)}
              placeholder="alice@example.com"
            />
          </div>

          <div className="space-y-2">
            <Label>Backend</Label>
            <Select
              value={backendName}
              onValueChange={(v) => setBackendName(v ?? "")}
              disabled={!isCreate}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(backendsQuery.data ?? []).map((b) => (
                  <SelectItem key={b.name} value={b.name}>
                    {b.display_name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {selectedBackend?.config_params.map((p) => (
            <div key={p.key} className="space-y-2">
              <ConfigField
                param={p}
                value={backendConfig[p.key]}
                onChange={(v) =>
                  setBackendConfig({ ...backendConfig, [p.key]: v })
                }
              />
            </div>
          ))}

          <Separator />

          <div className="space-y-2">
            <Label>Calendar</Label>
            {!isCreate && (
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => probe.mutate()}
                  disabled={probe.isPending}
                >
                  {probe.isPending ? "Probing…" : "Probe calendars"}
                </Button>
              </div>
            )}
            {probedCalendars.length > 0 ? (
              <Select
                value={calendarId}
                onValueChange={(v) => setCalendarId(v ?? "")}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {probedCalendars.map((c) => (
                    <SelectItem key={c.id} value={c.id}>
                      {c.name}
                      {c.primary ? " (primary)" : ""}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : (
              <Input
                value={calendarId}
                onChange={(e) => setCalendarId(e.target.value)}
                placeholder="primary"
              />
            )}
          </div>

          <div className="space-y-2">
            <Label>Timezone (IANA)</Label>
            <Input
              value={timezone}
              onChange={(e) => setTimezone(e.target.value)}
              placeholder="America/New_York"
            />
          </div>

          <div className="grid grid-cols-2 gap-2">
            <div className="space-y-2">
              <Label>Working hours start</Label>
              <Input
                type="number"
                min={0}
                max={23}
                value={whStart}
                onChange={(e) => setWhStart(parseInt(e.target.value, 10))}
              />
            </div>
            <div className="space-y-2">
              <Label>Working hours end</Label>
              <Input
                type="number"
                min={1}
                max={24}
                value={whEnd}
                onChange={(e) => setWhEnd(parseInt(e.target.value, 10))}
              />
            </div>
          </div>

          <div className="space-y-2">
            <Label>
              <input
                type="checkbox"
                checked={pollEnabled}
                onChange={(e) => setPollEnabled(e.target.checked)}
                className="mr-2"
              />
              Poll enabled (sync events)
            </Label>
          </div>

          <div className="space-y-2">
            <Label>Poll interval (seconds)</Label>
            <Input
              type="number"
              min={30}
              value={pollInterval}
              onChange={(e) => setPollInterval(parseInt(e.target.value, 10))}
            />
          </div>

          {error && (
            <div className="flex items-center gap-2 text-sm text-destructive">
              <AlertTriangleIcon className="h-4 w-4" />
              {error}
            </div>
          )}

          <div className="flex justify-between gap-2 pt-2">
            <div>
              {!isCreate && account!.can_admin && (
                <Button
                  variant="destructive"
                  onClick={() => {
                    if (window.confirm(`Delete ${account!.name}?`)) {
                      deleteAccount.mutate();
                    }
                  }}
                >
                  <Trash2Icon className="h-4 w-4 mr-1" /> Delete
                </Button>
              )}
            </div>
            <div className="flex gap-2">
              <Button variant="outline" onClick={onClose}>
                Cancel
              </Button>
              <Button onClick={submit} disabled={create.isPending || update.isPending}>
                {isCreate ? "Create" : "Save"}
              </Button>
            </div>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}

