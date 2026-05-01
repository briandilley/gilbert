"""User memory synthesis — auto-captures facts about a user from chat
transcripts and persists them as ``user_memories`` entries that the AI's
system prompt builder already injects.

Triggers:
- ``chat.conversation.archiving`` event (fires right before a chat is
  deleted; payload still has the full transcript)
- A scheduled sweep that picks up chats which have gone idle for at
  least ``idle_after_hours`` and haven't been re-synthesized since their
  last update — long-running chats get processed multiple times as new
  messages accumulate.

The synthesis call sees the existing ``user_memories`` for the owner
plus the transcript and returns a JSON list of ops (``add``, ``update``,
``delete``) so it can consolidate instead of stacking duplicates. A
per-user ``asyncio.Lock`` ensures two synthesises for the same user
can't race on the memory list.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from gilbert.interfaces.ai import Message, MessageRole
from gilbert.interfaces.configuration import (
    ConfigParam,
    ConfigurationReader,
)
from gilbert.interfaces.events import Event, EventBusProvider
from gilbert.interfaces.scheduler import Schedule, SchedulerProvider
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import (
    Filter,
    FilterOp,
    IndexDefinition,
    Query,
    StorageBackend,
    StorageProvider,
)
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)


# Collections written by this service. ``user_memories`` is shared with
# the manual ``memory`` AI tool — entries we add are tagged
# ``source="auto"`` so a future UI can distinguish them.
_USER_MEMORIES = "user_memories"
_CHAT_STATE = "user_memory_chat_state"
_CONVERSATIONS = "ai_conversations"

_AI_CALL_NAME = "user_memory_synthesis"
_SWEEP_JOB_NAME = "user_memory.idle_sweep"


_DEFAULT_SYNTHESIS_PROMPT = """\
You are maintaining a private memory file about a single user, derived
from their chat conversations with the assistant. Your job is to keep
the file useful, current, and short.

You will receive:
1. The user's existing memories (each has a memory_id, summary, content,
   and source). Only entries with source="auto" came from this process;
   entries with source="user" were explicitly saved by the user or an
   admin and should be treated as authoritative — never delete them and
   only update them when the new transcript clearly contradicts the
   stored content.
2. A chat transcript between this user and the assistant. The user is
   identified for you. Other speakers in the transcript (the assistant,
   tool results) are NOT the subject — only capture things about the
   user.

Look for things that would help future conversations feel natural:
preferences, working style, recurring projects, communication tone,
domain expertise, things they dislike or have asked you to avoid,
recurring people / pets / places they reference, decisions they've made
about how they want the assistant to behave.

Do NOT capture:
- Anything that's only true for one conversation ("today they're
  working on X").
- Personally sensitive information they didn't volunteer
  intentionally — credentials, medical details, family conflicts.
- Other people's private information mentioned in passing.
- Trivia about a single task they happened to do once.

Output a JSON object with a single key ``ops`` whose value is a list.
Each op is one of:

  {"op": "add", "summary": "...", "content": "..."}
      Save a new memory. ``summary`` is a short headline (≤ 80 chars);
      ``content`` is one or two sentences with the relevant detail.

  {"op": "update", "memory_id": "memory_xxx",
   "summary": "...", "content": "..."}
      Replace an existing entry — use this when the transcript refines
      or contradicts something already stored, instead of adding a
      near-duplicate.

  {"op": "delete", "memory_id": "memory_xxx"}
      Drop a memory the transcript clearly invalidates (e.g. they
      stated a new preference). Only target source="auto" entries.

If the transcript yields nothing worth remembering, return ``{"ops": []}``.
Never output more than 8 ops in one call. Prefer ``update`` over
``add`` when an existing memory is on the same topic. Never duplicate
information that's already captured.

