import { Outlet } from "react-router-dom";
import { SideNav } from "./SideNav";
import { TopBar } from "./TopBar";
import { PageSidebarProvider } from "./PageSidebar";
import { useMcpBridge } from "@/hooks/useMcpBridge";
import { PluginPanelSlot } from "@/components/PluginPanelSlot";
import { BrowserSpeakerProvider } from "@/hooks/useBrowserSpeaker";

export function AppShell() {
  // Mount the MCP browser-bridge here so it lives for the full
  // authenticated session but never runs on the login page.
  useMcpBridge();
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
