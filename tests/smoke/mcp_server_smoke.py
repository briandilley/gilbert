"""Manual smoke test for Part 4.2 — Gilbert as an MCP server.

Boots the Gilbert FastAPI app in-process, creates an MCP client
registration, then drives a real ``streamablehttp_client`` against
``/mcp`` to verify the end-to-end flow:

1. 401 returned for unauthenticated requests.
2. 401 returned for malformed bearer tokens.
3. Successful auth → ``list_tools`` returns the tools discovered
   under the configured AI profile, filtered by the owner's role.
4. ``call_tool`` dispatches through Gilbert's registry and returns
   the tool's result.
5. After a rotate, the old token no longer authenticates and the
   new one does.

Requires no real backend — we stub the tool-providing services with
a fake ``echo_server`` tool registered via a minimal ToolProvider.

Run from the repo root::

    uv run python tests/smoke/mcp_server_smoke.py
"""

from __future__ import annotations

import asyncio
import socket
import sys
import time
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402

from gilbert.core.services.ai import (  # noqa: E402
    _BUILTIN_PROFILES,
    AIContextProfile,
)
from gilbert.core.services.mcp_server import MCPServerService  # noqa: E402
from gilbert.core.services.mcp_server_http import MCPServerHttpApp  # noqa: E402
from gilbert.interfaces.auth import UserContext  # noqa: E402
from gilbert.interfaces.service import (  # noqa: E402
    Service,
    ServiceInfo,
    ServiceResolver,
)
from gilbert.interfaces.storage import StorageBackend  # noqa: E402
from gilbert.interfaces.tools import (  # noqa: E402
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)
from tests.unit.test_mcp_service import FakeACL, FakeStorage  # noqa: E402

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"


# ── Stubs ───────────────────────────────────────────────────────────


class _EchoToolProvider(Service):
    """Minimal ToolProvider exposing a single ``echo`` tool."""

    tool_provider_name = "echo"

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="echo",
            capabilities=frozenset({"ai_tools"}),
        )

    def get_tools(
        self,
        user_ctx: UserContext | None = None,
    ) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="echo",
                description="Echo the input text.",
                parameters=[
                    ToolParameter(
                        name="text",
                        type=ToolParameterType.STRING,
                        description="Text to echo.",
                    ),
                ],
                required_role="user",
            ),
        ]

    async def execute_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> str:
        if name != "echo":
            raise KeyError(name)
        return f"echoed: {arguments.get('text', '')}"


class _FakeAIService(Service):
    """Stand-in for AIService exposing just ``discover_tools``."""

    def __init__(self, providers: list[_EchoToolProvider]) -> None:
        self._providers = providers
        self._profiles = {p.name: p for p in _BUILTIN_PROFILES}

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(name="ai", capabilities=frozenset({"ai"}))

    def discover_tools(
        self,
        *,
        user_ctx: UserContext,
        profile_name: str | None = None,
    ) -> dict[str, tuple[Any, ToolDefinition]]:
        profile: AIContextProfile | None = None
        if profile_name and profile_name in self._profiles:
            profile = self._profiles[profile_name]
        out: dict[str, tuple[Any, ToolDefinition]] = {}
        for prov in self._providers:
            for tool_def in prov.get_tools(user_ctx):
                if (
                    profile is None
                    or profile.tool_mode == "all"
                    or (profile.tool_mode == "include" and tool_def.name in profile.tools)
                    or (profile.tool_mode == "exclude" and tool_def.name not in profile.tools)
                ):
                    out[tool_def.name] = (prov, tool_def)
        return out


class _FakeUsersService(Service):
    def __init__(self, users: dict[str, dict[str, Any]]) -> None:
        self._users = users

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(name="users", capabilities=frozenset({"users"}))

    async def get_user(self, user_id: str) -> dict[str, Any] | None:
        return self._users.get(user_id)


class _FakeStorageService(Service):
    """Service-shaped wrapper around FakeStorage so the resolver can
    expose it under the ``entity_storage`` capability."""

    def __init__(self, backend: StorageBackend) -> None:
        self._backend = backend

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="entity_storage",
            capabilities=frozenset({"entity_storage"}),
        )

    @property
    def backend(self) -> StorageBackend:
        return self._backend

    @property
    def raw_backend(self) -> StorageBackend:
        return self._backend

    def create_namespaced(self, namespace: str) -> Any:
        raise NotImplementedError


class _FakeResolver(ServiceResolver):
    def __init__(self, caps: dict[str, Any]) -> None:
        self._caps = caps

    def get_capability(self, capability: str) -> Service | None:
        svc = self._caps.get(capability)
        return svc if isinstance(svc, Service) else None

    def require_capability(self, capability: str) -> Service:
        svc = self.get_capability(capability)
        if svc is None:
            raise LookupError(capability)
        return svc

    def get_all(self, capability: str) -> list[Service]:
        svc = self._caps.get(capability)
        return [svc] if isinstance(svc, Service) else []


# ── App factory ─────────────────────────────────────────────────────


