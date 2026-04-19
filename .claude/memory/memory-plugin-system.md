# Plugin System

## Summary
Plugins extend Gilbert with new services, tools, and capabilities. They can live in external directories, declare dependencies on other plugins, provide default configuration, and store data in isolated directories. Plugins implement the `Configurable` protocol to read settings from the `ConfigurationService`; there is no CredentialService. Config is stored in entity storage.

## Details

### Plugin Interface (`src/gilbert/interfaces/plugin.py`)
- **`PluginMeta`** dataclass — `name`, `version`, `description`, `provides` (capabilities), `requires` (capabilities), `depends_on` (other plugin names)
- **`PluginContext`** dataclass — passed to `setup()`, contains:
  - `services: ServiceManager` — register and discover services
  - `config: dict[str, Any]` — this plugin's resolved config section
  - `data_dir: Path` — `.gilbert/plugin-data/<plugin-name>/` for persistent data
- **`Plugin`** ABC — `metadata()`, `setup(context: PluginContext)`, `teardown()`

### Plugin Manifest (`plugin.yaml`)
Each plugin directory contains a `plugin.yaml` manifest declaring:
- Metadata: `name`, `version`, `description`
- Capability declarations: `provides`, `requires`
- Plugin-level dependencies: `depends_on` (other plugin names)
- Default configuration: `config` section (merged into config chain)

### Plugin Directory Layout
Every plugin directory contains:
- `plugin.yaml` — manifest (see above)
- `plugin.py` — defines `create_plugin()` returning a `Plugin` instance; its `setup()` does the side-effect imports (`from . import sonos_speaker  # noqa: F401`) that trigger `__init_subclass__` registration on the relevant backend ABCs
- `pyproject.toml` — **required** for every std-plugin; declares third-party Python deps. See uv workspace section below.
- `__init__.py` — empty (makes the directory a Python package)
- `tests/conftest.py` — registers the plugin as `gilbert_plugin_<name>` in `sys.modules` for pytest collection. When a plugin has multiple internal modules with relative imports (`from .client import Foo` inside `presence.py`), the conftest must **omit** `submodule_search_locations` in its `spec_from_file_location` call — see `std-plugins/unifi/tests/conftest.py` for the detailed comment.
- backend source files — the actual integration code

### uv Workspace (Plugin Dependencies)
Each plugin's third-party deps live in its own `std-plugins/<name>/pyproject.toml`:

```toml
[project]
name = "gilbert-plugin-sonos"
version = "1.0.0"
requires-python = ">=3.12"
dependencies = ["soco>=0.30.15"]

[tool.uv]
package = false
```

The root `pyproject.toml` treats all plugin directories (`std-plugins/*`, `local-plugins/*`, `installed-plugins/*`) as uv workspace members via `[tool.uv.workspace]`, and declares each std-plugin as a root dependency via `[tool.uv.sources] … { workspace = true }`. A plain `uv sync` installs every plugin's deps into the shared venv — no per-plugin commands.

Plugins with no third-party deps beyond core (`httpx`, `aiohttp`, `pillow`, etc.) still need a `pyproject.toml` with `dependencies = []` — uv errors if a workspace member matched by the glob lacks one.

