"""Web API service — dashboard, system inspector, and entity browser WS handlers.

Thin service that owns WebSocket RPC handlers for cross-cutting web UI
endpoints that don't belong to a single domain service (dashboard cards,
service inspector, entity browser).
"""

import logging
from typing import Any

from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver

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
        gilbert = conn.manager._gilbert
        if gilbert is None:
            return {"type": "dashboard.get.result", "ref": frame.get("id"), "cards": []}

        _DASHBOARD_CARDS = [
            {"title": "Chat", "description": "Talk with Gilbert", "url": "/chat", "icon": "message-square", "required_role": "everyone"},
            {"title": "Documents", "description": "Knowledge base", "url": "/documents", "icon": "file-text", "required_role": "user"},
            {"title": "Inbox", "description": "Email management", "url": "/inbox", "icon": "inbox", "required_role": "admin"},
            {"title": "Roles", "description": "Roles & access control", "url": "/roles", "icon": "shield", "required_role": "admin"},
            {"title": "Entities", "description": "Entity browser", "url": "/entities", "icon": "database", "required_role": "admin"},
            {"title": "System", "description": "Service inspector", "url": "/system", "icon": "settings", "required_role": "admin"},
        ]

        acl = gilbert.service_manager.get_by_capability("access_control")
        cards = []
        for card in _DASHBOARD_CARDS:
            if acl is not None:
                required_level = acl.get_role_level(card["required_role"])
                if conn.user_level > required_level:
                    continue
            cards.append(card)

        return {"type": "dashboard.get.result", "ref": frame.get("id"), "cards": cards}

    async def _ws_system_list(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        from gilbert.interfaces.ws import require_admin
        err = require_admin(conn, frame)
        if err:
            return err

        gilbert = conn.manager._gilbert
        if gilbert is None:
            return {"type": "system.services.list.result", "ref": frame.get("id"), "services": []}

        from gilbert.core.services.configuration import ConfigurationService
        from gilbert.interfaces.configuration import Configurable
        from gilbert.interfaces.tools import ToolProvider

        sm = gilbert.service_manager
        config_svc = sm.get_by_capability("configuration")
        services = []

        for name in list(sm._registered.keys()):
            svc = sm._registered[name]
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
                entry["config_params"] = [
                    {"key": p.key, "type": p.type.value, "description": p.description, "default": p.default, "restart_required": p.restart_required}
                    for p in svc.config_params()
                ]
                if isinstance(config_svc, ConfigurationService):
                    entry["config_values"] = config_svc.get_section(svc.config_namespace)

            if isinstance(svc, ToolProvider):
                entry["tools"] = [
                    {"name": t.name, "description": t.description, "required_role": t.required_role,
                     "parameters": [{"name": p.name, "type": p.type.value, "description": p.description, "required": p.required} for p in t.parameters]}
                    for t in svc.get_tools()
                ]

            services.append(entry)

        return {"type": "system.services.list.result", "ref": frame.get("id"), "services": services}

    async def _ws_entities_list(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        from gilbert.interfaces.ws import require_admin
        err = require_admin(conn, frame)
        if err:
            return err

        gilbert = conn.manager._gilbert
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
        from gilbert.interfaces.ws import require_admin
        err = require_admin(conn, frame)
        if err:
            return err

        collection = frame.get("collection", "")
        if not collection:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "collection required", "code": 400}

        gilbert = conn.manager._gilbert
        if gilbert is None:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Storage not available", "code": 503}

        storage_svc = gilbert.service_manager.get_by_capability("entity_storage")
        if storage_svc is None:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Storage not available", "code": 503}

        from gilbert.interfaces.storage import Query, SortField
        page = int(frame.get("page", 1))
        sort_field = frame.get("sort", "_id")
        order = frame.get("order", "asc")
        page_size = 50
        offset = (page - 1) * page_size

        sort = [SortField(field=sort_field, descending=(order == "desc"))]
        entities = await storage_svc.backend.query(Query(
            collection=collection, sort=sort, limit=page_size, offset=offset,
        ))
        try:
            total = await storage_svc.backend.count(Query(collection=collection))
        except Exception:
            total = len(entities)
        total_pages = max(1, (total + page_size - 1) // page_size)

        # Derive sortable fields from first entity
        sortable_fields = []
        if entities:
            sortable_fields = sorted(entities[0].keys())

        # FK map
        fk_map = {}
        if hasattr(storage_svc.backend, "get_foreign_keys"):
            fk_map = await storage_svc.backend.get_foreign_keys(collection) or {}

        return {
            "type": "entities.collection.query.result", "ref": frame.get("id"),
            "collection": collection, "entities": entities, "total": total,
            "page": page, "total_pages": total_pages,
            "sortable_fields": sortable_fields, "fk_map": fk_map,
        }

    async def _ws_entity_get(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
        from gilbert.interfaces.ws import require_admin
        err = require_admin(conn, frame)
        if err:
            return err

        collection = frame.get("collection", "")
        entity_id = frame.get("entity_id", "")
        if not collection or not entity_id:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "collection and entity_id required", "code": 400}

        gilbert = conn.manager._gilbert
        if gilbert is None:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Storage not available", "code": 503}

        storage_svc = gilbert.service_manager.get_by_capability("entity_storage")
        if storage_svc is None:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Storage not available", "code": 503}

        entity = await storage_svc.backend.get(collection, entity_id)
        if entity is None:
            return {"type": "gilbert.error", "ref": frame.get("id"), "error": "Entity not found", "code": 404}

        fk_map = {}
        if hasattr(storage_svc.backend, "get_foreign_keys"):
            fk_map = await storage_svc.backend.get_foreign_keys(collection) or {}

        return {
            "type": "entities.entity.get.result", "ref": frame.get("id"),
            "collection": collection, "entity_id": entity_id,
            "entity": entity, "fk_map": fk_map,
        }
