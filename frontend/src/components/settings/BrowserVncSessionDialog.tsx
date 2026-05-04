/**
 * BrowserVncSessionDialog — modal that hosts a noVNC iframe pointed at
 * a server-side headed Chromium. The user logs into a site
 * interactively; on close, the headed context's storage_state is
 * merged into their persistent headless context.
 */

import { useEffect, useRef, useState } from "react";
import { useWsApi } from "@/hooks/useWsApi";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { BrowserVncSession } from "@/types/browser";

interface Props {
  credentialId?: string;
  targetUrl?: string;
  onClose: () => void;
}

export function BrowserVncSessionDialog({
  credentialId,
  targetUrl,
  onClose,
}: Props) {
  const api = useWsApi();
  const [session, setSession] = useState<BrowserVncSession | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  // Track session id in a ref so the unmount cleanup sees the latest
  // value even if React batches the close via re-render.
  const sessionIdRef = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await api.startBrowserVncSession({
          credential_id: credentialId,
          target_url: targetUrl,
        });
        if (cancelled) return;
        if (r.ok && r.session) {
          setSession(r.session);
          sessionIdRef.current = r.session.id;
        } else {
          setError("Failed to start VNC session");
        }
      } catch (e) {
        if (!cancelled) {
          setError((e as Error).message ?? "Failed to start VNC session");
        }
      } finally {
        if (!cancelled) setBusy(false);
      }
    })();
    return () => {
      cancelled = true;
      const id = sessionIdRef.current;
      sessionIdRef.current = null;
      if (id) {
        api.stopBrowserVncSession(id).catch(() => {});
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [credentialId, targetUrl]);

  const handleDone = async () => {
    onClose();
  };

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>Live login session</DialogTitle>
        </DialogHeader>

        <div className="rounded bg-yellow-500/10 text-yellow-800 dark:text-yellow-300 px-3 py-2 text-xs">
          This is a real browser running on the server. Don't enter
          passwords for sites you don't own. The session ends as soon as
          you click <strong>Done</strong>.
        </div>

        <div className="rounded border bg-black/90 aspect-[4/3] overflow-hidden">
          {busy ? (
            <div className="flex items-center justify-center h-full text-muted-foreground text-sm">
              Starting browser session…
            </div>
          ) : error ? (
            <div className="flex items-center justify-center h-full text-red-400 text-sm p-4 text-center">
              {error}
            </div>
          ) : session ? (
            <iframe
              title="VNC session"
              src={`/api/browser/novnc/vnc.html?path=${encodeURIComponent(
                session.vnc_url,
              )}&autoconnect=1&resize=scale`}
              className="w-full h-full border-0"
            />
          ) : null}
        </div>

        <DialogFooter>
          <Button onClick={handleDone}>Done</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
