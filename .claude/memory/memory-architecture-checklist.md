# Architecture Violation Checklist

## Summary
The full checklist to run when the user says "check the rules," "check for violations," "audit the architecture," or similar. Audit `src/` and `std-plugins/`; fix every violation found in the same pass. README freshness is part of the checklist — stale docs are treated as regressions, not deferred cleanup.

## Details

### Layer Import Violations (most critical)

Scan imports in each layer:

- **`interfaces/`** imports from `core/`, `integrations/`, `storage/`, or `web/` — must import nothing outside `interfaces/`, stdlib, and third-party type packages.
- **`core/services/`** imports from `integrations/` (except side-effect `import gilbert.integrations.foo  # noqa: F401` for backend registration) or from `web/`.
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

### Multi-User Isolation

Services are singletons shared across every user and in-flight request. Per-request state stored on `self` will race under concurrent users and leak events/data between conversations. Audit procedure:

- **Flag every instance attribute matching `_current_*`, `_active_*`, `_pending_*`.** For each, decide whether the value is service-lifetime (config, backend handles — fine) or request-scoped (user id, conversation id, active turn — must migrate). Grep: `self\._current_|self\._active_|self\._pending_` in `src/gilbert/core/services/` and `std-plugins/*/`.
- **Any attribute set inside a request handler and read after an `await`** is suspicious. Even attributes that look request-scoped ("I only set this at the start of each request") race when two requests overlap, because the second one's `set` lands during the first one's `await`.
- **Request-scoped identity (user id, conversation id, correlation id, trace context)** must live in a `ContextVar` in `gilbert.core.context`, not on a service instance. Reads go through `get_current_*()`, writes through `set_current_*()` at entry points only.
- **Parallel `asyncio.Task`s must be spawned with an explicit context:** `asyncio.Task(coro, context=contextvars.copy_context())`. Otherwise `ContextVar.set()` inside one task leaks to siblings. Grep for `asyncio.gather` and `asyncio.create_task` / `asyncio.Task` — any instance without `context=` that's invoked inside a request handler needs review.
- **Global locks protecting per-target resources** (e.g., `_announce_lock` covering every speaker) serialize unrelated work. A lock should be global only if the thing it protects is genuinely global. If the operation is parameterized by a target (speaker id, file path, user id, conversation id), gate by a `dict[target_id, asyncio.Lock]` keyed appropriately.
- **Module-level mutable state** (`_current_session: dict = {}` at module scope, populated during one request and read during another) has the same failure mode as instance attributes. Treat it the same way.
- **Tool handlers** should read caller identity from injected `_user_id` / `_conversation_id` arguments, not from `self` or global context — this makes the race surface explicit at the call boundary.

See [Multi-User Isolation](memory-multi-user-isolation.md) for the full pattern catalog, failure modes, and fix recipes.

### Plugin-Specific Checks

- Plugin resolves dependencies via **concrete imports** instead of `resolver.require_capability()` / `resolver.get_capability()`.
- Plugin reads config via `context.config` for runtime settings instead of implementing `Configurable` and using the `ConfigurationReader` protocol.
- Plugin accesses `_private` attributes on resolved services.
- Plugin `Service` class that provides tools does **not** declare `slash_namespace` — plugins should set a short, user-friendly namespace rather than relying on the auto-detected directory-name fallback. Verify by grepping for `class ... (Service)` in `plugins/` and checking each tool-providing class has a `slash_namespace = "..."` class attribute.

### Slash Command Violations

- **Tools without a `slash_command`** — audit every `ToolDefinition(...)` in `src/gilbert/core/services/`, `src/gilbert/integrations/`, and `plugins/`. Tools should set `slash_command="..."` unless they fit one of the documented exceptions (raw HTML/multi-line required inputs, opaque-ID-only inputs, complex structured arrays/objects, mid-AI-turn callbacks). Missing `slash_command` on an eligible tool is a violation.
- **Missing `slash_help`** on tools that declare `slash_command` — every exposed command should also have a one-line `slash_help` string for the autocomplete popover. Shorter than `description` is fine; terse hint, not a duplicate.
- **Multi-tool services that don't use `slash_group`** — any service exposing three or more slash-enabled tools that are conceptually related should collapse them under a `slash_group` so the top-level namespace stays tidy.
- **Parameter order hostile to shell use** — if a tool's first positional is a rarely-supplied parameter (e.g. `limit` before `query`), slash users always need keyword args. Fix by reordering parameters so the most-commonly-set ones come first. Doesn't affect the AI since it sends structured JSON.
- **Non-identifier `slash_command` or `slash_group` values** — both must match `[a-zA-Z][a-zA-Z0-9_\-]*`. Dots are reserved for plugin namespacing; spaces are reserved for group/subcommand composition. Enforced by `tests/unit/test_slash_command_uniqueness.py`.
- **Duplicate `(slash_group, slash_command)` pairs across core tools** — collisions within a group. Uniqueness test catches this. Same leaf name under different groups (`/radio stop` vs `/speaker stop`) is allowed.

### Documentation Freshness

These READMEs are product documentation and must stay in sync with reality. Drift is a regression to fix in the same change that caused it, not defer.

- **`README.md` (Gilbert root)** — overview, integration table, plugin system summary, getting started, configuration instructions, development commands. Update when configuration, startup, bundled integrations, or plugin directory structure change.
- **`std-plugins/README.md`** — canonical inventory of every plugin (table + per-plugin detail section with config keys, deps, slash commands). Update in the same commit when you add/remove/rename a plugin or change its `config_params()`.
- **`std-plugins/CLAUDE.md`** — plugin development conventions. Update when plugin layout rules, test conventions, or workspace wiring change.
- **`CLAUDE.md` (root)** — the architecture reference. Update when layer rules, capability protocols, or the plugin contract change.

Freshness pass: after making changes, grep affected READMEs for strings the change may have invalidated (plugin names, config keys, commands, paths) and update them.

### How to Run

Run the full checklist across `src/` and `std-plugins/`, report findings, and fix them — including README freshness. Don't just flag stale docs; fix them.

## Related
- [Capability Protocols](memory-capability-protocols.md) — the protocol table consumers should be checking against
- [Backend Pattern](memory-backend-pattern.md) — the registry pattern the concrete-class violations target
- [Slash Commands](memory-slash-commands.md) — the semantics the slash-command violations enforce
- [Plugin System](memory-plugin-system.md) — plugin layout rules
- `tests/unit/test_slash_command_uniqueness.py` — static enforcement for slash collisions
