"""Host-resources service — exposes host hardware as a discoverable capability.

Thin wrapper over a ``HostResourcesBackend``: resolves the ``"local"``
backend from the registry at ``start()`` and delegates ``get_host_resources``
to its ``probe()``. The data is host-global, so nothing is cached per-user
(ADR-0009 isolation is moot here — there is no user-specific state). The
side-effect import that registers ``LocalHostResources`` lives in the
composition root (``core/app.py``), per the backend-registry rule.
"""

from __future__ import annotations

import logging

from gilbert.interfaces.host_resources import (
    HostResources,
    HostResourcesBackend,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver

logger = logging.getLogger(__name__)


class HostResourcesService(Service):
    """Discoverable host-resources probe.

    Capabilities: host_resources
    """

    def __init__(self) -> None:
        self._backend: HostResourcesBackend | None = None

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="host_resources",
            capabilities=frozenset({"host_resources"}),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        backend_cls = self._select_backend(HostResourcesBackend.registered_backends())
        if backend_cls is None:
            raise ValueError("No available host-resources backend")
        self._backend = backend_cls()
        logger.info(
            "Host-resources service started (backend=%s)",
            backend_cls.backend_name or backend_cls.__name__,
        )

    @staticmethod
    def _select_backend(
        backends: dict[str, type[HostResourcesBackend]],
    ) -> type[HostResourcesBackend] | None:
        """Pick the highest-priority backend that reports itself available.

        Plugins register richer optional backends (e.g. ``llmfit`` for
        multi-vendor GPU detection) at a higher ``priority`` than the
        built-in ``local`` floor; when such a backend's external tool is
        absent it returns ``is_available() is False`` and we fall back to the
        next one down. Ties break by name for deterministic selection. The
        registry is read at ``start()`` — after plugin ``setup()`` has run
        (``app._load_plugins`` precedes ``start_all``), so plugin-registered
        backends are visible here.
        """
        for _name, cls in sorted(backends.items(), key=lambda kv: (-kv[1].priority, kv[0])):
            if cls.is_available():
                return cls
        return None

    # --- HostResourcesProvider ---

    async def get_host_resources(self) -> HostResources:
        """Return a snapshot of the host's RAM and GPU resources."""
        if self._backend is None:
            raise RuntimeError("host-resources service not started")
        return await self._backend.probe()
