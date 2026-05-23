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
from typing import Any

from gilbert.interfaces.ai import (
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
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.transcription import (
    AudioEncoding,
    AudioFormat as TranscriptionAudioFormat,
    FinalTranscript,
    SpeechEnded,
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

    __slots__ = ("active", "cancelled", "generation")

    def __init__(self) -> None:
        self.active = False
        self.cancelled = False
        # Bumped on each "we want to speak" attempt so a stale cancel
        # from an old utterance can't poison the next one. Compared
        # against a per-loop snapshot in the TTS chunk-writer.
        self.generation = 0


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
            pcm = audioop.ulaw2lin(chunk, 2)  # 8-bit µ-law → 16-bit PCM
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
            if pump_count % 50 == 0:
                logger.info(
                    "audio pump → STT: chunks_forwarded=%d",
                    pump_count,
                )
    except Exception:
        logger.debug("audio pump ended", exc_info=True)


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
        # Track whether the opening utterance has happened (either via
        # the fallback timer or the listen-loop reacting to inbound
        # speech). Used by the WAIT_FOR_REMOTE opening policy's latch.
        already_spoke = False

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

        async def _think_and_speak() -> None:
            """One LLM turn → optional speech → optional tool dispatch."""
            nonlocal spoke_at_all
            if self._ai is None or self._tts is None:
                log.warning("AI or TTS missing — cannot respond")
                return

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

                try:
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
            await _think_and_speak()

        async def _wait_then_open() -> None:
            """Fallback for WAIT_FOR_REMOTE: cold-open after timeout."""
            try:
                await asyncio.sleep(
                    config.opening_policy.fallback_timeout_seconds
                )
            except asyncio.CancelledError:
                return
            if not already_spoke and not stop.is_set():
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
            nonlocal already_spoke
            if self._transcription is None:
                log.warning("Transcription unavailable — conversation continues TTS-only")
                outcome["transcription_available"] = False
                return
            try:
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
                log.exception(
                    "Failed to open transcription stream — conversation continues TTS-only"
                )
                outcome["transcription_available"] = False
                return

            def _on_local_vad_speech() -> None:
                if not speaking.active:
                    return
                speaking.cancelled = True
                asyncio.create_task(session.audio_out.clear())
                log.info("local VAD: barge-in cancelling in-flight TTS")

            pump_task = asyncio.create_task(
                _pump_audio_to_stt(
                    session.audio_in,
                    stt_stream,
                    on_speech_detected=_on_local_vad_speech,
                )
            )
            try:
                async for ev in stt_stream.events():
                    if stop.is_set():
                        break
                    if isinstance(ev, SpeechStarted):
                        # Scribe-emitted barge-in signal — same handling
                        # as the local-VAD path. Idempotent.
                        if speaking.active:
                            speaking.cancelled = True
                            await session.audio_out.clear()
                    elif isinstance(ev, FinalTranscript):
                        text = ev.text.strip()
                        if not text:
                            continue
                        already_spoke = True
                        await _record_turn("them", text)
                        messages.append(
                            Message(role=MessageRole.USER, content=text)
                        )
                        await _think_and_speak()
                    elif isinstance(ev, SpeechEnded):
                        pass
            except Exception:
                log.exception(
                    "listen loop crashed — conversation continues TTS-only"
                )
                outcome["transcription_failed_midcall"] = True
            finally:
                pump_task.cancel()
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

        # ── orchestrate ───────────────────────────────────────────────

        started_at = clock.now()
        log.info("voice_brain: entering gather of status/listen/watchdog loops")
        try:
            results = await asyncio.gather(
                _status_loop(),
                _listen_loop(),
                _watchdog(),
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
