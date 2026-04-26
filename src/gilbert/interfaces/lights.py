"""Lights service interface — turn lights on/off and (optionally) dim them.

Modeled on the music interface: a thin abstract backend with class-level
``supports_*`` capability flags, an auto-registering subclass registry,
and per-device metadata (``LightInfo``) so the service layer can present
mixed dimmer/switch systems sensibly.

A backend implementation typically wraps a vendor protocol (Lutron
RadioRA, Hue, Caseta, …) and converts its native object model into
``LightInfo`` instances and ``light_id``-keyed operations.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

__all__ = [
    "LightInfo",
    "LightsBackend",
]

if TYPE_CHECKING:
    from gilbert.interfaces.configuration import ConfigParam


@dataclass(frozen=True)
class LightInfo:
    """Per-device metadata for a single light.

    ``supports_dimming`` is a per-device flag (a Lutron system can mix
    dimmers and switches) — the service uses it at execute time to
    skip switch-only loads when the user asks to set brightness.
    ``level`` is the last-known level 0..100; backends populate it
    from cached state when ``list_lights()`` returns, so no extra
    round-trip is needed for a list view.
    """

    light_id: str
    name: str
    area: str = ""
    supports_dimming: bool = False
    level: float = 0.0


class LightsBackend(ABC):
    """Abstract lights backend — discover, query, and control lights."""

    _registry: dict[str, type[LightsBackend]] = {}
    backend_name: str = ""
    supports_dimming: bool = False
    """True when this backend's protocol can dim at all (i.e. some of its
    devices may be dimmers). Gates the service's ``set_brightness`` tool —
    a backend whose hardware is exclusively switches leaves this ``False``
    and the brightness tool stays hidden."""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            LightsBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type[LightsBackend]]:
        return dict(cls._registry)

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        """Describe backend-specific configuration parameters."""
        return []

    @abstractmethod
    async def initialize(self, config: dict[str, object]) -> None:
        """Initialize the backend with provider-specific configuration."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close connections and release resources."""
        ...

    @abstractmethod
    async def list_lights(self) -> list[LightInfo]:
        """Return every known light, with cached level + per-device flags."""
        ...

    @abstractmethod
    async def get_level(self, light_id: str) -> float:
        """Return the current level (0..100) for the given light.

        Implementations may query the device for fresh state — callers
        should treat this as potentially blocking on a network round trip.
        """
        ...

    @abstractmethod
    async def set_level(self, light_id: str, level: float) -> None:
        """Set the level (0..100). For switches, anything > 0 means on."""
        ...
