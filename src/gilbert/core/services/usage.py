"""AI token usage tracking service.

Records one entity per AI round into the ``ai_token_usage`` collection,
computes USD cost from a per-model pricing table, and exposes a flexible
query + aggregation API for reporting.

Pricing defaults are hardcoded current public rates for the backends Gilbert
ships with; they can be overridden at runtime via the ``pricing_overrides``
config param (JSON). Prices drift over time — the overrides field is the
intended single point of adjustment.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from gilbert.interfaces.ai import TokenUsage
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import (
    Filter,
    FilterOp,
    IndexDefinition,
    Query,
    SortField,
    StorageBackend,
)
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.usage import (
    ModelPricing,
    UsageAggregate,
    UsageQuery,
    UsageRecord,
)

logger = logging.getLogger(__name__)


USAGE_COLLECTION = "ai_token_usage"


# Default per-million-token USD pricing. Sourced from public pricing pages;
# update as providers change their rates. Users can override via the
# ``pricing_overrides`` config param without a code change.
_DEFAULT_PRICING: dict[str, dict[str, ModelPricing]] = {
    "anthropic": {
        "claude-opus-4-20250514": ModelPricing(
            input_per_mtok=15.0,
            output_per_mtok=75.0,
            cache_creation_per_mtok=18.75,
            cache_read_per_mtok=1.50,
        ),
        "claude-sonnet-4-20250514": ModelPricing(
            input_per_mtok=3.0,
            output_per_mtok=15.0,
            cache_creation_per_mtok=3.75,
            cache_read_per_mtok=0.30,
        ),
        "claude-haiku-4-5-20251001": ModelPricing(
            input_per_mtok=1.0,
            output_per_mtok=5.0,
            cache_creation_per_mtok=1.25,
            cache_read_per_mtok=0.10,
        ),
    },
    "openai": {
        "gpt-4o": ModelPricing(
            input_per_mtok=2.50,
            output_per_mtok=10.0,
            cache_read_per_mtok=1.25,
        ),
        "gpt-4o-mini": ModelPricing(
            input_per_mtok=0.15,
            output_per_mtok=0.60,
            cache_read_per_mtok=0.075,
        ),
        "gpt-4.1": ModelPricing(
            input_per_mtok=2.0,
            output_per_mtok=8.0,
            cache_read_per_mtok=0.50,
        ),
        "o1": ModelPricing(
            input_per_mtok=15.0,
            output_per_mtok=60.0,
            cache_read_per_mtok=7.50,
        ),
        "o3-mini": ModelPricing(
            input_per_mtok=1.10,
            output_per_mtok=4.40,
            cache_read_per_mtok=0.55,
        ),
    },
}


_VALID_GROUP_BY: frozenset[str] = frozenset(
    {
        "user_id",
        "user_name",
        "backend",
        "model",
        "profile",
        "conversation_id",
        "tool_name",
        "date",
        "invocation_source",
    }
)


class UsageService(Service):
    """Records and reports AI token usage.

    Implements:

    - ``UsageRecorder`` — ``record_round`` is called by ``AIService`` after
      every MESSAGE_COMPLETE event and writes an ``ai_token_usage`` entity.
    - ``UsagePricingProvider`` — ``compute_cost`` returns USD for a given
      ``(backend, model, TokenUsage)`` triple using the merged pricing
      table (defaults + user overrides).
    - ``UsageProvider`` — ``query_usage`` + ``list_models_with_usage`` for
      reporting UIs.
    """

    def __init__(self) -> None:
        self._storage: StorageBackend | None = None
        self._overrides: dict[str, dict[str, ModelPricing]] = {}
        # Rolling cache-savings telemetry. Every ``_rolling_log_every``
        # recorded rounds, we emit one INFO log summarizing the
        # window's cache hit ratio + dollars saved. Lives entirely in
        # memory — restarts reset the window, which is fine since the
        # persisted ``ai_token_usage`` records are the canonical
        # source. The rolling log is a "live signal in journalctl"
        # supplement, not the audit trail.
        self._rolling_log_every: int = 25
        self._rolling_window: list[dict[str, float]] = []

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="usage",
            capabilities=frozenset(
                {
                    "usage_reporting",
                    "usage_recording",
                    "ws_handlers",
                    # ``ai_cost_savings`` — exposed so the LLM can answer
                    # "how much have we saved on AI costs today?" mid-
                    # conversation without the operator having to open
                    # the dashboard. Same plumbing as feeds / mentra.
                    "ai_tools",
                }
            ),
            requires=frozenset({"entity_storage"}),
            optional=frozenset({"configuration"}),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        storage_svc = resolver.require_capability("entity_storage")
        from gilbert.interfaces.storage import StorageProvider

        if not isinstance(storage_svc, StorageProvider):
            raise RuntimeError(
                "UsageService requires a StorageProvider-capable storage service"
            )
        self._storage = storage_svc.backend

        await self._storage.ensure_index(
            IndexDefinition(collection=USAGE_COLLECTION, fields=["timestamp"])
        )
        await self._storage.ensure_index(
            IndexDefinition(collection=USAGE_COLLECTION, fields=["user_id"])
        )
        await self._storage.ensure_index(
            IndexDefinition(collection=USAGE_COLLECTION, fields=["conversation_id"])
        )

        config_svc = resolver.get_capability("configuration")
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                section = config_svc.get_section_safe(self.config_namespace)
                if section:
                    await self.on_config_changed(section)

        logger.info("Usage service started")

    # --- Configurable ------------------------------------------------

    @property
    def config_namespace(self) -> str:
        return "usage"

    @property
    def config_category(self) -> str:
        return "Intelligence"

    def config_params(self) -> list[ConfigParam]:
        params: list[ConfigParam] = [
            ConfigParam(
                key="rolling_log_every",
                type=ToolParameterType.INTEGER,
                description=(
                    "Emit one INFO log summarizing cache hit rate + "
                    "dollars saved every N recorded rounds. Set to 0 "
                    "to disable. Default 25 — gives ~5 minutes between "
                    "lines under typical load, frequent enough to see "
                    "caching take effect, sparse enough to not flood "
                    "journalctl."
                ),
                default=25,
            ),
        ]
        params.extend(self._pricing_config_params())
        return params

    def _pricing_config_params(self) -> list[ConfigParam]:
        """One numeric form field per (backend, model, rate-field).

        Fields are generated from ``_DEFAULT_PRICING`` so adding a model to
        that table automatically surfaces it in the settings UI with sane
        defaults. ``cache_creation_per_mtok`` and ``cache_read_per_mtok``
        only render for models whose default is non-zero — keeps the form
        uncluttered (OpenAI doesn't charge for cache writes, so that field
        would just be a noisy 0).

        Keys are dotted paths: ``pricing.<backend>.<sanitized-model>.<field>``.
        Model IDs are sanitized (``-`` and ``.`` → ``_``) because dots are the
        path separator and would split ``gpt-4.1`` into two levels.
        """
        params: list[ConfigParam] = []
        for backend, models in _DEFAULT_PRICING.items():
            for model, pricing in models.items():
                base = f"pricing.{backend}.{_sanitize_model_key(model)}"
                params.append(
                    ConfigParam(
                        key=f"{base}.input_per_mtok",
                        type=ToolParameterType.NUMBER,
                        description=(
                            f"{backend} / {model} — input tokens USD per million"
                        ),
                        default=pricing.input_per_mtok,
                    )
                )
                params.append(
                    ConfigParam(
                        key=f"{base}.output_per_mtok",
                        type=ToolParameterType.NUMBER,
                        description=(
                            f"{backend} / {model} — output tokens USD per million"
                        ),
                        default=pricing.output_per_mtok,
                    )
                )
                if pricing.cache_creation_per_mtok > 0:
                    params.append(
                        ConfigParam(
                            key=f"{base}.cache_creation_per_mtok",
                            type=ToolParameterType.NUMBER,
                            description=(
                                f"{backend} / {model} — cache write USD per million"
                            ),
                            default=pricing.cache_creation_per_mtok,
                        )
                    )
                if pricing.cache_read_per_mtok > 0:
                    params.append(
                        ConfigParam(
                            key=f"{base}.cache_read_per_mtok",
                            type=ToolParameterType.NUMBER,
                            description=(
                                f"{backend} / {model} — cache read USD per million"
                            ),
                            default=pricing.cache_read_per_mtok,
                        )
                    )
        return params

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        """Rebuild ``self._overrides`` from the pricing.* config subtree.

        The configuration service delivers dotted-path values as a nested
        dict: ``{pricing: {anthropic: {<sanitized>: {input_per_mtok: 15.0,
        ...}}}}``. We walk that tree, map sanitized model keys back to
        their real IDs via ``_DEFAULT_PRICING``, and build a fresh override
        table. Missing or unparseable values fall through to the defaults.
        """
        # Rolling-log knob — outside the pricing subtree so operators
        # can tune it without touching prices. 0 disables.
        try:
            self._rolling_log_every = max(0, int(config.get("rolling_log_every", 25)))
        except (TypeError, ValueError):
            self._rolling_log_every = 25

        pricing_cfg = config.get("pricing")
        if not isinstance(pricing_cfg, dict):
            self._overrides = {}
            return

        overrides: dict[str, dict[str, ModelPricing]] = {}
        for backend, models in pricing_cfg.items():
            if not isinstance(models, dict):
                continue
            reverse_lookup = {
                _sanitize_model_key(m): m
                for m in _DEFAULT_PRICING.get(backend, {})
            }
            per_backend: dict[str, ModelPricing] = {}
            for sanitized_model, fields in models.items():
                if not isinstance(fields, dict):
                    continue
                model = reverse_lookup.get(sanitized_model, sanitized_model)
                defaults = _DEFAULT_PRICING.get(backend, {}).get(
                    model,
                    ModelPricing(input_per_mtok=0.0, output_per_mtok=0.0),
                )
                per_backend[model] = ModelPricing(
                    input_per_mtok=_coerce_float(
                        fields.get("input_per_mtok"), defaults.input_per_mtok
                    ),
                    output_per_mtok=_coerce_float(
                        fields.get("output_per_mtok"), defaults.output_per_mtok
                    ),
                    cache_creation_per_mtok=_coerce_float(
                        fields.get("cache_creation_per_mtok"),
                        defaults.cache_creation_per_mtok,
                    ),
                    cache_read_per_mtok=_coerce_float(
                        fields.get("cache_read_per_mtok"),
                        defaults.cache_read_per_mtok,
                    ),
                )
            if per_backend:
                overrides[backend] = per_backend
        self._overrides = overrides
        logger.info(
            "Pricing overrides loaded for %d backend(s)",
            len(overrides),
        )

    # --- Pricing -----------------------------------------------------

    def _resolve_pricing(self, backend: str, model: str) -> ModelPricing | None:
        override = self._overrides.get(backend, {}).get(model)
        if override is not None:
            return override
        return _DEFAULT_PRICING.get(backend, {}).get(model)

    def compute_cost(
        self,
        *,
        backend: str,
        model: str,
        usage: TokenUsage,
    ) -> float:
        """USD cost for one round. Returns ``0.0`` if pricing is unknown."""
        pricing = self._resolve_pricing(backend, model)
        if pricing is None:
            return 0.0
        cost = (
            usage.input_tokens * pricing.input_per_mtok
            + usage.output_tokens * pricing.output_per_mtok
            + usage.cache_creation_tokens * pricing.cache_creation_per_mtok
            + usage.cache_read_tokens * pricing.cache_read_per_mtok
        ) / 1_000_000.0
        return round(cost, 6)

    def compute_naive_cost(
        self,
        *,
        backend: str,
        model: str,
        usage: TokenUsage,
    ) -> float:
        """Counterfactual cost — what this round would have cost with
        prompt caching OFF. Every cache_creation + cache_read token is
        treated as a regular uncached input token at ``input_per_mtok``.

        This is the baseline reporting subtracts ``compute_cost`` from
        to surface "saved by prompt caching" in dollars. Returns
        ``0.0`` when pricing is unknown — caller can treat a zero
        result as "no comparison available" rather than "we saved
        nothing", since the actual recorded cost would also be zero.
        """
        pricing = self._resolve_pricing(backend, model)
        if pricing is None:
            return 0.0
        # Treat the SUM of input + cache_creation + cache_read as if
        # every token had been billed at the uncached input rate.
        # That's exactly what we'd have paid before caching: those
        # cached tokens are still tokens we sent to Anthropic, just
        # billed at a discount.
        total_input_equivalent = (
            usage.input_tokens
            + usage.cache_creation_tokens
            + usage.cache_read_tokens
        )
        cost = (
            total_input_equivalent * pricing.input_per_mtok
            + usage.output_tokens * pricing.output_per_mtok
        ) / 1_000_000.0
        return round(cost, 6)

    # --- Recording ---------------------------------------------------

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
        cost = self.compute_cost(backend=backend, model=model, usage=usage)
        # Record both costs so historical "what did caching save us"
        # queries don't need to re-derive pricing — pricing may have
        # changed between record-time and report-time. Storing the
        # counterfactual at write-time makes the savings number stable
        # and immune to ConfigParam edits later.
        naive_cost = self.compute_naive_cost(
            backend=backend, model=model, usage=usage
        )
        record = UsageRecord(
            timestamp=datetime.now(UTC),
            user_id=user_ctx.user_id,
            user_name=user_ctx.display_name or user_ctx.user_id,
            conversation_id=conversation_id,
            profile=profile,
            backend=backend,
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cost_usd=cost,
            naive_cost_usd=naive_cost,
            tool_names=tuple(tool_names),
            stop_reason=stop_reason,
            round_num=round_num,
            invocation_source=invocation_source,
        )
        if self._storage is not None:
            try:
                await self._storage.put(
                    USAGE_COLLECTION,
                    str(uuid.uuid4()),
                    _record_to_dict(record),
                )
            except Exception as exc:
                # Never let recording break the AI loop.
                logger.warning(
                    "Failed to persist usage record (user=%s conv=%s): %s",
                    user_ctx.user_id,
                    conversation_id,
                    exc,
                )
        self._track_rolling(record)
        return record

    def _track_rolling(self, record: UsageRecord) -> None:
        """Add this round to the in-memory rolling window. Every Nth
        round emits one INFO line summarizing the window so journalctl
        gets a live "is caching working?" signal without anyone needing
        to query the DB. Cheap — single dict append + a counter check.
        """
        # Defensive: an out-of-band call (e.g. a fake recorder in a
        # test) might leave this unset. Default to "off".
        every = self._rolling_log_every
        if every <= 0:
            return
        self._rolling_window.append(
            {
                "input_tokens": float(record.input_tokens),
                "output_tokens": float(record.output_tokens),
                "cache_creation_tokens": float(record.cache_creation_tokens),
                "cache_read_tokens": float(record.cache_read_tokens),
                "cost_usd": float(record.cost_usd),
                "naive_cost_usd": float(record.naive_cost_usd),
            }
        )
        if len(self._rolling_window) < every:
            return

        window = self._rolling_window
        self._rolling_window = []

        total_cost = sum(r["cost_usd"] for r in window)
        total_naive = sum(r["naive_cost_usd"] for r in window)
        savings = max(0.0, total_naive - total_cost)
        total_input = sum(
            r["input_tokens"]
            + r["cache_creation_tokens"]
            + r["cache_read_tokens"]
            for r in window
        )
        if total_input <= 0:
            return  # all-output rounds — nothing meaningful to report
        read_pct = round(
            100 * sum(r["cache_read_tokens"] for r in window) / total_input
        )
        write_pct = round(
            100 * sum(r["cache_creation_tokens"] for r in window) / total_input
        )
        uncached_pct = 100 - read_pct - write_pct
        savings_pct = (
            round(100 * savings / total_naive) if total_naive > 0 else 0
        )

        logger.info(
            "AI cost rolling window (last %d rounds): paid $%.4f vs "
            "$%.4f naive — saved $%.4f (%d%%); cache %d%% read / "
            "%d%% write / %d%% uncached",
            every,
            total_cost,
            total_naive,
            savings,
            savings_pct,
            read_pct,
            write_pct,
            uncached_pct,
        )

    # --- Querying ----------------------------------------------------

    async def query_usage(self, spec: UsageQuery) -> list[UsageAggregate]:
        if self._storage is None:
            return []
        for field in spec.group_by:
            if field not in _VALID_GROUP_BY:
                raise ValueError(f"Invalid group_by field: {field}")

        filters: list[Filter] = []
        if spec.start is not None:
            filters.append(
                Filter(
                    field="timestamp",
                    op=FilterOp.GTE,
                    value=spec.start.isoformat(),
                )
            )
        if spec.end is not None:
            filters.append(
                Filter(
                    field="timestamp",
                    op=FilterOp.LT,
                    value=spec.end.isoformat(),
                )
            )
        if spec.user_id:
            filters.append(Filter(field="user_id", op=FilterOp.EQ, value=spec.user_id))
        if spec.conversation_id:
            filters.append(
                Filter(
                    field="conversation_id",
                    op=FilterOp.EQ,
                    value=spec.conversation_id,
                )
            )
        if spec.backend:
            filters.append(Filter(field="backend", op=FilterOp.EQ, value=spec.backend))
        if spec.model:
            filters.append(Filter(field="model", op=FilterOp.EQ, value=spec.model))
        if spec.profile:
            filters.append(Filter(field="profile", op=FilterOp.EQ, value=spec.profile))

        rows = await self._storage.query(
            Query(
                collection=USAGE_COLLECTION,
                filters=filters,
                sort=[SortField(field="timestamp", descending=True)],
            )
        )

        # Apply tool_name filter in Python — storage doesn't have a
        # contains-any for list fields.
        if spec.tool_name:
            rows = [
                r for r in rows
                if spec.tool_name in (r.get("tool_names") or [])
            ]

        if not spec.group_by:
            return [_row_to_aggregate(r) for r in rows]

        return _aggregate_rows(rows, spec.group_by)

    async def list_models_with_usage(self) -> list[dict[str, Any]]:
        if self._storage is None:
            return []
        rows = await self._storage.query(Query(collection=USAGE_COLLECTION))
        seen: set[tuple[str, str]] = set()
        for r in rows:
            backend = str(r.get("backend") or "")
            model = str(r.get("model") or "")
            if backend and model:
                seen.add((backend, model))
        return [{"backend": b, "model": m} for b, m in sorted(seen)]

    async def list_dimensions(self) -> dict[str, list[dict[str, Any]]]:
        """Return every distinct dimension value seen in the usage
        collection so reporting UIs can populate their filter dropdowns.

        One round-trip instead of N: lets the frontend render the whole
        filter strip from a single RPC.
        """
        if self._storage is None:
            return {
                "users": [],
                "backends": [],
                "models": [],
                "profiles": [],
                "tools": [],
                "invocation_sources": [],
            }
        rows = await self._storage.query(Query(collection=USAGE_COLLECTION))
        users: dict[str, str] = {}
        backends: set[str] = set()
        models: set[tuple[str, str]] = set()
        profiles: set[str] = set()
        tools: set[str] = set()
        sources: set[str] = set()
        for r in rows:
            uid = str(r.get("user_id") or "")
            if uid:
                users[uid] = str(r.get("user_name") or uid)
            b = str(r.get("backend") or "")
            m = str(r.get("model") or "")
            if b:
                backends.add(b)
            if b and m:
                models.add((b, m))
            p = str(r.get("profile") or "")
            if p:
                profiles.add(p)
            for t in r.get("tool_names") or []:
                if t:
                    tools.add(str(t))
            src = str(r.get("invocation_source") or "")
            if src:
                sources.add(src)
        return {
            "users": [
                {"user_id": uid, "user_name": name}
                for uid, name in sorted(users.items(), key=lambda p: p[1].lower())
            ],
            "backends": [{"backend": b} for b in sorted(backends)],
            "models": [{"backend": b, "model": m} for b, m in sorted(models)],
            "profiles": [{"profile": p} for p in sorted(profiles)],
            "tools": [{"tool_name": t} for t in sorted(tools)],
            "invocation_sources": [{"source": s} for s in sorted(sources)],
        }

    # --- Tool surface ───────────────────────────────────────────────

    @property
    def tool_provider_name(self) -> str:
        return "usage"

    def get_tools(self, user_ctx: Any = None) -> list[Any]:
        from gilbert.interfaces.tools import (
            ToolDefinition,
            ToolParameter,
            ToolParameterType,
        )

        return [
            ToolDefinition(
                name="ai_cost_savings",
                slash_group="usage",
                slash_command="savings",
                slash_help=(
                    "Show how much prompt-caching has saved on AI costs "
                    "for a time window (default: today)."
                ),
                description=(
                    "Report AI token-cost savings from prompt caching. "
                    "Returns: total rounds, what we paid, what we would "
                    "have paid without caching, dollars saved, and the "
                    "cache hit rate. USE THIS when the user asks 'how "
                    "much have we saved?', 'is caching working?', "
                    "'what's our AI bill look like today?', or any "
                    "variation. Voice-friendly summary by default. "
                    "Window defaults to today; pass 'since_hours' for "
                    "the last N hours, or 'all_time' for cumulative."
                ),
                parameters=[
                    ToolParameter(
                        name="since_hours",
                        type=ToolParameterType.INTEGER,
                        description=(
                            "Look back N hours from now. Omit for "
                            "'today so far' (midnight UTC → now)."
                        ),
                        required=False,
                    ),
                    ToolParameter(
                        name="all_time",
                        type=ToolParameterType.BOOLEAN,
                        description=(
                            "When true, ignore time window and report "
                            "savings across every recorded round."
                        ),
                        required=False,
                        default=False,
                    ),
                    ToolParameter(
                        name="by_model",
                        type=ToolParameterType.BOOLEAN,
                        description=(
                            "When true, break down savings per "
                            "(backend, model). Default is overall total."
                        ),
                        required=False,
                        default=False,
                    ),
                ],
                required_role="user",
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name != "ai_cost_savings":
            raise KeyError(f"usage has no tool {name!r}")
        return await self._tool_ai_cost_savings(arguments)

    async def _tool_ai_cost_savings(self, arguments: dict[str, Any]) -> str:
        """Build the time window, run a usage query, format a TTS-friendly
        summary. Returns one or two short paragraphs the LLM can read out
        without further synthesis."""
        all_time = bool(arguments.get("all_time", False))
        by_model = bool(arguments.get("by_model", False))
        since_hours_raw = arguments.get("since_hours")
        since_hours: int | None = None
        if since_hours_raw is not None:
            try:
                since_hours = max(1, int(since_hours_raw))
            except (TypeError, ValueError):
                since_hours = None

        now = datetime.now(UTC)
        if all_time:
            start: datetime | None = None
            window_label = "all-time"
        elif since_hours is not None:
            start = now.replace(microsecond=0) - _hours(since_hours)
            window_label = f"last {since_hours}h"
        else:
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            window_label = "today"

        group_by: tuple[str, ...] = (
            ("backend", "model") if by_model else ()
        )
        rows = await self.query_usage(
            UsageQuery(start=start, end=None, group_by=group_by)
        )

        if not rows:
            return (
                f"No AI usage recorded {window_label}. "
                "Either nothing has run yet or the usage service "
                "isn't persisting records."
            )

        if not by_model:
            agg = _sum_aggregates(rows)
            return _format_savings_paragraph(agg, window_label)

        # by_model: one short paragraph per model, sorted by actual cost.
        rows_sorted = sorted(rows, key=lambda a: a.cost_usd, reverse=True)
        chunks = [
            _format_savings_paragraph(
                a,
                window_label,
                model_label=(
                    f"{a.dimensions.get('backend', '?')}/"
                    f"{a.dimensions.get('model', '?')}"
                ),
            )
            for a in rows_sorted
        ]
        total = _sum_aggregates(rows)
        chunks.append(
            _format_savings_paragraph(total, window_label, model_label="total")
        )
        return "\n\n".join(chunks)

    # --- WS handlers -------------------------------------------------

    def get_ws_handlers(self) -> dict[str, Any]:
        return {
            "usage.query": self._ws_query,
            "usage.models": self._ws_models,
            "usage.dimensions": self._ws_dimensions,
        }

    async def _ws_query(
        self,
        conn: Any,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        payload = frame.get("payload", {}) or {}
        spec = _parse_query_payload(payload)
        results = await self.query_usage(spec)
        return {
            "type": "usage.query.result",
            "ref": frame.get("id"),
            "rows": [_aggregate_to_dict(a) for a in results],
        }

    async def _ws_models(
        self,
        conn: Any,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "type": "usage.models.result",
            "ref": frame.get("id"),
            "models": await self.list_models_with_usage(),
        }

    async def _ws_dimensions(
        self,
        conn: Any,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "type": "usage.dimensions.result",
            "ref": frame.get("id"),
            **(await self.list_dimensions()),
        }


# ── Helpers ─────────────────────────────────────────────────────────


def _sanitize_model_key(model: str) -> str:
    """Turn a model ID into a dot-safe config-key segment.

    Config keys use ``.`` as the path separator; model IDs like ``gpt-4.1``
    or ``claude-opus-4-20250514`` contain dots and hyphens that would either
    break path parsing or render as awkward humanized labels. Collapsing
    both to underscores gives a stable, human-readable key segment.
    """
    return model.replace("-", "_").replace(".", "_")


def _coerce_float(value: Any, fallback: float) -> float:
    """Coerce a config value to float, falling back on parse failure."""
    if value is None:
        return fallback
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _record_to_dict(record: UsageRecord) -> dict[str, Any]:
    return {
        "timestamp": record.timestamp.isoformat(),
        "date": record.timestamp.strftime("%Y-%m-%d"),
        "user_id": record.user_id,
        "user_name": record.user_name,
        "conversation_id": record.conversation_id,
        "profile": record.profile,
        "backend": record.backend,
        "model": record.model,
        "input_tokens": record.input_tokens,
        "output_tokens": record.output_tokens,
        "cache_creation_tokens": record.cache_creation_tokens,
        "cache_read_tokens": record.cache_read_tokens,
        "cost_usd": record.cost_usd,
        "naive_cost_usd": record.naive_cost_usd,
        "tool_names": list(record.tool_names),
        "stop_reason": record.stop_reason,
        "round_num": record.round_num,
        "invocation_source": record.invocation_source,
    }


def _hours(n: int) -> timedelta:
    """Tiny helper — ``timedelta(hours=...)`` reads worse inline."""
    return timedelta(hours=n)


def _sum_aggregates(aggregates: list[UsageAggregate]) -> UsageAggregate:
    """Collapse a list of UsageAggregate rows into a single grand total.

    Used by the savings tool when reporting "by model" — each
    per-model row already comes from ``query_usage``; this gives us
    the bottom-line total alongside the breakdown without an extra
    query.
    """
    total_cost = sum(a.cost_usd for a in aggregates)
    total_naive = sum(a.naive_cost_usd for a in aggregates)
    return UsageAggregate(
        dimensions={},
        rounds=sum(a.rounds for a in aggregates),
        input_tokens=sum(a.input_tokens for a in aggregates),
        output_tokens=sum(a.output_tokens for a in aggregates),
        cache_creation_tokens=sum(a.cache_creation_tokens for a in aggregates),
        cache_read_tokens=sum(a.cache_read_tokens for a in aggregates),
        cost_usd=total_cost,
        naive_cost_usd=total_naive,
        savings_usd=max(0.0, total_naive - total_cost),
    )


def _format_savings_paragraph(
    agg: UsageAggregate,
    window_label: str,
    *,
    model_label: str = "",
) -> str:
    """Render one UsageAggregate as a TTS-friendly summary.

    The shape is deliberately one paragraph (no bullets, no tables)
    so it survives the voice-brain → TTS pipeline cleanly. Numbers are
    rounded to dollars-and-cents at the high end and to half-cents
    when costs are low so we don't say "saved zero dollars" when the
    real answer is "saved 3 cents".
    """
    total_input = (
        agg.input_tokens + agg.cache_creation_tokens + agg.cache_read_tokens
    )
    if total_input == 0:
        # Edge case: only output tokens (an empty-prompt round?) — no
        # input to cache, nothing to report on. Shouldn't happen in
        # practice but we shouldn't divide by zero either.
        return (
            f"No input tokens recorded for {model_label or 'AI calls'} "
            f"{window_label}, so there's nothing to compare on caching."
        )
    cache_hit_pct = round(100 * agg.cache_read_tokens / total_input)
    cache_write_pct = round(100 * agg.cache_creation_tokens / total_input)
    uncached_pct = 100 - cache_hit_pct - cache_write_pct

    if agg.naive_cost_usd >= 1.0 or agg.cost_usd >= 1.0:
        actual_str = f"${agg.cost_usd:.2f}"
        naive_str = f"${agg.naive_cost_usd:.2f}"
        saved_str = f"${agg.savings_usd:.2f}"
    else:
        # Sub-dollar: show cents at higher precision.
        actual_str = f"${agg.cost_usd:.4f}"
        naive_str = f"${agg.naive_cost_usd:.4f}"
        saved_str = f"${agg.savings_usd:.4f}"

    if agg.naive_cost_usd > 0:
        savings_pct = round(100 * agg.savings_usd / agg.naive_cost_usd)
    else:
        savings_pct = 0

    label_prefix = f"{model_label.capitalize()}: " if model_label else ""

    if agg.cache_read_tokens == 0 and agg.cache_creation_tokens == 0:
        return (
            f"{label_prefix}For {window_label}, {agg.rounds} AI "
            f"round{'s' if agg.rounds != 1 else ''} cost {actual_str}. "
            f"Prompt caching hasn't fired yet — no cached tokens "
            f"recorded, so savings are $0.00 so far."
        )
    return (
        f"{label_prefix}For {window_label}, {agg.rounds} AI "
        f"round{'s' if agg.rounds != 1 else ''} cost {actual_str} with "
        f"prompt caching, versus {naive_str} we'd have paid without it "
        f"— saved {saved_str} ({savings_pct}%). Cache hit rate "
        f"{cache_hit_pct}% read, {cache_write_pct}% write, "
        f"{uncached_pct}% uncached."
    )


def _naive_cost_of_row(row: dict[str, Any]) -> float:
    """Read ``naive_cost_usd`` from a stored row, falling back to
    ``cost_usd`` when the field is absent — older entities written
    before this field existed had zero cache tokens, so ``cost_usd``
    equals what the counterfactual would have produced. Without this
    fallback, historical totals would understate baseline cost and
    the "saved by caching" delta would be artificially inflated for
    backfill windows that span the deploy.
    """
    raw = row.get("naive_cost_usd")
    if raw is None:
        return float(row.get("cost_usd", 0.0) or 0.0)
    return float(raw or 0.0)


def _row_to_aggregate(row: dict[str, Any]) -> UsageAggregate:
    """Wrap one raw entity row as an ungrouped ``UsageAggregate``."""
    cost = float(row.get("cost_usd", 0.0) or 0.0)
    naive_cost = _naive_cost_of_row(row)
    return UsageAggregate(
        dimensions={
            "timestamp": str(row.get("timestamp") or ""),
            "user_id": str(row.get("user_id") or ""),
            "user_name": str(row.get("user_name") or ""),
            "conversation_id": str(row.get("conversation_id") or ""),
            "profile": str(row.get("profile") or ""),
            "backend": str(row.get("backend") or ""),
            "model": str(row.get("model") or ""),
            "stop_reason": str(row.get("stop_reason") or ""),
            "invocation_source": str(row.get("invocation_source") or ""),
            "tool_names": ",".join(row.get("tool_names") or []),
        },
        rounds=1,
        input_tokens=int(row.get("input_tokens", 0) or 0),
        output_tokens=int(row.get("output_tokens", 0) or 0),
        cache_creation_tokens=int(row.get("cache_creation_tokens", 0) or 0),
        cache_read_tokens=int(row.get("cache_read_tokens", 0) or 0),
        cost_usd=cost,
        naive_cost_usd=naive_cost,
        savings_usd=max(0.0, naive_cost - cost),
    )


def _aggregate_rows(
    rows: list[dict[str, Any]],
    group_by: tuple[str, ...],
) -> list[UsageAggregate]:
    """Group rows by ``group_by`` and sum token + cost totals per group.

    When grouping by ``tool_name``, a row with N tool_names contributes N
    separate entries (one per tool), each carrying the full token/cost
    counts for that round. This matches the "did tool X get called during
    a round that cost $0.04" reading; splitting the cost fractionally would
    be misleading since tokens are billed per round, not per tool.
    """
    buckets: dict[tuple[str, ...], UsageAggregate] = {}
    for row in rows:
        tool_names = row.get("tool_names") or [""]
        if not tool_names:
            tool_names = [""]

        row_keys: list[str] = []
        dim_templates: list[dict[str, str]] = []
        for tn in tool_names:
            dim: dict[str, str] = {}
            key_parts: list[str] = []
            for field in group_by:
                if field == "tool_name":
                    value = str(tn)
                elif field == "date":
                    # ``date`` is denormalized at write time as YYYY-MM-DD.
                    value = str(row.get("date") or "")
                    if not value:
                        ts = str(row.get("timestamp") or "")
                        value = ts[:10]
                else:
                    value = str(row.get(field) or "")
                dim[field] = value
                key_parts.append(value)
            dim_templates.append(dim)
            row_keys.append("|".join(key_parts))

        row_cost = float(row.get("cost_usd", 0.0) or 0.0)
        row_naive = _naive_cost_of_row(row)
        for key, dim in zip(row_keys, dim_templates, strict=False):
            tup = tuple(key.split("|"))
            existing = buckets.get(tup)
            if existing is None:
                buckets[tup] = UsageAggregate(
                    dimensions=dim,
                    rounds=1,
                    input_tokens=int(row.get("input_tokens", 0) or 0),
                    output_tokens=int(row.get("output_tokens", 0) or 0),
                    cache_creation_tokens=int(row.get("cache_creation_tokens", 0) or 0),
                    cache_read_tokens=int(row.get("cache_read_tokens", 0) or 0),
                    cost_usd=row_cost,
                    naive_cost_usd=row_naive,
                    savings_usd=max(0.0, row_naive - row_cost),
                )
            else:
                new_cost = existing.cost_usd + row_cost
                new_naive = existing.naive_cost_usd + row_naive
                buckets[tup] = UsageAggregate(
                    dimensions=existing.dimensions,
                    rounds=existing.rounds + 1,
                    input_tokens=existing.input_tokens
                    + int(row.get("input_tokens", 0) or 0),
                    output_tokens=existing.output_tokens
                    + int(row.get("output_tokens", 0) or 0),
                    cache_creation_tokens=existing.cache_creation_tokens
                    + int(row.get("cache_creation_tokens", 0) or 0),
                    cache_read_tokens=existing.cache_read_tokens
                    + int(row.get("cache_read_tokens", 0) or 0),
                    cost_usd=new_cost,
                    naive_cost_usd=new_naive,
                    savings_usd=max(0.0, new_naive - new_cost),
                )

    return sorted(
        buckets.values(),
        key=lambda a: a.cost_usd,
        reverse=True,
    )


def _aggregate_to_dict(agg: UsageAggregate) -> dict[str, Any]:
    return {
        "dimensions": agg.dimensions,
        "rounds": agg.rounds,
        "input_tokens": agg.input_tokens,
        "output_tokens": agg.output_tokens,
        "cache_creation_tokens": agg.cache_creation_tokens,
        "cache_read_tokens": agg.cache_read_tokens,
        "cost_usd": round(agg.cost_usd, 6),
        "naive_cost_usd": round(agg.naive_cost_usd, 6),
        "savings_usd": round(agg.savings_usd, 6),
    }


def _parse_query_payload(payload: dict[str, Any]) -> UsageQuery:
    def _dt(val: Any) -> datetime | None:
        if not val or not isinstance(val, str):
            return None
        try:
            return datetime.fromisoformat(val)
        except ValueError:
            return None

    group_by_raw = payload.get("group_by") or []
    if not isinstance(group_by_raw, list):
        group_by_raw = []
    group_by = tuple(str(g) for g in group_by_raw if isinstance(g, str))

    return UsageQuery(
        start=_dt(payload.get("start")),
        end=_dt(payload.get("end")),
        user_id=payload.get("user_id") or None,
        conversation_id=payload.get("conversation_id") or None,
        backend=payload.get("backend") or None,
        model=payload.get("model") or None,
        profile=payload.get("profile") or None,
        tool_name=payload.get("tool_name") or None,
        group_by=group_by,
    )
