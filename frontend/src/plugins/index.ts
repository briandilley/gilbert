/**
 * Plugin UI auto-loader.
 *
 * Every plugin under ``std-plugins/<name>/`` (or ``local-plugins`` /
 * ``installed-plugins``) that ships SPA components places them under
 * its own ``frontend/`` directory and ships a ``frontend/panels.ts``
 * (or any other side-effect file) that calls ``registerPanel(...)``.
 *
 * Vite's ``import.meta.glob`` walks the matching files at build time
 * and, with ``eager: true``, evaluates each one — so all plugin
 * panel registrations land in the registry before any page mounts a
 * ``<PluginPanelSlot>``.
 *
 * Adding a new plugin's UI is a purely additive change inside the
 * plugin's directory: drop ``<plugin>/frontend/panels.ts`` calling
 * ``registerPanel(panelId, Component)``. No edits to this file or any
 * core file are needed.
 */

// Globs are relative to this file: src/plugins/index.ts is at
// frontend/src/plugins/, so ``../../..`` lands at the repo root.
const _modules = import.meta.glob(
  [
    "../../../std-plugins/*/frontend/panels.ts",
    "../../../std-plugins/*/frontend/panels.tsx",
    "../../../local-plugins/*/frontend/panels.ts",
    "../../../local-plugins/*/frontend/panels.tsx",
    "../../../installed-plugins/*/frontend/panels.ts",
    "../../../installed-plugins/*/frontend/panels.tsx",
  ],
  { eager: true },
);

// Reference the imports so bundlers don't tree-shake them away. Vite
// already evaluates side-effect-import modules under ``eager: true``,
// so this is belt-and-suspenders.
void Object.keys(_modules).length;
