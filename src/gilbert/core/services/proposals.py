"""Proposals service — autonomously proposes self-improvements.

Gilbert observes events flowing through the bus, periodically reflects on
recent activity, and writes structured proposals into the entity store.
Admins triage them via the WS API. Each proposal carries a self-contained
``implementation_prompt`` so a fresh Claude session can implement it
without needing the original conversation context.

Design constraints:

- Observation is passive — events are summarized and dropped into an
  in-memory ring buffer. No AI cost per event.
- Reflection runs only on a schedule (default daily) or via a manual
  admin trigger. The AI is invoked at most once per cycle.
- A reflection cycle is skipped entirely when the observation buffer
  hasn't grown by ``min_observations_per_cycle`` events since the last
  cycle, and when the unreviewed proposal backlog is already past
  ``max_pending_proposals``. Both knobs protect installations with
  light use from spending tokens on no signal.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections import Counter, deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from gilbert.interfaces.ai import AISamplingProvider, Message, MessageRole
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
    ConfigurationReader,
)
from gilbert.interfaces.events import Event, EventBusProvider
from gilbert.interfaces.proposals import (
    PROPOSAL_KINDS,
    PROPOSAL_STATUSES,
    PROPOSALS_COLLECTION,
    STATUS_PROPOSED,
)
from gilbert.interfaces.scheduler import Schedule, SchedulerProvider
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import (
    Filter,
    FilterOp,
    IndexDefinition,
    Query,
    SortField,
    StorageBackend,
    StorageProvider,
)
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.ws import RpcHandler, require_admin

logger = logging.getLogger(__name__)


# Identifier for the scheduler job that runs the reflection cycle. The
# scheduler treats this as a system job (re-registered on each startup)
# so the user can't accidentally remove it from the scheduler page.
_REFLECTION_JOB_NAME = "proposals.reflection"

_DEFAULT_OBSERVATION_PATTERNS: tuple[str, ...] = (
    "ai.tool_call.*",
    "chat.user_message",
    "chat.assistant_response",
    "service.error",
    "service.start.failed",
    "scheduler.job.failed",
    "doorbell.*",
    "presence.*",
    "knowledge.search.miss",
    "inbox.message.received",
    "alarm.fired",
    "timer.fired",
)
"""Default event patterns the observer subscribes to.

Conservative on purpose — these are the signals most likely to reveal a
capability gap (failed tool calls, errors, things the AI couldn't
answer). Operators can broaden via the ``observation_event_patterns``
config param when they want richer reflection input. The wildcard ``*``
is supported by ``EventBus.subscribe_pattern``.
"""

_REFLECTION_SYSTEM_PROMPT = """You are Gilbert's self-improvement reflector.

Your role: review what has been happening in this Gilbert installation
recently and propose concrete, implementable changes that would make
Gilbert more useful — new plugins, new core services, configuration
changes, or removal of unused functionality.

CRITICAL RULES:

1. PROPOSE ONLY WHAT THE EVIDENCE SUPPORTS. If the observed activity is
   sparse, repetitive, or doesn't reveal a clear gap, return an empty
   proposals list. It is correct and expected to return zero proposals
   when there is nothing to propose. Do not invent needs that the
   evidence doesn't show.

2. ONE CONCEPT PER PROPOSAL. Don't bundle unrelated changes.

3. DON'T DUPLICATE EXISTING PROPOSALS. The list of recent proposals is
   provided — if a similar idea is already pending, skip it.

4. DON'T DUPLICATE EXISTING CAPABILITIES. The list of currently-active
   services and plugins is provided. If the gap can already be filled by
   something installed, don't propose adding it again.

5. SPECIFICATIONS MUST BE COMPLETE. Every proposal you return must
   include a fully-formed `spec` and an `implementation_prompt` that a
   future engineer (human or AI) could pick up and implement WITHOUT
   reading any of this conversation.

OUTPUT FORMAT: a single JSON object with one key, "proposals", whose
value is an array of zero or more proposal objects. No prose, no
markdown fences, no commentary outside the JSON. Each proposal object
must match the schema:

