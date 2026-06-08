"""Host-resources interface — describe the hardware Gilbert runs on.

Exposes a ``@runtime_checkable`` ``HostResourcesProvider`` capability so
any service or backend can ask the host what RAM / GPU / VRAM it has,
plus a ``HostResourcesBackend`` registry for vendor-free probes. The
capability returns **raw data only** — turning it into a runnability
verdict (does model X fit?) is a consumer's policy, not core's (ADR-0020).
The probe is localhost-only and best-effort: when a value cannot be
determined it is reported as ``None`` ("unknown"), never fabricated.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "GPUInfo",
    "HostResources",
    "HostResourcesBackend",
    "HostResourcesProvider",
]


@dataclass(frozen=True)
class GPUInfo:
    """A single GPU on the host.

    ``total_vram_bytes`` is ``None`` when the amount of video memory could
    not be determined (best-effort detection on a non-NVIDIA card, a
    container without device passthrough, an unparseable tool output, …).
    A ``None`` here means "unknown," never zero.
    """

    name: str
    total_vram_bytes: int | None = None


@dataclass(frozen=True)
class HostResources:
    """Snapshot of the host's memory and GPU resources.

    RAM figures are always populated (``psutil`` is a hard dependency).
    ``gpus`` is empty when no GPU was detected — distinct from a detected
    GPU whose VRAM is unknown, which appears as a ``GPUInfo`` with
    ``total_vram_bytes is None``.
    """

    total_ram_bytes: int
    available_ram_bytes: int
    gpus: tuple[GPUInfo, ...] = field(default_factory=tuple)

    @property
    def has_gpu(self) -> bool:
        """Whether at least one GPU was detected on the host."""
        return len(self.gpus) > 0


@runtime_checkable
class HostResourcesProvider(Protocol):
    """Capability protocol for querying host hardware resources.

    Consumers resolve this via ``resolver.get_capability("host_resources")``
    and ``isinstance``-check against ``HostResourcesProvider`` rather than
    importing the concrete service. The local-model manager uses it for its
    hardware-fit filter; ``whisper`` / ``kokoro`` ``device=auto`` paths can
    use it to detect a GPU instead of asking the operator.
    """

    async def get_host_resources(self) -> HostResources:
        """Return a snapshot of the host's RAM and GPU resources."""
        ...


class HostResourcesBackend(ABC):
    """Abstract host-resources backend — vendor-free, localhost-only.

    Follows the universal backend pattern: an ABC plus an
    ``__init_subclass__`` registry keyed by ``backend_name``. Services
    discover implementations via ``registered_backends()`` after a
    side-effect import, never by importing the concrete class.
    """

    _registry: dict[str, type[HostResourcesBackend]] = {}
    backend_name: str = ""
    """Short identifier used to select the backend (e.g., ``"local"``)."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.backend_name:
            HostResourcesBackend._registry[cls.backend_name] = cls

    @classmethod
    def registered_backends(cls) -> dict[str, type[HostResourcesBackend]]:
        """Return ``{name: class}`` for all registered backends."""
        return dict(cls._registry)

    @abstractmethod
    async def probe(self) -> HostResources:
        """Probe the host and return its resources.

        Best-effort: must never raise for a missing or unreadable GPU —
        report ``gpus=()`` (no GPU) or ``total_vram_bytes=None`` (unknown
        VRAM) instead.
        """
        ...
