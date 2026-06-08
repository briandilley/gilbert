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
        backends = HostResourcesBackend.registered_backends()
        backend_cls = backends.get("local")
        if backend_cls is None:
            raise ValueError("Unknown host-resources backend: local")
        self._backend = backend_cls()
        logger.info("Host-resources service started")

    # --- HostResourcesProvider ---

    async def get_host_resources(self) -> HostResources:
        """Return a snapshot of the host's RAM and GPU resources."""
        if self._backend is None:
            raise RuntimeError("host-resources service not started")
        return await self._backend.probe()
