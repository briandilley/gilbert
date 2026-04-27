"""Web API service — dashboard, system inspector, and entity browser WS handlers.

Thin service that owns WebSocket RPC handlers for cross-cutting web UI
endpoints that don't belong to a single domain service (dashboard cards,
service inspector, entity browser).
"""

import logging
from typing import Any

from gilbert.interfaces.service import Service, ServiceEnumerator, ServiceInfo, ServiceResolver

logger = logging.getLogger(__name__)


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
                        "label": "Restart",
                        "description": "Restart the Gilbert host process",
                        "icon": "rotate-ccw",
                        "required_role": "admin",
                        "action": "restart_host",
                    },
                ],
            },
        ]

        acl = gilbert.service_manager.get_by_capability("access_control")
        sm = gilbert.service_manager

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

        visible_nav: list[dict[str, Any]] = []
        for group in nav_groups:
            raw_items = group.get("items") or []
            visible_items = [
                {k: v for k, v in it.items() if k != "requires_capability"}
                for it in raw_items
                if _visible(it)
            ]
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
            else:
                # Leaf: filter by its own role/capability.
                if not _visible(group):
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
        # URL.
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

        return {
            "type": "dashboard.get.result",
            "ref": frame.get("id"),
            "cards": cards,
            "nav": visible_nav,
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

        for name, svc in sm.list_services().items():
            info = svc.service_info()
            started = name in sm.started_services
            failed = name in sm.failed_services

            entry: dict[str, Any] = {
                "name": info.name,
                "capabilities": sorted(info.capabilities),
                "requires": sorted(info.requires),
                "optional": sorted(info.optional),
                "ai_calls": sorted(info.ai_calls),
                "events": sorted(info.events),
                "started": started,
                "failed": failed,
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