Output ONLY the JSON object — no commentary, no markdown fences."""


@dataclass
class _Op:
    """Parsed synthesis op. Unknown / malformed ops are dropped."""

    op: str
    memory_id: str = ""
    summary: str = ""
    content: str = ""


class UserMemoryService(Service):
    """Auto-captures user memories from chat transcripts.

    Capabilities: ``user_memory_synthesis``.
    Requires: ``ai_chat``, ``entity_storage``.
    Optional: ``event_bus``, ``scheduler``, ``configuration``.
    """

    def __init__(self) -> None:
        self._enabled: bool = True
        self._ai_profile: str = "standard"
        self._synthesis_prompt: str = _DEFAULT_SYNTHESIS_PROMPT
        self._min_user_turns: int = 4
        self._max_memories_per_user: int = 50
        self._max_memory_chars: int = 500
        self._idle_after_hours: int = 24
        self._synthesis_cooldown_minutes: int = 5
        self._opted_out_user_ids: frozenset[str] = frozenset()

        self._ai: Any = None  # AIService
        self._storage: StorageBackend | None = None
        self._scheduler: SchedulerProvider | None = None
        self._user_svc: Any = None  # UserService (optional, for self-opt-out)
        self._unsubscribe_archiving: Any = None

        # Fire-and-forget tasks spawned from event handlers. Tracking
        # them avoids the GC-cancels-an-unreferenced-task gotcha and
        # gives ``stop()`` a chance to drain them on shutdown.
        self._background_tasks: set[asyncio.Task[Any]] = set()

        # Per-user lock so concurrent synthesises don't race on the
        # same memory list. Created lazily on first use.
        self._user_locks: dict[str, asyncio.Lock] = {}
        # Last synthesis monotonic timestamp per user, for the
        # cross-chat cooldown. Map gets pruned only on restart; entries
        # are tiny so the leak is bounded by the user count.
        self._last_synth_at: dict[str, float] = {}

    # ── Service lifecycle ────────────────────────────────────────

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="user_memory",
            capabilities=frozenset({"user_memory_synthesis"}),
            requires=frozenset({"ai_chat", "entity_storage"}),
            optional=frozenset(
                {"event_bus", "scheduler", "configuration", "users"}
            ),
            ai_calls=frozenset({_AI_CALL_NAME}),
            toggleable=True,
            toggle_description="Auto-capture user memories from chats",
        )

    async def start(self, resolver: ServiceResolver) -> None:
        # Read config first so an admin can disable the service before
        # any background work spins up.
        config_svc = resolver.get_capability("configuration")
        if isinstance(config_svc, ConfigurationReader):
            section = config_svc.get_section(self.config_namespace)
            self._apply_config(section)

        if not self._enabled:
            logger.info("UserMemoryService disabled via config")
            return

        self._ai = resolver.require_capability("ai_chat")
        self._user_svc = resolver.get_capability("users")

        storage_svc = resolver.require_capability("entity_storage")
        if isinstance(storage_svc, StorageProvider):
            self._storage = storage_svc.backend
        if self._storage is None:
            raise RuntimeError(
                "UserMemoryService requires entity_storage capability"
            )

        await self._storage.ensure_index(
            IndexDefinition(collection=_USER_MEMORIES, fields=["user_id"])
        )
        await self._storage.ensure_index(
            IndexDefinition(collection=_CONVERSATIONS, fields=["updated_at"])
        )

        event_bus_svc = resolver.get_capability("event_bus")
        if isinstance(event_bus_svc, EventBusProvider):
            self._unsubscribe_archiving = event_bus_svc.bus.subscribe(
                "chat.conversation.archiving",
                self._on_chat_archiving,
            )

        scheduler_svc = resolver.get_capability("scheduler")
        if isinstance(scheduler_svc, SchedulerProvider):
            self._scheduler = scheduler_svc
            self._scheduler.add_job(
                _SWEEP_JOB_NAME,
                Schedule.every(1800),  # 30 minutes
                self._run_idle_sweep,
                system=True,
            )

        logger.info(
            "UserMemoryService started (profile=%s, idle_after_hours=%d, "
            "min_user_turns=%d)",
            self._ai_profile,
            self._idle_after_hours,
            self._min_user_turns,
        )

    async def stop(self) -> None:
        if self._unsubscribe_archiving is not None:
            try:
                self._unsubscribe_archiving()
            except Exception:
                pass
        if self._scheduler is not None:
            try:
                self._scheduler.remove_job(_SWEEP_JOB_NAME)
            except Exception:
                pass
        # Drain in-flight synthesises briefly, then cancel anything
        # still running so shutdown isn't held up by a slow AI call.
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

    def _spawn_background(self, coro: Any, *, label: str) -> None:
        """Schedule a fire-and-forget task and track it for cleanup.

        Used by event handlers that MUST NOT block their publisher —
        ``EventBus.publish`` awaits every subscriber via ``gather``, so a
        synchronous-await on a multi-second AI call inside the handler
        would block the chat-delete RPC return. The task self-removes
        from the tracking set when it finishes so the set stays small;
        ``stop()`` drains anything still pending.
        """
        try:
            task = asyncio.create_task(coro, name=label)
        except RuntimeError:
            # No running loop — coro can't run. Close it so we don't
            # leak an unawaited-coroutine warning.
            coro.close()
            return
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    # ── Configurable protocol ────────────────────────────────────

    @property
    def config_namespace(self) -> str:
        return "user_memory"

    @property
    def config_category(self) -> str:
        # Sits next to ``ai``, ``knowledge``, ``mcp``, ``vision``, etc.
        # in the Settings UI — every other AI-adjacent service uses
        # ``Intelligence``, not ``AI``.
        return "Intelligence"

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="enabled",
                type=ToolParameterType.BOOLEAN,
                description=(
                    "When on, the assistant studies finished chat "
                    "conversations and saves notable facts about the "
                    "user as memories that get injected into future "
                    "system prompts."
                ),
                default=True,
            ),
            ConfigParam(
                key="ai_profile",
                type=ToolParameterType.STRING,
                description=(
                    "AI context profile used for the memory-synthesis "
                    "call. Pick a cheaper profile here to reduce cost."
                ),
                default="standard",
                choices_from="ai_profiles",
            ),
            ConfigParam(
                key="synthesis_prompt",
                type=ToolParameterType.STRING,
                description=(
                    "System prompt sent to the synthesis call. Edit if "
                    "you want to change what the AI looks for, the "
                    "output format guidance, or the privacy rules."
                ),
                default=_DEFAULT_SYNTHESIS_PROMPT,
                multiline=True,
            ),
            ConfigParam(
                key="min_user_turns",
                type=ToolParameterType.INTEGER,
                description=(
                    "Skip synthesis on chats with fewer user messages "
                    "than this — keeps single-shot tests and quick "
                    "questions from generating memories."
                ),
                default=4,
            ),
            ConfigParam(
                key="max_memories_per_user",
                type=ToolParameterType.INTEGER,
                description=(
                    "Hard cap on stored memories per user. The "
                    "synthesis call is asked to consolidate when "
                    "approaching this limit; entries beyond it are "
                    "dropped after each synthesis."
                ),
                default=50,
            ),
            ConfigParam(
                key="max_memory_chars",
                type=ToolParameterType.INTEGER,
                description=(
                    "Per-memory content character cap. Anything longer "
                    "is truncated before storage."
                ),
                default=500,
            ),
            ConfigParam(
                key="idle_after_hours",
                type=ToolParameterType.INTEGER,
                description=(
                    "How long a chat must have been idle before the "
                    "background sweep considers it ready for synthesis."
                ),
                default=24,
            ),
            ConfigParam(
                key="synthesis_cooldown_minutes",
                type=ToolParameterType.INTEGER,
                description=(
                    "Per-user rate limit. Subsequent synthesises for "
                    "the same user are skipped until this many minutes "
                    "have passed."
                ),
                default=5,
            ),
            ConfigParam(
                key="opted_out_user_ids",
                type=ToolParameterType.ARRAY,
                description=(
                    "User IDs that should never have memories "
                    "auto-captured. Users can also opt out themselves "
                    "from the Account page."
                ),
                default=[],
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        was_enabled = self._enabled
        self._apply_config(config)
        if was_enabled and not self._enabled:
            logger.info("UserMemoryService disabled at runtime")
        elif not was_enabled and self._enabled:
            logger.info(
                "UserMemoryService enabled at runtime — restart to "
                "wire up event subscription and scheduler job"
            )

    def _apply_config(self, section: dict[str, Any]) -> None:
        self._enabled = bool(section.get("enabled", True))
        self._ai_profile = str(section.get("ai_profile", "standard"))
        prompt = section.get("synthesis_prompt", "") or ""
        self._synthesis_prompt = prompt or _DEFAULT_SYNTHESIS_PROMPT
        self._min_user_turns = int(section.get("min_user_turns", 4))
        self._max_memories_per_user = int(
            section.get("max_memories_per_user", 50)
        )
        self._max_memory_chars = int(section.get("max_memory_chars", 500))
        self._idle_after_hours = int(section.get("idle_after_hours", 24))
        self._synthesis_cooldown_minutes = int(
            section.get("synthesis_cooldown_minutes", 5)
        )
        opted_out = section.get("opted_out_user_ids", []) or []
        self._opted_out_user_ids = frozenset(str(u) for u in opted_out)

    # ── Triggers ─────────────────────────────────────────────────

    async def _on_chat_archiving(self, event: Event) -> None:
        """Handler for ``chat.conversation.archiving`` — payload still
        carries the full conversation snapshot (the row is deleted right
        after this event fires).

        ``EventBus.publish`` awaits every handler via ``gather``, so the
        whole delete RPC waits for whatever this returns. Synthesis is a
        multi-second AI call, so we hand it off to a tracked background
        task and return immediately — same pattern as
        ``ProposalsService._spawn_background``. The chat row gets
        deleted on schedule and synthesis runs on its own time.
        """
        if not self._enabled:
            return
        conversation = event.data.get("conversation")
        if not isinstance(conversation, dict):
            return
        # The raw storage row doesn't carry ``_id`` (SQLite ``get`` only
        # populates it via ``query``). The event has it explicitly, so
        # graft it on for the gating code below.
        conversation = dict(conversation)
        conversation["_id"] = str(event.data.get("conversation_id") or "")
        self._spawn_background(
            self._safe_synthesize(conversation, source="delete"),
            label=f"user_memory.synth.{conversation['_id']}",
        )

    async def _safe_synthesize(
        self, conversation: dict[str, Any], source: str
    ) -> None:
        """Background-task wrapper around ``_maybe_synthesize`` — never
        let synthesis errors escape the task and pollute the global
        unhandled-exception logs."""
        try:
            await self._maybe_synthesize(conversation, source=source)
        except Exception as exc:
            logger.warning(
                "user_memory: synthesis (%s) failed for %s: %s",
                source,
                conversation.get("_id", "?"),
                exc,
            )

    async def _run_idle_sweep(self) -> None:
        """Scheduler callback — sweeps idle chats once every 30 min."""
        if not self._enabled or self._storage is None:
            return
        cutoff = (
            datetime.now(UTC) - timedelta(hours=self._idle_after_hours)
        ).isoformat()
        try:
            results = await self._storage.query(
                Query(
                    collection=_CONVERSATIONS,
                    filters=[
                        Filter(
                            field="updated_at",
                            op=FilterOp.LT,
                            value=cutoff,
                        )
                    ],
                    limit=200,
                )
            )
        except Exception as exc:
            logger.warning("user_memory: idle sweep query failed: %s", exc)
            return
        for conv in results:
            try:
                await self._maybe_synthesize(conv, source="sweep")
            except Exception as exc:
                logger.warning(
                    "user_memory: sweep synthesis failed for %s: %s",
                    conv.get("_id", "?"),
                    exc,
                )

    # ── Synthesis ────────────────────────────────────────────────

    async def _maybe_synthesize(
        self, conversation: dict[str, Any], source: str
    ) -> None:
        """Run a synthesis call for a chat if all gating checks pass.

        Honors: enabled flag, ownership (no shared rooms — multiple
        authors confound the "about this user" framing), opt-out,
        per-user cooldown, minimum-length, and a per-chat
        ``last_synthesized_at`` watermark so we don't re-process a chat
        that hasn't gained new content since its last synthesis.
        """
        if self._storage is None or self._ai is None:
            return
        chat_id = str(conversation.get("_id") or "")
        user_id = str(conversation.get("user_id") or "")
        if not chat_id or not user_id or user_id in ("system", "guest"):
            return
        if conversation.get("shared"):
            # Shared rooms have multiple authors — synthesis would
            # confuse "about this user" with "things said in front of
            # this user". Out of scope for this round.
            return
        if user_id in self._opted_out_user_ids:
            logger.debug(
                "user_memory: skipping %s — user %s opted out (admin)",
                chat_id,
                user_id,
            )
            return
        if await self._user_self_opted_out(user_id):
            logger.debug(
                "user_memory: skipping %s — user %s opted out (self)",
                chat_id,
                user_id,
            )
            return
        messages = conversation.get("messages") or []
        user_turns = sum(1 for m in messages if m.get("role") == "user")
        if user_turns < self._min_user_turns:
            logger.debug(
                "user_memory: skipping %s — only %d user turns",
                chat_id,
                user_turns,
            )
            return

        # Per-chat watermark: skip if no new activity since last synthesis.
        updated_at = str(conversation.get("updated_at") or "")
        state = await self._storage.get(_CHAT_STATE, chat_id)
        if state is not None and updated_at and state.get(
            "last_synthesized_at_chat_updated"
        ) == updated_at:
            logger.debug(
                "user_memory: skipping %s — already processed at this watermark",
                chat_id,
            )
            return

        # Cross-chat per-user cooldown.
        cooldown_seconds = self._synthesis_cooldown_minutes * 60
        now_mono = time.monotonic()
        last = self._last_synth_at.get(user_id, 0.0)
        if now_mono - last < cooldown_seconds:
            logger.debug(
                "user_memory: skipping %s — user %s in cooldown",
                chat_id,
                user_id,
            )
            return

        # Single-flight per user.
        lock = self._user_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            # Re-check watermark inside the lock — another coroutine
            # may have synthesized while we were waiting.
            state = await self._storage.get(_CHAT_STATE, chat_id)
            if state is not None and updated_at and state.get(
                "last_synthesized_at_chat_updated"
            ) == updated_at:
                return

            await self._synthesize(
                user_id=user_id,
                chat_id=chat_id,
                messages=messages,
                updated_at=updated_at,
                source=source,
            )
            self._last_synth_at[user_id] = time.monotonic()

    async def _synthesize(
        self,
        *,
        user_id: str,
        chat_id: str,
        messages: list[dict[str, Any]],
        updated_at: str,
        source: str,
    ) -> None:
        assert self._storage is not None and self._ai is not None
        existing = await self._fetch_user_memories(user_id)
        transcript = self._format_transcript(messages)

        existing_block = self._format_existing_for_prompt(existing)
        user_message = (
            f"User ID: {user_id}\n\n"
            f"Existing memories ({len(existing)}):\n{existing_block}\n\n"
            f"Transcript:\n{transcript}"
        )

        try:
            response = await self._ai.complete_one_shot(
                messages=[Message(role=MessageRole.USER, content=user_message)],
                system_prompt=self._synthesis_prompt,
                profile_name=self._ai_profile,
                tools_override=[],
            )
        except Exception as exc:
            logger.warning(
                "user_memory: AI call failed for %s/%s: %s",
                user_id,
                chat_id,
                exc,
            )
            return

        ops = self._parse_ops(response.message.content if response.message else "")
        if not ops:
            logger.info(
                "user_memory: no ops returned for %s/%s (source=%s)",
                user_id,
                chat_id,
                source,
            )
        else:
            await self._apply_ops(user_id, ops)

        # Mark this chat processed at its current updated_at watermark
        # so the sweep doesn't reprocess it until it sees new activity.
        await self._storage.put(
            _CHAT_STATE,
            chat_id,
            {
                "chat_id": chat_id,
                "user_id": user_id,
                "last_synthesized_at_chat_updated": updated_at,
                "last_synthesized_at": datetime.now(UTC).isoformat(),
                "last_source": source,
                "ops_count": len(ops),
            },
        )
        logger.info(
            "user_memory: synthesized %s/%s (source=%s, ops=%d)",
            user_id,
            chat_id,
            source,
            len(ops),
        )

    async def _fetch_user_memories(self, user_id: str) -> list[dict[str, Any]]:
        assert self._storage is not None
        return await self._storage.query(
            Query(
                collection=_USER_MEMORIES,
                filters=[Filter(field="user_id", op=FilterOp.EQ, value=user_id)],
            )
        )

    def _format_transcript(self, messages: list[dict[str, Any]]) -> str:
        """Render the message list into a compact role-tagged transcript.

        Tool-result rows and assistant rows that only carried tool calls
        are included as terse markers — they tell the synthesis model
        that work happened without ballooning the prompt.
        """
        lines: list[str] = []
        for m in messages:
            role = m.get("role", "")
            content = m.get("content", "")
            if isinstance(content, list):
                content = json.dumps(content)
            content = str(content).strip()
            if role == "user":
                lines.append(f"USER: {content}")
            elif role == "assistant":
                if content:
                    lines.append(f"ASSISTANT: {content}")
                elif m.get("tool_calls"):
                    names = [
                        str(tc.get("tool_name") or tc.get("name") or "?")
                        for tc in m["tool_calls"]
                        if isinstance(tc, dict)
                    ]
                    lines.append(
                        f"ASSISTANT: [called tools: {', '.join(names) or '?'}]"
                    )
            elif role == "tool_result":
                # The synthesis call only needs to know a tool returned
                # something; the actual result text is rarely about the
                # user. Include the first 200 chars in case it is.
                if content:
                    snippet = content[:200]
                    lines.append(f"TOOL_RESULT: {snippet}")
        return "\n".join(lines)

    def _format_existing_for_prompt(
        self, memories: list[dict[str, Any]]
    ) -> str:
        if not memories:
            return "(none)"
        lines: list[str] = []
        for m in memories:
            mid = m.get("_id") or m.get("memory_id") or "?"
            summary = m.get("summary", "")
            content = m.get("content", "")
            source = m.get("source", "user")
            lines.append(
                f"- [{mid}] (source={source}) {summary}\n    {content}"
            )
        return "\n".join(lines)

    def _parse_ops(self, raw: str) -> list[_Op]:
        """Parse the synthesis output. Tolerant of leading/trailing prose
        and accidental code fences — strict parsing would cause a single
        stray character to drop a whole synthesis."""
        if not raw:
            return []
        text = raw.strip()
        # Strip ``` fences if the model added them despite the instructions.
        if text.startswith("```"):
            text = text.strip("`")
            # Drop a leading "json" language tag if present.
            if text.lower().startswith("json"):
                text = text[4:].lstrip()
        # Trim to outermost JSON object.
        first = text.find("{")
        last = text.rfind("}")
        if first == -1 or last == -1 or last <= first:
            return []
        text = text[first : last + 1]
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return []
        ops_raw = payload.get("ops") if isinstance(payload, dict) else None
        if not isinstance(ops_raw, list):
            return []
        ops: list[_Op] = []
        for entry in ops_raw[:8]:  # hard ceiling on ops per call
            if not isinstance(entry, dict):
                continue
            op = str(entry.get("op", "")).lower()
            if op not in ("add", "update", "delete"):
                continue
            ops.append(
                _Op(
                    op=op,
                    memory_id=str(entry.get("memory_id") or ""),
                    summary=str(entry.get("summary") or "")[
                        : self._max_memory_chars
                    ],
                    content=str(entry.get("content") or "")[
                        : self._max_memory_chars
                    ],
                )
            )
        return ops

    async def _apply_ops(self, user_id: str, ops: list[_Op]) -> None:
        """Apply the parsed ops to ``user_memories``.

        Skips ops that would violate ownership (delete/update of
        another user's memory) or write empty content. Enforces the
        per-user cap by trimming the lowest-priority auto entries
        after applying the batch.
        """
        assert self._storage is not None
        now = datetime.now(UTC).isoformat()

        for op in ops:
            if op.op == "add":
                if not op.summary:
                    continue
                content = op.content or op.summary
                memory_id = f"memory_{uuid.uuid4().hex[:12]}"
                await self._storage.put(
                    _USER_MEMORIES,
                    memory_id,
                    {
                        "memory_id": memory_id,
                        "user_id": user_id,
                        "summary": op.summary,
                        "content": content,
                        "source": "auto",
                        "access_count": 0,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
            elif op.op == "update":
                if not op.memory_id:
                    continue
                record = await self._storage.get(_USER_MEMORIES, op.memory_id)
                if record is None or record.get("user_id") != user_id:
                    continue
                # Don't silently overwrite user-authored memories — the
                # synthesis prompt forbids it but the layer enforces it.
                if record.get("source") == "user":
                    continue
                if op.summary:
                    record["summary"] = op.summary
                if op.content:
                    record["content"] = op.content
                record["updated_at"] = now
                await self._storage.put(_USER_MEMORIES, op.memory_id, record)
            elif op.op == "delete":
                if not op.memory_id:
                    continue
                record = await self._storage.get(_USER_MEMORIES, op.memory_id)
                if record is None or record.get("user_id") != user_id:
                    continue
                if record.get("source") == "user":
                    continue
                await self._storage.delete(_USER_MEMORIES, op.memory_id)

        await self._enforce_cap(user_id)

    async def _enforce_cap(self, user_id: str) -> None:
        """Trim auto-source memories down to ``max_memories_per_user``.

        User-authored entries are never trimmed; if the cap is already
        exceeded by user entries alone, this is a no-op (the cap is a
        soft guard against runaway auto-capture, not a hard policy on
        user-authored data)."""
        assert self._storage is not None
        memories = await self._fetch_user_memories(user_id)
        if len(memories) <= self._max_memories_per_user:
            return
        auto = [m for m in memories if m.get("source") == "auto"]
        # Drop the oldest, least-accessed auto entries first.
        auto.sort(
            key=lambda m: (
                m.get("access_count", 0),
                m.get("updated_at", ""),
            )
        )
        excess = len(memories) - self._max_memories_per_user
        for victim in auto[:excess]:
            mid = victim.get("_id") or victim.get("memory_id")
            if mid:
                await self._storage.delete(_USER_MEMORIES, str(mid))
                logger.debug(
                    "user_memory: trimmed auto-memory %s for %s (cap=%d)",
                    mid,
                    user_id,
                    self._max_memories_per_user,
                )

    async def _user_self_opted_out(self, user_id: str) -> bool:
        """Read ``metadata.user_memory_opted_out`` from the user entity.

        Returns False (don't skip synthesis) if the user service isn't
        wired up or the user record can't be fetched — failing closed
        would silently disable the whole feature in tests/dev where the
        user backend is stubbed out.
        """
        if self._user_svc is None:
            return False
        try:
            user = await self._user_svc.backend.get_user(user_id)
        except Exception:
            return False
        if user is None:
            return False
        return bool((user.get("metadata") or {}).get("user_memory_opted_out"))

    # ── Public API for the SPA ──────────────────────────────────

    async def list_user_memories(self, user_id: str) -> list[dict[str, Any]]:
        """Return all memories owned by ``user_id`` for the Account page."""
        if self._storage is None or not user_id:
            return []
        return await self._fetch_user_memories(user_id)

    async def delete_user_memory(self, user_id: str, memory_id: str) -> bool:
        """Delete one memory the calling user owns. Returns True if a
        record was actually removed."""
        if self._storage is None or not user_id or not memory_id:
            return False
        record = await self._storage.get(_USER_MEMORIES, memory_id)
        if record is None or record.get("user_id") != user_id:
            return False
        await self._storage.delete(_USER_MEMORIES, memory_id)
        return True

    async def clear_user_memories(self, user_id: str) -> int:
        """Delete every memory owned by ``user_id``. Returns the count."""
        if self._storage is None or not user_id:
            return 0
        memories = await self._fetch_user_memories(user_id)
        count = 0
        for m in memories:
            mid = m.get("_id") or m.get("memory_id")
            if not mid:
                continue
            await self._storage.delete(_USER_MEMORIES, str(mid))
            count += 1
        return count

    async def get_self_opt_out(self, user_id: str) -> bool:
        """Whether the user has opted out via the Account page."""
        return await self._user_self_opted_out(user_id)

    async def set_self_opt_out(self, user_id: str, opted_out: bool) -> None:
        """Toggle the per-user opt-out flag on the user entity.

        Stored under ``metadata.user_memory_opted_out`` so it doesn't
        require schema changes to the user record. No-op when the user
        service isn't available."""
        if self._user_svc is None or not user_id:
            return
        user = await self._user_svc.backend.get_user(user_id)
        if user is None:
            return
        metadata = dict(user.get("metadata") or {})
        if opted_out:
            metadata["user_memory_opted_out"] = True
        else:
            metadata.pop("user_memory_opted_out", None)
        await self._user_svc.backend.update_user(
            user_id, {"metadata": metadata}
        )
