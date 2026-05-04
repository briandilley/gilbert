/**
 * Plugin UI extension registry.
 *
 * Plugins ship a side-effect-import file under
 * ``frontend/src/plugins/<name>/panels.ts`` that calls
 * ``registerPanel(panelId, Component)`` for each panel they contribute
 * to a named slot. The Account / Settings / etc. pages declare their
 * extension slots with ``<PluginPanelSlot slot="...">``; the slot
 * fetches ``ui.panels.list`` from the backend, matches by panel_id,
 * and renders whatever components are registered.
 *
 * Adding a new plugin's UI is a purely additive change: drop a new
 * directory under ``src/plugins/``, register your components, and
 * import the side-effect file from ``src/plugins/index.ts``. No core
 * page ever imports a plugin's component directly.
 */

import type { ComponentType } from "react";

const _registry = new Map<string, ComponentType>();

export function registerPanel(panelId: string, Component: ComponentType): void {
  if (_registry.has(panelId)) {
    // Re-registration is a sign of a duplicated import; warn but keep
    // the most recent registration.
    console.warn(`Plugin panel "${panelId}" registered twice — keeping the latest.`);
  }
  _registry.set(panelId, Component);
}

export function getPanel(panelId: string): ComponentType | null {
  return _registry.get(panelId) ?? null;
}

export function listRegisteredPanels(): string[] {
  return [..._registry.keys()];
}
