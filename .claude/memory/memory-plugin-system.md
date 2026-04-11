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

### Plugin Data Directory
Plugins store persistent data in `.gilbert/plugin-data/<plugin-name>/`. Plugins never write to their own source directory. The data dir is created automatically during plugin setup.

### Credential Handling
There is no CredentialService. Plugins store credentials inline in their configuration (via `ConfigurationService` and entity storage). Sensitive config params are marked with `sensitive=True` in `ConfigParam` declarations.

## Related
- `src/gilbert/interfaces/plugin.py` — Plugin, PluginMeta, PluginContext
- `src/gilbert/plugins/loader.py` — PluginLoader, PluginManifest
- `src/gilbert/config.py` — PluginsConfig, load_config()
- `src/gilbert/core/app.py` — Gilbert.create(), _load_plugins()
- [Service System](memory-service-system.md) — how services work
- [Configuration and Data Directory](memory-config-and-data-dir.md) — config layering
