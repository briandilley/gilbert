# Gilbert

AI assistant for home and business automation. Extensible, plugin-driven architecture with discoverable services, integrations, and AI capabilities.

## Tech Stack

- **Language:** Python (3.12+), managed via uv (always use `uv run` to execute commands, `uv add` for dependencies — never use pip directly)
- **Database:** SQLite (local store), interface-abstracted for swappable backends
- **Storage API:** Generic entity store with query interface (not SQL-shaped). New entity types require no migrations.
- **Infrastructure:** Docker for dependent services
- **Testing:** pytest with mocks; database tests use a real test SQLite database
- **Logging:** Python logging framework throughout. Colored console output (stderr), file logging, and separate AI API call log.

## Architecture

### Interface-First Design

Everything is designed as an abstract interface (Python ABCs) with concrete implementations. This applies to:

- **Data layer** — e.g., `StorageBackend` ABC with `SQLiteStorage` implementation (swappable to PostgreSQL, etc.)
- **Backend abstractions** — e.g., `TTSBackend` ABC with `ElevenLabsTTS`, `AIBackend` ABC with `AnthropicAI`, `AuthBackend` with `LocalAuth`/`GoogleAuth`, `VisionBackend` with `AnthropicVision`, `TunnelBackend` with `NgrokTunnel`, etc.
- **Service-level protocols** — e.g., `Configurable` for runtime config, `ToolProvider` for AI tool registration
- **Capability protocols** — e.g., `ConfigurationReader` for config access, `SchedulerProvider` for job scheduling, `EventBusProvider` for event pub/sub, `StorageProvider` for entity storage, `AccessControlProvider` for RBAC queries

New integrations are added by implementing the relevant backend ABC. The `__init_subclass__` auto-registration means just defining the class is enough — no wiring code needed.

### Plugin System

Plugins are loaded from:
- **GitHub URLs** — fetched and installed at runtime
- **Local file paths** — for development or private plugins
- **Plugin directories** — scanned for subdirectories containing `plugin.yaml`

Plugins implement published interfaces to extend Gilbert with new integrations or capabilities. Plugins that need configuration implement `Configurable` and read their config via the `ConfigurationReader` protocol (resolved from the `"configuration"` capability), not from `context.config` (which only contains the initial config snapshot at load time).

### Installation Data Directory (`.gilbert/`)

The `.gilbert/` folder is the per-installation data directory. It is **gitignored** and auto-created on first run. Users clone the repo and run it — no source files need editing.

Contents:
- `config.yaml` — bootstrap overrides only (storage, logging, web)
- `gilbert.db` — SQLite database (includes all runtime configuration)
- `gilbert.log` / `ai_calls.log` — log files
- `plugins/` — cached plugins fetched from GitHub
- `chromadb/` — vector store for knowledge service

### Configuration System

Configuration is split between YAML (bootstrap) and entity storage (everything else).

**Bootstrap config (YAML)** — only `storage`, `logging`, and `web` sections live in YAML because they are needed before entity storage is available:

1. `gilbert.yaml` (committed) — bootstrap defaults
2. `.gilbert/config.yaml` (gitignored) — per-installation bootstrap overrides, deep-merged on top

**Runtime config (entity storage)** — all other configuration is stored in the `gilbert.config` entity collection and managed via the web UI at `/settings`. On first run, non-bootstrap sections from YAML are seeded into entity storage. After that, entity storage is the source of truth.

The `ConfigurationService` provides read/write access to all config, persists changes, and notifies or restarts affected services. Services implement the `Configurable` protocol to declare their parameters and handle live updates.

### Backend Pattern

All swappable components follow a universal backend pattern:

- **`backend_name`** class attribute for automatic registry
- **`backend_config_params()`** classmethod declaring backend-specific settings as `ConfigParam` objects
- **`__init_subclass__`** auto-registration into the backend's `_registry` dict
- **`initialize(config)` / `close()`** lifecycle methods

The owning service exposes backend params in the Settings UI under a "Backend Settings" section (params with `backend_param=True`). Backend selection and credentials (API keys, etc.) are configured directly on each backend via the Settings UI.

