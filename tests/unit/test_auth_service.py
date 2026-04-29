"""Tests for AuthService — login flow, sessions, provider discovery."""

from typing import Any

import pytest

from gilbert.config import AuthConfig
from gilbert.core.services.auth import AuthService
from gilbert.core.services.users import UserService
from gilbert.interfaces.auth import (
    AuthBackend,
    AuthInfo,
    GuestPolicy,
    LoginMethod,
    OAuthLoginBackend,
)
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
        self.backend = backend
        self.raw_backend = backend

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(name="storage", capabilities=frozenset({"entity_storage"}))

    def create_namespaced(self, namespace: str) -> Any:
        from gilbert.interfaces.storage import NamespacedStorageBackend

        return NamespacedStorageBackend(self.backend, namespace)


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
async def auth_service(sqlite_storage: StorageBackend, user_service: UserService) -> AuthService:
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
    ctx = await auth_service_with_provider.authenticate("stub", {"email": "test@example.com"})
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
    result = await auth_service_with_provider.authenticate("stub", {"email": "wrong@example.com"})
    assert result is None


async def test_validate_session(
    auth_service_with_provider: AuthService,
) -> None:
    ctx = await auth_service_with_provider.authenticate("stub", {"email": "test@example.com"})
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
    ctx = await auth_service_with_provider.authenticate("stub", {"email": "test@example.com"})
    assert ctx is not None and ctx.session_id is not None

    await auth_service_with_provider.invalidate_session(ctx.session_id)
    result = await auth_service_with_provider.validate_session(ctx.session_id)
    assert result is None


async def test_authenticate_links_existing_user(
    auth_service_with_provider: AuthService, user_service: UserService
) -> None:
    """If a user with the same email already exists, link rather than create."""
    await user_service.create_user(
        "existing",
        {
            "email": "test@example.com",
            "display_name": "Existing",
        },
    )

    ctx = await auth_service_with_provider.authenticate("stub", {"email": "test@example.com"})
    assert ctx is not None
    assert ctx.user_id == "existing"


async def test_authenticate_does_not_link_root(
    auth_service_with_provider: AuthService, user_service: UserService
) -> None:
    """Auth should not add provider links to the root user."""
    root = await user_service.get_user("root")
    assert root is not None
    assert root["provider_links"] == []


# ---- OAuthLoginBackend protocol ----
#
# The generic /auth/login/<provider_type>/start and .../callback routes
# in web/routes/auth.py use ``isinstance(backend, OAuthLoginBackend)``
# to decide whether a backend can drive a redirect-based login. A
# backend that defines the two structural methods must satisfy the
# protocol; one missing either must not.


class _StubOAuthBackend:
    """Minimal backend that structurally satisfies OAuthLoginBackend."""

    def get_callback_url(self, request_base_url: str) -> str:
        return f"{request_base_url}/cb"

    def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        return f"https://provider/auth?r={redirect_uri}&s={state}"


class _StubPartialBackend:
    """Only has ``get_callback_url`` — should NOT satisfy the protocol."""

    def get_callback_url(self, request_base_url: str) -> str:
        return ""


def test_oauth_login_backend_protocol_satisfied() -> None:
    assert isinstance(_StubOAuthBackend(), OAuthLoginBackend)


def test_oauth_login_backend_protocol_not_satisfied_when_incomplete() -> None:
    assert not isinstance(_StubPartialBackend(), OAuthLoginBackend)


# --- GuestPolicy ----------------------------------------------------


def test_auth_service_satisfies_guest_policy(auth_service: AuthService) -> None:
    """AuthService must satisfy the GuestPolicy protocol so the web
    layer can ask whether unauthenticated visitors are allowed."""
    assert isinstance(auth_service, GuestPolicy)


def test_allow_guests_default_true(auth_service: AuthService) -> None:
    """Default keeps the previous behavior — local guests allowed."""
    assert auth_service.is_guest_allowed() is True


@pytest.mark.asyncio
async def test_on_config_changed_toggles_allow_guests(auth_service: AuthService) -> None:
    """Setting ``allow_guests`` via the Settings UI must propagate
    immediately — the web middleware reads this on every request, so
    a stale value would leave the system open after an admin
    explicitly turned guests off."""
    await auth_service.on_config_changed({"allow_guests": False})
    assert auth_service.is_guest_allowed() is False

    await auth_service.on_config_changed({"allow_guests": True})
    assert auth_service.is_guest_allowed() is True


@pytest.mark.asyncio
async def test_on_config_changed_ignores_unrelated_keys(auth_service: AuthService) -> None:
    """A config payload that doesn't mention ``allow_guests`` must
    leave the current value alone — otherwise tweaking, say,
    ``session_ttl_seconds`` would silently flip the guest gate."""
    await auth_service.on_config_changed({"allow_guests": False})
    await auth_service.on_config_changed({"session_ttl_seconds": 1234})
    assert auth_service.is_guest_allowed() is False


# --- revoke_user_sessions / change_password / user_has_password ----


async def _seed_local_user(
    auth_svc: AuthService,
    user_service: UserService,
    user_id: str = "u1",
    email: str = "alice@example.com",
    password: str = "correct horse battery staple",
) -> None:
    """Create a user with a real argon2 password_hash via the local backend.

    A unique ``username`` is required: ``users.username`` is a UNIQUE
    index and SQLite's ``INSERT OR REPLACE`` semantics mean two rows
    with the same (empty) username silently overwrite each other.
    """
    from gilbert.integrations.local_auth import LocalAuthBackend

    local = auth_svc._backends["local"]
    assert isinstance(local, LocalAuthBackend)
    await user_service.create_user(
        user_id,
        {
            "username": user_id,
            "email": email,
            "display_name": "Alice",
            "password_hash": local.hash_password(password),
        },
    )


