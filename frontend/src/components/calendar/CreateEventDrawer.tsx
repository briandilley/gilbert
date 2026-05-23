import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useWsApi } from "@/hooks/useWsApi";
import type { CalendarAccount } from "@/types/calendar";
import { addMinutes, defaultEventTimesForDate } from "./datetime";

interface Props {
  accounts: CalendarAccount[];
  defaultAccountId: string | null;
  defaultStartDate: Date | null;
  onClose: () => void;
  onCreated: () => void;
}

/** "New event" drawer for the human-confirmed create-event flow.
 *
 * Per the spec, ``send_invites`` defaults to ``true`` here (this is a
 * human-confirmed flow), which is the inverse of the AI tool's
 * default. The user is in the loop for every field so opt-in is
 * implicit.
 */
export function CreateEventDrawer({
  accounts,
  defaultAccountId,
  defaultStartDate,
  onClose,
  onCreated,
}: Props) {
  const api = useWsApi();
  const writableAccounts = accounts.filter((a) => a.access !== null);
  const [accountId, setAccountId] = useState(
    defaultAccountId || writableAccounts[0]?.id || "",
  );
  const defaultTimes = defaultEventTimesForDate(defaultStartDate);
  const [title, setTitle] = useState("");
  const [start, setStart] = useState(defaultTimes.start);
  const [end, setEnd] = useState(defaultTimes.end);
  const [description, setDescription] = useState("");
  const [location, setLocation] = useState("");
  const [attendees, setAttendees] = useState("");
  const [allDay, setAllDay] = useState(false);
  const [sendInvites, setSendInvites] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: () => {
      if (!accountId) throw new Error("Pick an account");
      if (!title) throw new Error("Title required");
      if (!start) throw new Error("Start required");
      if (!end) throw new Error("End required");
      const attendeeEmails = attendees
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      return api.createCalendarEvent(accountId, {
        title,
        start: new Date(start).toISOString(),
        end: new Date(end).toISOString(),
        description,
        location,
        attendees: attendeeEmails,
        all_day: allDay,
        send_invites: sendInvites,
      });
    },
    onSuccess: () => {
      onCreated();
      onClose();
    },
    onError: (e: Error) => setError(e.message),
  });

  const setStartDate = (date: string) => {
    if (!date) return;
    const nextStart = `${date}T${timePart(start)}`;
    setStart(nextStart);
    if (end <= nextStart) {
      setEnd(addMinutes(nextStart, 60));
    }
  };

  const setStartTime = (time: string) => {
    if (!time) return;
    const nextStart = `${datePart(start)}T${time}`;
    setStart(nextStart);
    if (end <= nextStart) {
      setEnd(addMinutes(nextStart, 60));
    }
  };

  const setEndDate = (date: string) => {
    if (date) setEnd(`${date}T${timePart(end)}`);
  };
  const setEndTime = (time: string) => {
    if (time) setEnd(`${datePart(end)}T${time}`);
  };

  return (
    <Sheet open onOpenChange={(o) => (!o ? onClose() : null)}>
      <SheetContent className="sm:max-w-lg overflow-y-auto">
        <SheetHeader>
          <SheetTitle>New event</SheetTitle>
        </SheetHeader>
        <div className="space-y-4 py-4">
          <div className="space-y-2">
            <Label>Account</Label>
            <Select value={accountId} onValueChange={(v) => setAccountId(v ?? "")}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {writableAccounts.map((a) => (
                  <SelectItem key={a.id} value={a.id}>
                    {a.name} ({a.email_address})
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-2">
            <Label>Title</Label>
            <Input value={title} onChange={(e) => setTitle(e.target.value)} />
          </div>
          <div className="grid grid-cols-[minmax(0,1fr)_7.5rem] gap-2">
            <div className="space-y-2">
              <Label>Start date</Label>
              <Input
                type="date"
                value={datePart(start)}
                onChange={(e) => setStartDate(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label>Start time</Label>
              <Input
                type="time"
                step={900}
                value={timePart(start)}
                onChange={(e) => setStartTime(e.target.value)}
              />
            </div>
          </div>
          <div className="grid grid-cols-[minmax(0,1fr)_7.5rem] gap-2">
            <div className="space-y-2">
              <Label>End date</Label>
              <Input
                type="date"
                value={datePart(end)}
                onChange={(e) => setEndDate(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label>End time</Label>
              <Input
                type="time"
                step={900}
                value={timePart(end)}
                onChange={(e) => setEndTime(e.target.value)}
              />
            </div>
          </div>
          <div className="flex flex-wrap gap-1">
            {[30, 60, 90, 120].map((minutes) => (
              <Button
                key={minutes}
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setEnd(addMinutes(start, minutes))}
              >
                {minutes < 60 ? `${minutes}m` : `${minutes / 60}h`}
              </Button>
            ))}
          </div>
          <div className="space-y-2">
            <Label>Location</Label>
            <Input
              value={location}
              onChange={(e) => setLocation(e.target.value)}
            />
          </div>
          <div className="space-y-2">
            <Label>Description</Label>
            <Textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={3}
            />
          </div>
          <div className="space-y-2">
            <Label>Attendees (comma-separated emails)</Label>
            <Input
              value={attendees}
              onChange={(e) => setAttendees(e.target.value)}
              placeholder="bob@example.com, carol@example.com"
            />
          </div>
          <div className="flex items-center gap-2">
            <Label>
              <input
                type="checkbox"
                checked={allDay}
                onChange={(e) => setAllDay(e.target.checked)}
                className="mr-2"
              />
              All day
            </Label>
            <Label>
              <input
                type="checkbox"
                checked={sendInvites}
                onChange={(e) => setSendInvites(e.target.checked)}
                className="mr-2"
              />
              Email invites
            </Label>
          </div>
          {error && (
            <div className="text-sm text-destructive">{error}</div>
          )}
          <div className="flex justify-end gap-2 pt-2">
            <Button variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button onClick={() => create.mutate()} disabled={create.isPending}>
              Create
            </Button>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}

function datePart(localDateTime: string): string {
  return localDateTime.slice(0, 10);
}

function timePart(localDateTime: string): string {
  return localDateTime.slice(11, 16);
}
