"""Web API service — dashboard, system inspector, and entity browser WS handlers.

Thin service that owns WebSocket RPC handlers for cross-cutting web UI
endpoints that don't belong to a single domain service (dashboard cards,
service inspector, entity browser).
"""

import logging
from typing import Any

from gilbert.interfaces.screens import GuestScreenPolicy
from gilbert.interfaces.service import Service, ServiceEnumerator, ServiceInfo, ServiceResolver

logger = logging.getLogger(__name__)


def _merge_plugin_nav(gilbert: Any, nav_groups: list[dict[str, Any]]) -> None:
    """Mutate ``nav_groups`` in place to include plugin contributions.

    Two sources from each loaded plugin:

    - ``ui_routes()`` entries with ``add_to_nav=True`` — add a leaf
      pointing at ``r.path``.
    - ``nav_contributions()`` — explicit nav items, with no associated
      route.

    Each item slots under ``parent_group`` (or ``nav_parent_group``
    on a route) when that key matches an existing top-level group;
    otherwise we create a new top-level group at the end of the list.
    """
    by_key: dict[str, dict[str, Any]] = {g["key"]: g for g in nav_groups}

    def _append(parent_key: str, item: dict[str, Any], *, group_label: str = "", group_icon: str = "") -> None:
        if parent_key and parent_key in by_key:
            by_key[parent_key].setdefault("items", []).append(item)
            return
        # New top-level group named after the plugin's chosen key.
        # When parent_key is blank we synthesize one from the item
        # label (lowercased). The caller's group_label/group_icon
        # control how the new group displays.
        new_key = parent_key or item["label"].lower().replace(" ", "-")
        if new_key in by_key:
            by_key[new_key].setdefault("items", []).append(item)
            return
        new_group = {
            "key": new_key,
            "label": group_label or item["label"],
            "description": item.get("description", ""),
            "url": item.get("url", ""),
            "icon": group_icon or item.get("icon", ""),
            "required_role": item.get("required_role", "user"),
            "items": [],  # leaf
        }
        # Propagate gating fields onto the synthesized group so the
        # leaf-branch of ``_visible_async`` (which inspects the group
        # itself when ``items`` is empty) can hide the entry when the
        # underlying service is missing / disabled. Without this, a
        # plugin route whose owning service was toggled off in Settings
        # → Services still left a dead top-level nav entry that landed
        # on a blank page (the route is gated, the nav is not).
        for gating in ("requires_capability", "requires_user_health_data"):
            if gating in item:
                new_group[gating] = item[gating]
        nav_groups.append(new_group)
        by_key[new_key] = new_group

    for entry in gilbert.list_loaded_plugins():
        plugin = entry.plugin
        try:
            routes = plugin.ui_routes()
        except Exception:
            logger.exception(
                "ui_routes() raised on %s", plugin.metadata().name
            )
            routes = []
        for r in routes:
            if not r.add_to_nav:
                continue
            item: dict[str, Any] = {
                "label": r.label or r.path,
                "description": r.description,
                "url": r.path,
                "icon": r.icon,
                "required_role": r.required_role,
            }
            if r.requires_capability:
                # ``_visible`` reads this and hides the nav item when
                # the underlying service is missing / disabled, so a
                # toggled-off plugin doesn't render dead nav entries.
                item["requires_capability"] = r.requires_capability
            _append(r.nav_parent_group, item)

        try:
            contribs = plugin.nav_contributions()
        except Exception:
            logger.exception(
                "nav_contributions() raised on %s", plugin.metadata().name
            )
            contribs = []
        for c in contribs:
            item: dict[str, Any] = {
                "label": c.label,
                "description": c.description,
                "icon": c.icon,
                "required_role": c.required_role,
            }
            if c.url:
                item["url"] = c.url
            elif c.action:
                item["action"] = c.action
            if c.requires_capability:
                item["requires_capability"] = c.requires_capability
            _append(c.parent_group, item)


