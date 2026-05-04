import { Outlet } from "react-router-dom";
import { NavBar } from "./NavBar";
import { useMcpBridge } from "@/hooks/useMcpBridge";
import { PluginPanelSlot } from "@/components/PluginPanelSlot";

export function AppShell() {
  // Mount the MCP browser-bridge here so it lives for the full
  // authenticated session but never runs on the login page.
  useMcpBridge();
  return (
    <div className="flex h-[100svh] flex-col overflow-hidden">
      <NavBar />
      <main className="flex-1 overflow-auto">
        <Outlet />
      </main>
      {/* Always-mounted slot for plugin background components —
          global listeners, modal hosts, etc. Plugins target
          ``slot="app.background"`` and render invisible components
          that hold app-wide state (e.g., the browser plugin's
          VNC modal mounter listening for agent-action triggers). */}
      <PluginPanelSlot slot="app.background" />
    </div>
  );
}