**How services discover backends** — services must never directly import concrete backend classes. Instead:

1. Import the integration module as a side-effect to trigger `__init_subclass__` registration:
   ```python
   try:
       import gilbert.integrations.elevenlabs_tts  # noqa: F401
   except ImportError:
       pass
   ```
2. Look up the backend class by name from the ABC registry:
   ```python
   backends = TTSBackend.registered_backends()
   cls = backends.get("elevenlabs")
   if cls:
       backend = cls()
       await backend.initialize(config)
   ```

**Never do this** (bypasses the registry):
```python
# WRONG — direct import of concrete backend
from gilbert.integrations.elevenlabs_tts import ElevenLabsTTS
backend = ElevenLabsTTS()
```

Backend ABCs following this pattern: `AIBackend`, `TTSBackend`, `AuthBackend`, `UserProviderBackend`, `TunnelBackend`, `VisionBackend`, `DocumentBackend`, `EmailBackend`, `MusicBackend`, `SpeakerBackend`, `DoorbellBackend`, `WebSearchBackend`.

### Capability Protocols

Services resolve dependencies via `resolver.get_capability("name")`, which returns the abstract `Service` type. To access domain-specific methods without depending on concrete service classes, the codebase defines `@runtime_checkable Protocol` classes in `interfaces/`. Services use `isinstance` checks against these protocols — never against concrete service classes.

| Protocol | Module | Capability | Key methods |
|---|---|---|---|
| `ConfigurationReader` | `interfaces/configuration.py` | `"configuration"` | `get()`, `get_section()`, `get_section_safe()`, `set()` |
| `SchedulerProvider` | `interfaces/scheduler.py` | `"scheduler"` | `add_job()`, `remove_job()`, `enable_job()`, `disable_job()`, `list_jobs()`, `get_job()`, `run_now()` |
| `EventBusProvider` | `interfaces/events.py` | `"event_bus"` | `bus` property → `EventBus` |
| `StorageProvider` | `interfaces/storage.py` | `"entity_storage"` | `backend` / `raw_backend` properties, `create_namespaced()` |
| `AccessControlProvider` | `interfaces/auth.py` | `"access_control"` | `get_role_level()`, `get_effective_level()`, `resolve_rpc_level()` |
| `SkillsProvider` | `interfaces/skills.py` | `"skills"` | `get_active_skills()`, `get_active_allowed_tools()`, `build_skills_context()` |
| `PresenceProvider` | `interfaces/presence.py` | `"presence"` | `who_is_here()` |
| `TTSProvider` | `interfaces/tts.py` | `"text_to_speech"` | `synthesize()` |
| `TunnelProvider` | `interfaces/tunnel.py` | `"tunnel"` | `public_url` property |
| `ServiceEnumerator` | `interfaces/service.py` | (resolver) | `list_services()`, `restart_service()`, `started_services`, `failed_services` |

**Usage pattern** (this is the only correct way to access service capabilities):

```python
from gilbert.interfaces.configuration import ConfigurationReader

config_svc = resolver.get_capability("configuration")
if isinstance(config_svc, ConfigurationReader):
    section = config_svc.get_section("my_namespace")
```

**Never do this** (imports a concrete class from `core/services/`):

```python
# WRONG — creates a concrete dependency
from gilbert.core.services.configuration import ConfigurationService
if isinstance(config_svc, ConfigurationService):
    ...
```

When a service exposes new methods that other services need, add a `@runtime_checkable Protocol` in the appropriate `interfaces/` module rather than having consumers import the concrete service class.

### Configurable Protocol

Services that accept runtime configuration implement `Configurable`:

- **`config_namespace`** — config section name (e.g., `"ai"`, `"tts"`)
- **`config_category`** — UI grouping (e.g., `"Media"`, `"Intelligence"`, `"Security"`)
- **`config_params()`** — declares all parameters with types, descriptions, defaults
- **`on_config_changed(config)`** — called when tunable params change at runtime

