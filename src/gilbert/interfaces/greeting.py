"""Greeting context provider capability.

Any service can advertise the ``"greeting_context"`` capability and
implement this protocol to contribute a labeled prose fact to the
auto-generated arrival greeting. GreetingService collects these into
a single bag of facts; the AI (guided by the user's prompt template)
decides what to use.

Adding a new contributor (e.g., calendar events for today, tasks due
today, doorbell counts) is purely additive — no edits to GreetingService.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class GreetingContext:
    """A single labeled fact contributed to the greeting prompt.

    ``provider_id`` matches the contributing service's
    ``greeting_context_id`` so the AI prompt (and the settings UI)
    can refer to specific contributions.
    """

    provider_id: str
    label: str
    prose: str


@runtime_checkable
class GreetingContextProvider(Protocol):
    """Services that contribute a labeled fact to arrival greetings.

    The contract is fire-and-forget: providers must never raise; on
    error or "no data right now" they return ``None`` and the greeting
    proceeds without them. They must not depend on greeting order.
    """

    @property
    def greeting_context_id(self) -> str:
        """Stable short id used in settings + the labeled prose block.

        Lowercase, snake_case, must match the value the settings UI
        uses to enable/disable this provider.
        """
        ...

    @property
    def greeting_context_label(self) -> str:
        """Human-readable label for the settings UI toggle row."""
        ...

    async def greeting_context(self, user_id: str) -> GreetingContext | None:
        """Return a labeled fact for ``user_id``'s greeting, or None.

        Implementers should:
        - Return None when the underlying capability is disabled,
          unconfigured, or has no data right now (cold start, error,
          quiet hours, already-shown-today guard, …).
        - Never raise. Catch and log internally.
        - Return prose that's a complete sentence (or two) — no
          leading label, no trailing newline. GreetingService adds the
          ``{label}:`` prefix when assembling the block.
        """
        ...
