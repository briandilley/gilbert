"""MCP server — Gilbert exposing its tools to external MCP clients.

Gilbert can act as both an MCP client (federating external servers'
tools into its own AI pipeline — see ``core/services/mcp.py``) and an
MCP server (letting external MCP-aware agents consume Gilbert's tools
directly over HTTP). This module handles the server direction.

Admins register one ``MCPServerClient`` entity per external client.
Each entity carries a bearer token (hashed at rest), names the
Gilbert user the client acts as, and points at an AI context profile
that controls which tools the client can see. The MCP HTTP endpoint
— mounted in ``web/routes/api.py`` — authenticates incoming requests
by hashing the bearer and looking up the matching client, then
dispatches tool calls through the existing tool registry under the
resolved ``UserContext``.

This file owns the persistence, CRUD RPCs, and token lifecycle.
The actual MCP protocol server lives in Part 4.2.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from gilbert.interfaces.ai import AIToolDiscoveryProvider
from gilbert.interfaces.auth import AccessControlProvider, UserContext
from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import (
    Filter,
    FilterOp,
    IndexDefinition,
    Query,
    StorageBackend,
    StorageProvider,
)
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.users import UserManagementProvider
from gilbert.interfaces.ws import RpcHandler, WsConnectionBase

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("gilbert.mcp_server.audit")

MCP_CLIENTS_COLLECTION = "mcp_server_clients"
TOKEN_PREFIX = "mcpc_"
"""Short prefix on tokens so Gilbert can reject malformed bearer
headers before doing an expensive argon2 verify. Lets the HTTP
middleware bail early on anything that obviously isn't a client
token."""


@dataclass(frozen=True)
class MCPServerClient:
    """Runtime snapshot of an MCP server client registration.

    Mirrors the row in ``mcp_server_clients``. ``token_hash`` is the
    argon2 hash of the plaintext token; the plaintext is shown to the
    admin exactly once at create/rotate time and never again.
    """

    id: str
    name: str
    description: str = ""
    owner_user_id: str = ""
    """The Gilbert user whose identity this client impersonates.
    Every tool call the client makes runs under this user's
    ``UserContext``, so RBAC applies as if the user made the call
    directly."""
    ai_profile: str = "standard"
    """Name of the AIContextProfile that filters which tools this
    client can see. Same machinery used for internal ``ai_calls``."""
    active: bool = True
    token_hash: str = ""
    token_prefix: str = ""
    """First few characters of the plaintext token — stored so admins
    can identify a client in lists/logs without having to keep the
    full secret. Six characters is enough to disambiguate without
    meaningfully weakening the hash."""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_used_at: datetime | None = None
    last_ip: str = ""


class MCPServerService(Service):
    """Owns the ``mcp_server_clients`` collection and the RPC surface.

    Does **not** own the HTTP endpoint itself — that's wired up in
    ``web/routes/api.py`` so it shares the FastAPI app's middleware
    stack. This service's job is persistence, token lifecycle, and
    the authentication lookup the HTTP layer calls into.
    """

    def __init__(self) -> None:
        self._storage: StorageBackend | None = None
        self._acl_svc: Any = None
        self._resolver: ServiceResolver | None = None
        self._hasher: Any = None
        self._enabled: bool = False

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="mcp_server",
            capabilities=frozenset({"mcp_server", "ws_handlers"}),
            requires=frozenset({"entity_storage"}),
            optional=frozenset({"access_control", "users"}),
            toggleable=True,
            toggle_description=(
                "MCP server — let external MCP clients connect to Gilbert and use its tools."
            ),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver
        self._acl_svc = resolver.get_capability("access_control")

        # Read the enable toggle from the configuration section. We
        # still register the service either way so the HTTP endpoint
        # can look up client tokens even while the subsystem is
        # "disabled" (the endpoint itself checks this flag) — but
        # the Settings UI toggle controls the reported service
        # state and the dashboard nav card visibility.
        config_svc = resolver.get_capability("configuration")
        section: dict[str, Any] = {}
        from gilbert.interfaces.configuration import ConfigurationReader

        if isinstance(config_svc, ConfigurationReader):
            section = config_svc.get_section_safe(self.config_namespace)
        self._enabled = bool(section.get("enabled", False))

        storage_svc = resolver.get_capability("entity_storage")
        if not isinstance(storage_svc, StorageProvider):
            raise RuntimeError(
                "MCPServerService requires an entity_storage capability",
            )
        self._storage = storage_svc.backend
        await self._storage.ensure_index(
            IndexDefinition(
                collection=MCP_CLIENTS_COLLECTION,
                fields=["token_hash"],
                unique=True,
            ),
        )
        await self._storage.ensure_index(
            IndexDefinition(
                collection=MCP_CLIENTS_COLLECTION,
                fields=["owner_user_id"],
            ),
        )

    # ── Configurable protocol ────────────────────────────────────────

    @property
    def config_namespace(self) -> str:
        return "mcp_server"

    @property
    def config_category(self) -> str:
        return "Intelligence"

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="enabled",
                type=ToolParameterType.BOOLEAN,
                description="Enable the MCP server endpoint.",
                default=False,
                restart_required=True,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._enabled = bool(config.get("enabled", False))

    # ── Token lifecycle ──────────────────────────────────────────────

    def _get_hasher(self) -> Any:
        if self._hasher is None:
            from argon2 import PasswordHasher

            self._hasher = PasswordHasher()
        return self._hasher

    def _hash_token(self, token: str) -> str:
        return str(self._get_hasher().hash(token))

    def _verify_token(self, stored_hash: str, token: str) -> bool:
        try:
            return bool(self._get_hasher().verify(stored_hash, token))
        except Exception:
            return False

    @staticmethod
    def _generate_token() -> str:
        """Generate a fresh client token.

        32 bytes of urlsafe randomness ≈ 43 characters, prefixed with
        ``mcpc_`` so Gilbert can shape-check the bearer before doing
        an argon2 verify."""
        return f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"

    # ── Authentication lookup (called by HTTP middleware) ────────────

    async def authenticate(
        self,
        token: str,
        *,
        client_ip: str = "",
    ) -> tuple[MCPServerClient, UserContext] | None:
        """Resolve a bearer token to the owning ``MCPServerClient`` and
        ``UserContext``. Returns ``None`` if no active client matches.

        This is the hot path — called on every incoming MCP request.
        We avoid argon2's expense for malformed tokens (wrong prefix
        → reject immediately) and iterate only active records with
        the expected token_prefix, which thins the search space
        significantly once there are more than a handful of clients.
        """
        if not token.startswith(TOKEN_PREFIX):
            return None
        prefix = token[: len(TOKEN_PREFIX) + 6]
        assert self._storage is not None

        docs = await self._storage.query(
            Query(
                collection=MCP_CLIENTS_COLLECTION,
                filters=[
                    Filter(field="active", op=FilterOp.EQ, value=True),
                    Filter(field="token_prefix", op=FilterOp.EQ, value=prefix),
                ],
            ),
        )
        for doc in docs:
            client = self._client_from_doc(doc)
            if not client.token_hash:
                continue
            if self._verify_token(client.token_hash, token):
                user_ctx = await self._resolve_owner(client)
                if user_ctx is None:
                    logger.warning(
                        "MCP client %s references unknown owner %s",
                        client.id,
                        client.owner_user_id,
                    )
                    return None
                # Update last_used_at / last_ip without blocking the
                # caller on a second storage round-trip failure.
                try:
                    await self._touch(client, client_ip=client_ip)
                except Exception:  # pragma: no cover
                    logger.exception(
                        "Failed to update last_used_at for %s",
                        client.id,
                    )
                return client, user_ctx
        return None

    async def _resolve_owner(
        self,
        client: MCPServerClient,
    ) -> UserContext | None:
        """Look up the owner user and build a ``UserContext``.

        Falls back to the users service capability — we don't import
        it directly to respect the layer rules."""
        if self._resolver is None:
            return None
        users_svc = self._resolver.get_capability("users")
        if not isinstance(users_svc, UserManagementProvider):
            return None
        user = await users_svc.backend.get_user(client.owner_user_id)
        if user is None:
            return None
        return UserContext(
            user_id=str(user.get("user_id") or client.owner_user_id),
            email=str(user.get("email") or ""),
            display_name=str(user.get("display_name") or ""),
            roles=frozenset(user.get("roles") or ()),
            provider=str(user.get("provider") or "local"),
            session_id=f"mcp_client:{client.id}",
            metadata={"mcp_client_id": client.id},
        )

    async def _touch(
        self,
        client: MCPServerClient,
        *,
        client_ip: str,
    ) -> None:
        assert self._storage is not None
        doc = await self._storage.get(MCP_CLIENTS_COLLECTION, client.id)
        if doc is None:
            return
        doc["last_used_at"] = datetime.now(UTC).isoformat()
        if client_ip:
            doc["last_ip"] = client_ip
        await self._storage.put(MCP_CLIENTS_COLLECTION, client.id, doc)

    # ── CRUD ─────────────────────────────────────────────────────────

    async def list_clients(self) -> list[MCPServerClient]:
        assert self._storage is not None
        docs = await self._storage.query(
            Query(collection=MCP_CLIENTS_COLLECTION),
        )
        return [self._client_from_doc(d) for d in docs]

    async def get_client(self, client_id: str) -> MCPServerClient | None:
        assert self._storage is not None
        doc = await self._storage.get(MCP_CLIENTS_COLLECTION, client_id)
        return self._client_from_doc(doc) if doc else None

    async def create_client(
        self,
        *,
        name: str,
        owner_user_id: str,
        ai_profile: str = "standard",
        description: str = "",
    ) -> tuple[MCPServerClient, str]:
        """Create a new client and return ``(client, plaintext_token)``.

        The plaintext token is shown to the caller exactly once — the
        caller MUST surface it to the user at create time because
        Gilbert never stores it and can't produce it again."""
        assert self._storage is not None
        if not name.strip():
            raise ValueError("Client name is required")
        if not owner_user_id.strip():
            raise ValueError("owner_user_id is required")
        if not ai_profile.strip():
            raise ValueError("ai_profile is required")

        now = datetime.now(UTC)
        plaintext = self._generate_token()
        client = MCPServerClient(
            id=str(uuid.uuid4()),
            name=name.strip(),
            description=description.strip(),
            owner_user_id=owner_user_id.strip(),
            ai_profile=ai_profile.strip(),
            active=True,
            token_hash=self._hash_token(plaintext),
            token_prefix=plaintext[: len(TOKEN_PREFIX) + 6],
            created_at=now,
            updated_at=now,
        )
        await self._storage.put(
            MCP_CLIENTS_COLLECTION,
            client.id,
            self._doc_from_client(client),
        )
        return client, plaintext

    async def update_client(
        self,
        client_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        ai_profile: str | None = None,
        active: bool | None = None,
    ) -> MCPServerClient:
        """Update non-secret fields. Use ``rotate_token`` to issue a new
        bearer — this method deliberately doesn't touch the hash."""
        assert self._storage is not None
        existing = await self.get_client(client_id)
        if existing is None:
            raise LookupError(f"Client not found: {client_id}")
        updated = replace(
            existing,
            name=name.strip() if name is not None else existing.name,
            description=(description.strip() if description is not None else existing.description),
            ai_profile=(ai_profile.strip() if ai_profile is not None else existing.ai_profile),
            active=active if active is not None else existing.active,
            updated_at=datetime.now(UTC),
        )
        if not updated.name:
            raise ValueError("Client name is required")
        if not updated.ai_profile:
            raise ValueError("ai_profile is required")
        await self._storage.put(
            MCP_CLIENTS_COLLECTION,
            client_id,
            self._doc_from_client(updated),
        )
        return updated

    async def rotate_token(self, client_id: str) -> tuple[MCPServerClient, str]:
        """Issue a new bearer token and invalidate the old one.

        Returns ``(client, plaintext_token)`` — same one-shot-reveal
        contract as ``create_client``."""
        assert self._storage is not None
        existing = await self.get_client(client_id)
        if existing is None:
            raise LookupError(f"Client not found: {client_id}")
        plaintext = self._generate_token()
        updated = replace(
            existing,
            token_hash=self._hash_token(plaintext),
            token_prefix=plaintext[: len(TOKEN_PREFIX) + 6],
            updated_at=datetime.now(UTC),
        )
        await self._storage.put(
            MCP_CLIENTS_COLLECTION,
            client_id,
            self._doc_from_client(updated),
        )
        return updated, plaintext

    async def delete_client(self, client_id: str) -> None:
        assert self._storage is not None
        await self._storage.delete(MCP_CLIENTS_COLLECTION, client_id)

    # ── WS RPC handlers ──────────────────────────────────────────────

    def get_ws_handlers(self) -> dict[str, RpcHandler]:
        """All handlers are admin-only via ``interfaces/acl.py``
        defaults — registering an MCP client means granting an
        external process permission to impersonate a Gilbert user,
        which is squarely an admin operation."""
        return {
            "mcp.clients.list": self._ws_list,
            "mcp.clients.get": self._ws_get,
            "mcp.clients.create": self._ws_create,
            "mcp.clients.update": self._ws_update,
            "mcp.clients.delete": self._ws_delete,
            "mcp.clients.rotate_token": self._ws_rotate,
            "mcp.clients.preview_tools": self._ws_preview_tools,
        }

    async def _ws_list(
        self,
        conn: WsConnectionBase,
        frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not self._is_admin(conn.user_ctx):
            return _ws_error(frame, "Admin access required", code=403)
        clients = await self.list_clients()
        return {
            "type": "mcp.clients.list.result",
            "ref": frame.get("id"),
            "clients": [self._serialize_client(c) for c in clients],
        }

    async def _ws_get(
        self,
        conn: WsConnectionBase,
        frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not self._is_admin(conn.user_ctx):
            return _ws_error(frame, "Admin access required", code=403)
        client_id = str(frame.get("client_id") or "").strip()
        if not client_id:
            return _ws_error(frame, "Missing 'client_id'")
        client = await self.get_client(client_id)
        if client is None:
            return _ws_error(frame, "Client not found", code=404)
        return {
            "type": "mcp.clients.get.result",
            "ref": frame.get("id"),
            "client": self._serialize_client(client),
        }

    async def _ws_create(
        self,
        conn: WsConnectionBase,
        frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not self._is_admin(conn.user_ctx):
            return _ws_error(frame, "Admin access required", code=403)
        payload = frame.get("client") or {}
        if not isinstance(payload, dict):
            return _ws_error(frame, "Missing or invalid 'client' payload")
        try:
            client, token = await self.create_client(
                name=str(payload.get("name") or ""),
                owner_user_id=str(payload.get("owner_user_id") or ""),
                ai_profile=str(payload.get("ai_profile") or "standard"),
                description=str(payload.get("description") or ""),
            )
        except ValueError as exc:
            return _ws_error(frame, str(exc))
        return {
            "type": "mcp.clients.create.result",
            "ref": frame.get("id"),
            "client": self._serialize_client(client),
            "token": token,
        }

    async def _ws_update(
        self,
        conn: WsConnectionBase,
        frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not self._is_admin(conn.user_ctx):
            return _ws_error(frame, "Admin access required", code=403)
        client_id = str(frame.get("client_id") or "").strip()
        if not client_id:
            return _ws_error(frame, "Missing 'client_id'")
        payload = frame.get("client") or {}
        if not isinstance(payload, dict):
            return _ws_error(frame, "Missing or invalid 'client' payload")
        try:
            client = await self.update_client(
                client_id,
                name=payload.get("name"),
                description=payload.get("description"),
                ai_profile=payload.get("ai_profile"),
                active=payload.get("active"),
            )
        except LookupError:
            return _ws_error(frame, "Client not found", code=404)
        except ValueError as exc:
            return _ws_error(frame, str(exc))
        return {
            "type": "mcp.clients.update.result",
            "ref": frame.get("id"),
            "client": self._serialize_client(client),
        }

    async def _ws_delete(
        self,
        conn: WsConnectionBase,
        frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not self._is_admin(conn.user_ctx):
            return _ws_error(frame, "Admin access required", code=403)
        client_id = str(frame.get("client_id") or "").strip()
        if not client_id:
            return _ws_error(frame, "Missing 'client_id'")
        await self.delete_client(client_id)
        return {
            "type": "mcp.clients.delete.result",
            "ref": frame.get("id"),
            "client_id": client_id,
        }

    async def _ws_rotate(
        self,
        conn: WsConnectionBase,
        frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not self._is_admin(conn.user_ctx):
            return _ws_error(frame, "Admin access required", code=403)
        client_id = str(frame.get("client_id") or "").strip()
        if not client_id:
            return _ws_error(frame, "Missing 'client_id'")
        try:
            client, token = await self.rotate_token(client_id)
        except LookupError:
            return _ws_error(frame, "Client not found", code=404)
        return {
            "type": "mcp.clients.rotate_token.result",
            "ref": frame.get("id"),
            "client": self._serialize_client(client),
            "token": token,
        }

    async def _ws_preview_tools(
        self,
        conn: WsConnectionBase,
        frame: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Preview the tool surface a would-be client would see.

        Drives the Create/Edit Client dialog's "this client would see
        X tools" panel. Runs the same ``ai.discover_tools`` path the
        live MCP server endpoint uses so the preview is authoritative
        — admins see exactly what the client will get before they
        issue a token."""
        if not self._is_admin(conn.user_ctx):
            return _ws_error(frame, "Admin access required", code=403)
        owner_user_id = str(frame.get("owner_user_id") or "").strip()
        profile_name = str(frame.get("profile_name") or "").strip()
        if not owner_user_id:
            return _ws_error(frame, "Missing 'owner_user_id'")
        if not profile_name:
            return _ws_error(frame, "Missing 'profile_name'")

        user_ctx = await self._build_user_ctx(owner_user_id)
        if user_ctx is None:
            return _ws_error(
                frame,
                f"Unknown owner user: {owner_user_id}",
                code=404,
            )

        if self._resolver is None:
            return _ws_error(frame, "Service not started", code=503)
        ai_svc = self._resolver.get_capability("ai")
        if not isinstance(ai_svc, AIToolDiscoveryProvider):
            return _ws_error(frame, "AI service unavailable", code=503)

        try:
            discovered = ai_svc.discover_tools(
                user_ctx=user_ctx,
                profile_name=profile_name,
            )
        except Exception as exc:  # noqa: BLE001
            return _ws_error(frame, f"discover_tools failed: {exc}")

        tools = [
            {
                "name": tool_def.name,
                "description": tool_def.description,
                "required_role": tool_def.required_role,
            }
            for _, tool_def in discovered.values()
        ]
        tools.sort(key=lambda t: t["name"])
        return {
            "type": "mcp.clients.preview_tools.result",
            "ref": frame.get("id"),
            "owner_user_id": owner_user_id,
            "profile_name": profile_name,
            "tool_count": len(tools),
            "tools": tools,
        }

    async def _build_user_ctx(
        self,
        owner_user_id: str,
    ) -> UserContext | None:
        """Shared helper between auth lookup and tool preview —
        resolves a user id into a ``UserContext``. Returns ``None``
        when the id doesn't exist in the users backend."""
        if self._resolver is None:
            return None
        users_svc = self._resolver.get_capability("users")
        if not isinstance(users_svc, UserManagementProvider):
            return None
        user = await users_svc.backend.get_user(owner_user_id)
        if user is None:
            return None
        return UserContext(
            user_id=str(user.get("user_id") or owner_user_id),
            email=str(user.get("email") or ""),
            display_name=str(user.get("display_name") or ""),
            roles=frozenset(user.get("roles") or ()),
            provider=str(user.get("provider") or "local"),
            session_id=f"mcp_client_preview:{owner_user_id}",
        )

    # ── helpers ──────────────────────────────────────────────────────

    def _is_admin(self, user_ctx: UserContext) -> bool:
        acl = self._acl_svc
        if acl is None or not isinstance(acl, AccessControlProvider):
            return "admin" in user_ctx.roles
        return bool(acl.get_effective_level(user_ctx) <= acl.get_role_level("admin"))

    @staticmethod
    def _serialize_client(client: MCPServerClient) -> dict[str, Any]:
        """JSON-safe view of a client record. Never includes the
        token hash — the only time any token material leaves the
        server is the one-shot plaintext return from
        ``create_client`` / ``rotate_token``."""
        return {
            "id": client.id,
            "name": client.name,
            "description": client.description,
            "owner_user_id": client.owner_user_id,
            "ai_profile": client.ai_profile,
            "active": client.active,
            "token_prefix": client.token_prefix,
            "created_at": (client.created_at.isoformat() if client.created_at else None),
            "updated_at": (client.updated_at.isoformat() if client.updated_at else None),
            "last_used_at": (client.last_used_at.isoformat() if client.last_used_at else None),
            "last_ip": client.last_ip,
        }

    @staticmethod
    def _doc_from_client(client: MCPServerClient) -> dict[str, Any]:
        return {
            "_id": client.id,
            "name": client.name,
            "description": client.description,
            "owner_user_id": client.owner_user_id,
            "ai_profile": client.ai_profile,
            "active": client.active,
            "token_hash": client.token_hash,
            "token_prefix": client.token_prefix,
            "created_at": (client.created_at.isoformat() if client.created_at else None),
            "updated_at": (client.updated_at.isoformat() if client.updated_at else None),
            "last_used_at": (client.last_used_at.isoformat() if client.last_used_at else None),
            "last_ip": client.last_ip,
        }

    @staticmethod
    def _client_from_doc(doc: dict[str, Any]) -> MCPServerClient:
        return MCPServerClient(
            id=str(doc.get("_id") or ""),
            name=str(doc.get("name") or ""),
            description=str(doc.get("description") or ""),
            owner_user_id=str(doc.get("owner_user_id") or ""),
            ai_profile=str(doc.get("ai_profile") or "standard"),
            active=bool(doc.get("active", True)),
            token_hash=str(doc.get("token_hash") or ""),
            token_prefix=str(doc.get("token_prefix") or ""),
            created_at=_parse_dt(doc.get("created_at")),
            updated_at=_parse_dt(doc.get("updated_at")),
            last_used_at=_parse_dt(doc.get("last_used_at")),
            last_ip=str(doc.get("last_ip") or ""),
        )


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _ws_error(
    frame: dict[str, Any],
    error: str,
    *,
    code: int = 400,
) -> dict[str, Any]:
    return {
        "type": "gilbert.error",
        "ref": frame.get("id"),
        "error": error,
        "code": code,
    }
