"""Tests for the built-in subagent type registry."""

from __future__ import annotations

from gilbert.core.subagents.types import (
    AgentType,
    get_agent_type,
    list_agent_types,
)


def test_general_purpose_type_is_registered() -> None:
    t = get_agent_type("general-purpose")
    assert t is not None
    assert isinstance(t, AgentType)
    assert t.id == "general-purpose"
    # Description is the routing hint the parent LLM will see — must be non-empty.
    assert t.description.strip()
    assert t.system_prompt.strip()
    # References an AI profile for model + tools; never names a backend itself.
    assert t.profile_name == "standard"
    assert t.max_rounds > 0


def test_get_unknown_type_returns_none() -> None:
    assert get_agent_type("does-not-exist") is None


def test_list_agent_types_includes_general_purpose() -> None:
    ids = {t.id for t in list_agent_types()}
    assert "general-purpose" in ids


def test_deep_research_type_registered() -> None:
    t = get_agent_type("deep-research")
    assert t is not None
    assert t.profile_name == "deep-research"
    # A longer budget than general-purpose — research is long-horizon.
    assert t.max_rounds >= 16
    # The prompt asks for a cited report.
    assert "report" in t.system_prompt.lower()
    assert "cit" in t.system_prompt.lower()  # "cite"/"citation"


def test_list_agent_types_includes_both_builtins() -> None:
    ids = {t.id for t in list_agent_types()}
    assert {"general-purpose", "deep-research"} <= ids