async def test_revoke_user_sessions_deletes_all(
    auth_service_with_provider: AuthService, user_service: UserService
) -> None:
    """All sessions for a user are gone after revoke_user_sessions."""
    a = await auth_service_with_provider.authenticate("stub", {"email": "test@example.com"})
    b = await auth_service_with_provider.authenticate("stub", {"email": "test@example.com"})
    assert a and b and a.session_id and b.session_id and a.session_id != b.session_id

    revoked = await auth_service_with_provider.revoke_user_sessions(a.user_id)
    assert revoked == 2
    assert await auth_service_with_provider.validate_session(a.session_id) is None
    assert await auth_service_with_provider.validate_session(b.session_id) is None


async def test_revoke_user_sessions_keeps_excepted(
    auth_service_with_provider: AuthService,
) -> None:
    """except_session_id is preserved so a 'change-password' caller
    isn't bounced from the device they just used to update it."""
    a = await auth_service_with_provider.authenticate("stub", {"email": "test@example.com"})
    b = await auth_service_with_provider.authenticate("stub", {"email": "test@example.com"})
    assert a and b and a.session_id and b.session_id

    revoked = await auth_service_with_provider.revoke_user_sessions(
        a.user_id, except_session_id=a.session_id
    )
    assert revoked == 1
    assert await auth_service_with_provider.validate_session(a.session_id) is not None
    assert await auth_service_with_provider.validate_session(b.session_id) is None


async def test_revoke_user_sessions_other_users_untouched(
    auth_service: AuthService, user_service: UserService
) -> None:
    """Revoking sessions for one user must not affect anyone else."""
    await user_service.create_user("u_mine", {"email": "mine@example.com"})
    await user_service.create_user("u_other", {"email": "other@example.com"})
    mine = await auth_service._create_session("u_mine", "local")
    other = await auth_service._create_session("u_other", "local")

    revoked = await auth_service.revoke_user_sessions("u_mine")
    assert revoked == 1
    assert await auth_service.validate_session(mine) is None
    assert await auth_service.validate_session(other) is not None


async def test_change_password_success(
    auth_service: AuthService, user_service: UserService
) -> None:
    """Happy path: old password verifies, new password takes effect."""
    await _seed_local_user(auth_service, user_service, password="oldpass12")

    await auth_service.change_password("u1", "oldpass12", "newpass12345")

    info = await auth_service._backends["local"].authenticate(  # type: ignore[attr-defined]
        {"email": "alice@example.com", "password": "newpass12345"}
    )
    assert info is not None
    bad = await auth_service._backends["local"].authenticate(  # type: ignore[attr-defined]
        {"email": "alice@example.com", "password": "oldpass12"}
    )
    assert bad is None


async def test_change_password_wrong_old(
    auth_service: AuthService, user_service: UserService
) -> None:
    await _seed_local_user(auth_service, user_service, password="oldpass12")

    with pytest.raises(ValueError, match="incorrect"):
        await auth_service.change_password("u1", "WRONG", "newpass12345")


async def test_change_password_too_short(
    auth_service: AuthService, user_service: UserService
) -> None:
    """Length check runs BEFORE password verification so we don't
    waste an argon2 verify on something we'd reject anyway."""
    await _seed_local_user(auth_service, user_service, password="oldpass12")

    with pytest.raises(ValueError, match="at least 8"):
        await auth_service.change_password("u1", "oldpass12", "short")


async def test_change_password_no_existing_hash(
    auth_service: AuthService, user_service: UserService
) -> None:
    """A user with no password_hash (e.g. OAuth-only) gets a clear
    error rather than a silent 'wrong old password'."""
    await user_service.create_user(
        "oauth_user",
        {"email": "oauth@example.com", "display_name": "OAuth"},
    )

    with pytest.raises(ValueError, match="no password set"):
        await auth_service.change_password("oauth_user", "anything", "newpass12345")


async def test_change_password_revokes_other_sessions(
    auth_service_with_provider: AuthService, user_service: UserService
) -> None:
    """Changing the password kills every other session — the device
    you changed it from stays signed in via keep_session_id."""
    await _seed_local_user(
        auth_service_with_provider, user_service, user_id="u1", password="oldpass12"
    )
    s1 = await auth_service_with_provider._create_session("u1", "local")
    s2 = await auth_service_with_provider._create_session("u1", "local")

    await auth_service_with_provider.change_password(
        "u1", "oldpass12", "newpass12345", keep_session_id=s1
    )

    assert await auth_service_with_provider.validate_session(s1) is not None
    assert await auth_service_with_provider.validate_session(s2) is None


async def test_user_has_password(
    auth_service: AuthService, user_service: UserService
) -> None:
    await _seed_local_user(auth_service, user_service, user_id="u1", password="x" * 12)
    await user_service.create_user(
        "u2",
        {"username": "u2", "email": "no-pw@example.com", "display_name": "No Password"},
    )

    assert await auth_service.user_has_password("u1") is True
    assert await auth_service.user_has_password("u2") is False
    assert await auth_service.user_has_password("nonexistent") is False
    assert await auth_service.user_has_password("") is False
