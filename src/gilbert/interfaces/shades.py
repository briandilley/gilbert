"""Shades service interface — control window shades, blinds, and covers.

Modeled on ``LightsBackend`` and the music interface: registry,
class-level ``supports_*`` flags, and ``ShadeInfo`` per-device records.

Position uses the convention 0 = closed, 100 = open.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

__all__ = [
    "ShadeInfo",
    "ShadesBackend",
]

if TYPE_CHECKING:
    from gilbert.interfaces.configuration import ConfigParam


@dataclass(frozen=True)
class ShadeInfo:
    """Per-device metadata for a single shade or cover.

    ``supports_position`` is per-device because a system may mix
    position-aware shades with simple raise/lower-only covers. Same
    rationale for ``supports_stop`` — some basic covers can't stop
    mid-travel.
    """

    shade_id: str
    name: str
    area: str = ""
    supports_position: bool = True
    supports_stop: bool = True
    position: float = 0.0


class ShadesBackend(ABC):
    """Abstract shades backend — discover, query, and control shades."""

    _registry: dict[str, type[ShadesBackend]] = {}
    backend_name: str = ""
    supports_position: bool = False
    """True when the backend can set arbitrary positions (not just
    open/close). Gates the service's ``set_position`` tool — a backend
    whose hardware only does raise/lower leaves this ``False`` and the
    set_position tool stays hidden."""
    supports_stop: bool = False
    """True when the backend can interrupt a moving shade. Gates the
    service's ``stop`` tool. Some basic relay-driven covers can't stop
    mid-travel and leave this ``False``."""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            ShadesBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type[ShadesBackend]]:
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return []

    @abstractmethod
    async def initialize(self, config: dict[str, object]) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def list_shades(self) -> list[ShadeInfo]: ...

    @abstractmethod
    async def get_position(self, shade_id: str) -> float:
        """Return the current position (0=closed, 100=open)."""
        ...

    @abstractmethod
    async def set_position(self, shade_id: str, position: float) -> None:
        """Set the shade to ``position`` (0=closed, 100=open).

        Backends without ``supports_position = True`` should map this to
        the nearest open/close action and ignore intermediate values, or
        raise ``NotImplementedError`` if even that is impossible.
        """
        ...

    async def stop(self, shade_id: str) -> None:
        """Stop a moving shade. Default raises ``NotImplementedError``;
        backends with ``supports_stop = True`` override this."""
        raise NotImplementedError("This shades backend does not support stop")
