# Plugin UI Extensions

## Summary
Generic mechanism for plugins to contribute SPA components into named slots without core knowing about any specific plugin. Backend declares panels via ``Plugin.ui_panels()``; frontend renders them via ``<PluginPanelSlot slot="…">`` after looking up registered React components by ``panel_id``.

## Details

### Backend
- ``UIPanel(panel_id, slot, label, description, required_role)`` in ``src/gilbert/interfaces/plugin.py``.
- ``Plugin.ui_panels() -> list[UIPanel]`` default-empty hook on the ``Plugin`` ABC.
- ``PluginManagerService.get_ws_handlers()`` includes ``ui.panels.list`` (in ``src/gilbert/core/services/plugin_manager.py``). The handler walks ``gilbert.list_loaded_plugins()``, calls ``ui_panels()`` on each, filters by:
  - optional ``slot`` parameter on the request frame, and
  - the calling user's role (resolved via ``access_control.get_role_level`` when available; falls back to a hardcoded ``{"admin":0,"user":100,"anonymous":200}`` table). A panel with ``required_role="admin"`` is omitted from the response when ``conn.user_level > 0``.
- ACL: ``ui.panels.`` prefix at user level (in ``src/gilbert/interfaces/acl.py``). Per-panel role filtering happens inside the handler.

### Frontend
- ``frontend/src/lib/plugin-panels.ts`` — module-level Map keyed by ``panel_id``. ``registerPanel(id, Component)`` and ``getPanel(id)``.
- ``frontend/src/components/PluginPanelSlot.tsx`` — `useQuery(["ui-panels", slot], () => api.listUIPanels(slot))`, then for each entry looks up ``getPanel(panel_id)`` and renders it. Skips panels with no registered component (plugin loaded backend-only).
- ``frontend/src/plugins/index.ts`` — ``import.meta.glob`` auto-loader that pulls every ``<plugin>/frontend/panels.ts`` (and ``.tsx``) under ``std-plugins``, ``local-plugins``, ``installed-plugins``. Side-effect imports populate the registry at SPA boot before any page mounts.
- ``main.tsx`` imports ``@/plugins`` once so the auto-loader runs.
- ``frontend/src/hooks/useWsApi.ts`` exposes ``listUIPanels(slot?)``.

### Per-plugin frontend layout
```
std-plugins/<name>/
    plugin.py
    plugin.yaml
    pyproject.toml
    frontend/
        types.ts            # plugin-local TS types
        api.ts              # plugin-local hook (e.g. useFooApi using rpc() from useWebSocket)
        FooPanel.tsx        # the React component
        panels.ts           # registerPanel("foo.bar", FooPanel)  — side-effect only
        styles.css          # plugin-scoped styles, if any
```
Plugin TS can import core helpers via the ``@/`` alias (e.g. ``@/components/ui/button``, ``@/hooks/useWebSocket``). Core never imports from a plugin's ``frontend/`` directory.

### Built-in slots
- ``account.extensions`` — per-user Account page (``/account``). Default for plugins that surface per-user UI (saved logins, OAuth tokens, …).
- ``settings.<category>`` — admin Settings page, scoped to a config category. Plugins that already declare a ``Configurable`` namespace + ``config_category`` can mount additional UI under their category by setting ``slot=f"settings.{category.lower()}"``.

Pages may declare more slots over time. The ``UIPanel`` dataclass is the source of truth.

### Vite / tsconfig wiring
- ``frontend/tsconfig.json`` includes ``"../std-plugins/*/frontend/**/*"`` so plugin TS gets type-checked.
- ``frontend/vite.config.ts`` sets ``server.fs.allow: [path.resolve(__dirname, "..")]`` so the dev server can read the plugin tree (one level above the Vite project root).

### Caveats
- Re-registering the same ``panel_id`` warns to the console and keeps the latest registration (helps catch a duplicated import path).
- A plugin that's loaded backend-only (e.g. installed at runtime without an updated SPA bundle) shows up in ``ui.panels.list`` but the slot silently skips it because ``getPanel(panel_id)`` returns ``null`` — graceful, no error.

## Related
- ``src/gilbert/interfaces/plugin.py`` (UIPanel + Plugin.ui_panels)
- ``src/gilbert/core/services/plugin_manager.py`` (_ws_ui_panels_list)
- ``frontend/src/lib/plugin-panels.ts``
- ``frontend/src/components/PluginPanelSlot.tsx``
- ``frontend/src/plugins/index.ts``
- ``std-plugins/browser/frontend/panels.ts`` (canonical example registration)
- ``std-plugins/CLAUDE.md`` "Plugin frontend" section
