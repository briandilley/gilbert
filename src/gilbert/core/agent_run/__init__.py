"""Shared single-agent execution engine.

The per-run shaping that sits *underneath* a chat turn — system prompt +
model/profile + tool gating + round/wall-clock budget + the budget-exhaustion
synthesis fallback + lifecycle events — extracted into one engine so both
``SubagentService.spawn()`` (ephemeral, headless) and
``AgentService._run_agent_internal()`` (durable, non-headless) build a
``RunSpec`` and call it instead of duplicating the logic. The engine is
parameterized on ``headless`` and never forces it, so the subagent no-nesting
gate stays correct while durable agents keep their peer/delegate tools.
"""

from gilbert.core.agent_run.engine import AgentRunEngine, RunResult, RunSpec

__all__ = ["AgentRunEngine", "RunResult", "RunSpec"]
