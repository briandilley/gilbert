"""Messaging backend interface — sending and receiving SMS / chat-style
text messages.

Shape mirrors the other I/O backends (``TelephonyBackend``,
``TTSBackend``, …):

- ABC with ``_registry`` + ``__init_subclass__`` auto-registration so
  the composition root discovers concretes via
  ``MessagingBackend.registered_backends()`` after a side-effect
  import.
- ``backend_config_params()`` declares the keys the operator sets in
  ``/settings`` for the chosen backend.
- ``initialize(config)`` / ``close()`` lifecycle hooks.
- One outbound operation: ``send_message(to, body, *, media_urls=…,
  from_number=…)`` returns the backend-issued message id.

Inbound delivery is push-based — the carrier hits a webhook, the core
``/api/<backend>/messages/webhook`` route resolves a
``<backend>_messaging_webhook`` capability, and the plugin parses the
event and calls back into ``MessagingService.receive_inbound(...)``.
The ABC doesn't need to know about that path; the parsing lives
inside the backend's own plugin code.

This module is pure: only standard library + cross-references inside
``interfaces/``. No HTTP clients, no plugin imports, no service code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar, Protocol, runtime_checkable

from gilbert.interfaces.configuration import ConfigParam

__all__ = [
    "Message",
    "MessageDirection",
    "MessageStatus",
    "MessagingBackend",
    "MessagingProvider",
    "MessagingWebhookEndpoint",
    "ThreadSummary",
]


# ── Wire shapes ──────────────────────────────────────────────────────


class MessageDirection(StrEnum):
    """Which way the message flowed.

    - ``INBOUND``: remote party → Gilbert.
    - ``OUTBOUND``: Gilbert → remote party.
    """

    INBOUND = "inbound"
    OUTBOUND = "outbound"


class MessageStatus(StrEnum):
    """Where this message is in its lifecycle.

    Inbound messages are ``RECEIVED`` the instant they hit the
    webhook — they don't go through queued/sent/delivered.
    Outbound messages walk ``QUEUED`` → ``SENT`` → ``DELIVERED`` (or
    ``FAILED``) as carrier acknowledgements arrive.
    """

    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    RECEIVED = "received"


@dataclass
class Message:
    """One message persisted in the message store.

    Identity is the backend's own message id (Telnyx UUID, etc.) —
    we don't mint our own so duplicate webhook deliveries collapse
    cleanly.

    ``user_id`` is the Gilbert user who OWNS this thread on our side
    (resolved at receive time by mapping ``our_number`` → its
    configured owner). ``other_number`` is the remote party.

    Threads are derived from messages by ``(user_id, other_number,
    our_number)`` rather than stored separately — keeps the schema
    flat and makes "merge two threads into one" a no-op.
    """

    message_id: str
    user_id: str
    our_number: str
    other_number: str
    direction: str  # MessageDirection value
    body: str
    status: str  # MessageStatus value
    created_at: str  # ISO 8601 UTC
    media_urls: list[str] = field(default_factory=list)
    error: str = ""
    backend: str = ""  # which MessagingBackend handled this (for diagnostics)


@dataclass
class ThreadSummary:
    """Derived view of one conversation thread.

    Built by ``MessagingProvider.list_threads()`` by grouping
    persisted messages on ``(user_id, our_number, other_number)``.
    Not separately persisted.
    """

    user_id: str
    our_number: str
    other_number: str
    last_message_at: str  # ISO 8601 UTC
    last_message_preview: str  # first ~80 chars of the most recent message body
    last_message_direction: str  # MessageDirection value
    unread_count: int  # inbound messages received since the user last marked read
    message_count: int


# ── Backend ABC ──────────────────────────────────────────────────────


class MessagingBackend(ABC):
    """Send-side abstraction for a text-message carrier (SMS, RCS,
    whatever the provider supports).

    Inbound delivery comes through the matching
    ``MessagingWebhookEndpoint`` capability — see that protocol for
    the parsing contract.
    """

    backend_name: ClassVar[str] = ""
    _registry: ClassVar[dict[str, type[MessagingBackend]]] = {}

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            MessagingBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type[MessagingBackend]]:
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        """Operator-tunable keys for this backend. Defaults to none —
        backends override to declare api_key, from_number, etc."""
        return []

    @abstractmethod
    async def initialize(self, config: dict[str, object]) -> None:
        """Wire credentials + per-backend state. Called once after the
        operator's stored config has been resolved. Idempotent — may
        be called again when config is updated at runtime."""

    @abstractmethod
    async def close(self) -> None:
        """Release HTTP clients / sockets. Idempotent."""

    @abstractmethod
    async def send_message(
        self,
        *,
        to: str,
        body: str,
        from_number: str = "",
        media_urls: list[str] | None = None,
    ) -> str:
        """Send one message. Returns the backend-issued message id.

        ``from_number`` is optional — when empty, the backend uses
        whatever default it has configured. Raises on transport
        errors; success means the carrier accepted the message for
        delivery, NOT that the recipient has received it (that comes
        via webhook callback)."""


# ── Capability protocols ─────────────────────────────────────────────


@runtime_checkable
class MessagingProvider(Protocol):
    """Capability exposed by ``MessagingService`` for other services
    and the SPA to consume.

    Reads are scoped by ``user_id`` so callers can't see threads they
    don't own. The service implements the multi-user filter; this
    protocol just declares the surface.
    """

    async def send(
        self,
        *,
        user_id: str,
        to_number: str,
        body: str,
        from_number: str = "",
        media_urls: list[str] | None = None,
    ) -> Message:
        """Send a message on behalf of ``user_id``. Returns the
        persisted ``Message`` row (with backend-issued ``message_id``
        and the resolved ``our_number``)."""
        ...

    async def list_threads(self, user_id: str) -> list[ThreadSummary]:
        """Every thread visible to ``user_id``, most recently active
        first."""
        ...

    async def get_messages(
        self,
        *,
        user_id: str,
        other_number: str,
        our_number: str = "",
        limit: int = 200,
    ) -> list[Message]:
        """Full message history for one thread, oldest first.
        ``our_number=""`` means "any of our numbers" — useful when
        the SPA only knows the remote party."""
        ...


# Callable signature a backend invokes to push an inbound message into
# the messaging service. The service implements multi-user routing,
# persistence, bus events, and (optionally) AI auto-reply.
InboundDeliverer = Callable[
    ["Message"],
    Awaitable[None],
]


@runtime_checkable
class MessagingWebhookEndpoint(Protocol):
    """Capability exposed by a messaging-aware backend plugin so the
    core ``/api/<backend>/messages/webhook`` route can hand the raw
    webhook payload off without importing the plugin module directly.

    The plugin parses the payload (provider-specific shape) into one
    or more ``Message`` records and calls back through the bound
    ``InboundDeliverer`` it received at startup. Mirrors
    ``TelnyxWebhookEndpoint`` for the voice side.
    """

    async def deliver_webhook_event(self, payload: dict[str, object]) -> None:
        ...
