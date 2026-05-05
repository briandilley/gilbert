"""Unit tests for AgentService — skeleton (Task 3).

Covers:
- service_info() declares required capabilities.
- AgentService satisfies the AgentProvider runtime-checkable protocol.
"""

from __future__ import annotations

from gilbert.interfaces.agent import AgentProvider
from gilbert.interfaces.service import ServiceInfo


def test_service_info_declares_capabilities() -> None:
    """service_info() returns correct name, capabilities, requires, and ai_calls."""
    from gilbert.core.services.agent import AgentService

    svc = AgentService()
    info = svc.service_info()

    assert isinstance(info, ServiceInfo)
    assert info.name == "agent"

    # Declared capabilities
    assert "agent" in info.capabilities
    assert "ai_tools" in info.capabilities
    assert "ws_handlers" in info.capabilities

    # Declared dependencies
    assert "entity_storage" in info.requires
    assert "event_bus" in info.requires
    assert "ai_chat" in info.requires
    assert "scheduler" in info.requires

    # AI call budget declarations
    assert "agent.run" in info.ai_calls


def test_agent_service_satisfies_agent_provider() -> None:
    """AgentService structurally satisfies the AgentProvider runtime-checkable Protocol.

    The Protocol verifies method *presence*, not behavior, so NotImplementedError
    stubs are sufficient.
    """
    from gilbert.core.services.agent import AgentService

    svc = AgentService()
    assert isinstance(svc, AgentProvider)
