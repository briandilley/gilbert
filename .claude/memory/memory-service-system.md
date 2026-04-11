# Service System

## Summary
Discoverable service layer where services declare capabilities and dependencies. The ServiceManager handles registration, topological dependency resolution, ordered startup, and runtime discovery. Services implement the `Configurable` protocol for runtime configuration. Backend ABCs use a registry pattern for auto-discovery.

## Details

### Core Concepts
- **Service** (`src/gilbert/interfaces/service.py`) — ABC with `service_info()`, `start(resolver)`, `stop()`
- **ServiceInfo** — declares `name`, `capabilities` (what it provides), `requires` (must exist), `optional` (nice to have)
- **ServiceResolver** — read-only view passed to `start()` for pulling dependencies: `get_capability()`, `require_capability()`, `get_all()`
- **ServiceManager** (`src/gilbert/core/service_manager.py`) — implements ServiceResolver, handles lifecycle

### Capabilities
Capabilities are **strings**, not types. Examples: `"entity_storage"`, `"event_bus"`, `"ai_tools"`. A service can provide multiple capabilities. Multiple services can provide the same capability. This enables flexible discovery ("find anything that provides weather").

### Configurable Protocol
Services implement `Configurable` (from `interfaces/configuration.py`) to participate in runtime configuration:
- `config_namespace` — config section name (e.g., `"ai"`, `"tts"`)
- `config_category` — UI grouping category (e.g., `"Intelligence"`, `"Media"`)
- `config_params()` — returns list of `ConfigParam` describing all tunable parameters
- `on_config_changed(config)` — called when config values change at runtime

The ConfigurationService auto-discovers all services implementing `Configurable`.

### Backend Registry Pattern
Backend ABCs (AIBackend, TTSBackend, AuthBackend, etc.) use a common registry pattern:
- `_registry: dict[str, type[Backend]]` class variable
- `backend_name: str = ""` — short identifier used in config (e.g., `"anthropic"`, `"elevenlabs"`)
- `__init_subclass__` — auto-registers concrete classes by `backend_name`
- `registered_backends()` — returns all registered backend classes
- `backend_config_params()` — classmethod for backends to declare their own ConfigParams

Services that own backends merge backend params into their own `config_params()` under the `settings.*` prefix with `backend_param=True`.

### Tool Exposure Rule
**Services should always expose AI tools for their capabilities.** Any service with meaningful operations should implement the `ToolProvider` protocol and declare `ai_tools` as a capability. The AIService auto-discovers all `ai_tools` providers.

### Lifecycle
1. Services are **registered** (constructed but not started)
2. Plugins load and register their own services
3. `start_all()` runs topological sort (Kahn's algorithm) on required capabilities
4. Each service starts in dependency order, receiving a `ServiceResolver`
5. Failed services are logged, added to `_failed`, dependents cascade-fail
6. Shutdown: `stop_all()` in reverse start order

### Core Service Wrappers (`src/gilbert/core/services/`)
Existing components are wrapped as services without modifying their ABCs:
- **StorageService** — wraps `StorageBackend`, provides `{"entity_storage", "query_storage", "ai_tools"}`
- **EventBusService** — wraps `EventBus`, provides `{"event_bus", "pub_sub", "ai_tools"}`
- **TTSService** — wraps `TTSBackend`, provides `{"text_to_speech", "ai_tools"}`
- **AIService** — wraps `AIBackend`, provides `{"ai_chat"}`

Each wrapper exposes the underlying component via a property (e.g., `storage_svc.backend`, `bus_svc.bus`).

### Output File Management
Services that produce files (e.g., TTS audio) write to `.gilbert/output/{service_name}/`. A shared utility (`core/output.py`) provides `get_output_dir()` and `cleanup_old_files()` with TTL-based expiry. Global `output_ttl_seconds` config (default 3600).

### Boot Sequence (app.py)
1. Logging
2. Create ServiceManager
3. Register core services (StorageService, EventBusService)
4. Register optional services (TTSService, AIService) if enabled
5. Register in old ServiceRegistry for backward compat
6. Load plugins via `plugin.setup(service_manager)`
7. `service_manager.start_all()` — dependency resolution + ordered startup

## Related
- `src/gilbert/interfaces/service.py` — Service ABC, ServiceInfo, ServiceResolver
- `src/gilbert/interfaces/configuration.py` — Configurable protocol, ConfigParam
- `src/gilbert/core/service_manager.py` — ServiceManager implementation
- `src/gilbert/core/services/` — all service wrappers
- `src/gilbert/core/output.py` — output file management utility
- `src/gilbert/core/app.py` — boot sequence using service system
- [AI Service](memory-ai-service.md) — the AI orchestrator that discovers tools
- [Configuration Service](memory-configuration-service.md) — runtime config management
- [Service Registry](memory-service-registry.md) — the legacy DI container that coexists
- [Plugin System](memory-plugin-system.md) — plugins register services via `setup()`
