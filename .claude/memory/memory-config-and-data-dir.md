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

These are defined in `config.YAML_ONLY_SECTIONS`. `.gilbert/config.yaml` (gitignored) is deep-merged over `gilbert.yaml` for every section, not just bootstrap ones — see `load_config()` in `src/gilbert/config.py`. For non-bootstrap sections that's only load-bearing on **first boot**: `seed_storage()` writes the merged YAML into entity storage once, and after that the DB is the source of truth (the Settings UI and WS RPCs edit the DB row, not the file). So editing `.gilbert/config.yaml` after first boot has no effect on already-seeded keys — to change those you edit the DB (Settings UI) or wipe `.gilbert/gilbert.db*` and re-seed. This ordering is why `auth.root_password` must go into `.gilbert/config.yaml` before the very first start.

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
- `config.yaml` — per-installation YAML overrides, deep-merged over `gilbert.yaml`. Authoritative for bootstrap sections on every boot; for non-bootstrap sections only seeded on first boot (entity storage wins after that). Put pre-first-boot seed values (e.g. `auth.root_password`) here.
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
