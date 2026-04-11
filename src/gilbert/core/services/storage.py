"""Storage service — wraps StorageBackend as a discoverable service."""

import json
import logging
from typing import Any

from gilbert.core.context import get_current_user
from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import (
    Filter,
    FilterOp,
    NamespacedStorageBackend,
    Query,
    SortField,
    StorageBackend,
)
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)

logger = logging.getLogger(__name__)


class StorageService(Service):
    """Exposes a StorageBackend as a service with entity_storage capability."""

    _DEFAULT_NAMESPACE = "gilbert"

    def __init__(self, backend: StorageBackend) -> None:
        self._raw_backend = backend
        self._backend = NamespacedStorageBackend(backend, self._DEFAULT_NAMESPACE)
        self._resolver: ServiceResolver | None = None

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="storage",
            capabilities=frozenset({"entity_storage", "query_storage", "ai_tools"}),
            optional=frozenset({"access_control"}),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver

    @property
    def backend(self) -> StorageBackend:
        """The default namespaced backend (``gilbert.`` prefix)."""
        return self._backend

    @property
    def raw_backend(self) -> StorageBackend:
        """The raw backend with no namespace prefix.

        Used by the entity browser to see all namespaces.
        """
        return self._raw_backend

    def create_namespaced(self, namespace: str) -> NamespacedStorageBackend:
        """Create a backend scoped to a custom namespace."""
        return NamespacedStorageBackend(self._raw_backend, namespace)

    async def stop(self) -> None:
        await self._raw_backend.close()

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "storage"

    @property
    def config_category(self) -> str:
        return "Infrastructure"

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="backend", type=ToolParameterType.STRING,
                description="Storage backend type.",
                default="sqlite", restart_required=True,
            ),
            ConfigParam(
                key="connection", type=ToolParameterType.STRING,
                description="Database connection string/path.",
                default=".gilbert/gilbert.db", restart_required=True,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        pass  # All storage params are restart_required

    # --- ToolProvider protocol ---

    @property
    def tool_provider_name(self) -> str:
        return "storage"

    def get_tools(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="store_entity",
                description="Store an entity in a collection. Overwrites if the ID already exists.",
                parameters=[
                    ToolParameter(
                        name="collection",
                        type=ToolParameterType.STRING,
                        description="The collection name.",
                    ),
                    ToolParameter(
                        name="id",
                        type=ToolParameterType.STRING,
                        description="The entity ID.",
                    ),
                    ToolParameter(
                        name="data",
                        type=ToolParameterType.OBJECT,
                        description="The entity data to store.",
                    ),
                ],
                required_role="admin",
            ),
            ToolDefinition(
                name="get_entity",
                description="Retrieve an entity by collection and ID.",
                parameters=[
                    ToolParameter(
                        name="collection",
                        type=ToolParameterType.STRING,
                        description="The collection name.",
                    ),
                    ToolParameter(
                        name="id",
                        type=ToolParameterType.STRING,
                        description="The entity ID.",
                    ),
                ],
                required_role="everyone",
            ),
            ToolDefinition(
                name="query_entities",
                description="Query entities in a collection with optional filters, sorting, and limit.",
                parameters=[
                    ToolParameter(
                        name="collection",
                        type=ToolParameterType.STRING,
                        description="The collection name.",
                    ),
                    ToolParameter(
                        name="filters",
                        type=ToolParameterType.ARRAY,
                        description=(
                            'Array of filter objects: {"field": "name", "op": "eq", "value": "foo"}. '
                            "Supported ops: eq, neq, gt, gte, lt, lte, in, contains, exists."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="sort",
                        type=ToolParameterType.ARRAY,
                        description=(
                            'Array of sort objects: {"field": "name", "descending": false}.'
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="limit",
                        type=ToolParameterType.INTEGER,
                        description="Maximum number of results to return.",
                        required=False,
                    ),
                ],
                required_role="everyone",
            ),
            ToolDefinition(
                name="list_collections",
                description="List all entity collection names.",
                required_role="everyone",
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        match name:
            case "store_entity":
                return await self._tool_store_entity(arguments)
            case "get_entity":
                return await self._tool_get_entity(arguments)
            case "query_entities":
                return await self._tool_query_entities(arguments)
            case "list_collections":
                return await self._tool_list_collections()
            case _:
                raise KeyError(f"Unknown tool: {name}")

    def _check_collection_access(self, collection: str, write: bool = False) -> str | None:
        """Check collection-level ACL. Returns an error message or None if allowed."""
        if self._resolver is None:
            return None
        acl_svc = self._resolver.get_capability("access_control")
        if acl_svc is None:
            return None
        user = get_current_user()
        if write:
            if not acl_svc.check_collection_write(user, collection):
                return f"Permission denied: cannot write to collection '{collection}'"
        else:
            if not acl_svc.check_collection_read(user, collection):
                return f"Permission denied: cannot read from collection '{collection}'"
        return None

    async def _tool_store_entity(self, arguments: dict[str, Any]) -> str:
        collection = arguments["collection"]
        err = self._check_collection_access(collection, write=True)
        if err:
            return json.dumps({"error": err})
        entity_id = arguments["id"]
        data = arguments["data"]
        await self._backend.put(collection, entity_id, data)
        return json.dumps({
            "status": "ok",
            "collection": collection,
            "id": entity_id,
        })

    async def _tool_get_entity(self, arguments: dict[str, Any]) -> str:
        collection = arguments["collection"]
        err = self._check_collection_access(collection, write=False)
        if err:
            return json.dumps({"error": err})
        entity_id = arguments["id"]
        entity = await self._backend.get(collection, entity_id)
        if entity is None:
            return json.dumps({"error": f"Entity not found: {collection}/{entity_id}"})
        return json.dumps(entity)

    async def _tool_query_entities(self, arguments: dict[str, Any]) -> str:
        collection = arguments["collection"]
        err = self._check_collection_access(collection, write=False)
        if err:
            return json.dumps({"error": err})

        filters = [
            Filter(
                field=f["field"],
                op=FilterOp(f["op"]),
                value=f.get("value"),
            )
            for f in arguments.get("filters", [])
        ]

        sort = [
            SortField(
                field=s["field"],
                descending=s.get("descending", False),
            )
            for s in arguments.get("sort", [])
        ]

        limit = arguments.get("limit")

        query = Query(
            collection=collection,
            filters=filters,
            sort=sort,
            limit=limit,
        )
        results = await self._backend.query(query)
        return json.dumps(results)

    async def _tool_list_collections(self) -> str:
        collections = await self._backend.list_collections()
        return json.dumps(collections)
