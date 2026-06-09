"""The ToolDefinition.interactive flag (headless-subagent gating)."""

from __future__ import annotations

from gilbert.interfaces.tools import ToolDefinition


def test_interactive_defaults_false() -> None:
    # Existing tools are unaffected — interactive is opt-in.
    assert ToolDefinition(name="x", description="d").interactive is False


def test_interactive_can_be_set() -> None:
    t = ToolDefinition(name="spawn_agent", description="d", interactive=True)
    assert t.interactive is True
