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

### Hardcoded AI Prompts

Every AI prompt MUST be exposed as `ConfigParam(multiline=True, ai_prompt=True)` on the owning service — see [AI Prompts Are Always Configurable](memory-ai-prompts-configurable.md). Audit procedure:

- **Grep for AI call sites:** `system_prompt=` and `Message(role=MessageRole.SYSTEM` across `src/gilbert/core/services/`, `src/gilbert/integrations/`, `std-plugins/`, and `local-plugins/`. Any literal string (not a `self._foo_prompt` attribute reference) on the right-hand side is a violation, except for short connection-test probes.
- **Grep for `_DEFAULT_*PROMPT` constants and similar.** Each one must be the `default=` of a `ConfigParam(ai_prompt=True)` declared on the same service, and must NOT be referenced at the call site directly — the call site reads `self._foo_prompt`, set in `on_config_changed`.
- **Backend wrappers must forward `ai_prompt=bp.ai_prompt`** when wrapping `backend_config_params()` into the parent service's params (`ai.py`, `tts.py`, `ocr.py`, `vision.py`, `lights.py`, `shades.py`, `speaker.py`, `users.py`, `knowledge.py`, `thermostat.py`, `auth.py`, `doorbell.py`, `websearch.py`, `presence.py`, `music.py`, `tunnel.py`).
- **Dead config fields** — a `ConfigParam(ai_prompt=True)` whose configured value is never actually consumed in any AI call. Either wire it or remove it.

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

### UI Extension Violations (frontend)

Plugin UI lives inside the plugin's own directory; core SPA never imports plugin-specific code. Audit procedure:

- **Plugin imports in `frontend/src/`** — grep `frontend/src/` for plugin-specific identifiers and any `import .* from "@/(types|api|components)/<plugin-specific-name>"`. Anything that names a specific plugin (a service the plugin provides, a component the plugin owns) is a violation. Example violations to look for: imports from `@/components/<plugin-name>/...`, `@/types/<plugin-name>`, `@/hooks/use<PluginName>Api`. The browser plugin is the canonical example of "done right" — every TS file lives under `std-plugins/browser/frontend/`.
- **Plugin-specific WS RPC bindings on core's `useWsApi`** — every entry in `frontend/src/hooks/useWsApi.ts` must be either generic chat / dashboard / settings infrastructure OR a non-plugin core service (agent, notifications, mcp). Plugin RPCs (`browser.*`, `slack.*`, `sonos.*`, …) belong in a per-plugin `<plugin>/frontend/api.ts` exporting a `useFooApi()` hook that uses the underlying `rpc()` from `useWebSocket`.
- **Plugin-specific React types on core's `frontend/src/types/`** — same rule. `BrowserCredential`-style types live in `<plugin>/frontend/types.ts`.
- **Plugin-specific panels mounted in core via hardcoded conditionals** — anti-pattern: `{current.name === "Browser" ? <BrowserCredentialsPanel /> : null}` in a core page. Replace with `<PluginPanelSlot slot="...">` and have the plugin register the component via `Plugin.ui_panels()` + a side-effect `<plugin>/frontend/panels.ts` calling `registerPanel`. See [Plugin UI Extensions](memory-plugin-ui-extensions.md).
- **Core pages that don't expose extension slots in the obvious places** — header (`header.widgets`, `header.user-menu`), dashboard (`dashboard.top`/`bottom`), per-page sidebars and toolbars. If you add a new core page, drop a `<PluginPanelSlot slot="<page-name>.toolbar">` (or similar) where a future plugin would naturally want to inject something — even if no plugin uses it yet. The slot itself costs nothing when empty.
- **`Plugin.ui_panels()` / `ui_routes()` declarations whose `panel_id` isn't registered** in any `<plugin>/frontend/panels.ts` — silent dead code. Backend declares the panel, the SPA queries `ui.panels.list`, the registry returns no component, slot renders nothing. Either remove the backend declaration or add the registration.

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
- [Plugin UI Extensions](memory-plugin-ui-extensions.md) — UIPanel / UIRoute / NavContribution / DashboardCard, the registry, slot semantics
- [Plugin runtime_dependencies](memory-runtime-dependencies.md) — non-pip OS deps + `gilbert doctor`
- `tests/unit/test_slash_command_uniqueness.py` — static enforcement for slash collisions
