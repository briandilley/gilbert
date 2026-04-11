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

New integrations are added by implementing the relevant backend ABC. The `__init_subclass__` auto-registration means just defining the class is enough — no wiring code needed.

### Plugin System

Plugins are loaded from:
- **GitHub URLs** — fetched and installed at runtime
- **Local file paths** — for development or private plugins
- **Plugin directories** — scanned for subdirectories containing `plugin.yaml`

Plugins implement published interfaces to extend Gilbert with new integrations or capabilities. Plugins that need configuration implement `Configurable` and read their config from `ConfigurationService`, not from `context.config` (which only contains the initial config snapshot at load time).

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

Backend ABCs following this pattern: `AIBackend`, `TTSBackend`, `AuthBackend`, `UserProviderBackend`, `TunnelBackend`, `VisionBackend`, `DocumentBackend`, `EmailBackend`, `MusicBackend`, `SpeakerBackend`, `DoorbellBackend`, `WebSearchBackend`.

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

- `src/gilbert/interfaces/` — ABCs and protocol definitions (AI, tools, storage, events, TTS, auth, users, vision, tunnel, knowledge, configuration, plugins, WS)
- `src/gilbert/core/` — Application bootstrap, service manager, event bus, logging, config loading
- `src/gilbert/core/services/` — Service wrappers that expose components as discoverable services (including WS RPC handlers via `WsHandlerProvider`)
- `src/gilbert/integrations/` — Concrete backend implementations (e.g., ElevenLabs TTS, Anthropic AI, ngrok tunnel, Google auth/directory, Gmail, GDrive)
- `src/gilbert/storage/` — Storage backend implementations (SQLite)
- `src/gilbert/plugins/` — Plugin loader
- `src/gilbert/web/` — Web server, SPA assets, API routes
- `tests/unit/` — Unit tests with mocks
- `tests/integration/` — Tests against real backends (e.g., SQLite)
- `.gilbert/` — Per-installation data directory (gitignored): bootstrap config, database, logs

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
- **No concrete dependencies in core.** Core code depends on interfaces, never on specific implementations. Use dependency injection.

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
