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

- **Data / backend abstractions** — `StorageBackend`, `AIBackend`, `TTSBackend`, `AuthBackend`, `VisionBackend`, `TunnelBackend`, etc. All follow the universal backend pattern (ABC + `__init_subclass__` registry + `backend_config_params()`). Only vendor-free backends live in `src/gilbert/integrations/`; every third-party integration is a std-plugin under `std-plugins/`.
- **Service-level protocols** — `Configurable` for runtime config, `ToolProvider` for AI tool registration, `WsHandlerProvider` for WebSocket RPCs.
- **Capability protocols** — `@runtime_checkable` protocols in `interfaces/` (`ConfigurationReader`, `SchedulerProvider`, `EventBusProvider`, etc.) that consumers `isinstance`-check against to avoid coupling to concrete service classes.

Plugins are loaded from GitHub URLs, local paths, or plugin directories (`std-plugins/`, `local-plugins/`, `installed-plugins/`).

Configuration is two-tier: `gilbert.yaml` for bootstrap (`storage`, `logging`, `web`) and the `gilbert.config` entity collection for everything else, managed at `/settings`.

## Key Directories

- `src/gilbert/interfaces/` — ABCs, protocol definitions, shared data types (`acl.py`, `knowledge.py` ext mappings, AI profile dataclass), WS connection protocol. Key modules include `tools.py` (tool definitions), `transcription.py` (speech-to-text ABCs and protocols), and `tts.py` (text-to-speech ABCs).
- `src/gilbert/core/` — Application bootstrap, service manager, event bus, logging, config loading, shared business logic (`core/chat.py`).
- `src/gilbert/core/services/` — Service wrappers that expose components as discoverable services (WS RPC handlers via `WsHandlerProvider`).
- `src/gilbert/integrations/` — Concrete vendor-free backend implementations (`LocalAuth`, `LocalDocuments`, `LocalSpeakerBackend`, `BrowserSpeakerBackend`, `LocalWhisperBackend` for speech-to-text, and the bundled MCP transports `StdioMCPBackend` / `HttpMCPBackend` / `SseMCPBackend` / `BrowserMCPBackend`).
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

## Deep-Dive Architecture Docs

Non-obvious design rationale, cross-cutting subsystem walkthroughs, and gotchas live in `docs/architecture/`. Read on demand when working in the relevant area (e.g., `speaker-system.md` before touching speaker code, `agent-service.md` before touching the agent loop). Architectural *rules* — enforceable constraints — live in the `validate-architecture` skill, not here.

`ls docs/architecture/` to browse. When a doc drifts from the code, fix it in the same change that caused the drift.

## Privacy

**Never put private or personal information in tracked files.** API keys, credentials, voice IDs, email addresses, and other personal data must only go in gitignored locations (entity storage in `.gilbert/gilbert.db`, `.gilbert/config.yaml`, etc.).

## Development Guidelines

- **Always write tests.** Unit tests use mocks for external dependencies. Database tests hit a real test SQLite database — no mocking the DB.
- **Test-driven bug fixes.** When you find a bug, first write a unit test that exposes the bug, then fix it, then verify the test passes.
- **Interface first.** Define the ABC before writing the implementation. Implementations should be swappable without changing callers.
- **Type hints everywhere.** All function signatures must have type annotations.
- **No concrete dependencies in core.** Core code depends on interfaces, never specific implementations. Use dependency injection. See Layer Dependency Rules above.
- **Use capability protocols, not concrete classes.** When accessing another service's methods, use the `@runtime_checkable Protocol` from `interfaces/`. Never `isinstance`-check against a concrete service class from `core/services/`.
- **Use the backend registry, not direct imports.** Discover backends via `Backend.registered_backends()` after a side-effect import. Never directly import and instantiate a concrete backend class from `integrations/`.
- **Keep business logic out of web routes.** Routes parse requests, call services, and format responses. Authorization, AI prompt construction, backend resolution, and third-party API URL building belong in services or backends.
- **Shared data lives in `interfaces/`.** If two integrations or two layers need the same constant/mapping/policy data, put it in the appropriate `interfaces/` module.
- **AI prompts are always configurable.** Every non-trivial string passed to `complete_one_shot(system_prompt=...)` / `chat(system_prompt=...)` / `Message(role=SYSTEM, content=...)` MUST be exposed as a `ConfigParam(multiline=True, ai_prompt=True)` on the owning service, with the bundled string as `default`. Read the active value from `self._foo_prompt` (cached in `on_config_changed`), never from the `_DEFAULT_*` constant.
- **Plugins ship their own UI inside their plugin directory.** A plugin contributing SPA components keeps every TS/TSX file under `<plugin>/frontend/` (types, API hooks, components, styles, side-effect register). Core SPA pages declare `<PluginPanelSlot slot="…">` extension points and never import from a plugin's `frontend/`. The plugin's Python `Plugin.ui_panels()` declares `UIPanel(panel_id, slot, required_role)` entries; the matching `<plugin>/frontend/panels.ts` calls `registerPanel(panel_id, Component)`.
- **Plugin OS deps go through `runtime_dependencies()`.** A plugin that needs binaries / system libraries beyond what `pyproject.toml` can install (Chromium, tesseract, ffmpeg, …) overrides `Plugin.runtime_dependencies()` with `RuntimeDependency` entries. `./gilbert.sh doctor` runs the checks; `--install` runs each `auto_install_cmd` for plugins that opted in. The check should ideally exercise the dep (e.g. actually launch the browser), not just probe a path.
- **Migrations must be idempotent.** Core migrations live in `src/gilbert/migrations/NNNN_<slug>.py`; plugin migrations live in `<plugin>/migrations/`. The runner records success after `up()` returns, so a crash mid-run re-executes the script — write it to tolerate partial prior application. Tracking is keyed by `<scope>:<basename>`: **don't rename applied migration files**, or they'll re-run under the new name.

## Architecture Rules — `validate-architecture` skill

The `validate-architecture` skill (`.claude/skills/validate-architecture/SKILL.md`) is the canonical architectural rulebook. It covers layer imports, concrete-class violations, duck-typing / private access, business logic placement, hardcoded AI prompts, multi-user isolation, plugin rules, slash-command requirements, frontend extension rules, AI-backend visibility, and documentation freshness (root `README.md`, `std-plugins/README.md`, `std-plugins/CLAUDE.md`, this file).

**Load it before implementing anything new.** Before adding a new service, integration, plugin, web route, AI tool, or any non-trivial feature, invoke the skill so the rules guide the design — not after the fact. Trivial fixes (typo, comment, one-line bug) don't need it.

**Run it as an audit** when the user says "check the rules," "check for violations," "audit the architecture," or similar. Don't just flag stale docs; fix them.

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
