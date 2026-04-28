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

import asyncio
import json
import logging
import re
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from gilbert.interfaces.ai import AISamplingProvider, Message, MessageRole, StopReason
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
    ConfigurationReader,
)
from gilbert.interfaces.events import Event, EventBusProvider
from gilbert.interfaces.proposals import (
    KIND_MODIFY_CORE,
    KIND_NEW_PLUGIN,
    OBSERVATION_SOURCES,
    OBSERVATIONS_COLLECTION,
    PROPOSAL_KINDS,
    PROPOSAL_STATUSES,
    PROPOSALS_COLLECTION,
    SOURCE_AI_TOOL,
    SOURCE_CONVERSATION_ABANDONED,
    SOURCE_CONVERSATION_ACTIVE,
    SOURCE_CONVERSATION_DELETED,
    SOURCE_EVENT,
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
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
    ToolResult,
)
from gilbert.interfaces.ws import RpcHandler, require_admin

logger = logging.getLogger(__name__)


# Scheduler job names. All registered as system jobs so they appear on
# the scheduler page but can't be removed by users.
_REFLECTION_JOB_NAME = "proposals.reflection"
_HARVEST_JOB_NAME = "proposals.conversation_harvest"
_FLUSH_JOB_NAME = "proposals.observation_flush"

# Conversations live in this collection (owned by AIService). We read
# from it during the harvest job to extract observations from active
# and abandoned conversations.
_AI_CONVERSATIONS_COLLECTION = "ai_conversations"

# Pre-delete event AIService publishes right before destroying a
# conversation. Carries the conversation snapshot so subscribers can
# act before the data is gone (we use it for last-chance observation
# extraction).
_CHAT_ARCHIVING_EVENT = "chat.conversation.archiving"

# Subscribing to "*" gives us the broadest possible signal, but a
# handful of event types fire on hot paths (per-token streaming) and
# would dominate the buffer with noise. We drop them at the handler
# rather than narrowing the subscription pattern, so an operator who
# does want them can simply remove the entry here.
_NOISY_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "chat.stream.text_delta",
        "chat.stream.round_complete",
    },
)

_DEFAULT_OBSERVATION_PATTERNS: tuple[str, ...] = ("*",)
"""Default event patterns the observer subscribes to.

Default is the ``*`` wildcard — every event flowing through the bus is
summarized into the ring buffer so the reflector has the broadest
possible signal to work with. Observation is synchronous and cheap
(no AI cost per event), and the ring buffer is bounded by
``observation_buffer_size``, so a chatty bus naturally evicts old
events without unbounded memory growth. Operators can narrow via the
``observation_event_patterns`` config param if they want to focus the
reflector on specific signals.
"""

_REFLECTION_SYSTEM_PROMPT = """You are Gilbert's self-improvement reflector.

Your role: review what has been happening in this Gilbert installation
recently and propose concrete, implementable changes that would make
Gilbert more useful. Look at the activity through TWO lenses:

A) GAPS — refusals, errors, things users asked for that Gilbert
   couldn't do. Propose new capabilities to fill these.

B) PATTERNS — things users are repeatedly DOING SUCCESSFULLY (across
   multiple conversations or many recurring events). When you see a
   pattern, ask: "could this be made faster, automated, or wrapped in
   a higher-level workflow?" Even when nothing is broken, recurring
   manual workflows are an opportunity for a tool, slash command,
   scheduled job, or template that saves the user steps. Workflow
   shortcuts are valid proposals.

Proposals can be: new plugins, new core services, modifications to
existing ones, configuration changes, or removal of unused functionality.

You have read-only access to Gilbert's own source tree via three
tools — ``gilbert_list_files``, ``gilbert_read_file``, ``gilbert_grep``.
USE THEM. Before proposing a change, look at what's actually there:
verify the plugin you want to extend exists, check whether the gap is
already partially solved, find the file paths and class names you'll
cite in the implementation_prompt. Proposals grounded in real source
read are far more useful than guesses, and the implementer will paste
your prompt into a fresh session that does NOT have this codebase
context — give them concrete file paths and symbol names you've
verified.

ARCHITECTURAL PREFERENCE — STRONGLY PREFER ADDITIVE CHANGES:

- First choice: a NEW plugin under ``std-plugins/`` or
  ``local-plugins/``. New capabilities almost always belong here —
  plugins compose, can be uninstalled, and don't risk regressing core
  behavior. Plugin scaffolding is well-trodden.
- Second choice: modifying a plugin you can clearly see was added by
  this same reflection system in a previous cycle (look for it in the
  capabilities snapshot or the recent-proposals list).
- Third choice: a small ``config_change`` to an existing service.
- Last resort: ``modify_core`` (changes to ``src/gilbert/`` itself).
  Use this kind ONLY when the evidence proves the change cannot live
  in a plugin — for example, the change has to touch a core ABC, a
  layer-dependency rule, the bootstrap path, or shared interfaces in
  ``src/gilbert/interfaces/``. Most refusals and feature gaps do NOT
  meet this bar.

When ``allow_core_modifications`` is FALSE (the snapshot tells you the
current value) you MUST NOT emit any proposal with kind
``modify_core``. Reframe the idea as a plugin or skip it entirely.

CRITICAL RULES:

1. PROPOSE ONLY WHAT THE EVIDENCE SUPPORTS. If the observed activity is
   sparse, repetitive in a non-actionable way, or doesn't reveal a clear
   gap or pattern, return an empty proposals list. It is correct and
   expected to return zero proposals when there is nothing to propose.
   Do not invent needs that the evidence doesn't show.

2. ONE CONCEPT PER PROPOSAL. Don't bundle unrelated changes.

3. DON'T DUPLICATE EXISTING PROPOSALS. The list of recent proposals is
   provided — if a similar idea is already pending, skip it.

4. DON'T DUPLICATE EXISTING CAPABILITIES. The list of currently-active
   services and plugins is provided. If the gap can already be filled by
   something installed, don't propose adding it again.

5. THE `implementation_prompt` MUST BE COMPREHENSIVE. This field is the
   most important thing you produce — an engineer (human or fresh AI
   session) will paste it into a new Claude Code session with no other
   context and expect to implement the entire proposal from it. It is
   NOT a one-paragraph summary. It is a full, multi-section briefing.
   Aim for 800–2000 words. It MUST include, in order:

     a. A "Project context" header re-stating that this is the Gilbert
        codebase (Python 3.12+, uv, layered architecture, plugin system),
        and that the implementer should read /CLAUDE.md and
        .claude/memory/MEMORIES.md before starting.
     b. The motivation paragraph — why this is being built, with the
        evidence summarized.
     c. An architecture section naming where the new code lives (which
        layer, plugin vs core, file paths to create, file paths to
        modify).
     d. Interfaces (ABCs / capability protocols) to define, with method
        signatures and one-sentence purposes.
     e. Data model (collection name, fields, indexes).
     f. Configuration parameters (key, type, default, description).
     g. WS RPC handlers (frame type, params, response shape, ACL level).
     h. AI tools to expose (name, parameters, required_role, description).
     i. Events published / subscribed.
     j. Python dependencies to add via `uv add`.
     k. External services / APIs touched (with auth model + scope).
     l. Tests to write, by layer.
     m. Risks + mitigations.
     n. Acceptance criteria as a checklist.
     o. An "Implementation checklist" section listing the steps to
        follow (define ABCs first, follow layer rules, run uv run
        pytest / mypy / ruff before declaring done, update memory
        files, do not commit).

   If you cannot fill in a section confidently from the evidence,
   write "Open question:" plus the specific decision the operator
   needs to make — don't fabricate. The implementation_prompt should
   read like a self-contained PR brief, not a marketing summary.

OUTPUT FORMAT: a single JSON object with one key, "proposals", whose
value is an array of zero or more proposal objects. No prose, no
markdown fences, no commentary outside the JSON. Each proposal object
must match the schema:

{
  "title": "Short imperative title (under 80 chars)",
  "summary": "1-2 sentence pitch",
  "kind": "new_plugin | modify_plugin | remove_plugin | new_service | remove_service | config_change | modify_core",
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


_CONVERSATION_EXTRACTION_SYSTEM_PROMPT = """You are extracting reflection-worthy observations from a Gilbert conversation transcript.

