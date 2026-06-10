"""Shared subagent type definition + catalog capability protocol.

A subagent *type* is a self-contained agent definition: model selection
(an AI profile, or raw backend/model/temperature), tool gating, round/time
budget, a system prompt, and an execution mode (sync vs background) +
delivery (inline vs report file). Types are stored as entities
(``subagent_types``) and managed by admins.

The dataclass lives here (``interfaces/``) because it is shared data: the
ephemeral ``SubagentService`` owns the catalog, and ``AgentService`` reads it
(durable agents reference a type for execution defaults). Per the layer rules,
shared data used by multiple services belongs in ``interfaces/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class SubagentType:
    id: str
    name: str
    description: str
    system_prompt: str
    # Model selection AND tool gating come from the referenced AI profile
    # (an AIContextProfile owns ``tool_mode``/``tools`` + model). ``ai_profile``
    # is the preferred, model-agnostic selector; when empty, the raw
    # ``backend``/``model``/``temperature`` fields apply (with all tools, since
    # there's no profile to gate them). A per-call model override beats both.
    ai_profile: str = ""
    backend: str = ""
    model: str = ""
    temperature: float | None = None
    max_tokens: int | None = None
    max_rounds: int = 12
    max_wall_clock_s: float | None = 300.0
    execution_mode: str = "sync"  # sync | background
    deliver_as: str = "inline"  # inline | report_file
    enabled: bool = True
    built_in: bool = False
    icon: str = ""


@runtime_checkable
class SubagentCatalog(Protocol):
    """Read surface for the subagent type catalog.

    Consumers (e.g. ``AgentService``) resolve this via
    ``resolver.get_capability("subagent")`` and ``isinstance``-check it to read
    types without importing the concrete ``SubagentService``.
    """

    def list_types(self) -> list[SubagentType]: ...

    def get_type(self, type_id: str) -> SubagentType | None: ...
