"""Shape tests for the GreetingContextProvider protocol.

The protocol must be runtime-checkable so GreetingService can
``isinstance(svc, GreetingContextProvider)`` after a generic
``resolver.get_all("greeting_context")``.
"""

from gilbert.interfaces.greeting import GreetingContext, GreetingContextProvider


def test_protocol_is_runtime_checkable() -> None:
    """Required so the framework can identify providers without
    importing concrete service classes."""

    class _Conformant:
        @property
        def greeting_context_id(self) -> str:
            return "demo"

        @property
        def greeting_context_label(self) -> str:
            return "Demo"

        async def greeting_context(self, user_id: str) -> GreetingContext | None:
            return None

    assert isinstance(_Conformant(), GreetingContextProvider)


def test_non_conformant_rejected() -> None:
    class _Missing:  # no greeting_context method
        @property
        def greeting_context_id(self) -> str:
            return "x"

        @property
        def greeting_context_label(self) -> str:
            return "X"

    assert not isinstance(_Missing(), GreetingContextProvider)


def test_greeting_context_dataclass_fields() -> None:
    """Frozen dataclass — providers shouldn't mutate after returning."""
    ctx = GreetingContext(provider_id="weather", label="Weather", prose="Sunny, 72°F.")
    assert ctx.provider_id == "weather"
    assert ctx.label == "Weather"
    assert ctx.prose == "Sunny, 72°F."
