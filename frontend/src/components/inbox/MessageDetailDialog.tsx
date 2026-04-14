import { useEffect, useRef, useCallback } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import type { MessageDetail } from "@/types/inbox";

interface MessageDetailDialogProps {
  messageId: string | null;
  mailboxId: string | null;
  onClose: () => void;
}

export function MessageDetailDialog({
  messageId, mailboxId, onClose,
}: MessageDetailDialogProps) {
  const api = useWsApi();
  const { connected } = useWebSocket();

  const { data: selectedMsg, isLoading: loadingDetail } = useQuery({
    queryKey: ["inbox-message", messageId],
    queryFn: () => api.getMessage(messageId!),
    enabled: connected && !!messageId,
  });

  const { data: threadData } = useQuery({
    queryKey: ["inbox-thread", mailboxId, selectedMsg?.thread_id],
    queryFn: () => api.getThread(selectedMsg!.thread_id!, mailboxId ?? undefined),
    enabled: connected && !!selectedMsg?.thread_id,
  });

  const threadMsgs = threadData ?? (selectedMsg ? [selectedMsg] : []);

  return (
    <>
      <Dialog open={loadingDetail} onOpenChange={() => {}}>
        <DialogContent
          showCloseButton={false}
          className="flex items-center justify-center py-8"
        >
          <LoadingSpinner text="Loading message..." />
        </DialogContent>
      </Dialog>

      <Dialog
        open={!!selectedMsg && !loadingDetail}
        onOpenChange={(open) => !open && onClose()}
      >
        <DialogContent className="flex max-h-[95vh] w-[calc(100%-1rem)] flex-col overflow-hidden sm:!max-w-3xl lg:!max-w-5xl">
          <DialogHeader>
            <DialogTitle className="pr-8 break-words">
              {selectedMsg?.subject}
            </DialogTitle>
          </DialogHeader>
          {selectedMsg && (
            <div className="flex-1 overflow-y-auto text-sm space-y-0 -mx-4 px-4">
              {threadMsgs.map((msg, i) => (
                <ThreadMessage key={msg.message_id || i} msg={msg} divider={i > 0} />
              ))}
            </div>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}

function ThreadMessage({ msg, divider }: { msg: MessageDetail; divider: boolean }) {
  return (
    <div className={divider ? "border-t pt-4 mt-4" : ""}>
      <div className="text-muted-foreground pb-3 break-words">
        <div>From: {msg.sender_name || msg.sender_email}</div>
        {msg.to?.length > 0 && (
          <div>To: {msg.to.map((a) => a.name || a.email).join(", ")}</div>
        )}
        {msg.cc && msg.cc.length > 0 && (
          <div>CC: {msg.cc.map((a) => a.name || a.email).join(", ")}</div>
        )}
        <div>Date: {new Date(msg.date).toLocaleString()}</div>
      </div>
      {msg.body_html ? (
        <EmailFrame html={msg.body_html} />
      ) : (
        <pre className="whitespace-pre-wrap break-words">{msg.body_text}</pre>
      )}
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
      /* cross-origin */
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
          observer.observe(doc.body, {
            childList: true, subtree: true, attributes: true,
          });
          doc.querySelectorAll("img").forEach((img) => {
            if (!img.complete) img.addEventListener("load", resize);
          });
        }
      } catch {
        /* cross-origin */
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
