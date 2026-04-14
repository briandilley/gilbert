import { useQuery } from "@tanstack/react-query";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import type { InboxMessage } from "@/types/inbox";

interface MessageListProps {
  mailboxId: string | null;
  sender: string;
  subject: string;
  onSenderChange: (v: string) => void;
  onSubjectChange: (v: string) => void;
  selectedMessageId: string | null;
  onSelectMessage: (msg: InboxMessage) => void;
}

export function MessageList({
  mailboxId, sender, subject,
  onSenderChange, onSubjectChange,
  selectedMessageId, onSelectMessage,
}: MessageListProps) {
  const api = useWsApi();
  const { connected } = useWebSocket();

  const { data: messages = [], refetch, isLoading } = useQuery({
    queryKey: ["inbox-messages", mailboxId, sender, subject],
    queryFn: () =>
      api.listMessages({
        mailbox_id: mailboxId ?? undefined,
        sender: sender || undefined,
        subject: subject || undefined,
      }),
    enabled: connected,
  });

  return (
    <div className="space-y-3">
      <div className="flex flex-col gap-2 sm:flex-row">
        <Input
          value={sender}
          onChange={(e) => onSenderChange(e.target.value)}
          placeholder="Filter by sender..."
          className="sm:w-48"
        />
        <Input
          value={subject}
          onChange={(e) => onSubjectChange(e.target.value)}
          placeholder="Filter by subject..."
          className="sm:flex-1 sm:max-w-xs"
        />
        <Button variant="outline" onClick={() => refetch()} className="sm:w-auto">
          Search
        </Button>
      </div>

      <Card>
        <CardContent className="p-0">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b">
                  <th className="px-3 py-2 text-left font-medium w-8"></th>
                  <th className="px-3 py-2 text-left font-medium whitespace-nowrap">Date</th>
                  <th className="px-3 py-2 text-left font-medium">From</th>
                  <th className="px-3 py-2 text-left font-medium">Subject</th>
                  <th className="hidden md:table-cell px-3 py-2 text-left font-medium">Preview</th>
                </tr>
              </thead>
              <tbody>
                {messages.length === 0 && !isLoading && (
                  <tr>
                    <td
                      colSpan={5}
                      className="px-3 py-8 text-center text-xs text-muted-foreground"
                    >
                      No messages.
                    </td>
                  </tr>
                )}
                {messages.map((msg) => (
                  <tr
                    key={msg.message_id}
                    className={`border-b hover:bg-accent/50 cursor-pointer ${
                      msg.message_id === selectedMessageId ? "bg-accent/30" : ""
                    }`}
                    onClick={() => onSelectMessage(msg)}
                  >
                    <td className="px-3 py-2">
                      {msg.is_inbound ? "\u2192" : "\u2190"}
                    </td>
                    <td className="px-3 py-2 whitespace-nowrap">
                      {new Date(msg.date).toLocaleDateString()}
                    </td>
                    <td className="px-3 py-2 truncate max-w-32">
                      {msg.sender_name || msg.sender_email}
                    </td>
                    <td className="px-3 py-2 truncate max-w-48">{msg.subject}</td>
                    <td className="hidden md:table-cell px-3 py-2 truncate max-w-64 text-muted-foreground">
                      {msg.snippet}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
