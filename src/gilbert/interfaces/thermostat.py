"""Thermostat service interface — read climate state and adjust setpoints.

Modeled on ``LightsBackend`` / ``ShadesBackend``: a thin abstract backend
with class-level ``supports_*`` capability flags, an auto-registering
subclass registry, and per-device metadata in ``ThermostatInfo`` so the
service layer can present mixed heat-only / heat-cool zones sensibly.

A backend implementation typically wraps a vendor cloud API
(Nexia / American Standard, Google SDM / Nest, Matter, …) and converts
its native object model into ``ThermostatInfo`` instances and
``thermostat_id``-keyed operations.

Conventions used by callers:

- Temperatures are passed and returned in the device's native unit. The
  per-device ``temperature_unit`` field on ``ThermostatInfo`` says
  whether that's Fahrenheit or Celsius.
- HVAC modes are one of ``HVAC_MODES``: ``off``, ``heat``, ``cool``,
  ``auto``. Backends that don't support one of these can reject the
  call with ``NotImplementedError``.
- Fan modes are one of ``FAN_MODES``: ``auto``, ``on``, ``circulate``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

__all__ = [
    "FAN_MODES",
    "HVAC_MODES",
    "ThermostatBackend",
    "ThermostatInfo",
]

if TYPE_CHECKING:
    from gilbert.interfaces.configuration import ConfigParam


HVAC_MODES: tuple[str, ...] = ("off", "heat", "cool", "auto")
FAN_MODES: tuple[str, ...] = ("auto", "on", "circulate")


@dataclass(frozen=True)
class ThermostatInfo:
    """Topology + last-known state for a single thermostat zone.

    ``supports_cooling`` / ``supports_heating`` / ``supports_fan_mode``
    are per-device because a single backend can expose mixed hardware
    (e.g. a heat-only zone alongside a heat-pump zone). The service
    uses them at execute time to skip incompatible operations.

    ``current_temperature`` and the optional ``current_humidity`` /
    setpoints / modes carry the last value returned by the backend so
    a list-view doesn't require a per-thermostat round trip.
    ``temperature_unit`` tells callers whether numbers are F or C — the
    backend reports values in its device's native unit and is
    responsible for converting if its API is locale-dependent.
    """

    thermostat_id: str
    name: str
    area: str = ""
    supports_cooling: bool = True
    supports_heating: bool = True
    supports_fan_mode: bool = False
    has_humidity_sensor: bool = False
    current_temperature: float = 0.0
    current_humidity: float | None = None
    heat_setpoint: float | None = None
    cool_setpoint: float | None = None
    mode: str = "off"
    fan_mode: str | None = None
    temperature_unit: str = "F"


class ThermostatBackend(ABC):
    """Abstract thermostat backend — discover, query, and control thermostats."""

    _registry: dict[str, type[ThermostatBackend]] = {}
    backend_name: str = ""
    supports_cooling: bool = True
    """True when this backend's protocol can address a cool setpoint
    on at least some of its devices. Gates the service's
    ``thermostats_set_cool`` tool — a heat-only system leaves this
    ``False`` and that tool stays hidden."""
    supports_heating: bool = True
    """True when this backend's protocol can address a heat setpoint.
    Almost always ``True``; included for symmetry with cooling so a
    cool-only system (rare, but possible) can be modeled cleanly."""
    supports_fan_mode: bool = False
    """True when the backend can change the fan mode independently of
    the HVAC mode. Gates the service's ``thermostats_set_fan_mode``
    tool."""
    supports_humidity: bool = False
    """True when ``ThermostatInfo.current_humidity`` is populated by
    this backend. Purely informational — humidity readings appear in
    status output when present, hide otherwise."""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            ThermostatBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type[ThermostatBackend]]:
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
    async def list_thermostats(self) -> list[ThermostatInfo]:
        """Return every known thermostat zone with cached state.

        Backends should cache the topology and refresh state on a
        reasonable cadence so a list-view remains snappy.
        """
        ...

    @abstractmethod
    async def get_status(self, thermostat_id: str) -> ThermostatInfo:
        """Return a fresh snapshot of state for one thermostat zone.

        Implementations typically force a refresh against the underlying
        API — callers should treat this as potentially blocking on a
        network round trip.
        """
        ...

    @abstractmethod
    async def set_setpoint(
        self,
        thermostat_id: str,
        *,
        heat: float | None = None,
        cool: float | None = None,
    ) -> None:
        """Set heat and/or cool setpoint(s).

        Pass only ``heat`` to change the heating target, only ``cool``
        for cooling, or both to set the auto-mode range. Backends that
        don't support a direction (``supports_heating`` /
        ``supports_cooling`` is ``False``) should raise
        ``NotImplementedError`` when asked to set that direction.
        """
        ...

    @abstractmethod
    async def set_mode(self, thermostat_id: str, mode: str) -> None:
        """Change the HVAC mode. ``mode`` is one of ``HVAC_MODES``."""
        ...

    async def set_fan_mode(self, thermostat_id: str, fan_mode: str) -> None:
        """Change the fan mode. ``fan_mode`` is one of ``FAN_MODES``.

        Default raises ``NotImplementedError``; backends with
        ``supports_fan_mode = True`` override this.
        """
        raise NotImplementedError("This thermostat backend does not support fan mode")