`ConfigParam` fields: `key`, `type`, `description`, `default`, `restart_required`, `sensitive` (masked in UI), `choices` (dropdown), `multiline` (textarea), `choices_from` (dynamic choices), `backend_param` (declared by backend, not service).

### AI Context Profiles

AI interactions use **named profiles** that control which tools are available. This decouples tool access from code — profiles are stored in entity storage (`ai_profiles` collection) and manageable at runtime via AI tools or the web UI at `/roles/profiles`.

**How it works:**

1. Services declare named AI interactions in `ServiceInfo.ai_calls` (e.g., `frozenset({"sales_initial_email", "sales_reply"})`).
2. Callers pass `ai_call="name"` to `ai.chat()`.
3. The AI service resolves the call name to a profile via the assignments table.
4. The profile's `tool_mode` (all/include/exclude) and `tools` list filter which tools the AI can see.
5. RBAC then filters by the user's role level, using the profile's optional `tool_roles` overrides.

**Key rules:**
- `ai_call=None` means no profile filtering (all tools available, RBAC still applies).
- Unassigned call names fall back to the `default` profile (all tools).
- Profiles control *which* tools are available; RBAC controls *who* can use them. Both always apply.
- Default profiles (`default`, `human_chat`, `text_only`, `sales_agent`) are seeded from built-in constants in `AIService` on first run.
- New services that call `ai.chat()` should declare their `ai_calls` and pass the call name. The profile assignment can be configured without code changes.

### Key Directories

- `src/gilbert/interfaces/` — ABCs, protocol definitions, shared data types, and WS connection protocol (`WsConnectionBase`, `RpcHandler`). Includes ACL policy defaults (`acl.py`), AI profile dataclass, document type mappings, and all capability protocols.
- `src/gilbert/core/` — Application bootstrap, service manager, event bus, logging, config loading, shared business logic (`core/chat.py`)
- `src/gilbert/core/services/` — Service wrappers that expose components as discoverable services (including WS RPC handlers via `WsHandlerProvider`)
- `src/gilbert/integrations/` — Concrete backend implementations (e.g., ElevenLabs TTS, Anthropic AI, ngrok tunnel, Google auth/directory, Gmail, GDrive)
- `src/gilbert/storage/` — Storage backend implementations (SQLite)
- `src/gilbert/plugins/` — Plugin loader
- `src/gilbert/web/` — Web server, SPA assets, API routes (thin layer — no business logic)
- `tests/unit/` — Unit tests with mocks
- `tests/integration/` — Tests against real backends (e.g., SQLite)
- `.gilbert/` — Per-installation data directory (gitignored): bootstrap config, database, logs

### Layer Dependency Rules

The codebase is organized into layers with strict import rules. Violations of these rules create coupling that defeats the plugin/backend architecture.

```
interfaces/     ← depends on nothing (pure abstractions + shared data)
    ↑
core/           ← depends on interfaces/ only
    ↑
integrations/   ← depends on interfaces/ only
storage/        ← depends on interfaces/ only
    ↑
web/            ← depends on interfaces/ and core/ (thin routing layer)
    ↑
app.py          ← composition root, may import anything
```

**Specific rules:**

1. **`interfaces/`** — No imports from `core/`, `integrations/`, `storage/`, or `web/`. Only standard library, third-party types, and cross-references within `interfaces/`.

2. **`core/services/`** — Import from `interfaces/` for types and protocols. Never import from `integrations/` except as side-effect imports (`import gilbert.integrations.foo  # noqa: F401`) to trigger backend registration. Never import from `web/`.

3. **`integrations/`** — Import from `interfaces/` only. Never import from `core/services/`, `web/`, or other integrations. If two integrations share data (e.g., file extension mappings), that data belongs in `interfaces/`.

4. **`web/`** — A thin routing and presentation layer. Import from `interfaces/` for protocols and types, from `core/` for shared business logic. Route handlers should delegate to services — not implement authorization logic, build AI prompts, resolve backends, or construct third-party API URLs. If a route handler is doing more than parsing the request, calling a service method, and formatting the response, the logic belongs in a service or `core/` module.

