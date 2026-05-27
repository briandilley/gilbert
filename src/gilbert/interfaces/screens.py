"""Screen display interfaces — capability protocols for the web layer."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class GuestScreenPolicy(Protocol):
    """Protocol for the "may unauthenticated visitors set up a screen?" toggle.

    The web layer resolves this via ``get_capability("screen_display")`` to
    decide screen access without depending on the concrete ``ScreenService``:

    - ``GET /screens/stream`` rejects unauthenticated visitors with 403 when
      this is ``False``.
    - ``GET /screens/info`` reports it so the login page and screens page can
      gate their UI without authenticating.
    - The System → Screens nav item is widened to ``everyone`` when it is on.
    """

    @property
    def allow_guest_screens(self) -> bool:
        ...
