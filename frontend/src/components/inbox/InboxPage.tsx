import { useRef, useEffect, useCallback } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import type { MessageDetail, InboxMessage } from "@/types/inbox";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export function InboxPage() {
  const queryClient = useQueryClient();
  const api = useWsApi();
  const { connected } = useWebSocket();
  const [searchParams, setSearchParams] = useSearchParams();

  const sender = searchParams.get("sender") || "";
  const subject = searchParams.get("subject") || "";
  const messageId = searchParams.get("msg") || "";

  const setSender = (v: string) => {
    const p = new URLSearchParams(searchParams);
    if (v) p.set("sender", v); else p.delete("sender");
    p.delete("msg");
    setSearchParams(p);
  };
  const setSubject = (v: string) => {
    const p = new URLSearchParams(searchParams);
    if (v) p.set("subject", v); else p.delete("subject");
    p.delete("msg");
    setSearchParams(p);
  };

  const { data: stats } = useQuery({
    queryKey: ["inbox-stats"],
    queryFn: api.inboxStats,
    enabled: connected,
  });

  const { data: messages = [], refetch } = useQuery({
    queryKey: ["inbox-messages", sender, subject],
    queryFn: () => api.listMessages({ sender: sender || undefined, subject: subject || undefined }),
    enabled: connected,
  });

  const { data: pending = [] } = useQuery({
    queryKey: ["inbox-pending"],
    queryFn: api.listPending,
    enabled: connected,
  });

  const cancelMutation = useMutation({
    mutationFn: api.cancelPending,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["inbox-pending"] }),
  });

  // Load message detail when msg param is set
  const { data: selectedMsg, isLoading: loadingDetail } = useQuery({
    queryKey: ["inbox-message", messageId],
    queryFn: () => api.getMessage(messageId),
    enabled: connected && !!messageId,
  });

  // Load thread if the selected message has a thread_id
  const { data: threadData } = useQuery({
    queryKey: ["inbox-thread", selectedMsg?.thread_id],
    queryFn: () => api.getThread(selectedMsg!.thread_id!),
    enabled: connected && !!selectedMsg?.thread_id,
  });

  const threadMsgs = threadData ?? (selectedMsg ? [selectedMsg] : []);

  function handleRowClick(msg: InboxMessage) {
    const p = new URLSearchParams(searchParams);
    p.set("msg", msg.message_id);
    setSearchParams(p);
  }

  function closeDetail() {
    const p = new URLSearchParams(searchParams);
    p.delete("msg");
    setSearchParams(p);
  }

  return (
    <div className="p-6 space-y-6 max-w-4xl mx-auto">
      <div className="flex items-center gap-4">
        <h1 className="text-2xl font-semibold text-center">Inbox</h1>
        {stats && (
          <Badge variant="secondary">
            {stats.total} messages
          </Badge>
        )}
      </div>

      {pending.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">
              Pending Outgoing ({pending.length})
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-2">
              {pending.map((p) => (
                <div
                  key={p.id}
                  className="flex items-center gap-3 text-sm border-b pb-2 last:border-0"
                >
                  <Badge variant="outline">{p.status}</Badge>
                  <span className="text-muted-foreground">{p.send_at}</span>
                  <span className="truncate flex-1">{p.customer_email} — {p.subject}</span>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="text-destructive"
                    onClick={() => cancelMutation.mutate(p.id)}
                  >
                    Cancel
                  </Button>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      <div className="flex gap-2">
        <Input
          value={sender}
          onChange={(e) => setSender(e.target.value)}
          placeholder="Filter by sender..."
          className="w-48"
        />
        <Input
          value={subject}
          onChange={(e) => setSubject(e.target.value)}
          placeholder="Filter by subject..."
          className="w-48"
        />
        <Button variant="outline" onClick={() => refetch()}>
          Search
        </Button>
      </div>

      <Card>
        <CardContent className="p-0">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b">
                <th className="px-3 py-2 text-left font-medium w-8"></th>
                <th className="px-3 py-2 text-left font-medium">Date</th>
                <th className="px-3 py-2 text-left font-medium">From</th>
                <th className="px-3 py-2 text-left font-medium">Subject</th>
                <th className="px-3 py-2 text-left font-medium">Preview</th>
              </tr>
            </thead>
            <tbody>
              {messages.map((msg) => (
                <tr
                  key={msg.message_id}
                  className={`border-b hover:bg-accent/50 cursor-pointer ${msg.message_id === messageId ? "bg-accent/30" : ""}`}
                  onClick={() => handleRowClick(msg)}
                >
                  <td className="px-3 py-2">
                    {msg.is_inbound ? "\u2192" : "\u2190"}
                  </td>
                  <td className="px-3 py-2 whitespace-nowrap">
                    {new Date(msg.date).toLocaleDateString()}
                  </td>
                  <td className="px-3 py-2 truncate max-w-32">{msg.sender_name || msg.sender_email}</td>
                  <td className="px-3 py-2 truncate max-w-48">{msg.subject}</td>
                  <td className="px-3 py-2 truncate max-w-64 text-muted-foreground">
                    {msg.snippet}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardContent>
      </Card>

      {/* Loading overlay for detail fetch */}
      <Dialog open={loadingDetail} onOpenChange={() => {}}>
        <DialogContent showCloseButton={false} className="flex items-center justify-center py-8">
          <LoadingSpinner text="Loading message..." />
        </DialogContent>
      </Dialog>

      {/* Message detail modal */}
      <Dialog open={!!selectedMsg && !loadingDetail} onOpenChange={() => closeDetail()}>
        <DialogContent className="overflow-y-auto" style={{ maxWidth: "95vw", width: "95vw", height: "95vh", maxHeight: "95vh" }}>
          <DialogHeader>
            <DialogTitle>{selectedMsg?.subject}</DialogTitle>
          </DialogHeader>
          {selectedMsg && (
            <div className="text-sm space-y-0">
              {threadMsgs.map((msg, i) => (
                <div key={msg.message_id || i} className={i > 0 ? "border-t pt-4 mt-4" : ""}>
                  <div className="text-muted-foreground pb-3">
                    <div>From: {msg.sender_name || msg.sender_email}</div>
                    {msg.to?.length > 0 && (
                      <div>To: {msg.to.map((a: any) => a.name || a.email).join(", ")}</div>
                    )}
                    {msg.cc?.length > 0 && (
                      <div>CC: {msg.cc.map((a: any) => a.name || a.email).join(", ")}</div>
                    )}
                    <div>Date: {new Date(msg.date).toLocaleString()}</div>
                  </div>
                  {msg.body_html ? (
                    <EmailFrame html={msg.body_html} />
                  ) : (
                    <pre className="whitespace-pre-wrap">{msg.body_text}</pre>
                  )}
                </div>
              ))}
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}

/** Sandboxed iframe that auto-sizes to fit its HTML content. */
function EmailFrame({ html }: { html: string }) {
  const ref = useRef<HTMLIFrameElement>(null);

  const resize = useCallback(() => {
    const iframe = ref.current;
    if (!iframe) return;
    try {
      const doc = iframe.contentDocument;
      if (!doc?.body) return;
      iframe.style.height = "0";
      const h = doc.documentElement.scrollHeight || doc.body.scrollHeight;
      iframe.style.height = h + "px";
    } catch {
      // Cross-origin — can't measure
    }
  }, []);

  useEffect(() => {
    const iframe = ref.current;
    if (!iframe) return;
    let observer: MutationObserver | null = null;

    const handleLoad = () => {
      resize();
      try {
        const doc = iframe.contentDocument;
        if (doc?.body) {
          observer = new MutationObserver(resize);
          observer.observe(doc.body, { childList: true, subtree: true, attributes: true });
          doc.querySelectorAll("img").forEach((img) => {
            if (!img.complete) img.addEventListener("load", resize);
          });
        }
      } catch {
        // cross-origin
      }
    };

    iframe.addEventListener("load", handleLoad);
    return () => {
      iframe.removeEventListener("load", handleLoad);
      observer?.disconnect();
    };
  }, [html, resize]);

  return (
    <iframe
      ref={ref}
      sandbox="allow-same-origin"
      srcDoc={html}
      className="w-full border-0 rounded bg-white"
      style={{ minHeight: "60px" }}
      title="Email content"
    />
  );
}
