"""Phone-call service — places outbound calls + drives the AI conversation.

Owns the call lifecycle:

1. ``make_phone_call`` AI tool (or the ``phone.call.test`` WS handler)
   spawns a new call.
2. ``PhoneCallService.start_call`` instantiates a ``TelephonyBackend`` session,
   creates a persistence record in the ``phone_calls`` collection, and kicks
   off the brain coroutine.
3. The brain (``_run_call``) wires inbound audio → STT → LLM → TTS → outbound
   audio, with barge-in cancellation. Transcript turns + status changes
   stream live via the event bus to whichever browser tab is watching.
4. On hang-up (either side) the brain finalizes the call record with a
   structured outcome summary and emits ``phone.call.ended``.

This module is deliberately the only place that knows about the conversation
loop. Telephony backends know nothing about LLMs; the AI service knows
nothing about audio. Each layer is replaceable.

The brain is fail-loud: any unhandled exception inside the loop logs the
traceback, hangs up the call, and marks the record as ``failed``. We never
leave a zombie carrier session.
"""

from __future__ import annotations

import asyncio
import audioop
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from gilbert.interfaces.ai import (
    AIResponse,
    AISamplingProvider,
    ConversationMessagePoster,
    Message,
    MessageRole,
)
from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.events import Event, EventBusProvider
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import StorageProvider
from gilbert.interfaces.telephony import (
    CallBrief,
    CallErrorEvent,
    CallSession,
    CallStatus,
    CallStatusEvent,
    DtmfEvent,
    TelephonyBackend,
    TranscriptTurn,
)
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)
from gilbert.interfaces.transcription import (
    AudioEncoding,
    FinalTranscript,
    SpeechEnded,
    SpeechStarted,
    StreamConfig,
    StreamingTranscriber,
)
from gilbert.interfaces.transcription import (
    AudioFormat as TranscriptionAudioFormat,
)
from gilbert.interfaces.tts import (
    AudioFormat as TTSAudioFormat,
)
from gilbert.interfaces.tts import (
    SynthesisRequest,
    TTSProvider,
)
from gilbert.interfaces.ui import ToolOutput

logger = logging.getLogger(__name__)


_COLLECTION = "phone_calls"

# Catch-all hard cap so a runaway call can't burn through carrier credit.
# Stops the brain (which then hangs up) once exceeded. Configurable in
# ``/settings`` via ``phone_call.max_call_seconds``.
_DEFAULT_MAX_CALL_SECONDS = 900  # 15 minutes

# How long of remote silence after the brain finishes speaking before we
# proactively prompt the remote ("are you still there?"). Voicemail
# detection sits on top of this — see ``_VOICEMAIL_SILENCE_SECONDS``.
_REMOTE_SILENCE_NUDGE_SECONDS = 12.0

# Sustained silence right after connect = probably hit a voicemail.
# Heuristic: if no inbound speech in this window after the brain
# finishes its greeting, switch to one-shot voicemail mode.
_VOICEMAIL_SILENCE_SECONDS = 6.0


# ── Persisted call-record shape (entity in ``phone_calls`` collection) ──


@dataclass
class _CallRecord:
    """In-memory view of a call entity. Persisted via ``_save_record``.

    The frontend reads this shape from the ``phone.call.get`` RPC. Keep
    field names stable — the SPA depends on them.
    """

    call_id: str
    user_id: str
    to_number: str
    from_number: str
    callback_number: str
    brief: str
    status: str  # CallStatus value (kept as raw str for storage simplicity)
    webhook_token: str
    # The conversation_id the user was in when they triggered the call.
    # Empty for calls placed outside a chat context (e.g. the
    # ``phone.call.test`` Settings button). When set, the brain posts
    # an "(call ended)" follow-up message into this conversation when
    # the call wraps so the next AI turn sees the outcome in history
    # instead of hallucinating that the call is still active.
    originating_conversation_id: str = ""
    started_at: str = ""
    ended_at: str = ""
    duration_seconds: float = 0.0
    transcript: list[dict[str, Any]] = field(default_factory=list)
    outcome: dict[str, Any] = field(default_factory=dict)
    failure_reason: str = ""
    interventions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "to_number": self.to_number,
            "from_number": self.from_number,
            "callback_number": self.callback_number,
            "brief": self.brief,
            "status": self.status,
            "webhook_token": self.webhook_token,
            "originating_conversation_id": self.originating_conversation_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": self.duration_seconds,
            "transcript": self.transcript,
            "outcome": self.outcome,
            "failure_reason": self.failure_reason,
            "interventions": self.interventions,
        }


# ── Brain runtime state — lives only inside ``_run_call`` ──────────────


@dataclass
class _Speaking:
    """Tracks whether the brain is currently outputting TTS.

    Barge-in: when the remote starts speaking while we're talking,
    flip ``cancelled`` and the TTS writer drops the rest of its buffer.
    The brain then resumes listening; the partial speech that was already
    out the door is the cost of doing business.
    """

    active: bool = False
    cancelled: bool = False
    # Set by the brain on each "we want to speak" task so a stale cancel
    # from a previous utterance doesn't poison the next one. Compared
    # against the writer's local snapshot.
    generation: int = 0


# ── Active-call tracker (one per user, enforces the concurrency cap) ───


@dataclass
class _ActiveCall:
    record: _CallRecord
    session: CallSession
    task: asyncio.Task[None]
    interventions_queue: asyncio.Queue[str]  # text the user injects mid-call


# ── The service ────────────────────────────────────────────────────────