5. **`app.py`** (composition root) — The only place that legitimately imports concrete service and integration classes to wire them together. This is standard DI practice.

6. **Shared data** — Constants, mappings, and policy data used by multiple layers belong in `interfaces/`. Examples: `EXT_TO_DOCUMENT_TYPE` in `interfaces/knowledge.py`, ACL policy defaults in `interfaces/acl.py`, role level constants in `interfaces/acl.py`.

7. **Tests** — Tests are composition roots for test scenarios and may import concrete classes directly. Test fakes for services should satisfy the relevant `@runtime_checkable Protocol` (e.g., a fake config service should implement `get()`, `get_section()`, `get_section_safe()`, and `set()` to satisfy `ConfigurationReader`).

## Agent Memory System

Claude AI agents use a file-based memory system located at `.claude/memory/` to retain knowledge about Gilbert's services, integrations, architectural decisions, and other project details across conversations.

### How It Works

1. **Index file:** `.claude/memory/MEMORIES.md` contains a flat list of all memories. Each entry is a one-line description with a markdown link to the detailed memory file. This index is the only file loaded into context by default.
2. **Memory files:** Individual files in `.claude/memory/` named `memory-<slug>.md` containing detailed information about a specific topic.
3. **Loading on demand:** When working on a task, check the index to see if a relevant memory exists. If so, load the memory file for detailed context. **Always mention in the terminal when loading a memory** (e.g., "Loading memory: facial-recognition-service").

### Keeping Memories Current

**This is not optional.** Memories are how future Claude sessions understand the system. Treat them like documentation that matters.

- **Create** a memory after designing or implementing a new service, integration, or significant component.
- **Create** a memory after making a significant architectural decision — record the decision and rationale.
- **Update** a memory when its system changes — new fields, renamed classes, changed behavior, new dependencies.
- **Remove** a memory when its system is deleted or replaced. Delete the file and remove it from the index. Stale memories are worse than no memories.
- After learning something non-obvious about a third-party integration — capture it.
- At the end of any significant work session, review whether affected memories need updating.
- **Before every commit**, review all memories touched by the changes being committed. Update stale memories, delete obsolete ones, and create new ones for anything significant that was added. Do not commit code that makes existing memories inaccurate.

### Memory File Format

All memory files follow this template:

```markdown
# <Title>

## Summary
One or two sentences describing what this is.

## Details
Detailed information — interfaces involved, key classes, configuration,
how it connects to the rest of the system, design decisions and rationale,
gotchas, etc.

## Related
- Links to related memory files or source paths
```

### Index Format (MEMORIES.md)

```markdown
# Memories

- [Facial Recognition Service](memory-facial-recognition-service.md) — identifies users by their face via camera integrations
- [Lutron RadioRA2 Integration](memory-lutron-radiora2.md) — controls Lutron lighting and shades
```

### Rules

- Keep the index concise — one line per memory, under 120 characters.
- Memory file names use the pattern `memory-<slug>.md` with kebab-case slugs.
- Do not dump entire source files into memories. Capture the *knowledge* — what it is, why it exists, how it fits together.
- Always keep the index in sync when creating, renaming, or deleting memory files.

## Privacy

**Never put private or personal information in tracked files.** API keys, credentials, voice IDs, email addresses, and any other personal data must only go in gitignored locations (entity storage in `.gilbert/gilbert.db`, `.gilbert/config.yaml`, etc.). This includes `.claude/memory/` files — those are committed to the repo. If you need to remember something private, use the user-scoped memory system instead of the project-scoped one.

## Development Guidelines

