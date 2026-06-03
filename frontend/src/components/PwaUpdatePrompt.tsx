import { useEffect, useState } from "react";
import { registerSW } from "virtual:pwa-register";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * PwaUpdatePrompt — fixed-position bottom-right toast that surfaces
 * when a new service worker is waiting.
 *
 * The page does NOT auto-reload when a new SW activates; the user
 * explicitly accepts via this prompt. That keeps in-flight state
 * (open chat, half-typed message, scroll position) intact. Clicking
 * "Reload" posts SKIP_WAITING to the waiting worker, which then
 * activates and refreshes the page.
 *
 * Styling is deliberately minimal — we reach for the existing
 * Button primitive and Tailwind tokens from index.css. No
 * design-system Card here because the toast must float over every
 * other surface without inheriting its layout.
 */
export function PwaUpdatePrompt() {
  const [needRefresh, setNeedRefresh] = useState(false);
  const [updateSW, setUpdateSW] = useState<
    ((reloadPage?: boolean) => Promise<void>) | null
  >(null);

  useEffect(() => {
    // registerSW returns the update function. We capture it in state
    // so the click handler can call updateSW(true) to skip-waiting
    // and reload.
    const update = registerSW({
      onNeedRefresh() {
        setNeedRefresh(true);
      },
      onOfflineReady() {
        // Intentionally silent — the SPA shell working offline is a
        // win, not something the user needs to ack.
      },
      onRegisterError(error) {
        // Surface registration failures in the console but don't
        // crash the page. The SPA still works without the SW.
        // eslint-disable-next-line no-console
        console.error("[pwa] service worker registration failed", error);
      },
    });
    setUpdateSW(() => update);
  }, []);

  if (!needRefresh) return null;

  const handleReload = () => {
    if (!updateSW) {
      // Should not happen — updateSW is captured before needRefresh
      // can ever flip true — but degrade gracefully.
      window.location.reload();
      return;
    }
    void updateSW(true);
  };

  const handleDismiss = () => setNeedRefresh(false);

  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        "fixed bottom-4 right-4 z-50",
        "flex items-center gap-3",
        "rounded-md border border-border-strong bg-popover text-popover-foreground",
        "px-3 py-2 shadow-lg",
        "max-w-sm",
      )}
    >
      <div className="flex flex-col">
        <span className="text-sm font-medium">Update available</span>
        <span className="text-xs text-muted-foreground">
          A new version of Gilbert is ready.
        </span>
      </div>
      <div className="flex items-center gap-1.5 ml-auto">
        <Button variant="ghost" size="sm" onClick={handleDismiss}>
          Later
        </Button>
        <Button variant="default" size="sm" onClick={handleReload}>
          Reload
        </Button>
      </div>
    </div>
  );
}
