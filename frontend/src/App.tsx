import { Routes, Route, Navigate } from "react-router-dom";
import { AppShell } from "@/components/layout/AppShell";
import { ProtectedRoute } from "@/components/layout/ProtectedRoute";
import { LoginPage } from "@/components/auth/LoginPage";
import { AccountPage } from "@/components/account/AccountPage";
import { DashboardPage } from "@/components/dashboard/DashboardPage";
import { ChatPage } from "@/components/chat/ChatPage";
import { DocumentsPage } from "@/components/documents/DocumentsPage";
import { EntitiesPage } from "@/components/entities/EntitiesPage";
import { CollectionDetail } from "@/components/entities/CollectionDetail";
import { EntityDetail } from "@/components/entities/EntityDetail";
import { InboxPage } from "@/components/inbox/InboxPage";
import { RolesPage } from "@/components/roles/RolesPage";
import { SettingsPage } from "@/components/settings/SettingsPage";
import { SystemPage } from "@/components/system/SystemPage";
import { ScreensPage } from "@/components/screens/ScreensPage";
import { SchedulerPage } from "@/components/scheduler/SchedulerPage";
import { PluginsPage } from "@/components/plugins/PluginsPage";
import { ProposalsPage } from "@/components/proposals/ProposalsPage";
import { McpPage } from "@/components/mcp/McpPage";
import { McpClientsPage } from "@/components/mcp/McpClientsPage";
import { McpLocalPage } from "@/components/mcp/McpLocalPage";
import { UsagePage } from "@/components/usage/UsagePage";
import { NotificationsPage } from "@/components/notifications/NotificationsPage";
import { AgentsListPage } from "@/components/agent/AgentsListPage";
import { AgentEditForm } from "@/components/agent/AgentEditForm";
import { AgentDetailPage } from "@/components/agent/AgentDetailPage";
import { GoalsListPage } from "@/components/goals/GoalsListPage";
import { WarRoomPage } from "@/components/goals/WarRoomPage";
import { usePluginRouteElements } from "@/components/PluginRoutes";

export default function App() {
  const pluginRoutes = usePluginRouteElements();
  return (
    <Routes>
      <Route path="/auth/login" element={<LoginPage />} />
      <Route element={<ProtectedRoute />}>
        <Route element={<AppShell />}>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/documents" element={<DocumentsPage />} />
          <Route path="/entities" element={<EntitiesPage />} />
          <Route path="/entities/:collection" element={<CollectionDetail />} />
          <Route
            path="/entities/:collection/:entityId"
            element={<EntityDetail />}
          />
          <Route path="/inbox" element={<InboxPage />} />
          <Route path="/security" element={<Navigate to="/security/users" replace />} />
          <Route path="/security/*" element={<RolesPage />} />
          <Route path="/scheduler" element={<SchedulerPage />} />
          <Route path="/mcp" element={<Navigate to="/mcp/servers" replace />} />
          <Route path="/mcp/servers" element={<McpPage />} />
          <Route path="/mcp/clients" element={<McpClientsPage />} />
          <Route path="/mcp/local" element={<McpLocalPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/plugins" element={<PluginsPage />} />
          <Route path="/proposals" element={<ProposalsPage />} />
          <Route path="/system" element={<SystemPage />} />
          <Route path="/screens" element={<ScreensPage />} />
          <Route path="/usage" element={<UsagePage />} />
          <Route path="/notifications" element={<NotificationsPage />} />
          <Route path="/agents" element={<AgentsListPage />} />
          <Route path="/agents/new" element={<AgentEditForm mode="create" />} />
          <Route path="/agents/:agentId" element={<AgentDetailPage />} />
          <Route path="/goals" element={<GoalsListPage />} />
          <Route path="/goals/:goalId" element={<WarRoomPage />} />
          <Route path="/account" element={<AccountPage />} />
          {/* Plugin-contributed routes — looked up server-side and
              rendered with components from the per-plugin registry. */}
          {pluginRoutes}
        </Route>
      </Route>
    </Routes>
  );
}
