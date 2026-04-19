# Capability Protocols

## Summary
Services resolve dependencies via `resolver.get_capability("name")`, which returns the abstract `Service` type. To access domain-specific methods without importing concrete service classes, the codebase defines `@runtime_checkable Protocol` classes in `interfaces/`. Consumers use `isinstance` checks against these protocols — never against concrete service classes from `core/services/`.

## Details

### Protocol Table

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

### Usage Pattern

The only correct way to access service capabilities:

```python
from gilbert.interfaces.configuration import ConfigurationReader

config_svc = resolver.get_capability("configuration")
if isinstance(config_svc, ConfigurationReader):
    section = config_svc.get_section("my_namespace")
```

### Anti-pattern

Never import a concrete class from `core/services/` to get at methods:

```python
# WRONG — creates a concrete dependency
from gilbert.core.services.configuration import ConfigurationService
if isinstance(config_svc, ConfigurationService):
    ...
```

### Adding New Capabilities

When a service exposes new methods that other services need, add a `@runtime_checkable Protocol` in the appropriate `interfaces/` module rather than having consumers import the concrete service class. The protocol keeps consumers decoupled from whichever concrete service happens to register the capability today.

### Related Rules

- Duck-typing with `getattr(svc, "method", ...)` is also a violation — use `isinstance(svc, Protocol)` instead.
- Private attribute access (`svc._field`) across services is a violation.
- The `ServiceEnumerator` protocol lets services query/control other services without importing `ServiceManager` directly.

## Related
- `src/gilbert/interfaces/` — all capability protocol definitions
- [Service System](memory-service-system.md) — how services register capabilities
- [Configuration Service](memory-configuration-service.md) — example `ConfigurationReader` consumer
