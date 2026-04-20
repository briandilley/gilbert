"""Usage tracking interfaces — record and query AI token usage."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from gilbert.interfaces.ai import TokenUsage
from gilbert.interfaces.auth import UserContext


@dataclass(frozen=True)
class UsageRecord:
    """One AI round's worth of token consumption.

    Written by ``UsageRecorder.record_round`` and returned (flattened to
    dicts) from ``UsageProvider.query_usage``.
    """

    timestamp: datetime
    user_id: str
    user_name: str
    conversation_id: str
    profile: str
    backend: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    cost_usd: float
    tool_names: tuple[str, ...] = ()
    stop_reason: str = ""
    round_num: int = 0
    invocation_source: str = "chat"
    """Free-form label describing where the call came from —
    ``chat`` | ``slash`` | ``mcp_sampling`` | ``ai_call:<name>``."""


@dataclass(frozen=True)
class ModelPricing:
    """Per-million-token USD pricing for a single model.

    Stored in ``UsageService`` configuration keyed by
    ``backend/model``. ``cache_creation_per_mtok`` is Anthropic-only; OpenAI
    writes cost is baked into ``input_per_mtok``. ``cache_read_per_mtok``
    defaults to 10% of ``input_per_mtok`` if not set.
    """

    input_per_mtok: float
    output_per_mtok: float
    cache_creation_per_mtok: float = 0.0
    cache_read_per_mtok: float = 0.0


@dataclass
class UsageQuery:
    """Filter + aggregation spec for ``UsageProvider.query_usage``."""

    start: datetime | None = None
    end: datetime | None = None
    user_id: str | None = None
    conversation_id: str | None = None
    backend: str | None = None
    model: str | None = None
    profile: str | None = None
    tool_name: str | None = None
    group_by: tuple[str, ...] = ()
    """Field names to group by — any subset of
    ``user_id`` | ``user_name`` | ``backend`` | ``model`` | ``profile`` |
    ``conversation_id`` | ``tool_name`` | ``date`` (YYYY-MM-DD bucket)."""


@dataclass(frozen=True)
class UsageAggregate:
    """One row from a grouped ``query_usage`` result.

    ``dimensions`` is keyed by the ``group_by`` fields requested. A raw
    (ungrouped) query returns one ``UsageAggregate`` per matching round
    with the individual ``UsageRecord`` fields copied into ``dimensions``.
    """

    dimensions: dict[str, str] = field(default_factory=dict)
    rounds: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0


@runtime_checkable
class UsageRecorder(Protocol):
    """Protocol for recording AI token usage.

    The ``AIService`` agentic loop calls this after every MESSAGE_COMPLETE
    event. ``UsageService`` is the canonical implementation; tests can
    supply a fake. Implementations MUST be cheap + fire-and-forget safe —
    the recording call sits on the hot path of every AI round.
    """

    async def record_round(
        self,
        *,
        user_ctx: UserContext,
        conversation_id: str,
        profile: str,
        backend: str,
        model: str,
        usage: TokenUsage,
        tool_names: list[str],
        stop_reason: str,
        round_num: int,
        invocation_source: str = "chat",
    ) -> UsageRecord:
        """Persist one AI round's usage, return the record with cost computed.

        The returned ``UsageRecord`` carries the cost the implementation
        computed so callers (AIService) can attach it to ``turn_rounds``
        without a second pricing lookup.
        """
        ...


@runtime_checkable
class UsagePricingProvider(Protocol):
    """Protocol for looking up per-model pricing without persisting a record.

    Lets the chat UI pipeline (or any future consumer) compute cost for a
    preview without going through ``record_round``.
    """

    def compute_cost(
        self,
        *,
        backend: str,
        model: str,
        usage: TokenUsage,
    ) -> float:
        """Return USD cost for this round given the pricing table."""
        ...


@runtime_checkable
class UsageProvider(Protocol):
    """Protocol for reading + aggregating AI token usage history."""

    async def query_usage(self, spec: UsageQuery) -> list[UsageAggregate]:
        """Execute a usage query, returning one ``UsageAggregate`` per
        group (or one per matching round when ``spec.group_by`` is empty).
        """
        ...

    async def list_models_with_usage(self) -> list[dict[str, Any]]:
        """Return distinct ``{backend, model}`` pairs seen in usage history.

        Drives filter dropdowns in reporting UIs.
        """
        ...
