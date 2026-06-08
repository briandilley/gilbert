"""Tests for the enablement-dependency mechanism (ADR-0018).

A service may declare that it needs a named backend or service to be
*enabled* before it can run. When the prerequisite is off the dependent
does NOT start and is surfaced as ``disabled, with the reason`` — never
auto-enabled.
"""

import pytest

from gilbert.core.service_manager import ServiceManager
from gilbert.interfaces.service import (
    EnablementDep,
    Service,
    ServiceInfo,
    ServiceResolver,
)


class OwnerService(Service):
    """A service that advertises a capability and can report a named
    backend as enabled/disabled (``BackendEnablementProvider``)."""

    def __init__(
        self,
        name: str,
        capability: str,
        *,
        enabled: bool = True,
        backends_enabled: frozenset[str] = frozenset(),
    ) -> None:
        self._name = name
        self._capability = capability
        self._enabled = enabled
        self._backends_enabled = backends_enabled
        self.started = False

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name=self._name,
            capabilities=frozenset({self._capability}),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        self.started = True

    async def stop(self) -> None:
        pass

    # BackendEnablementProvider
    def is_backend_enabled(self, backend_name: str) -> bool:
        return backend_name in self._backends_enabled


class DependentService(Service):
    """A service that declares an enablement dependency."""

    def __init__(self, name: str, deps: tuple[EnablementDep, ...]) -> None:
        self._name = name
        self._deps = deps
        self.started = False

    def service_info(self) -> ServiceInfo:
        # NB: capabilities named only in ``requires_enabled`` gate the
        # startup wave too — we deliberately do NOT also list them in
        # ``requires``, so an absent owner surfaces as *disabled* (the
        # prerequisite is off / not present), not *failed*.
        return ServiceInfo(
            name=self._name,
            requires_enabled=self._deps,
        )

    async def start(self, resolver: ServiceResolver) -> None:
        self.started = True

    async def stop(self) -> None:
        pass


@pytest.fixture
def manager() -> ServiceManager:
    return ServiceManager()


# --- Backend-target deps (dep.backend set) ---


async def test_backend_disabled_dependent_does_not_start(manager: ServiceManager) -> None:
    """When the owning service reports the named backend disabled, the
    dependent must not start and must appear in ``disabled_services`` with
    a reason naming the missing prerequisite."""
    owner = OwnerService("ai", "ai_chat", backends_enabled=frozenset())  # ollama OFF
    dependent = DependentService("model_manager", (EnablementDep("ai_chat", "ollama"),))
    manager.register(owner)
    manager.register(dependent)
    await manager.start_all()

    assert owner.started
    assert not dependent.started
    assert "model_manager" not in manager.started_services
    assert "model_manager" not in manager.failed_services
    assert "model_manager" in manager.disabled_services
    reason = manager.disabled_services["model_manager"]
    assert "ollama" in reason


async def test_backend_enabled_dependent_starts(manager: ServiceManager) -> None:
    """When the named backend is enabled, the dependent starts normally."""
    owner = OwnerService("ai", "ai_chat", backends_enabled=frozenset({"ollama"}))
    dependent = DependentService("model_manager", (EnablementDep("ai_chat", "ollama"),))
    manager.register(owner)
    manager.register(dependent)
    await manager.start_all()

    assert dependent.started
    assert "model_manager" in manager.started_services
    assert "model_manager" not in manager.disabled_services


async def test_prerequisite_never_auto_enabled(manager: ServiceManager) -> None:
    """The prerequisite backend must NEVER be enabled as a side effect of
    a dependent declaring a dependency on it (ADR-0018)."""
    owner = OwnerService("ai", "ai_chat", backends_enabled=frozenset())
    dependent = DependentService("model_manager", (EnablementDep("ai_chat", "ollama"),))
    manager.register(owner)
    manager.register(dependent)
    await manager.start_all()

    # The owner still reports ollama disabled — nothing reached over and
    # turned it on.
    assert owner.is_backend_enabled("ollama") is False


async def test_missing_owner_dependent_disabled(manager: ServiceManager) -> None:
    """If no service advertises the required capability the dependent is
    disabled with a reason (and not counted as failed)."""
    dependent = DependentService("model_manager", (EnablementDep("ai_chat", "ollama"),))
    manager.register(dependent)
    await manager.start_all()

    assert not dependent.started
    # ai_chat is an unmet `requires` capability → handled by the wave
    # logic; the service is recorded as disabled, not falsely "failed".
    assert "model_manager" in manager.disabled_services
    assert "model_manager" not in manager.started_services


# --- Service-target deps (dep.backend == "") ---


async def test_service_target_disabled_dependent_does_not_start(
    manager: ServiceManager,
) -> None:
    """``backend=""`` means the *service* itself must be enabled (its
    ``.enabled`` property). A disabled owner blocks the dependent."""
    owner = OwnerService("ai", "ai_chat", enabled=False)
    dependent = DependentService("dependent", (EnablementDep("ai_chat"),))
    manager.register(owner)
    manager.register(dependent)
    await manager.start_all()

    assert not dependent.started
    assert "dependent" in manager.disabled_services
    assert "ai_chat" in manager.disabled_services["dependent"] or (
        "ai" in manager.disabled_services["dependent"]
    )


async def test_service_target_enabled_dependent_starts(
    manager: ServiceManager,
) -> None:
    owner = OwnerService("ai", "ai_chat", enabled=True)
    dependent = DependentService("dependent", (EnablementDep("ai_chat"),))
    manager.register(owner)
    manager.register(dependent)
    await manager.start_all()

    assert dependent.started
    assert "dependent" in manager.started_services
    assert "dependent" not in manager.disabled_services


async def test_no_enablement_deps_unaffected(manager: ServiceManager) -> None:
    """A service with an empty ``requires_enabled`` starts as before."""
    owner = OwnerService("ai", "ai_chat", backends_enabled=frozenset())
    plain = DependentService("plain", ())
    manager.register(owner)
    manager.register(plain)
    await manager.start_all()

    assert plain.started
    assert "plain" in manager.started_services
