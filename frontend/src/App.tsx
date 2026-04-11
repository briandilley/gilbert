import { Routes, Route } from "react-router-dom";
import { AppShell } from "@/components/layout/AppShell";
import { ProtectedRoute } from "@/components/layout/ProtectedRoute";
import { LoginPage } from "@/components/auth/LoginPage";
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

export default function App() {
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
          <Route path="/roles/*" element={<RolesPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/system" element={<SystemPage />} />
          <Route path="/screens" element={<ScreensPage />} />
        </Route>
      </Route>
    </Routes>
  );
}
