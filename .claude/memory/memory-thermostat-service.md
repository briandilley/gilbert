# Thermostat Service

## Summary
Generic core service for controlling HVAC thermostats, modeled directly on `LightsService`/`ShadesService`: a thin orchestration service over a swappable backend chosen by the user from `ThermostatBackend.registered_backends()`. The `american-standard` std-plugin provides the first concrete backend (Nexia cloud — covers American Standard, Trane, Nexia, and Asair branded thermostats).

## Details

### Interface (`src/gilbert/interfaces/thermostat.py`)

- `ThermostatBackend` ABC + `ThermostatInfo` dataclass + `HVAC_MODES` and `FAN_MODES` tuples.
- Class-level capability flags on the backend: `supports_cooling`, `supports_heating`, `supports_fan_mode`, `supports_humidity` — gate the matching service tools.
- `ThermostatInfo` carries topology *and* last-known state (temperature, humidity, setpoints, mode, fan mode, temperature_unit). Per-device flags `supports_cooling` / `supports_heating` / `supports_fan_mode` / `has_humidity_sensor` because a single backend can mix capability levels (e.g. heat-only zone alongside heat-pump zone).
- HVAC modes are lower-case strings: `off`, `heat`, `cool`, `auto`. Fan modes: `auto`, `on`, `circulate`. Backends translate to/from their native constants.
- Temperatures pass through in the device's native unit; `temperature_unit` (`"F"` or `"C"`) on each `ThermostatInfo` tells consumers which.
- Abstracts: `initialize(config)`, `close()`, `list_thermostats()`, `get_status(id)`, `set_setpoint(id, *, heat=None, cool=None)`, `set_mode(id, mode)`. `set_fan_mode` has a `NotImplementedError` default; backends with `supports_fan_mode = True` override.
- Registry via `__init_subclass__` keyed on `backend_name`.

### Core service (`src/gilbert/core/services/thermostat.py`)

`ThermostatService(Service)` with `slash_namespace = "climate"` (slash commands live under `/climate`, not `/thermostats`) and `config_namespace = "thermostats"` (storage key — distinct from the user-facing slash namespace). Mirrors lights/shades verbatim:

- `start()` reads `enabled`/`backend`/`settings` from its config namespace, looks up the backend class from the registry, instantiates, and calls `await backend.initialize(settings)`.
- `config_params()` returns a `backend` dropdown (choices from `registered_backends()`) plus the selected backend's `backend_config_params()` nested under `settings.<key>`.
- `on_config_changed(config)` re-runs `backend.initialize` so credential changes apply without a process restart.
- `config_actions()` / `invoke_config_action()` forward to `_backend_actions.all_backend_actions` / `invoke_backend_action`.
- `get_tools()` registers a base set; capability-gated tools only appear when the backend's class flag is set:
  - Always: `thermostats_list`, `thermostats_status`, `thermostats_set_mode`.
  - Gated by `supports_heating`: `thermostats_set_heat`.
  - Gated by `supports_cooling`: `thermostats_set_cool`.
  - Gated by both: `thermostats_set_range` (sets heat+cool together for AUTO mode).
  - Gated by `supports_fan_mode`: `thermostats_set_fan_mode`.
- Name resolution: `_resolve(query, items)` — area equality wins; otherwise substring match on the name. Backends never see free text, only `thermostat_id`.
- `_format_status(info)` is a shared formatter that prints `name: <temp>°<unit> · <humidity>% RH · mode=<x> · heat <h>°<u>, cool <c>°<u> · fan=<f>`, omitting humidity / fan / setpoint segments when not present.

### Composition root

Registered in `core/app.py` after `LightsService`/`ShadesService` and given a `_factory_thermostat` factory under the `"thermostats"` config namespace so it hot-swaps on config change.

### American Standard plugin (`std-plugins/american-standard/`)

Backend-only side-effect plugin. `plugin.py` imports `nexia_backend` (which auto-registers `NexiaThermostatBackend` on the ABC), then calls `nexia_backend.set_plugin_data_dir(context.data_dir)` so the backend can write a per-account `nexia-state-<username>.json` file under `.gilbert/plugin-data/american-standard/` to persist Nexia's device UUID across restarts (avoids account-lockout from re-registering as a new device).

- `nexia_backend.py` — owns its own `aiohttp.ClientSession` (the `nexia` library requires the caller to provide one); creates it in `_connect()` and closes it in `close()`. The library handles re-auth on 401/302 internally.
- `NexiaThermostatBackend` — `backend_name = "american-standard"`. All four capability flags (`supports_cooling`, `supports_heating`, `supports_fan_mode`, `supports_humidity`) are `True`.
- Models each *zone* (not gateway) as a Gilbert thermostat. `thermostat_id` is encoded as `"<therm_id>:<zone_id>"` to disambiguate across multiple gateways on the same account. `_split_id` and `_coerce_id` handle parsing back to original int/string types since nexia returns ids as either depending on version.
- Mode mapping: nexia uses uppercase `"OFF"/"HEAT"/"COOL"/"AUTO"`; we expose lowercase. `_MODE_OUT` and `_MODE_IN` translate.
- Fan modes: `NexiaThermostat.get_fan_modes()` returns the device's allowed labels dynamically. `set_fan_mode` matches the user's input case-insensitively against that list.
- Humidity normalization: some nexia firmwares report humidity as `0..1`, others as `0..100`. We treat anything `<= 1.0` as a fraction and multiply by 100.
- `_safe_call(obj, "method", default=...)` is a small helper that calls a method if it exists on the object; lets us defensively read attributes (`get_relative_humidity`, `get_unit`, `get_fan_modes`) that may not exist on every nexia firmware/library version.
- Three config params (username/email, password, brand). `brand` is a dropdown of `("nexia", "asair")`. Password is `sensitive=True`.
- Implements `BackendActionProvider` with a `test_connection` action that logs in with a fresh, short-lived `aiohttp.ClientSession` (so it doesn't disturb the running connection) and reports thermostat + zone counts.

### Tests

- `tests/unit/test_thermostat_service.py` — service tests using `StubThermostatBackend` (full-feature) and `HeatOnlyBackend` to exercise capability gating, name resolution, mode/setpoint/fan dispatch, range validation, and the auto-mode `_set_range` ordering check.
- `std-plugins/american-standard/tests/test_nexia_backend.py` — backend tests with a fake `nexia.home` module (`monkeypatch.setitem(sys.modules, "nexia.home", fake)`) and a fake `aiohttp.ClientSession`. Covers registration, login flow, listing thermostats grouped per zone, humidity fraction-to-percent normalization, mode translation, fan mode dispatch, and the `test_connection` action.

## Related
- `src/gilbert/interfaces/lights.py` / `shades.py` — sister interfaces (single-backend selection pattern).
- `src/gilbert/core/services/lights.py` / `shades.py` — sister core services.
- `src/gilbert/core/services/_backend_actions.py` — `all_backend_actions` / `invoke_backend_action` helpers.
- [Lights & Shades Services](memory-lights-shades-services.md)
- [Backend Pattern](memory-backend-pattern.md)
- [Capability Protocols](memory-capability-protocols.md)