class PhoneCallService(Service):
    """Outbound phone calls driven by the AI orchestrator.

    Capabilities provided: ``phone_calls``, ``ai_tools``, ``ws_handlers``.
    Capabilities consumed: ``text_to_speech``, ``speech_to_text``,
    ``ai_chat``, ``entity_storage``, ``event_bus``.

    Concurrency: at most one active call per user (enforced in
    ``start_call``). Trying to make a second call returns a 409-style
    error; the user must cancel the active one first.
    """

    # Slash namespace used by Gilbert's slash command router. Short and
    # human-friendly so ``/call …`` is the user-facing shape.
    slash_namespace = "call"

    def __init__(self) -> None:
        self._backend: TelephonyBackend | None = None
        self._backend_name: str = "telnyx"
        self._config: dict[str, object] = {}
        self._enabled: bool = False
        self._from_number: str = ""
        self._max_call_seconds: int = _DEFAULT_MAX_CALL_SECONDS
        self._opening_disclosure_prompt: str = _DEFAULT_OPENING_DISCLOSURE
        self._call_system_prompt: str = _DEFAULT_CALL_SYSTEM_PROMPT

        self._resolver: ServiceResolver | None = None
        self._storage: StorageProvider | None = None
        self._tts: TTSProvider | None = None
        self._transcription: StreamingTranscriber | None = None
        self._ai: AISamplingProvider | None = None
        # Optional. Set when the AI service satisfies
        # ``ConversationMessagePoster`` — used to post a "call ended"
        # message back into the originating chat conversation so the
        # next AI turn doesn't hallucinate a still-active call.
        self._message_poster: ConversationMessagePoster | None = None
        # Bus-subscription cleanup. Tracked so ``stop`` can detach.
        self._unsubscribe_ended: Any = None

        # user_id -> active call (one slot, hard cap of 1 per user)
        self._active: dict[str, _ActiveCall] = {}

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="phone_call",
            capabilities=frozenset({"phone_calls", "ai_tools", "ws_handlers"}),
            requires=frozenset(
                {
                    "entity_storage",
                    "event_bus",
                    "ai_chat",
                    "text_to_speech",
                    "speech_to_text",
                }
            ),
            optional=frozenset({"configuration"}),
            toggleable=True,
            toggle_description="Outbound phone calls (Telnyx).",
        )

    # --- Lifecycle ---------------------------------------------------

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver

        storage = resolver.get_capability("entity_storage")
        if isinstance(storage, StorageProvider):
            self._storage = storage
        ai = resolver.get_capability("ai_chat")
        if isinstance(ai, AISamplingProvider):
            self._ai = ai
        # Same service, narrower protocol — used at call end to post a
        # follow-up message into the conversation that triggered the
        # call. AIService satisfies both protocols simultaneously.
        if isinstance(ai, ConversationMessagePoster):
            self._message_poster = ai
        # ``TTSProvider`` / ``StreamingTranscriber`` are duck-typed in
        # via the service-level protocols — the service objects already
        # satisfy them.
        tts_svc = resolver.get_capability("text_to_speech")
        if isinstance(tts_svc, TTSProvider):
            self._tts = tts_svc
        st_svc = resolver.get_capability("speech_to_text")
        if isinstance(st_svc, StreamingTranscriber):
            self._transcription = st_svc

        config_svc = resolver.get_capability("configuration")
        section: dict[str, Any] = {}
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                section = config_svc.get_section(self.config_namespace)

        if not section.get("enabled", False):
            logger.info("Phone call service disabled")
            return

        self._enabled = True
        await self._apply_config(section)

        if self._backend is None:
            logger.warning(
                "Phone call service enabled but no telephony backend configured "
                "(set phone_call.backend in /settings)"
            )
            return
        # Subscribe to our own ``phone.call.ended`` bus events to post
        # a follow-up assistant message into the originating chat —
        # this is what stops the next AI turn from hallucinating that
        # the call is still active when the user types a follow-up
        # question.
        bus_svc = resolver.get_capability("event_bus")
        if isinstance(bus_svc, EventBusProvider):
            self._unsubscribe_ended = bus_svc.bus.subscribe(
                "phone.call.ended", self._on_call_ended
            )

        # Sweep any orphaned call records left CONNECTED / RINGING /
        # INITIATED across a restart. The brain task is in-memory, so
        # if the process exited abruptly (NixOS rebuild, OOM kill,
        # SIGKILL) any in-flight call's record never gets its outer
        # finally — it sits in storage looking active forever, which
        # breaks the SPA's display and confuses the AI's
        # follow-up message via ``ConversationMessagePoster``.
        await self._sweep_orphaned_calls()

        logger.info(
            "Phone call service started — backend=%s from=%s",
            self._backend_name,
            self._from_number or "<not configured>",
        )

    async def stop(self) -> None:
        if self._unsubscribe_ended is not None:
            try:
                self._unsubscribe_ended()
            except Exception:
                pass
            self._unsubscribe_ended = None
        # Gracefully hang up active calls. ``stop_all`` upstream gives
        # us 5 seconds; finishing any one call is fast (just hang up,
        # don't await final summary).
        for active in list(self._active.values()):
            try:
                await active.session.hang_up()
            except Exception:
                logger.exception("Hang up failed for %s", active.record.call_id)
            active.task.cancel()
        self._active.clear()
        if self._backend is not None:
            try:
                await self._backend.close()
            except Exception:
                logger.exception("Telephony backend close failed")
        self._backend = None

    # --- Configurable -----------------------------------------------

    @property
    def config_namespace(self) -> str:
        return "phone_call"

    @property
    def config_category(self) -> str:
        return "Phone"

    def config_params(self) -> list[ConfigParam]:
        params: list[ConfigParam] = [
            ConfigParam(
                key="backend",
                type=ToolParameterType.STRING,
                description="Telephony backend provider.",
                default="telnyx",
                restart_required=True,
                choices=tuple(TelephonyBackend.registered_backends().keys()),
            ),
            ConfigParam(
                key="from_number",
                type=ToolParameterType.STRING,
                description=(
                    "Shared E.164 caller-ID for outbound calls "
                    '(e.g. "+13035550100"). Must be a number you control on '
                    "the chosen telephony provider."
                ),
                default="",
            ),
            ConfigParam(
                key="max_call_seconds",
                type=ToolParameterType.NUMBER,
                description=(
                    "Hard cap on a single call's duration. Brain hangs up "
                    "at this many seconds even mid-sentence."
                ),
                default=float(_DEFAULT_MAX_CALL_SECONDS),
            ),
            ConfigParam(
                key="opening_disclosure_prompt",
                type=ToolParameterType.STRING,
                description=(
                    "The first thing Gilbert says on every outbound call. "
                    'Use ``{display_name}`` for the user\'s name. Required '
                    "by federal AI-call disclosure rules."
                ),
                default=_DEFAULT_OPENING_DISCLOSURE,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="call_system_prompt",
                type=ToolParameterType.STRING,
                description=(
                    "System prompt the LLM uses while driving a phone call. "
                    "Receives the brief + transcript per turn. Keep it tight "
                    "— phone latency is sensitive to long contexts."
                ),
                default=_DEFAULT_CALL_SYSTEM_PROMPT,
                multiline=True,
                ai_prompt=True,
            ),
        ]
        # Backend-specific config (API key, etc) flattened in.
        backend_cls = TelephonyBackend.registered_backends().get(self._backend_name)
        if backend_cls is not None:
            for bp in backend_cls.backend_config_params():
                params.append(
                    ConfigParam(
                        key=f"settings.{bp.key}",
                        type=bp.type,
                        description=bp.description,
                        default=bp.default,
                        restart_required=bp.restart_required,
                        sensitive=bp.sensitive,
                        choices=bp.choices,
                        backend_param=True,
                    )
                )
        return params

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        await self._apply_config(config)

    async def _apply_config(self, section: dict[str, Any]) -> None:
        self._from_number = str(section.get("from_number") or "")
        self._max_call_seconds = int(
            section.get("max_call_seconds") or _DEFAULT_MAX_CALL_SECONDS
        )
        self._opening_disclosure_prompt = str(
            section.get("opening_disclosure_prompt") or _DEFAULT_OPENING_DISCLOSURE
        )
        self._call_system_prompt = str(
            section.get("call_system_prompt") or _DEFAULT_CALL_SYSTEM_PROMPT
        )
        self._config = section.get("settings", {}) or {}

        backend_name = str(section.get("backend") or self._backend_name)
        registry = TelephonyBackend.registered_backends()
        backend_cls = registry.get(backend_name)
        if backend_cls is None:
            logger.warning(
                "Unknown telephony backend %r — available: %s",
                backend_name,
                sorted(registry.keys()),
            )
            return

        # Tear down old backend if it changed.
        if self._backend is not None and self._backend_name != backend_name:
            try:
                await self._backend.close()
            except Exception:
                logger.exception("Old telephony backend close failed")
            self._backend = None

        self._backend_name = backend_name
        if self._backend is None:
            self._backend = backend_cls()
            try:
                await self._backend.initialize(self._config)
            except Exception:
                logger.exception("Telephony backend initialize failed")
                self._backend = None

    # --- WS handler provider ----------------------------------------

    def get_ws_handlers(self) -> dict[str, Any]:
        return {
            "phone.call.list": self._ws_call_list,
            "phone.call.get": self._ws_call_get,
            "phone.call.test": self._ws_call_test,
            "phone.call.intervene_text": self._ws_call_intervene_text,
            "phone.call.hang_up": self._ws_call_hang_up,
        }

    async def _ws_call_list(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        """List recent calls for the caller (or all calls if admin)."""
        from gilbert.interfaces.storage import Query

        if self._storage is None:
            return _err(frame, 503, "Storage unavailable")
        rows = await self._storage.backend.query(Query(collection=_COLLECTION))
        # Newest first; per-user filter unless caller is admin (level 0).
        is_admin = getattr(conn, "user_level", 100) <= 0
        rows = [
            r for r in rows
            if is_admin or r.get("user_id") == getattr(conn, "user_id", "")
        ]
        rows.sort(key=lambda r: r.get("started_at", ""), reverse=True)
        return {
            "type": "phone.call.list.result",
            "ref": frame.get("id"),
            "calls": [_summarize_for_list(r) for r in rows[:50]],
        }

    async def _ws_call_get(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        call_id = str(frame.get("call_id") or "").strip()
        if not call_id:
            return _err(frame, 400, "call_id is required")
        if self._storage is None:
            return _err(frame, 503, "Storage unavailable")
        row = await self._storage.backend.get(_COLLECTION, call_id)
        if row is None:
            return _err(frame, 404, "Call not found")
        # Caller can see their own calls; admins see all.
        is_admin = getattr(conn, "user_level", 100) <= 0
        if not is_admin and row.get("user_id") != getattr(conn, "user_id", ""):
            return _err(frame, 403, "Not permitted")
        return {
            "type": "phone.call.get.result",
            "ref": frame.get("id"),
            "call": {"call_id": call_id, **row},
        }

    async def _ws_call_test(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        """Place a one-shot test call. The brain reads the disclosure +
        a brief that says "this is a test, please hang up" then waits
        for the remote to drop. Useful from the Settings page to verify
        the backend is wired up without committing to a real task."""
        to_number = str(frame.get("to_number") or "").strip()
        if not to_number:
            return _err(frame, 400, "to_number is required")
        call_id = await self.start_call(
            user_id=getattr(conn, "user_id", ""),
            display_name=getattr(conn, "display_name", "the user"),
            to_number=to_number,
            brief=(
                "This is a connectivity test from Gilbert. Greet the answerer, "
                "explain you're verifying telephony setup, apologize for the "
                "intrusion, and politely end the call as quickly as possible."
            ),
        )
        return {
            "type": "phone.call.test.result",
            "ref": frame.get("id"),
            "call_id": call_id,
        }

    async def _ws_call_intervene_text(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        """Inject a directive into an active call as a 'system note' the
        brain sees on its next turn. The brain prepends it to its next
        response so the user's direction takes effect immediately."""
        call_id = str(frame.get("call_id") or "").strip()
        directive = str(frame.get("directive") or "").strip()
        if not call_id or not directive:
            return _err(frame, 400, "call_id and directive are required")
        active = self._find_active_by_call(call_id)
        if active is None:
            return _err(frame, 404, "Call is not active")
        if active.record.user_id != getattr(conn, "user_id", ""):
            return _err(frame, 403, "Not your call")
        active.interventions_queue.put_nowait(directive)
        active.record.interventions.append(
            {
                "ts": _now_iso(),
                "who": "user",
                "text": directive,
            }
        )
        return {"type": "gilbert.result", "ref": frame.get("id"), "ok": True}

    async def _ws_call_hang_up(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        call_id = str(frame.get("call_id") or "").strip()
        active = self._find_active_by_call(call_id)
        if active is None:
            return _err(frame, 404, "Call is not active")
        if active.record.user_id != getattr(conn, "user_id", ""):
            return _err(frame, 403, "Not your call")
        try:
            await active.session.hang_up()
        except Exception:
            logger.exception("hang_up failed")
        return {"type": "gilbert.result", "ref": frame.get("id"), "ok": True}

    # --- AI tool provider -------------------------------------------

    @property
    def tool_provider_name(self) -> str:
        return "call"

    def get_tools(self, user_ctx: Any = None) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        return [
            ToolDefinition(
                name="make_phone_call",
                slash_group="call",
                slash_command="make",
                slash_help=(
                    "Place an outbound phone call on the user's behalf. "
                    '/call make "+13035550100" "<brief>"'
                ),
                description=(
                    "Place an outbound phone call. Gilbert will identify "
                    "himself as an automated assistant calling on behalf "
                    "of the user and follow the brief. The user can watch "
                    "the live transcript on the Calls page (/calls) and "
                    "intervene by text from there. "
                    "\n\n"
                    "IMPORTANT: ALWAYS invoke this tool when the user "
                    "asks to make a call. Do NOT assume from chat history "
                    "that a previous call is still active — call records "
                    "live in their own store, not in this conversation. "
                    "The tool itself enforces the one-active-call-per-user "
                    "limit and will return a specific error if a real "
                    "active call exists. If you THINK a call is in "
                    "progress but the user is asking again, the previous "
                    "call almost certainly ended; place the new call. "
                    "\n\n"
                    "This tool does NOT deliver outcome notifications "
                    "back into chat — do not promise the user you'll "
                    "report back here. Direct them to /calls for the "
                    "transcript and outcome."
                ),
                parameters=[
                    ToolParameter(
                        name="to_number",
                        type=ToolParameterType.STRING,
                        description=(
                            "Destination phone number in E.164 form "
                            '(e.g. "+13035550100").'
                        ),
                    ),
                    ToolParameter(
                        name="brief",
                        type=ToolParameterType.STRING,
                        description=(
                            "Natural-language description of what Gilbert "
                            "should accomplish on the call. Include any "
                            "context the receptionist will need (account "
                            "number, dates, preferences, hard constraints)."
                        ),
                    ),
                    ToolParameter(
                        name="callback_number",
                        type=ToolParameterType.STRING,
                        description=(
                            "Optional E.164 number to give the remote "
                            "party if they need to call the user back."
                        ),
                        required=False,
                    ),
                ],
                required_role="user",
            )
        ]

    async def execute_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> str | ToolOutput:
        if name != "make_phone_call":
            raise KeyError(name)
        # User context comes from the async-local set by the AI
        # orchestrator in ``_execute_tool_calls`` right before
        # invoking the provider. ``get_current_user`` returns
        # ``UserContext.SYSTEM`` when nothing is set — we reject
        # that case explicitly because a phone call without a real
        # caller can't be attributed for the concurrency cap or the
        # call record's ``user_id``.
        from gilbert.interfaces.auth import UserContext
        from gilbert.interfaces.context import (
            get_current_conversation_id,
            get_current_user,
        )

        ctx = get_current_user()
        if ctx is UserContext.SYSTEM or not getattr(ctx, "user_id", ""):
            raise ValueError(
                "make_phone_call must be invoked in the context of a user"
            )
        to_number = str(arguments.get("to_number") or "").strip()
        brief = str(arguments.get("brief") or "").strip()
        callback_number = str(arguments.get("callback_number") or "").strip()
        if not to_number or not brief:
            raise ValueError("to_number and brief are required")
        # Capture which conversation triggered the call so the brain
        # can post a "call ended" follow-up there on completion. Empty
        # string when the tool is invoked outside a chat context (e.g.
        # the /settings test button); the brain skips the follow-up
        # post in that case.
        originating_conv = get_current_conversation_id() or ""
        try:
            call_id = await self.start_call(
                user_id=str(getattr(ctx, "user_id", "")),
                display_name=str(
                    getattr(ctx, "display_name", "") or "the user"
                ),
                to_number=to_number,
                brief=brief,
                callback_number=callback_number,
                originating_conversation_id=originating_conv,
            )
        except RuntimeError as exc:
            return f"Could not place the call: {exc}"
        # Note carefully — the LLM has historically interpreted "I'll
        # report back" as a promise to the user that it'll deliver an
        # outcome message later in chat. We don't have that wiring yet
        # (the call ends as a bus event the AIService doesn't currently
        # listen to). Phrase the tool result as "I started the call,
        # you go watch it" — no promise, just a hand-off.
        return (
            f"I started a phone call to {to_number} (call id `{call_id}`). "
            "It's running in the background — the live transcript is on the "
            "Calls page (/calls) and you can intervene by text from there. "
            "I won't get a notification when the call ends, so don't expect "
            "a follow-up in this chat; check the Calls page for the outcome."
        )

    # --- start_call (the public entry point) ------------------------

    async def start_call(
        self,
        *,
        user_id: str,
        display_name: str,
        to_number: str,
        brief: str,
        callback_number: str = "",
        originating_conversation_id: str = "",
    ) -> str:
        """Place an outbound call. Returns the new call_id immediately.

        The call runs in a background task; observers tail it via
        ``phone.call.*`` events on the bus or the ``phone.call.get`` RPC.
        Raises ``RuntimeError`` if the user already has an active call
        or the service isn't configured.
        """
        if not self._enabled or self._backend is None:
            raise RuntimeError("Phone call service is not configured")
        if not self._from_number:
            raise RuntimeError("phone_call.from_number is not set in /settings")
        if user_id in self._active:
            raise RuntimeError(
                "You already have an active call — cancel it before placing another."
            )

        call_id = f"call_{uuid.uuid4().hex[:12]}"
        webhook_token = secrets.token_urlsafe(24)
        record = _CallRecord(
            call_id=call_id,
            user_id=user_id,
            to_number=to_number,
            from_number=self._from_number,
            callback_number=callback_number,
            brief=brief,
            status=CallStatus.INITIATED.value,
            webhook_token=webhook_token,
            originating_conversation_id=originating_conversation_id,
            started_at=_now_iso(),
        )
        await self._save_record(record)
        await self._publish(
            "phone.call.started",
            {
                "call_id": call_id,
                "user_id": user_id,
                "to_number": to_number,
                "brief": brief,
            },
        )

        try:
            session = await self._backend.place_call(
                to_number=to_number,
                from_number=self._from_number,
                call_id=call_id,
                webhook_token=webhook_token,
            )
        except Exception as exc:
            logger.exception("place_call failed for %s", call_id)
            record.status = CallStatus.FAILED.value
            record.failure_reason = f"backend.place_call failed: {exc}"
            record.ended_at = _now_iso()
            await self._save_record(record)
            await self._publish(
                "phone.call.ended",
                {"call_id": call_id, "user_id": user_id, "status": "failed"},
            )
            raise RuntimeError(
                f"Telephony backend failed to place the call: {exc}"
            ) from exc

        interventions: asyncio.Queue[str] = asyncio.Queue()
        task = asyncio.create_task(
            self._run_call(
                session=session,
                record=record,
                brief=CallBrief(
                    brief_text=brief, callback_number=callback_number
                ),
                display_name=display_name,
                interventions=interventions,
            ),
            name=f"phone-call-brain:{call_id}",
        )
        self._active[user_id] = _ActiveCall(
            record=record,
            session=session,
            task=task,
            interventions_queue=interventions,
        )
        # Clean the active slot when the brain finishes. Done in a
        # callback rather than at the end of ``_run_call`` so an
        # exception inside the brain still releases the slot.
        task.add_done_callback(lambda _t, uid=user_id: self._active.pop(uid, None))
        return call_id

    def _find_active_by_call(self, call_id: str) -> _ActiveCall | None:
        for active in self._active.values():
            if active.record.call_id == call_id:
                return active
        return None

    # --- The brain --------------------------------------------------

    async def _run_call(
        self,
        *,
        session: CallSession,
        record: _CallRecord,
        brief: CallBrief,
        display_name: str,
        interventions: asyncio.Queue[str],
    ) -> None:
        """The heart of the system: drive the LLM-powered conversation.

        Three concurrent jobs:

        - ``_status_loop``    — turn carrier events into record updates
        - ``_listen_loop``    — STT inbound audio, feed brain on end-of-speech
        - ``_speak_loop``     — LLM completions → TTS → outbound audio,
                                interruptible on remote barge-in

        ``_status_loop`` ends the call by setting ``stop.set()``; the
        other two cooperatively exit. We then finalize the record + emit
        the summary event.
        """
        log = logger.getChild(f"call:{record.call_id}")
        stop = asyncio.Event()
        speaking = _Speaking()
        messages: list[Message] = []
        # Cumulative seconds-since-call-start clock. Updated by the
        # status loop on each carrier event; the loops stamp transcript
        # turns + intervention rows from it.
        clock = _MonotonicClock()

        # System prompt for the LLM. The user's natural-language brief
        # is embedded verbatim — the LLM's job is to read it, hold the
        # conversation, and call the structured tools (hang_up,
        # confirm_and_end, etc.) when appropriate.
        system_prompt = self._call_system_prompt.format(
            display_name=display_name or "the user",
            brief=brief.brief_text,
            callback_number=brief.callback_number or "<none provided>",
        )
        opening_line = self._opening_disclosure_prompt.format(
            display_name=display_name or "the user",
        )
        # Prime the brain so its first "what should I say?" produces
        # the disclosure-plus-greeting sequence rather than waiting for
        # the receptionist to speak first. The LLM sees this as a
        # "system says go" instruction.
        messages.append(
            Message(
                role=MessageRole.USER,
                content=(
                    "(SYSTEM) The call has been answered. Open with the "
                    "required disclosure line (verbatim or near-verbatim) "
                    f"and then continue naturally: {opening_line!r}"
                ),
            )
        )

        # ── helpers ────────────────────────────────────────────────────

        async def _record_turn(who: str, text: str) -> None:
            turn = TranscriptTurn(who=who, text=text, ts_seconds=clock.now())
            record.transcript.append(
                {"who": turn.who, "text": turn.text, "ts": turn.ts_seconds}
            )
            await self._save_record(record)
            await self._publish(
                "phone.call.transcript_delta",
                {
                    "call_id": record.call_id,
                    "user_id": record.user_id,
                    "who": turn.who,
                    "text": turn.text,
                    "ts": turn.ts_seconds,
                },
            )

        async def _set_status(new: CallStatus, reason: str = "") -> None:
            if record.status == new.value:
                log.info("status unchanged: already %s", new.value)
                return
            log.info(
                "status transition: %s → %s (reason=%r)",
                record.status,
                new.value,
                reason,
            )
            record.status = new.value
            if reason:
                record.failure_reason = reason
            await self._save_record(record)
            await self._publish(
                "phone.call.status_changed",
                {
                    "call_id": record.call_id,
                    "user_id": record.user_id,
                    "status": new.value,
                    "reason": reason,
                },
            )

        # ── status loop: drains backend events, terminates the call ───
        #
        # Also fires the FIRST ``think_and_speak`` once the call reaches
        # CONNECTED. Without this the brain would sit silent on every
        # call because the previous design only triggered speaking from
        # the listen loop's FinalTranscript handler — i.e. the remote
        # had to talk first, which they won't until WE introduce
        # ourselves. The opener is the disclosure line + whatever the
        # LLM extracts from the brief.

        already_spoke = False

        async def _maybe_open_with_disclosure() -> None:
            nonlocal already_spoke
            if already_spoke:
                return
            already_spoke = True
            await _think_and_speak()

        async def _status_loop() -> None:
            log.info("status_loop: starting")
            try:
                async for event in session.events:
                    log.info("status_loop: received event %s", type(event).__name__)
                    if isinstance(event, CallStatusEvent):
                        await _set_status(event.status, event.reason)
                        if event.status is CallStatus.CONNECTED:
                            # Kick off the greeting in a task so the
                            # status loop keeps draining events
                            # (including a remote hangup mid-greeting).
                            asyncio.create_task(_maybe_open_with_disclosure())
                        if event.status in (CallStatus.HUNG_UP, CallStatus.FAILED):
                            log.info(
                                "status_loop: terminal status %s — setting stop",
                                event.status.value,
                            )
                            stop.set()
                            return
                    elif isinstance(event, DtmfEvent):
                        await _record_turn("them", f"(DTMF pressed: {event.digit})")
                    elif isinstance(event, CallErrorEvent):
                        log.warning("Call stream error: %s", event.message)
                log.info("status_loop: events iterator exhausted (closed)")
            except Exception:
                log.exception("status loop crashed")
                stop.set()

        # ── listen loop: STT → think_and_speak on end-of-speech ───────

        async def _listen_loop() -> None:
            if self._transcription is None:
                # Degrade gracefully — call continues, brain speaks the
                # opening + brief but won't get any responses back.
                # Useful for voicemail / leave-a-message flows where
                # transcription isn't strictly necessary.
                log.warning("Transcription unavailable — call continues TTS-only")
                record.outcome["transcription_available"] = False
                return
            try:
                # Wrap mulaw-8k inbound bytes into PCM s16le-8k stream
                # bytes; Deepgram is configured to expect linear16.
                stt_stream = await self._transcription.open_stream(
                    StreamConfig(
                        format=TranscriptionAudioFormat(
                            encoding=AudioEncoding.PCM_S16LE,
                            sample_rate=8000,
                            channels=1,
                        ),
                        interim_results=True,
                        vad_events=True,
                    )
                )
            except Exception:
                # STT couldn't open. Don't terminate the call — the
                # opening disclosure from the brain's CONNECTED handler
                # already went out, and the user might still want to
                # talk AT the remote even without transcription. The
                # call ends when the remote hangs up or the watchdog
                # fires.
                log.exception("Failed to open transcription stream — call continues TTS-only")
                record.outcome["transcription_available"] = False
                record.failure_reason = "stt_open_failed"
                return

            pump_task = asyncio.create_task(
                _pump_audio_to_stt(session.audio_in, stt_stream)
            )
            try:
                last_final_at: float | None = None
                async for ev in stt_stream.events():
                    if stop.is_set():
                        break
                    if isinstance(ev, SpeechStarted):
                        # Barge-in: if we're talking, cancel.
                        if speaking.active:
                            speaking.cancelled = True
                            await session.audio_out.clear()
                    elif isinstance(ev, FinalTranscript):
                        text = ev.text.strip()
                        if not text:
                            continue
                        await _record_turn("them", text)
                        messages.append(
                            Message(role=MessageRole.USER, content=text)
                        )
                        last_final_at = clock.now()
                        await _think_and_speak()
                    elif isinstance(ev, SpeechEnded):
                        # Recorded already by FinalTranscript; nothing
                        # extra here. Voicemail-detection heuristics
                        # use the sustained-silence signal from the
                        # status loop, not this single end.
                        _ = last_final_at  # keep for future heuristics
            except Exception:
                # Mid-call STT crash: log + degrade. Don't terminate the
                # call — opening greeting / brief execution may still be
                # in flight via TTS, and forcing a hangup wastes that.
                # The remote-hangup path or the watchdog will clean up.
                log.exception("listen loop crashed — call continues TTS-only")
                record.outcome["transcription_failed_midcall"] = True
            finally:
                pump_task.cancel()
                try:
                    await stt_stream.close()
                except Exception:
                    pass

        # ── think_and_speak: one LLM turn → TTS → audio_out ───────────

        async def _think_and_speak() -> None:
            if self._ai is None or self._tts is None:
                log.warning("AI or TTS missing — cannot respond")
                return
            # Drain any queued user-text-interventions; they ride as
            # extra system notes so the LLM weighs them on this turn.
            while not interventions.empty():
                try:
                    note = interventions.get_nowait()
                except asyncio.QueueEmpty:
                    break
                messages.append(
                    Message(
                        role=MessageRole.USER,
                        content=f"(SYSTEM NOTE from your supervisor) {note}",
                    )
                )

            response: AIResponse
            try:
                response = await self._ai.complete_one_shot(
                    messages=messages,
                    system_prompt=system_prompt,
                    max_tokens=600,
                    tools_override=_brain_tools(),
                )
            except Exception:
                log.exception("LLM call failed")
                return

            # We speak the LLM's text BEFORE dispatching its tool
            # calls. The old order was tool-calls-first, which meant
            # if the LLM emitted ``hang_up`` alongside its actual
            # reply ("Yes, the sky is blue. Goodbye!" + ``hang_up``),
            # the tool would fire and ``return`` before the answer
            # ever made it to TTS — the remote got dead air, the
            # call ended, and the chat log showed an
            # answered-but-never-spoken outcome.
            #
            # All brain tools (``hang_up`` / ``confirm_and_end`` /
            # ``escalate_to_user`` / ``note``) only need to fire
            # AFTER whatever the LLM wanted to say on this turn. The
            # tool-dispatch loop now runs at the bottom of this
            # function, after the audio has been written to the
            # carrier.
            text = response.message.content.strip()
            if not text and not response.message.tool_calls:
                return
            if not text:
                # No spoken response this turn, just tool calls — skip
                # straight to the dispatch loop at the bottom.
                for tc in response.message.tool_calls:
                    handled = await _handle_brain_tool(tc.tool_name, tc.arguments)
                    if handled == "hang_up":
                        stop.set()
                        try:
                            await session.hang_up()
                        except Exception:
                            log.debug("hang_up cleanup error", exc_info=True)
                        return
                return

            messages.append(Message(role=MessageRole.ASSISTANT, content=text))
            await _record_turn("us", text)

            # Synthesize and write to the carrier. ``MULAW_8000`` lets
            # ElevenLabs hand back carrier-ready bytes — no resample.
            try:
                # ``voice_id=""`` tells the TTS service to use its
                # configured default voice (set in /settings under TTS).
                # All other callers follow this pattern.
                synth = await self._tts.synthesize(
                    SynthesisRequest(
                        text=text,
                        voice_id="",
                        output_format=TTSAudioFormat.MULAW_8000,
                    )
                )
            except Exception:
                log.exception("TTS synthesize failed")
                return

            # Diagnostic: are we getting actual ulaw bytes back? A
            # short response, all-zeros prefix, or a header signature
            # (RIFF / fLaC / OggS) all tell us the format-map fix
            # wasn't enough.
            audio = synth.audio
            log.info(
                "TTS synth complete — format=%s bytes=%d "
                "first_8_hex=%s last_8_hex=%s zero_ratio=%.2f text_chars=%d",
                synth.format,
                len(audio),
                audio[:8].hex(),
                audio[-8:].hex() if len(audio) >= 8 else "",
                (audio.count(b"\xff") + audio.count(b"\x7f")) / max(len(audio), 1),
                len(text),
            )

            speaking.active = True
            speaking.cancelled = False
            generation = speaking.generation = speaking.generation + 1
            try:
                # Write in ~20ms chunks (160 bytes mulaw @ 8kHz mono)
                # so barge-in cancel takes effect within that window.
                chunk_size = 160
                chunks_written = 0
                for i in range(0, len(synth.audio), chunk_size):
                    if speaking.cancelled or generation != speaking.generation:
                        break
                    await session.audio_out.write(synth.audio[i : i + chunk_size])
                    chunks_written += 1
                    # Realtime pacing so we don't blast the carrier
                    # buffer (which can disable barge-in). 20ms.
                    await asyncio.sleep(0.02)
                log.info(
                    "TTS playback done — chunks_written=%d bytes=%d "
                    "wall_seconds≈%.2f (cancelled=%s)",
                    chunks_written,
                    chunks_written * chunk_size,
                    chunks_written * 0.02,
                    speaking.cancelled,
                )
            finally:
                speaking.active = False

            # Dispatch the LLM's tool calls now that the audio has
            # been spoken. ``hang_up`` here ends the call cleanly —
            # the remote heard the goodbye line before the line went
            # dead.
            for tc in response.message.tool_calls:
                handled = await _handle_brain_tool(tc.tool_name, tc.arguments)
                if handled == "hang_up":
                    stop.set()
                    try:
                        await session.hang_up()
                    except Exception:
                        log.debug("hang_up cleanup error", exc_info=True)
                    return

        # ── brain tools (the LLM-callable ones during the call) ─────

        async def _handle_brain_tool(
            name: str, args: dict[str, Any]
        ) -> str:
            if name == "hang_up":
                record.outcome["hang_up_reason"] = str(args.get("reason") or "")
                await _record_turn(
                    "system", f"(brain hung up: {args.get('reason', '')})"
                )
                return "hang_up"
            if name == "confirm_and_end":
                summary = args.get("summary") or {}
                if isinstance(summary, dict):
                    record.outcome.update(summary)
                await _record_turn(
                    "system",
                    f"(brain reading back confirmation: {summary})",
                )
                return "ok"
            if name == "escalate_to_user":
                record.outcome["escalated"] = True
                record.outcome["escalation_reason"] = str(args.get("reason") or "")
                await _record_turn(
                    "system",
                    f"(brain escalating: {args.get('reason', '')})",
                )
                await self._publish(
                    "phone.call.escalation_requested",
                    {
                        "call_id": record.call_id,
                        "user_id": record.user_id,
                        "reason": str(args.get("reason") or ""),
                    },
                )
                return "ok"
            if name == "note":
                key = str(args.get("key") or "").strip()
                value = args.get("value")
                if key:
                    record.outcome[key] = value
                return "ok"
            if name == "send_dtmf":
                # The LLM declared DTMF digits — backend doesn't yet
                # support sending these, so we just log + record.
                # Phase 4 will route this through a backend hook.
                digits = str(args.get("digits") or "")
                await _record_turn("us", f"(DTMF: {digits})")
                return "ok"
            log.warning("brain emitted unknown tool: %s", name)
            return "unknown"

        # ── timeout watchdog: hard cap the call duration ──────────────

        async def _watchdog() -> None:
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._max_call_seconds)
            except TimeoutError:
                log.warning(
                    "Call exceeded %ds cap — forcing hang up",
                    self._max_call_seconds,
                )
                record.outcome["forced_hangup_reason"] = "max_duration_exceeded"
                stop.set()
                try:
                    await session.hang_up()
                except Exception:
                    pass

        # ── orchestrate the three loops ────────────────────────────────

        log.info("_run_call: entering gather of status/listen/watchdog loops")
        try:
            results = await asyncio.gather(
                _status_loop(),
                _listen_loop(),
                _watchdog(),
                return_exceptions=True,
            )
            log.info(
                "_run_call: gather returned — results=%s",
                [type(r).__name__ if isinstance(r, BaseException) else "ok" for r in results],
            )
        finally:
            log.info("_run_call: entering finally cleanup (status before=%s)", record.status)
            # Always tell the carrier to terminate. If the brain exited
            # cleanly through the remote-hangup path the backend's
            # hang_up is already a no-op; if we exited because of an
            # internal crash (transcription failure, LLM error, etc.)
            # this is what stops Telnyx from keeping the call line open
            # and billing for it.
            try:
                await session.hang_up()
            except Exception:
                log.debug("hang_up cleanup error", exc_info=True)

            record.ended_at = _now_iso()
            try:
                started = datetime.fromisoformat(
                    record.started_at.replace("Z", "+00:00")
                )
                ended = datetime.fromisoformat(
                    record.ended_at.replace("Z", "+00:00")
                )
                record.duration_seconds = (ended - started).total_seconds()
            except Exception:
                pass
            if record.status not in (
                CallStatus.HUNG_UP.value,
                CallStatus.FAILED.value,
            ):
                record.status = CallStatus.HUNG_UP.value
            await self._save_record(record)
            await self._publish(
                "phone.call.ended",
                {
                    "call_id": record.call_id,
                    "user_id": record.user_id,
                    "status": record.status,
                    "duration_seconds": record.duration_seconds,
                    "outcome": record.outcome,
                },
            )

    # --- Persistence + bus ------------------------------------------

    async def _save_record(self, record: _CallRecord) -> None:
        if self._storage is None:
            return
        await self._storage.backend.put(
            _COLLECTION, record.call_id, record.to_dict()
        )

    async def _publish(self, event_type: str, data: dict[str, Any]) -> None:
        if self._resolver is None:
            return
        bus_svc = self._resolver.get_capability("event_bus")
        if not isinstance(bus_svc, EventBusProvider):
            return
        await bus_svc.bus.publish(
            Event(event_type=event_type, source="phone_call", data=data)
        )

    # --- Orphan cleanup ───────────────────────────────────────────────

    async def _sweep_orphaned_calls(self) -> None:
        """Mark any still-active call records as ``hung_up`` on startup.

        The brain runs in-process; if the process exits abruptly the
        outer try/finally that finalizes the record never runs. After
        restart, those records sit in storage looking like active
        calls forever — the SPA renders them as such, the AI's
        ``ConversationMessagePoster`` doesn't fire for them (because
        they never publish ``phone.call.ended``), and they crowd the
        list page.

        This method finds records where ``status`` is one of the
        "in-flight" values and rewrites them to ``hung_up`` with
        ``failure_reason = "orphaned_at_restart"``. Also publishes
        the ended event so any subscriber (e.g. the chat-message
        poster) gets a chance to clean up — though the originating
        conversation might be stale by the time we fire it.

        Safe to call repeatedly: only touches rows that match the
        in-flight filter.
        """
        if self._storage is None:
            return
        from gilbert.interfaces.storage import Filter, FilterOp, Query

        in_flight = {
            CallStatus.INITIATED.value,
            CallStatus.RINGING.value,
            CallStatus.CONNECTED.value,
        }
        try:
            rows = await self._storage.backend.query(
                Query(collection=_COLLECTION)
            )
        except Exception:
            logger.debug("orphan sweep: query failed", exc_info=True)
            return

        swept = 0
        for row in rows:
            status = str(row.get("status") or "")
            if status not in in_flight:
                continue
            call_id = str(row.get("_id") or "")
            if not call_id:
                continue
            row["status"] = CallStatus.HUNG_UP.value
            row["failure_reason"] = "orphaned_at_restart"
            row["ended_at"] = _now_iso()
            # Best-effort duration from started_at if we have it.
            if row.get("started_at") and not row.get("duration_seconds"):
                try:
                    started = datetime.fromisoformat(
                        str(row["started_at"]).replace("Z", "+00:00")
                    )
                    ended = datetime.now(UTC)
                    row["duration_seconds"] = (ended - started).total_seconds()
                except Exception:
                    pass
            try:
                # ``put`` overwrites the entity body keyed by id. The
                # storage layer drops ``_id`` for us if present.
                clean = {k: v for k, v in row.items() if k != "_id"}
                await self._storage.backend.put(_COLLECTION, call_id, clean)
                swept += 1
            except Exception:
                logger.debug(
                    "orphan sweep: failed to update %s", call_id, exc_info=True
                )

        if swept > 0:
            logger.info(
                "Phone call orphan sweep: marked %d stuck-active record(s) "
                "as hung_up (orphaned_at_restart)",
                swept,
            )

    # --- Bus subscription: phone.call.ended → follow-up chat message ──

    async def _on_call_ended(self, event: Event) -> None:
        """Post a synthetic assistant message into the originating chat
        summarizing how the call wrapped.

        Fires for every ``phone.call.ended`` event the bus sees. The
        record is reloaded fresh from storage (the event payload only
        carries a summary) so the message reflects the actual final
        transcript / outcome — including anything the watchdog wrote
        after the bus event was scheduled.

        Best-effort throughout. A missing storage, missing record,
        unset originating conversation, or missing AI poster each silently
        no-ops — the failure mode is "no follow-up message," which is
        the current behavior and not actively harmful.
        """
        if self._storage is None or self._message_poster is None:
            return
        data = event.data or {}
        call_id = str(data.get("call_id") or "")
        if not call_id:
            return
        try:
            row = await self._storage.backend.get(_COLLECTION, call_id)
        except Exception:
            logger.debug(
                "_on_call_ended: storage read failed for %s", call_id, exc_info=True
            )
            return
        if not row:
            return
        conv_id = str(row.get("originating_conversation_id") or "")
        if not conv_id:
            return  # call wasn't triggered from a chat (e.g. test button)

        record = _record_from_dict(row, call_id=call_id)
        message_text = _format_call_ended_summary(record)
        try:
            await self._message_poster.append_assistant_message(
                conversation_id=conv_id,
                content=message_text,
            )
        except Exception:
            logger.debug(
                "_on_call_ended: append_assistant_message failed for conv %s",
                conv_id,
                exc_info=True,
            )

    # --- Callback routing (inbound calls to the shared number) ──────

    async def find_call_for_inbound(
        self, *, from_number: str
    ) -> _CallRecord | None:
        """When somebody calls the shared from-number, look up the most
        recent outbound to that caller. Used by the telephony backend
        to route the inbound into the same brain with the original brief.

        Returns ``None`` if no match — the backend then treats it as a
        stranger and plays a generic greeting.
        """
        from gilbert.interfaces.storage import Filter, FilterOp, Query

        if self._storage is None:
            return None
        rows = await self._storage.backend.query(
            Query(
                collection=_COLLECTION,
                filters=[Filter(field="to_number", op=FilterOp.EQ, value=from_number)],
            )
        )
        if not rows:
            return None
        rows.sort(key=lambda r: r.get("started_at", ""), reverse=True)
        latest = rows[0]
        return _record_from_dict(latest, call_id=str(latest.get("_id") or ""))


# ── Module-level helpers ──────────────────────────────────────────────


def _err(frame: dict[str, Any], code: int, message: str) -> dict[str, Any]:
    return {
        "type": "gilbert.error",
        "ref": frame.get("id"),
        "code": code,
        "error": message,
    }


def _summarize_for_list(row: dict[str, Any]) -> dict[str, Any]:
    """Shrink the row before shipping a list of them — drops the full
    transcript and any large fields. Frontend fetches the full record
    via ``phone.call.get`` when the user opens one."""
    return {
        "call_id": row.get("_id", ""),
        "user_id": row.get("user_id", ""),
        "to_number": row.get("to_number", ""),
        "status": row.get("status", ""),
        "started_at": row.get("started_at", ""),
        "ended_at": row.get("ended_at", ""),
        "duration_seconds": row.get("duration_seconds", 0),
        "brief_preview": (row.get("brief") or "")[:120],
        "outcome": row.get("outcome", {}),
        "failure_reason": row.get("failure_reason", ""),
    }


def _record_from_dict(d: dict[str, Any], *, call_id: str) -> _CallRecord:
    return _CallRecord(
        call_id=call_id,
        user_id=str(d.get("user_id", "")),
        to_number=str(d.get("to_number", "")),
        from_number=str(d.get("from_number", "")),
        callback_number=str(d.get("callback_number", "")),
        brief=str(d.get("brief", "")),
        status=str(d.get("status", "")),
        webhook_token=str(d.get("webhook_token", "")),
        originating_conversation_id=str(
            d.get("originating_conversation_id", "")
        ),
        started_at=str(d.get("started_at", "")),
        ended_at=str(d.get("ended_at", "")),
        duration_seconds=float(d.get("duration_seconds") or 0.0),
        transcript=list(d.get("transcript") or []),
        outcome=dict(d.get("outcome") or {}),
        failure_reason=str(d.get("failure_reason", "")),
        interventions=list(d.get("interventions") or []),
    )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _format_call_ended_summary(record: _CallRecord) -> str:
    """Render the "call ended" follow-up the originating chat sees.

    Keep this in plain markdown — the chat renderer handles it the
    same way as any other assistant message. The leading
    "(call ended)" marker makes it obvious to both the user (visual
    cue) and the LLM (on the next turn) that this is an out-of-band
    insertion, not a real assistant response in the conversation.
    """
    duration = ""
    if record.duration_seconds and record.duration_seconds > 0:
        mins = int(record.duration_seconds // 60)
        secs = int(record.duration_seconds % 60)
        duration = (
            f"{mins}m {secs}s" if mins else f"{secs}s"
        )

    parts: list[str] = []
    parts.append(
        f"(call ended) The phone call I started to {record.to_number} has "
        f"ended (status: `{record.status}`"
        + (f", duration: {duration}" if duration else "")
        + f", call id: `{record.call_id}`)."
    )
    if record.failure_reason:
        parts.append(f"\nFailure reason: `{record.failure_reason}`.")
    if record.outcome:
        # Keep the structured outcome readable but compact. Skip the
        # internal-only flags the brain stamps for debugging.
        outcome = {
            k: v
            for k, v in record.outcome.items()
            if not k.startswith("transcription_")
        }
        if outcome:
            parts.append(f"\nOutcome:\n```\n{outcome}\n```")
    # Brief transcript preview — at most the last 6 turns so the
    # follow-up message doesn't bloat the conversation.
    if record.transcript:
        tail = record.transcript[-6:]
        lines = [f"- **{t.get('who', '?')}**: {t.get('text', '')}" for t in tail]
        parts.append("\nRecent transcript:\n" + "\n".join(lines))
    parts.append(
        "\nThe call is no longer active. Full transcript + recording "
        "(when available) is on the Calls page."
    )
    return "\n".join(parts)


class _MonotonicClock:
    """Seconds-since-construction clock for transcript timestamps."""

    def __init__(self) -> None:
        self._start = asyncio.get_event_loop().time()

    def now(self) -> float:
        return asyncio.get_event_loop().time() - self._start


async def _pump_audio_to_stt(
    audio_in: Any,
    stream: Any,
) -> None:
    """Read mulaw-8k chunks from the carrier and feed PCM-16 to the
    transcriber. Decodes per-chunk so latency stays at the chunk
    boundary instead of buffering.

    ``audioop.ulaw2lin`` is deprecated in 3.13 but still functional.
    Replace with ``soxr`` or a vendored C helper if it gets removed.
    """
    try:
        async for chunk in audio_in:
            pcm = audioop.ulaw2lin(chunk, 2)  # 8-bit µ-law → 16-bit PCM
            await stream.send(pcm)
    except Exception:
        logger.debug("audio pump ended", exc_info=True)


def _brain_tools() -> list[ToolDefinition]:
    """Tools the LLM may call during a phone conversation.

    These are NOT exposed as Gilbert chat tools — they're scoped to the
    one-shot LLM call inside the brain. The brain dispatches them
    locally rather than going back through the service manager.
    """
    return [
        ToolDefinition(
            name="hang_up",
            description="End the call. Use when the conversation is done.",
            parameters=[
                ToolParameter(
                    name="reason",
                    type=ToolParameterType.STRING,
                    description="Short reason recorded on the call.",
                ),
            ],
        ),
        ToolDefinition(
            name="confirm_and_end",
            description=(
                "Read back a structured summary of what was agreed, then "
                "end the call once the remote confirms. Use when an "
                "appointment / booking / commitment was made."
            ),
            parameters=[
                ToolParameter(
                    name="summary",
                    type=ToolParameterType.OBJECT,
                    description=(
                        "Structured outcome to read back — e.g. "
                        '{"appointment_datetime": "...", '
                        '"service_advisor": "...", "loaner_confirmed": true}'
                    ),
                ),
            ],
        ),
        ToolDefinition(
            name="escalate_to_user",
            description=(
                "Bail out — the situation needs the actual user. Gilbert "
                "will apologize, ask the remote to call back, and hang up."
            ),
            parameters=[
                ToolParameter(
                    name="reason",
                    type=ToolParameterType.STRING,
                    description="Why escalation is required.",
                ),
            ],
        ),
        ToolDefinition(
            name="note",
            description=(
                "Stash a fact onto the call's structured outcome. Use for "
                "anything worth surfacing in the post-call summary that "
                "doesn't trigger an end-of-call."
            ),
            parameters=[
                ToolParameter(
                    name="key",
                    type=ToolParameterType.STRING,
                    description="Outcome field name (snake_case).",
                ),
                ToolParameter(
                    name="value",
                    type=ToolParameterType.STRING,
                    description="Value to store.",
                ),
            ],
        ),
        ToolDefinition(
            name="send_dtmf",
            description=(
                "Send DTMF digits to navigate an IVR menu. Use ONLY when "
                "the remote prompts for a key press."
            ),
            parameters=[
                ToolParameter(
                    name="digits",
                    type=ToolParameterType.STRING,
                    description='Sequence of 0-9, *, # — e.g. "2" or "1234#".',
                ),
            ],
        ),
    ]


_DEFAULT_OPENING_DISCLOSURE = (
    "Hi, this is Gilbert, an automated assistant calling on behalf of "
    "{display_name}. This call is being recorded for quality."
)

_DEFAULT_CALL_SYSTEM_PROMPT = """\
You are Gilbert, an automated phone assistant calling on behalf of {display_name}.

OBJECTIVE (verbatim from the user):
{brief}

CALLBACK NUMBER: {callback_number}

RULES OF ENGAGEMENT
1. Open with the required disclosure line word-for-word the first time you
   speak. Don't paraphrase it away.
2. Be conversational and brief. This is a phone call, not an email — short
   sentences, no markdown, no bullet lists.
3. NEVER confirm a time / price / commitment the remote hasn't actually
   offered. Read times back verbatim before agreeing.
4. If the remote asks "are you a real person" or "is this AI / a bot",
   confirm: "Yes, I'm an automated assistant calling on behalf of
   {display_name}." Offer that {display_name} can call back directly if
   they prefer.
5. If the remote asks for a callback number, provide the callback number
   above. If none was given, say so and offer to have {display_name} reach
   out directly.
6. When you've reached the objective, call ``confirm_and_end`` with a
   structured summary, hear the confirmation, then ``hang_up``.
7. If the situation is beyond your authority (legal questions, payment
   information, etc), call ``escalate_to_user`` with a reason.
8. If the receptionist needs to transfer you and asks to put you on hold,
   say "of course" and stay quiet — don't fill the silence. Pick up the
   conversation when whoever they transferred you to speaks.
9. If you hit voicemail, leave a single concise message including the
   callback number, then ``hang_up`` with reason "voicemail".
10. Stay in the voice register. No "as I mentioned above" — phone calls
    don't have an "above."
"""
