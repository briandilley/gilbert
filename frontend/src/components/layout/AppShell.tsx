import { useEffect } from "react";
import { Outlet } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { SideNav } from "./SideNav";
import { TopBar } from "./TopBar";
import { PageSidebarProvider } from "./PageSidebar";
import { useMcpBridge } from "@/hooks/useMcpBridge";
import { useWebSocket } from "@/hooks/useWebSocket";
import { PluginPanelSlot } from "@/components/PluginPanelSlot";
import { BrowserSpeakerProvider } from "@/hooks/useBrowserSpeaker";

/**
 * Invalidate the dashboard nav + plugin-routes queries whenever a
 * service's lifecycle state changes (started / stopped / failed) so
 * a toggle in Settings → Services takes effect in the header / side
 * nav / route table without a page refresh. Without this, disabling
 * a service that owns a ``requires_capability`` route leaves its
 * nav icon hanging until the user hard-reloads.
 */
function useNavInvalidationOnServiceChange(): void {
  const queryClient = useQueryClient();
  const { subscribe } = useWebSocket();
  useEffect(() => {
    const invalidate = () => {
      queryClient.invalidateQueries({ queryKey: ["dashboard"] });
      queryClient.invalidateQueries({ queryKey: ["ui-routes"] });
    };
    const unsubs = [
      subscribe("service.started", invalidate),
      subscribe("service.stopped", invalidate),
      subscribe("service.failed", invalidate),
    ];
    return () => {
      for (const u of unsubs) u();
    };
  }, [queryClient, subscribe]);
}

export function AppShell() {
  // Mount the MCP browser-bridge here so it lives for the full
  // authenticated session but never runs on the login page.
  useMcpBridge();
  useNavInvalidationOnServiceChange();
  return (
    <BrowserSpeakerProvider>
      <PageSidebarProvider>
        <div className="flex h-[100svh] flex-col overflow-hidden">
          <TopBar />
          <div className="flex flex-1 min-h-0">
            <SideNav />
            <main className="flex-1 overflow-auto min-w-0">
              <Outlet />
            </main>
          </div>
          {/* Always-mounted slot for plugin background components —
              global listeners, modal hosts, etc. */}
          <PluginPanelSlot slot="app.background" />
        </div>
      </PageSidebarProvider>
    </BrowserSpeakerProvider>
  );
}
