# Gilbert

AI assistant for home and business automation. Extensible, plugin-driven architecture with discoverable services, integrations, and AI capabilities.

## Tech Stack

- **Language:** Python 3.12+, managed via uv (always use `uv run` / `uv add` — never use pip directly)
- **Database:** SQLite (local store), interface-abstracted for swappable backends
- **Storage API:** Generic entity store with query interface (not SQL-shaped). New entity types require no migrations.
- **Plugins:** `std-plugins/` is a **git submodule** of [`briandilley/gilbert-plugins`](https://github.com/briandilley/gilbert-plugins). Every plugin is a uv workspace member with its own `pyproject.toml`, resolved by the root `uv sync`.
- **Testing:** pytest with mocks; database tests use a real test SQLite database. Plugin tests are discovered via `testpaths = ["tests", "std-plugins", "local-plugins", "installed-plugins"]`.
- **Logging:** Python logging framework throughout. Colored console output (stderr), file logging, and separate AI API call log.

## Architecture Overview

Everything is designed as an abstract interface (Python ABC) with concrete implementations. This applies at three levels:

- **Data / backend abstractions** — `StorageBackend`, `AIBackend`, `TTSBackend`, `AuthBackend`, `VisionBackend`, `TunnelBackend`, etc. All follow the universal backend pattern (ABC + `__init_subclass__` registry + `backend_config_params()`). Only vendor-free backends live in `src/gilbert/integrations/`; every third-party integration is a std-plugin under `std-plugins/`. See [Backend Pattern](.claude/memory/memory-backend-pattern.md).
- **Service-level protocols** — `Configurable` for runtime config, `ToolProvider` for AI tool registration, `WsHandlerProvider` for WebSocket RPCs.
- **Capability protocols** — `@runtime_checkable` protocols in `interfaces/` (`ConfigurationReader`, `SchedulerProvider`, `EventBusProvider`, etc.) that consumers `isinstance`-check against to avoid coupling to concrete service classes. See [Capability Protocols](.claude/memory/memory-capability-protocols.md).

Plugins are loaded from GitHub URLs, local paths, or plugin directories (`std-plugins/`, `local-plugins/`, `installed-plugins/`). See [Plugin System](.claude/memory/memory-plugin-system.md) for the manifest, uv-workspace layout, runtime install flow, and supervised-restart pattern.

Configuration is two-tier: `gilbert.yaml` for bootstrap (`storage`, `logging`, `web`) and the `gilbert.config` entity collection for everything else, managed at `/settings`. See [Configuration Service](.claude/memory/memory-configuration-service.md) and [Configuration and Data Directory](.claude/memory/memory-config-and-data-dir.md).

## Key Directories

- `src/gilbert/interfaces/` — ABCs, protocol definitions, shared data types (`acl.py`, `knowledge.py` ext mappings, AI profile dataclass), WS connection protocol.
- `src/gilbert/core/` — Application bootstrap, service manager, event bus, logging, config loading, shared business logic (`core/chat.py`).
- `src/gilbert/core/services/` — Service wrappers that expose components as discoverable services (WS RPC handlers via `WsHandlerProvider`).
- `src/gilbert/integrations/` — Concrete vendor-free backend implementations (`LocalAuth`, `LocalDocuments`).
- `src/gilbert/storage/` — Storage backend implementations (SQLite).
- `src/gilbert/plugins/` — Plugin loader.
- `src/gilbert/web/` — Web server, SPA assets, API routes (thin layer — no business logic).
- `std-plugins/` — First-party plugins (submodule), one directory per integration.
- `tests/unit/` — Unit tests with mocks.
- `tests/integration/` — Tests against real backends (e.g., SQLite).
- `.gilbert/` — Per-installation data directory (gitignored): bootstrap config, database, logs, skill workspaces.

## Layer Dependency Rules

The codebase is organized into layers with strict import rules. Violations create coupling that defeats the plugin/backend architecture.

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
2. **`core/services/`** — Import from `interfaces/`. Never import from `integrations/` except as side-effect imports (`import gilbert.integrations.foo  # noqa: F401`) to trigger backend registration. Never import from `web/`.
3. **`integrations/`** — Import from `interfaces/` only. Never import from `core/services/`, `web/`, or other integrations. Shared data (e.g., file extension mappings) belongs in `interfaces/`.
4. **`web/`** — Thin routing/presentation layer. Import from `interfaces/` and `core/`. Routes parse requests, call services, and format responses — authorization, AI prompt construction, backend resolution, and third-party API URL building belong in services or backends.
5. **`app.py`** (composition root) — The only place that legitimately imports concrete service and integration classes to wire them together.
6. **Shared data** — Constants, mappings, and policy data used by multiple layers belong in `interfaces/` (e.g., `EXT_TO_DOCUMENT_TYPE` in `interfaces/knowledge.py`, ACL defaults in `interfaces/acl.py`).
7. **Plugins** — Plugins must only import from `gilbert.interfaces.*` and their own internal modules. No imports from `core/services/`, `integrations/`, `web/`, or `storage/`.
8. **Tests** — Tests are composition roots for test scenarios and may import concrete classes directly. Test fakes for services should satisfy the relevant `@runtime_checkable Protocol`.

## Agent Memory System

Claude AI agents use a file-based memory system at `.claude/memory/` to retain knowledge about Gilbert's services, integrations, and architectural decisions across conversations.

### How It Works

1. **Index file:** `.claude/memory/MEMORIES.md` contains a flat list of all memories. Each entry is a one-line description with a markdown link. Only the index is loaded into context by default.
2. **Memory files:** Individual files named `memory-<slug>.md` containing detailed information about a specific topic.
3. **Loading on demand:** Check the index for a relevant memory; load the file when needed. **Always mention in the terminal when loading a memory** (e.g., "Loading memory: facial-recognition-service").

### Keeping Memories Current

**Not optional.** Memories are how future Claude sessions understand the system.

- **Create** a memory after designing/implementing a new service, integration, or significant component, or after a significant architectural decision (record the decision and rationale).
- **Update** a memory when its system changes — new fields, renamed classes, changed behavior, new dependencies.
- **Remove** a memory when its system is deleted. Delete the file and remove it from the index. Stale memories are worse than no memories.
- **Before every commit**, review memories touched by the changes. Update stale memories, delete obsolete ones, and create new ones for anything significant added. Do not commit code that makes existing memories inaccurate.

### Memory File Format

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

### Rules

- Keep the index concise — one line per memory, under 120 characters.
- Memory file names use `memory-<slug>.md` with kebab-case slugs.
- Don't dump source files into memories. Capture the *knowledge* — what it is, why it exists, how it fits together.
- Always keep the index in sync when creating, renaming, or deleting memory files.

## Privacy

**Never put private or personal information in tracked files.** API keys, credentials, voice IDs, email addresses, and other personal data must only go in gitignored locations (entity storage in `.gilbert/gilbert.db`, `.gilbert/config.yaml`, etc.). This includes `.claude/memory/` files — those are committed. For private data, use the user-scoped memory system instead of the project-scoped one.

## Development Guidelines

- **Always write tests.** Unit tests use mocks for external dependencies. Database tests hit a real test SQLite database — no mocking the DB.
- **Test-driven bug fixes.** When you find a bug, first write a unit test that exposes the bug, then fix it, then verify the test passes.
- **Interface first.** Define the ABC before writing the implementation. Implementations should be swappable without changing callers.
- **Type hints everywhere.** All function signatures must have type annotations.
- **No concrete dependencies in core.** Core code depends on interfaces, never specific implementations. Use dependency injection. See Layer Dependency Rules above.
- **Use capability protocols, not concrete classes.** When accessing another service's methods, use the `@runtime_checkable Protocol` from `interfaces/`. Never `isinstance`-check against a concrete service class from `core/services/`. See [Capability Protocols](.claude/memory/memory-capability-protocols.md).
- **Use the backend registry, not direct imports.** Discover backends via `Backend.registered_backends()` after a side-effect import. Never directly import and instantiate a concrete backend class from `integrations/`. See [Backend Pattern](.claude/memory/memory-backend-pattern.md).
- **Keep business logic out of web routes.** Routes parse requests, call services, and format responses. Authorization, AI prompt construction, backend resolution, and third-party API URL building belong in services or backends.
- **Shared data lives in `interfaces/`.** If two integrations or two layers need the same constant/mapping/policy data, put it in the appropriate `interfaces/` module.

## Architecture Violation Checklist

When the user says "check the rules," "check for violations," "audit the architecture," or similar, run the full checklist in [Architecture Violation Checklist](.claude/memory/memory-architecture-checklist.md) across `src/` and `std-plugins/`, report findings, and fix them — including README freshness. Don't just flag stale docs; fix them.

Top-level categories covered there: layer import violations, concrete-class violations, duck-typing / private access, business logic in wrong layer, plugin-specific checks, slash command violations, and documentation freshness (root `README.md`, `std-plugins/README.md`, `std-plugins/CLAUDE.md`, this file).

## Commands

```bash
# Install Gilbert core + every std-plugin's deps (uv resolves the whole workspace)
uv sync

# Install with dev tooling (ruff, mypy, pytest-cov)
uv sync --extra dev

# Run all tests (includes every std-plugin's tests via pyproject.toml testpaths)
uv run pytest

# Run tests with coverage
uv run pytest --cov=gilbert

# Type checking
uv run mypy src/

# Linting
uv run ruff check src/ tests/

# Formatting
uv run ruff format src/ tests/

# Initialize/update the std-plugins submodule (normally ./gilbert.sh start handles this)
git submodule update --init --recursive
```