- **Always write tests.** Unit tests use mocks for external dependencies. Database tests hit a real test SQLite database — no mocking the DB.
- **Test-driven bug fixes.** When you find a bug, first write a unit test that exposes the bug, then fix it, then verify the test passes. This builds a robust regression suite over time.
- **Interface first.** Define the ABC before writing the implementation. Implementations should be swappable without changing callers.
- **Type hints everywhere.** All function signatures must have type annotations.
- **No concrete dependencies in core.** Core code depends on interfaces, never on specific implementations. Use dependency injection. See "Layer Dependency Rules" above for the full import policy.
- **Use capability protocols, not concrete classes.** When accessing another service's methods, use the `@runtime_checkable Protocol` from `interfaces/` (e.g., `ConfigurationReader`, `SchedulerProvider`). Never `isinstance`-check against a concrete service class from `core/services/`.
- **Use the backend registry, not direct imports.** Discover backends via `Backend.registered_backends()` after a side-effect import. Never directly import and instantiate a concrete backend class from `integrations/`.
- **Keep business logic out of web routes.** Routes parse requests, call services, and format responses. Authorization checks, AI prompt construction, backend resolution, and third-party API URL building belong in services or backends.
- **Shared data lives in `interfaces/`.** If two integrations or two layers need the same constant/mapping/policy data, put it in the appropriate `interfaces/` module. Never import across integration modules or from `web/` into `core/`.

## Architecture Violation Checklist

When asked to "check the rules" or "check for violations," audit the entire codebase (including `plugins/`) against every item below. Fix violations immediately.

### Layer Import Violations

These are the most critical. Scan imports in each layer:

- **`interfaces/`** imports from `core/`, `integrations/`, `storage/`, or `web/` — must import nothing outside `interfaces/`, stdlib, and third-party type packages.
- **`core/services/`** imports from `integrations/`** (except side-effect `import gilbert.integrations.foo  # noqa: F401` for backend registration) or from `web/`.
- **`integrations/`** imports from `core/services/`, `web/`, or another integration module.
- **`storage/`** imports from `core/`, `integrations/`, or `web/`.
- **`web/`** imports from `integrations/` or `storage/` directly.
- **Plugins** import from `core/services/`, `integrations/`, `web/`, or `storage/`. Plugins must only import from `gilbert.interfaces.*` and their own internal modules.

### Concrete Class Violations

- **`isinstance` checks against concrete service classes** (e.g., `isinstance(svc, ConfigurationService)`). Must use capability protocols from `interfaces/` instead (e.g., `ConfigurationReader`, `EventBusProvider`, `SchedulerProvider`).
- **Direct instantiation of backend classes** (e.g., `ElevenLabsTTS()`). Must use the backend registry: `Backend.registered_backends().get("name")`.
- **Direct import of concrete backends** from `integrations/` outside of `app.py` or side-effect registration imports.

### Duck-Typing and Private Access Violations

- **`getattr(obj, "method", ...)`** to access service capabilities — must use `isinstance` check against the appropriate protocol instead.
- **Private attribute access** (`obj._field`) on objects from other modules.
- **`# type: ignore`** comments — each one should be reviewed. Most indicate a missing type narrowing that can be resolved with `isinstance` guards, `str()` wrapping for numeric conversions, or filtering with `if isinstance(item, dict)` in comprehensions.

### Business Logic in Wrong Layer

- **Web routes** implementing authorization logic, AI prompt construction, backend resolution, or third-party API URL building. Routes should only parse requests, call services, and format responses.
- **Shared constants/mappings** defined in `core/`, `integrations/`, or `web/` that are used by multiple layers — these belong in `interfaces/`.

### Plugin-Specific Checks

- Plugin resolves dependencies via **concrete imports** instead of `resolver.require_capability()` / `resolver.get_capability()`.
- Plugin reads config via `context.config` for runtime settings instead of implementing `Configurable` and using the `ConfigurationReader` protocol.
- Plugin accesses `_private` attributes on resolved services.

### How to Run

The user can ask to check these rules at any time by saying "check the rules," "check for violations," "audit the architecture," or similar. Run the full checklist across `src/` and `plugins/`, report all findings, and fix them.

## Commands

```bash
# Run tests
uv run pytest

# Run tests with coverage
uv run pytest --cov=gilbert

# Type checking
uv run mypy src/

# Linting
uv run ruff check src/ tests/

# Formatting
uv run ruff format src/ tests/

# Install/sync dependencies
uv sync
```
