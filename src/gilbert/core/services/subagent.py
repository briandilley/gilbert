"""Subagent engine — spawn ephemeral, headless agents in a fresh context.

A subagent is a one-shot, autonomous run: a fresh conversation seeded with a
shared headless preamble + a *subagent type*'s system prompt, driven from the
type's self-contained definition (model + generation params, tool gating, round
budget) with a bounded round budget, returning its final message. It cannot ask
the user anything.

Subagent types are entity-backed (``subagent_types`` collection), admin-managed,
and seeded from a curated built-in catalog (``builtin_seed_list``). A type's
``execution_mode`` (sync vs background) and ``deliver_as`` (inline vs
report_file) generalize what used to be the special-cased ``deep_research``
flow: a background+report type detaches the run, writes a Markdown report file,
and delivers it as an attachment + link + notification into the parent
conversation.

First-party orchestration of the AI capability — lives in core, resolves
``ai_chat`` (``AIProvider``) via the resolver, and never hardcodes a
backend/model (the type owns those, admin-selected).
"""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from gilbert.core.subagents.types import SubagentType, builtin_seed_list
from gilbert.interfaces.ai import AIProvider, ConversationMessagePoster
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import ConfigParam, ConfigurationReader
from gilbert.interfaces.context import (
    get_current_conversation_id,
    get_current_user,
    set_current_conversation_id,
    set_workspace_conversation_id,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import Query, StorageBackend, StorageProvider
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)
from gilbert.interfaces.workspace import WorkspaceProvider
from gilbert.interfaces.ws import WsHandlerProvider

logger = logging.getLogger(__name__)

_TYPES_COLLECTION = "subagent_types"


@dataclass
class _Run:
    subagent_id: str
    agent_type: str
    query: str
    conversation_id: str
    parent_conversation_id: str | None
    user_id: str
    status: str  # running | completed | stopped | failed
    started_at: str
    stop_flag: list[bool] = field(default_factory=lambda: [False])
    task: Any = None

_DEFAULT_PREAMBLE = (
    "You are a subagent launched to complete a single task autonomously. You "
    "cannot ask the user questions or wait for input — make reasonable "
    "assumptions and proceed. Your final message is returned verbatim as the "
    "result to the agent that launched you; it is not shown to the user "
    "directly. Be thorough, then stop."
)


