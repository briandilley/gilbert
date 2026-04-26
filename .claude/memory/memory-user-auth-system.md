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
- `LocalAuthBackend` — bundled in core at `src/gilbert/integrations/local_auth.py`. Email/password auth with argon2. Renders form on login page. Satisfies `UserBackendAware` so AuthService can inject the user backend after `initialize()`.
- `GoogleAuthBackend` — lives in `std-plugins/google/google_auth.py`. Google OAuth redirect flow. Renders "Sign in with Google" button. Satisfies `TunnelAwareAuthBackend` so AuthService injects the tunnel provider for public callback URLs.
- `GoogleDirectoryBackend` — lives in `std-plugins/google/google_directory.py`. Reads users/groups from Google Admin Directory API. User provider backend.

### Services
- `UserService` — capability: `users`, `ai_tools`. Always registered. Wraps `StorageUserBackend`. Creates root user on startup. Owns `UserProviderBackend` instances (as a dict keyed on `backend_name`) and syncs on demand during `list_users()`.
- `AuthService` — capability: `authentication`. Owns `AuthBackend` instances internally (no separate services per backend). Manages sessions in `auth_sessions` collection. Methods: `authenticate()`, `handle_callback()`, `get_login_methods()`, `validate_session()`, `invalidate_session()`.

### Storage
- `StorageUserBackend` — implements `UserBackend` over `StorageBackend`. Collections: `users`, `provider_users`.
- Root user: id="root", username="root", email="" (empty by default), is_root=true, roles=["admin"], cannot be deleted or linked to external providers. Password hash comes from `auth.root_password` config; if empty the user exists but local login is impossible — `UserService._warn_if_root_unusable` logs a WARNING on every boot in that state. Setting `auth.root_password` in `.gilbert/config.yaml` before the first start is the supported way to make root loggable (first-run seeding copies the value into entity storage, which then wins).

### Web Auth
- `AuthMiddleware` — checks cookie/bearer token, validates session, redirects unauthenticated to login.
- Login page renders all available auth methods dynamically (forms and OAuth buttons with "or" dividers).
- Routes: GET `/auth/login`, POST `/auth/login/local`, GET `/auth/login/google/start`, GET `/auth/login/google/callback`, POST `/auth/logout`, GET `/auth/me`.
- Logout button in nav header.

### Configuration — Generic Backend Discovery
Auth and user config is stored in entity storage (not YAML).

Neither `AuthService` nor `UserService` hard-codes any backend name. Both services iterate their respective backend registry at `start()` and `config_params()` time:

- **`config_params()`** appends service-level params, then for each registered backend emits `<backend_name>.enabled` plus a copy of each param from `cls.backend_config_params()` namespaced as `<backend_name>.<key>` with `backend_param=True`. `LocalAuthBackend` is the only exception — it's always on, so AuthService skips it in the loop (no `enabled` toggle, no config subsection).

- **`start()`** iterates the registry and enables any non-local backend whose `<backend_name>.enabled` flag is set, initializing it with its own subsection. Capability injection is protocol-driven: `isinstance(instance, UserBackendAware)` → inject user backend (local auth); `isinstance(instance, TunnelAwareAuthBackend)` → inject tunnel provider (OAuth).

- **`config_actions()`** iterates the registry, using the live instance if present or a fresh probe otherwise, and prefixes every action key with `<backend_name>.` so the UI can disambiguate and `invoke_config_action()` can route back by splitting on the first `.`.

Adding a new auth backend or user provider plugin requires zero changes in `src/gilbert/core/services/auth.py` or `users.py` — the service discovers it via the registry.

**Legacy migration:** pre-refactor, `auth.google_oauth.*` was the hard-coded config section for Google OAuth. `AuthService._migrate_legacy_keys()` runs once on startup, copies any surviving values from `auth.google_oauth.*` into `auth.google.*` (matching `GoogleAuthBackend.backend_name`), and drops the old subsection. Users don't need to re-enter credentials.

## Related
- `src/gilbert/interfaces/auth.py` — UserContext, AuthInfo, AuthBackend, LoginMethod, `UserBackendAware` and `TunnelAwareAuthBackend` runtime-checkable protocols
- `src/gilbert/interfaces/users.py` — UserBackend, UserProviderBackend, ExternalUser
- `src/gilbert/core/services/auth.py` — AuthService (session mgmt, owns auth backends, legacy key migration)
- `src/gilbert/core/services/users.py` — UserService (owns user provider backends)
- `src/gilbert/integrations/local_auth.py` — LocalAuthBackend (bundled in core)
- `std-plugins/google/google_auth.py` — GoogleAuthBackend
- `std-plugins/google/google_directory.py` — GoogleDirectoryBackend
- `src/gilbert/web/auth.py` — middleware and dependencies
- `src/gilbert/web/routes/auth.py` — auth routes
