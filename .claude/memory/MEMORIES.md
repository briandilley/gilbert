# Memories

- [AI Service](memory-ai-service.md) — central AI orchestrator with agentic tool loop, Anthropic Claude backend
- [Configuration Service](memory-configuration-service.md) — runtime config management with hot-swap, Configurable protocol
- [Service System](memory-service-system.md) — discoverable services with capabilities, dependencies, and lifecycle management
- [Credential Service](memory-credential-service.md) — named credentials (API keys, passwords, Google SA) from config
- [Storage Backend](memory-storage-backend.md) — generic JSON document store with query/filter/index API, SQLite implementation
- [Event System](memory-event-system.md) — async pub/sub event bus with glob-pattern subscriptions
- [Plugin System](memory-plugin-system.md) — plugin loading from local paths/GitHub, registers services into ServiceManager
- [Configuration and Data Directory](memory-config-and-data-dir.md) — layered YAML config, .gilbert/ data folder
- [Service Registry](memory-service-registry.md) — legacy DI container, coexists with ServiceManager
- [Multi-backend Aggregator Pattern](memory-multi-backend-pattern.md) — services with multiple backends use aggregator pattern
