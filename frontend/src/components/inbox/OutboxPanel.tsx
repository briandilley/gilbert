import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import type { OutboxEntry, OutboxStatus } from "@/types/inbox";

interface OutboxPanelProps {
  mailboxId: string | null;
}

const STATUS_VARIANTS: Record<OutboxStatus, "default" | "destructive" | "outline" | "secondary"> = {
  pending: "outline",
  sending: "secondary",
  sent: "default",
  failed: "destructive",
  cancelled: "outline",
};

export function OutboxPanel({ mailboxId }: OutboxPanelProps) {
  const api = useWsApi();
  const { connected } = useWebSocket();
  const queryClient = useQueryClient();

  const { data: entries = [] } = useQuery({
    queryKey: ["inbox-outbox", mailboxId],
    queryFn: () => api.listOutbox({ mailbox_id: mailboxId ?? undefined }),
    enabled: connected,
  });

  const cancelMutation = useMutation({
    mutationFn: api.cancelOutbox,
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["inbox-outbox"] }),
  });

  // Only show non-terminal or recent entries to avoid clutter.
  const active = entries.filter(
    (e) => e.status === "pending" || e.status === "sending" || e.status === "failed",
  );

  if (active.length === 0) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm">
          Outbox ({active.length})
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="space-y-2">
          {active.map((e) => (
            <OutboxRow
              key={e.id}
              entry={e}
              onCancel={() => cancelMutation.mutate(e.id)}
            />
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

function OutboxRow({
  entry, onCancel,
}: {
  entry: OutboxEntry;
  onCancel: () => void;
}) {
  const date = entry.send_at ? new Date(entry.send_at).toLocaleString() : "";
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm border-b pb-2 last:border-0 sm:gap-3">
      <Badge variant={STATUS_VARIANTS[entry.status]}>{entry.status}</Badge>
      <span className="text-muted-foreground text-xs sm:text-sm">{date}</span>
      <span className="min-w-0 flex-1 basis-full truncate sm:basis-auto">
        {entry.to.join(", ")} — {entry.subject || "(no subject)"}
      </span>
      {entry.error && (
        <span
          className="basis-full text-xs text-destructive truncate"
          title={entry.error}
        >
          Error: {entry.error}
        </span>
      )}
      {(entry.status === "pending" || entry.status === "failed") && (
        <Button
          variant="ghost"
          size="sm"
          className="ml-auto text-destructive"
          onClick={onCancel}
        >
          Cancel
        </Button>
      )}
    </div>
  );
}
