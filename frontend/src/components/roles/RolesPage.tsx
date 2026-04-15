import { Routes, Route, Navigate } from "react-router-dom";
import { RolesList } from "./RolesList";
import { ToolPermissions } from "./ToolPermissions";
import { AIProfiles } from "./AIProfiles";
import { UserRoles } from "./UserRoles";
import { CollectionACLs } from "./CollectionACLs";
import { EventVisibility } from "./EventVisibility";
import { RpcPermissions } from "./RpcPermissions";

export function RolesPage() {
  return (
    <div className="p-4 sm:p-6 space-y-4 sm:space-y-6 max-w-4xl mx-auto">
      <Routes>
        <Route index element={<Navigate to="/security/users" replace />} />
        <Route path="users" element={<UserRoles />} />
        <Route path="roles" element={<RolesList />} />
        <Route path="tools" element={<ToolPermissions />} />
        <Route path="profiles" element={<AIProfiles />} />
        <Route path="collections" element={<CollectionACLs />} />
        <Route path="events" element={<EventVisibility />} />
        <Route path="rpc" element={<RpcPermissions />} />
      </Routes>
    </div>
  );
}
