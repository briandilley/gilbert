# Service System

## Summary
Discoverable service layer where services declare capabilities and dependencies. The ServiceManager handles registration, topological dependency resolution, ordered startup, and runtime discovery. Failed services are skipped gracefully with cascade failure for dependents.

## Details

### Core Concepts
- **Service** (`src/gilbert/interfaces/service.py`) — ABC with `service_info()`, `start(resolver)`, `stop()`
- **ServiceInfo** — declares `name`, `capabilities` (what it provides), `requires` (must exist), `optional` (nice to have)
- **ServiceResolver** — read-only view passed to `start()` for pulling dependencies: `get_capability()`, `require_capability()`, `get_all()`
- **ServiceManager** (`src/gilbert/core/service_manager.py`) — implements ServiceResolver, handles lifecycle

### Capabilities
Capabilities are **strings**, not types. Examples: `"entity_storage"`, `"event_bus"`, `"ai_tools"`. A service can provide multiple capabilities. Multiple services can provide the same capability. This enables flexible discovery ("find anything that provides weather").

### Tool Exposure Rule
**Services should always expose AI tools for their capabilities.** Any service with meaningful operations should implement the `ToolProvider` protocol and declare `ai_tools` as a capability. The AIService auto-discovers all `ai_tools` providers. Exception: CredentialService — no tools for security reasons.

Current tool-providing services:
- **StorageService** — `store_entity`, `get_entity`, `query_entities`, `list_collections`
- **EventBusService** — `publish_event`
- **TTSService** — `speak`, `list_voices`

### Lifecycle
1. Services are **registered** (constructed but not started)
2. Plugins load and register their own services
3. `start_all()` runs topological sort (Kahn's algorithm) on required capabilities
4. Each service starts in dependency order, receiving a `ServiceResolver`
5. Failed services → logged, added to `_failed`, dependents cascade-fail
6. Shutdown: `stop_all()` in reverse start order

### Core Service Wrappers (`src/gilbert/core/services/`)
Existing components are wrapped as services without modifying their ABCs:
- **StorageService** — wraps `StorageBackend`, provides `{"entity_storage", "query_storage", "ai_tools"}`
- **EventBusService** — wraps `EventBus`, provides `{"event_bus", "pub_sub", "ai_tools"}`
- **CredentialService** — provides `{"credentials"}` (no ai_tools — security)
- **TTSService** — wraps `TTSBackend`, provides `{"text_to_speech", "ai_tools"}`
- **AIService** — wraps `AIBackend`, provides `{"ai_chat"}`

Each wrapper exposes the underlying component via a property (e.g., `storage_svc.backend`, `bus_svc.bus`).

### Output File Management
Services that produce files (e.g., TTS audio) write to `.gilbert/output/{service_name}/`. A shared utility (`core/output.py`) provides `get_output_dir()` and `cleanup_old_files()` with TTL-based expiry. Global `output_ttl_seconds` config (default 3600).

### Boot Sequence (app.py)
1. Logging
2. Create ServiceManager
3. Register core services (StorageService, EventBusService, CredentialService)
4. Register optional services (TTSService, AIService) if enabled
5. Register in old ServiceRegistry for backward compat
6. Load plugins → `plugin.setup(service_manager)`
7. `service_manager.start_all()` — dependency resolution + ordered startup

## Related
- `src/gilbert/interfaces/service.py` — Service ABC, ServiceInfo, ServiceResolver
- `src/gilbert/core/service_manager.py` — ServiceManager implementation
- `src/gilbert/core/services/` — all service wrappers
- `src/gilbert/core/output.py` — output file management utility
- `src/gilbert/core/app.py` — boot sequence using service system
- [AI Service](memory-ai-service.md) — the AI orchestrator that discovers tools
- [Service Registry](memory-service-registry.md) — the legacy DI container that coexists
- [Plugin System](memory-plugin-system.md) — plugins register services via `setup()`
