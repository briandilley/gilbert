"""Manual smoke test for MCP Parts 2.1 + 2.2.

Spawns a real HTTP MCP server subprocess and drives:

1. ``HttpMCPBackend`` directly — connect, list_tools, call_tool, close.
2. Bearer auth — same server with a required bearer token; verifies
   the backend's ``Authorization`` header reaches the server.
3. ``MCPService`` end-to-end — alice creates a private HTTP server,
   bob doesn't see it, alice's AI call sees the right tools.
4. OAuth components in isolation — ``EntityStorageTokenStorage`` round
   trip + ``OAuthFlowManager.begin`` / ``complete`` pair, without a
   live auth server (which would need browser interaction).

Run from the repo root::

    uv run python tests/smoke/mcp_part2_smoke.py

Prints a step-by-step report to stdout and exits non-zero on failure.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from mcp.shared.auth import OAuthToken

# Ensure repo imports resolve when run as a script.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from gilbert.core.services.mcp import MCPService  # noqa: E402
from gilbert.core.services.mcp_oauth import (  # noqa: E402
    EntityStorageTokenStorage,
    OAuthFlowManager,
)
from gilbert.integrations.mcp_http import HttpMCPBackend  # noqa: E402
from gilbert.interfaces.auth import UserContext  # noqa: E402
from gilbert.interfaces.mcp import MCPAuthConfig, MCPServerRecord  # noqa: E402

# Reuse the fake storage from the unit tests
sys.path.insert(0, str(REPO_ROOT))
from tests.unit.test_mcp_service import FakeACL, FakeStorage  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "mcp_http_echo_server.py"

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"


def pick_free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_for_port(port: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            s.close()
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"server on port {port} didn't come up within {timeout}s")


async def drive_backend(url: str, *, bearer: str | None = None) -> list[str]:
    """Drive HttpMCPBackend directly. Returns tool names."""
    backend = HttpMCPBackend()
    auth = MCPAuthConfig(kind="bearer", bearer_token=bearer) if bearer else MCPAuthConfig()
    record = MCPServerRecord(
        id="smoke",
        name="Smoke",
        slug="smoke",
        transport="http",
        url=url,
        command=(),
        owner_id="alice",
        auth=auth,
    )
    await backend.connect(record)
    try:
        tools = await backend.list_tools()
        names = [t.name for t in tools]
        result = await backend.call_tool("echo", {"text": "hello from smoke"})
        assert result.is_error is False, f"echo returned error: {result}"
        assert len(result.content) == 1
        assert result.content[0].type == "text"
        text = result.content[0].text
        assert "hello from smoke" in text, f"unexpected echo text: {text!r}"
        return names
    finally:
        await backend.close()


async def verify_bearer_rejected(url: str, wrong_token: str) -> None:
    """Confirm the server actually rejects bad tokens (otherwise our
    'bearer works' test below is meaningless)."""
    async with httpx.AsyncClient() as client:
        r = await client.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            headers={"Authorization": f"Bearer {wrong_token}"},
        )
        assert r.status_code == 401, f"expected 401, got {r.status_code}"


async def drive_through_service(url: str) -> None:
    """Exercise MCPService with a live HTTP backend: alice sees it,
    bob doesn't, alice can execute_tool through the encoded name."""
    from gilbert.interfaces.context import set_current_user

    svc = MCPService()
    svc._enabled = True
    svc._storage = FakeStorage()
    svc._acl_svc = FakeACL()

    record = MCPServerRecord(
        id="smoke-svc",
        name="SmokeSvc",
        slug="smoke-svc",
        transport="http",
        url=url,
        command=(),
        owner_id="alice",
        scope="private",
    )
    # Use the service's private start-client path so the lifecycle
    # matches production — the supervisor runs the connect
    # asynchronously, so we wait briefly for it to finish.
    entry = await svc._start_client(record)
    assert entry is not None
    for _ in range(100):
        if entry.connected:
            break
        await asyncio.sleep(0.05)
    assert entry.connected, (
        f"expected connected, got error: {entry.last_error} (retry_count={entry.retry_count})"
    )
    assert entry.tools, "expected at least one tool"

    alice = UserContext(
        user_id="alice",
        email="a@x",
        display_name="A",
        roles=frozenset({"user"}),
    )
    bob = UserContext(
        user_id="bob",
        email="b@x",
        display_name="B",
        roles=frozenset({"user"}),
    )

    alice_tools = [t.name for t in svc.get_tools(alice)]
    bob_tools = [t.name for t in svc.get_tools(bob)]
    assert "mcp__smoke-svc__echo" in alice_tools, f"alice missing tool: {alice_tools}"
    assert bob_tools == [], f"bob shouldn't see private tools: {bob_tools}"

    set_current_user(alice)
    result = await svc.execute_tool(
        "mcp__smoke-svc__echo",
        {"text": "through service"},
    )
    assert "through service" in result, f"unexpected service result: {result!r}"

    # Bob can't hit it even with the raw name.
    set_current_user(bob)
    try:
        await svc.execute_tool("mcp__smoke-svc__echo", {"text": "hack"})
        raise AssertionError("bob should have been rejected")
    except PermissionError:
        pass

    await svc._stop_client("smoke-svc")


