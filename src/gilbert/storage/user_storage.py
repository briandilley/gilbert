"""User storage — UserBackend implementation over StorageBackend."""

from datetime import UTC, datetime
from typing import Any

from gilbert.interfaces.storage import (
    Filter,
    FilterOp,
    IndexDefinition,
    Query,
    SortField,
    StorageBackend,
)
from gilbert.interfaces.users import UserBackend

_USERS = "users"
_PROVIDER_USERS = "provider_users"


class StorageUserBackend(UserBackend):
    """Implements UserBackend by delegating to a generic StorageBackend."""

    def __init__(self, storage: StorageBackend) -> None:
        self._storage = storage

    async def ensure_indexes(self) -> None:
        """Create indexes required for efficient user queries."""
        await self._storage.ensure_index(
            IndexDefinition(collection=_USERS, fields=["username"], unique=True)
        )
        await self._storage.ensure_index(IndexDefinition(collection=_USERS, fields=["email"]))
        await self._storage.ensure_index(
            IndexDefinition(collection=_PROVIDER_USERS, fields=["provider_type"])
        )
        await self._storage.ensure_index(
            IndexDefinition(collection=_PROVIDER_USERS, fields=["local_user_id"])
        )

    # ---- User CRUD ----

    async def create_user(self, user_id: str, data: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        entity: dict[str, Any] = {
            "username": data.get("username", "").lower(),
            "email": data.get("email", ""),
            "display_name": data.get("display_name", ""),
            "password_hash": data.get("password_hash", ""),
            "is_root": data.get("is_root", False),
            "roles": list(data.get("roles", [])),
            "provider_links": list(data.get("provider_links", [])),
            "metadata": data.get("metadata", {}),
            # IANA timezone (e.g. "America/Los_Angeles"); ``None`` until
            # the user picks one. Read by features that need to render
            # time-of-day in the user's zone (push fan-out quiet hours,
            # daily summaries, etc.).
            "tz": data.get("tz"),
            "created_at": now,
            "last_login": None,
        }
        await self._storage.put(_USERS, user_id, entity)
        entity["_id"] = user_id
        return entity

    async def get_user(self, user_id: str) -> dict[str, Any] | None:
        data = await self._storage.get(_USERS, user_id)
        if data is not None:
            data["_id"] = user_id
        return data

    async def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        results = await self._storage.query(
            Query(
                collection=_USERS,
                filters=[Filter(field="username", op=FilterOp.EQ, value=username.lower())],
                limit=1,
            )
        )
        return results[0] if results else None

    async def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        results = await self._storage.query(
            Query(
                collection=_USERS,
                filters=[Filter(field="email", op=FilterOp.EQ, value=email)],
                limit=1,
            )
        )
        return results[0] if results else None

    async def get_user_by_provider_link(
        self, provider_type: str, provider_user_id: str
    ) -> dict[str, Any] | None:
        # Look up via the provider_users collection which has the local_user_id.
        provider_user = await self.get_provider_user(provider_type, provider_user_id)
        if provider_user is None:
            return None
        local_id = provider_user.get("local_user_id")
        if local_id is None:
            return None
        return await self.get_user(local_id)

    async def update_user(self, user_id: str, data: dict[str, Any]) -> None:
        existing = await self._storage.get(_USERS, user_id)
        if existing is None:
            raise KeyError(f"User not found: {user_id}")
        existing.pop("_id", None)
        existing.update(data)
        await self._storage.put(_USERS, user_id, existing)

    async def delete_user(self, user_id: str) -> None:
        await self._storage.delete(_USERS, user_id)

    async def list_users(self, limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
        return await self._storage.query(
            Query(
                collection=_USERS,
                sort=[SortField(field="email")],
                limit=limit,
                offset=offset,
            )
        )

    # ---- Provider links ----

    async def add_provider_link(
        self, user_id: str, provider_type: str, provider_user_id: str
    ) -> None:
        existing = await self._storage.get(_USERS, user_id)
        if existing is None:
            raise KeyError(f"User not found: {user_id}")
        links: list[dict[str, str]] = existing.get("provider_links", [])
        # Replace if provider_type already linked.
        links = [lk for lk in links if lk.get("provider_type") != provider_type]
        links.append({"provider_type": provider_type, "provider_user_id": provider_user_id})
        existing.pop("_id", None)
        existing["provider_links"] = links
        await self._storage.put(_USERS, user_id, existing)

    async def remove_provider_link(self, user_id: str, provider_type: str) -> None:
        existing = await self._storage.get(_USERS, user_id)
        if existing is None:
            raise KeyError(f"User not found: {user_id}")
        links: list[dict[str, str]] = existing.get("provider_links", [])
        existing["provider_links"] = [
            lk for lk in links if lk.get("provider_type") != provider_type
        ]
        existing.pop("_id", None)
        await self._storage.put(_USERS, user_id, existing)

    # ---- Roles ----

    async def set_roles(self, user_id: str, roles: set[str]) -> None:
        existing = await self._storage.get(_USERS, user_id)
        if existing is None:
            raise KeyError(f"User not found: {user_id}")
        existing.pop("_id", None)
        existing["roles"] = sorted(roles)
        await self._storage.put(_USERS, user_id, existing)

    async def get_roles(self, user_id: str) -> set[str]:
        existing = await self._storage.get(_USERS, user_id)
        if existing is None:
            raise KeyError(f"User not found: {user_id}")
        return set(existing.get("roles", []))

    # ---- Provider users (remote user cache) ----

    async def put_provider_user(
        self, provider_type: str, provider_user_id: str, data: dict[str, Any]
    ) -> None:
        entity_id = f"{provider_type}:{provider_user_id}"
        entity: dict[str, Any] = {
            **data,
            "provider_type": provider_type,
            "provider_user_id": provider_user_id,
            "synced_at": datetime.now(UTC).isoformat(),
        }
        await self._storage.put(_PROVIDER_USERS, entity_id, entity)

    async def get_provider_user(
        self, provider_type: str, provider_user_id: str
    ) -> dict[str, Any] | None:
        entity_id = f"{provider_type}:{provider_user_id}"
        data = await self._storage.get(_PROVIDER_USERS, entity_id)
        if data is not None:
            data["_id"] = entity_id
        return data

    async def list_provider_users(
        self, provider_type: str, limit: int | None = None, offset: int = 0
    ) -> list[dict[str, Any]]:
        return await self._storage.query(
            Query(
                collection=_PROVIDER_USERS,
                filters=[Filter(field="provider_type", op=FilterOp.EQ, value=provider_type)],
                sort=[SortField(field="email")],
                limit=limit,
                offset=offset,
            )
        )