### std-plugins submodule
`std-plugins/` is a git submodule pointing at [`briandilley/gilbert-plugins`](https://github.com/briandilley/gilbert-plugins). `gilbert.sh start` runs `git submodule update --init --recursive` if empty, then `uv sync`, then boots Gilbert. First-party plugins are developed in that separate repo and updated here via the submodule pointer.

### Plugin Loader (`src/gilbert/plugins/loader.py`)
- **Directory scanning**: `scan_directories(dirs)` — finds subdirs with `plugin.yaml`
- **Manifest parsing**: `PluginManifest` class wraps parsed `plugin.yaml` data
- **Config collection**: `collect_default_configs(manifests)` — gathers all plugin default configs
- **Dependency resolution**: `topological_sort(manifests)` — orders by `depends_on` (cycle detection)
- **Loading**: `load(source)` for path/URL, `load_from_manifest(manifest)` for scanned plugins
- Entry point contract: `plugin.py` with `create_plugin() -> Plugin` function

### Configuration Layering
Three-layer merge order:
1. `gilbert.yaml` (core defaults)
2. Plugin default configs from `plugin.yaml` files (namespaced under `plugins.config.<name>`)
3. `.gilbert/config.yaml` (user overrides — wins over plugin defaults)

Users override plugin config in `.gilbert/config.yaml`:
```yaml
plugins:
  config:
    my-plugin:
      poll_interval: 60
```

### Config Model (`src/gilbert/config.py`)
- **`PluginsConfig`** — `directories: list[str]`, `sources: list[PluginSource]`, `config: dict[str, dict[str, Any]]`
- `GilbertConfig.plugins` is a `PluginsConfig` (replaces old `list[PluginSource]`)
- Legacy list format is auto-migrated during `load_config()`

### Bootstrap Flow (`src/gilbert/core/app.py`)
- `Gilbert.create()` class method handles the full config layering:
  1. Reads base config to discover plugin directories
  2. Scans directories for manifests
  3. Collects plugin default configs
  4. Calls `load_config(plugin_defaults=...)` for three-layer merge
- `Gilbert.start()` loads plugins after core services are registered:
  1. Topologically sorts discovered manifests by `depends_on`
  2. Loads each plugin, creates its data dir, passes `PluginContext`
  3. Also loads legacy explicit sources (path/URL)
  4. Then `service_manager.start_all()` resolves service dependencies
- Each successfully-loaded plugin is tracked in `Gilbert._plugins` as a `LoadedPlugin` dataclass holding the plugin instance, the install path on disk, and the set of service names registered during `setup()` (snapshotted by diffing `service_manager.list_services()` before/after).
- `Gilbert.make_plugin_context(name)` is the shared context builder used by both the boot-time loader and the runtime `PluginManagerService`.

### Runtime Install / Uninstall (`src/gilbert/core/services/plugin_manager.py`)
The `PluginManagerService` allows admins to install plugins at runtime from the web UI (`/plugins`) or chat (`/plugin install <url>` / `/plugin uninstall <name>` / `/plugin list`). Capabilities: `plugin_manager`, `ai_tools`, `ws_handlers`. WS frames: `plugins.list` / `plugins.install` / `plugins.uninstall` (admin-level via `interfaces/acl.py`).

**Install flow** (`PluginLoader.install_from_url` then service-side):
1. `_fetch_to(url)` routes by URL shape — archive suffix wins (`.zip`, `.tar.gz`, `.tgz`, `.tar.bz2`); GitHub URLs (with optional `/tree/<ref>/<subpath>` or `/blob/...`) are shallow-cloned. Anything else is rejected.
2. Archives are downloaded with `httpx.stream` (in `asyncio.to_thread`), extracted with safe-extract helpers that reject `..` and absolute paths, and unwrapped if there's a single top-level dir (the GitHub source-zip convention).
3. `_validate_plugin_dir` checks for `plugin.yaml` + `plugin.py` at the root, valid name (`[a-zA-Z][a-zA-Z0-9_-]*`), and required version.
4. `_test_load` imports the plugin under a throwaway `gilbert_plugin_test_<uuid>` package name (cleaned up afterward) so `create_plugin()` is verified before we commit anything to disk.
5. The directory is moved into `installed-plugins/<name>/`. Existing installs raise unless `force=True`.
6. `PluginManagerService.install` snapshots the registered-service set, calls `loader.load_from_manifest()` + `plugin.setup(ctx)`, diffs to learn which services the plugin added, then `service_manager.start_service(name)` for each new one.
7. The plugin is appended to `Gilbert._plugins` (so it's torn down on shutdown) and a row is persisted in the `gilbert.plugin_installs` entity collection (`{_id, name, version, source_url, install_path, installed_at, registered_services}`).
8. **Rollback on failure**: any service registered between snapshots is `stop_and_unregister`'d, the install dir is removed, and the registry stays clean.

**Uninstall flow**: `plugin.teardown()`, `service_manager.stop_and_unregister(name)` for each registered service (capabilities are unindexed and the service is dropped from `_registered`/`_started`), `Gilbert.remove_loaded_plugin`, registry row deleted, install dir removed, and any cached `gilbert_plugin_<sanitized>.*` entries are purged from `sys.modules` so a future re-install gets a fresh import.

**ServiceManager helpers** (`src/gilbert/core/service_manager.py`):
- `start_service(name)` — start a service that was registered after `start_all()` (e.g. inside a plugin `setup()` that ran post-boot). No-op if already started.
- `stop_and_unregister(name)` — stop + remove a service entirely, with capability index cleanup. Publishes `service.stopped`. Used by uninstall.

**Source buckets**: `list_installed()` classifies each plugin as `std` / `local` / `installed` / `unknown` by which configured `plugins.directories` entry contains its install path. Only `installed`-bucket plugins are uninstallable through this service — std-plugins and local-plugins are managed outside the runtime.

### Runtime install with restart-required (Option C)
When `PluginManagerService.install()` fetches a plugin that declares non-empty `[project].dependencies` in its `pyproject.toml`, it **cannot** hot-load it — the new deps aren't in the running venv. The install is persisted with `needs_restart=True` and surfaced in the UI. The user then triggers a restart via `/plugin restart` (or the `plugins.restart_host` WS handler); Gilbert calls `request_restart()` which flips uvicorn's `should_exit` and sets a flag so `__main__.py` exits the process with `RESTART_EXIT_CODE = 75` once uvicorn's serve loop returns. The `gilbert.sh` supervisor loop catches that exit code, re-runs `uv sync` (installing the new workspace member's deps), and relaunches Gilbert; the boot-time loader imports the plugin normally and `PluginManagerService.reconcile_loaded_plugins()` clears the `needs_restart` flag. Plugins with zero third-party deps still hot-load without any restart.

### Supervised restart pattern
`gilbert.sh start` / `gilbert.sh dev` run Gilbert under a supervisor loop that distinguishes "exit and restart" (exit code `75`, `EX_TEMPFAIL`) from "exit and stop" (exit code `0`, `130` from Ctrl+C, `143` from SIGTERM, or anything else from a crash). The loop re-runs `uv sync` on each iteration so any plugin dep changes land in the venv before the next boot. A SIGINT/SIGTERM trap in the supervisor sets a stop flag so Ctrl+C during a `uv sync` or between Gilbert runs still exits cleanly instead of looping back. The restart is triggered from inside Gilbert via `Gilbert.request_restart()` (set a flag + call a shutdown callback wired from `__main__.py` that flips uvicorn's `server.should_exit`). Services should never call `sys.exit()` directly — they should call `request_restart()` and let `__main__.py` return the exit code on the way out.

### Plugin Data Directory
Plugins store persistent data in `.gilbert/plugin-data/<plugin-name>/`. Plugins never write to their own source directory. The data dir is created automatically during plugin setup.

### Credential Handling
There is no CredentialService. Plugins store credentials inline in their configuration (via `ConfigurationService` and entity storage). Sensitive config params are marked with `sensitive=True` in `ConfigParam` declarations.

## Related
- `src/gilbert/interfaces/plugin.py` — Plugin, PluginMeta, PluginContext
- `src/gilbert/plugins/loader.py` — PluginLoader, PluginManifest, install_from_url, archive helpers
- `src/gilbert/config.py` — PluginsConfig, load_config()
- `src/gilbert/core/app.py` — Gilbert.create(), _load_plugins(), make_plugin_context(), LoadedPlugin
- `src/gilbert/core/service_manager.py` — start_service(), stop_and_unregister() for hot load/unload
- `src/gilbert/core/services/plugin_manager.py` — PluginManagerService (install/uninstall/list_installed, /plugin tools, plugins.* WS handlers)
- `frontend/src/components/plugins/PluginsPage.tsx` — admin UI for the install registry
- [Service System](memory-service-system.md) — how services work
- [Configuration and Data Directory](memory-config-and-data-dir.md) — config layering
