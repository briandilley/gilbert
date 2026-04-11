"""Tests for AuthService — login flow, sessions, provider discovery."""

from typing import Any

import pytest

from gilbert.config import AuthConfig
from gilbert.core.services.auth import AuthService
from gilbert.core.services.users import UserService
from gilbert.interfaces.auth import AuthBackend, AuthInfo, LoginMethod
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import StorageBackend

# --- Stubs ---


class StubAuthBackend(AuthBackend):
    """Auth backend that always succeeds for a known email."""

    backend_name = ""  # don't register globally

    def __init__(self, email: str = "test@example.com") -> None:
        self._email = email

    @property
    def provider_type(self) -> str:
        return "stub"

    async def initialize(self, config: dict[str, Any]) -> None:
        pass

    async def close(self) -> None:
        pass

    def get_login_method(self) -> LoginMethod:
        return LoginMethod(
            provider_type="stub",
            display_name="Stub Auth",
            method="form",
            form_action="/auth/login/stub",
        )

    async def authenticate(self, credentials: dict[str, Any]) -> AuthInfo | None:
        if credentials.get("email") == self._email:
            return AuthInfo(
                provider_type="stub",
                provider_user_id="stub_001",
                email=self._email,
                display_name="Test User",
            )
        return None


class StubStorageService(Service):
    def __init__(self, backend: StorageBackend) -> None:
        self._backend = backend

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(name="storage", capabilities=frozenset({"entity_storage"}))

    @property
    def backend(self) -> StorageBackend:
        return self._backend


class StubResolver(ServiceResolver):
    def __init__(self, services: dict[str, Service | list[Service]]) -> None:
        self._by_cap = services

    def get_capability(self, capability: str) -> Service | None:
        val = self._by_cap.get(capability)
        if isinstance(val, list):
            return val[0] if val else None
        return val

    def require_capability(self, capability: str) -> Service:
        svc = self.get_capability(capability)
        if svc is None:
            raise LookupError(f"Missing: {capability}")
        return svc

    def get_all(self, capability: str) -> list[Service]:
        val = self._by_cap.get(capability)
        if isinstance(val, list):
            return val
        return [val] if val else []


# --- Fixtures ---


@pytest.fixture
async def user_service(sqlite_storage: StorageBackend) -> UserService:
    svc = UserService(root_password_hash="", default_roles=["user"])
    resolver = StubResolver({"entity_storage": StubStorageService(sqlite_storage)})
    await svc.start(resolver)
    return svc


def _make_auth_service_resolver(
    sqlite_storage: StorageBackend,
    user_service: UserService,
    providers: list[Service] | None = None,
) -> StubResolver:
    """Build a resolver that wires up auth dependencies and optional providers."""
    caps: dict[str, Service | list[Service]] = {
        "users": user_service,
        "entity_storage": StubStorageService(sqlite_storage),
    }
    if providers:
        caps["authentication_provider"] = providers
    return caps


@pytest.fixture
async def auth_service(
    sqlite_storage: StorageBackend, user_service: UserService
) -> AuthService:
    """AuthService with NO providers (bare)."""
    config = AuthConfig(
        enabled=True,
        providers=[],
        session_ttl_seconds=3600,
    )
    svc = AuthService(config)
    caps = _make_auth_service_resolver(sqlite_storage, user_service)
    resolver = StubResolver(caps)
    await svc.start(resolver)
    return svc


@pytest.fixture
async def auth_service_with_provider(
    sqlite_storage: StorageBackend, user_service: UserService
) -> AuthService:
    """AuthService with a StubAuthBackend injected."""
    config = AuthConfig(
        enabled=True,
        providers=[],
        session_ttl_seconds=3600,
    )
    svc = AuthService(config)
    caps = _make_auth_service_resolver(sqlite_storage, user_service)
    resolver = StubResolver(caps)
    await svc.start(resolver)
    # Inject stub backend after start (local is already there)
    stub = StubAuthBackend()
    await stub.initialize({})
    svc._backends["stub"] = stub
    return svc


# --- Tests ---


async def test_local_provider_always_present(auth_service: AuthService) -> None:
    methods = auth_service.get_login_methods()
    assert len(methods) >= 1
    assert any(m.provider_type == "local" for m in methods)


async def test_local_backend_provides_login_method(
    auth_service: AuthService,
) -> None:
    methods = auth_service.get_login_methods()
    local_methods = [m for m in methods if m.provider_type == "local"]
    assert len(local_methods) == 1
    assert local_methods[0].method == "form"


async def test_authenticate_unknown_provider(auth_service: AuthService) -> None:
    result = await auth_service.authenticate("nonexistent", {"email": "a@b.com"})
    assert result is None


async def test_authenticate_success_creates_user_and_session(
    auth_service_with_provider: AuthService, user_service: UserService
) -> None:
    ctx = await auth_service_with_provider.authenticate(
        "stub", {"email": "test@example.com"}
    )
    assert ctx is not None
    assert ctx.email == "test@example.com"
    assert ctx.session_id is not None
    assert ctx.provider == "stub"

    # User should exist now.
    user = await user_service.get_user_by_email("test@example.com")
    assert user is not None
    assert user["display_name"] == "Test User"


async def test_authenticate_failure(
    auth_service_with_provider: AuthService,
) -> None:
    result = await auth_service_with_provider.authenticate(
        "stub", {"email": "wrong@example.com"}
    )
    assert result is None


async def test_validate_session(
    auth_service_with_provider: AuthService,
) -> None:
    ctx = await auth_service_with_provider.authenticate(
        "stub", {"email": "test@example.com"}
    )
    assert ctx is not None
    session_id = ctx.session_id
    assert session_id is not None

    validated = await auth_service_with_provider.validate_session(session_id)
    assert validated is not None
    assert validated.user_id == ctx.user_id
    assert validated.email == "test@example.com"


async def test_validate_invalid_session(auth_service: AuthService) -> None:
    result = await auth_service.validate_session("nonexistent_session")
    assert result is None


async def test_invalidate_session(
    auth_service_with_provider: AuthService,
) -> None:
    ctx = await auth_service_with_provider.authenticate(
        "stub", {"email": "test@example.com"}
    )
    assert ctx is not None and ctx.session_id is not None

    await auth_service_with_provider.invalidate_session(ctx.session_id)
    result = await auth_service_with_provider.validate_session(ctx.session_id)
    assert result is None


async def test_authenticate_links_existing_user(
    auth_service_with_provider: AuthService, user_service: UserService
) -> None:
    """If a user with the same email already exists, link rather than create."""
    await user_service.create_user("existing", {
        "email": "test@example.com",
        "display_name": "Existing",
    })

    ctx = await auth_service_with_provider.authenticate(
        "stub", {"email": "test@example.com"}
    )
    assert ctx is not None
    assert ctx.user_id == "existing"


async def test_authenticate_does_not_link_root(
    auth_service_with_provider: AuthService, user_service: UserService
) -> None:
    """Auth should not add provider links to the root user."""
    root = await user_service.get_user("root")
    assert root is not None
    assert root["provider_links"] == []
