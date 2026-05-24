"""Conversation-engine interface — the reusable bidirectional-audio loop.

The conversation engine drives any back-and-forth voice exchange Gilbert is
in: outbound phone calls, wake-word activated local voice sessions,
videoconference participants, etc. The shape mirrors what proved necessary
in the phone-call brain — a session-like audio I/O endpoint, a few status
events, an LLM brain that turn-takes and barges-in, and a pluggable set of
"brain tools" the LLM can call to mutate the conversation's outcome.

This module deliberately contains nothing carrier-, hardware-, or
modality-specific. The phone-call wrapper builds on top of these
abstractions; the eventual voice-agent / wake-word wrapper will too. Each
of them brings their own brain-tool provider, their own session
implementation, and their own opening policy (do we speak first? wait for
the other side?) — the engine just orchestrates.

This file is pure: standard library + the existing ``gilbert.interfaces``
sub-modules only. No core service imports.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from gilbert.interfaces.tools import ToolDefinition

# ── Status / event types ─────────────────────────────────────────────


class ConversationStatus(StrEnum):
    """High-level lifecycle states a conversation session moves through.

    These are the engine-visible states. Modality-specific layers can
    introduce their own (``CallStatus`` carries ``RINGING`` for phone
    calls, for instance) and translate to / from these on the boundary.
    """

    PENDING = "pending"        # session created, audio not yet flowing
    ACTIVE = "active"          # bidirectional audio is live
    ENDED = "ended"            # session closed cleanly
    FAILED = "failed"          # session never reached active, or crashed


@dataclass(frozen=True)
class ConversationStatusEvent:
    """Lifecycle transition emitted by the session."""

    status: ConversationStatus
    reason: str = ""


@dataclass(frozen=True)
class ConversationErrorEvent:
    """Non-fatal stream-level issue surfaced by the session."""

    message: str
    recoverable: bool = True


# Concrete event types layers like telephony will extend this union.
# Engine code branches on ``isinstance`` so unknown subtypes are
# logged-and-ignored gracefully.
ConversationEvent = ConversationStatusEvent | ConversationErrorEvent


# ── Audio I/O ────────────────────────────────────────────────────────


class AudioSink(Protocol):
    """Where the engine pushes synthesized audio that should reach the
    remote/listener.

    Implementations buffer + chunk for whatever transport carries the
    session (a Media Stream WebSocket, a speaker driver, an HTTP audio
    sink, etc.). ``clear()`` discards anything buffered-but-unsent — the
    barge-in handler calls this so the engine can stop talking the
    instant the user starts.

    ``flush()`` signals end-of-utterance — the engine calls this once
    the LLM's spoken text has been fully written. Real-time transports
    (carrier media streams) ignore it because they're already pumping
    chunks at wire rate. Turn-taking transports (a browser tab that
    plays an MP3 per turn via ``<audio>``) use the flush boundary to
    package the buffered bytes into a single clip and dispatch it.
    The default implementation is a no-op, so existing sinks don't
    have to change.
    """

    async def write(self, chunk: bytes) -> None: ...
    async def clear(self) -> None: ...

    async def flush(self) -> None:
        """Optional end-of-utterance signal. Default is a no-op."""
        return None


# ── Session ──────────────────────────────────────────────────────────


@dataclass
class ConversationSession:
    """One open conversation. The engine reads inbound audio + events from
    this, and writes outbound audio through ``audio_out``. Closing is the
    consumer's responsibility (``end_session`` calls back into whatever
    transport owns the session).

    Modality-specific layers extend this dataclass with their own fields
    (``CallSession`` adds DTMF events + caller-ID metadata, a future
    ``LocalVoiceSession`` would add wake-word-source information, etc.).
    The engine only touches the common fields.

    Audio format: 8 kHz mulaw is the lowest common denominator (carriers
    require it; local mics resample down to it). Higher-rate sessions
    can negotiate via the audio-format negotiation hook (TODO when the
    voice-agent plugin lands and needs 16 kHz mic input).
    """

    session_id: str
    audio_in: AsyncIterator[bytes]
    audio_out: AudioSink
    events: AsyncIterator[ConversationEvent]

    async def end_session(self) -> None:
        """Tear down the underlying transport. Idempotent — repeated
        calls are safe. The session's audio iterators must stop yielding
        after this returns."""
        ...


# ── Brain-tool framework ─────────────────────────────────────────────


class BrainToolResult(StrEnum):
    """What the engine should do after dispatching a brain tool.

    - ``OK``: tool ran, conversation continues.
    - ``END_CONVERSATION``: tool requested termination — engine should
      call ``session.end_session()`` and exit the loop.
    - ``ESCALATE``: tool flagged this as something the supervising user
      needs to handle. Engine ends the session (semantically the same as
      END_CONVERSATION at this layer); the wrapper plugin's bus-event
      handler is what actually surfaces the escalation to the user.
    """

    OK = "ok"
    END_CONVERSATION = "end_conversation"
    ESCALATE = "escalate"


@dataclass
class ConversationContext:
    """Per-conversation runtime context handed to brain-tool handlers.

    Lets tool providers record transcript turns, mutate a structured
    outcome dict the wrapper persists, publish bus events, and inspect
    the session itself. Lives only during the conversation; the wrapper
    typically reads the final state from ``outcome`` after the engine
    returns.

    Mutable on purpose — tool handlers update ``outcome`` in place. The
    record-turn / publish-event hooks are async because the wrappers
    they bridge to (storage writes, bus publishes) are too.
    """

    session: ConversationSession
    outcome: dict[str, Any]
    failure_reason: str = ""

    record_turn: Callable[[str, str], Awaitable[None]] = field(
        default=lambda who, text: _noop_record(who, text)  # type: ignore[assignment, return-value]
    )
    publish_event: Callable[[str, dict[str, Any]], Awaitable[None]] = field(
        default=lambda etype, data: _noop_publish(etype, data)  # type: ignore[assignment, return-value]
    )


async def _noop_record(who: str, text: str) -> None:
    """Default ``record_turn`` callback — drops the turn on the floor.

    Tests and skeleton wrappers can construct a ``ConversationContext``
    without wiring up persistence; this avoids requiring a callback at
    every callsite. Production wrappers (phone-call service, voice-agent
    plugin) override this with their real persistence.
    """
    return None


async def _noop_publish(event_type: str, data: dict[str, Any]) -> None:
    """Default ``publish_event`` callback — drops the event on the floor."""
    return None


class BrainToolProvider(Protocol):
    """Plug-in source of brain tools for the conversation engine.

    The engine asks for the tool list once at conversation start
    (``get_brain_tools()``) and dispatches every tool call the LLM
    emits through ``handle_brain_tool``. Providers carry their own
    state — the phone-call wrapper carries the ``_CallRecord``, a
    voice-agent wrapper would carry its own session-state — and they
    return a ``BrainToolResult`` telling the engine whether the
    conversation should continue.

    All providers SHOULD include a way for the LLM to end the
    conversation; without one, the engine will only exit on the
    session's own status events (timeout, remote hangup, etc.).
    """

    def get_brain_tools(self) -> list[ToolDefinition]: ...

    async def handle_brain_tool(
        self,
        name: str,
        args: dict[str, Any],
        ctx: ConversationContext,
    ) -> BrainToolResult: ...


@runtime_checkable
class BrainToolProviderRT(Protocol):
    """Runtime-checkable variant of ``BrainToolProvider``.

    Use this when you need ``isinstance(x, BrainToolProviderRT)`` (e.g.
    discovery / wiring code). The plain ``Protocol`` form above is
    preferred for type hints because it doesn't drag in
    ``@runtime_checkable``'s reflection cost on every isinstance call.
    """

    def get_brain_tools(self) -> list[ToolDefinition]: ...

    async def handle_brain_tool(
        self,
        name: str,
        args: dict[str, Any],
        ctx: ConversationContext,
    ) -> BrainToolResult: ...


# ── Opening policy ───────────────────────────────────────────────────


class OpeningBehavior(StrEnum):
    """Who speaks first when the session becomes active?

    - ``WAIT_FOR_REMOTE``: stay silent and listen; on the first inbound
      FinalTranscript, run the brain in response. Phone-call use:
      recipient picks up, says "hello?", brain responds.
    - ``SPEAK_FIRST``: brain produces a cold-open immediately on the
      ACTIVE status. Wake-word use: wake fires, brain greets.
    """

    WAIT_FOR_REMOTE = "wait_for_remote"
    SPEAK_FIRST = "speak_first"


@dataclass(frozen=True)
class OpeningPolicy:
    """How the engine should open the conversation.

    ``behavior`` selects the strategy. ``fallback_timeout_seconds`` only
    applies to ``WAIT_FOR_REMOTE`` — if the remote stays silent past
    this many seconds, the engine cold-opens anyway so the line doesn't
    hang in dead silence (voicemail, mute, hold music).
    """

    behavior: OpeningBehavior = OpeningBehavior.SPEAK_FIRST
    fallback_timeout_seconds: float = 4.0


# ── Engine configuration ─────────────────────────────────────────────


@dataclass
class ConversationConfig:
    """Per-conversation configuration handed to the engine.

    Carries everything the engine needs that isn't part of the session
    itself: the LLM system prompt, the brain-tool provider, the opening
    policy, and a few callbacks the wrapper uses to observe the
    conversation as it progresses.
    """

    system_prompt: str
    brain_tool_provider: BrainToolProvider
    opening_policy: OpeningPolicy = field(default_factory=OpeningPolicy)
    max_conversation_seconds: int = 900
    # TTS output format used for synthesis. Phone calls want mulaw_8000
    # (Telnyx wire format). Browser-tab voice agents want MP3 because
    # the SPA plays clips via HTMLAudioElement. The default is
    # ``None`` which means "use the engine's default" (mulaw_8000,
    # matching the original phone-call brain). The engine passes this
    # straight to ``TTSProvider.synthesize`` as ``output_format``.
    tts_output_format: Any = None  # gilbert.interfaces.tts.AudioFormat | None
    # MIME hint for the buffered/flushed audio, used by sinks that
    # need to package the audio into a single clip and dispatch it
    # via a URL-based player. The voice-agent's ``BrowserAudioSink``
    # reads this to set the data-URL MIME type; sinks that don't
    # need it can ignore.
    tts_output_mime: str = "audio/wav"

    # Format of the bytes the session's ``audio_in`` iterator yields.
    # Phone calls hand the engine raw mulaw 8 kHz (the carrier wire
    # format). Voice-agent browser sessions hand us PCM_S16LE
    # 16 kHz (what WebAudio captures natively). The engine's STT
    # pump branches on this to skip the ulaw→PCM decode for the
    # PCM-in case.
    #
    # ``None`` means "the legacy mulaw_8000 default" so existing
    # phone-call wrappers don't need to opt in.
    audio_input_format: Any = None  # gilbert.interfaces.transcription.AudioFormat | None

    # Format the engine asks STT for. Phone calls always run STT at
    # 8 kHz because that's the carrier rate; voice-agent uses 16 kHz
    # because the mic captures cleanly there and Scribe Realtime is
    # happier with higher-rate audio. ``None`` falls back to PCM_S16LE
    # @ 8 kHz (phone-call default).
    stt_audio_format: Any = None  # gilbert.interfaces.transcription.AudioFormat | None

    # Whether the engine should pace TTS chunks at realtime (20ms per
    # chunk). Phone calls NEED this — Telnyx expects mulaw frames at
    # 50fps for the carrier to play them correctly. Voice-agent
    # sessions DON'T — the browser plays the whole MP3 clip in one
    # shot from a data URL, and pacing the bytes adds 30+ seconds of
    # gratuitous delay (the engine paces MP3 at the mulaw byte-rate
    # which is ~10x slower than the MP3's actual playback rate).
    # Default True to preserve phone-call behaviour.
    tts_realtime_pacing: bool = True

    # Whether the engine should drive the LLM via ``AIService.chat()``
    # (full agentic loop — knowledge.search, MCP tools, agent
    # dispatch, scheduler, etc. all available) or
    # ``AIService.complete_one_shot()`` (single round, brain tools
    # only).
    #
    # ``True`` — voice-agent path. The engine no longer maintains
    # its own ``messages`` array; the AI service does (persisted by
    # ``conversation_id``). Brain tools (``end_conversation``,
    # ``hang_up``, …) are registered as regular Gilbert tools and
    # read the active session via
    # ``get_current_conversation_ctx()``. The engine sets that
    # ContextVar before each ai.chat() call and inspects
    # ``ctx.outcome["end_requested"]`` after to decide whether to
    # terminate.
    #
    # ``False`` — phone-call path (existing). Engine maintains
    # messages, calls complete_one_shot with brain-tool override,
    # dispatches via the ``BrainToolProvider`` callback.
    use_full_ai_service: bool = False

    # Optional priming messages prepended to the message list before the
    # first LLM turn. Phone-call wrapper uses this to inject the
    # "(SYSTEM) call answered" cue + the disclosure-line example.
    priming_messages: list[Any] = field(default_factory=list)

    # Conversational filler ("hmm, let me check…") played while the LLM
    # is thinking. The engine kicks off the chat() call, waits up to
    # ``filler_threshold_seconds`` for it to return, and if it hasn't,
    # speaks a randomly-chosen phrase from ``filler_phrases`` before
    # awaiting the real result. This only kicks in on the
    # ``use_full_ai_service=True`` path (the chat() agentic loop with
    # tool calls) — the complete_one_shot path is single-round and
    # rarely slow enough to need a filler.
    #
    # Set ``filler_threshold_seconds`` to 0.0 OR pass an empty list for
    # ``filler_phrases`` to disable. Both default off so existing
    # consumers (phone) don't pick up the behaviour by accident.
    filler_threshold_seconds: float = 0.0
    filler_phrases: list[str] = field(default_factory=list)

    # External pause signal for the engine's STT lifecycle. When the
    # wrapper sets this Event, the engine closes its open STT stream
    # and cancels the audio pump (so it isn't paying ElevenLabs Scribe
    # to listen to nothing). When the wrapper clears the Event, the
    # engine opens a fresh STT stream and resumes pumping. The
    # voice-agent plugin uses this for its wake-word "dormant" mode:
    # set on dormant, clear on wake. ``None`` means "no pause
    # mechanism" — the engine opens STT once and leaves it open for
    # the conversation lifetime (the existing phone-call behaviour).
    listening_paused: Any = None  # asyncio.Event | None

    # ── Observability callbacks (all optional) ───────────────────────
    #
    # The engine invokes these as the conversation progresses. Wrappers
    # use them to persist their own per-modality records, publish bus
    # events, and so on. ``None`` callbacks are skipped — no need to
    # write a no-op stub at every callsite.

    on_status_change: (
        Callable[[ConversationStatus, str], Awaitable[None]] | None
    ) = None
    on_transcript_turn: (
        Callable[[str, str, float], Awaitable[None]] | None
    ) = None  # (who, text, ts_seconds)
    on_llm_turn: (
        Callable[[str, list[str]], Awaitable[None]] | None
    ) = None  # (text, tool_names)
    on_speaking_done: (
        Callable[[], Awaitable[None]] | None
    ) = None  # fired after each TTS playback completes (engine quiet)


# ── Outcome the engine returns ───────────────────────────────────────


@dataclass
class ConversationOutcome:
    """Final state of a completed conversation.

    The engine returns this; the wrapper uses it to populate its
    per-modality record (a ``_CallRecord`` for phone calls, a
    ``_VoiceConversationRecord`` for voice-agent sessions, etc.).
    """

    final_status: ConversationStatus
    duration_seconds: float
    outcome: dict[str, Any]  # what brain-tools wrote
    failure_reason: str = ""
    # Whether any LLM turn was actually produced (False means we never
    # got past the opening — useful for filtering out "ringback then
    # hangup" non-conversations from the SPA listing).
    spoke_at_all: bool = False


# ── Service-level capability protocol ────────────────────────────────


# ── Active-conversation context (ContextVar-backed) ─────────────────


# Holds the active ``ConversationContext`` for the running async task.
# The engine sets this before invoking the AI; brain tools that get
# dispatched as regular Gilbert tools (voice-agent's
# ``end_conversation``, phone-call's ``hang_up``, …) read it back to
# find the session they're modifying. Default ``None`` outside of any
# voice/phone session — tools should check and return early if they
# can't locate a session.
_current_conversation_ctx: ContextVar[ConversationContext | None] = ContextVar(
    "_current_conversation_ctx", default=None
)


def get_current_conversation_ctx() -> ConversationContext | None:
    """Return the active ``ConversationContext`` for this async task,
    or ``None`` when not inside a voice/phone session.

    Tools that want to mutate the live conversation (set outcome
    fields, signal "end the conversation") read this and return early
    when it's ``None`` (they're being called outside any session,
    e.g. accidentally surfaced in regular chat).
    """
    return _current_conversation_ctx.get()


def set_current_conversation_ctx(ctx: ConversationContext | None) -> None:
    """Set / clear the active ``ConversationContext`` for this task.

    Engine calls this before invoking the AI service so that any
    brain tool the LLM calls can find its session. Should be paired
    with a clear (``set(None)``) at the end of the turn, OR scoped
    via ``copy_context()`` so the value doesn't leak across tasks.
    """
    _current_conversation_ctx.set(ctx)


@runtime_checkable
class ConversationEngine(Protocol):
    """The capability other services / plugins consume to run a
    conversation. Implemented by the core ``VoiceBrainService`` (Step
    3 of the conversation-engine extraction); other services resolve
    it via ``resolver.get_capability("voice_brain")``.

    Single method: ``run_conversation(session, config) -> outcome``.
    The implementation handles three-loop orchestration (status drain,
    listen+STT, watchdog), barge-in (local-VAD plus STT signal),
    speak-first / wait-for-remote opening, LLM turn-loop with tool
    dispatch, and TTS pacing. None of that is the caller's concern.
    """

    async def run_conversation(
        self,
        session: ConversationSession,
        config: ConversationConfig,
    ) -> ConversationOutcome: ...