Your output is fed into Gilbert's self-improvement reflector — it does NOT
become a proposal directly. Pull out only signals that could later inform
a proposal: capability gaps, recurring frustrations, requests Gilbert
couldn't fulfill, knowledge gaps, ideas the user articulated, or
patterns of usage that aren't yet served well.

CRITICAL RULES:

1. Return zero observations if the transcript doesn't reveal anything
   worth reflecting on. Routine successful interactions should produce
   no observations.

2. Each observation must be a single short sentence — NOT a summary of
   the whole conversation. One signal per entry.

3. Don't restate what Gilbert already did successfully. Only flag
   things that suggest Gilbert could be MORE useful.

OUTPUT FORMAT: a JSON object with one key "observations":

{
  "observations": [
    {
      "summary": "Single short sentence capturing one signal.",
      "category": "capability_gap | recurring_frustration | knowledge_gap | feature_request | usage_pattern | other"
    }
  ]
}

If nothing is worth recording, return: {"observations": []}
"""


def _slugify(value: str) -> str:
    """Lower-case, hyphen-joined slug for plugin/service ids in prompts."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned or "proposal"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _action_status_result(status: str, *, kind: str) -> ConfigActionResult:
    """Map a background-job kickoff status to a ConfigActionResult toast."""
    if status == "started":
        return ConfigActionResult(
            status="ok",
            message=(
                f"{kind} started — running in the background. New results "
                "will appear here as they're produced."
            ),
        )
    if status == "already_running":
        return ConfigActionResult(
            status="error",
            message=f"{kind} is already running. Please wait for it to finish.",
        )
    if status == "disabled":
        return ConfigActionResult(
            status="error",
            message=(
                f"{kind} could not start — the proposals service is disabled."
            ),
        )
    return ConfigActionResult(status="error", message=f"Unknown status: {status}")


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
    _DEFAULT_MIN_OBSERVATIONS_PER_CYCLE = 25
    _DEFAULT_MAX_PENDING_PROPOSALS = 10
    _DEFAULT_AI_PROFILE = "advanced"
    _DEFAULT_ENABLED = True
    # Off by default: changes inside ``src/gilbert/`` carry more risk
    # than a new plugin and must be opted into by the operator. When
    # this is False, the reflection AI is told it must not emit
    # ``modify_core`` proposals; if one slips through anyway, we
    # downgrade it to ``new_plugin`` so the idea isn't lost but the
    # implementer is steered toward the additive approach.
    _DEFAULT_ALLOW_CORE_MODIFICATIONS = False
    # Reflection AI may need a few rounds to read source before
    # producing the proposals JSON. Cap the loop so a misbehaving
    # model can't fan out unbounded reads.
    _DEFAULT_REFLECTION_MAX_TOOL_ROUNDS = 8

    # New observation-system defaults.
    _DEFAULT_HARVEST_INTERVAL_SECONDS = 21_600  # 6h
    _DEFAULT_HARVEST_MAX_CONVERSATIONS_PER_CYCLE = 20
    _DEFAULT_ABANDONMENT_THRESHOLD_SECONDS = 86_400  # 24h
    _DEFAULT_OBSERVATION_CAP_TOTAL = 5_000
    _DEFAULT_OBSERVATION_FLUSH_INTERVAL_SECONDS = 30
    _DEFAULT_OBSERVATION_FLUSH_THRESHOLD = 50
    _DEFAULT_REFLECTION_OBSERVATION_LIMIT = 400

    def __init__(self) -> None:
        # Configuration — populated in start() / on_config_changed().
        self._enabled: bool = self._DEFAULT_ENABLED
        self._reflection_interval_seconds: int = self._DEFAULT_REFLECTION_INTERVAL_SECONDS
        self._max_proposals_per_cycle: int = self._DEFAULT_MAX_PROPOSALS_PER_CYCLE
        self._min_observations_per_cycle: int = self._DEFAULT_MIN_OBSERVATIONS_PER_CYCLE
        self._max_pending_proposals: int = self._DEFAULT_MAX_PENDING_PROPOSALS
        self._ai_profile: str = self._DEFAULT_AI_PROFILE
        self._observation_patterns: tuple[str, ...] = _DEFAULT_OBSERVATION_PATTERNS
        self._harvest_interval_seconds: int = self._DEFAULT_HARVEST_INTERVAL_SECONDS
        self._harvest_max_conversations_per_cycle: int = (
            self._DEFAULT_HARVEST_MAX_CONVERSATIONS_PER_CYCLE
        )
        self._abandonment_threshold_seconds: int = (
            self._DEFAULT_ABANDONMENT_THRESHOLD_SECONDS
        )
        self._observation_cap_total: int = self._DEFAULT_OBSERVATION_CAP_TOTAL
        self._observation_flush_threshold: int = (
            self._DEFAULT_OBSERVATION_FLUSH_THRESHOLD
        )
        self._observation_flush_interval_seconds: int = (
            self._DEFAULT_OBSERVATION_FLUSH_INTERVAL_SECONDS
        )
        self._reflection_observation_limit: int = (
            self._DEFAULT_REFLECTION_OBSERVATION_LIMIT
        )
        self._allow_core_modifications: bool = self._DEFAULT_ALLOW_CORE_MODIFICATIONS
        self._reflection_max_tool_rounds: int = self._DEFAULT_REFLECTION_MAX_TOOL_ROUNDS

        # Runtime state.
        self._resolver: ServiceResolver | None = None
        self._storage: StorageBackend | None = None
        self._event_bus: Any = None

        # Pending observations not yet flushed to the DB. Each entry is
        # a fully-formed observation row (matching ``OBSERVATIONS_COLLECTION``
        # schema). Flushed on size-threshold, periodic timer, and stop.
        self._observation_buffer: list[dict[str, Any]] = []
        # Single lock guarding the buffer so concurrent event handlers
        # and the periodic flusher don't trip over each other.
        self._buffer_lock: asyncio.Lock | None = None

        self._unsubscribers: list[Any] = []
        self._scheduler_job_registered: bool = False
        # Background tasks we kicked off (e.g. fire-and-forget archive
        # extraction). Tracked so they don't get GC'd mid-run and so
        # ``stop()`` can cancel any still in flight.
        self._background_tasks: set[asyncio.Task[Any]] = set()
        # Mutexes preventing overlapping reflection / harvest runs.
        # Manual triggers and the scheduler share these — if the
        # scheduled cycle is mid-flight when an admin clicks "Reflect
        # now", we surface a clear "already running" instead of
        # double-firing the AI call.
        self._reflection_running: bool = False
        self._harvest_running: bool = False

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="proposals",
            capabilities=frozenset({"proposals", "ws_handlers", "ai_tools"}),
            optional=frozenset(
                {"entity_storage", "event_bus", "scheduler", "ai_chat", "configuration"}
            ),
            ai_calls=frozenset({"record_observation"}),
            events=frozenset(
                {
                    "proposal.created",
                    "proposal.status_changed",
                    "proposal.reflection_completed",
                    "proposal.harvest_completed",
                },
            ),
            toggleable=True,
            toggle_description="Autonomously proposes self-improvements (admin-only).",
        )

    # ── Lifecycle ────────────────────────────────────────────────────

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver
        self._buffer_lock = asyncio.Lock()

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

        # Wire up entity storage + indexes.
        storage_svc = resolver.get_capability("entity_storage")
        if storage_svc is not None and isinstance(storage_svc, StorageProvider):
            self._storage = storage_svc.backend
            await self._ensure_indexes()
        else:
            logger.warning(
                "Proposals service has no entity storage — list/get will return nothing",
            )

        # Subscribe to events for the observation buffer + the
        # pre-delete archive event for last-chance conversation
        # extraction.
        bus_svc = resolver.get_capability("event_bus")
        if bus_svc is not None and isinstance(bus_svc, EventBusProvider):
            self._event_bus = bus_svc.bus
            for pattern in self._observation_patterns:
                unsub = self._event_bus.subscribe_pattern(pattern, self._on_event)
                self._unsubscribers.append(unsub)
            unsub = self._event_bus.subscribe(
                _CHAT_ARCHIVING_EVENT, self._on_chat_archiving
            )
            self._unsubscribers.append(unsub)
        else:
            logger.warning(
                "Proposals service has no event bus — observations will be sparse",
            )

        # Register periodic jobs (reflection, conversation harvest,
        # observation buffer flush).
        scheduler_svc = resolver.get_capability("scheduler")
        if scheduler_svc is not None and isinstance(scheduler_svc, SchedulerProvider):
            self._register_jobs(scheduler_svc)
        else:
            logger.warning(
                "Proposals service has no scheduler — reflection / harvest "
                "only run via manual trigger; observations flush on threshold only",
            )

        logger.info(
            "Proposals service started (reflection every %ds, harvest every %ds, max %d proposals/cycle)",
            self._reflection_interval_seconds,
            self._harvest_interval_seconds,
            self._max_proposals_per_cycle,
        )

    def _register_jobs(self, scheduler: SchedulerProvider) -> None:
        """Register all our scheduler jobs, tolerating duplicates."""
        for name, schedule, callback in (
            (
                _REFLECTION_JOB_NAME,
                Schedule.every(self._reflection_interval_seconds),
                self._scheduled_reflection_callback,
            ),
            (
                _HARVEST_JOB_NAME,
                Schedule.every(self._harvest_interval_seconds),
                self._scheduled_harvest_callback,
            ),
            (
                _FLUSH_JOB_NAME,
                Schedule.every(self._observation_flush_interval_seconds),
                self._scheduled_flush_callback,
            ),
        ):
            try:
                scheduler.add_job(
                    name=name,
                    schedule=schedule,
                    callback=callback,
                    system=True,
                )
            except ValueError:
                # Already registered — happens after a hot-swap restart.
                pass
        self._scheduler_job_registered = True

    async def stop(self) -> None:
        # Drop subscriptions first so no new events arrive while we
        # flush the tail of the buffer.
        for unsub in self._unsubscribers:
            try:
                unsub()
            except Exception:
                logger.debug("Proposals: unsubscribe raised", exc_info=True)
        self._unsubscribers.clear()
        # Let any in-flight background extractions finish briefly, then
        # cancel anything still running so shutdown isn't held up.
        if self._background_tasks:
            pending = list(self._background_tasks)
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=5.0,
                )
            except TimeoutError:
                for task in pending:
                    if not task.done():
                        task.cancel()
        self._background_tasks.clear()
        try:
            await self._flush_observations()
        except Exception:
            logger.debug("Proposals: final flush raised", exc_info=True)

    def _spawn_background(self, coro: Any, *, label: str) -> None:
        """Schedule a fire-and-forget task and track it for cleanup.

        Used by event handlers that must NOT block their publisher (e.g.
        the pre-delete archive handler — the user's deletion shouldn't
        wait on an AI extraction round). The task self-removes from the
        tracking set when it finishes so the set stays small.
        """
        try:
            task = asyncio.create_task(coro, name=label)
        except RuntimeError:
            # No running loop — coro will not run. Caller is in a sync
            # context that can't schedule async work; closing the
            # coroutine prevents the un-awaited warning.
            coro.close()
            return
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

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
        for fields in (
            ["source_type"],
            ["occurred_at"],
            ["consumed_in_cycle"],
            ["details.conversation_id"],
        ):
            try:
                await self._storage.ensure_index(
                    IndexDefinition(collection=OBSERVATIONS_COLLECTION, fields=fields),
                )
            except Exception:
                logger.debug(
                    "Proposals: ensure_index(observations,%s) failed",
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
                key="harvest_interval_seconds",
                type=ToolParameterType.INTEGER,
                description=(
                    "How often the conversation-harvest job runs. The harvest "
                    "walks active and abandoned conversations and asks the AI "
                    "to extract observation candidates from each. Default "
                    "21600 = 6h."
                ),
                default=self._DEFAULT_HARVEST_INTERVAL_SECONDS,
                restart_required=True,
            ),
            ConfigParam(
                key="harvest_max_conversations_per_cycle",
                type=ToolParameterType.INTEGER,
                description=(
                    "Hard cap on conversations processed per harvest run. Each "
                    "conversation is one AI extraction call, so this is the "
                    "per-cycle cost ceiling."
                ),
                default=self._DEFAULT_HARVEST_MAX_CONVERSATIONS_PER_CYCLE,
            ),
            ConfigParam(
                key="abandonment_threshold_seconds",
                type=ToolParameterType.INTEGER,
                description=(
                    "A conversation is considered 'abandoned' once its "
                    "updated_at is older than this. Active conversations "
                    "produce 'conversation_active' observations; abandoned "
                    "ones produce 'conversation_abandoned'. Default 86400 = 24h."
                ),
                default=self._DEFAULT_ABANDONMENT_THRESHOLD_SECONDS,
            ),
            ConfigParam(
                key="observation_cap_total",
                type=ToolParameterType.INTEGER,
                description=(
                    "Total observation rows kept in the database. The oldest "
                    "rows are pruned at the start of each reflection cycle "
                    "once this cap is exceeded."
                ),
                default=self._DEFAULT_OBSERVATION_CAP_TOTAL,
            ),
            ConfigParam(
                key="observation_flush_threshold",
                type=ToolParameterType.INTEGER,
                description=(
                    "Buffered observations flush to the DB once this many have "
                    "queued (a periodic timer also flushes on a slower schedule)."
                ),
                default=self._DEFAULT_OBSERVATION_FLUSH_THRESHOLD,
            ),
            ConfigParam(
                key="observation_flush_interval_seconds",
                type=ToolParameterType.INTEGER,
                description=(
                    "Periodic flush cadence for the observation buffer. Even if "
                    "the size threshold isn't hit, the buffer is drained this "
                    "often so observations don't sit unflushed."
                ),
                default=self._DEFAULT_OBSERVATION_FLUSH_INTERVAL_SECONDS,
                restart_required=True,
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
            ConfigParam(
                key="allow_core_modifications",
                type=ToolParameterType.BOOLEAN,
                description=(
                    "Allow the reflector to propose changes to Gilbert's "
                    "own core code (``src/gilbert/``). When OFF (the "
                    "default), proposals are limited to new plugins, "
                    "modifications to plugins the reflector itself created "
                    "previously, and configuration changes. Turn this ON "
                    "only if you're comfortable letting Gilbert suggest "
                    "edits to interfaces, services, and bootstrap code."
                ),
                default=self._DEFAULT_ALLOW_CORE_MODIFICATIONS,
            ),
            ConfigParam(
                key="reflection_max_tool_rounds",
                type=ToolParameterType.INTEGER,
                description=(
                    "Maximum number of tool-calling rounds the reflection "
                    "AI may take before it must emit its final JSON. "
                    "Source-inspection tools count against this cap."
                ),
                default=self._DEFAULT_REFLECTION_MAX_TOOL_ROUNDS,
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
        self._harvest_interval_seconds = max(
            60,
            int(
                config.get(
                    "harvest_interval_seconds",
                    self._DEFAULT_HARVEST_INTERVAL_SECONDS,
                ),
            ),
        )
        self._harvest_max_conversations_per_cycle = max(
            0,
            int(
                config.get(
                    "harvest_max_conversations_per_cycle",
                    self._DEFAULT_HARVEST_MAX_CONVERSATIONS_PER_CYCLE,
                ),
            ),
        )
        self._abandonment_threshold_seconds = max(
            60,
            int(
                config.get(
                    "abandonment_threshold_seconds",
                    self._DEFAULT_ABANDONMENT_THRESHOLD_SECONDS,
                ),
            ),
        )
        self._observation_cap_total = max(
            100,
            int(
                config.get(
                    "observation_cap_total",
                    self._DEFAULT_OBSERVATION_CAP_TOTAL,
                ),
            ),
        )
        self._observation_flush_threshold = max(
            1,
            int(
                config.get(
                    "observation_flush_threshold",
                    self._DEFAULT_OBSERVATION_FLUSH_THRESHOLD,
                ),
            ),
        )
        self._observation_flush_interval_seconds = max(
            5,
            int(
                config.get(
                    "observation_flush_interval_seconds",
                    self._DEFAULT_OBSERVATION_FLUSH_INTERVAL_SECONDS,
                ),
            ),
        )
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
        self._allow_core_modifications = bool(
            config.get(
                "allow_core_modifications",
                self._DEFAULT_ALLOW_CORE_MODIFICATIONS,
            ),
        )
        self._reflection_max_tool_rounds = max(
            1,
            int(
                config.get(
                    "reflection_max_tool_rounds",
                    self._DEFAULT_REFLECTION_MAX_TOOL_ROUNDS,
                ),
            ),
        )

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
            ConfigAction(
                key="trigger_harvest",
                label="Run conversation harvest now",
                description=(
                    "Manually walk all conversations and extract "
                    "observation candidates. Subject to the per-cycle "
                    "conversation cap."
                ),
            ),
        ]

    async def invoke_config_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        if key == "trigger_reflection":
            status = self.start_reflection_in_background()
            return _action_status_result(status, kind="Reflection")
        if key == "trigger_harvest":
            status = self.start_harvest_in_background()
            return _action_status_result(status, kind="Harvest")
        return ConfigActionResult(status="error", message=f"Unknown action: {key}")

    # ── Observation ──────────────────────────────────────────────────

    async def _on_event(self, event: Event) -> None:
        """Buffer an event observation.

        Synchronous summarization only — we never call the AI from this
        path. Noisy hot-path event types are dropped at the gate to
        keep the buffer focused on signals worth reflecting on.
        """
        if event.event_type in _NOISY_EVENT_TYPES:
            return
        # Avoid recording our own emissions — they'd be circular and
        # would dominate the buffer once proposal activity picks up.
        if event.event_type.startswith("proposal."):
            return
        try:
            summary = self._summarize_event_data(event.data)
            await self._record_observation(
                source_type=SOURCE_EVENT,
                summary=summary or event.event_type,
                occurred_at=event.timestamp,
                details={
                    "event_type": event.event_type,
                    "source": event.source or "",
                    **{
                        k: v
                        for k, v in event.data.items()
                        if isinstance(v, (str, int, float, bool))
                    },
                },
            )
        except Exception:
            logger.debug("Proposals: failed to record observation", exc_info=True)

    async def _record_observation(
        self,
        *,
        source_type: str,
        summary: str,
        occurred_at: datetime,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Append an observation row to the in-memory buffer.

        Flushes immediately when the buffer hits the size threshold so a
        burst of events is bounded. Otherwise, the periodic flush job
        drains it. Observations are stored with ``consumed_in_cycle=""``
        until a reflection cycle marks them.
        """
        now = datetime.now(UTC)
        obs_id = f"{int(now.timestamp() * 1000)}-{uuid.uuid4().hex[:8]}"
        row = {
            "_id": obs_id,
            "id": obs_id,
            "source_type": source_type,
            "summary": (summary or "")[:500],
            "details": details or {},
            "occurred_at": occurred_at.isoformat()
            if isinstance(occurred_at, datetime)
            else str(occurred_at),
            "created_at": now.isoformat(),
            "consumed_in_cycle": "",
        }
        if self._buffer_lock is None:
            # Pre-start state — drop. We only buffer once the service
            # is running so tests that exercise observation paths
            # before start() don't accumulate cross-test state.
            return
        async with self._buffer_lock:
            self._observation_buffer.append(row)
            should_flush = len(self._observation_buffer) >= self._observation_flush_threshold
        if should_flush:
            await self._flush_observations()

    async def _flush_observations(self) -> None:
        """Move buffered observations into the entity store.

        Best-effort: each row is written individually so a partial DB
        failure doesn't lose the whole batch. The buffer is drained
        BEFORE writes so concurrent record_observation calls always see
        a fresh buffer to append to.
        """
        if self._storage is None or self._buffer_lock is None:
            return
        async with self._buffer_lock:
            pending = self._observation_buffer
            self._observation_buffer = []
        if not pending:
            return
        for row in pending:
            try:
                await self._storage.put(OBSERVATIONS_COLLECTION, row["_id"], row)
            except Exception:
                logger.debug(
                    "Proposals: failed to persist observation %s",
                    row.get("_id"),
                    exc_info=True,
                )

    async def _scheduled_flush_callback(self) -> None:
        """Scheduler entry point — periodic buffer flush."""
        try:
            await self._flush_observations()
        except Exception:
            logger.exception("Proposals: scheduled flush raised")

    async def _prune_observations(self) -> None:
        """Drop the oldest observations once the cap is exceeded.

        Runs at the start of each reflection cycle. Counts the total
        rows; if over cap, queries the oldest and deletes them. Cheap
        no-op when under the cap.
        """
        if self._storage is None:
            return
        try:
            total = await self._storage.count(Query(collection=OBSERVATIONS_COLLECTION))
        except Exception:
            logger.debug("Proposals: count for prune failed", exc_info=True)
            return
        excess = total - self._observation_cap_total
        if excess <= 0:
            return
        try:
            oldest = await self._storage.query(
                Query(
                    collection=OBSERVATIONS_COLLECTION,
                    sort=[SortField(field="occurred_at", descending=False)],
                    limit=excess,
                ),
            )
        except Exception:
            logger.debug("Proposals: prune query failed", exc_info=True)
            return
        for row in oldest:
            try:
                await self._storage.delete(OBSERVATIONS_COLLECTION, row["_id"])
            except Exception:
                logger.debug(
                    "Proposals: prune delete failed for %s",
                    row.get("_id"),
                    exc_info=True,
                )
        if oldest:
            logger.info("Proposals: pruned %d old observations", len(oldest))

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

    async def observation_count(self) -> int:
        """Total observation rows currently in the DB (post-flush)."""
        if self._storage is None:
            return len(self._observation_buffer)
        try:
            persisted = await self._storage.count(
                Query(collection=OBSERVATIONS_COLLECTION),
            )
        except Exception:
            persisted = 0
        return persisted + len(self._observation_buffer)

    async def _count_unconsumed_observations(self) -> int:
        """Count observations not yet attributed to a reflection cycle.

        Includes the in-memory buffer (which is by definition unconsumed)
        so the min-observations gate sees a faithful "new since last
        cycle" number.
        """
        if self._storage is None:
            return len(self._observation_buffer)
        try:
            persisted = await self._storage.count(
                Query(
                    collection=OBSERVATIONS_COLLECTION,
                    filters=[
                        Filter(field="consumed_in_cycle", op=FilterOp.EQ, value=""),
                    ],
                ),
            )
        except Exception:
            persisted = 0
        return persisted + len(self._observation_buffer)

    def _resolve_inspector_tools(
        self,
    ) -> tuple[list[ToolDefinition], Any]:
        """Return source-inspection tool defs + an executor coroutine.

        Always-on for the reflection AI: even if an admin disabled the
        inspector for normal AI profiles, the proposals reflector still
        gets the tools so it can ground its proposals in the actual
        source. Returns ``([], None)`` if the inspector service isn't
        registered (e.g., unit-test resolver without it).
        """
        if self._resolver is None:
            return [], None
        inspector = self._resolver.get_capability("source_inspector")
        if inspector is None:
            return [], None
        get_tool_definitions = getattr(inspector, "get_tool_definitions", None)
        execute_tool = getattr(inspector, "execute_tool", None)
        if get_tool_definitions is None or execute_tool is None:
            return [], None
        try:
            tools = list(get_tool_definitions())
        except Exception:
            logger.debug(
                "Proposals: inspector get_tool_definitions failed",
                exc_info=True,
            )
            return [], None
        return tools, execute_tool

    # ── Reflection ───────────────────────────────────────────────────

    async def _scheduled_reflection_callback(self) -> None:
        """Scheduler entry point — never raises."""
        if self._reflection_running:
            logger.debug("Proposals: scheduled reflection skipped — already running")
            return
        try:
            await self._run_reflection(manual=False)
        except Exception:
            logger.exception("Proposals reflection cycle raised")

    async def trigger_reflection(self) -> int:
        """Run a reflection cycle now (synchronous — for tests / programmatic use).

        Returns the number of new proposals stored. WS callers should
        prefer ``start_reflection_in_background`` so the request doesn't
        block on the AI round.
        """
        return await self._run_reflection(manual=True)

    def start_reflection_in_background(self) -> str:
        """Kick off a reflection cycle as a background task.

        Returns one of ``"started"`` / ``"already_running"`` / ``"disabled"``.
        Used by the WS handler and config action so the user-facing
        request returns immediately — the AI round can take 30+ seconds
        on the advanced profile and would otherwise blow the RPC timeout.
        """
        if not self._enabled:
            return "disabled"
        if self._reflection_running:
            return "already_running"
        self._spawn_background(
            self._run_reflection_safe(manual=True),
            label="proposals.reflection.manual",
        )
        return "started"

    async def _run_reflection_safe(self, *, manual: bool) -> None:
        """Background-task wrapper — swallow exceptions, never propagate."""
        try:
            await self._run_reflection(manual=manual)
        except Exception:
            logger.exception("Proposals: background reflection raised")

    async def _run_reflection(self, *, manual: bool) -> int:
        """Build context, ask the AI for proposals, persist whatever comes back.

        ``manual=True`` bypasses the min-observations gate (the operator
        explicitly asked) but the pending-cap and per-cycle ceiling still
        apply so a manual trigger can't pile up runaway cost either.

        The ``_reflection_running`` flag prevents overlapping cycles —
        it's checked by callers, but also by the inner body as a
        belt-and-suspenders guard so concurrent calls can't both pass
        the check.
        """
        if not self._enabled:
            logger.info("Proposals: reflection skipped (service disabled)")
            return 0
        if self._max_proposals_per_cycle <= 0:
            logger.info("Proposals: reflection skipped (max_proposals_per_cycle=0)")
            return 0
        if self._reflection_running:
            logger.info("Proposals: reflection skipped — already running")
            return 0

        self._reflection_running = True
        created = 0
        considered = 0
        try:
            created, considered = await self._run_reflection_inner(manual=manual)
            return created
        finally:
            self._reflection_running = False
            await self._publish(
                "proposal.reflection_completed",
                {
                    "created": created,
                    "observations_considered": considered,
                    "manual": manual,
                },
            )

    async def _run_reflection_inner(self, *, manual: bool) -> tuple[int, int]:
        """Returns (proposals_created, observations_considered)."""
        # Drain the buffer and prune old observations before counting,
        # so the gate sees an accurate "new since last cycle" number.
        await self._flush_observations()
        await self._prune_observations()

        unconsumed = await self._count_unconsumed_observations()
        if not manual and unconsumed < self._min_observations_per_cycle:
            logger.info(
                "Proposals: reflection skipped — only %d new observations (need %d)",
                unconsumed,
                self._min_observations_per_cycle,
            )
            return 0, 0

        pending_count = await self._count_pending_proposals()
        if pending_count >= self._max_pending_proposals:
            logger.info(
                "Proposals: reflection skipped — %d pending proposals already (cap %d)",
                pending_count,
                self._max_pending_proposals,
            )
            return 0, 0

        ai_svc: Any = (
            self._resolver.get_capability("ai_chat") if self._resolver is not None else None
        )
        if not isinstance(ai_svc, AISamplingProvider):
            logger.warning("Proposals: reflection skipped — no AI service available")
            return 0, 0

        # Pull the observations we'll cite, build the prompt, then call.
        observations = await self._load_unconsumed_observations()
        user_prompt = await self._build_reflection_user_prompt(observations)
        cycle_id = uuid.uuid4().hex

        # Source-inspection tools are ALWAYS injected here, regardless
        # of the inspector service's user-facing enabled flag. The
        # proposals service has already decided this call is happening
        # and benefits from the AI being able to read source.
        inspector_tools, inspector_executor = self._resolve_inspector_tools()

        messages: list[Message] = [Message(role=MessageRole.USER, content=user_prompt)]
        text = ""
        try:
            for _round_num in range(self._reflection_max_tool_rounds):
                response = await ai_svc.complete_one_shot(
                    messages=messages,
                    system_prompt=_REFLECTION_SYSTEM_PROMPT,
                    profile_name=self._ai_profile,
                    tools_override=inspector_tools,
                )
                # Always append the assistant's reply so the next round
                # has the prior tool_calls in context.
                messages.append(response.message)

                if (
                    response.stop_reason == StopReason.TOOL_USE
                    and response.message.tool_calls
                    and inspector_executor is not None
                ):
                    tool_results: list[ToolResult] = []
                    for call in response.message.tool_calls:
                        try:
                            content = await inspector_executor(
                                call.tool_name, call.arguments
                            )
                            is_error = False
                        except Exception as exc:
                            content = json.dumps({"error": str(exc)})
                            is_error = True
                        tool_results.append(
                            ToolResult(
                                tool_call_id=call.tool_call_id,
                                content=content,
                                is_error=is_error,
                            ),
                        )
                    messages.append(
                        Message(
                            role=MessageRole.TOOL_RESULT,
                            tool_results=tool_results,
                        ),
                    )
                    continue

                # Either END_TURN, MAX_TOKENS, or a tool call we can't
                # service — take whatever text we got and break out.
                text = (response.message.content or "").strip()
                break
            else:
                # Loop exhausted without END_TURN. Use the last message's
                # text if there is any so we don't waste the round.
                logger.warning(
                    "Proposals: reflection hit tool-round cap (%d), "
                    "using last assistant text",
                    self._reflection_max_tool_rounds,
                )
                last = messages[-1] if messages else None
                if last is not None and last.role == MessageRole.ASSISTANT:
                    text = (last.content or "").strip()
        except Exception:
            logger.exception("Proposals: AI call failed during reflection")
            return 0, len(observations)

        proposals = self._parse_proposals_response(text)
        # Mark the observations we showed the AI as consumed regardless
        # of whether it returned proposals — they've been "seen", so the
        # next cycle should focus on what's NEW. Same signal won't be
        # retried; the AI either already extracted what was useful or
        # decided there was nothing.
        await self._mark_consumed(observations, cycle_id)

        if not proposals:
            logger.info("Proposals: AI returned no proposals (cycle=%s)", cycle_id)
            return 0, len(observations)

        # Cap to per-cycle ceiling and pending budget.
        capacity = max(0, self._max_pending_proposals - pending_count)
        cap = min(self._max_proposals_per_cycle, capacity)
        proposals = proposals[:cap]

        created = 0
        for raw in proposals:
            try:
                record = self._build_record(raw, cycle_id=cycle_id)
            except ValueError as exc:
                logger.warning("Proposals: discarding malformed AI proposal: %s", exc)
                continue
            await self._persist_proposal(record)
            created += 1

        logger.info(
            "Proposals: reflection cycle %s created %d proposal(s) from %d observations",
            cycle_id,
            created,
            len(observations),
        )
        return created, len(observations)

    async def _publish(self, event_type: str, data: dict[str, Any]) -> None:
        """Best-effort publish — never raises into the caller."""
        if self._event_bus is None:
            return
        try:
            await self._event_bus.publish(
                Event(event_type=event_type, data=data, source="proposals"),
            )
        except Exception:
            logger.debug("Proposals: publish %s failed", event_type, exc_info=True)

    async def _load_unconsumed_observations(self) -> list[dict[str, Any]]:
        """Return the observations the upcoming cycle will reason over."""
        if self._storage is None:
            return []
        try:
            return await self._storage.query(
                Query(
                    collection=OBSERVATIONS_COLLECTION,
                    filters=[
                        Filter(field="consumed_in_cycle", op=FilterOp.EQ, value=""),
                    ],
                    sort=[SortField(field="occurred_at", descending=True)],
                    limit=self._reflection_observation_limit,
                ),
            )
        except Exception:
            logger.debug("Proposals: load observations failed", exc_info=True)
            return []

    async def _mark_consumed(
        self, observations: list[dict[str, Any]], cycle_id: str
    ) -> None:
        if self._storage is None or not observations:
            return
        for row in observations:
            row["consumed_in_cycle"] = cycle_id
            try:
                await self._storage.put(OBSERVATIONS_COLLECTION, row["_id"], row)
            except Exception:
                logger.debug(
                    "Proposals: mark consumed failed for %s",
                    row.get("_id"),
                    exc_info=True,
                )

    async def _build_reflection_user_prompt(
        self, observations: list[dict[str, Any]]
    ) -> str:
        """Compose the user-side reflection prompt.

        Sections: source-grouped observations (events vs in-chat notes
        vs conversation extracts vs deletion extracts), currently-
        active capabilities (so the AI doesn't re-propose existing
        ones), and recent proposals (so the AI doesn't re-propose
        pending ideas). The source breakdown helps the AI weight
        signals differently — an in-chat note from Gilbert himself is
        usually a stronger signal than 200 raw event firings.
        """
        observations_block = self._format_observations_by_source(observations)
        capabilities_block = self._build_capabilities_snapshot()
        recent_proposals_block = await self._build_recent_proposals_snapshot()
        core_mods_state = "ON" if self._allow_core_modifications else "OFF"
        return (
            "Reflect on the activity below and propose any improvements.\n\n"
            "## Settings snapshot\n"
            f"- allow_core_modifications: {core_mods_state}\n\n"
            "## Observations\n"
            f"{observations_block}\n\n"
            "## Currently active capabilities\n"
            f"{capabilities_block}\n\n"
            "## Recent proposals (do not duplicate)\n"
            f"{recent_proposals_block}\n\n"
            "You may use the source-inspection tools "
            "(``gilbert_list_files``, ``gilbert_read_file``, "
            "``gilbert_grep``) to ground your proposals in the actual "
            "current code before answering. When you're done inspecting, "
            f"return at most {self._max_proposals_per_cycle} proposal(s) "
            f"as JSON. If nothing is worth proposing, return "
            '{"proposals": []}.\n'
        )

    @staticmethod
    def _format_observations_by_source(observations: list[dict[str, Any]]) -> str:
        """Render observations grouped by source_type with per-group caps."""
        if not observations:
            return "(no observations yet)"
        by_source: dict[str, list[dict[str, Any]]] = {}
        for obs in observations:
            by_source.setdefault(obs.get("source_type", "unknown"), []).append(obs)

        sections: list[str] = []
        for source in OBSERVATION_SOURCES:
            rows = by_source.pop(source, [])
            if not rows:
                continue
            sections.append(
                ProposalsService._format_one_source_section(source, rows),
            )
        # Anything we didn't recognize, render last so we don't lose it.
        for source, rows in by_source.items():
            sections.append(ProposalsService._format_one_source_section(source, rows))
        return "\n\n".join(sections)

    @staticmethod
    def _format_one_source_section(source: str, rows: list[dict[str, Any]]) -> str:
        """Render one source's observations.

        Events are grouped by event_type (since one type can fire
        thousands of times); other sources list each row directly with
        its summary, since they're already higher-signal entries.
        """
        if source == SOURCE_EVENT:
            type_counts: Counter[str] = Counter()
            latest: dict[str, dict[str, Any]] = {}
            for r in rows:
                event_type = str(r.get("details", {}).get("event_type", "?"))
                type_counts[event_type] += 1
                latest[event_type] = r
            lines = [f"### {source} ({len(rows)} total)"]
            for event_type, count in type_counts.most_common(40):
                sample = latest[event_type]
                summary = sample.get("summary") or "(no summary)"
                occurred = sample.get("occurred_at", "")
                lines.append(
                    f"- {event_type} ({count}×, last {occurred}): {summary}"
                )
            return "\n".join(lines)
        # Non-event sources — one line per observation.
        lines = [f"### {source} ({len(rows)} total)"]
        for r in rows[:40]:
            occurred = r.get("occurred_at", "")
            details = r.get("details") or {}
            extras: list[str] = []
            for k in ("category", "conversation_id", "state"):
                if k in details:
                    extras.append(f"{k}={details[k]}")
            extras_str = f" [{' '.join(extras)}]" if extras else ""
            lines.append(f"- ({occurred}){extras_str} {r.get('summary', '')}")
        return "\n".join(lines)

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
            kind = KIND_NEW_PLUGIN  # safe default — no destructive action implied
        if kind == KIND_MODIFY_CORE and not self._allow_core_modifications:
            # The system prompt told the AI not to emit this when the
            # flag is off, but the model occasionally ignores that.
            # Don't lose the idea — relabel as a plugin proposal so the
            # human reviewer can decide whether the spec actually fits a
            # plugin or warrants flipping the flag.
            logger.info(
                "Proposals: downgrading modify_core -> new_plugin "
                "(allow_core_modifications is off)",
            )
            kind = KIND_NEW_PLUGIN

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

    # ── Conversation harvest ─────────────────────────────────────────

    async def _scheduled_harvest_callback(self) -> None:
        """Scheduler entry point — never raises."""
        if self._harvest_running:
            logger.debug("Proposals: scheduled harvest skipped — already running")
            return
        try:
            await self._run_harvest()
        except Exception:
            logger.exception("Proposals: harvest cycle raised")

    async def trigger_harvest(self) -> int:
        """Run the conversation harvest now (synchronous — for tests).

        WS callers should prefer ``start_harvest_in_background`` so the
        request doesn't block on per-conversation AI calls.
        """
        return await self._run_harvest()

    def start_harvest_in_background(self) -> str:
        """Kick off a harvest as a background task. See start_reflection_in_background."""
        if not self._enabled:
            return "disabled"
        if self._harvest_running:
            return "already_running"
        self._spawn_background(
            self._run_harvest_safe(),
            label="proposals.harvest.manual",
        )
        return "started"

    async def _run_harvest_safe(self) -> None:
        try:
            await self._run_harvest()
        except Exception:
            logger.exception("Proposals: background harvest raised")

    async def _run_harvest(self) -> int:
        """Walk conversations and extract observations from each.

        - Pulls all conversations (capped per-cycle).
        - Classifies each as ``active`` (recent ``updated_at``) or
          ``abandoned`` (older than the threshold).
        - Skips conversations where the most recent observation already
          covers the current message_count — avoids re-summarizing the
          same content.
        - For each remaining conversation, asks the AI to extract
          observation candidates and persists them.
        """
        if not self._enabled:
            return 0
        if self._storage is None:
            return 0
        if self._harvest_max_conversations_per_cycle <= 0:
            return 0
        if self._harvest_running:
            logger.info("Proposals: harvest skipped — already running")
            return 0
        self._harvest_running = True
        try:
            return await self._run_harvest_inner()
        finally:
            self._harvest_running = False

    async def _run_harvest_inner(self) -> int:
        ai_svc: Any = (
            self._resolver.get_capability("ai_chat")
            if self._resolver is not None
            else None
        )
        if not isinstance(ai_svc, AISamplingProvider):
            logger.warning("Proposals: harvest skipped — no AI service available")
            return 0

        try:
            convs = await self._storage.query(  # type: ignore[union-attr]
                Query(
                    collection=_AI_CONVERSATIONS_COLLECTION,
                    sort=[SortField(field="updated_at", descending=True)],
                    limit=self._harvest_max_conversations_per_cycle * 4,
                ),
            )
        except Exception:
            logger.exception("Proposals: harvest conversation query failed")
            return 0

        now = datetime.now(UTC)
        threshold = now - timedelta(seconds=self._abandonment_threshold_seconds)
        created = 0
        processed = 0
        for conv in convs:
            if processed >= self._harvest_max_conversations_per_cycle:
                break
            try:
                if await self._already_summarized(conv):
                    continue
                state_source = (
                    SOURCE_CONVERSATION_ACTIVE
                    if self._is_recent(conv, threshold)
                    else SOURCE_CONVERSATION_ABANDONED
                )
                added = await self._extract_observations_from_conversation(
                    ai_svc=ai_svc,
                    conv=conv,
                    source_type=state_source,
                )
                created += added
                processed += 1
            except Exception:
                logger.exception(
                    "Proposals: harvest of conversation %s failed",
                    conv.get("_id"),
                )
        if processed:
            logger.info(
                "Proposals: harvest processed %d conversation(s), recorded %d observation(s)",
                processed,
                created,
            )
        await self._publish(
            "proposal.harvest_completed",
            {"processed": processed, "observations_recorded": created},
        )
        return created

    @staticmethod
    def _is_recent(conv: dict[str, Any], threshold: datetime) -> bool:
        """True when conv.updated_at is at or after the threshold."""
        updated_at = conv.get("updated_at") or conv.get("created_at") or ""
        if not updated_at:
            return False
        try:
            dt = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
        except ValueError:
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt >= threshold

    async def _already_summarized(self, conv: dict[str, Any]) -> bool:
        """True when the latest observation matches the current message count.

        Uses the most recent observation tagged with this conversation
        id — if its ``details.message_count_at_summary`` equals the
        conversation's current message count, no new content has
        arrived since the last summary and we skip.
        """
        if self._storage is None:
            return False
        conv_id = conv.get("_id")
        if not conv_id:
            return False
        current_count = len(conv.get("messages") or [])
        try:
            recent = await self._storage.query(
                Query(
                    collection=OBSERVATIONS_COLLECTION,
                    filters=[
                        Filter(
                            field="details.conversation_id",
                            op=FilterOp.EQ,
                            value=conv_id,
                        ),
                    ],
                    sort=[SortField(field="occurred_at", descending=True)],
                    limit=1,
                ),
            )
        except Exception:
            return False
        if not recent:
            return False
        last = recent[0].get("details", {}).get("message_count_at_summary")
        return bool(last == current_count)

    async def _extract_observations_from_conversation(
        self,
        *,
        ai_svc: AISamplingProvider,
        conv: dict[str, Any],
        source_type: str,
    ) -> int:
        """Ask the AI to extract observation candidates from a conversation.

        Returns the number of observations stored.
        """
        transcript = self._render_conversation_transcript(conv)
        if not transcript.strip():
            return 0
        conv_id = str(conv.get("_id", ""))
        message_count = len(conv.get("messages") or [])
        user_prompt = (
            f"Conversation id: {conv_id}\n"
            f"State: {source_type}\n"
            f"Message count: {message_count}\n\n"
            "## Transcript\n"
            f"{transcript}\n\n"
            "Return any reflection-worthy observations as JSON, or "
            '{"observations": []} if nothing stands out.'
        )
        try:
            response = await ai_svc.complete_one_shot(
                messages=[Message(role=MessageRole.USER, content=user_prompt)],
                system_prompt=_CONVERSATION_EXTRACTION_SYSTEM_PROMPT,
                profile_name=self._ai_profile,
                tools_override=[],
            )
        except Exception:
            logger.exception(
                "Proposals: extraction call failed for conv %s", conv_id
            )
            return 0
        observations = self._parse_observations_response(
            response.message.content or ""
        )
        if not observations:
            # Even a "nothing here" response counts as a summary —
            # record a placeholder so we don't re-process the same
            # message_count next cycle. The summary is intentionally
            # dull so it doesn't influence the reflector.
            await self._record_observation(
                source_type=source_type,
                summary=f"(no signals extracted from conv {conv_id[:8]})",
                occurred_at=datetime.now(UTC),
                details={
                    "conversation_id": conv_id,
                    "message_count_at_summary": message_count,
                    "state": source_type,
                    "empty_extraction": True,
                },
            )
            return 0
        now = datetime.now(UTC)
        for obs in observations:
            summary = str(obs.get("summary") or "").strip()
            if not summary:
                continue
            await self._record_observation(
                source_type=source_type,
                summary=summary,
                occurred_at=now,
                details={
                    "conversation_id": conv_id,
                    "message_count_at_summary": message_count,
                    "state": source_type,
                    "category": str(obs.get("category") or "other"),
                },
            )
        # Force a flush so subsequent _already_summarized checks within
        # this same harvest run see the row we just wrote.
        await self._flush_observations()
        return len(observations)

    @staticmethod
    def _render_conversation_transcript(conv: dict[str, Any]) -> str:
        """Build a compact transcript suitable for the extraction prompt.

        Caps per-message text length and total message count so a
        runaway conversation doesn't blow the context window.
        """
        messages = conv.get("messages") or []
        if not messages:
            return ""
        # Keep the most recent ~60 messages — enough to capture intent
        # but bounded.
        slice_ = messages[-60:]
        lines: list[str] = []
        for m in slice_:
            role = str(m.get("role", "?"))
            content = str(m.get("content", "") or "")
            if len(content) > 800:
                content = content[:797] + "..."
            lines.append(f"[{role}] {content}")
        return "\n".join(lines)

    @staticmethod
    def _parse_observations_response(text: str) -> list[dict[str, Any]]:
        """Extract the observations array from the model's JSON response."""
        if not text:
            return []
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
            if stripped.endswith("```"):
                stripped = stripped[: -len("```")]
            stripped = stripped.strip()
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            start = stripped.find("{")
            end = stripped.rfind("}")
            if start == -1 or end == -1 or end <= start:
                return []
            try:
                payload = json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                return []
        if not isinstance(payload, dict):
            return []
        observations = payload.get("observations")
        if not isinstance(observations, list):
            return []
        return [o for o in observations if isinstance(o, dict)]

    # ── Pre-delete extraction (chat.conversation.archiving) ──────────

    async def _on_chat_archiving(self, event: Event) -> None:
        """Last-chance extraction before a conversation is destroyed.

        Returns IMMEDIATELY after capturing the conversation snapshot —
        the actual AI extraction runs in a background task so the
        deletion path (and any other subscribers) aren't blocked by an
        AI round. The extraction has up to ``stop()``'s shutdown grace
        period to finish if a restart lands mid-flight.
        """
        if not self._enabled:
            return
        conv = event.data.get("conversation")
        if not isinstance(conv, dict):
            return
        ai_svc: Any = (
            self._resolver.get_capability("ai_chat")
            if self._resolver is not None
            else None
        )
        if not isinstance(ai_svc, AISamplingProvider):
            return
        # Snapshot the conversation now — the publisher is about to
        # delete the underlying record and we don't want a reference
        # to a dict that gets mutated underneath us.
        conv_snapshot = dict(conv)
        conv_id = conv_snapshot.get("_id", "?")
        self._spawn_background(
            self._archive_extraction_task(ai_svc, conv_snapshot),
            label=f"proposals.archive.{conv_id}",
        )

    async def _archive_extraction_task(
        self,
        ai_svc: AISamplingProvider,
        conv: dict[str, Any],
    ) -> None:
        """Background body for the archive extraction. Never raises."""
        try:
            await self._extract_observations_from_conversation(
                ai_svc=ai_svc,
                conv=conv,
                source_type=SOURCE_CONVERSATION_DELETED,
            )
        except Exception:
            logger.exception(
                "Proposals: archiving extraction failed for conv %s",
                conv.get("_id"),
            )

    # ── ToolProvider (record_observation) ────────────────────────────

    @property
    def tool_provider_name(self) -> str:
        return "proposals"

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="record_observation",
                description=(
                    "Record an observation about Gilbert himself for later "
                    "review by the autonomous self-improvement reflector. "
                    "Use this when, during a conversation, you notice something "
                    "that could lead to a self-improvement: a recurring user "
                    "frustration, a missing capability, a knowledge gap, a "
                    "feature request, or a usage pattern that isn't well "
                    "served. One observation per call; be concise and specific. "
                    "These observations are batched into the next reflection "
                    "cycle, where they may turn into a proposal for an admin "
                    "to review. Admin-only — non-admin users cannot trigger "
                    "this tool."
                ),
                parameters=[
                    ToolParameter(
                        name="summary",
                        type=ToolParameterType.STRING,
                        description=(
                            "A single short sentence describing what you noticed."
                        ),
                    ),
                    ToolParameter(
                        name="category",
                        type=ToolParameterType.STRING,
                        description="What kind of signal this is.",
                        required=False,
                        enum=[
                            "capability_gap",
                            "recurring_frustration",
                            "knowledge_gap",
                            "feature_request",
                            "usage_pattern",
                            "other",
                        ],
                    ),
                    ToolParameter(
                        name="context",
                        type=ToolParameterType.STRING,
                        description=(
                            "Optional one-paragraph context — what was the user "
                            "trying to do, what happened, why it matters."
                        ),
                        required=False,
                    ),
                ],
                required_role="admin",
                parallel_safe=True,
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name != "record_observation":
            raise KeyError(f"Unknown tool: {name}")
        summary = str(arguments.get("summary") or "").strip()
        if not summary:
            return json.dumps({"error": "'summary' is required"})
        category = str(arguments.get("category") or "other").strip()
        context = str(arguments.get("context") or "").strip()
        await self._record_observation(
            source_type=SOURCE_AI_TOOL,
            summary=summary,
            occurred_at=datetime.now(UTC),
            details={
                "category": category,
                "context": context[:1000],
            },
        )
        return json.dumps({"status": "recorded", "category": category})

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
        # Spawn the cycle in the background and return immediately —
        # the AI round can take 30+ seconds on the advanced profile,
        # which would otherwise blow the WS RPC timeout. New proposals
        # appear via the existing list refetch + ``proposal.created``
        # events.
        status = self.start_reflection_in_background()
        return {
            "type": "proposals.trigger_reflection.result",
            "ref": frame.get("id"),
            "status": status,
        }