async def build_test_app() -> tuple[FastAPI, MCPServerService]:
    """Wire together a minimal FastAPI app with:
    - a fake AI service returning the echo tool
    - a fake users service with a single ``alice`` user
    - an MCPServerService backed by FakeStorage
    - the ``/mcp`` route mounted
    """
    app = FastAPI()

    echo = _EchoToolProvider()
    ai_svc = _FakeAIService([echo])
    users_svc = _FakeUsersService(
        {
            "alice": {
                "user_id": "alice",
                "email": "alice@example.com",
                "display_name": "Alice",
                "roles": ["user"],
            },
        },
    )
    mcp_svc = MCPServerService()
    storage_svc = _FakeStorageService(FakeStorage())
    resolver = _FakeResolver(
        {
            "ai": ai_svc,
            "users": users_svc,
            "access_control": FakeACL(),
            "entity_storage": storage_svc,
        },
    )
    await mcp_svc.start(resolver)

    http_app = MCPServerHttpApp(resolver)
    app.state.mcp_http_app = http_app

    @app.on_event("shutdown")
    async def _stop_http() -> None:
        try:
            await http_app.stop()
        except Exception:
            pass

    # Mount the same raw-ASGI endpoint the production app uses so
    # the smoke test exercises the real code path rather than a
    # parallel FastAPI route.
    from starlette.routing import Route as _StarletteRoute

    # For the smoke test we wrap a tiny service-manager stand-in
    # that exposes ``get_by_capability("mcp_server")`` so the raw
    # ASGI handler can find the service we just built.
    class _SmokeServiceManager:
        # ``self_inner`` avoids shadowing the outer closure's ``resolver``
        # binding if ruff's ``self`` rename ever flagged this as an issue.
        def get_by_capability(self, name: str) -> Any:  # noqa: N805
            if name == "mcp_server":
                return mcp_svc
            return resolver.get_capability(name)

    class _SmokeGilbert:
        service_manager = _SmokeServiceManager()

    app.state.gilbert = _SmokeGilbert()

    from gilbert.web.routes.mcp import mcp_asgi_endpoint

    app.router.routes.append(
        _StarletteRoute(
            "/mcp",
            endpoint=mcp_asgi_endpoint,
            methods=["GET", "POST", "DELETE", "OPTIONS"],
            include_in_schema=False,
        ),
    )

    return app, mcp_svc


def pick_free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def wait_for_port(port: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.1)
    raise RuntimeError(f"server on port {port} didn't come up within {timeout}s")


# ── Smoke scenarios ─────────────────────────────────────────────────


async def drive_client(url: str, token: str) -> dict[str, Any]:
    """Connect a real SDK streamablehttp_client and run list_tools +
    call_tool. Returns a dict with the results so the harness can
    assert on them."""
    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_result = await session.list_tools()
            tool_names = [t.name for t in tools_result.tools]
            call_result = await session.call_tool(
                "echo",
                {"text": "hello from smoke"},
            )
            return {
                "tool_names": tool_names,
                "call_text": (call_result.content[0].text if call_result.content else ""),
                "call_is_error": call_result.isError,
            }


async def try_unauthenticated(url: str) -> int:
    import httpx

    async with httpx.AsyncClient() as client:
        r = await client.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
        return r.status_code


async def main() -> int:
    results: list[tuple[bool, str]] = []

    def record(label: str, passed: bool, detail: str = "") -> None:
        mark = PASS if passed else FAIL
        results.append((passed, label))
        print(f"  {mark} {label}")
        if detail:
            print(f"      {detail}")

    print("=== MCP server smoke test ===\n")

    app, mcp_svc = await build_test_app()

    # Create a client with a known owner. Note: we re-run service
    # setup here because the FastAPI app factory's mini-event-loop
    # already closed; create_client is async so we call it under
    # this process's main loop.
    client, token = await mcp_svc.create_client(
        name="Smoke Client",
        owner_user_id="alice",
        ai_profile="standard",
    )

    # Boot uvicorn in a background task so we can drive HTTP.
    port = pick_free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    try:
        await wait_for_port(port)
        url = f"http://127.0.0.1:{port}/mcp"

        # 1. Unauthenticated → 401
        try:
            status = await try_unauthenticated(url)
            record(
                "unauthenticated POST returns 401",
                status == 401,
                f"status={status}",
            )
        except Exception as exc:
            record("unauthenticated POST returns 401", False, str(exc))

        # 2. Bad token → client can't initialize. The SDK wraps the
        # underlying 401 in a task-group exception so we match on
        # "any failure" rather than the text.
        try:
            rt = await drive_client(url, "mcpc_totallyfake")
            record("bad token rejected", False, f"unexpected success: {rt}")
        except Exception as exc:
            record(
                "bad token rejected",
                True,
                f"raised: {type(exc).__name__}",
            )

        # 3. Authenticated round-trip
        try:
            rt = await drive_client(url, token)
            assert "echo" in rt["tool_names"], f"got tools: {rt['tool_names']}"
            assert "hello from smoke" in rt["call_text"], f"got: {rt['call_text']}"
            assert rt["call_is_error"] is False
            record(
                "authenticated round-trip (list_tools + call_tool)",
                True,
                f"tool_names={rt['tool_names']}, call_text={rt['call_text']!r}",
            )
        except Exception as exc:
            record(
                "authenticated round-trip (list_tools + call_tool)",
                False,
                f"{type(exc).__name__}: {exc}",
            )

        # 4. Rotate invalidates old token, new one works
        try:
            _, new_token = await mcp_svc.rotate_token(client.id)
            try:
                await drive_client(url, token)
                record("rotated old token rejected", False, "still worked")
            except Exception:
                record("rotated old token rejected", True)

            rt2 = await drive_client(url, new_token)
            assert "echo" in rt2["tool_names"]
            record(
                "rotated new token works",
                True,
                f"tool_names={rt2['tool_names']}",
            )
        except Exception as exc:
            record(
                "rotate flow",
                False,
                f"{type(exc).__name__}: {exc}",
            )
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=5.0)
        except Exception:
            pass

    passed = sum(1 for ok, _ in results if ok)
    total = len(results)
    print()
    if passed == total:
        print(f"{PASS} {passed}/{total} steps passed")
        return 0
    print(f"{FAIL} {passed}/{total} steps passed")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
