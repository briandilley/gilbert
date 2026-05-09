"""Push-notification backend interface — ABC, registry, and shared types.

The :class:`PushNotificationBackend` ABC follows Gilbert's universal
backend pattern: ``__init_subclass__`` registers concrete subclasses by
``backend_name`` so :class:`PushNotificationService` can discover them
without importing concrete classes. Each backend exposes its
**admin-level** config via :meth:`backend_config_params`, its **per-user
destination shape** via :meth:`destination_params`, and one or more
:class:`~gilbert.interfaces.configuration.ConfigAction` buttons via
:meth:`backend_actions`.

Method-binding rules (do not "simplify" these):

- :meth:`backend_config_params`, :meth:`destination_params`, and
  :meth:`backend_actions` are ``@classmethod`` because the Settings UI
  and the per-user Routes UI consume them **before** any instance is
  initialised. They describe shape, not state.
- :meth:`initialize`, :meth:`close`, :meth:`send`, and
  :meth:`invoke_backend_action` are instance methods. They run after
  the service has called ``initialize(config)`` and may rely on
  ``self._client``, ``self._auth_token``, etc.

Confusing the two breaks the service in non-obvious ways: marking
``invoke_backend_action`` as ``@classmethod`` would mean the
"Test connection" button can't see the live HTTP client; marking
``backend_config_params`` as an instance method would force the
Settings UI to instantiate every backend before rendering the form.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.notifications import NotificationUrgency


class PushDeliveryStatus(StrEnum):
    """Outcome of a single delivery attempt to one route."""

    DELIVERED = "delivered"
    """The provider accepted the message (HTTP 2xx, send call returned)."""

    REJECTED = "rejected"
    """The provider explicitly rejected the message (4xx, invalid token).
    Retrying will not help; the service must not retry on this status."""

    TRANSIENT_ERROR = "transient_error"
    """Network blip, 5xx, timeout. Retry until budget exhausted."""

    DISABLED = "disabled"
    """The route or backend is disabled / not configured. Skipped."""


@dataclass(frozen=True)
class PushDeliveryResult:
    """Result of one ``send`` call.

    ``message`` MUST be a status line only — never raw response bodies
    or full URLs. Backends MUST funnel exception text through
    ``_safe_repr`` (provided by :mod:`gilbert.core.services.push_notifications`)
    before stuffing it here so credentials in error strings don't leak.
    """

    status: PushDeliveryStatus
    message: str = ""
    """Human-legible summary; logged on failure, surfaced in test
    connection toasts. Backends MUST scrub credentials before placing
    text here."""

    provider_message_id: str = ""
    """Whatever id the provider returned (Pushover receipt, Telegram
    ``message_id``, etc.) — for future audit/dedup; empty if not
    applicable. MUST NOT contain values that themselves embed a secret
    (e.g. webhook tokens)."""

    retry_after_s: float | None = None
    """Provider-supplied retry hint, in seconds. When set on a
    ``TRANSIENT_ERROR`` result, the service worker uses this value
    instead of its configured backoff for the next attempt — Discord
    ``X-RateLimit-Reset-After``, Telegram ``parameters.retry_after``,
    and any standard ``Retry-After`` header are parsed by the backend
    and surfaced here. Capped service-side at 60s to keep one wedged
    provider from monopolising a worker."""


@dataclass(frozen=True)
class PushDestination:
    """Per-user destination data passed to ``send``.

    ``data`` carries the backend-specific fields (Pushover ``user_key``,
    Discord ``webhook_url``, Telegram ``chat_id``, ntfy topic + optional
    server). The backend defines what keys it expects via
    :meth:`PushNotificationBackend.destination_params`.

    ``user_id`` is the recipient's Gilbert user id, included so backends
    can log it without parsing the route record. ``route_id`` is the
    route's id, used by the delivery worker for logging correlation.
    """

    user_id: str
    route_id: str
    data: dict[str, Any]


@dataclass(frozen=True)
class PushMessage:
    """The payload to deliver. Pre-built by the service from a Notification."""

    title: str
    body: str
    urgency: NotificationUrgency
    source: str
    """Origin tag from the original ``Notification.source`` (e.g.
    ``"agent"``, ``"scheduler"``). Backends that support per-source
    icons or topics can use it."""

    source_ref: dict[str, Any] | None = None
    """Optional structured pointer back to whatever produced the original
    notification. Backends that can attach 'click here' URLs (Pushover,
    Discord, ntfy, Telegram inline-keyboard) read
    ``source_ref["deep_link_url"]`` if present — the service sets this
    derived field when ``default_deep_link_origin`` is configured and
    ``source_ref`` carries a known shape."""

    notification_id: str = ""
    """Original ``Notification.id`` — for logging and (v1.1) outbox dedup."""


class PushNotificationBackend(ABC):
    """Abstract interface for external notification delivery providers.

    Each concrete backend (ntfy, Pushover, Discord webhook, Telegram bot)
    is a small std-plugin that subclasses this ABC, sets ``backend_name``,
    and implements :meth:`send`. Backends auto-register via
    ``__init_subclass__`` on import; :class:`PushNotificationService`
    discovers them via :meth:`registered_backends`.
    """

    _registry: dict[str, type[PushNotificationBackend]] = {}
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            PushNotificationBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type[PushNotificationBackend]]:
        return dict(cls._registry)

    # --- Admin-level config (server-wide) ----------------------------------

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        """Server-wide config (API tokens, default endpoints).

        Rendered on the admin Settings page under the plugin's category
        with ``backend_param=True``. Sensitive values (tokens, app keys)
        MUST set ``sensitive=True``.
        """
        return []

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        """Action buttons on the admin Settings page (e.g. Test connection)."""
        return []

    async def invoke_backend_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        return ConfigActionResult(status="error", message=f"Unknown action: {key}")

    # --- Per-user destination shape ---------------------------------------

    @classmethod
    @abstractmethod
    def destination_params(cls) -> list[ConfigParam]:
        """Describe the per-user destination fields a route requires.

        These are rendered on the per-user Notification Routes page. The
        UI builds a form from this list, the user fills it in, and the
        resulting dict becomes ``PushDestination.data`` at delivery time.
        """

    # --- Lifecycle --------------------------------------------------------

    @abstractmethod
    async def initialize(self, config: dict[str, Any]) -> None:
        """Initialise the backend with admin config. Called by the service
        on start and on backend-config change."""

    @abstractmethod
    async def close(self) -> None:
        """Clean up (HTTP clients, connection pools)."""

    # --- Optional runtime hints ------------------------------------------

    def runtime_data(self) -> dict[str, Any]:
        """Per-backend dict the service exposes via ``push.backends.list``.

        Lazily populated after :meth:`initialize` (e.g. Telegram caches
        ``getMe.username`` so the chat-id wizard's ``https://t.me/<bot>``
        deep link renders without a second roundtrip). MUST NEVER
        contain secret material — strictly UI hints (bot username, max
        upload size, etc.). Default: empty dict.
        """
        return {}

    # --- Delivery --------------------------------------------------------

    @abstractmethod
    async def send(
        self,
        destination: PushDestination,
        message: PushMessage,
    ) -> PushDeliveryResult:
        """Deliver one message.

        Backends MUST NOT raise on transient errors; return
        ``PushDeliveryResult(status=TRANSIENT_ERROR, message=...)``
        instead. Raising is reserved for programmer errors (bad
        destination shape, unhandled exception type) — the service
        treats raised exceptions as ``REJECTED`` and logs them at
        ``ERROR``.
        """


__all__ = [
    "PushDeliveryResult",
    "PushDeliveryStatus",
    "PushDestination",
    "PushMessage",
    "PushNotificationBackend",
]