{
  "title": "Short imperative title (under 80 chars)",
  "summary": "1-2 sentence pitch",
  "kind": "new_plugin | modify_plugin | remove_plugin | new_service | remove_service | config_change",
  "target": "name of the plugin/service this affects, or empty string",
  "motivation": "WHY — the observed behavior that triggered this",
  "evidence": [
    {"event_type": "...", "summary": "...", "occurred_at": "ISO-8601", "count": 1}
  ],
  "spec": {
    "overview": "...",
    "architecture_notes": "Where this fits in the layered architecture (interfaces/ -> core/ -> integrations/storage/ -> web/).",
    "interfaces": [{"name": "...", "purpose": "...", "methods": [{"signature": "...", "description": "..."}]}],
    "data_model": [{"collection": "...", "fields": {...}, "indexes": [...]}],
    "config_params": [{"key": "...", "type": "...", "default": ..., "description": "..."}],
    "ws_handlers": [{"frame_type": "...", "params": {...}, "response": {...}, "acl_level": 0}],
    "ai_tools": [{"name": "...", "description": "...", "params": {...}}],
    "events_published": ["..."],
    "events_subscribed": ["..."],
    "dependencies": ["python_package_a", "..."],
    "external_services": ["e.g. Spotify Web API + scopes/auth model"],
    "files_to_create": [{"path": "...", "purpose": "..."}],
    "files_to_modify": [{"path": "...", "what_changes": "..."}],
    "tests": [{"layer": "unit | integration", "scenario": "..."}]
  },
  "implementation_prompt": "Self-contained prompt a fresh Claude session could paste in and implement from. Embed the full spec text here.",
  "impact": {
    "affected_components": ["..."],
    "breaking_changes": ["..."],
    "migration_steps": ["..."]
  },
  "risks": [{"category": "security | stability | privacy | cost", "description": "...", "mitigation": "..."}],
  "acceptance_criteria": ["concrete check 1", "..."],
  "open_questions": ["question for the operator to resolve before/while implementing"]
}

If there are no good proposals, return: {"proposals": []}
"""


def _slugify(value: str) -> str:
    """Lower-case, hyphen-joined slug for plugin/service ids in prompts."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned or "proposal"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class _Observation:
    """A summarized event in the reflection ring buffer."""

    __slots__ = ("event_type", "source", "summary", "occurred_at")

    def __init__(self, event_type: str, source: str, summary: str, occurred_at: str) -> None:
        self.event_type = event_type
        self.source = source
        self.summary = summary
        self.occurred_at = occurred_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "source": self.source,
            "summary": self.summary,
            "occurred_at": self.occurred_at,
        }


