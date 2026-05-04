/**
 * PluginPanelSlot — render every plugin-contributed panel for a slot.
 *
 * Pages drop ``<PluginPanelSlot slot="account.extensions" />`` (or any
 * other slot name) wherever they want to allow plugins to inject UI.
 * Backend's ``ui.panels.list`` returns the panels visible to the
 * calling user; we look each one up in the side-effect-registered
 * component registry and render it. Panels whose ``panel_id`` isn't
 * registered in the SPA bundle are silently skipped — this happens when
 * a plugin is loaded on the backend but its frontend panels module
 * wasn't imported from ``frontend/src/plugins/index.ts``.
 */

import { useQuery } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { getPanel } from "@/lib/plugin-panels";

interface Props {
  slot: string;
}

export function PluginPanelSlot({ slot }: Props) {
  const api = useWsApi();
  const { connected } = useWebSocket();
  const { data } = useQuery({
    queryKey: ["ui-panels", slot],
    queryFn: () => api.listUIPanels(slot),
    enabled: connected,
  });

  const panels = data?.panels ?? [];
  if (panels.length === 0) return null;

  return (
    <>
      {panels.map((p) => {
        const Component = getPanel(p.panel_id);
        if (!Component) return null;
        return <Component key={p.panel_id} />;
      })}
    </>
  );
}
