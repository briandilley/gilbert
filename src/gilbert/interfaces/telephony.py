"""Telephony backend interface — placing outbound PSTN calls + streaming audio.

Shape mirrors the other "live audio" backends (``TranscriptionBackend``,
``TTSBackend``):

- ABC with ``_registry`` + ``__init_subclass__`` auto-registration so the
  composition root discovers concretes via
  ``TelephonyBackend.registered_backends()`` after a side-effect import.
- ``backend_config_params()`` declares the keys the operator sets in
  ``/settings`` for the chosen backend.
- ``initialize(config)`` / ``close()`` lifecycle hooks.
- One operation: ``place_call(to, from, ...)`` returns a ``CallSession``
  with async iterators for inbound audio + control events and a sink for
  outbound audio, plus a ``hang_up`` action.

Audio everywhere is **8 kHz mono µ-law (G.711)** — that's what the
carriers actually carry. Higher-rate audio gets re-sampled on the way
in/out of the loop; the wire is always mulaw.

This module is pure: only standard library + cross-references inside
``interfaces/``. No HTTP clients, no plugin imports, no service code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar, Protocol, runtime_checkable

from gilbert.interfaces.configuration import ConfigParam

# ``AudioSink`` and the generic conversation primitives live in
# ``interfaces/conversation``. ``CallSession`` is a phone-call-specific
# ``ConversationSession`` — we re-export ``AudioSink`` from here for
# backward compatibility with the carrier-plugin code that still
# imports it from this module.
from gilbert.interfaces.conversation import (
    AudioSink,
    ConversationErrorEvent,
    ConversationSession,
    ConversationStatusEvent,
)

__all__ = [
    "AudioSink",
    "CallBrief",
    "CallErrorEvent",
    "CallEvent",
    "CallSession",
    "CallStatus",
    "CallStatusEvent",
    "DtmfEvent",
    "PhoneCallProvider",
    "TelephonyBackend",
    "TranscriptTurn",
]

# ── Call status / events ─────────────────────────────────────────────


class CallStatus(StrEnum):
    """High-level lifecycle states of a call.

    ``VOICEMAIL`` isn't a backend-emitted state — the call brain decides
    we hit voicemail based on the audio (long silence after greeting +
    classic "leave a message" phrasing). The backend just reports
    ``CONNECTED`` and we infer the rest.
    """

    INITIATED = "initiated"      # API accepted the create-call request
    RINGING = "ringing"          # carrier reports we're ringing the other side
    CONNECTED = "connected"      # the other party picked up
    HUNG_UP = "hung_up"          # call ended cleanly (either side)
    FAILED = "failed"            # never connected (busy, unreachable, error)


@dataclass(frozen=True)
class CallStatusEvent:
    """A call lifecycle transition (ringing → connected → hung_up, etc.)."""

    status: CallStatus
    # Carrier-specific reason code when available (e.g. "user_busy",
    # "no_answer", "call_rejected"). Empty when the carrier didn't
    # provide one or the transition is normal.
    reason: str = ""


@dataclass(frozen=True)
class DtmfEvent:
    """Inbound DTMF tone — the remote end pressed a key.

    Useful for IVRs that ask the remote to confirm input ("press 1 to
    confirm"). Less common on outbound calls but the carriers do
    surface it, so we expose it.
    """

    digit: str  # "0"-"9", "*", or "#"


@dataclass(frozen=True)
class CallErrorEvent:
    """Non-fatal stream-level issue (e.g. transient WebSocket drop the
    backend already recovered from). Logged but doesn't terminate the
    session."""

    message: str
    recoverable: bool = True


CallEvent = CallStatusEvent | DtmfEvent | CallErrorEvent


# ── The session handed out by ``place_call`` ─────────────────────────
#
# ``CallSession`` is a phone-call-specific specialization of the
# generic ``ConversationSession`` defined in
# ``gilbert.interfaces.conversation``. The engine sees it as a
# ``ConversationSession``; the carrier-plugin code (and any caller
# wanting to send DTMF, transfer the call, etc.) treats it as a
# ``CallSession`` and uses the phone-specific extensions.
#
# The ``hang_up`` method is the phone-specific name for
# ``end_session`` — kept as the canonical telephony name. The engine
# expects ``end_session`` so we provide it as a thin alias below.


@dataclass
class CallSession(ConversationSession):
    """An open phone call. Inherits the generic conversation-session
    shape (``session_id``, ``audio_in``, ``audio_out``, ``events``,
    ``end_session``) and adds phone-call-specific bits.

    Single use — close it by calling ``hang_up`` (or the engine-level
    ``end_session``; they're aliases).

    The three audio/event streams are independent producer/consumer
    endpoints:

    - ``audio_in``  — async iterator of mulaw 8 kHz chunks from the remote
    - ``audio_out`` — sink the brain writes our synthesized audio into
    - ``events``    — async iterator of ``ConversationEvent``s, with
      phone-specific ``DtmfEvent`` and ``CallStatusEvent`` mixed in.

    The brain typically spawns one task per stream and joins them on
    hang-up. The backend handles reconnect/retry of the underlying
    transport before surfacing anything terminal; if ``events`` emits a
    ``CallStatusEvent(status=HUNG_UP|FAILED)`` the call is over and the
    streams will close shortly after.
    """

    # Phone-specific aliases / extensions on top of ConversationSession.
    # ``session_id`` is the backend-issued unique call id; we expose
    # it under the legacy name ``call_id`` via a property below for
    # the considerable amount of existing code that reads it that way.

    @property
    def call_id(self) -> str:
        """Phone-specific alias for ``session_id`` (the
        carrier-issued unique call identifier). Kept so the existing
        telephony plugin + service code doesn't have to be rewritten
        in lockstep with the abstraction extraction."""
        return self.session_id

    async def hang_up(self) -> None:
        """Phone-specific alias for ``end_session``. Backends set
        this in their session-construction code (the alias relationship
        is enforced via composition at the carrier layer). Calling
        through to ``end_session`` here covers the fall-through case
        when a backend didn't override; for the real implementations
        the backend's ``hang_up`` IS the underlying carrier action."""
        await self.end_session()


# ── The backend ABC ──────────────────────────────────────────────────


class TelephonyBackend(ABC):
    """Abstract telephony backend (carrier integration).

    Concrete implementations live in plugins (e.g. ``std-plugins/telnyx``)
    and self-register via ``__init_subclass__``. The owning
    ``PhoneCallService`` instantiates whichever one is selected in
    ``phone_call.backend`` config.

    Single-instance per process — calls are tracked by ``call_id``
    internally; ``place_call`` is safe to invoke concurrently.
    """

    _registry: ClassVar[dict[str, type[TelephonyBackend]]] = {}
    backend_name: ClassVar[str] = ""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            TelephonyBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type[TelephonyBackend]]:
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        """Backend-specific config keys (API token, from-number, …)."""
        return []

    @abstractmethod
    async def initialize(self, config: dict[str, object]) -> None:
        """Bring up the backend with operator-supplied config.

        Called once during ``PhoneCallService.start``. Re-called on
        live config changes via the standard ``Configurable`` flow.
        """

    @abstractmethod
    async def close(self) -> None:
        """Tear down. Called on service stop or backend swap."""

    @abstractmethod
    async def place_call(
        self,
        *,
        to_number: str,
        from_number: str,
        call_id: str,
        webhook_token: str,
    ) -> CallSession:
        """Initiate an outbound call.

        Args:
            to_number: E.164 destination ("+13035550100").
            from_number: E.164 caller-ID to present.
            call_id: Gilbert-issued id (used to correlate webhook
                callbacks back to the session that triggered them).
                The backend uses this for its own correlation too.
            webhook_token: opaque token Gilbert generates and stores on
                the call record. The backend stamps it into outgoing
                webhook URLs / media-stream-start metadata so inbound
                events can be authenticated as "really for this call"
                rather than spoofed.

        Returns a ``CallSession`` whose streams are open as soon as the
        carrier accepts the call. Status events flow through
        ``session.events``; the brain doesn't usually await
        ``CONNECTED`` itself — it can start writing greeting audio
        immediately, and the carrier buffers until pickup.
        """


# ── Capability provider protocol ─────────────────────────────────────


@runtime_checkable
class PhoneCallProvider(Protocol):
    """Capability exposed by ``PhoneCallService`` for other services to
    consume (e.g. an agent skill that wants to place a call).

    Kept narrow: most callers should go through the AI tool, not the
    raw API. ``start_call`` returns the call_id immediately; live state
    is observed via the event bus or storage.
    """

    async def start_call(
        self,
        *,
        user_id: str,
        to_number: str,
        brief: str,
        callback_number: str = "",
    ) -> str: ...


# ── Dialog state shared across the brain ─────────────────────────────


@dataclass
class CallBrief:
    """Structured form of the user's natural-language instruction.

    The ``brief_text`` is the verbatim user instruction; the optional
    structured fields are extracted opportunistically by the AI tool
    layer but the brain works fine without them — the system prompt
    just embeds ``brief_text`` directly.
    """

    brief_text: str
    callback_number: str = ""
    extracted: dict[str, str] = field(default_factory=dict)


@dataclass
class TranscriptTurn:
    """One side of the conversation transcript.

    ``who`` is ``"them"`` for the remote party, ``"us"`` for Gilbert.
    ``ts_seconds`` is offset from call start, not wall-clock — useful
    for syncing with the eventual recording playback.
    """

    who: str  # "them" | "us" | "user_intervention" | "system"
    text: str
    ts_seconds: float