class ProposalsService(Service):
    """Autonomously proposes self-improvements based on observed activity.

    Capabilities: ``proposals``, ``ws_handlers``.
    """

    # Conservative defaults appropriate for low-signal installations —
    # 6-hour reflection, small caps, and a skip threshold so we don't
    # pay for token usage when nothing has happened. The system prompt
    # also tells the AI it MUST return an empty proposals list when the
    # evidence doesn't support anything new.
    _DEFAULT_REFLECTION_INTERVAL_SECONDS = 21_600  # 6h
    _DEFAULT_MAX_PROPOSALS_PER_CYCLE = 3
    _DEFAULT_OBSERVATION_BUFFER_SIZE = 500
    _DEFAULT_MIN_OBSERVATIONS_PER_CYCLE = 25
    _DEFAULT_MAX_PENDING_PROPOSALS = 10
    _DEFAULT_AI_PROFILE = "standard"
    _DEFAULT_ENABLED = True

    def __init__(self) -> None:
        # Configuration — populated in start() / on_config_changed().
        self._enabled: bool = self._DEFAULT_ENABLED
        self._reflection_interval_seconds: int = self._DEFAULT_REFLECTION_INTERVAL_SECONDS
        self._max_proposals_per_cycle: int = self._DEFAULT_MAX_PROPOSALS_PER_CYCLE
        self._observation_buffer_size: int = self._DEFAULT_OBSERVATION_BUFFER_SIZE
        self._min_observations_per_cycle: int = self._DEFAULT_MIN_OBSERVATIONS_PER_CYCLE
        self._max_pending_proposals: int = self._DEFAULT_MAX_PENDING_PROPOSALS
        self._ai_profile: str = self._DEFAULT_AI_PROFILE
        self._observation_patterns: tuple[str, ...] = _DEFAULT_OBSERVATION_PATTERNS

        # Runtime state.
        self._resolver: ServiceResolver | None = None
        self._storage: StorageBackend | None = None
        self._event_bus: Any = None
        self._observations: deque[_Observation] = deque(
            maxlen=self._DEFAULT_OBSERVATION_BUFFER_SIZE,
        )
        self._observations_seen_total: int = 0
        self._observations_seen_at_last_cycle: int = 0
        self._unsubscribers: list[Callable[[], None]] = []
        self._scheduler_job_registered: bool = False

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="proposals",
            capabilities=frozenset({"proposals", "ws_handlers"}),
            optional=frozenset(
                {"entity_storage", "event_bus", "scheduler", "ai_chat", "configuration"}
            ),
            events=frozenset({"proposal.created", "proposal.status_changed"}),
            toggleable=True,
            toggle_description="Autonomously proposes self-improvements (admin-only).",
        )

    # ── Lifecycle ────────────────────────────────────────────────────

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver

        # Apply persisted configuration first so the storage indexes,
        # observation patterns, and reflection cadence pick up the
        # operator's values rather than the defaults.
        config_svc = resolver.get_capability("configuration")
        if config_svc is not None and isinstance(config_svc, ConfigurationReader):
            section = config_svc.get_section_safe(self.config_namespace)
            if section:
                await self.on_config_changed(section)

        if not self._enabled:
            logger.info("Proposals service disabled by config")
            return

        # Wire up entity storage + indexes for the proposals collection.
        storage_svc = resolver.get_capability("entity_storage")
        if storage_svc is not None and isinstance(storage_svc, StorageProvider):
            self._storage = storage_svc.backend
            await self._ensure_indexes()
        else:
            logger.warning(
                "Proposals service has no entity storage — list/get will return nothing",
            )

        # Subscribe to events for the observation ring buffer.
        bus_svc = resolver.get_capability("event_bus")
        if bus_svc is not None and isinstance(bus_svc, EventBusProvider):
            self._event_bus = bus_svc.bus
            for pattern in self._observation_patterns:
                unsub = self._event_bus.subscribe_pattern(pattern, self._on_event)
                self._unsubscribers.append(unsub)
        else:
            logger.warning(
                "Proposals service has no event bus — reflection will run with empty observation buffer",
            )

        # Register the periodic reflection job.
        scheduler_svc = resolver.get_capability("scheduler")
        if scheduler_svc is not None and isinstance(scheduler_svc, SchedulerProvider):
            try:
                scheduler_svc.add_job(
                    name=_REFLECTION_JOB_NAME,
                    schedule=Schedule.every(self._reflection_interval_seconds),
                    callback=self._scheduled_reflection_callback,
                    system=True,
                )
                self._scheduler_job_registered = True
            except ValueError:
                # Already registered — happens after a hot-swap restart.
                self._scheduler_job_registered = True
        else:
            logger.warning(
                "Proposals service has no scheduler — reflection only runs via manual trigger",
            )

        logger.info(
            "Proposals service started (reflection every %ds, max %d/cycle)",
            self._reflection_interval_seconds,
            self._max_proposals_per_cycle,
        )

    async def stop(self) -> None:
        for unsub in self._unsubscribers:
            try:
                unsub()
            except Exception:
                logger.debug("Proposals: unsubscribe raised", exc_info=True)
        self._unsubscribers.clear()

    async def _ensure_indexes(self) -> None:
        """Declare indexes for the queries we run."""
        if self._storage is None:
            return
        for fields in (["status"], ["kind"], ["created_at"], ["reflection_cycle_id"]):
            try:
                await self._storage.ensure_index(
                    IndexDefinition(collection=PROPOSALS_COLLECTION, fields=fields),
                )
            except Exception:
                logger.debug(
                    "Proposals: ensure_index(%s) failed",
                    fields,
                    exc_info=True,
                )

    # ── Configurable protocol ────────────────────────────────────────

    @property
    def config_namespace(self) -> str:
        return "proposals"

    @property
    def config_category(self) -> str:
        return "Intelligence"

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="enabled",
                type=ToolParameterType.BOOLEAN,
                description="Run autonomous reflection on a schedule.",
                default=self._DEFAULT_ENABLED,
            ),
            ConfigParam(
                key="reflection_interval_seconds",
                type=ToolParameterType.INTEGER,
                description=(
                    "How often the reflection cycle runs. Default 21600 = "
                    "every 6 hours. Lower values increase responsiveness "
                    "but also increase token usage on quiet installations. "
                    "The AI is allowed (and instructed) to return zero "
                    "proposals when there is nothing to propose."
                ),
                default=self._DEFAULT_REFLECTION_INTERVAL_SECONDS,
                restart_required=True,
            ),
            ConfigParam(
                key="max_proposals_per_cycle",
                type=ToolParameterType.INTEGER,
                description="Maximum new proposals the AI is allowed to emit per cycle.",
                default=self._DEFAULT_MAX_PROPOSALS_PER_CYCLE,
            ),
            ConfigParam(
                key="observation_buffer_size",
                type=ToolParameterType.INTEGER,
                description=(
                    "Ring buffer size for observed events. Older events are evicted when full."
                ),
                default=self._DEFAULT_OBSERVATION_BUFFER_SIZE,
            ),
            ConfigParam(
                key="min_observations_per_cycle",
                type=ToolParameterType.INTEGER,
                description=(
                    "Skip the reflection AI call when fewer than this many new "
                    "events have been observed since the last cycle. Protects "
                    "low-signal installations from token spend on empty signal."
                ),
                default=self._DEFAULT_MIN_OBSERVATIONS_PER_CYCLE,
            ),
            ConfigParam(
                key="max_pending_proposals",
                type=ToolParameterType.INTEGER,
                description=(
                    "Skip the reflection AI call when this many proposals are "
                    "already in 'proposed' status awaiting admin triage. "
                    "Stops the backlog from growing while the operator is busy."
                ),
                default=self._DEFAULT_MAX_PENDING_PROPOSALS,
            ),
            ConfigParam(
                key="ai_profile",
                type=ToolParameterType.STRING,
                description="AI profile used for proposal generation.",
                default=self._DEFAULT_AI_PROFILE,
                choices_from="ai_profiles",
            ),
            ConfigParam(
                key="observation_event_patterns",
                type=ToolParameterType.ARRAY,
                description=(
                    "Event-bus glob patterns to observe. Defaults capture "
                    "the signals most likely to expose capability gaps."
                ),
                default=list(_DEFAULT_OBSERVATION_PATTERNS),
                restart_required=True,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._enabled = bool(config.get("enabled", self._DEFAULT_ENABLED))
        self._reflection_interval_seconds = int(
            config.get(
                "reflection_interval_seconds",
                self._DEFAULT_REFLECTION_INTERVAL_SECONDS,
            ),
        )
        self._max_proposals_per_cycle = max(
            0,
            int(config.get("max_proposals_per_cycle", self._DEFAULT_MAX_PROPOSALS_PER_CYCLE)),
        )
        new_buffer_size = max(
            10,
            int(config.get("observation_buffer_size", self._DEFAULT_OBSERVATION_BUFFER_SIZE)),
        )
        if new_buffer_size != self._observation_buffer_size:
            self._observation_buffer_size = new_buffer_size
            # Resize the deque, preserving the most recent observations.
            new_buffer: deque[_Observation] = deque(
                self._observations,
                maxlen=new_buffer_size,
            )
            self._observations = new_buffer
        self._min_observations_per_cycle = max(
            0,
            int(
                config.get(
                    "min_observations_per_cycle",
                    self._DEFAULT_MIN_OBSERVATIONS_PER_CYCLE,
                ),
            ),
        )
        self._max_pending_proposals = max(
            0,
            int(config.get("max_pending_proposals", self._DEFAULT_MAX_PENDING_PROPOSALS)),
        )
        self._ai_profile = str(config.get("ai_profile", self._DEFAULT_AI_PROFILE))
        patterns = config.get("observation_event_patterns")
        if isinstance(patterns, (list, tuple)) and all(isinstance(p, str) for p in patterns):
            self._observation_patterns = tuple(patterns)

    # ── ConfigActionProvider ─────────────────────────────────────────

    def config_actions(self) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="trigger_reflection",
                label="Run reflection now",
                description=(
                    "Manually run the reflection cycle. Bypasses the "
                    "min-observations gate but still respects the "
                    "max-pending-proposals cap."
                ),
            ),
        ]

    async def invoke_config_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        if key == "trigger_reflection":
            try:
                created = await self.trigger_reflection()
            except Exception as exc:
                logger.exception("Manual reflection trigger failed")
                return ConfigActionResult(status="error", message=str(exc))
            return ConfigActionResult(
                status="ok",
                message=(
                    f"Reflection complete — {created} new proposal"
                    f"{'s' if created != 1 else ''} created."
                ),
            )
        return ConfigActionResult(status="error", message=f"Unknown action: {key}")

    # ── Observation ──────────────────────────────────────────────────

    async def _on_event(self, event: Event) -> None:
        """Append an event to the observation ring buffer.

        Synchronous summarization only — we never call the AI from this
        path. The summary is a small string built from the event's data
        dict so the reflection prompt can describe what happened without
        carrying the full payload.
        """
        try:
            summary = self._summarize_event_data(event.data)
            self._observations.append(
                _Observation(
                    event_type=event.event_type,
                    source=event.source or "",
                    summary=summary,
                    occurred_at=event.timestamp.isoformat(),
                ),
            )
            self._observations_seen_total += 1
        except Exception:
            logger.debug("Proposals: failed to record observation", exc_info=True)

    @staticmethod
    def _summarize_event_data(data: dict[str, Any]) -> str:
        """Build a short, single-line description of an event payload.

        Picks the most informative scalar fields — preferring textual
        clues like ``message``, ``error``, ``tool``, ``user_id`` — and
        truncates to keep the reflection prompt compact.
        """
        if not data:
            return ""
        preferred = ("message", "error", "reason", "tool", "name", "user_id", "subject")
        parts: list[str] = []
        for key in preferred:
            if key in data and data[key] is not None:
                value = str(data[key])
                if len(value) > 80:
                    value = value[:77] + "..."
                parts.append(f"{key}={value}")
        if parts:
            return " ".join(parts)
        # Fall back to a few keys' worth of generic info.
        for key, value in list(data.items())[:3]:
            if value is None:
                continue
            text = str(value)
            if len(text) > 60:
                text = text[:57] + "..."
            parts.append(f"{key}={text}")
        return " ".join(parts)

    def observation_count(self) -> int:
        """Public accessor for tests + diagnostics."""
        return len(self._observations)

    # ── Reflection ───────────────────────────────────────────────────

    async def _scheduled_reflection_callback(self) -> None:
        """Scheduler entry point — never raises."""
        try:
            await self._run_reflection(manual=False)
        except Exception:
            logger.exception("Proposals reflection cycle raised")

    async def trigger_reflection(self) -> int:
        """Run a reflection cycle now. Returns the number of new proposals stored."""
        return await self._run_reflection(manual=True)

    async def _run_reflection(self, *, manual: bool) -> int:
        """Build context, ask the AI for proposals, persist whatever comes back.

        ``manual=True`` bypasses the min-observations gate (the operator
        explicitly asked) but the pending-cap and per-cycle ceiling still
        apply so a manual trigger can't pile up runaway cost either.
        """
        if not self._enabled:
            logger.info("Proposals: reflection skipped (service disabled)")
            return 0
        if self._max_proposals_per_cycle <= 0:
            logger.info("Proposals: reflection skipped (max_proposals_per_cycle=0)")
            return 0

        delta = self._observations_seen_total - self._observations_seen_at_last_cycle
        if not manual and delta < self._min_observations_per_cycle:
            logger.info(
                "Proposals: reflection skipped — only %d new observations (need %d)",
                delta,
                self._min_observations_per_cycle,
            )
            self._observations_seen_at_last_cycle = self._observations_seen_total
            return 0

        pending_count = await self._count_pending_proposals()
        if pending_count >= self._max_pending_proposals:
            logger.info(
                "Proposals: reflection skipped — %d pending proposals already (cap %d)",
                pending_count,
                self._max_pending_proposals,
            )
            self._observations_seen_at_last_cycle = self._observations_seen_total
            return 0

        ai_svc: Any = (
            self._resolver.get_capability("ai_chat") if self._resolver is not None else None
        )
        if not isinstance(ai_svc, AISamplingProvider):
            logger.warning("Proposals: reflection skipped — no AI service available")
            return 0

        # Build the user message: observations + current capability
        # snapshot + recent proposals (for dedup).
        user_prompt = await self._build_reflection_user_prompt()
        cycle_id = uuid.uuid4().hex
        try:
            response = await ai_svc.complete_one_shot(
                messages=[Message(role=MessageRole.USER, content=user_prompt)],
                system_prompt=_REFLECTION_SYSTEM_PROMPT,
                profile_name=self._ai_profile,
                tools_override=[],
            )
        except Exception:
            logger.exception("Proposals: AI call failed during reflection")
            return 0

        text = (response.message.content or "").strip()
        proposals = self._parse_proposals_response(text)
        if not proposals:
            logger.info("Proposals: AI returned no proposals (cycle=%s)", cycle_id)
            self._observations_seen_at_last_cycle = self._observations_seen_total
            return 0

        # Cap to per-cycle ceiling and pending budget. The smaller of
        # (per_cycle_max, pending_capacity_remaining) wins so a busy
        # backlog narrows the per-cycle output.
        capacity = max(0, self._max_pending_proposals - pending_count)
        cap = min(self._max_proposals_per_cycle, capacity)
        proposals = proposals[:cap]

        created = 0
        for raw in proposals:
            try:
                record = self._build_record(raw, cycle_id=cycle_id)
            except ValueError as exc:
                logger.warning(
                    "Proposals: discarding malformed AI proposal: %s",
                    exc,
                )
                continue
            await self._persist_proposal(record)
            created += 1

        self._observations_seen_at_last_cycle = self._observations_seen_total
        logger.info(
            "Proposals: reflection cycle %s created %d proposal(s)",
            cycle_id,
            created,
        )
        return created

    async def _build_reflection_user_prompt(self) -> str:
        """Compose the user-side reflection prompt.

        Three sections: observed events (deduplicated by event_type,
        with counts), currently-active capabilities (so the AI doesn't
        re-propose existing ones), and recent proposals (so the AI
        doesn't re-propose pending ideas).
        """
        # Observed events — group by event_type so the same event firing
        # 200 times reads as "200 occurrences" rather than 200 lines.
        type_counts: Counter[str] = Counter()
        latest_per_type: dict[str, _Observation] = {}
        for obs in self._observations:
            type_counts[obs.event_type] += 1
            latest_per_type[obs.event_type] = obs
        observed_lines: list[str] = []
        for event_type, count in type_counts.most_common(40):
            sample = latest_per_type[event_type]
            sample_summary = sample.summary or "(no summary)"
            observed_lines.append(
                f"- {event_type} ({count}×, last {sample.occurred_at}): {sample_summary}"
            )
        observed_block = "\n".join(observed_lines) or "(no observations yet)"

        # Active capabilities snapshot — concrete service + plugin names
        # from the running service manager + plugin loader.
        capabilities_block = self._build_capabilities_snapshot()

        # Recent proposals (last 50) — title + status so the AI can
        # avoid duplicating in-flight ideas.
        recent_proposals_block = await self._build_recent_proposals_snapshot()

        return (
            "Reflect on the activity below and propose any improvements.\n\n"
            "## Observed events (last buffer)\n"
            f"{observed_block}\n\n"
            "## Currently active capabilities\n"
            f"{capabilities_block}\n\n"
            "## Recent proposals (do not duplicate)\n"
            f"{recent_proposals_block}\n\n"
            f"Return at most {self._max_proposals_per_cycle} proposal(s) "
            f"as JSON. If nothing is worth proposing, return "
            '{"proposals": []}.\n'
        )

    def _build_capabilities_snapshot(self) -> str:
        """Render the running service-manager state as a flat list of names.

        The ``ServiceManager`` (which is also the ``ServiceResolver``
        passed to ``start()``) implements the ``ServiceEnumerator``
        protocol, so we runtime-check for it. If a different resolver
        is wired in (e.g. tests), we degrade gracefully.
        """
        from gilbert.interfaces.service import ServiceEnumerator

        if self._resolver is None or not isinstance(self._resolver, ServiceEnumerator):
            return "(service inventory unavailable)"
        try:
            all_services = self._resolver.list_services()
            started = set(self._resolver.started_services)
            active = sorted(name for name in all_services if name in started)
            inactive = sorted(name for name in all_services if name not in started)
        except Exception:
            logger.debug("Proposals: failed to snapshot capabilities", exc_info=True)
            return "(service inventory unavailable)"
        lines = ["Active services: " + (", ".join(active) or "(none)")]
        if inactive:
            lines.append("Disabled / not-started: " + ", ".join(inactive))
        return "\n".join(lines)

    async def _build_recent_proposals_snapshot(self) -> str:
        if self._storage is None:
            return "(none)"
        try:
            recent = await self._storage.query(
                Query(
                    collection=PROPOSALS_COLLECTION,
                    sort=[SortField(field="created_at", descending=True)],
                    limit=50,
                ),
            )
        except Exception:
            logger.debug("Proposals: recent-proposals query failed", exc_info=True)
            return "(unavailable)"
        if not recent:
            return "(none)"
        lines = []
        for p in recent:
            title = str(p.get("title", "(untitled)"))[:80]
            status = p.get("status", "?")
            kind = p.get("kind", "?")
            lines.append(f"- [{status}/{kind}] {title}")
        return "\n".join(lines)

    @staticmethod
    def _parse_proposals_response(text: str) -> list[dict[str, Any]]:
        """Extract the proposals array from the model's JSON response.

        Tolerant of stray markdown fences and leading/trailing prose —
        we look for the first ``{`` and last ``}`` and parse the slice.
        """
        if not text:
            return []
        # Strip a single fenced code block if present (```json\n...\n```).
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
            if stripped.endswith("```"):
                stripped = stripped[: -len("```")]
            stripped = stripped.strip()
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            # Last-resort: slice between the outermost braces.
            start = stripped.find("{")
            end = stripped.rfind("}")
            if start == -1 or end == -1 or end <= start:
                logger.warning("Proposals: AI response was not JSON; discarding")
                return []
            try:
                payload = json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                logger.warning("Proposals: AI response JSON parse failed; discarding")
                return []
        if not isinstance(payload, dict):
            return []
        proposals = payload.get("proposals")
        if not isinstance(proposals, list):
            return []
        return [p for p in proposals if isinstance(p, dict)]

    def _build_record(self, raw: dict[str, Any], *, cycle_id: str) -> dict[str, Any]:
        """Validate and normalize an AI-emitted proposal into a stored record.

        Raises ``ValueError`` for proposals that are missing the minimum
        viable shape (title + spec + implementation_prompt) — the caller
        discards those rather than persisting garbage.
        """
        title = str(raw.get("title") or "").strip()
        if not title:
            raise ValueError("missing title")
        spec = raw.get("spec") or {}
        if not isinstance(spec, dict) or not spec:
            raise ValueError("missing or empty spec")
        impl_prompt = str(raw.get("implementation_prompt") or "").strip()
        if not impl_prompt:
            raise ValueError("missing implementation_prompt")

        kind = str(raw.get("kind") or "").strip()
        if kind not in PROPOSAL_KINDS:
            kind = "new_plugin"  # safe default — no destructive action implied

        proposal_id = (
            f"{int(datetime.now(UTC).timestamp())}-{_slugify(title)[:40]}-{uuid.uuid4().hex[:6]}"
        )
        now_iso = _now_iso()

        return {
            "_id": proposal_id,
            "id": proposal_id,
            "title": title[:200],
            "summary": str(raw.get("summary") or "").strip()[:1000],
            "kind": kind,
            "target": str(raw.get("target") or "").strip()[:120],
            "status": STATUS_PROPOSED,
            "motivation": str(raw.get("motivation") or "").strip(),
            "evidence": list(raw.get("evidence") or []),
            "spec": spec,
            "implementation_prompt": impl_prompt,
            "impact": dict(raw.get("impact") or {}),
            "risks": list(raw.get("risks") or []),
            "acceptance_criteria": list(raw.get("acceptance_criteria") or []),
            "open_questions": list(raw.get("open_questions") or []),
            "admin_notes": [],
            "ai_profile_used": self._ai_profile,
            "reflection_cycle_id": cycle_id,
            "created_at": now_iso,
            "updated_at": now_iso,
        }

    async def _persist_proposal(self, record: dict[str, Any]) -> None:
        if self._storage is None:
            return
        try:
            await self._storage.put(PROPOSALS_COLLECTION, record["_id"], record)
        except Exception:
            logger.exception("Proposals: failed to persist proposal %s", record.get("_id"))
            return
        if self._event_bus is not None:
            try:
                await self._event_bus.publish(
                    Event(
                        event_type="proposal.created",
                        data={
                            "proposal_id": record["_id"],
                            "title": record["title"],
                            "kind": record["kind"],
                        },
                        source="proposals",
                    ),
                )
            except Exception:
                logger.debug("Proposals: publish proposal.created failed", exc_info=True)

    # ── Read paths (ProposalsProvider) ───────────────────────────────

    async def list_proposals(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if self._storage is None:
            return []
        filters: list[Filter] = []
        if status:
            filters.append(Filter(field="status", op=FilterOp.EQ, value=status))
        if kind:
            filters.append(Filter(field="kind", op=FilterOp.EQ, value=kind))
        try:
            return await self._storage.query(
                Query(
                    collection=PROPOSALS_COLLECTION,
                    filters=filters,
                    sort=[SortField(field="created_at", descending=True)],
                    limit=limit,
                ),
            )
        except Exception:
            logger.exception("Proposals: list query failed")
            return []

    async def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        if self._storage is None:
            return None
        try:
            return await self._storage.get(PROPOSALS_COLLECTION, proposal_id)
        except Exception:
            logger.exception("Proposals: get(%s) failed", proposal_id)
            return None

    async def _count_pending_proposals(self) -> int:
        if self._storage is None:
            return 0
        try:
            return await self._storage.count(
                Query(
                    collection=PROPOSALS_COLLECTION,
                    filters=[
                        Filter(field="status", op=FilterOp.EQ, value=STATUS_PROPOSED),
                    ],
                ),
            )
        except Exception:
            logger.debug("Proposals: pending count failed", exc_info=True)
            return 0

    # ── Write paths (admin actions) ──────────────────────────────────

    async def update_status(
        self,
        proposal_id: str,
        status: str,
        actor_user_id: str,
    ) -> dict[str, Any] | None:
        if status not in PROPOSAL_STATUSES:
            raise ValueError(f"Invalid status: {status}")
        if self._storage is None:
            return None
        record = await self.get_proposal(proposal_id)
        if record is None:
            return None
        previous = record.get("status")
        record["status"] = status
        record["updated_at"] = _now_iso()
        await self._storage.put(PROPOSALS_COLLECTION, proposal_id, record)
        if self._event_bus is not None:
            try:
                await self._event_bus.publish(
                    Event(
                        event_type="proposal.status_changed",
                        data={
                            "proposal_id": proposal_id,
                            "from": previous,
                            "to": status,
                            "actor": actor_user_id,
                        },
                        source="proposals",
                    ),
                )
            except Exception:
                logger.debug(
                    "Proposals: publish proposal.status_changed failed",
                    exc_info=True,
                )
        return record

    async def add_note(
        self,
        proposal_id: str,
        note: str,
        author_user_id: str,
    ) -> dict[str, Any] | None:
        text = note.strip()
        if not text:
            raise ValueError("Note cannot be empty")
        if self._storage is None:
            return None
        record = await self.get_proposal(proposal_id)
        if record is None:
            return None
        notes = list(record.get("admin_notes") or [])
        notes.append(
            {
                "author_id": author_user_id,
                "note": text,
                "added_at": _now_iso(),
            },
        )
        record["admin_notes"] = notes
        record["updated_at"] = _now_iso()
        await self._storage.put(PROPOSALS_COLLECTION, proposal_id, record)
        return record

    async def delete_proposal(self, proposal_id: str) -> bool:
        if self._storage is None:
            return False
        if not await self._storage.exists(PROPOSALS_COLLECTION, proposal_id):
            return False
        await self._storage.delete(PROPOSALS_COLLECTION, proposal_id)
        return True

    # ── WS RPC handlers (admin-only via ACL defaults) ────────────────

    def get_ws_handlers(self) -> dict[str, RpcHandler]:
        return {
            "proposals.list": self._ws_list,
            "proposals.get": self._ws_get,
            "proposals.update_status": self._ws_update_status,
            "proposals.add_note": self._ws_add_note,
            "proposals.delete": self._ws_delete,
            "proposals.trigger_reflection": self._ws_trigger_reflection,
        }

    @staticmethod
    def _err(frame: dict[str, Any], message: str, code: int = 400) -> dict[str, Any]:
        return {
            "type": "gilbert.error",
            "ref": frame.get("id"),
            "error": message,
            "code": code,
        }

    async def _ws_list(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        if (err := require_admin(conn, frame)) is not None:
            return err
        status = frame.get("status") or None
        kind = frame.get("kind") or None
        try:
            limit = max(1, min(500, int(frame.get("limit", 100))))
        except (TypeError, ValueError):
            limit = 100
        proposals = await self.list_proposals(status=status, kind=kind, limit=limit)
        return {
            "type": "proposals.list.result",
            "ref": frame.get("id"),
            "proposals": proposals,
            "available_statuses": list(PROPOSAL_STATUSES),
            "available_kinds": list(PROPOSAL_KINDS),
        }

    async def _ws_get(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        if (err := require_admin(conn, frame)) is not None:
            return err
        proposal_id = str(frame.get("proposal_id") or "").strip()
        if not proposal_id:
            return self._err(frame, "Missing 'proposal_id'")
        record = await self.get_proposal(proposal_id)
        if record is None:
            return self._err(frame, f"Proposal not found: {proposal_id}", 404)
        return {
            "type": "proposals.get.result",
            "ref": frame.get("id"),
            "proposal": record,
        }

    async def _ws_update_status(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        if (err := require_admin(conn, frame)) is not None:
            return err
        proposal_id = str(frame.get("proposal_id") or "").strip()
        new_status = str(frame.get("status") or "").strip()
        if not proposal_id:
            return self._err(frame, "Missing 'proposal_id'")
        if new_status not in PROPOSAL_STATUSES:
            return self._err(
                frame,
                f"Invalid status (must be one of {list(PROPOSAL_STATUSES)})",
            )
        actor = getattr(getattr(conn, "user_ctx", None), "user_id", "")
        try:
            record = await self.update_status(proposal_id, new_status, actor)
        except ValueError as exc:
            return self._err(frame, str(exc))
        if record is None:
            return self._err(frame, f"Proposal not found: {proposal_id}", 404)
        return {
            "type": "proposals.update_status.result",
            "ref": frame.get("id"),
            "proposal": record,
        }

    async def _ws_add_note(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        if (err := require_admin(conn, frame)) is not None:
            return err
        proposal_id = str(frame.get("proposal_id") or "").strip()
        note = str(frame.get("note") or "").strip()
        if not proposal_id:
            return self._err(frame, "Missing 'proposal_id'")
        if not note:
            return self._err(frame, "Missing 'note'")
        author = getattr(getattr(conn, "user_ctx", None), "user_id", "")
        try:
            record = await self.add_note(proposal_id, note, author)
        except ValueError as exc:
            return self._err(frame, str(exc))
        if record is None:
            return self._err(frame, f"Proposal not found: {proposal_id}", 404)
        return {
            "type": "proposals.add_note.result",
            "ref": frame.get("id"),
            "proposal": record,
        }

    async def _ws_delete(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        if (err := require_admin(conn, frame)) is not None:
            return err
        proposal_id = str(frame.get("proposal_id") or "").strip()
        if not proposal_id:
            return self._err(frame, "Missing 'proposal_id'")
        deleted = await self.delete_proposal(proposal_id)
        if not deleted:
            return self._err(frame, f"Proposal not found: {proposal_id}", 404)
        return {
            "type": "proposals.delete.result",
            "ref": frame.get("id"),
            "proposal_id": proposal_id,
            "status": "deleted",
        }

    async def _ws_trigger_reflection(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        if (err := require_admin(conn, frame)) is not None:
            return err
        try:
            created = await self.trigger_reflection()
        except Exception as exc:
            logger.exception("Manual reflection via WS failed")
            return self._err(frame, str(exc), 500)
        return {
            "type": "proposals.trigger_reflection.result",
            "ref": frame.get("id"),
            "created": created,
        }
