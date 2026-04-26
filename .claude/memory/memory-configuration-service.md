# Configuration Service

## Summary
Runtime configuration management backed by entity storage, with web UI, hot-swap support, and admin-only access. Services implement the `Configurable` protocol with categories to describe their parameters for auto-generated settings UI.

## Details

### Architecture
- **`interfaces/configuration.py`** — `ConfigParam` and `Configurable` protocol
- **`core/services/configuration.py`** — `ConfigurationService` with capabilities `configuration` + `ai_tools` + `ws_handlers`

### ConfigParam Fields
`ConfigParam` is a frozen dataclass describing a single configurable parameter:
- `key` — parameter name
- `type` — `ToolParameterType` (STRING, INTEGER, BOOLEAN, etc.)
- `description` — human-readable description
- `default` — default value
- `restart_required` — whether changing this requires a service restart
- `sensitive` — mask value in the UI (for passwords, API keys)
- `choices` — fixed tuple of allowed values, renders as dropdown
- `multiline` — render as textarea instead of single-line input
- `choices_from` — dynamic choices resolved at runtime (e.g., `"speakers"`)
- `backend_param` — True if declared by a backend, not the service itself

### Configurable Protocol
Services implement `Configurable` to participate in configuration:
- `config_namespace` — config section name (e.g., `"ai"`, `"tts"`)
- `config_category` — UI grouping (e.g., `"Intelligence"`, `"Media"`)
- `config_params()` — returns list of `ConfigParam`
- `on_config_changed(config)` — called when tunable params change

### Storage Backend
- **Bootstrap config (YAML-only):** `storage`, `logging`, `web` — defined in `config.YAML_ONLY_SECTIONS`
- **All other config:** stored in entity storage collection `gilbert.config`, one entity per namespace (`_id` = namespace)
- **Migration sentinel:** `gilbert.config_meta._schema` entity marks that entity storage was seeded
- **First run:** `seed_storage()` writes each non-bootstrap YAML section into entity storage
- **Subsequent runs:** `load_from_storage()` reads from entity storage, merges over YAML defaults
- No YAML persistence for non-bootstrap config — all service config lives in entity storage

### Backend Registry Pattern
Services with swappable backends (AI, TTS, auth, etc.) use a registry pattern:
- Backend ABCs declare `backend_name: str = ""` and `_registry: dict`
- `__init_subclass__` auto-registers concrete backends by name
- `backend_config_params()` classmethod lets backends declare their own ConfigParams
- Service `config_params()` merges its own params with backend params (prefixed as `settings.*`, marked `backend_param=True`)

### ConfigurationService
- **Read:** `get(path)` for dot-path access, `get_section(namespace)` for a service's full config
- **Write:** `set(path, value)` — validates via Pydantic, persists to entity storage (or YAML for bootstrap sections), notifies or restarts
- **Describe:** `describe_all()` and `describe_categories()` — collects ConfigParams from all Configurable services, grouped by category
- **Factories:** `register_factory(namespace, factory)` — for hot-swap service reconstruction
- **Masking:** sensitive fields masked as `********` in API responses

### Categories
Services declare a `config_category` property. Standard categories:
- Intelligence (ai, knowledge, websearch, skills)
- Media (tts, speaker, music)
- Communication (inbox, inbox_ai_chat, slack, greeting, roast)
- Security (auth, users)
- Monitoring (presence, doorbell)
- Infrastructure (tunnel, backup, screens, storage, event_bus)

### WebSocket RPC Handlers (admin-only, `config.` prefix)
- `config.describe.list` — all categories with sections, params, and current values
- `config.section.get` — single namespace's params + values
- `config.section.set` — set values in a namespace
- `config.section.reset` — reset namespace to defaults

### Web UI
- Settings page at `/settings` with category tabs
- `ConfigSection` — collapsible card per service (enabled toggle, status badge, form fields)
- `ConfigField` — auto-generated form control based on ConfigParam type (text, number, boolean toggle, select dropdown, password, array tags, JSON editor)
- Save button per section with restart notification

### AI Tools
- `get_configuration` — read config by path or full dump
- `set_configuration` — write a config value (validates, persists, triggers reload)
- `describe_configuration` — list all configurable params with types and descriptions

## Related
- [Service System](memory-service-system.md)
- [Configuration and Data Directory](memory-config-and-data-dir.md)
- `src/gilbert/interfaces/configuration.py`
- `src/gilbert/core/services/configuration.py`
- `frontend/src/components/settings/`
