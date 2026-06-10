"""Built-in subagent types.

A subagent *type* is an immutable definition the engine runs: a description
(the routing hint the spawning model sees), a type-specific system prompt
(prepended with a shared headless preamble at run time), and a reference to an
AI profile that supplies the model + tool gating. The type never names a
backend/model itself — that lives in the profile (per the AI-backend-visibility
rule). v1 ships built-ins only; user-defined types are a later slice.
"""

from __future__ import annotations

from dataclasses import dataclass

_GENERAL_PURPOSE_PROMPT = (
    "You are a general-purpose subagent. Complete the assigned task thoroughly "
    "and autonomously using the tools available to you. Gather what you need, "
    "reason it through, and report your findings and conclusions as your final "
    "message."
)


@dataclass(frozen=True)
class AgentType:
    """An immutable subagent type definition."""

    id: str
    description: str
    system_prompt: str
    profile_name: str = "standard"
    max_rounds: int = 12
    max_wall_clock_s: float | None = 300.0


_GENERAL_PURPOSE = AgentType(
    id="general-purpose",
    description=(
        "General-purpose agent for researching complex questions and executing "
        "multi-step tasks autonomously in a fresh context. Use when a task "
        "benefits from focused, independent work and you don't need a more "
        "specialized agent."
    ),
    system_prompt=_GENERAL_PURPOSE_PROMPT,
    profile_name="standard",
    max_rounds=12,
)

_DEEP_RESEARCH_PROMPT = (
    "You are a deep-research subagent. Investigate the question thoroughly and "
    "autonomously: plan what you need to find, search the web, read the most "
    "relevant pages in full, and cross-check claims across multiple independent "
    "sources. Iterate — search again to fill gaps — until you can answer with "
    "confidence. Then write a clear, well-structured report in Markdown that "
    "directly addresses the question, with inline citations (page title + URL) "
    "for every non-obvious claim and a 'Sources' list at the end. Prefer primary "
    "sources; surface uncertainty and disagreements between sources rather than "
    "smoothing them over."
    " Handle both broad, open-domain questions and specialized or academic ones."
    " Rely on credible, diverse sources and stay objective. When you read a page,"
    " extract the most relevant evidence while preserving its full original"
    " context, and weigh how much it actually answers the question before moving"
    " on."
    " When you have media (an image, chart, or file) you saved to the workspace,"
    " embed it in the report with a relative Markdown link like"
    " ![caption](outputs/<file>). Produce the full report as your final message"
    " in Markdown — it will be saved as a file and linked into the chat."
    " IMPORTANT — you have a LIMITED number of research steps. Budget them: do"
    " NOT spend them all searching. Once you have enough material to answer"
    " (typically after a handful of focused searches), STOP searching and WRITE"
    " the report. Writing the final report is mandatory — running out of steps"
    " mid-search without a written report is a failure. If you sense you are"
    " running low on steps, write the report immediately with what you have."
)

_DEEP_RESEARCH = AgentType(
    id="deep-research",
    description=(
        "Deep web research: a long-horizon agent that searches, reads pages, "
        "cross-checks sources, and returns a cited Markdown report. Use for "
        "questions needing current information or synthesis across many sources."
    ),
    system_prompt=_DEEP_RESEARCH_PROMPT,
    profile_name="deep-research",
    max_rounds=40,
    max_wall_clock_s=900.0,
)

BUILTIN_AGENT_TYPES: dict[str, AgentType] = {t.id: t for t in (_GENERAL_PURPOSE, _DEEP_RESEARCH)}


def get_agent_type(type_id: str) -> AgentType | None:
    """Return the built-in agent type with this id, or ``None``."""
    return BUILTIN_AGENT_TYPES.get(type_id)


def list_agent_types() -> list[AgentType]:
    """Return all built-in agent types."""
    return list(BUILTIN_AGENT_TYPES.values())
