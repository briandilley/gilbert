# Gilbert

AI assistant for home and business automation. Extensible, plugin-driven architecture with discoverable services, integrations, and AI capabilities.

## Tech Stack

- **Language:** Python (3.12+), managed via uv (always use `uv run` to execute commands, `uv add` for dependencies — never use pip directly)
- **Database:** SQLite (local store), interface-abstracted for swappable backends
- **Storage API:** Generic entity store with query interface (not SQL-shaped). New entity types require no migrations.
- **Plugins:** `std-plugins/` is a **git submodule** of [`briandilley/gilbert-plugins`](https://github.com/briandilley/gilbert-plugins). Every plugin is a uv workspace member with its own `pyproject.toml` declaring third-party deps, resolved by the root `uv sync`.
- **Testing:** pytest with mocks; database tests use a real test SQLite database. Plugin tests are discovered automatically via `testpaths = ["tests", "std-plugins", "local-plugins", "installed-plugins"]`.
- **Logging:** Python logging framework throughout. Colored console output (stderr), file logging, and separate AI API call log.

## Architecture

### Interface-First Design

Everything is designed as an abstract interface (Python ABCs) with concrete implementations. This applies to:

- **Data layer** — e.g., `StorageBackend` ABC with `SQLiteStorage` implementation (swappable to PostgreSQL, etc.)
- **Backend abstractions** — e.g., `TTSBackend` ABC (`ElevenLabsTTS` implementation ships in the `elevenlabs` plugin), `AIBackend` ABC (`AnthropicAI` in the `anthropic` plugin), `AuthBackend` with `LocalAuth` in core and `GoogleAuth` in the `google` plugin, `VisionBackend` with `AnthropicVision` in the `anthropic` plugin, `TunnelBackend` with `NgrokTunnel` in the `ngrok` plugin, etc. Only vendor-free backends (`LocalAuth`, `LocalDocuments`) live in `src/gilbert/integrations/`; every third-party integration is a std-plugin under `std-plugins/`.
- **Service-level protocols** — e.g., `Configurable` for runtime config, `ToolProvider` for AI tool registration
- **Capability protocols** — e.g., `ConfigurationReader` for config access, `SchedulerProvider` for job scheduling, `EventBusProvider` for event pub/sub, `StorageProvider` for entity storage, `AccessControlProvider` for RBAC queries

New integrations are added by implementing the relevant backend ABC. The `__init_subclass__` auto-registration means just defining the class is enough — no wiring code needed.

### Plugin System

Plugins are loaded from:
- **GitHub URLs** — fetched and installed at runtime via `/plugin install <url>`
- **Local file paths** — for development or private plugins
- **Plugin directories** — `std-plugins/` (git submodule), `local-plugins/`, `installed-plugins/`; every subdirectory containing a `plugin.yaml` is loaded at startup

`std-plugins/` is a git submodule pointing at [`briandilley/gilbert-plugins`](https://github.com/briandilley/gilbert-plugins). `gilbert.sh start` runs `git submodule update --init --recursive` if the directory is empty, then `uv sync`, then boots Gilbert. First-party plugins are developed in that separate repo and updated in Gilbert via the submodule pointer.

Plugins implement published interfaces to extend Gilbert with new integrations or capabilities. Plugins that need configuration implement `Configurable` and read their config via the `ConfigurationReader` protocol (resolved from the `"configuration"` capability), not from `context.config` (which only contains the initial config snapshot at load time).

**Plugin layout.** Every plugin directory contains:

- `plugin.yaml` — manifest (name, version, provides, requires, depends_on)
- `plugin.py` — defines `create_plugin()` returning a `Plugin` instance; its `setup()` method imports backend modules (`from . import sonos_speaker  # noqa: F401`) which triggers `__init_subclass__` registration on the relevant ABC
- `pyproject.toml` — **required** for every std-plugin; declares the plugin's own third-party Python dependencies. See the *Plugin Dependencies* section below.
- `__init__.py` — empty (makes the directory a Python package)
- `tests/conftest.py` — registers the plugin as `gilbert_plugin_<name>` in `sys.modules` for pytest collection. When a plugin has multiple internal modules with relative imports between them (`from .client import Foo` inside `presence.py`), the conftest must **omit** `submodule_search_locations` in its `spec_from_file_location` calls — see `std-plugins/unifi/tests/conftest.py` for the detailed comment on why.
- backend source files — the actual integration code

**Plugin Dependencies (uv workspace).** Each plugin lists its own third-party Python deps in its own `std-plugins/<name>/pyproject.toml`:

```toml
[project]
name = "gilbert-plugin-sonos"
version = "1.0.0"
requires-python = ">=3.12"
dependencies = ["soco>=0.30.15"]

[tool.uv]
package = false
```

The root `pyproject.toml` treats all plugin directories (`std-plugins/*`, `local-plugins/*`, `installed-plugins/*`) as uv workspace members via `[tool.uv.workspace]`, and declares each std-plugin as a root dependency via `[tool.uv.sources] … { workspace = true }`. Result: a plain `uv sync` installs every plugin's declared deps into the shared venv without any per-plugin commands.

Plugins with no third-party deps beyond what's already in core (`httpx`, `aiohttp`, `pillow`, etc.) still need a `pyproject.toml` with `dependencies = []` — uv errors if a workspace member matched by the glob lacks one.

**Runtime plugin install and deps (Option C: restart required).** When `PluginManagerService.install()` fetches a plugin that declares non-empty `[project].dependencies` in its `pyproject.toml`, it **cannot** hot-load it — the new deps aren't in the running venv. The install is persisted with `needs_restart=True` and surfaced in the UI. The user then triggers a restart via `/plugin restart` (or the equivalent `plugins.restart_host` WS handler); Gilbert calls `request_restart()` which flips uvicorn's `should_exit` and sets a flag so `__main__.py` exits the process with `RESTART_EXIT_CODE = 75` once uvicorn's serve loop returns. The `gilbert.sh` supervisor loop catches that exit code, re-runs `uv sync` (installing the new workspace member's deps), and relaunches Gilbert; the boot-time loader imports the plugin normally and `PluginManagerService.reconcile_loaded_plugins()` clears the `needs_restart` flag. Plugins that declare zero third-party deps still hot-load as before without any restart.

**Supervised restart pattern.** `gilbert.sh start` / `gilbert.sh dev` run Gilbert under a supervisor loop that distinguishes "exit and restart" (exit code `75`, `EX_TEMPFAIL`) from "exit and stop" (exit code `0`, `130` from Ctrl+C, `143` from SIGTERM, or anything else from a crash). The loop re-runs `uv sync` on each iteration so any plugin dep changes land in the venv before the next boot. A SIGINT/SIGTERM trap in the supervisor sets a stop flag so Ctrl+C during a `uv sync` or between Gilbert runs still exits cleanly instead of looping back. The restart is triggered from inside Gilbert via `Gilbert.request_restart()` (set a flag + call a shutdown callback wired from `__main__.py` that flips uvicorn's `server.should_exit`). Services should never call `sys.exit()` directly — they should call `request_restart()` and let `__main__.py` return the exit code on the way out.

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

### AI Backend Streaming and Capabilities

`AIBackend` exposes two optional surfaces on top of the base `generate()` contract so core code can branch on backend support without any provider-specific `isinstance` checks:

- **`capabilities() -> AIBackendCapabilities`** — advertises `streaming` and `attachments_user` flags. Default returns both `False`; backends override to opt in.
- **`generate_stream(request) -> AsyncIterator[StreamEvent]`** — yields provider-neutral `StreamEvent`s (`TEXT_DELTA`, `TOOL_CALL_START`, `TOOL_CALL_DELTA`, `TOOL_CALL_END`, `MESSAGE_COMPLETE`) as tokens arrive. The default implementation calls `generate()` and yields a single `MESSAGE_COMPLETE`, so backends that can't stream still compose with the core loop for free.

`AIService.chat()` drives the backend via `generate_stream()` unconditionally and forwards each `TEXT_DELTA` onto the event bus as `chat.stream.text_delta` (with the conversation's audience in `visible_to`) for live frontend typing. `MESSAGE_COMPLETE` gives it the fully assembled `AIResponse` used for tool-call dispatch / stop-reason handling — identical to what the old non-streaming path returned.

**All Anthropic-specific event names and SSE parsing live in `std-plugins/anthropic/anthropic_ai.py`.** Core never imports from the plugin. Adding a new streaming backend (OpenAI, Gemini, local llama.cpp) means implementing `generate_stream` on its own `AIBackend` subclass — nothing outside that file needs to change.

**Max-tokens recovery.** `AIService` handles `StopReason.MAX_TOKENS` with two sub-cases: text-only cutoffs trigger a bounded "please continue" synthetic turn (configurable via `ai.max_continuation_rounds`, default 2) and adjacent assistant rows are merged at persist time so the user sees a single coherent bubble; a `max_tokens` response carrying truncated `tool_calls` is unrecoverable — the broken tool_call is stripped, an error is annotated onto the assistant message, and an entry is added to `tool_usage` so the UI can surface it. Raising the backend's `max_tokens` setting (default `16384` on Anthropic) is the user-facing fix.

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

### Tool-Produced Attachments

Tools can hand files back to the assistant message so the user can download them from the chat bubble. A tool returns a `ToolResult` (or `ToolOutput`) with an `attachments` tuple of `FileAttachment` objects; `AIService` collects every turn's tool attachments, lands them on the final assistant `Message`, and surfaces them on `ChatTurnResult.attachments` + the `chat.message.send.result` WS frame.

`FileAttachment` has two modes:

- **Inline** — `data` (base64) or `text` (UTF-8) carries the full payload. Used for user uploads coming in from the chat input.
- **Workspace reference** — `workspace_skill` + `workspace_path` + `workspace_conv` together name a file on disk. With `workspace_conv` set (the normal case for tool-produced files), the path is `.gilbert/skill-workspaces/users/<user_id>/conversations/<workspace_conv>/<workspace_skill>/<workspace_path>`. With `workspace_conv` empty (legacy attachments persisted before per-conversation workspaces existed), it resolves to the legacy `.gilbert/skill-workspaces/<user_id>/<workspace_skill>/<workspace_path>`. No bytes ride on the message; the frontend fetches them on click via `skills.workspace.download` (which tries the conv-scoped path first, then falls back to legacy). This is the preferred mode for anything tool-generated (PDFs, images, spreadsheets) — keeps the conversation row small.

**`SkillService.attach_workspace_file`** is the tool for this: give it `(skill_name, path, [display_name])` and it returns a reference-style attachment, stamped with the current `_conversation_id`, that the frontend renders as a download chip. The typical flow is "run a script that writes a PDF to the workspace, then call `attach_workspace_file` to hand it back."

### Per-Conversation Skill Workspaces

Every `(user, skill, conversation)` triple gets its own workspace directory:

```
.gilbert/skill-workspaces/
    users/
        <user_id>/
            conversations/
                <conversation_id>/
                    <skill_name>/         ← default for in-chat tool runs
                        generate_po.py
                        po.pdf
                        ...
    <user_id>/                            ← legacy single-workspace shape
        <skill_name>/                     (read-only fallback for old chats)
            ...
```

This isolation is enforced by `SkillService._get_workspace(user_id, skill_name, conversation_id)`. Tools read the conversation id from the injected `_conversation_id` argument (set by both `AIService._execute_tool_calls` and `_execute_slash_command`) and pass it through. Read-side tools (`read_skill_workspace_file`, `browse_skill_workspace`, the `skills.workspace.download` WS handler) try the conv-scoped path first and fall back to the legacy per-(user, skill) path so attachments persisted before the refactor still resolve. Write-side tools (`write_skill_workspace_file`, `run_workspace_script`, `attach_workspace_file`) only touch the conv-scoped path — new files never land in legacy.

When a personal conversation is deleted via `chat.conversation.delete`, `AIService` publishes `chat.conversation.destroyed` with `{conversation_id, owner_id}`. `SkillService` subscribes at start time and runs `shutil.rmtree` on the matching `users/<owner>/conversations/<conv>/` subtree. Defense-in-depth refuses to rm anything outside `_workspace_root().resolve()`.

### Skill Activation Gate

Skill activation in this codebase has dual semantics:

1. **Soft signal (always was):** activated skills get their `SKILL.md` instructions injected into the system prompt via `SkillService.build_skills_context()`. Activation is per-conversation, stored on the conversation row's `state.active_skills`.
2. **Hard gate (new):** AI-driven calls to skill tools — `read_skill_file`, `run_skill_script`, `browse_skill_workspace`, `read_skill_workspace_file`, `write_skill_workspace_file`, `run_workspace_script`, `attach_workspace_file` — refuse when the skill isn't on the conversation's active list. The refusal is a JSON error string telling the AI to ask the user to enable the skill instead of retrying.

Slash invocations bypass the gate. The user typing `/skill wsread pdf foo.txt` is an explicit "use this skill in this chat" signal, so it's allowed even when pdf isn't activated. System callers (no `_conversation_id`) also bypass — they don't have an active-skills list to consult.

The mechanism: both `_execute_tool_calls` and `_execute_slash_command` inject two new underscore-prefixed args alongside `_user_id` etc.: `_conversation_id` and `_invocation_source` (`"ai"` or `"slash"`). `_sanitize_tool_args` strips underscore-prefixed keys before they're shown in the UI. `SkillService._assert_skill_accessible(skill_name, arguments)` is the gate helper called at the top of every gated tool.

`manage_skills(action=list)` is also gated for the AI: when invoked via the AI path on a conversation context, it returns only skills active for that conversation. Slash invocations and system callers see the full catalog.

### Chat Turn Grouping

Conversation history is delivered to the frontend as a list of **turns**, not individual messages. A turn is one user→assistant exchange:

```
ChatTurn = {
    user_message: { content, attachments, [author_id, author_name] },
    rounds: [
        { reasoning: str, tools: [{tool_call_id, tool_name, arguments,
                                   result, is_error}, ...] },
        ...
    ],
    final_content: str,
    final_attachments: [...],
    incomplete: bool,         # true if turn never reached a final answer
                              # (max_tool_rounds, error, ...)
}
```

The `chat.history.load.result` frame returns `turns: list[ChatTurn]` instead of the old flat `messages` list. `chat.message.send.result` returns the same `rounds` field on the just-finished turn so the frontend can commit the authoritative shape after streaming. `AIService.chat()` builds `turn_rounds` alongside `tool_usage` during the agentic loop; the slash-command path emits a zero-rounds turn with the tool result as `final_content`. History replay walks the persisted message rows in `_group_persisted_messages_into_turns` and produces the same shape.

Frontend renders one `TurnBubble` per turn: user message at top, a collapsible "thinking card" showing per-round reasoning + tool calls, and the final answer below. The thinking card shows a live preview of the most recent reasoning + tool name in its collapsed header, updates incrementally from `chat.stream.text_delta` / `chat.tool.started` / `chat.tool.completed` events, and uses `chat.stream.round_complete` as the explicit round-boundary signal so text from the next round goes into a fresh round entry instead of getting concatenated to the previous one.

### Slash Commands — Direct Tool Invocation

### Slash Commands — Direct Tool Invocation

Every tool is also a candidate for direct user invocation from the chat input as a slash command (e.g. `/announce "hello" speakers`). Slash commands bypass the AI entirely: the chat handler parses the input, enforces RBAC, and calls `ToolProvider.execute_tool()` directly. The result is recorded in the conversation with the same `tool_calls`/`tool_results` shape as an AI-driven tool use.

**Opting a tool in:**

```python
# Top-level command: /mycmd <args>
ToolDefinition(
    name="my_tool",
    slash_command="mycmd",
    slash_help="Short one-line hint shown in autocomplete",
    description="Full description (used by the AI)",
    parameters=[...],
    required_role="user",
)
```

**Grouped commands.** Services with several related tools should share a single top-level slash prefix by declaring `slash_group`. The user invokes them as `/<group> <subcommand> <args>`:

```python
# User types /radio start, /radio stop, /radio skip, etc.
ToolDefinition(
    name="radio_start",
    slash_group="radio",
    slash_command="start",
    slash_help="Start the radio DJ: /radio start [genre]",
    ...
)
ToolDefinition(
    name="radio_stop",
    slash_group="radio",
    slash_command="stop",
    ...
)
```

Grouping is the preferred pattern whenever a service exposes more than two related tools — it keeps the global slash namespace uncluttered (`/radio` has 9 subcommands under one prefix instead of 9 top-level commands), and autocomplete naturally narrows as the user types `/radio st` → `start`/`status`/`stop`. The same leaf name can be reused across groups: `/radio stop` and `/speaker stop` don't collide.

Single-tool or otherwise-unique services (e.g. `/announce`, `/greet`, `/rename`, `/memory`) should stay top-level — grouping is only worth it when there's actually something to group.

**The standard is that most tools SHOULD expose a slash command.** The exceptions are tools whose parameter shapes don't translate well to shell syntax:
- Tools that take raw HTML / multi-line structured content as a required field (e.g. `inbox_reply`'s `body_html`).
- Tools whose required arguments are opaque IDs the user can't know by heart (fine if the ID also appears in another slash command's output).
- Tools with complex `object`/`array` inputs that have no natural positional form (e.g. `query_entities`' filter list).
- Tools that only make sense as a mid-AI-turn callback (e.g. `email_attach` inside an active draft).

Everything else — including admin tools — should opt in. RBAC automatically hides commands from users who can't invoke them, so admin-only commands pollute nothing for non-admins.

**Parser rules (see `core/slash_commands.py`):**

- Tokenization uses `shlex` — standard shell quoting for values with spaces.
- Positional arguments are assigned to parameters in declaration order (skipping injected `_*` params and any keyword-supplied ones).
- Keyword args: `key=value`, `--key=value`, or `--key value`.
- Positional slots can't be skipped — to set a later parameter without setting an earlier one, use a keyword arg. **Order your parameters so the most-commonly-supplied ones come first** if you want a natural positional form; complex or less-common parameters should live near the end so keyword access is practical.
- Type coercion per `ToolParameterType`: strings pass through, numbers parse, booleans accept `true/yes/1/on`/`false/no/0/off`, arrays accept JSON (`["a","b"]`) or comma-split (`a,b,c`), objects must be JSON.
- Enums are validated post-coercion.

**Plugin namespacing:**

Tools provided by plugins are automatically exposed under a dotted namespace (e.g. `/currev.time_logs`) to prevent collisions with core tools or other plugins. The namespace is resolved by `AIService._resolve_slash_namespace()`:

1. If the service class declares `slash_namespace: str` as a class attribute, that wins — plugins use this to pick a short, user-friendly prefix.
2. Otherwise, if the service class's `__module__` starts with `gilbert_plugin_`, the sanitized plugin name (the part after the prefix, up to the first `.`) is used.
3. Core services (no plugin module, no class attribute) get no prefix — their slash commands are bare.

Plugins should set `slash_namespace` on their `Service` subclass rather than relying on the auto-detected form, both for brevity and because the auto-detected name mirrors the plugin directory name (which can be long).

**Uniqueness:**

Within a namespace, slash commands must be unique. A static test (`tests/unit/test_slash_command_uniqueness.py`) walks every `ToolDefinition` under `src/gilbert/core/services/` and `src/gilbert/integrations/` and fails the build if two core tools claim the same `slash_command` — so collisions are caught before they reach production. Plugin tools are checked at discovery time with a runtime warning.

**Autocomplete:**

`slash.commands.list` (everyone role) is an RPC that returns the RBAC-filtered command list for the caller. The chat input (`ChatInput.tsx`) fetches it on connect and drives a popover with prefix-filtered suggestions plus a parameter help strip that highlights the current argument as the user types.

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
- Plugin `Service` class that provides tools does **not** declare `slash_namespace` — plugins should set a short, user-friendly namespace rather than relying on the auto-detected directory-name fallback. Verify by grepping for `class ... (Service)` in `plugins/` and checking each tool-providing class has a `slash_namespace = "..."` class attribute.

### Slash Command Violations

- **Tools without a `slash_command`** — audit every `ToolDefinition(...)` in `src/gilbert/core/services/`, `src/gilbert/integrations/`, and `plugins/`. Tools should set `slash_command="..."` unless they fit one of the documented exceptions (raw HTML/multi-line required inputs, opaque-ID-only inputs, complex structured arrays/objects, mid-AI-turn callbacks). Missing `slash_command` on an eligible tool is a violation.
- **Missing `slash_help`** on tools that declare `slash_command` — every exposed command should also have a one-line `slash_help` string for the autocomplete popover. It's fine for `slash_help` to be shorter than `description`; the goal is a terse hint, not a duplicate of the AI-facing docs.
- **Multi-tool services that don't use `slash_group`** — any service exposing three or more slash-enabled tools that are conceptually related (e.g. radio, speaker, knowledge, users) should collapse them under a `slash_group` so the top-level namespace stays tidy. Services with one or two tools, or with tools that aren't really a cohesive set, can stay top-level.
- **Parameter order hostile to shell use** — if a tool's first positional is a rarely-supplied parameter (e.g. `limit` before `query`), slash users will always have to use keyword args. Fix by reordering parameters so the most-commonly-set ones come first. This doesn't affect the AI since it always sends structured JSON.
- **Non-identifier `slash_command` or `slash_group` values** — both must match `[a-zA-Z][a-zA-Z0-9_\-]*`. Dots are reserved for plugin namespacing and spaces are reserved for group/subcommand composition; both are applied automatically at discovery time. `tests/unit/test_slash_command_uniqueness.py` enforces this.
- **Duplicate `(slash_group, slash_command)` pairs across core tools** — two tools claiming the same slash in the same group is a collision. The uniqueness test catches this. The same leaf name CAN appear under different groups (`/radio stop` vs `/speaker stop`) — that's by design.

### Documentation Freshness

The following README files are considered part of the product and must stay in sync with reality. Drift is a regression to be fixed in the same change that caused it, not deferred.

- **`README.md` (Gilbert root)** — overview, integration table, plugin system summary, getting started, configuration instructions, development commands. When you change how the system is configured, how it starts up, what integrations it bundles, or the plugin directory structure, update this file.
- **`std-plugins/README.md`** — canonical inventory of every plugin (table + per-plugin detail section with config keys, deps, slash commands). When you add/remove/rename a plugin or change its `config_params()`, update this file in the same commit.
- **`std-plugins/CLAUDE.md`** — plugin development conventions. Update when plugin layout rules, test conventions, or workspace wiring changes.
- **`CLAUDE.md` (this file)** — the architecture reference Claude reads on every session. Update when layer rules, capability protocols, or the plugin contract change.

The `README.md` freshness pass runs as part of the verification sweep: after making changes, grep the README(s) for any strings that the change might have invalidated (plugin names, config key names, commands, paths) and update them. "Refactor touched X, README still talks about X the old way" is a regression.

### How to Run

The user can ask to check these rules at any time by saying "check the rules," "check for violations," "audit the architecture," or similar. Run the full checklist across `src/` and `std-plugins/`, report all findings, and fix them. The checklist now includes README.md freshness for both the Gilbert root and `std-plugins/` — don't just flag stale docs, fix them.

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
