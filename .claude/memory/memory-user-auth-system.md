# User & Authentication System

## Summary
Multi-user support with local accounts, external provider syncing, role-based access, and session authentication. Auth backends and user provider backends use the standard registry pattern (`backend_name`, `__init_subclass__`). AuthService owns auth backends internally; UserService owns user provider backends.

## Details

### Interfaces
- `UserContext` (frozen dataclass) — immutable identity flowing through the system. Fields: user_id, email, display_name, roles (frozenset), provider, session_id, metadata. Class-level sentinels: `SYSTEM` (background jobs, bypasses RBAC), `GUEST` (unauthenticated local visitors, has "everyone" role).
- `AuthInfo` (frozen dataclass) — returned by auth backends after successful authentication.
- `AuthBackend` (ABC) — pluggable auth backend with registry pattern. Has `backend_name`, `_registry`, `__init_subclass__`, `backend_config_params()`. Methods: `initialize()`, `close()`, `authenticate()`, `handle_callback()`, `sync_users()`, `get_role_mappings()`.
- `LoginMethod` (dataclass) — describes how an auth method appears on the login page (form vs redirect button).
- `UserProviderBackend` (ABC) — external user source with registry pattern. Has `backend_name`, `_registry`, `__init_subclass__`, `backend_config_params()`. Methods: `initialize()`, `close()`, `list_external_users()`, `get_external_user()`, `get_external_user_by_email()`, `list_groups()`.
- `ExternalUser` (dataclass) — user record from external provider.
- `UserBackend` (ABC) — user CRUD, provider links, roles, remote user cache.

### Concrete Backends
- `LocalAuthBackend` — email/password auth with argon2. Renders form on login page.
- `GoogleAuthBackend` — Google OAuth redirect flow. Renders "Sign in with Google" button.
- `GoogleDirectoryBackend` — reads users/groups from Google Admin Directory API. User provider backend.

### Services
- `UserService` — capability: `users`, `ai_tools`. Always registered. Wraps `StorageUserBackend`. Creates root user on startup. Owns `UserProviderBackend` instances and syncs on demand during `list_users()`.
- `AuthService` — capability: `authentication`. Owns `AuthBackend` instances internally (no separate services per backend). Manages sessions in `auth_sessions` collection. Methods: `authenticate()`, `handle_callback()`, `get_login_methods()`, `validate_session()`, `invalidate_session()`.

### Storage
- `StorageUserBackend` — implements `UserBackend` over `StorageBackend`. Collections: `users`, `provider_users`.
- Root user: id="root", email="root@localhost", is_root=true, cannot be deleted or linked to external providers.

### Web Auth
- `AuthMiddleware` — checks cookie/bearer token, validates session, redirects unauthenticated to login.
- Login page renders all available auth methods dynamically (forms and OAuth buttons with "or" dividers).
- Routes: GET `/auth/login`, POST `/auth/login/local`, GET `/auth/login/google/start`, GET `/auth/login/google/callback`, POST `/auth/logout`, GET `/auth/me`.
- Logout button in nav header.

### Configuration
Auth and user config is stored in entity storage (not YAML). Auth backends declare their own `backend_config_params()` which are merged into the auth service's config params. Credentials (OAuth client ID/secret, API keys) are inline in backend settings, not referenced by name.

## Related
- `src/gilbert/interfaces/auth.py` — UserContext, AuthInfo, AuthBackend, LoginMethod
- `src/gilbert/interfaces/users.py` — UserBackend, UserProviderBackend, ExternalUser
- `src/gilbert/core/services/auth.py` — AuthService (session mgmt, owns auth backends)
- `src/gilbert/core/services/users.py` — UserService (owns user provider backends)
- `src/gilbert/integrations/local_auth.py` — LocalAuthBackend
- `src/gilbert/integrations/google_auth.py` — GoogleAuthBackend
- `src/gilbert/integrations/google_directory.py` — GoogleDirectoryBackend
- `src/gilbert/web/auth.py` — middleware and dependencies
- `src/gilbert/web/routes/auth.py` — auth routes
