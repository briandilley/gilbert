"""Tests for the built-in subagent type catalog."""

from __future__ import annotations

from gilbert.core.subagents.types import (
    BUILTIN_SUBAGENT_TYPES,
    SubagentType,
    builtin_seed_list,
)


def test_subagent_type_has_self_contained_fields() -> None:
    t = SubagentType(id="x", name="X", description="d", system_prompt="p")
    # Defaults
    assert t.ai_profile == ""
    assert t.tool_mode == "all"
    assert t.execution_mode == "sync"
    assert t.deliver_as == "inline"
    assert t.max_rounds == 12
    assert t.enabled is True
    assert t.built_in is False


def test_subagent_type_is_shared_interface_type() -> None:
    # The dataclass lives in interfaces/ (shared data); core re-exports it.
    from gilbert.interfaces.subagent import SubagentType as IfaceType

    assert SubagentType is IfaceType


def test_catalog_ships_expected_builtins_with_ids() -> None:
    ids = {t.id for t in builtin_seed_list()}
    assert ids == {
        "general-purpose", "deep-research", "quick-answer", "software-engineer",
        "code-reviewer", "qa-engineer", "product-manager", "market-analyst",
        "fact-checker", "summarizer",
        "durable-default",
    }
    assert all(t.built_in for t in builtin_seed_list())


def test_durable_default_is_neutral_disabled_profile() -> None:
    t = {x.id: x for x in builtin_seed_list()}["durable-default"]
    assert t.ai_profile == "standard"
    assert t.tool_mode == "all"
    assert t.system_prompt == ""
    assert t.max_rounds == 50
    assert t.max_wall_clock_s is None
    assert t.built_in is True
    # Excluded from the spawn_agent menu — it's a durable-agent profile only.
    assert t.enabled is False


def test_deep_research_and_market_analyst_are_background_report() -> None:
    by_id = {t.id: t for t in builtin_seed_list()}
    for tid in ("deep-research", "market-analyst"):
        assert by_id[tid].execution_mode == "background"
        assert by_id[tid].deliver_as == "report_file"
    # A sync/inline one for contrast
    assert by_id["software-engineer"].execution_mode == "sync"
    assert by_id["software-engineer"].temperature == 0.1


def test_builtin_prompts_are_substantial() -> None:
    for t in builtin_seed_list():
        # durable-default intentionally ships an empty role prompt (it's a
        # neutral execution profile, not a spawnable specialist).
        if t.id == "durable-default":
            continue
        assert len(t.system_prompt) > 120, t.id


def test_builtin_subagent_types_dict_keyed_by_id() -> None:
    assert set(BUILTIN_SUBAGENT_TYPES) == {t.id for t in builtin_seed_list()}
    assert BUILTIN_SUBAGENT_TYPES["deep-research"].id == "deep-research"