def _visible_plugin_cards(
    gilbert: Any,
    visible_fn: Any,
) -> list[dict[str, Any]]:
    """Collect dashboard cards from plugin ``dashboard_cards()`` and
    from routes with ``show_in_dashboard=True``. Filtered by the same
    role/capability rule used for nav."""
    out: list[dict[str, Any]] = []
    for entry in gilbert.list_loaded_plugins():
        plugin = entry.plugin
        try:
            cards = plugin.dashboard_cards()
        except Exception:
            logger.exception(
                "dashboard_cards() raised on %s", plugin.metadata().name
            )
            cards = []
        for c in cards:
            row: dict[str, Any] = {
                "title": c.title,
                "description": c.description,
                "url": c.url,
                "icon": c.icon,
                "required_role": c.required_role,
            }
            if c.requires_capability:
                row["requires_capability"] = c.requires_capability
            if visible_fn(row):
                out.append({k: v for k, v in row.items() if k != "requires_capability"})

        try:
            routes = plugin.ui_routes()
        except Exception:
            logger.exception(
                "ui_routes() raised on %s", plugin.metadata().name
            )
            routes = []
        for r in routes:
            if not r.show_in_dashboard:
                continue
            row = {
                "title": r.label or r.path,
                "description": r.description,
                "url": r.path,
                "icon": r.icon,
                "required_role": r.required_role,
            }
            if r.requires_capability:
                row["requires_capability"] = r.requires_capability
            if visible_fn(row):
                out.append({k: v for k, v in row.items() if k != "requires_capability"})
    return out


