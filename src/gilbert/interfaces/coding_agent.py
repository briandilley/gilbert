"""Coding-agent conduit — backend ABC + capability protocol.

Gilbert acts as a *conduit* between the user (typically over voice
via Mentra glasses or the chat UI) and a coding agent the user runs
on their own machine — OpenCode (``opencode serve``), Claude Code,
etc. Gilbert never writes code or makes editorial judgments; it
just relays messages back and forth so the user can drop a thought
("tell Claude to write tests for the new endpoint") without
breaking focus, and learn what the agent did without watching the
terminal.

Two halves of the contract:

- ``CodingAgentBackend`` (ABC) is what a concrete integration
  implements: send a message into a session, list sessions, report
  whether the integration is ready. Backends are intentionally
  minimal — notification policy, project-alias resolution, and
  voice/TTS routing live in the service layer that wraps them.

- ``CodingConduitProvider`` (runtime_checkable Protocol) is what
  other Gilbert services can discover via
  ``resolver.get_capability("code_conduit")``. Lets the Mentra
  camera tool or a future "morning briefing" pipeline drop a
  message into the agent without depending on a concrete service
  class.

Design notes:

- Backends are loaded as side-effect imports from the owning
  plugin's ``plugin.py``. Multiple backends can coexist in the
  registry; the service layer picks the active one by name via the
  ``backend`` config param (same pattern as ``vision`` / ``ocr`` /
  ``tts``).
- ``send_message`` is fire-and-forget. The voice loop must NEVER
  block on a coding response — agents can take minutes to finish a
  task. The send returns when the agent has *received* the
  message, not when it has *responded*. Phase-2 work will surface
  the responses asynchronously via an event-bus capability.
- ``session_id`` is opaque — backends mint their own format. The
  conduit treats it as an identifier to pin replies/follow-ups to
  the same conversation; it isn't a Gilbert UUID.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from gilbert.interfaces.configuration import ConfigParam

# Severity buckets a ``CodingAgentEvent`` falls into. Tighter than a
# free-form string because consumers (voice-brain, push, frontend
# feed) need to make routing decisions on it without parsing the
# detail blob. See the docstring on ``CodingAgentEvent.kind`` for
# what each one means.
EVENT_KIND_DONE = "done"
EVENT_KIND_ERROR = "error"
EVENT_KIND_ATTENTION = "attention"
EVENT_KIND_INFO = "info"


@dataclass(frozen=True)
class CodingAgentEvent:
    """One inbound event from the coding agent — Gilbert's "the
    coder did a thing" signal.

    Generic by design: each backend (OpenCode, Claude Code, ...)
    translates its native event format into this shape so the
    service layer above doesn't have to learn N different
    protocols. Backends are intentionally minimal — they map
    *what kind* of event happened; consumers decide *what to do*
    about it (TTS interrupt vs. silent log vs. push notification
    vs. SPA feed entry).

    Field ``kind`` is the routing knob. Four buckets:

    - ``"done"`` — the agent finished a task and is back to
      waiting for input. The single most useful notification:
      "Claude finished the test suite." Consumers typically
      surface this when the voice loop is idle so it doesn't
      interrupt an in-progress conversation.
    - ``"error"`` — the agent or runtime errored. Worth
      interrupting for — the user usually needs to step in.
    - ``"attention"`` — the agent is blocked waiting for the
      user (permission prompt, ambiguous instruction, missing
      input). Worth interrupting for — the user needs to answer.
    - ``"info"`` — everything else (tool calls, token deltas,
      diagnostics, installation pings). Default-silent;
      surfaces in the activity feed but not via TTS.

    ``summary`` is the voice-friendly one-liner the TTS layer
    will speak. ``detail`` is the longer string the SPA feed
    renders. Keep ``summary`` under ~80 chars so it fits a
    glasses-display line and doesn't blow up TTS latency.
    """

    session_id: str = ""
    project_path: str = ""
    kind: str = EVENT_KIND_INFO
    summary: str = ""
    detail: str = ""
    timestamp: str = ""
    """ISO-8601 timestamp string from the backend, or ``""`` when
    unavailable. Same string-typed rationale as
    ``CodingAgentSession.last_updated``."""
    raw_type: str = ""
    """The backend's native event-type name (e.g. OpenCode's
    ``session.idle``). Carried through so the SPA feed / debug
    webview can render the raw type alongside the normalized
    ``kind`` — useful when an unknown event was bucketed into
    ``info`` and the operator wants to know what fell through."""


@dataclass(frozen=True)
class CodingAgentSession:
    """One coding-agent session — a thread the user has open with
    their coding agent. Sessions belong to projects; a single
    project may have many sessions over time."""

    session_id: str
    project_path: str = ""
    title: str = ""
    last_updated: str = ""
    """ISO-8601 timestamp string; ``""`` if the backend doesn't
    report one. Stored as a string rather than a ``datetime`` so the
    wire format stays uniform across backends with different
    timezone conventions."""


@dataclass(frozen=True)
class CodingAgentSendResult:
    """Result of a fire-and-forget send into a coding agent."""

    session_id: str
    """The session the message was routed to. The backend creates a
    new session and returns its id when the caller didn't supply
    one — callers may want to remember it for follow-up sends."""

    project_path: str = ""
    """Resolved working directory for this session, when the backend
    reports one."""

    status: str = "sent"
    """Status discriminator:
    - ``"sent"`` — the agent has accepted the prompt and is working
      (the typical OpenCode ``prompt_async`` 204 path).
    - ``"queued"`` — the message is waiting for the agent to pick
      it up (used by backends that can't push directly into a
      running session, e.g. a file-based queue read by a Claude
      Code hook on its next interactive turn).

    Callers should not branch on this for correctness — both
    statuses mean "we did our part." It's surfaced to the user so
    the spoken acknowledgment can be accurate ("queued for Claude"
    vs "sent to OpenCode")."""


class CodingAgentBackend(ABC):
    """Abstract coding-agent backend. Subclasses are auto-registered
    in ``CodingAgentBackend._registry`` when their module is
    imported — same pattern as every other Gilbert backend ABC.
    """

    _registry: dict[str, type[CodingAgentBackend]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            CodingAgentBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type[CodingAgentBackend]]:
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        """Backend-specific configuration parameters surfaced under
        ``settings.<key>`` on the parent ``CodeConduitService``.
        """
        return []

    @abstractmethod
    async def initialize(self, config: dict[str, Any]) -> None:
        """Initialize from the resolved ``settings.*`` dict."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Release resources (HTTP client pool, subprocess handles)."""
        ...

    @abstractmethod
    async def send_message(
        self,
        *,
        message: str,
        project_path: str,
        session_id: str = "",
        new_session: bool = False,
    ) -> CodingAgentSendResult:
        """Relay ``message`` to the coding agent.

        Args:
            message: The user's text — verbatim, no preprocessing.
                Gilbert is a conduit, not an editor.
            project_path: Absolute path to the project root. Empty
                means "the backend's default" (whatever the agent
                is configured to fall back to, often the current
                working directory of the daemon).
            session_id: Continue an existing session. Empty means
                "use whatever the backend treats as default" —
                most backends will start a new session in that case.
            new_session: Force a fresh session even when
                ``session_id`` is supplied. Useful when the user
                wants a clean slate ("forget what we were doing,
                tell Claude to ...").

        Returns:
            A ``CodingAgentSendResult`` with the resolved session id
            so the caller can pin follow-ups.

        Implementations should NOT wait for the agent to finish —
        the voice loop assumes this returns within a second or two.
        """
        ...

    @abstractmethod
    async def list_sessions(
        self,
        *,
        project_path: str = "",
        limit: int = 20,
    ) -> list[CodingAgentSession]:
        """List recent sessions, most-recent first. Filter by
        ``project_path`` when set. Empty when the backend isn't
        ready or has no sessions."""
        ...

    @abstractmethod
    def stream_events(self) -> AsyncIterator[CodingAgentEvent]:
        """Yield inbound events as the agent emits them — Gilbert's
        notification source.

        Implementations are expected to handle reconnect themselves
        (network blips, daemon restart) so a single ``async for``
        loop on the service side stays alive across transient
        failures. Yielding stops only on ``close()`` /
        ``asyncio.CancelledError``.

        Backends that have no inbound channel (e.g. a future
        subprocess-only Claude Code wrapper before the stop-hook
        webhook lands) should yield nothing and never raise — the
        service must not crash because one backend lacks the
        feature. Use ``async def`` + an empty body that just awaits
        a never-fires event, OR yield zero times then return.
        """
        ...

    @property
    @abstractmethod
    def available(self) -> bool:
        """Whether the backend is configured + ready to send."""
        ...


@runtime_checkable
class CodingConduitProvider(Protocol):
    """Capability protocol for "relay a message to the coding agent."

    Other Gilbert services can resolve this via
    ``resolver.get_capability("code_conduit")`` and use it without
    depending on the concrete ``CodeConduitService`` class.

    The minimal surface (``send_message`` only) is intentional —
    consumers want to fire a message, not manage sessions. Session
    listing + history live on the concrete service via its own
    ``ws_handlers`` / tool surface.

    The ``project`` argument here is the *alias* (or absolute path
    — both accepted) rather than the absolute path the backend
    eventually sees. Alias-to-path resolution happens inside the
    conduit service so callers don't need to know the user's
    operator-configured project map.
    """

    async def send_message(
        self,
        *,
        message: str,
        project: str = "",
        session_id: str = "",
        new_session: bool = False,
    ) -> CodingAgentSendResult:
        """Relay ``message`` to the active coding agent. Empty
        ``project`` falls back to the operator-configured default."""
        ...
