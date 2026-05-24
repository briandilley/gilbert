"""Helpers for services that want to forward config actions to a backend.

Most services with a swappable backend expose actions by delegating to
``backend_actions()`` / ``invoke_backend_action()``. This module captures
that forwarding pattern so each service doesn't have to re-implement it.

Usage:

.. code-block:: python

    from gilbert.core.services._backend_actions import (
        all_backend_actions, invoke_backend_action,
    )

    class MyService(Service):
        def config_actions(self) -> list[ConfigAction]:
            # Return actions for every registered backend, each tagged
            # with its backend name. The UI filters by current dropdown
            # value so unsaved backend changes immediately surface the
            # right button set.
            return all_backend_actions(
                registry=MyBackend.registered_backends(),
                current_backend=self._backend,
            )

        async def invoke_config_action(self, key, payload):
            return await invoke_backend_action(self._backend, key, payload)
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from gilbert.interfaces.configuration import (
    BackendActionProvider,
    ConfigAction,
    ConfigActionResult,
)


def all_backend_actions(
    *,
    registry: dict[str, type],
    current_backend: Any = None,
) -> list[ConfigAction]:
    """Return actions from every registered backend, tagged with backend name.

    ``registry`` is the ``{name: cls}`` mapping returned by
    ``Backend.registered_backends()``. ``current_backend`` is the
    service's running backend instance — if set and its class appears
    in the registry, we use the instance's ``backend_actions()`` so
    live state (e.g. whether an auth token is already linked) is
    reflected. For other registered backends we instantiate a fresh
    probe — concrete backend ``__init__`` methods are cheap (no I/O).

    Every returned action has ``backend_action=True`` and ``backend``
    set to the registry key, so the UI can filter by the current
    dropdown value in an unsaved state.
    """
    actions: list[ConfigAction] = []
    current_cls = type(current_backend) if current_backend is not None else None

    for name, cls in registry.items():
        source: BackendActionProvider | None = None
        if current_cls is cls and isinstance(current_backend, BackendActionProvider):
            source = current_backend
        else:
            try:
                probe = cls()
            except Exception:
                continue
            if isinstance(probe, BackendActionProvider):
                source = probe
        if source is None:
            continue
        try:
            raw = source.backend_actions()
        except Exception:
            continue
        for a in raw:
            actions.append(replace(a, backend_action=True, backend=name))
    return actions


def merge_backend_actions(
    backend: Any,
    fallback_cls: type | None = None,
) -> list[ConfigAction]:
    """Return the current backend's actions with ``backend_action=True``.

    Kept for backwards compatibility with services that only surface
    their single configured backend's actions. New services should
    prefer ``all_backend_actions`` so unsaved dropdown changes are
    reflected immediately.
    """
    source: BackendActionProvider | None = None
    if isinstance(backend, BackendActionProvider):
        source = backend
    elif fallback_cls is not None:
        try:
            probe = fallback_cls()
        except Exception:
            return []
        if isinstance(probe, BackendActionProvider):
            source = probe

    if source is None:
        return []
    return [replace(a, backend_action=True) for a in source.backend_actions()]


async def invoke_backend_action(
    backend: Any,
    key: str,
    payload: dict[str, Any],
) -> ConfigActionResult:
    """Invoke an action on the backend, or return an error result."""
    if backend is None:
        return ConfigActionResult(
            status="error",
            message="Service isn't running — enable it first.",
        )
    if not isinstance(backend, BackendActionProvider):
        return ConfigActionResult(
            status="error",
            message=f"Backend doesn't support action '{key}'",
        )
    return await backend.invoke_backend_action(key, payload)


async def invoke_backend_action_from_payload(
    *,
    registry: dict[str, type],
    current_backend: Any,
    key: str,
    payload: dict[str, Any],
) -> ConfigActionResult:
    """Invoke a backend action using payload-selected unsaved backend config.

    Resource editors can call service-level ``ConfigAction`` entries
    before a mailbox/account/list exists. The payload supplies
    ``backend`` and optional ``config``; this helper instantiates that
    backend, initializes it when needed, and forwards the action.
    """

    backend_name = str(payload.get("backend") or "")
    config = payload.get("config")
    if backend_name:
        cls = registry.get(backend_name)
        if cls is None:
            return ConfigActionResult(
                status="error",
                message=f"Unknown backend '{backend_name}'",
            )
        backend = cls()
    else:
        backend = current_backend

    if backend is None:
        return ConfigActionResult(
            status="error",
            message="No backend selected for this action.",
        )
    if not isinstance(backend, BackendActionProvider):
        return ConfigActionResult(
            status="error",
            message=f"Backend doesn't support action '{key}'",
        )

    if isinstance(config, dict) and key == "test_connection":
        initialize = getattr(backend, "initialize", None)
        if callable(initialize):
            await initialize(config)

    return await backend.invoke_backend_action(key, payload)
