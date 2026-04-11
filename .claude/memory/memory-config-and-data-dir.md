# Configuration and Data Directory

## Summary
Two-tier configuration: `gilbert.yaml` provides bootstrap-only defaults (storage, logging, web), while all service configuration lives in entity storage (`gilbert.config` collection). The `.gilbert/` directory is the gitignored data folder for all per-instance data.

## Details

### Configuration Tiers

**Tier 1 — Bootstrap (YAML):**
`gilbert.yaml` at repo root contains only the settings needed before entity storage is available:
- `storage` — backend type + connection string (default: `.gilbert/gilbert.db`)
- `logging` — level, file path, AI log file path
- `web` — host, port, and related web server settings

These are defined in `config.YAML_ONLY_SECTIONS`. Bootstrap config can also be overridden via `.gilbert/config.yaml` (deep-merged on top), but this is a legacy mechanism.

**Tier 2 — Entity Storage:**
All non-bootstrap configuration (AI, TTS, auth, speakers, music, etc.) is stored in the `gilbert.config` entity storage collection, one entity per config namespace. This config is managed at runtime via the web UI settings page or AI tools — no file editing required.

On first run, `seed_storage()` migrates non-bootstrap sections from `gilbert.yaml` into entity storage. After that, entity storage is the source of truth for those sections.

### Config Models (Pydantic)
- `GilbertConfig` — top-level: storage, logging, web, plugins, plus dynamic sections
- `StorageConfig` — backend type + connection string
- `LoggingConfig` — level, file path, AI log file path
- `PluginsConfig` — `directories` (scan paths), `sources` (explicit path/URL), `config` (per-plugin overrides)

### `.gilbert/` Directory
Contains per-installation data (gitignored, auto-created on first start):
- `config.yaml` — legacy user configuration overrides (bootstrap sections only)
- `gilbert.db` — SQLite database (entity storage)
- `gilbert.log` — general application log
- `ai_calls.log` — AI API call log
- `plugin-data/<plugin-name>/` — per-plugin persistent data directories
- Plugin cache (fetched GitHub repos)

### Key Principle
Users clone the repo and run it. `.gilbert/` is auto-created on first start. Service configuration is done through the web UI settings page — no source files or config files need editing for customization.

## Related
- `src/gilbert/config.py` — config loading, Pydantic models, `YAML_ONLY_SECTIONS`
- `gilbert.yaml` — committed bootstrap defaults
- `.gitignore` — `.gilbert/` is gitignored
- `src/gilbert/core/app.py` — reads config during bootstrap
- [Configuration Service](memory-configuration-service.md) — runtime config management
- [Plugin System](memory-plugin-system.md) — plugin architecture details
