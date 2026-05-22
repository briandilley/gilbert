"""End-to-end MCP integration test.

Spawns the ``tests/fixtures/mcp_echo_server.py`` subprocess (which uses
the real MCP SDK server side) and drives it through ``StdioMCPBackend``
+ ``MCPService``. Exercises the full transport, handshake, tool
discovery, tool invocation, and per-user visibility filter — no mocks.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from gilbert.interfaces.context import set_current_user
from gilbert.core.services.mcp import MCPService, _ClientEntry
from gilbert.integrations.mcp_stdio import StdioMCPBackend
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.mcp import MCPBackend, MCPServerRecord

ECHO_SERVER = Path(__file__).parent.parent / "fixtures" / "mcp_echo_server.py"


def _make_record(
    *,
    owner_id: str = "alice",
    scope: str = "private",
) -> MCPServerRecord:
    return MCPServerRecord(
        id="echo-server",
        name="Echo",
        slug="echo",
        transport="stdio",
        command=(sys.executable, str(ECHO_SERVER.absolute())),
        owner_id=owner_id,
        scope=scope,  # type: ignore[arg-type]
    )


async def test_stdio_backend_roundtrip() -> None:
    """Raw StdioMCPBackend: connect → list_tools → call_tool → close."""
    backend = StdioMCPBackend()
    record = _make_record()
    await backend.connect(record)
    try:
        tools = await backend.list_tools()
        names = {t.name for t in tools}
        assert names == {"echo", "add"}

        echo = next(t for t in tools if t.name == "echo")
        assert "text" in echo.input_schema["properties"]

        result = await backend.call_tool("echo", {"text": "hello"})
        assert result.is_error is False
        assert len(result.content) == 1
        assert result.content[0].type == "text"
        assert result.content[0].text == "echoed: hello"

        math = await backend.call_tool("add", {"a": 2, "b": 3})
        assert math.content[0].text == "5"
    finally:
        await backend.close()


async def test_service_end_to_end_private_visibility() -> None:
    """Drive a real MCP server through MCPService, confirm per-user visibility."""
    svc = MCPService()
    svc._enabled = True
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

    # Spin up a live backend and splice it directly into the service. We
    # bypass ``_start_client`` because that expects a wired-up storage
    # and ACL; we only want to test the tool discovery + execute path
    # here, not the full lifecycle manager.
    backend = StdioMCPBackend()
    record = _make_record(owner_id="alice", scope="private")
    await backend.connect(record)
    try:
        specs = await backend.list_tools()
        entry = _ClientEntry(record, backend)
        entry.connected = True
        entry.tools = specs
        entry.tools_fetched_at = float("inf")
        svc._clients[record.id] = entry

        # Alice sees the echo tools through the Gilbert adapter.
        alice_tools = svc.get_tools(alice)
        tool_names = {t.name for t in alice_tools}
        assert "mcp__echo__echo" in tool_names
        assert "mcp__echo__add" in tool_names

        # Bob's tool list is empty — the server is private to Alice.
        assert svc.get_tools(bob) == []

        # Alice can invoke the encoded name.
        set_current_user(alice)
        result = await svc.execute_tool("mcp__echo__echo", {"text": "hi"})
        assert result == "echoed: hi"

        # Bob cannot, even with the raw Gilbert tool name.
        set_current_user(bob)
        with pytest.raises(PermissionError):
            await svc.execute_tool("mcp__echo__echo", {"text": "hi"})
    finally:
        await backend.close()


async def test_stdio_connect_failure_cleans_up() -> None:
    """A bad command must leave the backend in a fully closed state so
    retries can use a fresh instance without leaking transport resources."""
    backend = StdioMCPBackend()
    record = MCPServerRecord(
        id="bad",
        name="Bad",
        slug="bad",
        transport="stdio",
        command=("/does/not/exist/definitely", "--nope"),
        owner_id="alice",
    )
    with pytest.raises((OSError, RuntimeError, ValueError)):
        await backend.connect(record)
    # Service state is clean; a follow-up close() is a no-op rather
    # than a crash.
    await backend.close()


async def test_stdio_registry_includes_stdio() -> None:
    """StdioMCPBackend is registered under ``stdio`` on import."""
    assert "stdio" in MCPBackend.registered_backends()
    assert MCPBackend.registered_backends()["stdio"] is StdioMCPBackend


async def test_stdio_backend_resources_roundtrip() -> None:
    """Raw StdioMCPBackend: list_resources + read_resource round-trip
    through the real echo server's resource handlers."""
    backend = StdioMCPBackend()
    record = _make_record()
    await backend.connect(record)
    try:
        resources = await backend.list_resources()
        assert len(resources) == 1
        greeting = resources[0]
        assert greeting.uri == "echo://greeting"
        assert greeting.name == "greeting"
        assert "greeting" in greeting.description
        assert greeting.mime_type == "text/plain"

        contents = await backend.read_resource("echo://greeting")
        assert len(contents) == 1
        first = contents[0]
        assert first.kind == "text"
        assert "hello from the echo server" in first.text
    finally:
        await backend.close()


async def test_stdio_backend_prompts_roundtrip() -> None:
    """Raw StdioMCPBackend: list_prompts + get_prompt (with arguments)
    round-trip through the real echo server's prompt handlers."""
    backend = StdioMCPBackend()
    record = _make_record()
    await backend.connect(record)
    try:
        prompts = await backend.list_prompts()
        assert len(prompts) == 1
        prompt = prompts[0]
        assert prompt.name == "friendly_intro"
        arg_names = {a.name for a in prompt.arguments}
        assert arg_names == {"user_name", "tone"}
        required = {a.name for a in prompt.arguments if a.required}
        assert required == {"user_name"}

        result = await backend.get_prompt(
            "friendly_intro",
            {"user_name": "Alice", "tone": "formal"},
        )
        assert len(result.messages) == 1
        msg = result.messages[0]
        assert msg.role == "user"
        assert msg.content.type == "text"
        assert "Alice" in msg.content.text
        assert "formal" in msg.content.text
    finally:
        await backend.close()
