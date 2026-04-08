import { Routes, Route, Link, useLocation } from "react-router-dom";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { RolesList } from "./RolesList";
import { ToolPermissions } from "./ToolPermissions";
import { AIProfiles } from "./AIProfiles";
import { UserRoles } from "./UserRoles";
import { CollectionACLs } from "./CollectionACLs";
import { EventVisibility } from "./EventVisibility";
import { RpcPermissions } from "./RpcPermissions";

const TABS = [
  { value: "roles", label: "Roles", path: "/roles" },
  { value: "tools", label: "Tools", path: "/roles/tools" },
  { value: "profiles", label: "AI Profiles", path: "/roles/profiles" },
  { value: "users", label: "Users", path: "/roles/users" },
  { value: "collections", label: "Collections", path: "/roles/collections" },
  { value: "events", label: "Events", path: "/roles/events" },
  { value: "rpc", label: "RPC", path: "/roles/rpc" },
];

export function RolesPage() {
  const location = useLocation();
  const currentTab =
    TABS.find((t) => t.path === location.pathname)?.value || "roles";

  return (
    <div className="p-6 space-y-6 max-w-4xl mx-auto">
      <h1 className="text-2xl font-semibold text-center">Roles & Access</h1>

      <Tabs value={currentTab}>
        <TabsList>
          {TABS.map((tab) => (
            <Link key={tab.value} to={tab.path}>
              <TabsTrigger value={tab.value}>{tab.label}</TabsTrigger>
            </Link>
          ))}
        </TabsList>
      </Tabs>

      <Routes>
        <Route index element={<RolesList />} />
        <Route path="tools" element={<ToolPermissions />} />
        <Route path="profiles" element={<AIProfiles />} />
        <Route path="users" element={<UserRoles />} />
        <Route path="collections" element={<CollectionACLs />} />
        <Route path="events" element={<EventVisibility />} />
        <Route path="rpc" element={<RpcPermissions />} />
      </Routes>
    </div>
  );
}
