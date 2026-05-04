# Plugin UI Extensions

## Summary
Generic mechanism for plugins to contribute SPA components, full pages, navigation entries, and dashboard cards — without core ever importing a single plugin module. Plugins can also declare named slots inside their own pages so other plugins can extend them. Components are registered by ``panel_id`` in a per-plugin side-effect file under ``<plugin>/frontend/panels.ts``; backend declares where they go (``ui_panels``, ``ui_routes``, ``nav_contributions``, ``dashboard_cards``).

## Details

### Backend dataclasses (all in ``src/gilbert/interfaces/plugin.py``)
- ``UIPanel(panel_id, slot, label, description, required_role)`` — a component to mount into a named slot on someone else's page.
- ``UIRoute(path, panel_id, label, description, icon, required_role, add_to_nav, nav_parent_group, show_in_dashboard)`` — a full SPA page. Optionally synthesizes a nav entry and/or dashboard card.
- ``NavContribution(label, url, action, icon, description, parent_group, required_role, requires_capability)`` — a nav-bar item with no associated route. Either ``url`` or ``action`` must be set. ``parent_group`` slots under an existing top-level group's key (``"system"``, ``"security"``, ``"mcp"``, …) or creates a new top-level group when blank/unknown.
- ``DashboardCard(title, description, url, icon, required_role, requires_capability)`` — a card on the ``/`` landing page.

### Plugin hooks (all default ``[]``)
- ``Plugin.ui_panels() -> list[UIPanel]``
- ``Plugin.ui_routes() -> list[UIRoute]``
- ``Plugin.nav_contributions() -> list[NavContribution]``
- ``Plugin.dashboard_cards() -> list[DashboardCard]``

### Backend WS RPCs
- ``ui.panels.list`` (in ``plugin_manager.py``) — walks every loaded plugin's ``ui_panels()``, filters by optional ``slot`` query and the caller's role.
- ``ui.routes.list`` (in ``web_api.py``) — same pattern for ``ui_routes()``. SPA queries this once on boot to inject ``<Route>`` elements.
- ``dashboard.get`` (in ``web_api.py``) — already merges every plugin's ``ui_routes()`` (``add_to_nav``/``show_in_dashboard``) and ``nav_contributions()`` and ``dashboard_cards()`` into the hardcoded core nav before role/capability filtering. So nav additions don't need a separate RPC.
- ACL: ``ui.panels.`` and ``ui.routes.`` prefixes at user level. Per-entry role filtering inside each handler.

### Frontend
- ``frontend/src/lib/plugin-panels.ts`` — module-level Map keyed by ``panel_id``. ``registerPanel(id, Component)`` and ``getPanel(id)``. Used for both panels AND routes (a route is functionally an "anywhere-mountable page").
- ``frontend/src/components/PluginPanelSlot.tsx`` — ``useQuery(["ui-panels", slot], () => api.listUIPanels(slot))``, then for each entry looks up ``getPanel(panel_id)`` and renders it. Skips panels with no registered component.
- ``frontend/src/components/PluginRoutes.tsx`` — ``usePluginRouteElements()`` hook returns an array of ``<Route>`` elements built from ``ui.routes.list``; ``App.tsx`` spreads them inside its top-level ``<Routes>`` block.
- ``frontend/src/plugins/index.ts`` — ``import.meta.glob`` auto-loader that pulls every ``<plugin>/frontend/panels.ts`` (and ``.tsx``) under ``std-plugins``, ``local-plugins``, ``installed-plugins``. Side-effect imports populate the registry at SPA boot before any page mounts.
- ``main.tsx`` imports ``@/plugins`` once so the auto-loader runs.
- ``frontend/src/hooks/useWsApi.ts`` exposes ``listUIPanels(slot?)`` and ``listUIRoutes()``.

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

Per-user / per-admin pages:
- ``account.extensions`` — per-user Account page (``/account``).
- ``settings.<category>`` — admin Settings page, scoped to a config category. Mount additional admin UI under your ``config_category``.

Top nav bar:
- ``header.widgets`` — between the connection indicator and the notification bell. Live-status widgets (sync indicator, queue depth, …).
- ``header.user-menu`` — items in the avatar dropdown. Wrap each in a ``<DropdownMenuItem>``. OAuth connect / disconnect, identity-bound links.

Dashboard (``/``):
- ``dashboard.top`` — banner widgets above the card grid.
- ``dashboard.bottom`` — long-form widgets below the grid.

Chat (``/chat``):
- ``chat.sidebar.bottom`` — bottom of the conversations sidebar. Now-playing music, presence, doorbell history.
- ``chat.input.toolbar`` — a strip above the chat input. Quick actions (share to slack, attach now-playing track, …).

Agent chat (``/agents``):
- ``agent.sidebar.bottom`` — bottom of the goals sidebar.
- ``agent.composer.toolbar`` — strip above the per-goal composer. Per-goal quick actions; workspace browse goes here.

Documents (``/documents``):
- ``documents.toolbar`` — alongside the search box. "Import from <source>" / "Sync now" buttons.

Plugins are also free to declare slots **inside their own pages** (e.g. ``browser.sessions.actions``) so other plugins can extend them. Just drop a ``<PluginPanelSlot slot="my-plugin.foo">`` somewhere; any other plugin can register components targeting that slot. Core never has to know.

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
