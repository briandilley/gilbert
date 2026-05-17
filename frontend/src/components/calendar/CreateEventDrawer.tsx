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

interface Props {
  accounts: CalendarAccount[];
  defaultAccountId: string | null;
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
  onClose,
  onCreated,
}: Props) {
  const api = useWsApi();
  const writableAccounts = accounts.filter((a) => a.access !== null);
  const [accountId, setAccountId] = useState(
    defaultAccountId || writableAccounts[0]?.id || "",
  );
  const [title, setTitle] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
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
          <div className="space-y-2">
            <Label>Start</Label>
            <Input
              type="datetime-local"
              value={start}
              onChange={(e) => setStart(e.target.value)}
            />
          </div>
          <div className="space-y-2">
            <Label>End</Label>
            <Input
              type="datetime-local"
              value={end}
              onChange={(e) => setEnd(e.target.value)}
            />
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

