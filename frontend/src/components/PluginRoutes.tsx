/**
 * PluginRoutes — render every plugin-contributed React Router Route.
 *
 * Mounted from App.tsx alongside the built-in routes. Backend's
 * ``ui.routes.list`` returns the routes visible to the calling user
 * (filtered by required_role). For each, we look up the registered
 * React component by ``panel_id`` and emit a ``<Route>`` that mounts
 * it. Routes whose ``panel_id`` isn't registered in the SPA bundle
 * are silently skipped — happens when a plugin is loaded backend-only
 * without its frontend panels module.
 *
 * Plugin authors register the route's component the same way they
 * register a panel:
 *
 *   // <plugin>/frontend/panels.ts
 *   import { registerPanel } from "@/lib/plugin-panels";
 *   import { MyPage } from "./MyPage";
 *   registerPanel("myplugin.page", MyPage);
 *
 * And declare the route on the Python side:
 *
 *   UIRoute(path="/myplugin", panel_id="myplugin.page",
 *           label="My plugin", icon="package",
 *           add_to_nav=True, show_in_dashboard=True)
 */

import { Route } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { getPanel } from "@/lib/plugin-panels";

export function usePluginRoutes() {
  const api = useWsApi();
  const { connected } = useWebSocket();
  return useQuery({
    queryKey: ["ui-routes"],
    queryFn: () => api.listUIRoutes(),
    enabled: connected,
    // Routes don't change at runtime within a session — once on boot
    // is plenty. Refresh on reconnect though, in case the user
    // installed a plugin in another tab.
    staleTime: 5 * 60_000,
  });
}

/**
 * Render an array of <Route> children, one per plugin-contributed
 * route whose component is registered. Use as
 * ``{usePluginRouteElements()}`` inside a Routes block.
 */
export function usePluginRouteElements() {
  const { data } = usePluginRoutes();
  const routes = data?.routes ?? [];
  return routes.flatMap((r) => {
    const Component = getPanel(r.panel_id);
    if (!Component) return [];
    return [
      <Route key={r.path} path={r.path} element={<Component />} />,
    ];
  });
}
