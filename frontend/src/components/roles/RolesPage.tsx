/**
 * Security/roles routes. The actual top-level nav for these sub-routes
 * comes from the side-nav (Security group children); this component is
 * just the router shell. Each sub-page renders its own PageHeader.
 */

import { Routes, Route, Navigate } from "react-router-dom";
import { RolesList } from "./RolesList";
import { ToolPermissions } from "./ToolPermissions";
import { AIProfiles } from "./AIProfiles";
import { ModelConfigEditor } from "./ModelConfigEditor";
import { UserRoles } from "./UserRoles";
import { CollectionACLs } from "./CollectionACLs";
import { EventVisibility } from "./EventVisibility";
import { RpcPermissions } from "./RpcPermissions";
import { Subagents } from "./Subagents";

export function RolesPage() {
  return (
    <Routes>
      <Route index element={<Navigate to="/security/users" replace />} />
      <Route path="users" element={<UserRoles />} />
      <Route path="roles" element={<RolesList />} />
      <Route path="tools" element={<ToolPermissions />} />
      <Route path="profiles" element={<AIProfiles />} />
      <Route path="subagents" element={<Subagents />} />
      <Route path="model-config" element={<ModelConfigEditor />} />
      <Route path="collections" element={<CollectionACLs />} />
      <Route path="events" element={<EventVisibility />} />
      <Route path="rpc" element={<RpcPermissions />} />
    </Routes>
  );
}