class SubagentService(Service, WsHandlerProvider):
    """Engine that runs a single ephemeral subagent and returns its result."""

    def __init__(self) -> None:
        self._enabled = True
        self._ai: AIProvider | None = None
        self._workspace: WorkspaceProvider | None = None
        # Registry of active/recent background runs, keyed by subagent_id.
        # Holds a strong reference to each task so the event loop can't
        # GC a long-running agent mid-flight.
        self._runs: dict[str, _Run] = {}
        # Strong refs to detached run tasks (the event loop only weak-refs them).
        # The registry's _Run.task also holds one; this set is the GC backstop
        # and covers the window before a run's _Run exists.
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._notifications: Any = None
        self._preamble = _DEFAULT_PREAMBLE
        # Entity-backed type catalog, loaded in start(). Falls back to the
        # in-memory seed list when no storage is available (e.g. unit tests
        # that drive the engine directly).
        self._types: dict[str, SubagentType] = {t.id: t for t in builtin_seed_list()}
        self._storage: StorageBackend | None = None
        self._resolver: ServiceResolver | None = None

    # --- Service ---

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="subagent",
            capabilities=frozenset({"subagent", "ai_tools", "ws_handlers"}),
            requires=frozenset({"ai_chat"}),
            toggleable=True,
            toggle_description=(
                "Let the AI spawn ephemeral subagents to work on focused tasks in a fresh context."
            ),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver
        # Toggleable service: determine ``self._enabled`` from config HERE.
        # ``ServiceManager.restart_service`` resets ``_enabled`` to False before
        # calling start() (so a disabled service can't carry a stale True) and
        # relies on start() to restore it. Without this, toggling the service on
        # triggers a restart that leaves it stuck disabled — its tools silently
        # vanish.
        config_svc = resolver.get_capability("configuration")
        if isinstance(config_svc, ConfigurationReader):
            section = config_svc.get_section_safe("subagent")
            if not section.get("enabled", True):
                self._enabled = False
                logger.info("Subagent service disabled")
                return
        # Resolve storage for the entity-backed type catalog. Optional so the
        # engine still runs (with the in-memory seed list) where storage isn't
        # wired — but required for admin edits to persist.
        storage = resolver.get_capability("entity_storage")
        self._storage = storage.backend if isinstance(storage, StorageProvider) else None
        await self._load_types()
        ai = resolver.get_capability("ai_chat")
        if ai is not None:
            if not isinstance(ai, AIProvider):
                raise RuntimeError("ai_chat capability does not implement AIProvider")
            self._ai = ai
        self._enabled = True
        ws = resolver.get_capability("workspace")
        self._workspace = ws if isinstance(ws, WorkspaceProvider) else None
        self._notifications = resolver.get_capability("notifications")
        logger.info("Subagent service started")

    # --- Configurable ---

    @property
    def config_namespace(self) -> str:
        return "subagent"

    @property
    def config_category(self) -> str:
        return "Intelligence"

    def config_params(self) -> list[ConfigParam]:
        # Per-type system prompts live on the type entity now (managed at
        # /security/subagents), not as config params. Only the shared, engine-
        # level config remains here.
        return [
            ConfigParam(
                key="enabled",
                type=ToolParameterType.BOOLEAN,
                description="Allow the AI to spawn subagents.",
                default=True,
            ),
            ConfigParam(
                key="preamble",
                type=ToolParameterType.STRING,
                description=(
                    "Shared headless preamble prepended to every subagent's "
                    "system prompt. Encodes the autonomy / no-user-feedback "
                    "contract."
                ),
                default=_DEFAULT_PREAMBLE,
                multiline=True,
                ai_prompt=True,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        # Use ``get(key, default)`` (not ``or default``) so an operator who
        # deliberately blanks the preamble gets the empty value, never a silent
        # revert to the bundled constant (per the AI-prompt rule).
        self._enabled = bool(config.get("enabled", True))
        self._preamble = str(config.get("preamble", _DEFAULT_PREAMBLE))

    # --- type store ---

    @staticmethod
    def _type_to_dict(t: SubagentType) -> dict[str, Any]:
        return dataclasses.asdict(t)

    @staticmethod
    def _type_from_dict(d: dict[str, Any]) -> SubagentType:
        fields = {f.name for f in dataclasses.fields(SubagentType)}
        return SubagentType(**{k: v for k, v in d.items() if k in fields})

    async def _load_types(self) -> None:
        """Seed missing built-ins (preserving edits), then load all into memory."""
        if self._storage is None:
            self._types = {t.id: t for t in builtin_seed_list()}
            return
        for seed in builtin_seed_list():
            existing = await self._storage.get(_TYPES_COLLECTION, seed.id)
            if existing is None:
                await self._storage.put(_TYPES_COLLECTION, seed.id, self._type_to_dict(seed))
                logger.info("Seeded built-in subagent type '%s'", seed.id)
        await self._refresh_types()

    async def _refresh_types(self) -> None:
        if self._storage is None:
            return
        rows = await self._storage.query(Query(collection=_TYPES_COLLECTION))
        self._types = {}
        for r in rows:
            tid = r.get("id") or r.get("_id")
            if tid:
                self._types[tid] = self._type_from_dict({**r, "id": tid})

    def list_types(self) -> list[SubagentType]:
        return sorted(self._types.values(), key=lambda t: t.name)

    def get_type(self, type_id: str) -> SubagentType | None:
        return self._types.get(type_id)

    async def save_type(self, t: SubagentType) -> None:
        if self._storage is not None:
            await self._storage.put(_TYPES_COLLECTION, t.id, self._type_to_dict(t))
        self._types[t.id] = t

    async def delete_type(self, type_id: str) -> bool:
        t = self._types.get(type_id)
        if t is None or t.built_in:
            return False
        if self._storage is not None:
            await self._storage.delete(_TYPES_COLLECTION, type_id)
        self._types.pop(type_id, None)
        return True

    async def reset_type(self, type_id: str) -> bool:
        """Restore a built-in type to its shipped seed values."""
        seed = next((s for s in builtin_seed_list() if s.id == type_id), None)
        if seed is None:
            return False
        await self.save_type(seed)
        return True

    # --- ToolProvider ---

    @property
    def tool_provider_name(self) -> str:
        return "subagent"

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        types = [t for t in self.list_types() if t.enabled]
        type_lines = "\n".join(f"- {t.name} ({t.id}): {t.description}" for t in types)
        return [
            ToolDefinition(
                name="spawn_agent",
                description=(
                    "Launch a subagent to work on a focused task autonomously in "
                    "a fresh context, then return its final report. The subagent "
                    "cannot ask you or the user questions — give it a complete, "
                    "self-contained task. Some agent types run in the background "
                    "and deliver a written report into the chat when done; the "
                    "rest answer inline. Available agent types:\n" + type_lines
                ),
                parameters=[
                    ToolParameter(
                        name="agent_type",
                        type=ToolParameterType.STRING,
                        description="Which agent type to launch.",
                        enum=[t.id for t in types],
                    ),
                    ToolParameter(
                        name="prompt",
                        type=ToolParameterType.STRING,
                        description=(
                            "The complete task for the subagent. Include all "
                            "context it needs; it has a fresh context and cannot "
                            "ask follow-up questions."
                        ),
                    ),
                    ToolParameter(
                        name="model",
                        type=ToolParameterType.STRING,
                        description=(
                            "Optional model override for this run (e.g. a "
                            "stronger model for a hard task). Leave empty to use "
                            "the agent type's configured model."
                        ),
                        required=False,
                    ),
                ],
                required_role="user",
                # interactive=True keeps spawn_agent out of headless subagent
                # runs, so subagents can't spawn more subagents (no nesting).
                interactive=True,
                # Conservative for v1: no parallel fan-out of (expensive)
                # sub-chats until a per-turn spawn/cost cap exists.
                parallel_safe=False,
            ),
            ToolDefinition(
                name="check_research",
                description=(
                    "List your recent and in-progress background agent runs and "
                    "their status (running/completed/stopped/failed) so you can "
                    "report progress or point at a finished report."
                ),
                parameters=[],
                slash_command="research-status",
                slash_help="Show running/recent background agents: /research-status",
                required_role="user",
                interactive=True,
            ),
        ]

    def _web_search_available(self) -> bool:
        """Whether a web-search backend is enabled (some agent types need one)."""
        if self._resolver is None:
            return False
        return self._resolver.get_capability("websearch") is not None

    # --- WsHandlerProvider ---

    def get_ws_handlers(self) -> dict[str, Any]:
        return {
            "subagent.stop": self._ws_stop_subagent,
            "subagent.list": self._ws_list_subagents,
            "subagent.types.list": self._ws_types_list,
            "subagent.types.save": self._ws_types_save,
            "subagent.types.delete": self._ws_types_delete,
            "subagent.types.reset": self._ws_types_reset,
        }

    async def _ws_stop_subagent(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        subagent_id = str(frame.get("subagent_id") or "")
        ok = self.stop_subagent(subagent_id, getattr(conn, "user_id", ""))
        return {"type": "subagent.stop.result", "ref": frame.get("id"), "ok": ok}

    async def _ws_list_subagents(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        runs = self.list_active_for_conversation(
            str(frame.get("conversation_id") or ""), getattr(conn, "user_id", "")
        )
        return {"type": "subagent.list.result", "ref": frame.get("id"), "runs": runs}

    # --- admin type CRUD (mirrors roles.profile.*) ---

    @staticmethod
    def _is_admin(conn: Any) -> bool:
        return "admin" in tuple(getattr(conn, "roles", ()) or ())

    @staticmethod
    def _forbidden(frame: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "gilbert.error",
            "ref": frame.get("id"),
            "error": "Admin role required",
            "code": 403,
        }

    def _all_tool_names(self) -> list[str]:
        """Every AI tool name any started ToolProvider exposes — backs the
        admin form's include/exclude checkbox list (mirrors profiles)."""
        from gilbert.interfaces.tools import ToolProvider

        names: set[str] = set()
        if self._resolver is not None:
            for svc in self._resolver.get_all("ai_tools"):
                if isinstance(svc, ToolProvider):
                    for t in svc.get_tools():
                        names.add(t.name)
        return sorted(names)

    async def _ws_types_list(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        if not self._is_admin(conn):
            return self._forbidden(frame)
        return {
            "type": "subagent.types.list.result",
            "ref": frame.get("id"),
            "types": [self._type_to_dict(t) for t in self.list_types()],
            "all_tool_names": self._all_tool_names(),
        }

    async def _ws_types_save(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        if not self._is_admin(conn):
            return self._forbidden(frame)
        raw = frame.get("type")
        if not isinstance(raw, dict) or not raw.get("id") or not raw.get("name"):
            return {
                "type": "gilbert.error",
                "ref": frame.get("id"),
                "error": "type requires at least 'id' and 'name'",
                "code": 400,
            }
        # Preserve the built_in flag from the existing type so an admin edit
        # can't accidentally un-protect (or fake-protect) a type.
        existing = self.get_type(str(raw["id"]))
        t = self._type_from_dict(raw)
        if existing is not None:
            t.built_in = existing.built_in
        await self.save_type(t)
        return {"type": "subagent.types.save.result", "ref": frame.get("id"), "ok": True}

    async def _ws_types_delete(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        if not self._is_admin(conn):
            return self._forbidden(frame)
        ok = await self.delete_type(str(frame.get("type_id") or ""))
        return {"type": "subagent.types.delete.result", "ref": frame.get("id"), "ok": ok}

    async def _ws_types_reset(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        if not self._is_admin(conn):
            return self._forbidden(frame)
        ok = await self.reset_type(str(frame.get("type_id") or ""))
        return {"type": "subagent.types.reset.result", "ref": frame.get("id"), "ok": ok}

    # --- ToolProvider ---

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "spawn_agent":
            agent_type = str(arguments.get("agent_type") or "")
            prompt = str(arguments.get("prompt") or "")
            model = str(arguments.get("model") or "")
            if not agent_type or not prompt:
                raise ValueError("spawn_agent requires 'agent_type' and 'prompt'")
            t = self.get_type(agent_type)
            if t is None:
                raise ValueError(f"Unknown agent type: {agent_type}")
            # Inherit the caller's full identity for the subagent's RBAC.
            caller = get_current_user()
            if t.execution_mode == "background":
                parent_conv = get_current_conversation_id()
                self._run_in_background(
                    self._run_agent_background(t, prompt, parent_conv, caller, model)
                )
                return (
                    f"\U0001f50d Running {t.name} on \"{prompt}\" in the background "
                    "— I'll post the report here when it's ready. You can keep "
                    "chatting."
                )
            return await self.spawn(t.id, prompt, user_ctx=caller, model_override=model)
        if name == "check_research":
            user = get_current_user()
            runs = self.list_runs(user.user_id)
            if not runs:
                return "No background agent runs found."
            lines = [
                f"- [{r['status']}] {r['agent_type']}: \"{r['query']}\" (started {r['started_at']})"
                for r in runs
            ]
            return "Background agent runs:\n" + "\n".join(lines)
        raise KeyError(f"Unknown tool: {name}")

    # --- run registry ---

    _RUN_CAP = 20

    def _register_run(self, run: _Run) -> None:
        self._runs[run.subagent_id] = run
        # Prune oldest finished runs beyond the cap.
        if len(self._runs) > self._RUN_CAP:
            finished = [r for r in self._runs.values() if r.status != "running"]
            finished.sort(key=lambda r: r.started_at)
            for r in finished[: len(self._runs) - self._RUN_CAP]:
                self._runs.pop(r.subagent_id, None)

    def list_runs(self, user_id: str) -> list[dict[str, Any]]:
        """Recent/active runs for a user — backs the check_research tool + UI."""
        return [
            {
                "subagent_id": r.subagent_id,
                "agent_type": r.agent_type,
                "query": r.query,
                "conversation_id": r.conversation_id,
                "status": r.status,
                "started_at": r.started_at,
            }
            for r in self._runs.values()
            if r.user_id == user_id
        ]

    def list_active_for_conversation(
        self, parent_conversation_id: str, user_id: str
    ) -> list[dict[str, Any]]:
        """Running subagent runs whose parent is ``parent_conversation_id``.

        Returns a list of dicts suitable for the ``subagent.list.result`` WS
        frame. Filters to ``status == "running"`` so completed/stopped/failed
        runs are not re-seeded as cards on the frontend.
        """
        return [
            {
                "subagent_id": r.subagent_id,
                "agent_type": r.agent_type,
                "query": r.query,
                "conversation_id": r.conversation_id,
                "status": r.status,
            }
            for r in self._runs.values()
            if r.user_id == user_id
            and r.status == "running"
            and r.parent_conversation_id == parent_conversation_id
        ]

    def stop_subagent(self, subagent_id: str, requester_id: str) -> bool:
        """Request a graceful stop of a running subagent. Returns True if the
        stop was applied (the run exists, is running, and is owned by the
        requester). No-op (False) for unknown/finished/foreign runs."""
        run = self._runs.get(subagent_id)
        if run is None or run.status != "running" or run.user_id != requester_id:
            return False
        run.stop_flag[0] = True
        logger.info("Subagent %s stop requested by %s", subagent_id, requester_id)
        return True

    # --- background helpers ---

    def _run_in_background(self, coro: Any) -> None:
        """Detach a coroutine as a tracked task, preserving request context.

        Holds a strong reference in the run registry so the event loop's
        weak ref can't let a long run be garbage-collected mid-flight.
        """
        task = asyncio.create_task(coro, context=contextvars.copy_context())
        # Keep a small backup set so tasks not yet tied to a _Run entry
        # (e.g. in tests that mock _run_in_background) still stay alive.
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_agent_background(
        self,
        t: SubagentType,
        query: str,
        parent_conversation_id: str | None,
        user_ctx: UserContext | None,
        model_override: str = "",
    ) -> None:
        """Run a background subagent off the parent turn and deliver its result
        into the parent conversation per the type's ``deliver_as``. Never raises
        — a detached task's failure must be delivered, not lost."""
        # Scope workspace writes to the PARENT conversation so the report (and
        # any media the agent saves) is linkable from the user's chat. Also pin
        # the conversation-id ContextVar so spawn()'s lifecycle events route to
        # the parent conversation (the inner chat() sets it to the ephemeral
        # subagent conversation otherwise).
        if parent_conversation_id:
            set_workspace_conversation_id(parent_conversation_id)
            set_current_conversation_id(parent_conversation_id)

        subagent_id = uuid.uuid4().hex
        sub_conv = uuid.uuid4().hex
        title = f"{t.name}: {query}"[:80]
        run = _Run(
            subagent_id=subagent_id,
            agent_type=t.id,
            query=query,
            conversation_id=sub_conv,
            parent_conversation_id=parent_conversation_id,
            user_id=user_ctx.user_id if user_ctx else "system",
            status="running",
            started_at=datetime.now(UTC).isoformat(),
        )
        self._register_run(run)
        # Record this detached task on the run so the registry is a strong-ref
        # holder too (the _background_tasks set is the primary GC anchor).
        run.task = asyncio.current_task()
        # Create the child conversation row NOW (before spawn emits
        # subagent_started) so it lists in the sidebar and is watchable while
        # the run is still in progress — not only once it finishes.
        if isinstance(self._ai, ConversationMessagePoster) and user_ctx is not None:
            try:
                await self._ai.ensure_conversation(
                    sub_conv,
                    user_ctx,
                    source="subagent",
                    parent_conversation_id=parent_conversation_id or "",
                    title=title,
                )
            except Exception:
                logger.exception("ensure_conversation failed for subagent %s", subagent_id)
        try:
            report = await self.spawn(
                t.id,
                query,
                user_ctx=user_ctx,
                conversation_id=sub_conv,
                subagent_id=subagent_id,
                should_stop=lambda: run.stop_flag[0],
                model_override=model_override,
                conversation_parent_id=parent_conversation_id or "",
                conversation_title=title,
            )
            run.status = "stopped" if run.stop_flag[0] else "completed"
            stopped = run.stop_flag[0]
            verb = (
                f"{t.name} stopped early — here's what it found so far."
                if stopped
                else f"**{t.name} complete.**"
            )
            from gilbert.interfaces.attachments import FileAttachment
            from gilbert.interfaces.notifications import NotificationProvider, NotificationUrgency

            attachments: list[FileAttachment] = []
            rel_path: str | None = None
            if t.deliver_as == "report_file":
                rel_path = await self._write_report(
                    parent_conversation_id,
                    user_ctx.user_id if user_ctx else "system",
                    report,
                )
                if rel_path and parent_conversation_id:
                    attachments = [
                        FileAttachment(
                            kind="text",
                            name=rel_path.split("/")[-1],
                            media_type="text/markdown",
                            workspace_skill="workspace",
                            workspace_path=rel_path,
                            workspace_conv=parent_conversation_id,
                        )
                    ]
                    url = f"/api/chat/download/{parent_conversation_id}/{rel_path}"
                    lead = report.strip().split("\n\n", 1)[0][:400]
                    message = f"{verb} [Open the report]({url})\n\n{lead}"
                else:
                    # No workspace — degrade to delivering the report inline.
                    message = f"{verb}\n\n{report}"
            else:
                # inline delivery: post the full report into the parent chat.
                message = f"{verb}\n\n{report}"
            await self._deliver(parent_conversation_id, message, attachments)
            if isinstance(self._notifications, NotificationProvider) and user_ctx:
                try:
                    await self._notifications.notify_user(
                        user_id=user_ctx.user_id,
                        message=f"{t.name} {run.status}: {query}",
                        urgency=NotificationUrgency.NORMAL,
                        source="subagent",
                        source_ref={
                            "conversation_id": parent_conversation_id,
                            "subagent_id": run.subagent_id,
                            "report_path": rel_path or "",
                        },
                    )
                except Exception:
                    logger.exception("subagent notification failed")
        except Exception as exc:  # noqa: BLE001 — deliver, don't crash
            run.status = "failed"
            logger.exception("Background subagent run failed")
            await self._publish_event(
                "chat.stream.subagent_failed",
                {
                    "conversation_id": parent_conversation_id,
                    "subagent_id": subagent_id,
                    "agent_type": t.id,
                    "reason": str(exc),
                    "visible_to": [user_ctx.user_id] if user_ctx and user_ctx.user_id else None,
                },
            )
            await self._deliver(
                parent_conversation_id, f"{t.name} failed: {exc}"
            )

    async def _write_report(
        self, conversation_id: str | None, user_id: str, content: str
    ) -> str | None:
        """Write the report markdown to outputs/ in the conversation workspace.
        Returns the rel_path, or None when no workspace is available."""
        if self._workspace is None or not conversation_id:
            return None
        filename = f"research-{uuid.uuid4().hex[:8]}.md"
        rel_path = f"outputs/{filename}"
        out_dir = self._workspace.get_output_dir(user_id, conversation_id)
        target = out_dir / filename
        target.write_text(content, encoding="utf-8")
        await self._workspace.register_file(
            conversation_id=conversation_id,
            user_id=user_id,
            category="output",
            filename=filename,
            rel_path=rel_path,
            media_type="text/markdown",
            size=len(content.encode("utf-8")),
            created_by="ai",
            description="Subagent report",
        )
        return rel_path

    async def _deliver(
        self,
        conversation_id: str | None,
        content: str,
        attachments: Any = None,
    ) -> None:
        """Post the result into the parent conversation (best-effort).

        Swallows delivery errors: a failed post must not escape the detached
        task (the ``_run_agent_background`` "never raises" contract), and it's
        also called from the failure path where re-raising would be worse.
        """
        if not conversation_id or not isinstance(self._ai, ConversationMessagePoster):
            return
        try:
            await self._ai.append_assistant_message(conversation_id, content, attachments)
        except Exception:
            logger.exception("Failed to deliver subagent message to %s", conversation_id)

    # --- engine ---

    async def _publish_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Best-effort publish to the event bus for live UI. No-op without one."""
        if self._resolver is None:
            return
        bus_svc = self._resolver.get_capability("event_bus")
        if bus_svc is None:
            return
        from gilbert.interfaces.events import Event, EventBusProvider

        if isinstance(bus_svc, EventBusProvider):
            await bus_svc.bus.publish(Event(event_type=event_type, data=data, source="subagent"))

    def _event_routing(self) -> dict[str, Any]:
        """Parent conversation + audience for subagent lifecycle events.

        Read from the chat-turn ContextVars: a subagent runs inside the
        spawning tool call, so these point at the PARENT chat. ``visible_to``
        scopes the event to the caller (the WS bridge applies it for
        ``chat.stream.*`` events). Both may be absent (direct, non-chat call)
        — then the event simply isn't routed to any conversation/user.
        """
        user = get_current_user()
        return {
            "conversation_id": get_current_conversation_id(),
            "visible_to": [user.user_id] if user.user_id else None,
        }

    async def spawn(
        self,
        agent_type: str,
        prompt: str,
        user_ctx: UserContext | None = None,
        *,
        conversation_id: str | None = None,
        subagent_id: str | None = None,
        should_stop: Any = None,
        model_override: str = "",
        backend_override: str = "",
        conversation_parent_id: str = "",
        conversation_title: str = "",
    ) -> str:
        """Run one ephemeral subagent of ``agent_type`` on ``prompt``.

        Drives a fresh chat turn (no parent history) with the shared preamble +
        the type's prompt, on the type's model + generation params + tool gating
        and round budget, inheriting the caller's identity for RBAC. Returns the
        subagent's final message text. The subagent cannot ask the user
        anything: the headless preamble plus ``headless=True`` on the chat call
        exclude all interactive tools — including ``spawn_agent`` itself, so a
        subagent can't spawn more subagents (no nesting).

        ``model_override`` / ``backend_override`` beat the type's configured
        model/backend for this run (e.g. a per-spawn model choice from the
        tool). ``conversation_id`` is the pre-allocated conversation id for this
        subagent run; if given, the subagent's messages are persisted under that
        id (watchable from the UI). ``should_stop`` is an optional callable
        ``() -> bool`` that the AI engine checks between rounds.
        """
        if not self._enabled:
            raise RuntimeError("subagent service is disabled")
        if self._ai is None:
            raise RuntimeError("subagent service not started")
        t = self.get_type(agent_type)
        if t is None:
            raise ValueError(f"Unknown agent type: {agent_type}")

        system_prompt = f"{self._preamble}\n\n{t.system_prompt}"
        tool_filter = (t.tool_mode, list(t.tools))

        subagent_id = subagent_id or uuid.uuid4().hex
        routing = self._event_routing()
        await self._publish_event(
            "chat.stream.subagent_started",
            {
                **routing,
                "subagent_id": subagent_id,
                "agent_type": t.id,
                "subagent_conversation_id": conversation_id,
                "query": prompt,
            },
        )
        try:
            result = await self._ai.chat(
                user_message=prompt,
                conversation_id=conversation_id,   # pre-allocated (watchable) or None
                user_ctx=user_ctx,
                system_prompt=system_prompt,
                ai_call=f"subagent.{t.id}",
                model=model_override or t.model,
                backend_override=backend_override or t.backend,
                temperature=t.temperature,
                tool_filter=tool_filter,
                max_tool_rounds=t.max_rounds,
                headless=True,
                # Tag the ephemeral subagent conversation so it's visible in
                # the user's chat list as a child of the parent conversation.
                source="subagent",
                should_stop_callback=should_stop,
                conversation_parent_id=conversation_parent_id,
                conversation_title=conversation_title,
            )
        except Exception as exc:
            await self._publish_event(
                "chat.stream.subagent_failed",
                {**routing, "subagent_id": subagent_id, "agent_type": t.id, "reason": str(exc)},
            )
            raise
        was_stopped = should_stop is not None and bool(should_stop())
        report = result.response_text
        # Budget-exhaustion guard: if the agent used up its rounds mid-tool-use
        # and never wrote a final answer (empty/near-empty text), force one
        # synthesis turn so we never return an empty "report". Only possible
        # when the run has a persisted conversation to reload its findings from.
        if not was_stopped and len(report.strip()) < 80 and conversation_id:
            try:
                synth = await self._ai.chat(
                    user_message=(
                        "You've reached your step limit. Do NOT call any "
                        "more tools. Using everything you have already gathered in "
                        "this conversation, write your COMPLETE final answer now — "
                        "a thorough, well-structured Markdown report that directly "
                        "answers the original task, with citations where relevant."
                    ),
                    conversation_id=conversation_id,
                    user_ctx=user_ctx,
                    system_prompt=system_prompt,
                    ai_call=f"subagent.{t.id}.synthesis",
                    model=model_override or t.model,
                    backend_override=backend_override or t.backend,
                    temperature=t.temperature,
                    tool_filter=tool_filter,
                    max_tool_rounds=2,
                    headless=True,
                    source="subagent",
                    conversation_parent_id=conversation_parent_id,
                    conversation_title=conversation_title,
                )
                if synth.response_text.strip():
                    report = synth.response_text
            except Exception:
                logger.exception("subagent synthesis fallback failed")
        # A graceful stop returns normally with the partial — emit the distinct
        # "stopped" terminal event so the UI can label it (both are terminal).
        await self._publish_event(
            "chat.stream.subagent_stopped" if was_stopped else "chat.stream.subagent_completed",
            {**routing, "subagent_id": subagent_id, "agent_type": t.id},
        )
        return report
