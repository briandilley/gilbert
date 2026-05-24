"""Voice-brain service — the reusable conversation-loop engine.

Drives any bidirectional voice conversation Gilbert is in. Originally
extracted from the phone-call service's ``_run_call`` brain; the same
engine now powers (or will power) any modality that needs to:

1. Listen to inbound audio.
2. Run that audio through STT.
3. Drive an LLM turn-by-turn with a configurable brain-tool catalog.
4. Speak the LLM's reply back via TTS, pacing chunks at carrier rate.
5. Barge-out the TTS the moment the user starts talking (local VAD).

Phone calls are the canonical first consumer. The voice-agent /
wake-word plugin will be the second. The engine doesn't know about
either — it consumes a ``ConversationSession`` (modality-specific
audio I/O + events), a ``ConversationConfig`` (system prompt, opening
policy, brain-tool provider, observability callbacks), and returns a
``ConversationOutcome``.

This module deliberately contains no carrier code, no persistence, no
chat-poster code. All that lives in the wrappers that call into the
engine. The split is concrete enough that a wrapper that doesn't
persist anything (an ephemeral voice prompt, say) is just a wrapper
with no-op callbacks.
"""

from __future__ import annotations

import asyncio
import audioop
import logging
import random
from typing import Any

from gilbert.interfaces.ai import (
    AIProvider,
    AIResponse,
    AISamplingProvider,
    Message,
    MessageRole,
)
from gilbert.interfaces.conversation import (
    BrainToolResult,
    ConversationConfig,
    ConversationContext,
    ConversationOutcome,
    ConversationSession,
    ConversationStatus,
    OpeningBehavior,
    set_current_conversation_ctx,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.transcription import (
    AudioEncoding,
    AudioFormat as TranscriptionAudioFormat,
    FinalTranscript,
    SpeechEnded,
    PartialTranscript,
    SpeechStarted,
    StreamConfig,
    StreamingTranscriber,
)
from gilbert.interfaces.tts import (
    AudioFormat as TTSAudioFormat,
    SynthesisRequest,
    TTSProvider,
)

logger = logging.getLogger(__name__)


# ── Status-value normalization ────────────────────────────────────────
#
# Sessions are modality-specific (a ``CallSession`` carries
# ``CallStatus`` values like "ringing"; a voice-agent session would
# carry its own enum). The engine duck-types on ``.status`` and
# normalizes the string value into ``ConversationStatus``.

_TERMINAL_STATUS_VALUES: frozenset[str] = frozenset(
    {"hung_up", "failed", "ended"}
)
_ACTIVE_STATUS_VALUES: frozenset[str] = frozenset(
    {"connected", "active"}
)


def _status_value(event: Any) -> str | None:
    """Pull a normalized status string off whatever status-bearing event
    the session emitted. Returns ``None`` for events that don't carry a
    status (DTMF, application errors, future modality events)."""
    status = getattr(event, "status", None)
    if status is None:
        return None
    if hasattr(status, "value"):
        return str(status.value)
    return str(status)


def _normalize_status(value: str) -> ConversationStatus | None:
    """Map a raw status string onto the generic ``ConversationStatus``."""
    if value in _TERMINAL_STATUS_VALUES:
        if value == "failed":
            return ConversationStatus.FAILED
        return ConversationStatus.ENDED
    if value in _ACTIVE_STATUS_VALUES:
        return ConversationStatus.ACTIVE
    return None


# ── Speaking-state book for barge-in ─────────────────────────────────


class _Speaking:
    """Per-conversation flag set the engine consults when deciding
    whether a brand-new inbound speech burst should cancel an
    in-flight TTS playback."""

    __slots__ = ("active", "cancelled", "generation", "cancel_event")

    def __init__(self) -> None:
        self.active = False
        self.cancelled = False
        # Bumped on each "we want to speak" attempt so a stale cancel
        # from an old utterance can't poison the next one. Compared
        # against a per-loop snapshot in the TTS chunk-writer.
        self.generation = 0
        # Wakes a sleeping wait-for-playback the moment a barge-in
        # cancellation fires, instead of letting the engine sleep
        # for the full estimated duration before noticing. Re-created
        # at the start of each ``_speak_text`` so a stale signal
        # from a prior utterance can't fire on a new one.
        self.cancel_event: asyncio.Event = asyncio.Event()


# ── Monotonic clock for per-conversation timestamps ──────────────────


class _MonotonicClock:
    """Seconds-since-construction clock for transcript timestamps.

    Used for the transcript-turn ``ts_seconds`` field. The wrapper
    persists these directly so the SPA can replay turns against the
    eventual recorded audio.
    """

    def __init__(self) -> None:
        self._start = asyncio.get_event_loop().time()

    def now(self) -> float:
        return asyncio.get_event_loop().time() - self._start


# ── Audio pump with local VAD ────────────────────────────────────────


async def _pump_audio_to_stt(
    audio_in: Any,
    stream: Any,
    on_speech_detected: Any = None,
    *,
    input_is_mulaw: bool = True,
) -> None:
    """Read mulaw-8k chunks from the session and feed PCM-16 to the
    transcriber. Decodes per-chunk so latency stays at the chunk
    boundary instead of buffering.

    Also runs a tiny local VAD on the PCM stream and calls
    ``on_speech_detected()`` when sustained-energy speech is
    detected. This is the engine's primary barge-in signal because
    Scribe Realtime's server-side VAD only emits
    ``partial_transcript`` / ``committed_transcript`` after the user
    pauses — useless during a continuous user-and-Gilbert overlap
    where the user keeps talking right through Gilbert's TTS.

    ``audioop.ulaw2lin`` is deprecated in 3.13 but still functional.
    Replace with ``soxr`` or a vendored C helper if it gets removed.
    """
    pump_count = 0
    logger.info("audio pump: starting (input_is_mulaw=%s)", input_is_mulaw)
    # Local VAD state — rolling RMS over the last N=10 chunks (200ms
    # at 50fps). Threshold tuned for an 8 kHz mulaw → 16-bit PCM
    # stream: silence RMS sits around 0-200, normal phone speech is
    # 1500-6000. 800 is conservative — high enough to ignore line
    # noise / breath / fans, low enough to catch a quiet "stop."
    _VAD_RMS_THRESHOLD = 800
    _VAD_WINDOW_FRAMES = 10
    rms_window: list[int] = []
    # Once we've fired the callback for one barge-in window, suppress
    # further fires for this many frames so we don't spam it during
    # a single user utterance. ~1s gap before we'd consider firing
    # again (which only matters if the brain didn't actually
    # cancel — the callback itself is idempotent, this is just for
    # log hygiene).
    suppress_until = 0
    try:
        async for chunk in audio_in:
            # Phone-call sessions yield mulaw 8 kHz (carrier wire
            # format) and we decode here. Voice-agent / browser-mic
            # sessions yield PCM_S16LE already; pass-through.
            pcm = audioop.ulaw2lin(chunk, 2) if input_is_mulaw else chunk
            await stream.send(pcm)
            pump_count += 1

            try:
                rms = audioop.rms(pcm, 2)
            except Exception:
                rms = 0
            rms_window.append(rms)
            if len(rms_window) > _VAD_WINDOW_FRAMES:
                rms_window.pop(0)
            if (
                on_speech_detected is not None
                and pump_count > suppress_until
                and len(rms_window) >= _VAD_WINDOW_FRAMES
                and sum(1 for r in rms_window if r > _VAD_RMS_THRESHOLD)
                >= int(_VAD_WINDOW_FRAMES * 0.7)
            ):
                avg_rms = sum(rms_window) // len(rms_window)
                logger.info(
                    "local VAD: speech detected (avg_rms=%d over last %d frames)",
                    avg_rms,
                    _VAD_WINDOW_FRAMES,
                )
                try:
                    on_speech_detected()
                except Exception:
                    logger.debug("on_speech_detected raised", exc_info=True)
                suppress_until = pump_count + 50

            # Heartbeat: ~1/sec at the 50fps inbound cadence. Confirms
            # the pump is keeping up with ingest during a TTS burst.
            # Includes the rolling RMS so we can tell at a glance
            # whether the inbound audio is silent (mic mute, wrong
            # source, AEC nuking everything) vs. real speech that's
            # just below the local-VAD threshold.
            if pump_count % 50 == 0:
                avg_rms = (
                    sum(rms_window) // len(rms_window)
                    if rms_window
                    else 0
                )
                peak_rms = max(rms_window) if rms_window else 0
                logger.info(
                    "audio pump → STT: chunks_forwarded=%d "
                    "avg_rms=%d peak_rms=%d (VAD thresh=%d)",
                    pump_count,
                    avg_rms,
                    peak_rms,
                    _VAD_RMS_THRESHOLD,
                )
    except asyncio.CancelledError:
        logger.info("audio pump: cancelled after %d chunks", pump_count)
        raise
    except Exception:
        # Promoted from DEBUG to WARNING — the pump silently dying
        # on a Scribe disconnect would leave the listen_loop unable
        # to recover and the user-visible symptom is "voice agent
        # stuck — no transcripts ever come back."
        logger.warning(
            "audio pump: ended unexpectedly after %d chunks (likely "
            "Scribe stream closed; the listen_loop will end on its "
            "own when the events iterator returns)",
            pump_count,
            exc_info=True,
        )
    else:
        logger.info(
            "audio pump: ended cleanly after %d chunks (audio_in "
            "iterator exhausted)",
            pump_count,
        )


# ── The engine service ───────────────────────────────────────────────


class VoiceBrainService(Service):
    """Generic conversation-loop engine.

    Capability provided: ``voice_brain``. Other services (phone-call
    service today, voice-agent plugin tomorrow) resolve this and call
    ``run_conversation(session, config)``.

    Capabilities consumed: ``ai_chat``, ``text_to_speech``, and
    ``speech_to_text`` — same providers other audio services use.
    """

    def __init__(self) -> None:
        self._resolver: ServiceResolver | None = None
        self._ai: AISamplingProvider | None = None
        # ``AIProvider`` is the multi-round agentic surface — used when
        # ``ConversationConfig.use_full_ai_service`` is True so the
        # brain can call knowledge.search / MCP tools / agent dispatch
        # / etc. via the AI service's standard tool aggregation +
        # multi-round loop. ``AISamplingProvider.complete_one_shot``
        # stays the path for single-round phone-call brains.
        self._ai_chat: AIProvider | None = None
        self._tts: TTSProvider | None = None
        self._transcription: StreamingTranscriber | None = None

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="voice_brain",
            capabilities=frozenset({"voice_brain"}),
            requires=frozenset(
                {"ai_chat", "text_to_speech", "speech_to_text"}
            ),
            optional=frozenset(),
            toggleable=False,
        )

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver
        ai = resolver.get_capability("ai_chat")
        if isinstance(ai, AISamplingProvider):
            self._ai = ai
        # The same service object also satisfies ``AIProvider`` (with
        # the agentic ``chat()`` surface). Double-narrowing — the
        # service implements both protocols.
        if isinstance(ai, AIProvider):
            self._ai_chat = ai
        tts_svc = resolver.get_capability("text_to_speech")
        if isinstance(tts_svc, TTSProvider):
            self._tts = tts_svc
        st_svc = resolver.get_capability("speech_to_text")
        if isinstance(st_svc, StreamingTranscriber):
            self._transcription = st_svc
        logger.info(
            "Voice brain service started (ai=%s tts=%s stt=%s)",
            "✓" if self._ai else "✗",
            "✓" if self._tts else "✗",
            "✓" if self._transcription else "✗",
        )

    async def stop(self) -> None:
        pass

    # --- Public API ---------------------------------------------------

    async def run_conversation(
        self,
        session: ConversationSession,
        config: ConversationConfig,
    ) -> ConversationOutcome:
        """Run a conversation to completion.

        Implements ``ConversationEngine``. Returns when:
        - A terminal status event arrives on the session
        - A brain tool returns ``END_CONVERSATION`` / ``ESCALATE``
        - The watchdog hits ``max_conversation_seconds``

        Doesn't touch persistence or carrier APIs. The wrapper's
        callbacks (``on_status_change``, ``on_transcript_turn``,
        ``on_llm_turn``) are where modality-specific behaviour goes.
        """
        if self._ai is None or self._tts is None:
            raise RuntimeError(
                "voice_brain not initialized — AI or TTS provider missing"
            )

        log = logger.getChild(f"conv:{session.session_id}")
        stop = asyncio.Event()
        speaking = _Speaking()
        messages: list[Message] = list(config.priming_messages)
        outcome: dict[str, Any] = {}
        failure_reason = ""
        spoke_at_all = False
        clock = _MonotonicClock()
        # Serializes ``_think_and_speak`` invocations across all
        # triggers — STT-driven (listen loop) and wrapper-injected
        # (synthetic-turn loop). Without this, an operator directive
        # arriving while the LLM is mid-chat would fire a parallel
        # ai.chat() call and double-speak responses.
        think_speak_lock = asyncio.Lock()
        # Track whether the opening utterance has happened (either via
        # the fallback timer or the listen-loop reacting to inbound
        # speech). Used by the WAIT_FOR_REMOTE opening policy's latch.
        already_spoke = False
        # Set True the moment STT shows ANY voice activity from the
        # remote — partial transcript, final transcript, or Scribe's
        # SpeechStarted signal. The WAIT_FOR_REMOTE fallback timer
        # checks this before firing the proactive opener so we don't
        # double-greet when Scribe takes >fallback_timeout_seconds
        # to commit the user's first utterance. Symptom without
        # this flag: phone call answered, remote says "Hello?",
        # Scribe takes 8s to commit it, fallback fires at 4s and
        # speaks the opener, THEN listen-loop responds to "Hello?"
        # with another greeting — caller hears Gilbert greet twice
        # back-to-back.
        remote_started_talking = False

        # ── helpers — none of these touch persistence ─────────────────

        async def _record_turn(who: str, text: str) -> None:
            ts = clock.now()
            if config.on_transcript_turn is not None:
                await config.on_transcript_turn(who, text, ts)

        async def _publish_event_via_provider(
            event_type: str, data: dict[str, Any]
        ) -> None:
            # Tool providers fire ``publish_event`` for their own
            # domain. The engine wires it through to the wrapper's
            # ``on_status_change`` only when the event LOOKS LIKE a
            # status event — otherwise it's modality-specific and we
            # rely on the wrapper to subscribe to its own bus events
            # the conventional way.
            return None  # passthrough — wrappers can override if they need it

        def _make_brain_ctx() -> ConversationContext:
            return ConversationContext(
                session=session,
                outcome=outcome,
                failure_reason=failure_reason,
                record_turn=_record_turn,
                publish_event=_publish_event_via_provider,
            )

        async def _set_status(
            status: ConversationStatus, reason: str = ""
        ) -> None:
            if config.on_status_change is not None:
                await config.on_status_change(status, reason)

        # ── the brain itself ───────────────────────────────────────────

        # Tracks the AI service conversation_id when running through
        # ai.chat() — we mint one on the first call and reuse it for
        # subsequent turns so history accumulates server-side. Stays
        # None in complete_one_shot mode where the engine owns
        # messages directly.
        chat_conv_id: str | None = None

        # Flips to True after the first ``_think_and_speak_via_chat``
        # call returns. We use this to suppress the filler ("hmm,
        # let me check…") on the SPEAK_FIRST opener — no user is
        # waiting yet, so a filler just delays the greeting, and
        # cold-cache TTFB on the first chat() call almost always
        # exceeds the filler threshold even for a trivial response.
        opener_done = False

        async def _speak_text(text: str, *, fire_done_event: bool = True) -> None:
            """Synthesize ``text`` and write it to the session's audio
            out. Shared between both think-and-speak paths.

            ``fire_done_event`` controls whether ``on_speaking_done``
            is invoked at end-of-playback. The voice-agent wrapper
            uses that callback to arm the dormant-silence countdown
            ("Gilbert finished talking, user's turn now"). For the
            engine-driven filler ("hmm, let me check…") that fires
            DURING LLM thinking, we explicitly suppress the callback
            because we're NOT done responding — the real answer
            still has to play after the chat() task completes. If
            we fired the callback at the filler's end, the silence
            monitor would arm during LLM thinking and trip dormant
            mid-response.
            """
            nonlocal spoke_at_all
            if not text or self._tts is None:
                return
            out_fmt = (
                config.tts_output_format
                if config.tts_output_format is not None
                else TTSAudioFormat.MULAW_8000
            )
            try:
                synth = await self._tts.synthesize(
                    SynthesisRequest(
                        text=text,
                        voice_id="",
                        output_format=out_fmt,
                    )
                )
            except Exception:
                log.exception("TTS synthesize failed")
                return
            audio = synth.audio
            log.info(
                "TTS synth complete — format=%s bytes=%d "
                "first_8_hex=%s last_8_hex=%s zero_ratio=%.2f text_chars=%d",
                synth.format,
                len(audio),
                audio[:8].hex(),
                audio[-8:].hex() if len(audio) >= 8 else "",
                (audio.count(b"\xff") + audio.count(b"\x7f"))
                / max(len(audio), 1),
                len(text),
            )
            speaking.active = True
            speaking.cancelled = False
            # Fresh cancel event per utterance so a barge-in signal
            # from a previous utterance can't fire on this one.
            speaking.cancel_event = asyncio.Event()
            generation = speaking.generation = speaking.generation + 1
            spoke_at_all = True
            write_start = asyncio.get_event_loop().time()
            try:
                chunk_size = 160
                chunks_written = 0
                pace = 0.02 if config.tts_realtime_pacing else 0.0
                for i in range(0, len(audio), chunk_size):
                    if (
                        speaking.cancelled
                        or generation != speaking.generation
                        or stop.is_set()
                    ):
                        break
                    await session.audio_out.write(audio[i : i + chunk_size])
                    chunks_written += 1
                    if pace > 0:
                        await asyncio.sleep(pace)
                log.info(
                    "TTS playback done — chunks_written=%d bytes=%d "
                    "wall_seconds≈%.2f (cancelled=%s pace=%.3fs)",
                    chunks_written,
                    chunks_written * chunk_size,
                    chunks_written * pace,
                    speaking.cancelled,
                    pace,
                )
                try:
                    await session.audio_out.flush()
                except Exception:
                    log.debug("audio_out.flush raised", exc_info=True)
            finally:
                speaking.active = False
            # When tts_realtime_pacing is False (voice-agent / browser
            # sessions), the loop above writes every chunk back-to-back
            # without sleeping — Browser plays the whole clip in one
            # shot from a data URL, and pacing it at 20ms/chunk would
            # stretch a 22s clip into 44s of buffering. But that means
            # ``on_speaking_done`` fires the moment we dispatch the
            # play event, NOT when the browser actually finishes
            # playing. The voice-agent silence monitor then arms its
            # dormancy countdown 10-15s too early and dropped the
            # session into dormant WHILE Gilbert was mid-sentence on
            # long answers. Fix: estimate playback duration from the
            # text length (ElevenLabs paces ~10 chars/sec for normal
            # speech) and sleep for the remainder before firing
            # ``on_speaking_done``. Cancelled / barge-in skips the
            # wait — the user is talking, the silence timer's about
            # to reset anyway. Realtime-paced (phone) sessions
            # already wait the right amount via the chunk-sleep so
            # this is effectively a no-op for them.
            if (
                not speaking.cancelled
                and not stop.is_set()
                and not config.tts_realtime_pacing
            ):
                # ~10 chars/sec is what ElevenLabs naturally hits;
                # use 9 to err slightly long so a quick answer
                # doesn't trip dormant immediately on a borderline
                # case. Min 1s so tiny clips (the "hmm" filler)
                # still get a small wait.
                synth_duration = synth.duration_seconds
                if synth_duration is None:
                    synth_duration = max(1.0, len(text) / 9.0)
                elapsed = asyncio.get_event_loop().time() - write_start
                remaining = synth_duration - elapsed
                if remaining > 0.05:
                    log.info(
                        "TTS waiting for browser playback — "
                        "estimated %.1fs total, %.1fs elapsed, "
                        "sleeping up to %.1fs before on_speaking_done",
                        synth_duration,
                        elapsed,
                        remaining,
                    )
                    # Race the playback estimate against a barge-in
                    # cancellation. Without this, an interrupt
                    # ("Oh.", "Stop.") arrives while we're snoozing
                    # for the full estimated duration — the user
                    # hears Gilbert finish his sentence before
                    # responding to their interjection. Waking on
                    # cancel_event lets ``on_speaking_done`` fire
                    # right away (timer resets, dormant countdown
                    # starts) AND lets the listen-loop's
                    # _think_and_speak call proceed without
                    # waiting for a stale TTS to "finish."
                    try:
                        await asyncio.wait_for(
                            speaking.cancel_event.wait(),
                            timeout=remaining,
                        )
                        log.info(
                            "TTS playback wait cancelled by barge-in"
                        )
                    except TimeoutError:
                        # Normal completion — playback estimate
                        # elapsed without a cancel.
                        pass
                    except asyncio.CancelledError:
                        # The conversation was being torn down
                        # while we waited — fall through and let
                        # the caller handle teardown.
                        pass
            # End-of-utterance signal for the wrapper. Voice-agent uses
            # this to reset its silence timer at the moment Gilbert
            # quiets down rather than when he STARTS speaking (which is
            # what the transcript-turn event fires at). Without this
            # distinction, a 12-second LLM answer would let the
            # 10-second silence elapse mid-sentence and drop the
            # session into dormant while Gilbert was still talking.
            if fire_done_event and config.on_speaking_done is not None:
                try:
                    await config.on_speaking_done()
                except Exception:
                    log.debug("on_speaking_done callback raised", exc_info=True)

        async def _think_and_speak_via_chat(user_text: str) -> None:
            """LLM turn via ``AIProvider.chat()`` — the agentic loop
            with the full Gilbert tool catalog (knowledge.search,
            MCP, agent dispatch, scheduler, etc.). The ContextVar
            is set so brain-tool ToolProviders (voice-agent's
            end_conversation, etc.) can find the active session.

            If ``config.filler_threshold_seconds`` > 0 and ``chat()``
            doesn't return within that window, the engine speaks one
            of ``config.filler_phrases`` ("hmm, let me check") via
            TTS so the user knows we heard them while tools / LLM
            are still working. The real response plays normally
            after chat() returns.
            """
            nonlocal chat_conv_id, opener_done
            if self._ai_chat is None:
                log.warning("ai_chat AIProvider missing — cannot respond")
                return

            ctx = _make_brain_ctx()
            set_current_conversation_ctx(ctx)
            try:
                # Kick chat() off as a task so we can race a filler
                # timer against it. ``asyncio.shield`` keeps
                # ``wait_for``'s timeout from cancelling the chat
                # itself — we only want the timeout to *notice*
                # slowness, not abort the LLM mid-run.
                chat_task: asyncio.Task[Any] = asyncio.create_task(
                    self._ai_chat.chat(
                        user_message=user_text,
                        conversation_id=chat_conv_id,
                        system_prompt=config.system_prompt,
                        source=config.source,
                    )
                )

                filler_enabled = (
                    opener_done
                    and config.filler_threshold_seconds > 0.0
                    and bool(config.filler_phrases)
                )

                result: Any
                if filler_enabled:
                    try:
                        result = await asyncio.wait_for(
                            asyncio.shield(chat_task),
                            timeout=config.filler_threshold_seconds,
                        )
                    except asyncio.TimeoutError:
                        # LLM hasn't returned in time — speak a
                        # filler, record it as a turn (so the
                        # transcript shows what the user heard),
                        # then await the real result. The filler is
                        # synth+play, which itself takes ~1-2s; by
                        # the time it finishes, chat_task may
                        # already be done.
                        #
                        # Race-guard: wait_for's timeout fires when
                        # the timer expires, but scheduling can let
                        # chat_task actually finish a few ms before
                        # we get woken up. In that case we'd speak
                        # a filler AFTER the LLM was already done —
                        # particularly bad when the LLM called
                        # hang_up with no text (the filler becomes
                        # Gilbert's only utterance before the call
                        # drops). Skip the filler if chat_task is
                        # already done by the time we observe the
                        # timeout.
                        if chat_task.done():
                            log.debug(
                                "filler suppressed — chat_task "
                                "completed during timeout window"
                            )
                            result = chat_task.result()
                        else:
                            filler = random.choice(config.filler_phrases)
                            log.info(
                                "LLM slow (>%.1fs) — speaking filler %r",
                                config.filler_threshold_seconds,
                                filler,
                            )
                            await _record_turn("us", filler)
                            # fire_done_event=False so the silence
                            # monitor doesn't think we're done talking
                            # — the real answer still has to play.
                            await _speak_text(filler, fire_done_event=False)
                            result = await chat_task
                else:
                    result = await chat_task
                # First turn (opener) just completed — future turns
                # are eligible for the filler.
                opener_done = True
            except Exception:
                log.exception("ai.chat() failed")
                return
            finally:
                set_current_conversation_ctx(None)

            chat_conv_id = result.conversation_id
            text = (result.response_text or "").strip()
            tool_names = [
                tool.get("tool_name", "")
                for r in (result.rounds or [])
                for tool in (r.get("tools", []) if isinstance(r, dict) else [])
            ]
            log.info(
                "LLM turn (chat): text_chars=%d rounds=%d tools=%s",
                len(text),
                len(result.rounds or []),
                tool_names,
            )
            if config.on_llm_turn is not None:
                try:
                    await config.on_llm_turn(text, tool_names)
                except Exception:
                    log.debug("on_llm_turn callback raised", exc_info=True)

            if text:
                await _record_turn("us", text)
                await _speak_text(text)

            # Tools (e.g. ``end_conversation``, ``hang_up``) set
            # this flag through the ContextVar-shared
            # ``ConversationContext``. Inspecting the outcome dict
            # after the chat() call lets the engine terminate
            # cleanly.
            if ctx.outcome.get("end_requested"):
                # Fallback goodbye — if the LLM called a hang-up
                # tool but didn't bother to generate any goodbye
                # text first (it's instructed to, but doesn't
                # always comply), speak a default phrase so the
                # remote / user doesn't get dead air → dial tone.
                # Skip if Gilbert already said something in the
                # final turn (his goodbye is presumably in ``text``).
                if not text and config.default_goodbye_phrases:
                    goodbye = random.choice(
                        config.default_goodbye_phrases
                    )
                    log.info(
                        "end-requested with no final text — "
                        "speaking default goodbye %r",
                        goodbye,
                    )
                    await _record_turn("us", goodbye)
                    await _speak_text(goodbye)
                outcome.update(ctx.outcome)
                log.info("brain tool requested end-of-conversation")
                stop.set()
                try:
                    await session.end_session()
                except Exception:
                    log.debug("end_session cleanup error", exc_info=True)

        async def _think_and_speak(user_text: str | None = None) -> None:
            """One LLM turn → optional speech → optional tool dispatch.

            Acquires ``think_speak_lock`` for the entire turn so STT-
            driven calls (from the listen loop) and synthetic-turn
            calls (from the wrapper-injected queue) can't run in
            parallel — that would race two concurrent ai.chat()
            invocations and double-speak responses.
            """
            async with think_speak_lock:
                await _think_and_speak_inner(user_text)

        async def _think_and_speak_inner(
            user_text: str | None = None
        ) -> None:
            """Lock-free body of ``_think_and_speak`` — split out so
            paths that already hold the lock can call without
            re-acquiring (none currently, but the split keeps the
            recursion-safety contract explicit)."""
            nonlocal spoke_at_all
            if self._ai is None or self._tts is None:
                log.warning("AI or TTS missing — cannot respond")
                return

            # Voice-agent path: full Gilbert tool ecosystem via
            # ai.chat(). The engine doesn't manage the messages list;
            # the AI service does (persisted by conversation_id). The
            # caller must pass ``user_text`` — for the SPEAK_FIRST
            # opener, the wrapper provides a synthetic cue.
            if config.use_full_ai_service:
                if not user_text:
                    log.warning(
                        "use_full_ai_service=True but no user_text given; "
                        "skipping turn (this is a bug in the caller — pass "
                        "a priming string for the opener cold-open)."
                    )
                    return
                await _think_and_speak_via_chat(user_text)
                return

            # Phone-call path (legacy): complete_one_shot + BrainToolProvider.
            response: AIResponse
            try:
                response = await self._ai.complete_one_shot(
                    messages=messages,
                    system_prompt=config.system_prompt,
                    max_tokens=600,
                    tools_override=config.brain_tool_provider.get_brain_tools(),
                )
            except Exception:
                log.exception("LLM call failed")
                return

            tool_names = [tc.tool_name for tc in response.message.tool_calls]
            log.info(
                "LLM turn: text_chars=%d tools=%s",
                len(response.message.content or ""),
                tool_names,
            )
            if config.on_llm_turn is not None:
                try:
                    await config.on_llm_turn(
                        response.message.content or "", tool_names
                    )
                except Exception:
                    log.debug("on_llm_turn callback raised", exc_info=True)

            text = response.message.content.strip()
            if not text and not response.message.tool_calls:
                return

            # Fallback for the misbehaving "tool-only" case. The brain
            # tools are documented as bookkeeping that don't speak on
            # their own; the LLM is supposed to put a spoken line in
            # the message content alongside the tool. If it forgets
            # (Sonnet occasionally does), we'd otherwise dispatch the
            # tool against dead air. Generate a generic-but-safe line
            # for ``hang_up`` / ``confirm_and_end`` so the conversation
            # doesn't end silently.
            if not text and response.message.tool_calls:
                names = {tc.tool_name for tc in response.message.tool_calls}
                if "hang_up" in names:
                    text = "Thanks so much, have a great day!"
                elif "confirm_and_end" in names:
                    summary_args: dict[str, Any] = {}
                    for tc in response.message.tool_calls:
                        if tc.tool_name == "confirm_and_end":
                            summary_args = tc.arguments.get("summary") or {}
                            break
                    if isinstance(summary_args, dict) and summary_args:
                        bits = ", ".join(
                            f"{k.replace('_', ' ')}: {v}"
                            for k, v in summary_args.items()
                        )
                        text = f"Just to confirm — {bits}. Does that sound right?"
                    else:
                        text = "Just to confirm what we agreed on — does that sound right?"
                if text:
                    log.warning(
                        "LLM emitted tool-only response; using fallback text: %r",
                        text,
                    )

            if text:
                messages.append(Message(role=MessageRole.ASSISTANT, content=text))
                await _record_turn("us", text)
                spoke_at_all = True

                # Pick the output format. Phone calls leave
                # ``tts_output_format`` unset → MULAW_8000 (Telnyx wire
                # format). Voice-agent browser sessions configure MP3
                # so the SPA can play the buffered clip via
                # HTMLAudioElement.
                out_fmt = (
                    config.tts_output_format
                    if config.tts_output_format is not None
                    else TTSAudioFormat.MULAW_8000
                )
                try:
                    synth = await self._tts.synthesize(
                        SynthesisRequest(
                            text=text,
                            voice_id="",
                            output_format=out_fmt,
                        )
                    )
                except Exception:
                    log.exception("TTS synthesize failed")
                    return

                audio = synth.audio
                log.info(
                    "TTS synth complete — format=%s bytes=%d "
                    "first_8_hex=%s last_8_hex=%s zero_ratio=%.2f text_chars=%d",
                    synth.format,
                    len(audio),
                    audio[:8].hex(),
                    audio[-8:].hex() if len(audio) >= 8 else "",
                    (audio.count(b"\xff") + audio.count(b"\x7f"))
                    / max(len(audio), 1),
                    len(text),
                )

                speaking.active = True
                speaking.cancelled = False
                generation = speaking.generation = speaking.generation + 1
                try:
                    chunk_size = 160  # 20ms mulaw @ 8kHz mono
                    chunks_written = 0
                    # Voice-agent / browser sessions don't need realtime
                    # pacing — the browser plays the whole clip in one
                    # shot. Pacing MP3 bytes at the mulaw rate added
                    # ~30 seconds of gratuitous delay before the browser
                    # got the audio. Carrier sessions (Telnyx) keep the
                    # 20ms-per-chunk pacing — without it the wire
                    # buffer overruns and Telnyx disables barge-in.
                    pace = 0.02 if config.tts_realtime_pacing else 0.0
                    for i in range(0, len(audio), chunk_size):
                        if (
                            speaking.cancelled
                            or generation != speaking.generation
                            or stop.is_set()
                        ):
                            break
                        await session.audio_out.write(
                            audio[i : i + chunk_size]
                        )
                        chunks_written += 1
                        if pace > 0:
                            await asyncio.sleep(pace)
                    log.info(
                        "TTS playback done — chunks_written=%d bytes=%d "
                        "wall_seconds≈%.2f (cancelled=%s pace=%.3fs)",
                        chunks_written,
                        chunks_written * chunk_size,
                        chunks_written * pace,
                        speaking.cancelled,
                        pace,
                    )
                    # End-of-utterance signal. Real-time sinks (Telnyx)
                    # ignore it; turn-taking sinks (browser tab) use
                    # this to flush their buffer and dispatch a single
                    # clip per utterance.
                    try:
                        await session.audio_out.flush()
                    except Exception:
                        log.debug("audio_out.flush raised", exc_info=True)
                finally:
                    speaking.active = False

            # Dispatch any tool calls now that we've spoken (or skipped
            # speaking for a pure tool turn). END_CONVERSATION /
            # ESCALATE drop the line.
            ctx = _make_brain_ctx()
            for tc in response.message.tool_calls:
                handled = await config.brain_tool_provider.handle_brain_tool(
                    tc.tool_name, tc.arguments, ctx
                )
                if handled in (
                    BrainToolResult.END_CONVERSATION,
                    BrainToolResult.ESCALATE,
                ):
                    stop.set()
                    try:
                        await session.end_session()
                    except Exception:
                        log.debug("end_session cleanup error", exc_info=True)
                    return

        # ── opening behavior ──────────────────────────────────────────

        async def _open_proactively() -> None:
            nonlocal already_spoke
            if already_spoke:
                return
            already_spoke = True
            # For the chat()-driven path we need to pass SOMETHING as
            # the user message — ai.chat() requires a non-empty
            # message. Use a synthetic system cue that tells the LLM
            # to greet the user.
            opener_cue = "(SYSTEM) Voice session activated. Greet me briefly."
            await _think_and_speak(opener_cue if config.use_full_ai_service else None)

        async def _wait_then_open() -> None:
            """Fallback for WAIT_FOR_REMOTE: cold-open after timeout
            UNLESS the remote has started talking. If Scribe has
            seen voice activity by now we let the listen loop
            handle the response — opening proactively here would
            stack a duplicate greeting in front of the listen-loop's
            response."""
            try:
                await asyncio.sleep(
                    config.opening_policy.fallback_timeout_seconds
                )
            except asyncio.CancelledError:
                return
            if already_spoke or stop.is_set():
                return
            if remote_started_talking:
                log.info(
                    "opening: fallback skipped — remote is talking, "
                    "letting listen loop handle the response"
                )
                return
            log.info(
                "opening: remote silent %.1fs after active — speaking proactively",
                config.opening_policy.fallback_timeout_seconds,
            )
            await _open_proactively()

        # ── three loops ───────────────────────────────────────────────

        async def _status_loop() -> None:
            log.info("status_loop: starting")
            try:
                async for event in session.events:
                    log.info(
                        "status_loop: event %s",
                        type(event).__name__,
                    )
                    raw_status = _status_value(event)
                    if raw_status is None:
                        # Non-status event — modality-specific. Surface
                        # as a transcript turn so it appears in the
                        # log (DTMF on phone calls etc).
                        ev_repr = repr(event)
                        await _record_turn("system", f"(event: {ev_repr})")
                        continue
                    normalized = _normalize_status(raw_status)
                    reason = getattr(event, "reason", "") or ""
                    await _set_status(
                        normalized or ConversationStatus.PENDING,
                        reason,
                    )
                    if normalized == ConversationStatus.ACTIVE:
                        if (
                            config.opening_policy.behavior
                            == OpeningBehavior.SPEAK_FIRST
                        ):
                            asyncio.create_task(_open_proactively())
                        else:
                            asyncio.create_task(_wait_then_open())
                    if normalized in (
                        ConversationStatus.ENDED,
                        ConversationStatus.FAILED,
                    ):
                        log.info(
                            "status_loop: terminal status %s — setting stop",
                            raw_status,
                        )
                        stop.set()
                        return
                log.info("status_loop: events iterator exhausted (closed)")
            except Exception:
                log.exception("status loop crashed")
                stop.set()

        async def _listen_loop() -> None:
            nonlocal already_spoke, remote_started_talking
            if self._transcription is None:
                log.warning("Transcription unavailable — conversation continues TTS-only")
                outcome["transcription_available"] = False
                return
            # Pick the STT input format. Phone calls leave
            # ``stt_audio_format`` unset → 8 kHz PCM (carrier rate).
            # Voice-agent / browser sessions configure 16 kHz because
            # the mic captures cleanly there.
            stt_fmt = config.stt_audio_format or TranscriptionAudioFormat(
                encoding=AudioEncoding.PCM_S16LE,
                sample_rate=8000,
                channels=1,
            )

            def _on_local_vad_speech() -> None:
                if not speaking.active:
                    return
                speaking.cancelled = True
                speaking.cancel_event.set()
                asyncio.create_task(session.audio_out.clear())
                log.info("local VAD: barge-in cancelling in-flight TTS")

            # Phone-call sessions send mulaw 8kHz over the wire and
            # the pump decodes to PCM. Voice-agent browser sessions
            # send PCM directly and the pump skips the decode. Set the
            # flag based on the configured input format.
            input_is_mulaw = (
                config.audio_input_format is None
                or getattr(config.audio_input_format, "encoding", None)
                != AudioEncoding.PCM_S16LE
            )

            # Outer pause/resume loop: opens STT, processes events
            # until either ``stop`` (terminal) or
            # ``listening_paused`` (transient — wrapper signalled
            # dormancy, e.g. wake-word mode) fires, then loops back
            # to wait for resume + re-open on the next iteration.
            # Without this the engine paid ElevenLabs Scribe per
            # second of stream even while the user was dormant
            # waiting for "Hey Gilbert" — Scribe also dropped the
            # WS after ~15s of idle audio, leaving the engine
            # unable to transcribe ever again after a single
            # dormancy.
            pause_event: asyncio.Event | None = config.listening_paused

            while not stop.is_set():
                # If the wrapper signalled pause, sit out the
                # dormant period before opening any new STT stream.
                # We wait for either resume (event clears) or
                # terminal stop. ``asyncio.Event.wait`` doesn't
                # support a "wait until cleared" operation
                # directly, so we poll the flag with short sleeps.
                if pause_event is not None and pause_event.is_set():
                    log.info("listen loop: paused (no STT stream open)")
                    while pause_event.is_set() and not stop.is_set():
                        try:
                            await asyncio.wait_for(stop.wait(), timeout=0.5)
                        except TimeoutError:
                            pass
                    if stop.is_set():
                        break
                    log.info("listen loop: resuming — reopening STT stream")

                try:
                    stt_stream = await self._transcription.open_stream(
                        StreamConfig(
                            format=stt_fmt,
                            interim_results=True,
                            vad_events=True,
                        )
                    )
                except Exception:
                    log.exception(
                        "Failed to open transcription stream — "
                        "conversation continues TTS-only"
                    )
                    outcome["transcription_available"] = False
                    return

                pump_task = asyncio.create_task(
                    _pump_audio_to_stt(
                        session.audio_in,
                        stt_stream,
                        on_speech_detected=_on_local_vad_speech,
                        input_is_mulaw=input_is_mulaw,
                    )
                )

                # Watcher closes the STT stream when stop fires OR
                # when the pause event fires, so the
                # ``async for ev in stt_stream.events()`` loop
                # below unblocks promptly. Without this the listen
                # loop sits forever on the underlying WS recv()
                # whenever the wrapper requests a pause, defeating
                # the entire purpose of the close/reopen mechanic.
                async def _close_stt_on_signal() -> None:
                    stop_task = asyncio.create_task(stop.wait())
                    waits = [stop_task]
                    if pause_event is not None:
                        pause_task = asyncio.create_task(pause_event.wait())
                        waits.append(pause_task)
                    try:
                        await asyncio.wait(
                            waits, return_when=asyncio.FIRST_COMPLETED
                        )
                    finally:
                        for w in waits:
                            w.cancel()
                    try:
                        await stt_stream.close()
                    except Exception:
                        log.debug(
                            "close-on-signal failed", exc_info=True
                        )

                stop_watcher = asyncio.create_task(_close_stt_on_signal())

                try:
                    async for ev in stt_stream.events():
                        if stop.is_set():
                            break
                        if (
                            pause_event is not None
                            and pause_event.is_set()
                        ):
                            break
                        if isinstance(ev, SpeechStarted):
                            # Scribe-emitted barge-in signal — same
                            # handling as the local-VAD path.
                            # Idempotent. Also marks remote-active so
                            # the WAIT_FOR_REMOTE opener fallback
                            # won't fire on top of an in-flight
                            # response.
                            remote_started_talking = True
                            if speaking.active:
                                speaking.cancelled = True
                                speaking.cancel_event.set()
                                await session.audio_out.clear()
                        elif isinstance(ev, PartialTranscript):
                            # Scribe is mid-transcribing the user.
                            # Flip the remote-active flag so the
                            # opener fallback doesn't fire while
                            # we're already mid-listen.
                            remote_started_talking = True
                        elif isinstance(ev, FinalTranscript):
                            text = ev.text.strip()
                            if not text:
                                continue
                            already_spoke = True
                            remote_started_talking = True
                            await _record_turn("them", text)
                            # Phone-call path: append to the engine's
                            # messages list; complete_one_shot will see it.
                            # Voice-agent path: ai.chat() takes the user
                            # text directly via the ``_think_and_speak``
                            # arg — no engine-side messages list needed.
                            if not config.use_full_ai_service:
                                messages.append(
                                    Message(
                                        role=MessageRole.USER, content=text
                                    )
                                )
                            await _think_and_speak(text)
                        elif isinstance(ev, SpeechEnded):
                            pass
                except Exception:
                    log.exception(
                        "listen loop crashed — conversation continues TTS-only"
                    )
                    outcome["transcription_failed_midcall"] = True
                    # Don't try to reopen on an unexpected crash;
                    # leave the conversation in TTS-only mode and
                    # exit the outer loop. Otherwise a persistently
                    # failing backend would churn forever.
                    pump_task.cancel()
                    stop_watcher.cancel()
                    try:
                        await stt_stream.close()
                    except Exception:
                        pass
                    return
                else:
                    # Async-for completed without an exception.
                    # Two normal causes:
                    #   1) stop fired (terminal close) — we'll
                    #      exit the outer while loop next check.
                    #   2) pause_event fired — we'll loop back,
                    #      wait for resume, and reopen.
                    # Unexpected close (Scribe idle timeout,
                    # network blip) shows up as stop=False AND
                    # pause=False; log it loudly because that
                    # used to silently break the conversation.
                    if stop.is_set():
                        pass
                    elif pause_event is not None and pause_event.is_set():
                        log.info(
                            "listen loop: STT closed (pause requested)"
                        )
                    else:
                        log.warning(
                            "listen loop: STT events iterator "
                            "returned unexpectedly — will attempt "
                            "to reopen (stop=False pause=False)"
                        )
                        outcome["stt_stream_closed_midcall"] = True
                finally:
                    pump_task.cancel()
                    stop_watcher.cancel()
                    try:
                        await stt_stream.close()
                    except Exception:
                        pass

        async def _watchdog() -> None:
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=config.max_conversation_seconds
                )
            except TimeoutError:
                log.warning(
                    "Conversation exceeded %ds cap — forcing end",
                    config.max_conversation_seconds,
                )
                outcome["forced_end_reason"] = "max_duration_exceeded"
                stop.set()
                try:
                    await session.end_session()
                except Exception:
                    pass

        async def _synthetic_turn_loop() -> None:
            """Watch the wrapper-injected synthetic-turn queue and
            run ``_think_and_speak`` for each entry. Used by phone's
            'Direct Gilbert' textbox so operator directives interrupt
            mid-call instead of queueing until the remote next
            speaks.

            Barge-in: if Gilbert is mid-TTS when a directive
            arrives, we set ``speaking.cancelled`` and clear
            audio_out BEFORE awaiting the think_speak_lock. The
            in-flight ``_speak_text`` notices the cancellation,
            breaks out of its chunk-write loop, releases the lock,
            and we're free to run the directive.
            """
            queue = config.inject_synthetic_user_turn_queue
            if queue is None:
                return
            log.info("synthetic_turn_loop: armed")
            while not stop.is_set():
                try:
                    text = await asyncio.wait_for(
                        queue.get(), timeout=0.5
                    )
                except TimeoutError:
                    continue
                except asyncio.CancelledError:
                    return
                if stop.is_set():
                    break
                log.info(
                    "synthetic_turn_loop: dispatching injected turn "
                    "(chars=%d, in-flight TTS=%s)",
                    len(text),
                    speaking.active,
                )
                # Barge-in: cancel any current TTS so the directive
                # takes effect immediately instead of after Gilbert
                # finishes his current sentence.
                if speaking.active:
                    speaking.cancelled = True
                    speaking.cancel_event.set()
                    try:
                        await session.audio_out.clear()
                    except Exception:
                        log.debug(
                            "synthetic_turn: audio_out.clear raised",
                            exc_info=True,
                        )
                try:
                    await _think_and_speak(text)
                except Exception:
                    log.exception(
                        "synthetic_turn: _think_and_speak failed for "
                        "injected text=%r",
                        text[:80],
                    )

        # ── orchestrate ───────────────────────────────────────────────

        started_at = clock.now()
        log.info(
            "voice_brain: entering gather of status/listen/watchdog/"
            "synthetic loops"
        )
        try:
            results = await asyncio.gather(
                _status_loop(),
                _listen_loop(),
                _watchdog(),
                _synthetic_turn_loop(),
                return_exceptions=True,
            )
            log.info(
                "voice_brain: gather returned — results=%s",
                [
                    type(r).__name__ if isinstance(r, BaseException) else "ok"
                    for r in results
                ],
            )
        finally:
            try:
                await session.end_session()
            except Exception:
                log.debug("end_session cleanup error", exc_info=True)

        duration = max(0.0, clock.now() - started_at)
        final_status = (
            ConversationStatus.FAILED
            if outcome.get("transcription_open_failed")
            else ConversationStatus.ENDED
        )
        return ConversationOutcome(
            final_status=final_status,
            duration_seconds=duration,
            outcome=outcome,
            failure_reason=failure_reason,
            spoke_at_all=spoke_at_all,
        )