async def drive_oauth_components() -> None:
    """Exercise the OAuth components without a live auth server."""
    storage = FakeStorage()

    # Token storage round trip
    ts = EntityStorageTokenStorage(storage, "srv1")
    assert await ts.get_tokens() is None
    await ts.set_tokens(
        OAuthToken(
            access_token="at-smoke",
            token_type="Bearer",
            expires_in=3600,
            refresh_token="rt-smoke",
            scope="read write",
        )
    )
    got = await ts.get_tokens()
    assert got is not None and got.access_token == "at-smoke"
    assert got.scope == "read write"

    # Flow manager: begin → capture state → complete → resolved
    mgr = OAuthFlowManager(storage)
    record = MCPServerRecord(
        id="oauth1",
        name="OAuth",
        slug="oauth",
        transport="http",
        url="https://example.com/mcp",
        command=(),
        owner_id="alice",
        auth=MCPAuthConfig(kind="oauth"),
    )
    state, _provider = await mgr.begin(record, "https://gilbert.local/callback")
    assert state

    # Simulate the callback route arriving
    flow = mgr._by_state[state]
    code_future = flow.code_future
    resolved = await mgr.complete(state, "auth-code-xyz", state)
    assert resolved, "complete() returned False"
    code, _ = await code_future
    assert code == "auth-code-xyz"
    await mgr.cancel(record.id)


async def main() -> int:
    steps = []

    def record(label: str, passed: bool, detail: str = "") -> None:
        mark = PASS if passed else FAIL
        steps.append((passed, label))
        line = f"  {mark} {label}"
        if detail:
            line += f"\n      {detail}"
        print(line)

    print("=== MCP Part 2 smoke test ===\n")

    # --- 1. Plain HTTP transport (no auth) ---
    print("1. HTTP transport, no auth")
    port = pick_free_port()
    proc = subprocess.Popen(
        [sys.executable, str(FIXTURE), "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for_port(port)
        url = f"http://127.0.0.1:{port}/mcp"
        try:
            names = await drive_backend(url)
            record("connect → list_tools → call_tool round-trip", True, f"tools: {names}")
        except Exception as exc:  # noqa: BLE001
            record("connect → list_tools → call_tool round-trip", False, str(exc))
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    # --- 2. HTTP transport with bearer auth ---
    print("\n2. HTTP transport, bearer auth")
    port = pick_free_port()
    proc = subprocess.Popen(
        [sys.executable, str(FIXTURE), "--port", str(port), "--bearer", "s3cret"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for_port(port)
        url = f"http://127.0.0.1:{port}/mcp"

        try:
            await verify_bearer_rejected(url, "wrong-token")
            record("server rejects a bad bearer token", True)
        except Exception as exc:  # noqa: BLE001
            record("server rejects a bad bearer token", False, str(exc))

        try:
            names = await drive_backend(url, bearer="s3cret")
            record("backend sends correct bearer → round-trip works", True, f"tools: {names}")
        except Exception as exc:  # noqa: BLE001
            record("backend sends correct bearer → round-trip works", False, str(exc))
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    # --- 3. MCPService end-to-end ---
    print("\n3. MCPService end-to-end (visibility + execute_tool)")
    port = pick_free_port()
    proc = subprocess.Popen(
        [sys.executable, str(FIXTURE), "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for_port(port)
        url = f"http://127.0.0.1:{port}/mcp"
        try:
            await drive_through_service(url)
            record("service visibility + execute_tool work against real HTTP", True)
        except Exception as exc:  # noqa: BLE001
            record("service visibility + execute_tool work against real HTTP", False, str(exc))
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    # --- 4. OAuth components in isolation ---
    print("\n4. OAuth components")
    try:
        await drive_oauth_components()
        record("token storage + flow manager behave correctly", True)
    except Exception as exc:  # noqa: BLE001
        record("token storage + flow manager behave correctly", False, str(exc))

    print()
    passed = sum(1 for ok, _ in steps if ok)
    total = len(steps)
    if passed == total:
        print(f"{PASS} {passed}/{total} steps passed")
        return 0
    print(f"{FAIL} {passed}/{total} steps passed")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
