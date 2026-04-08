"""Access control service — role hierarchy and per-tool permission management."""

import json
import logging
from typing import Any

from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import StorageBackend
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)

logger = logging.getLogger(__name__)

_ROLES_COLLECTION = "acl_roles"
_OVERRIDES_COLLECTION = "acl_tool_overrides"
_COLLECTION_ACL = "acl_collections"
_EVENT_ACL_COLLECTION = "acl_event_visibility"

# Built-in roles — cannot be removed or have their level changed
_BUILTIN_ROLES: list[dict[str, Any]] = [
    {"name": "admin", "level": 0, "builtin": True, "description": "Full system access"},
    {"name": "user", "level": 100, "builtin": True, "description": "Standard user access"},
    {"name": "everyone", "level": 200, "builtin": True, "description": "Minimum access for any authenticated user"},
]

# Default level for unknown roles (treated as "everyone")
_DEFAULT_LEVEL = 200

# SYSTEM user level — bypasses all checks
_SYSTEM_LEVEL = -1


class AccessControlService(Service):
    """Manages the role hierarchy and per-tool permission overrides.

    Roles have a numeric level (lower = more privileged). A user's effective
    level is the minimum across their assigned roles. A tool is accessible if
    the user's effective level <= the tool's required role level.

    Built-in roles (admin, user, everyone) cannot be removed or reordered.
    Custom roles can be added at any level.
    """

    def __init__(self) -> None:
        self._storage: StorageBackend | None = None
        # In-memory cache: role name → level
        self._role_levels: dict[str, int] = {r["name"]: r["level"] for r in _BUILTIN_ROLES}
        # Override cache: tool name → required role name
        self._tool_overrides: dict[str, str] = {}
        # Collection ACL cache: collection → {"read_role": str, "write_role": str}
        self._collection_acl: dict[str, dict[str, str]] = {}
        # Event visibility override cache: event prefix → role name
        self._event_acl: dict[str, str] = {}

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="access_control",
            capabilities=frozenset({"access_control", "ai_tools"}),
            requires=frozenset({"entity_storage"}),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        from gilbert.core.services.storage import StorageService

        storage_svc = resolver.require_capability("entity_storage")
        if not isinstance(storage_svc, StorageService):
            raise TypeError("Expected StorageService for entity_storage")
        self._storage = storage_svc.backend

        # Seed built-in roles (idempotent)
        for role in _BUILTIN_ROLES:
            existing = await self._storage.get(_ROLES_COLLECTION, role["name"])
            if existing is None:
                await self._storage.put(_ROLES_COLLECTION, role["name"], dict(role))

        # Load caches
        await self._refresh_caches()
        logger.info(
            "Access control started — %d roles, %d tool overrides",
            len(self._role_levels),
            len(self._tool_overrides),
        )

    async def stop(self) -> None:
        pass

    # --- Cache management ---

    async def _refresh_caches(self) -> None:
        """Reload role levels and tool overrides from storage."""
        if self._storage is None:
            return

        from gilbert.interfaces.storage import Query

        # Roles
        roles = await self._storage.query(Query(collection=_ROLES_COLLECTION))
        self._role_levels = {r["name"]: r["level"] for r in roles if "name" in r and "level" in r}

        # Ensure built-ins are always present in cache
        for role in _BUILTIN_ROLES:
            self._role_levels.setdefault(role["name"], role["level"])

        # Tool overrides
        overrides = await self._storage.query(Query(collection=_OVERRIDES_COLLECTION))
        self._tool_overrides = {
            o["tool_name"]: o["required_role"]
            for o in overrides
            if "tool_name" in o and "required_role" in o
        }

        # Collection ACLs
        col_acls = await self._storage.query(Query(collection=_COLLECTION_ACL))
        self._collection_acl = {
            c["collection"]: {"read_role": c.get("read_role", "user"), "write_role": c.get("write_role", "admin")}
            for c in col_acls
            if "collection" in c
        }

        # Event visibility overrides
        event_acls = await self._storage.query(Query(collection=_EVENT_ACL_COLLECTION))
        self._event_acl = {
            e["event_prefix"]: e["min_role"]
            for e in event_acls
            if "event_prefix" in e and "min_role" in e
        }

    # --- Role queries ---

    def get_role_level(self, role_name: str) -> int:
        """Get the numeric level for a role. Returns 200 (everyone) for unknown roles."""
        return self._role_levels.get(role_name, _DEFAULT_LEVEL)

    def get_effective_level(self, user_ctx: UserContext) -> int:
        """Get the user's effective permission level (lowest = most privileged).

        SYSTEM user gets level -1 (bypasses all checks).
        """
        if user_ctx.user_id == "system":
            return _SYSTEM_LEVEL
        if not user_ctx.roles:
            return _DEFAULT_LEVEL
        return min(self.get_role_level(r) for r in user_ctx.roles)

    def check_tool_access(self, user_ctx: UserContext, tool_def: ToolDefinition) -> bool:
        """Check if a user can access a tool based on the role hierarchy."""
        if user_ctx.user_id == "system":
            return True
        effective = self.get_effective_level(user_ctx)
        # Check override first, then tool's declared required_role
        required_role = self._tool_overrides.get(tool_def.name, tool_def.required_role)
        required_level = self.get_role_level(required_role)
        return effective <= required_level

    # --- Role CRUD ---

    async def list_roles(self) -> list[dict[str, Any]]:
        """List all roles sorted by level."""
        if self._storage is None:
            return [dict(r) for r in _BUILTIN_ROLES]
        from gilbert.interfaces.storage import Query, SortField

        return await self._storage.query(Query(
            collection=_ROLES_COLLECTION,
            sort=[SortField(field="level")],
        ))

    async def get_role(self, name: str) -> dict[str, Any] | None:
        """Get a role by name."""
        if self._storage is None:
            return None
        return await self._storage.get(_ROLES_COLLECTION, name)

    async def create_role(self, name: str, level: int, description: str = "") -> dict[str, Any]:
        """Create a custom role. Raises ValueError if name is taken."""
        if self._storage is None:
            raise RuntimeError("Storage not available")
        existing = await self._storage.get(_ROLES_COLLECTION, name)
        if existing is not None:
            raise ValueError(f"Role '{name}' already exists")

        role = {"name": name, "level": level, "builtin": False, "description": description}
        await self._storage.put(_ROLES_COLLECTION, name, role)
        await self._refresh_caches()
        logger.info("Created role '%s' at level %d", name, level)
        return role

    async def update_role(
        self, name: str, level: int | None = None, description: str | None = None
    ) -> dict[str, Any]:
        """Update a custom role. Built-in role levels cannot be changed."""
        if self._storage is None:
            raise RuntimeError("Storage not available")
        existing = await self._storage.get(_ROLES_COLLECTION, name)
        if existing is None:
            raise KeyError(f"Role not found: {name}")

        if existing.get("builtin", False):
            if level is not None and level != existing["level"]:
                raise ValueError(f"Cannot change level of built-in role '{name}'")

        updates = dict(existing)
        if level is not None:
            updates["level"] = level
        if description is not None:
            updates["description"] = description
        await self._storage.put(_ROLES_COLLECTION, name, updates)
        await self._refresh_caches()
        logger.info("Updated role '%s'", name)
        return updates

    async def delete_role(self, name: str) -> None:
        """Delete a custom role. Built-in roles cannot be deleted."""
        if self._storage is None:
            raise RuntimeError("Storage not available")
        existing = await self._storage.get(_ROLES_COLLECTION, name)
        if existing is None:
            raise KeyError(f"Role not found: {name}")
        if existing.get("builtin", False):
            raise ValueError(f"Cannot delete built-in role '{name}'")
        await self._storage.delete(_ROLES_COLLECTION, name)
        await self._refresh_caches()
        logger.info("Deleted role '%s'", name)

    # --- Tool permission overrides ---

    async def list_tool_overrides(self) -> list[dict[str, Any]]:
        """List all tool permission overrides."""
        if self._storage is None:
            return []
        from gilbert.interfaces.storage import Query

        return await self._storage.query(Query(collection=_OVERRIDES_COLLECTION))

    async def set_tool_override(self, tool_name: str, required_role: str) -> None:
        """Set or update a tool permission override."""
        if self._storage is None:
            raise RuntimeError("Storage not available")
        if required_role not in self._role_levels:
            raise ValueError(f"Unknown role: {required_role}")
        await self._storage.put(_OVERRIDES_COLLECTION, tool_name, {
            "tool_name": tool_name,
            "required_role": required_role,
        })
        self._tool_overrides[tool_name] = required_role
        logger.info("Tool '%s' now requires role '%s'", tool_name, required_role)

    async def clear_tool_override(self, tool_name: str) -> None:
        """Remove a tool permission override (revert to default)."""
        if self._storage is None:
            raise RuntimeError("Storage not available")
        await self._storage.delete(_OVERRIDES_COLLECTION, tool_name)
        self._tool_overrides.pop(tool_name, None)
        logger.info("Tool override removed for '%s'", tool_name)

    # --- Collection-level ACL ---

    def check_collection_read(self, user_ctx: UserContext, collection: str) -> bool:
        """Check if a user can read from a collection."""
        if user_ctx.user_id == "system":
            return True
        acl = self._collection_acl.get(collection)
        read_role = acl["read_role"] if acl else "user"
        return self.get_effective_level(user_ctx) <= self.get_role_level(read_role)

    def check_collection_write(self, user_ctx: UserContext, collection: str) -> bool:
        """Check if a user can write to a collection."""
        if user_ctx.user_id == "system":
            return True
        acl = self._collection_acl.get(collection)
        write_role = acl["write_role"] if acl else "admin"
        return self.get_effective_level(user_ctx) <= self.get_role_level(write_role)

    async def set_collection_acl(
        self, collection: str, read_role: str = "user", write_role: str = "admin"
    ) -> None:
        """Set read/write role requirements for a collection."""
        if self._storage is None:
            raise RuntimeError("Storage not available")
        for role in (read_role, write_role):
            if role not in self._role_levels:
                raise ValueError(f"Unknown role: {role}")
        await self._storage.put(_COLLECTION_ACL, collection, {
            "collection": collection,
            "read_role": read_role,
            "write_role": write_role,
        })
        self._collection_acl[collection] = {"read_role": read_role, "write_role": write_role}
        logger.info("Collection '%s' ACL set: read=%s, write=%s", collection, read_role, write_role)

    async def clear_collection_acl(self, collection: str) -> None:
        """Remove collection ACL (revert to defaults: read=user, write=admin)."""
        if self._storage is None:
            raise RuntimeError("Storage not available")
        await self._storage.delete(_COLLECTION_ACL, collection)
        self._collection_acl.pop(collection, None)

    async def list_collection_acls(self) -> list[dict[str, Any]]:
        """List all collection ACL entries."""
        if self._storage is None:
            return []
        from gilbert.interfaces.storage import Query

        return await self._storage.query(Query(collection=_COLLECTION_ACL))

    # --- Event visibility ---

    def get_event_visibility_role(self, event_type: str) -> str:
        """Resolve the minimum role required to see an event type.

        Checks overrides first, then falls back to the built-in defaults
        in ``ws_protocol``. Longest prefix match wins.
        """
        from gilbert.web.ws_protocol import _EVENT_VISIBILITY, _DEFAULT_VISIBILITY_LEVEL

        # Check overrides (longest prefix match)
        best = ""
        for prefix in self._event_acl:
            if event_type.startswith(prefix) and len(prefix) > len(best):
                best = prefix
        if best:
            return self._event_acl[best]

        # Check built-in defaults
        best = ""
        best_level = _DEFAULT_VISIBILITY_LEVEL
        for prefix, level in _EVENT_VISIBILITY.items():
            if event_type.startswith(prefix) and len(prefix) > len(best):
                best = prefix
                best_level = level

        # Reverse-lookup level → role name
        for name, lv in sorted(self._role_levels.items(), key=lambda x: x[1]):
            if lv == best_level:
                return name
        return "user"

    async def set_event_visibility(self, event_prefix: str, min_role: str) -> None:
        """Set or update an event visibility override."""
        if self._storage is None:
            return
        await self._storage.put(_EVENT_ACL_COLLECTION, event_prefix, {
            "event_prefix": event_prefix,
            "min_role": min_role,
        })
        await self._refresh_caches()

    async def clear_event_visibility(self, event_prefix: str) -> None:
        """Remove an event visibility override (reverts to default)."""
        if self._storage is None:
            return
        await self._storage.delete(_EVENT_ACL_COLLECTION, event_prefix)
        await self._refresh_caches()

    async def list_event_visibility(self) -> list[dict[str, Any]]:
        """List all event visibility rules (defaults + overrides)."""
        from gilbert.web.ws_protocol import _EVENT_VISIBILITY

        # Start with defaults
        rules: dict[str, dict[str, Any]] = {}
        for prefix, level in _EVENT_VISIBILITY.items():
            role = "user"
            for name, lv in sorted(self._role_levels.items(), key=lambda x: x[1]):
                if lv == level:
                    role = name
                    break
            rules[prefix] = {"event_prefix": prefix, "min_role": role, "source": "default"}

        # Layer overrides on top
        for prefix, role in self._event_acl.items():
            rules[prefix] = {"event_prefix": prefix, "min_role": role, "source": "override"}

        return sorted(rules.values(), key=lambda r: r["event_prefix"])

    # --- ToolProvider protocol ---

    @property
    def tool_provider_name(self) -> str:
        return "access_control"

    def get_tools(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="list_roles",
                description="List all roles in the system with their hierarchy levels.",
                required_role="everyone",
            ),
            ToolDefinition(
                name="create_role",
                description="Create a custom role with a name and hierarchy level (lower number = more privileged).",
                parameters=[
                    ToolParameter(name="name", type=ToolParameterType.STRING, description="Role name."),
                    ToolParameter(name="level", type=ToolParameterType.INTEGER, description="Hierarchy level (lower = more privileged). admin=0, user=100, everyone=200."),
                    ToolParameter(name="description", type=ToolParameterType.STRING, description="Role description.", required=False),
                ],
                required_role="admin",
            ),
            ToolDefinition(
                name="update_role",
                description="Update a custom role's level or description. Built-in roles (admin, user, everyone) cannot have their level changed.",
                parameters=[
                    ToolParameter(name="name", type=ToolParameterType.STRING, description="Role name to update."),
                    ToolParameter(name="level", type=ToolParameterType.INTEGER, description="New hierarchy level.", required=False),
                    ToolParameter(name="description", type=ToolParameterType.STRING, description="New description.", required=False),
                ],
                required_role="admin",
            ),
            ToolDefinition(
                name="delete_role",
                description="Delete a custom role. Built-in roles cannot be deleted.",
                parameters=[
                    ToolParameter(name="name", type=ToolParameterType.STRING, description="Role name to delete."),
                ],
                required_role="admin",
            ),
            ToolDefinition(
                name="get_tool_permissions",
                description="List all tools and their required roles, including any overrides.",
                required_role="everyone",
            ),
            ToolDefinition(
                name="set_tool_permission",
                description="Override the required role for a specific tool.",
                parameters=[
                    ToolParameter(name="tool_name", type=ToolParameterType.STRING, description="Tool name."),
                    ToolParameter(name="required_role", type=ToolParameterType.STRING, description="Role required to use this tool."),
                ],
                required_role="admin",
            ),
            ToolDefinition(
                name="clear_tool_permission",
                description="Remove a tool permission override, reverting to the tool's default role.",
                parameters=[
                    ToolParameter(name="tool_name", type=ToolParameterType.STRING, description="Tool name."),
                ],
                required_role="admin",
            ),
            ToolDefinition(
                name="list_collection_acls",
                description="List access control rules for entity collections.",
                required_role="everyone",
            ),
            ToolDefinition(
                name="set_collection_acl",
                description="Set read/write role requirements for an entity collection.",
                parameters=[
                    ToolParameter(name="collection", type=ToolParameterType.STRING, description="Collection name."),
                    ToolParameter(name="read_role", type=ToolParameterType.STRING, description="Role required to read (default: user)."),
                    ToolParameter(name="write_role", type=ToolParameterType.STRING, description="Role required to write (default: admin)."),
                ],
                required_role="admin",
            ),
            ToolDefinition(
                name="list_event_visibility",
                description="List all event visibility rules (which roles can see which events via WebSocket).",
                required_role="everyone",
            ),
            ToolDefinition(
                name="set_event_visibility",
                description="Set the minimum role required to see events matching a prefix via WebSocket.",
                parameters=[
                    ToolParameter(name="event_prefix", type=ToolParameterType.STRING, description="Event type prefix (e.g., 'inbox.' or 'chat.message.')."),
                    ToolParameter(name="min_role", type=ToolParameterType.STRING, description="Minimum role required (e.g., 'admin', 'user', 'everyone')."),
                ],
                required_role="admin",
            ),
            ToolDefinition(
                name="clear_event_visibility",
                description="Remove an event visibility override, reverting to the built-in default.",
                parameters=[
                    ToolParameter(name="event_prefix", type=ToolParameterType.STRING, description="Event type prefix to clear."),
                ],
                required_role="admin",
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        match name:
            case "list_roles":
                return await self._tool_list_roles()
            case "create_role":
                return await self._tool_create_role(arguments)
            case "update_role":
                return await self._tool_update_role(arguments)
            case "delete_role":
                return await self._tool_delete_role(arguments)
            case "get_tool_permissions":
                return self._tool_get_permissions()
            case "set_tool_permission":
                return await self._tool_set_permission(arguments)
            case "clear_tool_permission":
                return await self._tool_clear_permission(arguments)
            case "list_collection_acls":
                return await self._tool_list_collection_acls()
            case "set_collection_acl":
                return await self._tool_set_collection_acl(arguments)
            case "list_event_visibility":
                rules = await self.list_event_visibility()
                return json.dumps(rules)
            case "set_event_visibility":
                await self.set_event_visibility(arguments["event_prefix"], arguments["min_role"])
                return json.dumps({"status": "ok", "event_prefix": arguments["event_prefix"], "min_role": arguments["min_role"]})
            case "clear_event_visibility":
                await self.clear_event_visibility(arguments["event_prefix"])
                return json.dumps({"status": "ok", "event_prefix": arguments["event_prefix"]})
            case _:
                raise KeyError(f"Unknown tool: {name}")

    async def _tool_list_roles(self) -> str:
        roles = await self.list_roles()
        for r in roles:
            r.pop("_id", None)
        return json.dumps(roles)

    async def _tool_create_role(self, arguments: dict[str, Any]) -> str:
        try:
            role = await self.create_role(
                name=arguments["name"],
                level=int(arguments["level"]),
                description=arguments.get("description", ""),
            )
            return json.dumps({"status": "created", "role": role})
        except ValueError as e:
            return json.dumps({"error": str(e)})

    async def _tool_update_role(self, arguments: dict[str, Any]) -> str:
        try:
            role = await self.update_role(
                name=arguments["name"],
                level=int(arguments["level"]) if "level" in arguments else None,
                description=arguments.get("description"),
            )
            return json.dumps({"status": "updated", "role": role})
        except (KeyError, ValueError) as e:
            return json.dumps({"error": str(e)})

    async def _tool_delete_role(self, arguments: dict[str, Any]) -> str:
        try:
            await self.delete_role(arguments["name"])
            return json.dumps({"status": "deleted"})
        except (KeyError, ValueError) as e:
            return json.dumps({"error": str(e)})

    def _tool_get_permissions(self) -> str:
        # Return override info — tools themselves are discovered via the AI service
        return json.dumps({
            "overrides": [
                {"tool_name": k, "required_role": v}
                for k, v in sorted(self._tool_overrides.items())
            ],
            "note": "Tools without overrides use their default required_role.",
        })

    async def _tool_set_permission(self, arguments: dict[str, Any]) -> str:
        try:
            await self.set_tool_override(arguments["tool_name"], arguments["required_role"])
            return json.dumps({"status": "set"})
        except ValueError as e:
            return json.dumps({"error": str(e)})

    async def _tool_clear_permission(self, arguments: dict[str, Any]) -> str:
        await self.clear_tool_override(arguments["tool_name"])
        return json.dumps({"status": "cleared"})

    async def _tool_list_collection_acls(self) -> str:
        acls = await self.list_collection_acls()
        for a in acls:
            a.pop("_id", None)
        return json.dumps(acls)

    async def _tool_set_collection_acl(self, arguments: dict[str, Any]) -> str:
        try:
            await self.set_collection_acl(
                collection=arguments["collection"],
                read_role=arguments.get("read_role", "user"),
                write_role=arguments.get("write_role", "admin"),
            )
            return json.dumps({"status": "set"})
        except ValueError as e:
            return json.dumps({"error": str(e)})
