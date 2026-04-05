# Configuration Service

## Summary
Runtime configuration management with read/write, persistence to `.gilbert/config.yaml`, and full hot-swap support. Services implement the `Configurable` protocol to describe their parameters and handle runtime changes.

## Details

### Architecture
- **`interfaces/configuration.py`** — `ConfigParam` (describes a param: key, type, description, default, restart_required) and `Configurable` protocol (config_namespace, config_params(), on_config_changed())
- **`core/services/configuration.py`** — `ConfigurationService` with capabilities `configuration` + `ai_tools`

### ConfigurationService
- **No dependencies** — starts before all other services
- Holds the live `GilbertConfig` and raw dict
- **Read:** `get(path)` for dot-path access, `get_section(namespace)` for a service's full config
- **Write:** `set(path, value)` — validates via Pydantic, persists to override file, notifies or restarts
- **Describe:** `describe_all()` — collects ConfigParams from all Configurable services
- **Factories:** `register_factory(namespace, factory)` — for hot-swap service reconstruction

### Config Change Flow
- **Tunable params:** update config → validate → persist → call `service.on_config_changed(section)`
- **Structural params (restart_required):** update config → validate → persist → use factory to create new service → `service_manager.restart_service(name, new_instance)`
- **Enabling/disabling:** uses factory + `register_and_start()` or stops the service

### ServiceManager Hot-Swap Methods
- `restart_service(name, new_instance=None)` — stop, optionally swap, start
- `register_and_start(service)` — for late registration

### Service Refactoring Pattern
Services take only structural deps in constructor (backend, credential_name). Tunable config loaded from ConfigurationService during `start()`. All services implement Configurable.

### AI Tools
- `get_configuration` — read config by path or full dump (excludes credentials)
- `set_configuration` — write a config value (validates, persists, triggers reload)
- `describe_configuration` — list all configurable params with types and descriptions

## Related
- [Service System](memory-service-system.md)
- [AI Service](memory-ai-service.md)
- `src/gilbert/interfaces/configuration.py`
- `src/gilbert/core/services/configuration.py`