class WebApiService(Service):
    """Provides dashboard, system, and entity browser WS handlers.

    Capabilities: ws_handlers
    """

    def __init__(self) -> None:
        self._resolver: ServiceResolver | None = None

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="web_api",
            capabilities=frozenset({"ws_handlers"}),
            requires=frozenset({"entity_storage"}),
            optional=frozenset({"access_control", "configuration"}),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver
        logger.info("WebApiService started")

    async def stop(self) -> None:
        pass

    # --- WebSocket RPC handlers ---

    def get_ws_handlers(self) -> dict[str, Any]:
        return {
            "dashboard.get": self._ws_dashboard_get,
            "system.services.list": self._ws_system_list,
            "entities.collection.list": self._ws_entities_list,
            "entities.collection.query": self._ws_entities_query,
            "entities.entity.get": self._ws_entity_get,
            "ui.routes.list": self._ws_ui_routes_list,
            "prompts.contributions.list": self._ws_prompt_contributions_list,
        }

    async def _ws_dashboard_get(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        gilbert = conn.manager.gilbert
        if gilbert is None:
            return {
                "type": "dashboard.get.result",
                "ref": frame.get("id"),
                "cards": [],
                "nav": [],
            }

        # The Screens nav item widens to ``everyone`` when guest screens are
        # allowed — local guests (who only get nav when the global guest
        # policy is on) can then reach screen setup. Otherwise it's a normal
        # logged-in entry. ``requires_capability`` still hides it when the
        # service is disabled.
        screens_svc = gilbert.service_manager.get_by_capability("screen_display")
        screens_role = (
            "everyone"
            if isinstance(screens_svc, GuestScreenPolicy) and screens_svc.allow_guest_screens
            else "user"
        )

        # Menu structure: each top-level entry is either a leaf (no
        # ``items``) or a group with child items. The group's ``url``
        # is the default destination when the group label is clicked;
        # typically this points at the first child. Each item
        # declares ``required_role`` and an optional
        # ``requires_capability`` — filtered below against the
        # current user's role level and whether the capability's
        # service is actually enabled. A group whose every child is
        # filtered out disappears entirely. See
        # ``frontend/src/components/layout/NavBar.tsx`` for how the
        # frontend consumes this.
        nav_groups: list[dict[str, Any]] = [
            {
                "key": "chat",
                "label": "Chat",
                "description": "Talk with Gilbert",
                "url": "/chat",
                "icon": "message-square",
                "required_role": "everyone",
                "items": [],
            },
            {
                "key": "agents",
                "label": "Agents",
                "description": "Autonomous agents — chat-style view per goal",
                "url": "/agents",
                "icon": "sparkles",
                "required_role": "user",
                "requires_capability": "agent",
                "items": [],
            },
            {
                "key": "goals",
                "label": "Goals",
                "description": "Multi-agent goals with kanban + war rooms",
                "url": "/goals",
                "icon": "target",
                "required_role": "user",
                "requires_capability": "agent",
                "items": [],
            },
            {
                "key": "inbox",
                "label": "Inbox",
                "description": "Email management",
                "url": "/inbox",
                "icon": "inbox",
                "required_role": "admin",
                "requires_capability": "email",
                "items": [],
            },
            {
                "key": "calendar",
                "label": "Calendar",
                "description": "Calendar accounts, events, and free/busy",
                "url": "/calendar",
                "icon": "calendar",
                "required_role": "user",
                "requires_capability": "calendar",
                "items": [],
            },
            {
                "key": "feeds",
                "label": "Feeds",
                "description": "RSS / news feeds, scoring, and the daily briefing",
                "url": "/feeds",
                "icon": "rss",
                "required_role": "user",
                "requires_capability": "feeds",
                "items": [],
            },
            {
                "key": "tasks",
                "label": "Tasks",
                "description": "Task lists, due work, and backlog triage",
                "url": "/tasks",
                "icon": "list-todo",
                "required_role": "user",
                "requires_capability": "tasks",
                "items": [],
            },
            {
                # Health nav is conditionally visible — per spec §17.3
                # users who haven't connected a backend don't need the
                # menu item. The dashboard.get handler runs a per-user
                # ``health_links`` count for connections that satisfy
                # the capability + role gate.
                "key": "health",
                "label": "Health",
                "description": "Personal health metrics and connected sources",
                "url": "/health",
                "icon": "heart-pulse",
                "required_role": "user",
                "requires_capability": "health",
                "requires_user_health_data": True,
                "items": [],
            },
            {
                "key": "knowledge",
                "label": "Knowledge",
                "description": "Browse, search, and manage indexed documents",
                "url": "/documents",
                "icon": "file-text",
                "required_role": "user",
                "requires_capability": "knowledge",
                "items": [],
            },
            {
                # Parent for plugin-contributed media sources (radio,
                # music libraries, etc.). Has no built-in children;
                # ``items`` is filled entirely by plugin
                # ``ui_routes(... nav_parent_group="media")`` entries.
                # ``placeholder_group`` tells the visibility filter to
                # drop the group if no plugin populated it — otherwise
                # an empty-items entry would render as a no-op leaf.
                "key": "media",
                "label": "Media",
                "description": "Listen to radio, music, and other audio sources",
                "url": "",
                "icon": "headphones",
                "required_role": "user",
                "items": [],
                "placeholder_group": True,
            },
            {
                "key": "mcp",
                "label": "MCP",
                "description": "Model Context Protocol",
                "url": "/mcp/servers",
                "icon": "plug",
                "required_role": "user",
                "items": [
                    {
                        "label": "Servers",
                        "description": "MCP servers Gilbert connects to",
                        "url": "/mcp/servers",
                        "icon": "plug",
                        "required_role": "user",
                        "requires_capability": "mcp",
                    },
                    {
                        "label": "Clients",
                        "description": "Bearer tokens for external MCP clients",
                        "url": "/mcp/clients",
                        "icon": "plug-zap",
                        "required_role": "admin",
                        "requires_capability": "mcp_server",
                    },
                    {
                        "label": "Local",
                        "description": (
                            "MCP servers running on your own machine, "
                            "bridged through this browser tab"
                        ),
                        "url": "/mcp/local",
                        "icon": "plug",
                        "required_role": "user",
                        "requires_capability": "mcp",
                    },
                ],
            },
            {
                "key": "security",
                "label": "Security",
                "description": "Users, roles & access control",
                "url": "/security/users",
                "icon": "shield",
                "required_role": "admin",
                "items": [
                    {
                        "label": "Users",
                        "description": "User accounts & role assignments",
                        "url": "/security/users",
                        "icon": "users",
                        "required_role": "admin",
                    },
                    {
                        "label": "Roles",
                        "description": "Role definitions & hierarchy",
                        "url": "/security/roles",
                        "icon": "shield",
                        "required_role": "admin",
                    },
                    {
                        "label": "Tools",
                        "description": "Per-tool role requirements",
                        "url": "/security/tools",
                        "icon": "wrench",
                        "required_role": "admin",
                    },
                    {
                        "label": "AI Profiles",
                        "description": "Named AI tool allowlists",
                        "url": "/security/profiles",
                        "icon": "sparkles",
                        "required_role": "admin",
                    },
                    {
                        "label": "Model Config",
                        "description": "Per-model generation defaults & enabled toggle",
                        "url": "/security/model-config",
                        "icon": "sliders",
                        "required_role": "admin",
                    },
                    {
                        "label": "Collections",
                        "description": "Per-collection ACLs",
                        "url": "/security/collections",
                        "icon": "folder-lock",
                        "required_role": "admin",
                    },
                    {
                        "label": "Events",
                        "description": "Per-event visibility",
                        "url": "/security/events",
                        "icon": "radio",
                        "required_role": "admin",
                    },
                    {
                        "label": "RPC",
                        "description": "Per-RPC-method permissions",
                        "url": "/security/rpc",
                        "icon": "terminal",
                        "required_role": "admin",
                    },
                ],
            },
            {
                "key": "system",
                "label": "System",
                "description": "Configuration & operations",
                "url": "/settings",
                "icon": "settings",
                "required_role": "admin",
                "items": [
                    {
                        "label": "Settings",
                        "description": "Service configuration",
                        "url": "/settings",
                        "icon": "sliders",
                        "required_role": "admin",
                    },
                    {
                        "label": "Scheduler",
                        "description": "Timers & scheduled jobs",
                        "url": "/scheduler",
                        "icon": "clock",
                        "required_role": "user",
                    },
                    {
                        "label": "Entities",
                        "description": "Raw entity storage browser",
                        "url": "/entities",
                        "icon": "database",
                        "required_role": "admin",
                    },
                    {
                        "label": "Usage",
                        "description": "AI token usage + cost reporting",
                        "url": "/usage",
                        "icon": "bar-chart",
                        "required_role": "admin",
                        "requires_capability": "usage_reporting",
                    },
                    {
                        "label": "Plugins",
                        "description": "Install & manage plugins",
                        "url": "/plugins",
                        "icon": "package",
                        "required_role": "admin",
                    },
                    {
                        "label": "Proposals",
                        "description": "Gilbert's autonomous improvement ideas",
                        "url": "/proposals",
                        "icon": "sparkles",
                        "required_role": "admin",
                        "requires_capability": "proposals",
                    },
                    {
                        "label": "Browser",
                        "description": "Service inspector",
                        "url": "/system",
                        "icon": "monitor",
                        "required_role": "admin",
                    },
                    {
                        "label": "Presence",
                        "description": "Map detected presence signals to users",
                        "url": "/presence",
                        "icon": "user-check",
                        "required_role": "admin",
                        "requires_capability": "presence",
                    },
                    {
                        "label": "Screens",
                        "description": "Remote display screens",
                        "url": "/screens",
                        "icon": "monitor",
                        "required_role": screens_role,
                        "requires_capability": "screen_display",
                    },
                    {
                        "label": "Restart",
                        "description": "Restart the Gilbert host process",
                        "icon": "rotate-ccw",
                        "required_role": "admin",
                        "action": "restart_host",
                    },
                ],
            },
        ]

        # Merge plugin contributions before role/capability filtering so
        # plugin items see exactly the same gating logic as core entries.
        # Two paths:
        #   - Routes with ``add_to_nav=True`` slot under their declared
        #     ``nav_parent_group`` (or create a new top-level group if
        #     blank).
        #   - Standalone NavContribution entries do the same, without
        #     binding to a route.
        _merge_plugin_nav(gilbert, nav_groups)

        # Pin the System group to the last position. Plugin-contributed
        # top-level groups are appended by ``_merge_plugin_nav``, which
        # would otherwise push System above them.
        for i, g in enumerate(nav_groups):
            if g.get("key") == "system":
                nav_groups.append(nav_groups.pop(i))
                break

        acl = gilbert.service_manager.get_by_capability("access_control")
        sm = gilbert.service_manager

        # Resolve "does this user have any health_links rows" once per
        # request. Per spec §17.3 the /health nav entry only renders
        # for users who've connected at least one backend (regardless
        # of ``enabled``); the SPA's empty-state covers the others.
        user_has_health_data: bool | None = None

        async def _user_has_health_data() -> bool:
            nonlocal user_has_health_data
            if user_has_health_data is not None:
                return user_has_health_data
            user_has_health_data = False
            health_svc = sm.get_by_capability("health")
            # Resolve via the public ``HealthLinkProvider`` protocol so
            # the question "does this user have any health_links?" is
            # service-owned, not raw storage access from the route. The
            # service ACL-gates the underlying read; the protocol keeps
            # the layer rules clean and the implementation swappable.
            from gilbert.interfaces.health import HealthLinkProvider
            if health_svc is not None and isinstance(health_svc, HealthLinkProvider):
                try:
                    user_has_health_data = await health_svc.user_has_active_links(
                        conn.user_ctx.user_id
                    )
                except Exception:
                    user_has_health_data = False
            return user_has_health_data

        def _visible(entry: dict[str, Any]) -> bool:
            cap = entry.get("requires_capability")
            if cap:
                svc = sm.get_by_capability(cap)
                if svc is None or not svc.enabled:
                    return False
            if acl is not None:
                required_level = acl.get_role_level(
                    entry.get("required_role", "admin"),
                )
                if conn.user_level > required_level:
                    return False
            return True

        async def _visible_async(entry: dict[str, Any]) -> bool:
            if not _visible(entry):
                return False
            if entry.get("requires_user_health_data"):
                if not await _user_has_health_data():
                    return False
            return True

        visible_nav: list[dict[str, Any]] = []
        for group in nav_groups:
            raw_items = group.get("items") or []
            visible_items: list[dict[str, Any]] = []
            for it in raw_items:
                if await _visible_async(it):
                    visible_items.append(
                        {
                            k: v
                            for k, v in it.items()
                            if k
                            not in (
                                "requires_capability",
                                "requires_user_health_data",
                            )
                        }
                    )
            if raw_items:
                # Group: hide if every child was filtered out.
                if not visible_items:
                    continue
                # Fall back to the first visible navigable child's URL
                # when the hard-coded default is unreachable for this
                # user. Action items (which have no ``url``) are skipped
                # — a group can't default-land on an RPC trigger.
                default_url = group["url"]
                navigable_urls = {i["url"] for i in visible_items if i.get("url")}
                if default_url not in navigable_urls:
                    default_url = next(
                        (i["url"] for i in visible_items if i.get("url")),
                        default_url,
                    )
                visible_nav.append(
                    {
                        "key": group["key"],
                        "label": group["label"],
                        "description": group.get("description", ""),
                        "url": default_url,
                        "icon": group.get("icon", ""),
                        "items": visible_items,
                    }
                )
            elif group.get("placeholder_group"):
                # Plugin-extension parent (e.g. Media). Hide entirely
                # when no plugin contributed a child — without this,
                # the empty-items entry would render as a dead leaf.
                continue
            else:
                # Leaf: filter by its own role/capability + per-user data.
                if not await _visible_async(group):
                    continue
                visible_nav.append(
                    {
                        "key": group["key"],
                        "label": group["label"],
                        "description": group.get("description", ""),
                        "url": group["url"],
                        "icon": group.get("icon", ""),
                        "items": [],
                    }
                )

        # Flat ``cards`` list preserved for the dashboard view.
        # Dashboard shows one card per top-level entry (leaves and
        # groups both) — clicking a group card lands on its default
        # URL — plus any extra cards plugins contributed.
        cards = [
            {
                "title": g["label"],
                "description": g["description"],
                "url": g["url"],
                "icon": g["icon"],
                "required_role": "everyone",
            }
            for g in visible_nav
        ]
        cards.extend(_visible_plugin_cards(gilbert, _visible))

        # The TopBar's <BrowserSpeakerControl/> needs a signal to hide
        # itself when the ``browser`` speaker backend isn't loaded — its
        # activate/deactivate RPCs would no-op anyway. Surface a simple
        # boolean rather than leaking the full backend list to every nav
        # consumer.
        speaker_svc = sm.get_by_capability("speaker_control")
        browser_speaker_available = (
            speaker_svc is not None
            and speaker_svc.enabled
            and getattr(speaker_svc, "backends", {}).get("browser") is not None
        )

        return {
            "type": "dashboard.get.result",
            "ref": frame.get("id"),
            "cards": cards,
            "nav": visible_nav,
            "browser_speaker_available": browser_speaker_available,
        }

    async def _ws_ui_routes_list(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        """Return the routes plugins contribute, role-filtered.

        SPA's ``<PluginRoutes />`` queries this once at boot and
        injects a ``<Route path={path} element={<Component/>}/>`` per
        entry — components are looked up by ``panel_id`` in the
        registered-component registry.
        """
        gilbert = conn.manager.gilbert
        if gilbert is None:
            return {"type": "ui.routes.list.result", "ref": frame.get("id"), "routes": []}
        sm = gilbert.service_manager
        acl = sm.get_by_capability("access_control")
        caller_level = getattr(conn, "user_level", 200)

        def _level_for(role: str) -> int:
            if acl is not None and hasattr(acl, "get_role_level"):
                try:
                    return acl.get_role_level(role)
                except Exception:
                    pass
            return {"admin": 0, "user": 100, "anonymous": 200}.get(role, 100)

        def _capability_live(cap: str) -> bool:
            # Mirrors the nav-visibility check in ``_ws_dashboard_get``:
            # a route's capability is only "live" when a service
            # advertises it AND that service is enabled. Disabled
            # toggleable services keep their capability declared but
            # report ``svc.enabled == False`` — the route should
            # disappear with them.
            if not cap:
                return True
            svc = sm.get_by_capability(cap)
            return svc is not None and svc.enabled

        out: list[dict[str, Any]] = []
        for entry in gilbert.list_loaded_plugins():
            try:
                routes = entry.plugin.ui_routes()
            except Exception:
                logger.exception(
                    "ui_routes() raised on %s", entry.plugin.metadata().name
                )
                continue
            for r in routes:
                if caller_level > _level_for(r.required_role):
                    continue
                if not _capability_live(r.requires_capability):
                    continue
                out.append(
                    {
                        "path": r.path,
                        "panel_id": r.panel_id,
                        "label": r.label,
                        "description": r.description,
                        "icon": r.icon,
                        "plugin": entry.plugin.metadata().name,
                    }
                )
        return {
            "type": "ui.routes.list.result",
            "ref": frame.get("id"),
            "routes": out,
        }

    async def _ws_prompt_contributions_list(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        """Return every PromptFragment contributed for ``target``.

        Settings UI calls this for each ConfigParam with
        ``extensible_target`` set so it can show the live list of
        currently-contributing fragments.
        """
        gilbert = conn.manager.gilbert
        target = str(frame.get("target") or "").strip()
        if gilbert is None or not target:
            return {
                "type": "prompts.contributions.list.result",
                "ref": frame.get("id"),
                "fragments": [],
            }

        from gilbert.interfaces.prompts import SystemPromptContributor

        sm = gilbert.service_manager
        out: list[dict[str, Any]] = []
        for svc in sm.get_all_by_capability("system_prompt_contributor"):
            if not isinstance(svc, SystemPromptContributor):
                continue
            try:
                fragments = svc.get_prompt_fragments()
            except Exception:
                logger.exception(
                    "get_prompt_fragments() raised on %s", type(svc).__name__
                )
                continue
            # Best-effort source-service name for display purposes.
            source_name = ""
            try:
                source_name = svc.service_info().name  # type: ignore[attr-defined]
            except Exception:
                source_name = type(svc).__name__
            for f in fragments:
                if f.target != target:
                    continue
                out.append(
                    {
                        "fragment_id": f.fragment_id,
                        "target": f.target,
                        "label": f.label,
                        "description": f.description,
                        "body": f.body,
                        "enabled": f.enabled,
                        "source_service": source_name,
                    }
                )
        return {
            "type": "prompts.contributions.list.result",
            "ref": frame.get("id"),
            "fragments": out,
        }

    async def _ws_system_list(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:

        gilbert = conn.manager.gilbert
        if gilbert is None:
            return {"type": "system.services.list.result", "ref": frame.get("id"), "services": []}

        from gilbert.interfaces.configuration import Configurable, ConfigurationReader
        from gilbert.interfaces.tools import ToolProvider

        sm = gilbert.service_manager
        config_svc = sm.get_by_capability("configuration")
        services = []

        if not isinstance(sm, ServiceEnumerator):
            return {"type": "system.services.list.result", "ref": frame.get("id"), "services": []}

        disabled_services = sm.disabled_services
        for name, svc in sm.list_services().items():
            info = svc.service_info()
            started = name in sm.started_services
            failed = name in sm.failed_services
            disabled_reason = disabled_services.get(name, "")

            entry: dict[str, Any] = {
                "name": info.name,
                "capabilities": sorted(info.capabilities),
                "requires": sorted(info.requires),
                "optional": sorted(info.optional),
                "ai_calls": sorted(info.ai_calls),
                "events": sorted(info.events),
                "started": started,
                "failed": failed,
                # Disabled by an unmet enablement dependency (ADR-0018):
                # not started, but deliberately — not a failure.
                "disabled": bool(disabled_reason),
                "disabled_reason": disabled_reason,
                "config_params": [],
                "config_values": {},
                "tools": [],
            }

            if isinstance(svc, Configurable):
                entry["config_namespace"] = svc.config_namespace
                try:
                    entry["config_params"] = [
                        {
                            "key": p.key,
                            "type": p.type.value,
                            "description": p.description,
                            "default": p.default,
                            "restart_required": p.restart_required,
                        }
                        for p in svc.config_params()
                    ]
                except Exception:
                    pass
                if isinstance(config_svc, ConfigurationReader):
                    try:
                        section = config_svc.get_section(svc.config_namespace)
                        # Ensure values are JSON-serializable
                        import json as _json

                        _json.dumps(section)
                        entry["config_values"] = section
                    except (TypeError, ValueError):
                        entry["config_values"] = {}

            if isinstance(svc, ToolProvider):
                entry["tools"] = [
                    {
                        "name": t.name,
                        "description": t.description,
                        "required_role": t.required_role,
                        "parameters": [
                            {
                                "name": p.name,
                                "type": p.type.value,
                                "description": p.description,
                                "required": p.required,
                            }
                            for p in t.parameters
                        ],
                    }
                    for t in svc.get_tools()
                ]

            services.append(entry)

        return {"type": "system.services.list.result", "ref": frame.get("id"), "services": services}

    async def _ws_entities_list(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:

        gilbert = conn.manager.gilbert
        if gilbert is None:
            return {"type": "entities.collection.list.result", "ref": frame.get("id"), "groups": []}

        storage_svc = gilbert.service_manager.get_by_capability("entity_storage")
        if storage_svc is None:
            return {"type": "entities.collection.list.result", "ref": frame.get("id"), "groups": []}

        from gilbert.interfaces.storage import Query as StorageQuery

        collections = await storage_svc.backend.list_collections()
        groups: dict[str, list[dict[str, Any]]] = {}
        for col in sorted(collections):
            parts = col.rsplit(".", 1)
            ns = parts[0] if len(parts) > 1 else "(default)"
            short = parts[-1]
            try:
                count = await storage_svc.backend.count(StorageQuery(collection=col))
            except Exception:
                count = 0
            groups.setdefault(ns, []).append({"name": col, "short_name": short, "count": count})

        result = [{"namespace": ns, "collections": cols} for ns, cols in groups.items()]
        return {"type": "entities.collection.list.result", "ref": frame.get("id"), "groups": result}

    async def _ws_entities_query(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:

        collection = frame.get("collection", "")
        if not collection:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": "collection required",
                "code": 400,
            }

        gilbert = conn.manager.gilbert
        if gilbert is None:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": "Storage not available",
                "code": 503,
            }

        storage_svc = gilbert.service_manager.get_by_capability("entity_storage")
        if storage_svc is None:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": "Storage not available",
                "code": 503,
            }

        from gilbert.interfaces.storage import Query, SortField

        page = int(frame.get("page", 1))
        sort_field = frame.get("sort", "_id")
        order = frame.get("order", "asc")
        page_size = 50
        offset = (page - 1) * page_size

        sort = [SortField(field=sort_field, descending=(order == "desc"))]
        entities = await storage_svc.backend.query(
            Query(
                collection=collection,
                sort=sort,
                limit=page_size,
                offset=offset,
            )
        )
        try:
            total = await storage_svc.backend.count(Query(collection=collection))
        except Exception:
            total = len(entities)
        total_pages = max(1, (total + page_size - 1) // page_size)

        # Derive sortable fields from first entity
        sortable_fields = []
        if entities:
            sortable_fields = sorted(entities[0].keys())

        fk_map: dict[str, Any] = {}

        # Build display columns: _id + indexed fields + FK fields

        display_columns: list[str] = ["_id"]
        try:
            indexes = await storage_svc.backend.list_indexes(collection)
            for idx in indexes:
                for field in idx.fields:
                    if field not in display_columns:
                        display_columns.append(field)
        except Exception:
            pass

        if isinstance(fk_map, dict):
            for field in fk_map:
                if field not in display_columns:
                    display_columns.append(field)

        return {
            "type": "entities.collection.query.result",
            "ref": frame.get("id"),
            "collection": collection,
            "entities": entities,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "sortable_fields": sortable_fields,
            "fk_map": fk_map,
            "display_columns": display_columns,
        }

    async def _ws_entity_get(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:

        collection = frame.get("collection", "")
        entity_id = frame.get("entity_id", "")
        if not collection or not entity_id:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": "collection and entity_id required",
                "code": 400,
            }

        gilbert = conn.manager.gilbert
        if gilbert is None:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": "Storage not available",
                "code": 503,
            }

        storage_svc = gilbert.service_manager.get_by_capability("entity_storage")
        if storage_svc is None:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": "Storage not available",
                "code": 503,
            }

        entity = await storage_svc.backend.get(collection, entity_id)
        if entity is None:
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": "Entity not found",
                "code": 404,
            }

        fk_map: dict[str, Any] = {}
        if hasattr(storage_svc.backend, "get_foreign_keys"):
            fk_map = await storage_svc.backend.get_foreign_keys(collection) or {}

        return {
            "type": "entities.entity.get.result",
            "ref": frame.get("id"),
            "collection": collection,
            "entity_id": entity_id,
            "entity": entity,
            "fk_map": fk_map,
        }
