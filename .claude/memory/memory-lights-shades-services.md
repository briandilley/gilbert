# Lights & Shades Services

## Summary
Generic core services for controlling lights and window shades, modeled directly on `MusicService`/`MusicBackend`: a thin orchestration service over a swappable backend chosen by the user from `LightsBackend.registered_backends()` / `ShadesBackend.registered_backends()`. The `lutron-radiora` std-plugin provides the first concrete backends.

## Details

### Interfaces (`src/gilbert/interfaces/`)

- **`lights.py`** — `LightsBackend` ABC + `LightInfo` dataclass.
  - Class flag `supports_dimming` (gates the service's `set_brightness` tool).
  - `LightInfo` fields: `light_id`, `name`, `area`, per-device `supports_dimming`, cached `level`.
  - Abstracts: `initialize(config)`, `close()`, `list_lights()`, `get_level(id)`, `set_level(id, level)`.
  - Registry via `__init_subclass__` keyed on `backend_name`.

- **`shades.py`** — `ShadesBackend` ABC + `ShadeInfo` dataclass.
  - Class flags `supports_position` (gates `set_position` tool), `supports_stop` (gates `stop` tool).
  - `ShadeInfo`: `shade_id`, `name`, `area`, per-device `supports_position` / `supports_stop`, `position`.
  - Position convention: 0 = closed, 100 = open.
  - Abstracts: `initialize`, `close`, `list_shades`, `get_position`, `set_position`. `stop` has a `NotImplementedError` default; backends with `supports_stop = True` override.

### Core services (`src/gilbert/core/services/`)

- **`lights.py`** — `LightsService(Service)` with `slash_namespace = "lights"`.
- **`shades.py`** — `ShadesService(Service)` with `slash_namespace = "shades"`.

Both follow the music-service pattern verbatim:
- `start()` reads `enabled`/`backend`/`settings` from its config namespace, looks up the backend class from the registry, instantiates, and calls `await backend.initialize(settings)`.
- `config_params()` returns a `backend` dropdown (`choices=tuple(Backend.registered_backends().keys())`) plus the selected backend's `backend_config_params()` nested under `settings.<key>`.
- `on_config_changed(config)` re-runs `backend.initialize` so credential changes apply without a process restart.
- `config_actions()` / `invoke_config_action()` forward to `_backend_actions.all_backend_actions` / `invoke_backend_action`.
- `get_tools()` registers a base set; capability-gated tools (`lights_set_brightness`, `shades_set_position`, `shades_stop`) only appear when the backend's class flag is set.
- Name resolution lives in service-level `_resolve(query, items)` helpers — area-name equality wins; falls back to substring match on the device name. Backends never see free text, only `light_id` / `shade_id`.

### Composition root

Both registered in `core/app.py:204-205`-ish (after `MusicService`) and given factories in the configuration-service factory map (`_factory_lights`, `_factory_shades`) so they hot-swap on config change.

### Lutron plugin (`std-plugins/lutron-radiora/`)

Backend-only side-effect plugin. `plugin.py` imports `lutron_lights` and `lutron_shades`; their class definitions trigger `__init_subclass__` on the respective ABCs.

- `bridge.py` — `LutronBridge` wraps `pylutron.Lutron` (sync, threaded). All pylutron calls are wrapped in `asyncio.to_thread` because pylutron's `Output.level` blocks up to a second on a `threading.Event`. Topology cache: `lights_by_id`, `shades_by_id`, area mapping by `id(output)`.
- Module-level `shared_bridge(host, user, password, cache_path=None)` — both backends share one telnet connection per repeater. Rebuilds when credentials change.
- `LutronLights` — `backend_name = "lutron-radiora"`, `supports_dimming = True`. `list_lights` skips `Shade` instances. Per-device `supports_dimming` reflects pylutron's `Output.is_dimmable` (which is a string-table lookup against the `OutputType` from the XML db).
- `LutronShades` — `backend_name = "lutron-radiora"`, both `supports_position` and `supports_stop` are `True`. RadioRA `Shade` extends `Output`, so `set_position` is the same `set_level` call; `stop` calls pylutron's `Shade.stop`.
- Both backends declare the same three config params (host/username/password) — the user enters them on each settings page. Same values → same `shared_bridge` key → one connection. Sensitive flag on the password masks it in the UI.
- Each backend implements `BackendActionProvider` with a single `test_connection` action that connects a short-lived probe `LutronBridge` and reports light/shade counts.

### pylutron caveats

- pylutron has no public disconnect — the connection thread is `daemon=True`. `LutronBridge.disconnect()` just drops the reference. The thread dies at process exit.
- `Output.level` blocks on `threading.Event.wait(1.0)`; on timeout it returns the last cached level. Callers should treat it as best-effort fresh.
- XML db is fetched from `http://<host>/DbXmlInfo.xml` synchronously via `urllib.request` inside `load_xml_db`. Cache to `<plugin-data>/lutron-db.xml` if available.
- Output type → class mapping: `output_type in ('SYSTEM_SHADE', 'MOTOR')` becomes `pylutron.Shade`; everything else stays `pylutron.Output`. Shade detection is `isinstance(output, pylutron.Shade)`.

### Tests

- `tests/unit/test_lights_service.py`, `test_shades_service.py` — service tests using fake backends (`StubLightsBackend`, `SwitchOnlyBackend`, etc.) to exercise capability gating + resolution.
- `std-plugins/lutron-radiora/tests/test_bridge.py`, `test_lutron_lights.py`, `test_lutron_shades.py` — Lutron tests with a fake `pylutron` module injected via `monkeypatch.setitem(sys.modules, "pylutron", fake)`. Tests cover topology parsing, level round-trips, the shared-bridge cache invalidation on credential change, and the `test_connection` action.

## Related
- `src/gilbert/interfaces/music.py` — the model we copied.
- `src/gilbert/core/services/music.py` — sister core service.
- `src/gilbert/core/services/_backend_actions.py` — `all_backend_actions` / `invoke_backend_action` helpers used here.
- [Backend Pattern](memory-backend-pattern.md)
- [Capability Protocols](memory-capability-protocols.md)
