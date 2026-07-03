# Mafia Game Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `mafia` std-plugin: Gilbert narrates an in-person Mafia party game aloud (TTS), players use phones on a dedicated `/mafia` SPA page (join by code — no account needed), with secret night actions, open day voting, and themed AI storytelling.

**Architecture:** Pure-logic game engine (`game.py`) + narration engine (`narrator.py`) + a `MafiaService` exposing `mafia.*` WS RPCs and pushing per-player-filtered state via direct `conn.enqueue` (never the event bus — secrets must not fan out). Two small core enablers land first: services can declare the minimum role for their own WS frames (`WsRpcRoleProvider`), and `ui.routes.list`/`ui.panels.list` open to `everyone` so guest phones can mount the plugin route. Design decisions were settled in a grilling session; domain vocabulary is in `std-plugins/CONTEXT.md` (Games section) and ADR `std-plugins/docs/adr/0011-mafia-players-ephemeral-not-users.md`.

**Tech Stack:** Python 3.12 (stdlib only), pytest (`asyncio_mode=auto`, run from repo root), React 19 + TypeScript plugin frontend (npm workspace, registered via `panels.ts`).

## Global Constraints

- Two repos, two feature branches (both already created): core work on `feat/mafia-game` in `/Users/dorkycam/projects/gilbert`; plugin work on `feat/mafia-game` in `/Users/dorkycam/projects/gilbert/std-plugins` (git submodule of `briandilley/gilbert-plugins`). **Never push to `main` of either.** Each task says which repo it commits to.
- Plugins import ONLY `gilbert.interfaces.*` and their own modules. Capabilities via `resolver.get_capability(...)` + `isinstance` against `@runtime_checkable` protocols.
- Per-player secrets NEVER ride the event bus — direct `conn.enqueue(frame)` only. All guests share `user_id="guest"` (level 200); Players are authenticated by per-game token, never by `conn.user_id` (ADR plugins-0011).
- Every non-trivial AI system prompt is a `ConfigParam(multiline=True, ai_prompt=True)` read from a field cached in `on_config_changed` (core ADR-0008). Narration AI calls use `AISamplingProvider.complete_one_shot(tools_override=[])` — NEVER `AIProvider.chat()` (it persists a conversation per call; core ADR-0010).
- Killer identities never appear in any AI prompt. Narration prompts carry only public facts (who died, revealed characters, story so far).
- Multi-user isolation (core ADR-0009): no request-scoped state on `self`; game state lives in `self._games: dict[game_id, MafiaGame]`; spawned tasks get `context=contextvars.copy_context()`.
- Type hints everywhere; `uv run pytest` / `uv run mypy src/` / `uv run ruff check` must pass. Run pytest from the gilbert repo root.
- Game rules (locked): min 4 players; 1 killer + 1 doctor always, detective at 7+, 2nd killer at 8+; killer duo = first tap proposes + partner confirms same target; doctor blind pick w/ self-save; detective private killer/not-killer; kill from night 1; every death reveals the Character inside eyes-closed narration (dawn for night kills, dusk for vote-outs); open changeable vote with abstain, strict majority (`len(alive)//2 + 1`) resolves instantly; Host button ends an undecided day (no elimination); killers win at parity (killers ≥ other living), citizens win when killers are gone; Ghosts see everything; Host is a normal Player with skip/end-day/remove/abort powers; no late joins after start.
- `std-plugins/README.md` table + detail section MUST be updated in the same change that adds the plugin (Task 12).

---

### Task 1: Core — `AccessControlProvider.get_rpc_override_level`

**Repo:** gilbert (core)

**Files:**
- Modify: `src/gilbert/interfaces/auth.py` (AccessControlProvider, ~line 339)
- Modify: `src/gilbert/core/services/access_control.py:189-206`
- Test: `tests/unit/test_access_control_service.py` (or create `tests/unit/test_rpc_override_level.py` if no suitable file exists)

**Interfaces:**
- Consumes: existing `AccessControlService._rpc_acl` override cache and `resolve_default_rpc_level` from `interfaces/acl.py`.
- Produces: `def get_rpc_override_level(self, frame_type: str) -> int | None` on both the `AccessControlProvider` protocol and `AccessControlService` — returns the admin-override level for the longest matching override prefix, or `None` when no override matches. Task 2 depends on this exact name/signature.

- [ ] **Step 1: Write the failing test**

Find the existing test file for AccessControlService (`grep -rl "AccessControlService" tests/unit/ | head -3`) and add (adjust fixture construction to match how that file builds the service; if none exists, create `tests/unit/test_rpc_override_level.py` reusing the storage fake pattern from a neighboring service test):

```python
async def test_get_rpc_override_level_none_without_override(acl_service):
    assert acl_service.get_rpc_override_level("mafia.game.join") is None


async def test_get_rpc_override_level_longest_prefix(acl_service):
    await acl_service.set_rpc_permission("mafia.", "everyone")
    await acl_service.set_rpc_permission("mafia.host.", "user")
    assert acl_service.get_rpc_override_level("mafia.game.join") == 200
    assert acl_service.get_rpc_override_level("mafia.host.abort") == 100


async def test_resolve_rpc_level_unchanged_behavior(acl_service):
    # No override → falls back to hardcoded defaults (unlisted = 100)
    assert acl_service.resolve_rpc_level("mafia.game.join") == 100
    await acl_service.set_rpc_permission("mafia.", "everyone")
    assert acl_service.resolve_rpc_level("mafia.game.join") == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/ -k "rpc_override" -v`
Expected: FAIL with `AttributeError: ... has no attribute 'get_rpc_override_level'`

- [ ] **Step 3: Implement**

In `src/gilbert/core/services/access_control.py`, replace `resolve_rpc_level` (lines 189-206) with:

```python
    def get_rpc_override_level(self, frame_type: str) -> int | None:
        """Admin-override level for the longest matching prefix, or None."""
        best = ""
        for prefix in self._rpc_acl:
            if frame_type.startswith(prefix) and len(prefix) > len(best):
                best = prefix
        if best:
            return self.get_role_level(self._rpc_acl[best])
        return None

    def resolve_rpc_level(self, frame_type: str) -> int:
        from gilbert.interfaces.acl import resolve_default_rpc_level

        override = self.get_rpc_override_level(frame_type)
        if override is not None:
            return override
        # Fall back to hardcoded defaults
        return resolve_default_rpc_level(frame_type)
```

In `src/gilbert/interfaces/auth.py`, add to `AccessControlProvider` after `resolve_rpc_level` (~line 341):

```python
    def get_rpc_override_level(self, frame_type: str) -> int | None:
        """Admin-override level for an RPC frame type, or None if no override matches."""
        ...
```

Then `grep -rn "AccessControlProvider" tests/ src/` for test fakes that satisfy the protocol via `isinstance` — `@runtime_checkable` `isinstance` checks method *presence*, so any fake missing the new method now fails the check. Add a `get_rpc_override_level` returning `None` to each such fake.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/ -k "rpc_override or access_control" -v`
Expected: PASS

- [ ] **Step 5: Commit (gilbert repo)**

```bash
git add src/gilbert/interfaces/auth.py src/gilbert/core/services/access_control.py tests/unit/
git commit -m "feat(acl): expose get_rpc_override_level on AccessControlProvider"
```

---

### Task 2: Core — services declare WS RPC roles (`WsRpcRoleProvider`)

**Repo:** gilbert (core)

**Files:**
- Modify: `src/gilbert/interfaces/ws.py` (new protocol after `WsHandlerProvider`, ~line 104)
- Modify: `src/gilbert/web/ws_protocol.py` (`WsConnectionManager.__init__` ~line 449, `subscribe_to_bus` ~lines 461-483, `_resolve_rpc_level` ~lines 823-836)
- Test: `tests/unit/test_ws_rpc_roles.py` (create)

**Interfaces:**
- Consumes: Task 1's `get_rpc_override_level`.
- Produces: `WsRpcRoleProvider` protocol in `gilbert.interfaces.ws` with `def get_ws_rpc_roles(self) -> dict[str, str]` (keys: exact frame types or dot-terminated prefixes like `"mafia."`; values: role names). Declared roles apply ONLY to frames whose handler the same service registered. Precedence: admin overrides > declared > hardcoded defaults. Task 8 (MafiaService) implements this protocol.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_ws_rpc_roles.py`:

```python
"""Service-declared WS RPC roles: declared level applies only to the
declaring service's own frames; admin overrides still win."""

from __future__ import annotations

from typing import Any

from gilbert.interfaces.service import Service, ServiceInfo
from gilbert.interfaces.ws import WsRpcRoleProvider
from gilbert.web.ws_protocol import WsConnectionManager, _resolve_rpc_level


class _GameService(Service):
    def service_info(self) -> ServiceInfo:
        return ServiceInfo(name="mafia", capabilities=frozenset({"ws_handlers"}))

    def get_ws_handlers(self) -> dict[str, Any]:
        async def handler(conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
            return {}

        return {"mafia.game.join": handler, "mafia.host.abort": handler}

    def get_ws_rpc_roles(self) -> dict[str, str]:
        return {"mafia.": "everyone", "chat.": "everyone"}  # chat. must be ignored


class _SM:
    def __init__(self, services: list[Service]):
        self._services = services

    def get_all_by_capability(self, cap: str) -> list[Service]:
        return list(self._services)

    def get_by_capability(self, cap: str) -> Service | None:
        return None  # no event bus, no access_control


class _Gilbert:
    def __init__(self, services: list[Service]):
        self.service_manager = _SM(services)


class _Conn:
    def __init__(self, manager: WsConnectionManager, level: int):
        self.manager = manager
        self.user_level = level


def _manager() -> WsConnectionManager:
    mgr = WsConnectionManager()
    mgr.subscribe_to_bus(_Gilbert([_GameService()]))
    return mgr


def test_declared_role_applies_to_own_frames() -> None:
    mgr = _manager()
    assert _resolve_rpc_level(_Conn(mgr, 200), "mafia.game.join") == 200
    assert _resolve_rpc_level(_Conn(mgr, 200), "mafia.host.abort") == 200


def test_declared_role_ignored_for_foreign_prefix() -> None:
    """The service tried to declare 'chat.' but owns no chat.* handlers."""
    mgr = _manager()
    # chat.message.send is a core handler (or unregistered) — declared
    # 'chat.' from _GameService must not lower it below the default.
    assert mgr.resolve_declared_rpc_role("chat.message.send") is None


def test_protocol_isinstance() -> None:
    assert isinstance(_GameService(), WsRpcRoleProvider)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_ws_rpc_roles.py -v`
Expected: FAIL with `ImportError: cannot import name 'WsRpcRoleProvider'`

- [ ] **Step 3: Implement**

`src/gilbert/interfaces/ws.py` — add after `WsHandlerProvider`:

```python
@runtime_checkable
class WsRpcRoleProvider(Protocol):
    """Optional companion to ``WsHandlerProvider``.

    A service may declare the minimum role required to call its OWN WS
    frames — e.g. a game plugin whose players are unauthenticated guests
    declares ``{"mafia.": "everyone"}``. Keys are exact frame types or
    dot-terminated prefixes; values are role names resolved through the
    access-control service. A declared entry is honored only for frame
    types whose handler the same service registered — it can never
    change the level of another service's frames. Admin runtime
    overrides still take precedence; hardcoded defaults remain the
    fallback.
    """

    def get_ws_rpc_roles(self) -> dict[str, str]:
        """Map frame types / prefixes to minimum role names."""
        ...
```

`src/gilbert/web/ws_protocol.py`:

1. In `WsConnectionManager.__init__` (next to `self._handlers`):

```python
        # frame_type → owning service name (for declared-role scoping)
        self._handler_owner: dict[str, str] = {}
        # declared prefix/frame → (role_name, owning service name)
        self._declared_rpc_roles: dict[str, tuple[str, str]] = {}
```

2. In `subscribe_to_bus`, inside the `for svc in ...ws_handlers` loop: record ownership when a handler is registered (in the `else` branch that does `self._handlers[frame_type] = handler`, add `self._handler_owner[frame_type] = svc.service_info().name`). After the handler loop for a service, add:

```python
                from gilbert.interfaces.ws import WsRpcRoleProvider

                if isinstance(svc, WsRpcRoleProvider):
                    svc_name = svc.service_info().name
                    for key, role in svc.get_ws_rpc_roles().items():
                        owns_match = any(
                            ft == key or ft.startswith(key)
                            for ft, owner in self._handler_owner.items()
                            if owner == svc_name
                        )
                        if not owns_match:
                            logger.warning(
                                "Ignoring declared RPC role %r=%r from %s: matches none of its own handlers",
                                key, role, svc_name,
                            )
                            continue
                        if key in self._declared_rpc_roles:
                            logger.warning(
                                "Declared RPC role conflict on %r: keeping %s, skipping %s",
                                key, self._declared_rpc_roles[key][1], svc_name,
                            )
                            continue
                        self._declared_rpc_roles[key] = (role, svc_name)
```

3. New method on `WsConnectionManager`:

```python
    def resolve_declared_rpc_role(self, frame_type: str) -> str | None:
        """Longest-prefix declared role for a frame the declarer owns."""
        owner = self._handler_owner.get(frame_type)
        if owner is None:
            return None
        best = ""
        best_role: str | None = None
        for key, (role, svc_name) in self._declared_rpc_roles.items():
            if svc_name != owner:
                continue
            if (frame_type == key or frame_type.startswith(key)) and len(key) > len(best):
                best, best_role = key, role
        return best_role
```

4. Replace `_resolve_rpc_level` (keep its imports at top of file; add `BUILTIN_ROLE_LEVELS, DEFAULT_RPC_LEVEL` to the existing `gilbert.interfaces.acl` import):

```python
def _resolve_rpc_level(conn: WsConnection, frame_type: str) -> int:
    """Resolve the required level for an RPC frame type.

    Precedence: admin runtime overrides, then roles the owning service
    declared for its own frames (WsRpcRoleProvider), then hardcoded
    defaults.
    """
    manager = conn.manager
    acl_svc: AccessControlProvider | None = None
    gilbert = manager.gilbert
    if gilbert is not None:
        svc = gilbert.service_manager.get_by_capability("access_control")
        if isinstance(svc, AccessControlProvider):
            acl_svc = svc

    if acl_svc is not None:
        override = acl_svc.get_rpc_override_level(frame_type)
        if override is not None:
            return override

    declared = manager.resolve_declared_rpc_role(frame_type)
    if declared is not None:
        if acl_svc is not None:
            return acl_svc.get_role_level(declared)
        return BUILTIN_ROLE_LEVELS.get(declared, DEFAULT_RPC_LEVEL)

    if acl_svc is not None:
        return acl_svc.resolve_rpc_level(frame_type)
    return get_rpc_permission_level(frame_type)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_ws_rpc_roles.py tests/unit/ -k "ws" -q`
Expected: PASS (including existing ws tests)

- [ ] **Step 5: Commit (gilbert repo)**

```bash
git add src/gilbert/interfaces/ws.py src/gilbert/web/ws_protocol.py tests/unit/test_ws_rpc_roles.py
git commit -m "feat(ws): services can declare RPC roles for their own frames (WsRpcRoleProvider)"
```

---

### Task 3: Core — guest-visible plugin routes/panels

**Repo:** gilbert (core)

**Files:**
- Modify: `src/gilbert/interfaces/acl.py:174-178`
- Modify: `src/gilbert/core/services/web_api.py` (`_level_for` fallback map, ~line 750)
- Modify: `src/gilbert/core/services/plugin_manager.py` (same fallback map, ~line 1000)
- Test: `tests/unit/test_ui_routes_guest.py` (create)

**Interfaces:**
- Produces: guests (level 200) can call `ui.routes.list` / `ui.panels.list`; per-entry `required_role="everyone"` filtering works even without an ACL service. The `/mafia` `UIRoute(required_role="everyone")` from Task 4 depends on this.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_ui_routes_guest.py`:

```python
"""Guests can list plugin routes/panels; per-entry role filter still applies."""

from __future__ import annotations

from gilbert.interfaces.acl import resolve_default_rpc_level


def test_ui_listing_rpcs_are_everyone_level() -> None:
    assert resolve_default_rpc_level("ui.routes.list") == 200
    assert resolve_default_rpc_level("ui.panels.list") == 200
```

Then locate the existing handler tests: `grep -rln "_ws_ui_routes_list\|ui.routes.list" tests/` — extend that file (same fixtures) with a guest-level call asserting a route with `required_role="everyone"` IS returned to a `user_level=200` conn and a `required_role="user"` route is NOT. If no handler test exists, add one to the new file following the direct-call pattern (`await svc._ws_ui_routes_list(_Conn(user_level=200), {"id": "r1"})` with a stub service manager exposing the routes).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_ui_routes_guest.py -v`
Expected: FAIL — `resolve_default_rpc_level("ui.routes.list")` returns 100

- [ ] **Step 3: Implement**

`src/gilbert/interfaces/acl.py` lines 174-178 — change to:

```python
    # Plugin UI extensions: anyone (incl. guests) can ask which panels
    # / routes the loaded plugins contribute. The handlers filter
    # per-entry by required_role, so guests only ever see entries
    # declared required_role="everyone".
    "ui.panels.": 200,
    "ui.routes.": 200,
```

In BOTH `web_api.py:_level_for` and `plugin_manager.py`'s copy, change the fallback map to include the canonical role name:

```python
        return {"admin": 0, "user": 100, "everyone": 200, "anonymous": 200}.get(role, 100)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_ui_routes_guest.py tests/unit/ -k "ui or acl" -q`
Expected: PASS

- [ ] **Step 5: Commit (gilbert repo)**

```bash
git add src/gilbert/interfaces/acl.py src/gilbert/core/services/web_api.py src/gilbert/core/services/plugin_manager.py tests/unit/test_ui_routes_guest.py
git commit -m "feat(acl): open ui.routes/panels listing to guests; handlers still filter per-entry"
```

---

### Task 4: Plugin skeleton — `std-plugins/mafia/`

**Repo:** gilbert-plugins (std-plugins submodule). All paths below relative to `std-plugins/`.

**Files:**
- Create: `mafia/__init__.py` (empty), `mafia/plugin.yaml`, `mafia/plugin.py`, `mafia/pyproject.toml`, `mafia/service.py` (shell), `mafia/tests/conftest.py`, `mafia/tests/test_plugin.py`

**Interfaces:**
- Produces: `MafiaService` shell — `service_info()` returns `ServiceInfo(name="mafia", capabilities=frozenset({"mafia_game", "ws_handlers", "ai_tools"}), optional=frozenset({"ai_chat", "speaker_control", "event_bus", "configuration", "access_control"}), requires_enabled=(EnablementDep(capability="text_to_speech"),), toggleable=True, toggle_description="In-person Mafia party game narrated aloud by Gilbert.")`; `Configurable` surface (`config_namespace="mafia"`, `config_category="Games"`); `slash_namespace = "mafia"`. Tasks 5-11 fill it in.

- [ ] **Step 1: Write the failing test**

`mafia/tests/conftest.py` — copy `std-plugins/model-manager/tests/conftest.py` verbatim, changing the package name to `gilbert_plugin_mafia` and the module list to `["game", "narrator", "service", "plugin"]` (leaf modules first; do NOT pass `submodule_search_locations=[]`).

`mafia/tests/test_plugin.py`:

```python
from __future__ import annotations

from gilbert.interfaces.configuration import Configurable
from gilbert.interfaces.service import EnablementDep
from gilbert_plugin_mafia.plugin import create_plugin
from gilbert_plugin_mafia.service import MafiaService


def test_plugin_metadata() -> None:
    plugin = create_plugin()
    meta = plugin.metadata()
    assert meta.name == "mafia"
    assert "mafia_game" in meta.provides


def test_ui_route_is_guest_visible() -> None:
    routes = create_plugin().ui_routes()
    assert len(routes) == 1
    r = routes[0]
    assert r.path == "/mafia"
    assert r.panel_id == "mafia.page"
    assert r.required_role == "everyone"
    assert r.requires_capability == "mafia_game"


def test_service_info() -> None:
    svc = MafiaService()
    info = svc.service_info()
    assert info.name == "mafia"
    assert {"mafia_game", "ws_handlers", "ai_tools"} <= set(info.capabilities)
    assert EnablementDep(capability="text_to_speech") in info.requires_enabled
    assert info.toggleable is True
    assert isinstance(svc, Configurable)
    assert svc.config_namespace == "mafia"
    assert MafiaService.slash_namespace == "mafia"
```

- [ ] **Step 2: Run test to verify it fails**

Run (from gilbert repo root): `uv run pytest std-plugins/mafia/tests/ -v`
Expected: FAIL (modules don't exist)

- [ ] **Step 3: Implement**

`mafia/plugin.yaml`:

```yaml
name: mafia
version: "1.0.0"
description: "In-person Mafia party game — Gilbert narrates aloud, players use their phones"

provides:
  - mafia_game

requires: []
depends_on: []
```

`mafia/pyproject.toml`:

```toml
[project]
name = "gilbert-plugin-mafia"
version = "1.0.0"
description = "In-person Mafia party game narrated by Gilbert"
requires-python = ">=3.12"
# stdlib only — the game engine, narration, and WS handlers need nothing
# beyond what Gilbert core already provides.
dependencies = []

[tool.uv]
package = false
```

`mafia/plugin.py`:

```python
from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta, UIRoute


class MafiaPlugin(Plugin):
    """Registers the MafiaService and the /mafia SPA page."""

    def __init__(self) -> None:
        self._service: object | None = None

    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="mafia",
            version="1.0.0",
            description="In-person Mafia party game narrated by Gilbert",
            provides=["mafia_game"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from .service import MafiaService

        service = MafiaService()
        context.services.register(service)
        self._service = service

    async def teardown(self) -> None:
        self._service = None

    def ui_routes(self) -> list[UIRoute]:
        return [
            UIRoute(
                path="/mafia",
                panel_id="mafia.page",
                label="Mafia",
                description="Social-deduction party game narrated by Gilbert",
                icon="moon",
                required_role="everyone",
                requires_capability="mafia_game",
                add_to_nav=True,
                show_in_dashboard=True,
            )
        ]


def create_plugin() -> Plugin:
    return MafiaPlugin()
```

`mafia/service.py` (shell — later tasks extend it):

```python
from __future__ import annotations

import logging
from typing import Any

from gilbert.interfaces.configuration import ConfigParam, ConfigurationReader
from gilbert.interfaces.service import EnablementDep, Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)


class MafiaService(Service):
    """Runs Mafia games: lobby, night actions, voting, narration."""

    slash_namespace = "mafia"
    config_namespace = "mafia"
    config_category = "Games"

    def __init__(self) -> None:
        self._enabled = False
        self._resolver: ServiceResolver | None = None
        self._config: dict[str, Any] = {}

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="mafia",
            capabilities=frozenset({"mafia_game", "ws_handlers", "ai_tools"}),
            optional=frozenset(
                {"ai_chat", "speaker_control", "event_bus", "configuration", "access_control"}
            ),
            requires_enabled=(EnablementDep(capability="text_to_speech"),),
            toggleable=True,
            toggle_description="In-person Mafia party game narrated aloud by Gilbert.",
        )

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="enabled",
                type=ToolParameterType.BOOLEAN,
                description="Enable the Mafia party game",
                default=False,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._config.update(config)

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver
        config_svc = resolver.get_capability("configuration")
        if isinstance(config_svc, ConfigurationReader):
            self._config = dict(config_svc.get_section("mafia"))
        self._enabled = bool(self._config.get("enabled", False))
        if not self._enabled:
            logger.info("Mafia service registered but disabled")
            return
        logger.info("Mafia service started")

    async def stop(self) -> None:
        self._enabled = False
```

`mafia/__init__.py`: empty file.

- [ ] **Step 4: Run tests**

Run: `uv run pytest std-plugins/mafia/tests/ -v`
Expected: PASS

- [ ] **Step 5: Commit (std-plugins repo)**

```bash
cd std-plugins
git add mafia/
git commit -m "feat(mafia): plugin skeleton — service shell, /mafia route, manifest"
cd ..
```

---

### Task 5: Game engine — players, characters, lifecycle (`game.py` part 1)

**Repo:** gilbert-plugins.

**Files:**
- Create: `mafia/game.py`
- Test: `mafia/tests/test_game.py`

**Interfaces:**
- Produces (Tasks 6-10 depend on these exact names):
  - `Character(StrEnum)`: `CITIZEN/KILLER/DOCTOR/DETECTIVE`; `Phase(StrEnum)`: `LOBBY/NIGHT_KILLERS/NIGHT_DOCTOR/NIGHT_DETECTIVE/DAWN/DAY/DUSK/ENDED`
  - `MIN_PLAYERS = 4`, `DETECTIVE_MIN_PLAYERS = 7`, `SECOND_KILLER_MIN_PLAYERS = 8`
  - `characters_for(count: int) -> list[Character]`
  - `THEME_PRESETS: list[tuple[str, str]]` (key, description) and `THEME_SURPRISE = "surprise"`
  - `@dataclass Player(player_id, name, token, user_id="", character=Character.CITIZEN, alive=True)`
  - `@dataclass MafiaGame(host_user_id, host_name, theme="", theme_key="", ...)` with `game_id` (uuid4 hex[:8]), `join_code` (6 chars from `ABCDEFGHJKMNPQRSTUVWXYZ23456789` via `secrets.choice`), `phase`, `night: int`, `players: dict[str, Player]`, `story: list[str]`, `winner: str`
  - Methods: `add_player(name, user_id="") -> Player` (raises `GameError` on non-lobby, dup name, empty name); `assign_characters(rng: random.Random | None = None) -> None` (raises `GameError` below MIN_PLAYERS); `alive_players() -> list[Player]`; `killers() -> list[Player]`; `alive_with(character) -> list[Player]`; `player_by_token(token) -> Player | None`
  - `class GameError(Exception)` with a user-facing message

- [ ] **Step 1: Write the failing tests**

`mafia/tests/test_game.py`:

```python
from __future__ import annotations

import random

import pytest

from gilbert_plugin_mafia.game import (
    Character,
    GameError,
    MafiaGame,
    Phase,
    characters_for,
)


def _game(n: int) -> MafiaGame:
    g = MafiaGame(host_user_id="usr_1", host_name="Cam")
    for i in range(n):
        g.add_player(f"P{i}", user_id="usr_1" if i == 0 else "")
    return g


class TestCharacterMatrix:
    def test_minimum_four(self) -> None:
        with pytest.raises(ValueError):
            characters_for(3)

    @pytest.mark.parametrize(
        ("count", "killers", "doctors", "detectives"),
        [(4, 1, 1, 0), (6, 1, 1, 0), (7, 1, 1, 1), (8, 2, 1, 1), (10, 2, 1, 1)],
    )
    def test_matrix(self, count: int, killers: int, doctors: int, detectives: int) -> None:
        chars = characters_for(count)
        assert len(chars) == count
        assert chars.count(Character.KILLER) == killers
        assert chars.count(Character.DOCTOR) == doctors
        assert chars.count(Character.DETECTIVE) == detectives
        assert chars.count(Character.CITIZEN) == count - killers - doctors - detectives


class TestLobby:
    def test_join_code_format(self) -> None:
        g = _game(0)
        assert len(g.join_code) == 6
        assert g.join_code.isalnum()

    def test_add_player_unique_names(self) -> None:
        g = _game(1)
        with pytest.raises(GameError):
            g.add_player("P0")

    def test_no_join_after_start(self) -> None:
        g = _game(4)
        g.assign_characters(random.Random(42))
        g.phase = Phase.NIGHT_KILLERS
        with pytest.raises(GameError):
            g.add_player("Late")

    def test_assign_requires_minimum(self) -> None:
        g = _game(3)
        with pytest.raises(GameError):
            g.assign_characters(random.Random(42))

    def test_assign_is_seeded_and_complete(self) -> None:
        g = _game(8)
        g.assign_characters(random.Random(42))
        assigned = [p.character for p in g.players.values()]
        assert assigned.count(Character.KILLER) == 2
        assert len(g.killers()) == 2

    def test_player_tokens_unique_and_lookup(self) -> None:
        g = _game(4)
        tokens = {p.token for p in g.players.values()}
        assert len(tokens) == 4
        some = next(iter(g.players.values()))
        assert g.player_by_token(some.token) is some
        assert g.player_by_token("nope") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest std-plugins/mafia/tests/test_game.py -v`
Expected: FAIL with import error (`game` module missing) — also update `tests/conftest.py`'s module list if `game` was not yet loadable.

- [ ] **Step 3: Implement `mafia/game.py`**

```python
"""Pure Mafia game state and rules — no I/O, no Gilbert imports.

Vocabulary per std-plugins/CONTEXT.md (Games): Player, Character, Host,
Ghost, Night/Day, Theme, Join code. Rules were locked in the design
grilling (see docs/plans/2026-07-01-mafia-game.md in the core repo).
"""

from __future__ import annotations

import random
import secrets
import uuid
from dataclasses import dataclass, field
from enum import StrEnum

MIN_PLAYERS = 4
DETECTIVE_MIN_PLAYERS = 7
SECOND_KILLER_MIN_PLAYERS = 8

_JOIN_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

THEME_SURPRISE = "surprise"
THEME_PRESETS: list[tuple[str, str]] = [
    ("camping", "A camping trip deep in the mountain woods"),
    ("mansion", "A storm-locked haunted mansion"),
    ("cruise", "A 1920s transatlantic cruise ship"),
    ("space", "A remote deep-space mining station"),
    ("western", "A dusty frontier town in the Old West"),
]


class GameError(Exception):
    """A rule violation with a player-facing message."""


class Character(StrEnum):
    CITIZEN = "citizen"
    KILLER = "killer"
    DOCTOR = "doctor"
    DETECTIVE = "detective"


class Phase(StrEnum):
    LOBBY = "lobby"
    NIGHT_KILLERS = "night_killers"
    NIGHT_DOCTOR = "night_doctor"
    NIGHT_DETECTIVE = "night_detective"
    DAWN = "dawn"
    DAY = "day"
    DUSK = "dusk"
    ENDED = "ended"


def characters_for(count: int) -> list[Character]:
    """The locked role matrix: killer+doctor always, detective at 7+, 2nd killer at 8+."""
    if count < MIN_PLAYERS:
        raise ValueError(f"Mafia needs at least {MIN_PLAYERS} players")
    chars = [Character.KILLER, Character.DOCTOR]
    if count >= DETECTIVE_MIN_PLAYERS:
        chars.append(Character.DETECTIVE)
    if count >= SECOND_KILLER_MIN_PLAYERS:
        chars.append(Character.KILLER)
    chars.extend([Character.CITIZEN] * (count - len(chars)))
    return chars


def _make_join_code() -> str:
    return "".join(secrets.choice(_JOIN_CODE_ALPHABET) for _ in range(6))


@dataclass
class Player:
    player_id: str
    name: str
    token: str
    user_id: str = ""  # real account id; "" for code-joined Players
    character: Character = Character.CITIZEN
    alive: bool = True


@dataclass
class MafiaGame:
    host_user_id: str
    host_name: str
    theme: str = ""       # resolved description text fed to the Narrator
    theme_key: str = ""   # preset key, "custom", or "surprise"
    game_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    join_code: str = field(default_factory=_make_join_code)
    phase: Phase = Phase.LOBBY
    night: int = 0
    players: dict[str, Player] = field(default_factory=dict)
    # night state (cleared by begin_night in Task 6)
    kill_proposed_by: str | None = None
    kill_proposal: str | None = None
    kill_target: str | None = None
    save_target: str | None = None
    checks: dict[str, list[str]] = field(default_factory=dict)  # detective pid → checked pids
    votes: dict[str, str] = field(default_factory=dict)  # voter pid → target pid | "abstain"
    story: list[str] = field(default_factory=list)
    winner: str = ""  # "" | "citizens" | "killers" | "aborted"

    def add_player(self, name: str, user_id: str = "") -> Player:
        if self.phase is not Phase.LOBBY:
            raise GameError("The game has already started")
        clean = name.strip()
        if not clean:
            raise GameError("Pick a name first")
        if any(p.name.lower() == clean.lower() for p in self.players.values()):
            raise GameError(f"The name {clean!r} is taken")
        player = Player(
            player_id=uuid.uuid4().hex[:8],
            name=clean,
            token=secrets.token_urlsafe(16),
            user_id=user_id,
        )
        self.players[player.player_id] = player
        return player

    def assign_characters(self, rng: random.Random | None = None) -> None:
        try:
            chars = characters_for(len(self.players))
        except ValueError as exc:
            raise GameError(str(exc)) from exc
        (rng or random).shuffle(chars)
        for player, character in zip(self.players.values(), chars, strict=True):
            player.character = character

    def alive_players(self) -> list[Player]:
        return [p for p in self.players.values() if p.alive]

    def alive_with(self, character: Character) -> list[Player]:
        return [p for p in self.alive_players() if p.character is character]

    def killers(self) -> list[Player]:
        return [p for p in self.players.values() if p.character is Character.KILLER]

    def player_by_token(self, token: str) -> Player | None:
        for p in self.players.values():
            if p.token and secrets.compare_digest(p.token, token):
                return p
        return None
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest std-plugins/mafia/tests/test_game.py -v`
Expected: PASS

- [ ] **Step 5: Commit (std-plugins repo)**

```bash
cd std-plugins
git add mafia/game.py mafia/tests/
git commit -m "feat(mafia): game engine — players, join codes, character matrix"
cd ..
```

---

### Task 6: Game engine — night actions, voting, win conditions (`game.py` part 2)

**Repo:** gilbert-plugins.

**Files:**
- Modify: `mafia/game.py`
- Test: `mafia/tests/test_game_rules.py` (create)

**Interfaces:**
- Produces (exact signatures on `MafiaGame`; Tasks 8-10 depend on them):
  - `begin_night() -> None` — increments `night`, clears night state + votes, sets `phase=Phase.NIGHT_KILLERS`
  - `killer_act(player_id: str, target_id: str) -> str` — returns `"proposed"` or `"confirmed"`; enforces: actor is living killer, target is living non-killer, duo confirm rule (single killer = instant confirm; with 2 living killers the second tap must match the proposal or `GameError`)
  - `doctor_act(player_id: str, target_id: str) -> None` — any living target incl. self
  - `detective_act(player_id: str, target_id: str) -> bool` — any living target except self; returns is-killer; records into `checks`
  - `resolve_night() -> Player | None` — the victim (killed and marked dead) or None if saved / no kill
  - `cast_vote(voter_id: str, target: str | None) -> None` — target is a living player_id, `"abstain"`, or None to clear; voter must be alive; phase must be DAY
  - `tally() -> dict[str, int]` (target pid / "abstain" → count), `majority_needed() -> int` (`len(alive)//2 + 1`), `majority_target() -> Player | None`
  - `eliminate(player_id: str) -> Player` — marks dead, returns the Player
  - `check_winner() -> str` — `""`, `"citizens"` (no killers alive), `"killers"` (killers ≥ other living)

- [ ] **Step 1: Write the failing tests**

`mafia/tests/test_game_rules.py`:

```python
from __future__ import annotations

import random

import pytest

from gilbert_plugin_mafia.game import Character, GameError, MafiaGame, Phase


def _started(n: int, seed: int = 42) -> MafiaGame:
    g = MafiaGame(host_user_id="usr_1", host_name="Cam")
    for i in range(n):
        g.add_player(f"P{i}", user_id="usr_1" if i == 0 else "")
    g.assign_characters(random.Random(seed))
    g.begin_night()
    return g


def _pid(g: MafiaGame, character: Character, index: int = 0) -> str:
    return [p for p in g.players.values() if p.character is character][index].player_id


def _citizen(g: MafiaGame, index: int = 0) -> str:
    return _pid(g, Character.CITIZEN, index)


class TestKillerDuo:
    def test_single_killer_confirms_instantly(self) -> None:
        g = _started(6)
        assert g.killer_act(_pid(g, Character.KILLER), _citizen(g)) == "confirmed"
        assert g.kill_target == _citizen(g)

    def test_duo_propose_then_confirm(self) -> None:
        g = _started(8)
        k1, k2 = _pid(g, Character.KILLER, 0), _pid(g, Character.KILLER, 1)
        target = _citizen(g)
        assert g.killer_act(k1, target) == "proposed"
        assert g.kill_target is None
        with pytest.raises(GameError):
            g.killer_act(k2, _citizen(g, 1))  # must confirm the proposal
        assert g.killer_act(k2, target) == "confirmed"
        assert g.kill_target == target

    def test_proposer_cannot_confirm_own_proposal(self) -> None:
        g = _started(8)
        k1 = _pid(g, Character.KILLER, 0)
        g.killer_act(k1, _citizen(g))
        with pytest.raises(GameError):
            g.killer_act(k1, _citizen(g))

    def test_killers_cannot_target_killers(self) -> None:
        g = _started(8)
        k1, k2 = _pid(g, Character.KILLER, 0), _pid(g, Character.KILLER, 1)
        with pytest.raises(GameError):
            g.killer_act(k1, k2)

    def test_non_killer_cannot_act(self) -> None:
        g = _started(6)
        with pytest.raises(GameError):
            g.killer_act(_citizen(g), _citizen(g, 1))


class TestDoctorDetective:
    def test_doctor_can_self_save(self) -> None:
        g = _started(6)
        doc = _pid(g, Character.DOCTOR)
        g.doctor_act(doc, doc)
        assert g.save_target == doc

    def test_detective_verdict(self) -> None:
        g = _started(7)
        det = _pid(g, Character.DETECTIVE)
        assert g.detective_act(det, _pid(g, Character.KILLER)) is True
        assert g.detective_act(det, _citizen(g)) is False

    def test_detective_cannot_check_self(self) -> None:
        g = _started(7)
        det = _pid(g, Character.DETECTIVE)
        with pytest.raises(GameError):
            g.detective_act(det, det)


class TestNightResolution:
    def test_kill_lands(self) -> None:
        g = _started(6)
        victim = _citizen(g)
        g.killer_act(_pid(g, Character.KILLER), victim)
        died = g.resolve_night()
        assert died is not None and died.player_id == victim
        assert not g.players[victim].alive

    def test_doctor_save_blocks_kill(self) -> None:
        g = _started(6)
        victim = _citizen(g)
        g.killer_act(_pid(g, Character.KILLER), victim)
        g.doctor_act(_pid(g, Character.DOCTOR), victim)
        assert g.resolve_night() is None
        assert g.players[victim].alive

    def test_no_kill_no_victim(self) -> None:
        g = _started(6)
        assert g.resolve_night() is None


class TestVoting:
    def _in_day(self, n: int) -> MafiaGame:
        g = _started(n)
        g.phase = Phase.DAY
        return g

    def test_majority_math(self) -> None:
        g = self._in_day(7)
        assert g.majority_needed() == 4

    def test_majority_target_and_change_and_abstain(self) -> None:
        g = self._in_day(4)  # majority = 3
        pids = [p.player_id for p in g.alive_players()]
        g.cast_vote(pids[0], pids[3])
        g.cast_vote(pids[1], pids[3])
        g.cast_vote(pids[2], "abstain")
        assert g.majority_target() is None
        g.cast_vote(pids[2], pids[3])  # changed vote
        target = g.majority_target()
        assert target is not None and target.player_id == pids[3]

    def test_dead_cannot_vote_or_be_target(self) -> None:
        g = self._in_day(5)
        pids = [p.player_id for p in g.alive_players()]
        g.eliminate(pids[4])
        with pytest.raises(GameError):
            g.cast_vote(pids[4], pids[0])
        with pytest.raises(GameError):
            g.cast_vote(pids[0], pids[4])


class TestWinConditions:
    def test_citizens_win_when_killers_gone(self) -> None:
        g = _started(6)
        for k in g.killers():
            g.eliminate(k.player_id)
        assert g.check_winner() == "citizens"

    def test_killers_win_at_parity(self) -> None:
        g = _started(8)  # 2 killers, 6 others
        others = [p for p in g.alive_players() if p.character is not Character.KILLER]
        for p in others[:3]:
            g.eliminate(p.player_id)
        assert g.check_winner() == ""  # 2 killers vs 3 others → continue
        g.eliminate(others[3].player_id)
        assert g.check_winner() == "killers"  # 2 killers vs 2 others → parity

    def test_game_continues_1v2(self) -> None:
        g = _started(6)  # 1 killer
        others = [p for p in g.alive_players() if p.character is not Character.KILLER]
        for p in others[:-2]:
            g.eliminate(p.player_id)
        assert g.check_winner() == ""  # 1 killer vs 2 others
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest std-plugins/mafia/tests/test_game_rules.py -v`
Expected: FAIL with `AttributeError: 'MafiaGame' object has no attribute 'begin_night'`

- [ ] **Step 3: Implement — add to `MafiaGame` in `mafia/game.py`**

```python
    # --- night ---

    def begin_night(self) -> None:
        self.night += 1
        self.kill_proposed_by = None
        self.kill_proposal = None
        self.kill_target = None
        self.save_target = None
        self.votes = {}
        self.phase = Phase.NIGHT_KILLERS

    def _living(self, player_id: str) -> Player:
        player = self.players.get(player_id)
        if player is None or not player.alive:
            raise GameError("That player is not in the game (or not alive)")
        return player

    def killer_act(self, player_id: str, target_id: str) -> str:
        actor = self._living(player_id)
        if actor.character is not Character.KILLER:
            raise GameError("You are not a killer")
        target = self._living(target_id)
        if target.character is Character.KILLER:
            raise GameError("You cannot target a fellow killer")
        living_killers = self.alive_with(Character.KILLER)
        if len(living_killers) == 1:
            self.kill_target = target.player_id
            return "confirmed"
        if self.kill_proposal is None:
            self.kill_proposal = target.player_id
            self.kill_proposed_by = actor.player_id
            return "proposed"
        if actor.player_id == self.kill_proposed_by:
            raise GameError("Wait for your partner to confirm")
        if target.player_id != self.kill_proposal:
            proposed = self.players[self.kill_proposal].name
            raise GameError(f"Your partner chose {proposed} — tap them to confirm")
        self.kill_target = target.player_id
        return "confirmed"

    def doctor_act(self, player_id: str, target_id: str) -> None:
        actor = self._living(player_id)
        if actor.character is not Character.DOCTOR:
            raise GameError("You are not the doctor")
        self.save_target = self._living(target_id).player_id

    def detective_act(self, player_id: str, target_id: str) -> bool:
        actor = self._living(player_id)
        if actor.character is not Character.DETECTIVE:
            raise GameError("You are not the detective")
        if target_id == player_id:
            raise GameError("You already know about yourself")
        target = self._living(target_id)
        self.checks.setdefault(actor.player_id, []).append(target.player_id)
        return target.character is Character.KILLER

    def resolve_night(self) -> Player | None:
        if self.kill_target is None or self.kill_target == self.save_target:
            return None
        victim = self.players[self.kill_target]
        victim.alive = False
        return victim

    # --- day ---

    def cast_vote(self, voter_id: str, target: str | None) -> None:
        if self.phase is not Phase.DAY:
            raise GameError("There is no vote right now")
        self._living(voter_id)
        if target is None:
            self.votes.pop(voter_id, None)
            return
        if target != "abstain":
            self._living(target)
        self.votes[voter_id] = target

    def majority_needed(self) -> int:
        return len(self.alive_players()) // 2 + 1

    def tally(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for target in self.votes.values():
            counts[target] = counts.get(target, 0) + 1
        return counts

    def majority_target(self) -> Player | None:
        needed = self.majority_needed()
        for target, count in self.tally().items():
            if target != "abstain" and count >= needed:
                return self.players[target]
        return None

    def eliminate(self, player_id: str) -> Player:
        player = self.players[player_id]
        player.alive = False
        return player

    # --- outcome ---

    def check_winner(self) -> str:
        living = self.alive_players()
        killers = [p for p in living if p.character is Character.KILLER]
        others = [p for p in living if p.character is not Character.KILLER]
        if not killers:
            return "citizens"
        if len(killers) >= len(others):
            return "killers"
        return ""
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest std-plugins/mafia/tests/ -v`
Expected: PASS

- [ ] **Step 5: Commit (std-plugins repo)**

```bash
cd std-plugins
git add mafia/game.py mafia/tests/test_game_rules.py
git commit -m "feat(mafia): night actions, killer-duo confirm, voting, parity win"
cd ..
```

---

### Task 7: Per-player view models (secrecy boundary)

**Repo:** gilbert-plugins.

**Files:**
- Modify: `mafia/game.py` (add module-level functions at the bottom)
- Test: `mafia/tests/test_views.py` (create)

**Interfaces:**
- Produces (Task 8's `_push_state` and the frontend `types.ts` depend on these exact shapes):
  - `public_state(game: MafiaGame) -> dict[str, Any]`:
    `{"game_id", "phase", "night", "theme_key", "join_code" (lobby only, else ""), "players": [{"player_id","name","alive","is_host","character": str|None}], "story": [...], "votes": {voter: target} (DAY only, else {}), "majority_needed": int (DAY only, else 0), "winner"}` — `character` is set only for dead players (reveal-on-death) or when `winner != ""`.
  - `state_for(game: MafiaGame, player_id: str) -> dict[str, Any]`: `public_state` plus `"you"`:
    `{"player_id","name","alive","is_host","character": str|None (own, after assignment),"partner_name": str|None (living-killer partner),"awaiting": "kill"|"kill_confirm"|"save"|"check"|None,"kill_proposal": {"target_id","target_name"}|None,"check_results": [{"player_id","name","is_killer"}],"ghost": {"characters": {player_id: character}}|None}` — `ghost` is set when the player is dead or the game ended.
  - Secrecy invariants (tested): a living citizen's state never contains any other player's character; killer A's state never appears in killer B-unrelated players' states; detective results only in the detective's own state.

- [ ] **Step 1: Write the failing tests**

`mafia/tests/test_views.py`:

```python
from __future__ import annotations

import json
import random

from gilbert_plugin_mafia.game import (
    Character,
    MafiaGame,
    Phase,
    public_state,
    state_for,
)


def _started(n: int, seed: int = 42) -> MafiaGame:
    g = MafiaGame(host_user_id="usr_1", host_name="Cam")
    for i in range(n):
        g.add_player(f"P{i}", user_id="usr_1" if i == 0 else "")
    g.assign_characters(random.Random(seed))
    g.begin_night()
    return g


def _by_char(g: MafiaGame, c: Character, i: int = 0):
    return [p for p in g.players.values() if p.character is c][i]


def test_public_state_hides_living_characters() -> None:
    g = _started(8)
    state = public_state(g)
    assert all(p["character"] is None for p in state["players"])
    assert state["join_code"] == ""  # not in lobby


def test_death_reveals_character_publicly() -> None:
    g = _started(8)
    victim = _by_char(g, Character.CITIZEN)
    g.eliminate(victim.player_id)
    state = public_state(g)
    entry = next(p for p in state["players"] if p["player_id"] == victim.player_id)
    assert entry["character"] == "citizen"


def test_citizen_sees_no_secrets() -> None:
    g = _started(8)
    killer = _by_char(g, Character.KILLER)
    citizen = _by_char(g, Character.CITIZEN)
    blob = json.dumps(state_for(g, citizen.player_id))
    # The word "killer" may appear only as the citizen's own (None) or phase names —
    # assert the killer's player_id is never associated with a character string.
    state = state_for(g, citizen.player_id)
    others = [p for p in state["players"] if p["player_id"] != citizen.player_id]
    assert all(p["character"] is None for p in others)
    assert state["you"]["character"] == "citizen"
    assert state["you"]["ghost"] is None
    assert state["you"]["partner_name"] is None


def test_killers_see_each_other() -> None:
    g = _started(8)
    k1, k2 = _by_char(g, Character.KILLER, 0), _by_char(g, Character.KILLER, 1)
    assert state_for(g, k1.player_id)["you"]["partner_name"] == k2.name


def test_awaiting_flags_follow_phase() -> None:
    g = _started(7)
    killer = _by_char(g, Character.KILLER)
    doctor = _by_char(g, Character.DOCTOR)
    assert state_for(g, killer.player_id)["you"]["awaiting"] == "kill"
    assert state_for(g, doctor.player_id)["you"]["awaiting"] is None
    g.phase = Phase.NIGHT_DOCTOR
    assert state_for(g, doctor.player_id)["you"]["awaiting"] == "save"


def test_detective_results_private() -> None:
    g = _started(7)
    det = _by_char(g, Character.DETECTIVE)
    killer = _by_char(g, Character.KILLER)
    g.detective_act(det.player_id, killer.player_id)
    mine = state_for(g, det.player_id)["you"]["check_results"]
    assert mine == [
        {"player_id": killer.player_id, "name": killer.name, "is_killer": True}
    ]
    citizen = _by_char(g, Character.CITIZEN)
    assert state_for(g, citizen.player_id)["you"]["check_results"] == []


def test_ghost_sees_everything() -> None:
    g = _started(8)
    citizen = _by_char(g, Character.CITIZEN)
    g.eliminate(citizen.player_id)
    ghost = state_for(g, citizen.player_id)["you"]["ghost"]
    assert ghost is not None
    assert set(ghost["characters"].values()) >= {"killer", "doctor"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest std-plugins/mafia/tests/test_views.py -v`
Expected: FAIL with `ImportError: cannot import name 'public_state'`

- [ ] **Step 3: Implement — append to `mafia/game.py`**

```python
def _character_public(game: MafiaGame, player: Player) -> str | None:
    """Reveal-on-death: characters are public once dead, or when the game is over."""
    if not player.alive or game.winner:
        return str(player.character)
    return None


def public_state(game: MafiaGame) -> dict[str, Any]:
    in_day = game.phase is Phase.DAY
    return {
        "game_id": game.game_id,
        "phase": str(game.phase),
        "night": game.night,
        "theme_key": game.theme_key,
        "join_code": game.join_code if game.phase is Phase.LOBBY else "",
        "players": [
            {
                "player_id": p.player_id,
                "name": p.name,
                "alive": p.alive,
                "is_host": p.user_id == game.host_user_id,
                "character": _character_public(game, p),
            }
            for p in game.players.values()
        ],
        "story": list(game.story),
        "votes": dict(game.votes) if in_day else {},
        "majority_needed": game.majority_needed() if in_day else 0,
        "winner": game.winner,
    }


def _awaiting_for(game: MafiaGame, player: Player) -> str | None:
    if not player.alive:
        return None
    if game.phase is Phase.NIGHT_KILLERS and player.character is Character.KILLER:
        if game.kill_target is not None:
            return None
        if game.kill_proposal is not None:
            return None if player.player_id == game.kill_proposed_by else "kill_confirm"
        return "kill"
    if game.phase is Phase.NIGHT_DOCTOR and player.character is Character.DOCTOR:
        return "save" if game.save_target is None else None
    if game.phase is Phase.NIGHT_DETECTIVE and player.character is Character.DETECTIVE:
        return "check"
    return None


def state_for(game: MafiaGame, player_id: str) -> dict[str, Any]:
    state = public_state(game)
    player = game.players[player_id]
    assigned = game.phase is not Phase.LOBBY
    partner_name: str | None = None
    if assigned and player.character is Character.KILLER:
        partners = [k for k in game.killers() if k.player_id != player.player_id and k.alive]
        partner_name = partners[0].name if partners else None
    proposal: dict[str, str] | None = None
    if (
        player.character is Character.KILLER
        and game.kill_proposal is not None
        and game.kill_target is None
    ):
        target = game.players[game.kill_proposal]
        proposal = {"target_id": target.player_id, "target_name": target.name}
    check_results = [
        {
            "player_id": pid,
            "name": game.players[pid].name,
            "is_killer": game.players[pid].character is Character.KILLER,
        }
        for pid in game.checks.get(player.player_id, [])
    ]
    ghost: dict[str, Any] | None = None
    if not player.alive or game.winner:
        ghost = {
            "characters": {p.player_id: str(p.character) for p in game.players.values()}
        }
    state["you"] = {
        "player_id": player.player_id,
        "name": player.name,
        "alive": player.alive,
        "is_host": player.user_id == game.host_user_id,
        "character": str(player.character) if assigned else None,
        "partner_name": partner_name,
        "awaiting": _awaiting_for(game, player),
        "kill_proposal": proposal,
        "check_results": check_results,
        "ghost": ghost,
    }
    return state
```

Add `from typing import Any` to the imports.

- [ ] **Step 4: Run tests**

Run: `uv run pytest std-plugins/mafia/tests/ -v`
Expected: PASS

- [ ] **Step 5: Commit (std-plugins repo)**

```bash
cd std-plugins
git add mafia/game.py mafia/tests/test_views.py
git commit -m "feat(mafia): per-player view models with reveal-on-death and ghost view"
cd ..
```

---

### Task 8: Narration engine (`narrator.py`) + service config params

**Repo:** gilbert-plugins.

**Files:**
- Create: `mafia/narrator.py`
- Modify: `mafia/service.py` (config params + cached prompt fields + narrator wiring in `start`)
- Test: `mafia/tests/test_narrator.py` (create)

**Interfaces:**
- Consumes: `AISamplingProvider.complete_one_shot(*, messages, system_prompt="", profile_name=None, max_tokens=None, tools_override=None) -> AIResponse` (read text via `response.message.content`); `SpeakerProvider.announce(text, speaker_names=None, volume=None, context="") -> str`.
- Produces: `class Narrator` in `mafia/narrator.py`:
  - `__init__(self, *, ai: Any, speaker: Any, system_prompt: str, ai_profile: str, speaker_names: list[str] | None, volume: int | None)` — `ai`/`speaker` are the resolved capability services or `None`.
  - `async def invent_theme(self) -> str` — one-shot asking for a 1-sentence setting (used for "surprise").
  - `async def narrate(self, game: MafiaGame, beat: str, facts: str) -> str` — builds the user message from `facts` + theme + story-so-far, calls the AI (fallback: returns `facts` verbatim when AI absent/erroring), appends the result to `game.story`, returns it.
  - `async def speak(self, text: str, *, context: str = "ominous but playful murder-mystery narrator") -> None` — announce, swallowing all errors (roast pattern).
  - `async def cue(self, game: MafiaGame, beat: str, facts: str) -> str` — `narrate` then `speak`, returns the text.
- Service config params (all cached in `on_config_changed`; Task 8 adds them): `narrator_prompt` (`ai_prompt=True, multiline=True`, default `_DEFAULT_NARRATOR_PROMPT`), `ai_profile` (STRING, `choices_from="ai_profiles"`, default `"standard"`), `speakers` (ARRAY, `choices_from="speakers"`, default `None`), `announce_volume` (INTEGER, default 70), `nudge_seconds` (INTEGER, default 45), `max_concurrent_games` (INTEGER, default 2).

- [ ] **Step 1: Write the failing tests**

`mafia/tests/test_narrator.py`:

```python
from __future__ import annotations

from typing import Any

from gilbert_plugin_mafia.game import MafiaGame
from gilbert_plugin_mafia.narrator import Narrator


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Resp:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _FakeAI:
    """Satisfies AISamplingProvider structurally (has_profile + complete_one_shot)."""

    def __init__(self, reply: str = "A grim tale unfolds.") -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    def has_profile(self, name: str) -> bool:
        return True

    async def complete_one_shot(self, **kwargs: Any) -> _Resp:
        self.calls.append(kwargs)
        return _Resp(self.reply)


class _FakeSpeaker:
    def __init__(self) -> None:
        self.announced: list[tuple[str, Any, Any, str]] = []

    @property
    def backends(self) -> dict[str, Any]:
        return {}

    def get_backend(self, name: str) -> Any:
        return None

    async def resolve_names(self, names: list[str]) -> dict[str, str]:
        return {}

    async def announce(
        self,
        text: str,
        speaker_names: list[str] | None = None,
        volume: int | None = None,
        context: str = "",
    ) -> str:
        self.announced.append((text, speaker_names, volume, context))
        return "ok"


def _narrator(ai: Any = None, speaker: Any = None) -> Narrator:
    return Narrator(
        ai=ai,
        speaker=speaker,
        system_prompt="You are the narrator.",
        ai_profile="standard",
        speaker_names=["Kitchen"],
        volume=70,
    )


def _game() -> MafiaGame:
    g = MafiaGame(host_user_id="u1", host_name="Cam", theme="a camping trip")
    for i in range(4):
        g.add_player(f"P{i}")
    return g


async def test_narrate_appends_story_and_carries_context() -> None:
    ai = _FakeAI()
    n = _narrator(ai=ai)
    g = _game()
    g.story.append("Night one was quiet.")
    text = await n.narrate(g, beat="dawn", facts="P1, the doctor, was found dead.")
    assert text == "A grim tale unfolds."
    assert g.story[-1] == text
    call = ai.calls[0]
    assert call["tools_override"] == []          # ADR-0010
    assert call["system_prompt"] == "You are the narrator."
    user_msg = call["messages"][0].content
    assert "a camping trip" in user_msg           # theme for consistency
    assert "Night one was quiet." in user_msg     # story so far
    assert "P1, the doctor, was found dead." in user_msg


async def test_narrate_falls_back_without_ai() -> None:
    n = _narrator(ai=None)
    g = _game()
    text = await n.narrate(g, beat="dawn", facts="Nobody died last night.")
    assert text == "Nobody died last night."
    assert g.story[-1] == text


async def test_speak_uses_configured_speakers_and_never_raises() -> None:
    spk = _FakeSpeaker()
    n = _narrator(speaker=spk)
    await n.speak("Hello town")
    assert spk.announced[0][0] == "Hello town"
    assert spk.announced[0][1] == ["Kitchen"]
    n_none = _narrator(speaker=None)
    await n_none.speak("silence is fine")  # must not raise


async def test_invent_theme() -> None:
    ai = _FakeAI(reply="A lighthouse cut off by a winter storm.")
    n = _narrator(ai=ai)
    assert await n.invent_theme() == "A lighthouse cut off by a winter storm."
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest std-plugins/mafia/tests/test_narrator.py -v`
Expected: FAIL (`narrator` module missing). Add `"narrator"` to the conftest module list if needed.

- [ ] **Step 3: Implement `mafia/narrator.py`**

```python
"""Narration engine: themed, story-consistent beats via one-shot AI calls.

Killer identities are never given to the model — only public facts
(deaths, revealed characters) plus the running story. AI calls use
complete_one_shot(tools_override=[]) per core ADR-0010; chat() would
persist a conversation per beat.
"""

from __future__ import annotations

import logging
from typing import Any

from gilbert.interfaces.ai import AISamplingProvider, Message, MessageRole
from gilbert.interfaces.speaker import SpeakerProvider

from .game import MafiaGame

logger = logging.getLogger(__name__)

_MAX_STORY_LINES = 20  # cap prompt growth on long games

_BEAT_INSTRUCTIONS: dict[str, str] = {
    "intro": "Open the game: set the scene, name the players as inhabitants, and tell everyone a killer walks among them. End by telling everyone to close their eyes as night falls.",
    "night": "Briefly narrate night falling. Everyone's eyes are closed.",
    "dawn": "Narrate the morning discovery described in the facts, then tell everyone to open their eyes.",
    "dusk": "Everyone has closed their eyes after the vote. Narrate the outcome described in the facts as part of the story.",
    "nudge": "One short in-character line gently hurrying a hesitating, unnamed someone in the dark. Do not name anyone.",
    "win": "Narrate the finale described in the facts, then congratulate the winners and reveal nothing else.",
}


class Narrator:
    """Builds prompts, calls the AI, speaks the result. All I/O degrades gracefully."""

    def __init__(
        self,
        *,
        ai: Any,
        speaker: Any,
        system_prompt: str,
        ai_profile: str,
        speaker_names: list[str] | None,
        volume: int | None,
    ) -> None:
        self._ai = ai
        self._speaker = speaker
        self._system_prompt = system_prompt
        self._ai_profile = ai_profile
        self._speaker_names = speaker_names
        self._volume = volume

    async def invent_theme(self) -> str:
        text = await self._one_shot(
            "Invent an evocative setting for a murder-mystery party game. "
            "Reply with a single short sentence describing the setting and nothing else."
        )
        return text or "A small town where everyone knows everyone"

    async def narrate(self, game: MafiaGame, beat: str, facts: str) -> str:
        story_tail = game.story[-_MAX_STORY_LINES:]
        parts = [
            f"Theme / setting (stay strictly consistent with it): {game.theme}",
            "Story so far:" if story_tail else "This is the very first beat of the story.",
            *story_tail,
            f"Facts to narrate now (do not contradict or invent deaths): {facts}",
            _BEAT_INSTRUCTIONS.get(beat, ""),
            "Write 2-4 sentences. Spoken aloud, so no stage directions or markdown.",
        ]
        text = await self._one_shot("\n".join(p for p in parts if p))
        if not text:
            text = facts
        game.story.append(text)
        return text

    async def speak(
        self, text: str, *, context: str = "ominous but playful murder-mystery narrator"
    ) -> None:
        if not isinstance(self._speaker, SpeakerProvider):
            logger.debug("No speaker service — narration not spoken: %s", text)
            return
        try:
            await self._speaker.announce(
                text,
                speaker_names=self._speaker_names,
                volume=self._volume,
                context=context,
            )
        except Exception:
            logger.exception("Mafia narration announce failed")

    async def cue(self, game: MafiaGame, beat: str, facts: str) -> str:
        text = await self.narrate(game, beat, facts)
        await self.speak(text)
        return text

    async def _one_shot(self, prompt: str) -> str:
        if not isinstance(self._ai, AISamplingProvider):
            return ""
        try:
            response = await self._ai.complete_one_shot(
                messages=[Message(role=MessageRole.USER, content=prompt)],
                system_prompt=self._system_prompt,
                profile_name=self._ai_profile or None,
                tools_override=[],
            )
            return response.message.content.strip()
        except Exception:
            logger.exception("Mafia narration AI call failed")
            return ""
```

In `mafia/service.py`: add the `_DEFAULT_NARRATOR_PROMPT` constant and extend `config_params()` / `on_config_changed`:

```python
_DEFAULT_NARRATOR_PROMPT = (
    "You are Gilbert, the narrator of an in-person party game of Mafia. "
    "You tell one continuous, atmospheric story set in the theme you are given, "
    "ominous but playful, suitable for a living room of friends. "
    "Stay strictly consistent with the theme and with every prior story beat: "
    "characters who died stay dead, places and details stay the same. "
    "Narrate only the facts you are given — never invent deaths, accusations, "
    "or clues about who the killers are, and never reveal hidden information."
)
```

```python
    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="enabled",
                type=ToolParameterType.BOOLEAN,
                description="Enable the Mafia party game",
                default=False,
            ),
            ConfigParam(
                key="narrator_prompt",
                type=ToolParameterType.STRING,
                description="System prompt for the Mafia narrator persona",
                default=_DEFAULT_NARRATOR_PROMPT,
                multiline=True,
                ai_prompt=True,
            ),
            ConfigParam(
                key="ai_profile",
                type=ToolParameterType.STRING,
                description="AI profile used for narration",
                default="standard",
                choices_from="ai_profiles",
            ),
            ConfigParam(
                key="speakers",
                type=ToolParameterType.ARRAY,
                description="Speakers for narration (empty = default announce speakers)",
                default=None,
                choices_from="speakers",
            ),
            ConfigParam(
                key="announce_volume",
                type=ToolParameterType.INTEGER,
                description="Narration volume (0-100)",
                default=70,
            ),
            ConfigParam(
                key="nudge_seconds",
                type=ToolParameterType.INTEGER,
                description="Seconds of silence before the narrator nudges a stalled phase",
                default=45,
            ),
            ConfigParam(
                key="max_concurrent_games",
                type=ToolParameterType.INTEGER,
                description="Maximum simultaneous Mafia games",
                default=2,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        self._config.update(config)
        self._narrator_prompt = str(
            self._config.get("narrator_prompt") or _DEFAULT_NARRATOR_PROMPT
        )
        self._ai_profile = str(self._config.get("ai_profile") or "standard")
        raw_speakers = self._config.get("speakers")
        self._speaker_names = [str(s) for s in raw_speakers] if raw_speakers else None
        self._volume = int(self._config.get("announce_volume") or 70)
        self._nudge_seconds = int(self._config.get("nudge_seconds") or 45)
        self._max_games = int(self._config.get("max_concurrent_games") or 2)
```

Initialize those fields in `__init__` (`self._narrator_prompt = _DEFAULT_NARRATOR_PROMPT`, etc.), call `await self.on_config_changed(self._config)` at the end of the config-read block in `start()`, and add a factory the handlers will use:

```python
    def _narrator(self) -> Narrator:
        assert self._resolver is not None
        return Narrator(
            ai=self._resolver.get_capability("ai_chat"),
            speaker=self._resolver.get_capability("speaker_control"),
            system_prompt=self._narrator_prompt,
            ai_profile=self._ai_profile,
            speaker_names=self._speaker_names,
            volume=self._volume,
        )
```

(import `Narrator` from `.narrator`).

- [ ] **Step 4: Run tests**

Run: `uv run pytest std-plugins/mafia/tests/ -v`
Expected: PASS

- [ ] **Step 5: Commit (std-plugins repo)**

```bash
cd std-plugins
git add mafia/narrator.py mafia/service.py mafia/tests/test_narrator.py
git commit -m "feat(mafia): narration engine — themed one-shot beats, configurable prompt"
cd ..
```

---

### Task 9: Service — join/create/resume RPCs + connection registry + state push

**Repo:** gilbert-plugins.

**Files:**
- Modify: `mafia/service.py`
- Test: `mafia/tests/test_service_lobby.py` (create)

**Interfaces:**
- Consumes: Tasks 5-8. `WsConnectionBase` protocol (`user_ctx`, `user_level`, `enqueue`, `add_close_callback`); Task 2's `WsRpcRoleProvider`.
- Produces WS frames (Task 10 and the frontend depend on these exact names/shapes):
  - `get_ws_rpc_roles() -> {"mafia.": "everyone"}` and `get_ws_handlers()` mapping all `mafia.*` frames.
  - `mafia.game.create` `{theme_key, theme_text?, id}` → `{"type":"mafia.game.create.result","ref",game_id,join_code,player_id,player_token,state}` — requires `conn.user_level <= 100` AND non-guest (`conn.user_ctx.user_id not in ("", "guest")`); host auto-joined as Player; enforces `max_concurrent_games`; `theme_key` is a preset key, `"custom"` (uses `theme_text`), or `"surprise"` (theme resolved at start).
  - `mafia.game.join` `{join_code, name, id}` → `{"type":"mafia.game.join.result","ref",game_id,player_id,player_token,state}` — anyone (incl. guests); registers the conn for that player.
  - `mafia.game.resume` `{game_id, player_token, id}` → `{"type":"mafia.game.resume.result","ref",state}` — re-attach after reload/disconnect.
  - `mafia.games.active` `{id}` → `{"type":"mafia.games.active.result","ref","games":[{game_id, join_code, host_name, phase, player_count}]}` — join codes are shown only for lobby-phase games.
  - Server push frame: `{"type": "mafia.state", "game_id", "state": state_for(game, player_id)}` via `conn.enqueue` to every registered conn of every player (per-player filtered). Internal registry: `self._conns: dict[str, dict[str, set[Any]]]` (game_id → player_id → conns), cleaned by `add_close_callback`.
  - Error frame helper: `{"type":"gilbert.error","ref",...,"error":str,"code":400|403|404}`.

- [ ] **Step 1: Write the failing tests**

`mafia/tests/test_service_lobby.py`:

```python
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from gilbert.interfaces.auth import UserContext
from gilbert_plugin_mafia.service import MafiaService


class _Conn:
    def __init__(self, user_id: str = "usr_1", name: str = "Cam", level: int = 100) -> None:
        self.user_ctx = UserContext(
            user_id=user_id, email="", display_name=name, roles=frozenset({"user"}), provider="local"
        )
        self.user_level = level
        self.sent: list[dict[str, Any]] = []
        self._close_cbs: list[Any] = []

    def enqueue(self, msg: dict[str, Any]) -> None:
        self.sent.append(msg)

    def add_close_callback(self, cb: Any) -> None:
        self._close_cbs.append(cb)

    def close(self) -> None:
        for cb in self._close_cbs:
            cb()


def _guest_conn(name: str = "Guest") -> _Conn:
    return _Conn(user_id="guest", name=name, level=200)


class _FakeResolver:
    def get_capability(self, name: str) -> Any:
        return None

    def require_capability(self, name: str) -> Any:
        raise LookupError(name)

    def get_all(self, name: str) -> list[Any]:
        return []


@pytest.fixture
async def svc() -> MafiaService:
    service = MafiaService()
    service._config = {"enabled": True}
    await service.on_config_changed(service._config)
    service._resolver = _FakeResolver()
    service._enabled = True
    return service


async def _create(svc: MafiaService, conn: _Conn) -> dict[str, Any]:
    resp = await svc._ws_game_create(conn, {"id": "r1", "theme_key": "camping"})
    assert resp["type"] == "mafia.game.create.result", resp
    return resp


async def test_create_requires_real_account(svc: MafiaService) -> None:
    resp = await svc._ws_game_create(_guest_conn(), {"id": "r1", "theme_key": "camping"})
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 403


async def test_create_and_join_flow(svc: MafiaService) -> None:
    host = _Conn()
    created = await _create(svc, host)
    assert created["join_code"]
    assert created["state"]["you"]["is_host"] is True

    guest = _guest_conn("Jess")
    joined = await svc._ws_game_join(
        guest, {"id": "r2", "join_code": created["join_code"], "name": "Jess"}
    )
    assert joined["type"] == "mafia.game.join.result"
    assert joined["player_token"]
    # host got a live state push when Jess joined
    assert any(m["type"] == "mafia.state" for m in host.sent)


async def test_join_bad_code(svc: MafiaService) -> None:
    resp = await svc._ws_game_join(_guest_conn(), {"id": "r", "join_code": "NOPE99", "name": "X"})
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 404


async def test_resume_reattaches(svc: MafiaService) -> None:
    host = _Conn()
    created = await _create(svc, host)
    guest = _guest_conn("Jess")
    joined = await svc._ws_game_join(
        guest, {"id": "r2", "join_code": created["join_code"], "name": "Jess"}
    )
    guest.close()  # simulates page reload — registry cleaned
    fresh = _guest_conn("Jess")
    resumed = await svc._ws_game_resume(
        fresh,
        {"id": "r3", "game_id": joined["game_id"], "player_token": joined["player_token"]},
    )
    assert resumed["type"] == "mafia.game.resume.result"
    assert resumed["state"]["you"]["name"] == "Jess"


async def test_resume_bad_token(svc: MafiaService) -> None:
    host = _Conn()
    created = await _create(svc, host)
    resp = await svc._ws_game_resume(
        _guest_conn(), {"id": "r", "game_id": created["game_id"], "player_token": "bad"}
    )
    assert resp["type"] == "gilbert.error"
    assert resp["code"] == 403


async def test_max_concurrent_games(svc: MafiaService) -> None:
    svc._max_games = 1
    await _create(svc, _Conn())
    resp = await svc._ws_game_create(_Conn(user_id="usr_2"), {"id": "r", "theme_key": "space"})
    assert resp["type"] == "gilbert.error"


async def test_declared_rpc_roles(svc: MafiaService) -> None:
    assert svc.get_ws_rpc_roles() == {"mafia.": "everyone"}
    handlers = svc.get_ws_handlers()
    assert "mafia.game.join" in handlers and "mafia.game.create" in handlers
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest std-plugins/mafia/tests/test_service_lobby.py -v`
Expected: FAIL with `AttributeError: ... '_ws_game_create'`

- [ ] **Step 3: Implement in `mafia/service.py`**

Add imports and registry/`__init__` fields:

```python
import asyncio
import contextvars
import secrets

from .game import (
    THEME_PRESETS,
    THEME_SURPRISE,
    Character,
    GameError,
    MafiaGame,
    Phase,
    public_state,
    state_for,
)
from .narrator import Narrator
```

```python
        # __init__ additions
        self._games: dict[str, MafiaGame] = {}
        # game_id → player_id → live connections (per-player secret channel)
        self._conns: dict[str, dict[str, set[Any]]] = {}
        self._nudge_tasks: dict[str, asyncio.Task[None]] = {}
        self._beat_tasks: dict[str, asyncio.Task[None]] = {}
```

Handlers + helpers:

```python
    # --- WS wiring ---

    def get_ws_handlers(self) -> dict[str, Any]:
        if not self._enabled:
            return {}
        return {
            "mafia.game.create": self._ws_game_create,
            "mafia.game.join": self._ws_game_join,
            "mafia.game.resume": self._ws_game_resume,
            "mafia.games.active": self._ws_games_active,
            "mafia.game.start": self._ws_game_start,
            "mafia.night.act": self._ws_night_act,
            "mafia.vote.cast": self._ws_vote_cast,
            "mafia.host.skip_phase": self._ws_host_skip,
            "mafia.host.end_day": self._ws_host_end_day,
            "mafia.host.remove_player": self._ws_host_remove,
            "mafia.host.abort": self._ws_host_abort,
        }

    def get_ws_rpc_roles(self) -> dict[str, str]:
        # Players are ephemeral guests (ADR plugins-0011); handlers do
        # per-frame auth via game-scoped player tokens / host account.
        return {"mafia.": "everyone"}

    @staticmethod
    def _err(frame: dict[str, Any], message: str, code: int = 400) -> dict[str, Any]:
        return {"type": "gilbert.error", "ref": frame.get("id"), "error": message, "code": code}

    def _register_conn(self, game_id: str, player_id: str, conn: Any) -> None:
        conns = self._conns.setdefault(game_id, {}).setdefault(player_id, set())
        if conn in conns:
            return
        conns.add(conn)

        def _cleanup() -> None:
            game_conns = self._conns.get(game_id, {})
            game_conns.get(player_id, set()).discard(conn)

        conn.add_close_callback(_cleanup)

    def _push_state(self, game: MafiaGame) -> None:
        """Per-player filtered state to every live connection of the game."""
        for player_id, conns in self._conns.get(game.game_id, {}).items():
            if player_id not in game.players:
                continue
            state = state_for(game, player_id)
            frame = {"type": "mafia.state", "game_id": game.game_id, "state": state}
            for conn in list(conns):
                conn.enqueue(frame)

    def _game_and_player(
        self, frame: dict[str, Any]
    ) -> tuple[MafiaGame, Any] | dict[str, Any]:
        game = self._games.get(str(frame.get("game_id", "")))
        if game is None:
            return self._err(frame, "Game not found", 404)
        player = game.player_by_token(str(frame.get("player_token", "")))
        if player is None:
            return self._err(frame, "Not a player in this game", 403)
        return game, player

    def _require_host(self, conn: Any, frame: dict[str, Any]) -> MafiaGame | dict[str, Any]:
        game = self._games.get(str(frame.get("game_id", "")))
        if game is None:
            return self._err(frame, "Game not found", 404)
        if conn.user_ctx.user_id != game.host_user_id:
            return self._err(frame, "Only the host can do that", 403)
        return game

    # --- lobby handlers ---

    async def _ws_game_create(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        user_id = conn.user_ctx.user_id
        if conn.user_level > 100 or user_id in ("", "guest"):
            return self._err(frame, "Creating a game needs a signed-in account", 403)
        active = [g for g in self._games.values() if g.phase is not Phase.ENDED]
        if len(active) >= self._max_games:
            return self._err(frame, "Too many games running — finish one first")
        theme_key = str(frame.get("theme_key", "") or THEME_SURPRISE)
        theme_text = str(frame.get("theme_text", "")).strip()
        theme = ""
        if theme_key == "custom":
            if not theme_text:
                return self._err(frame, "Describe your custom theme")
            theme = theme_text
        elif theme_key != THEME_SURPRISE:
            preset = dict(THEME_PRESETS).get(theme_key)
            if preset is None:
                return self._err(frame, f"Unknown theme {theme_key!r}")
            theme = preset
        game = MafiaGame(
            host_user_id=user_id,
            host_name=conn.user_ctx.display_name,
            theme=theme,
            theme_key=theme_key,
        )
        host_player = game.add_player(conn.user_ctx.display_name, user_id=user_id)
        self._games[game.game_id] = game
        self._register_conn(game.game_id, host_player.player_id, conn)
        return {
            "type": "mafia.game.create.result",
            "ref": frame.get("id"),
            "game_id": game.game_id,
            "join_code": game.join_code,
            "player_id": host_player.player_id,
            "player_token": host_player.token,
            "state": state_for(game, host_player.player_id),
        }

    async def _ws_game_join(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        code = str(frame.get("join_code", "")).strip().upper()
        game = next((g for g in self._games.values() if g.join_code == code), None)
        if game is None:
            return self._err(frame, "No game with that code", 404)
        try:
            player = game.add_player(
                str(frame.get("name", "")), user_id=conn.user_ctx.user_id
                if conn.user_ctx.user_id != "guest"
                else "",
            )
        except GameError as exc:
            return self._err(frame, str(exc))
        self._register_conn(game.game_id, player.player_id, conn)
        self._push_state(game)
        return {
            "type": "mafia.game.join.result",
            "ref": frame.get("id"),
            "game_id": game.game_id,
            "player_id": player.player_id,
            "player_token": player.token,
            "state": state_for(game, player.player_id),
        }

    async def _ws_game_resume(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        result = self._game_and_player(frame)
        if isinstance(result, dict):
            return result
        game, player = result
        self._register_conn(game.game_id, player.player_id, conn)
        return {
            "type": "mafia.game.resume.result",
            "ref": frame.get("id"),
            "state": state_for(game, player.player_id),
        }

    async def _ws_games_active(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "mafia.games.active.result",
            "ref": frame.get("id"),
            "games": [
                {
                    "game_id": g.game_id,
                    "join_code": g.join_code if g.phase is Phase.LOBBY else "",
                    "host_name": g.host_name,
                    "phase": str(g.phase),
                    "player_count": len(g.players),
                }
                for g in self._games.values()
                if g.phase is not Phase.ENDED
            ],
        }
```

Also add placeholder async methods raising nothing yet for the Task 10 handlers referenced in `get_ws_handlers` (`_ws_game_start`, `_ws_night_act`, `_ws_vote_cast`, `_ws_host_skip`, `_ws_host_end_day`, `_ws_host_remove`, `_ws_host_abort`) so this task imports cleanly:

```python
    async def _ws_game_start(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        return self._err(frame, "Not implemented yet", 400)
```

(one per handler — Task 10 replaces them; these stubs keep `get_ws_handlers` truthful and are exercised only by Task 10's tests).

Update `stop()` to cancel tasks and clear games:

```python
    async def stop(self) -> None:
        self._enabled = False
        for task in (*self._nudge_tasks.values(), *self._beat_tasks.values()):
            task.cancel()
        self._nudge_tasks.clear()
        self._beat_tasks.clear()
        self._games.clear()
        self._conns.clear()
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest std-plugins/mafia/tests/ -v`
Expected: PASS

- [ ] **Step 5: Commit (std-plugins repo)**

```bash
cd std-plugins
git add mafia/service.py mafia/tests/test_service_lobby.py
git commit -m "feat(mafia): lobby RPCs — create/join/resume, token auth, per-player push"
cd ..
```

---

### Task 10: Service — game loop: start, night, vote, host powers, nudges

**Repo:** gilbert-plugins.

**Files:**
- Modify: `mafia/service.py` (replace Task 9's stubs)
- Test: `mafia/tests/test_service_flow.py` (create)

**Interfaces:**
- Consumes: everything above.
- Produces the remaining frames:
  - `mafia.game.start` `{game_id}` (host only, ≥4 players): resolves "surprise" theme via `Narrator.invent_theme()`, `assign_characters()`, runs the intro beat, then `begin_night()` → phase `NIGHT_KILLERS`, pushes, starts nudge timer.
  - `mafia.night.act` `{game_id, player_token, target_id}`: dispatches by current phase — killer propose/confirm (confirmed → advance), doctor save (→ advance), detective check (returns `{"is_killer": bool}` in the RPC result; → advance). Each action cancels+restarts the nudge timer. Phase advance skips dead specials: `NIGHT_KILLERS → NIGHT_DOCTOR` (if living doctor) `→ NIGHT_DETECTIVE` (if living detective) `→ dawn resolution`.
  - Dawn resolution (internal `_dawn(game)`): `resolve_night()`, narrate `dawn` beat (victim + revealed character, or "nobody died"), `check_winner()` → possibly finale, else phase `DAY`, push.
  - `mafia.vote.cast` `{game_id, player_token, target: pid|"abstain"|null}`: `cast_vote`, push tally; on `majority_target()` → dusk: eliminate, narrate `dusk` beat with reveal, `check_winner()` → finale or `begin_night()` + `night` beat, push.
  - `mafia.host.end_day` `{game_id}` (host): dusk with no elimination (narrated), then night.
  - `mafia.host.skip_phase` `{game_id}` (host): forfeits the awaited night action (no kill / no save / no check) and advances.
  - `mafia.host.remove_player` `{game_id, player_id}` (host): eliminate + narrated reveal ("left the story"), winner check, if the removed player was the awaited actor → advance.
  - `mafia.host.abort` `{game_id}` (host): `winner="aborted"`, phase `ENDED`, push, cancel timers, delete game after final push.
  - Nudge timer: per-game asyncio task sleeping `self._nudge_seconds`, then `Narrator.cue(game, "nudge", ...)`, repeating; created with `asyncio.create_task(..., context=contextvars.copy_context())` (ADR-0009); cancelled on every phase change/action and at ENDED.
  - Narration beats run inline in the handler (`await`) — the party is waiting on the voice anyway; only nudge timers are background tasks.

- [ ] **Step 1: Write the failing tests**

`mafia/tests/test_service_flow.py` (reuse `_Conn`, `_guest_conn`, `_FakeResolver` by importing from `test_service_lobby` or duplicating — duplicate to keep files independent; also a `_FakeAI`-wired resolver so narration is deterministic):

```python
from __future__ import annotations

from typing import Any

import pytest

from gilbert.interfaces.auth import UserContext
from gilbert_plugin_mafia.game import Character, Phase
from gilbert_plugin_mafia.service import MafiaService


class _Conn:
    def __init__(self, user_id: str = "usr_1", name: str = "Cam", level: int = 100) -> None:
        self.user_ctx = UserContext(
            user_id=user_id, email="", display_name=name, roles=frozenset({"user"}), provider="local"
        )
        self.user_level = level
        self.sent: list[dict[str, Any]] = []
        self._close_cbs: list[Any] = []

    def enqueue(self, msg: dict[str, Any]) -> None:
        self.sent.append(msg)

    def add_close_callback(self, cb: Any) -> None:
        self._close_cbs.append(cb)


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Resp:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _FakeAI:
    def has_profile(self, name: str) -> bool:
        return True

    async def complete_one_shot(self, **kwargs: Any) -> _Resp:
        return _Resp("A story beat.")


class _FakeResolver:
    def get_capability(self, name: str) -> Any:
        return _FakeAI() if name == "ai_chat" else None

    def require_capability(self, name: str) -> Any:
        raise LookupError(name)

    def get_all(self, name: str) -> list[Any]:
        return []


@pytest.fixture
async def table() -> tuple[MafiaService, dict[str, Any], dict[str, _Conn], dict[str, dict[str, Any]]]:
    """A started 4-player game. Returns (svc, created, conns, joins by name)."""
    svc = MafiaService()
    svc._config = {"enabled": True, "nudge_seconds": 9999}
    await svc.on_config_changed(svc._config)
    svc._resolver = _FakeResolver()
    svc._enabled = True

    host_conn = _Conn()
    created = await svc._ws_game_create(host_conn, {"id": "c", "theme_key": "camping"})
    conns: dict[str, _Conn] = {"Cam": host_conn}
    joins: dict[str, dict[str, Any]] = {
        "Cam": {
            "game_id": created["game_id"],
            "player_id": created["player_id"],
            "player_token": created["player_token"],
        }
    }
    for name in ("Ana", "Ben", "Dot"):
        conn = _Conn(user_id="guest", name=name, level=200)
        joined = await svc._ws_game_join(
            conn, {"id": "j", "join_code": created["join_code"], "name": name}
        )
        conns[name] = conn
        joins[name] = joined
    resp = await svc._ws_game_start(host_conn, {"id": "s", "game_id": created["game_id"]})
    assert resp["type"] == "mafia.game.start.result", resp
    return svc, created, conns, joins


def _game(svc: MafiaService, created: dict[str, Any]):
    return svc._games[created["game_id"]]


def _player_named(game: Any, name: str):
    return next(p for p in game.players.values() if p.name == name)


def _by_char(game: Any, c: Character, i: int = 0):
    return [p for p in game.players.values() if p.character is c][i]


def _token(game: Any, joins: dict[str, dict[str, Any]], player: Any) -> str:
    return joins[player.name]["player_token"]


async def _act(svc: MafiaService, game: Any, joins: dict[str, Any], player: Any, target: Any):
    return await svc._ws_night_act(
        None,
        {
            "id": "a",
            "game_id": game.game_id,
            "player_token": _token(game, joins, player),
            "target_id": target.player_id,
        },
    )


async def test_start_assigns_and_enters_night(table) -> None:
    svc, created, conns, joins = table
    game = _game(svc, created)
    assert game.phase is Phase.NIGHT_KILLERS
    assert game.night == 1  # kill from night 1
    assert len(game.story) >= 1  # intro beat narrated


async def test_start_requires_host(table) -> None:
    svc, created, conns, joins = table
    resp = await svc._ws_game_start(conns["Ana"], {"id": "x", "game_id": created["game_id"]})
    assert resp["type"] == "gilbert.error"


async def test_full_night_to_day(table) -> None:
    svc, created, conns, joins = table
    game = _game(svc, created)
    killer = _by_char(game, Character.KILLER)
    doctor = _by_char(game, Character.DOCTOR)
    victim = _by_char(game, Character.CITIZEN)

    resp = await _act(svc, game, joins, killer, victim)
    assert resp["type"] == "mafia.night.act.result"
    assert game.phase is Phase.NIGHT_DOCTOR  # 4 players → no detective

    other = next(p for p in game.alive_players() if p.player_id != victim.player_id)
    await _act(svc, game, joins, doctor, other)  # saves the wrong person
    assert game.phase is Phase.DAY
    assert not game.players[victim.player_id].alive
    # dawn narration revealed the character while eyes were closed
    assert len(game.story) >= 3


async def test_vote_majority_eliminates_and_night_falls(table) -> None:
    svc, created, conns, joins = table
    game = _game(svc, created)
    killer = _by_char(game, Character.KILLER)
    doctor = _by_char(game, Character.DOCTOR)
    victim = _by_char(game, Character.CITIZEN)
    await _act(svc, game, joins, killer, victim)
    await _act(svc, game, joins, doctor, doctor)  # self-save, kill lands on victim

    alive = game.alive_players()  # 3 alive → majority 2
    target = killer
    voters = [p for p in alive if p.player_id != target.player_id][:2]
    for voter in voters:
        resp = await svc._ws_vote_cast(
            None,
            {
                "id": "v",
                "game_id": game.game_id,
                "player_token": _token(game, joins, voter),
                "target": target.player_id,
            },
        )
        assert resp["type"] == "mafia.vote.cast.result"
    # killer voted out → citizens win, game ends
    assert game.winner == "citizens"
    assert game.phase is Phase.ENDED


async def test_host_end_day_without_majority(table) -> None:
    svc, created, conns, joins = table
    game = _game(svc, created)
    killer = _by_char(game, Character.KILLER)
    doctor = _by_char(game, Character.DOCTOR)
    victim = _by_char(game, Character.CITIZEN)
    await _act(svc, game, joins, killer, victim)
    await _act(svc, game, joins, doctor, victim)  # doctor saves the victim → nobody dies
    assert game.phase is Phase.DAY
    assert game.players[victim.player_id].alive
    resp = await svc._ws_host_end_day(conns["Cam"], {"id": "e", "game_id": game.game_id})
    assert resp["type"] == "mafia.host.end_day.result"
    assert game.phase is Phase.NIGHT_KILLERS
    assert game.night == 2


async def test_host_skip_forfeits_kill(table) -> None:
    svc, created, conns, joins = table
    game = _game(svc, created)
    resp = await svc._ws_host_skip(conns["Cam"], {"id": "k", "game_id": game.game_id})
    assert resp["type"] == "mafia.host.skip_phase.result"
    assert game.phase is not Phase.NIGHT_KILLERS  # advanced past killers


async def test_host_abort(table) -> None:
    svc, created, conns, joins = table
    game = _game(svc, created)
    resp = await svc._ws_host_abort(conns["Cam"], {"id": "q", "game_id": game.game_id})
    assert resp["type"] == "mafia.host.abort.result"
    assert game.winner == "aborted"
    assert created["game_id"] not in svc._games


async def test_secret_push_targeting(table) -> None:
    """The killer's awaiting flag reaches only the killer's connection."""
    svc, created, conns, joins = table
    game = _game(svc, created)
    killer = _by_char(game, Character.KILLER)
    for name, conn in conns.items():
        player = _player_named(game, name)
        states = [m["state"] for m in conn.sent if m.get("type") == "mafia.state"]
        assert states, f"{name} got no pushes"
        last = states[-1]
        assert last["you"]["player_id"] == player.player_id
        if player.player_id != killer.player_id:
            others = [p for p in last["players"] if p["player_id"] != player.player_id]
            assert all(p["character"] is None for p in others if p["alive"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest std-plugins/mafia/tests/test_service_flow.py -v`
Expected: FAIL — start handler returns the stub error

- [ ] **Step 3: Implement — replace the Task 9 stubs in `mafia/service.py`**

```python
    # --- game loop ---

    async def _ws_game_start(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        result = self._require_host(conn, frame)
        if isinstance(result, dict):
            return result
        game = result
        if game.phase is not Phase.LOBBY:
            return self._err(frame, "The game has already started")
        narrator = self._narrator()
        if game.theme_key == THEME_SURPRISE and not game.theme:
            game.theme = await narrator.invent_theme()
        try:
            game.assign_characters()
        except GameError as exc:
            return self._err(frame, str(exc))
        names = ", ".join(p.name for p in game.players.values())
        await narrator.cue(game, "intro", f"The inhabitants: {names}.")
        game.begin_night()
        await narrator.cue(
            game, "night", "Night falls. Killers, open your eyes and choose a victim."
        )
        self._push_state(game)
        self._restart_nudge(game)
        return {"type": "mafia.game.start.result", "ref": frame.get("id")}

    async def _ws_night_act(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        result = self._game_and_player(frame)
        if isinstance(result, dict):
            return result
        game, player = result
        target_id = str(frame.get("target_id", ""))
        extra: dict[str, Any] = {}
        try:
            if game.phase is Phase.NIGHT_KILLERS:
                outcome = game.killer_act(player.player_id, target_id)
                if outcome == "confirmed":
                    await self._advance_night(game)
            elif game.phase is Phase.NIGHT_DOCTOR:
                game.doctor_act(player.player_id, target_id)
                await self._advance_night(game)
            elif game.phase is Phase.NIGHT_DETECTIVE:
                extra["is_killer"] = game.detective_act(player.player_id, target_id)
                await self._advance_night(game)
            else:
                return self._err(frame, "There is nothing to do right now")
        except GameError as exc:
            return self._err(frame, str(exc))
        self._push_state(game)
        self._restart_nudge(game)
        return {"type": "mafia.night.act.result", "ref": frame.get("id"), **extra}

    async def _advance_night(self, game: MafiaGame) -> None:
        """Move to the next living special, or resolve the night at dawn."""
        narrator = self._narrator()
        if game.phase is Phase.NIGHT_KILLERS and game.alive_with(Character.DOCTOR):
            game.phase = Phase.NIGHT_DOCTOR
            await narrator.speak("Killers, close your eyes. Doctor, open yours — who will you save?")
            return
        if game.phase in (Phase.NIGHT_KILLERS, Phase.NIGHT_DOCTOR) and game.alive_with(
            Character.DETECTIVE
        ):
            game.phase = Phase.NIGHT_DETECTIVE
            await narrator.speak(
                "Close your eyes. Detective, open yours — whose secret will you learn?"
            )
            return
        await self._dawn(game)

    async def _dawn(self, game: MafiaGame) -> None:
        game.phase = Phase.DAWN
        narrator = self._narrator()
        victim = game.resolve_night()
        if victim is None:
            facts = "Nobody died last night — the town wakes relieved."
        else:
            facts = (
                f"{victim.name} was killed in the night. "
                f"They were the {victim.character}. Reveal this inside the story."
            )
        await narrator.cue(game, "dawn", facts)
        if await self._maybe_finish(game):
            return
        game.phase = Phase.DAY
        await narrator.speak("Everyone, open your eyes. Discuss — then vote on your phones.")

    async def _ws_vote_cast(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        result = self._game_and_player(frame)
        if isinstance(result, dict):
            return result
        game, player = result
        raw = frame.get("target")
        target = None if raw is None else str(raw)
        try:
            game.cast_vote(player.player_id, target)
        except GameError as exc:
            return self._err(frame, str(exc))
        chosen = game.majority_target()
        if chosen is not None:
            await self._dusk(game, eliminated=chosen)
        self._push_state(game)
        return {"type": "mafia.vote.cast.result", "ref": frame.get("id")}

    async def _dusk(self, game: MafiaGame, eliminated: Any | None) -> None:
        game.phase = Phase.DUSK
        narrator = self._narrator()
        await narrator.speak("The town has decided. Everyone, close your eyes.")
        if eliminated is not None:
            game.eliminate(eliminated.player_id)
            facts = (
                f"The town cast out {eliminated.name}. They were the "
                f"{eliminated.character}. Reveal this inside the story."
            )
        else:
            facts = "The town argued until sundown but could not agree. Nobody was cast out."
        await narrator.cue(game, "dusk", facts)
        if await self._maybe_finish(game):
            return
        game.begin_night()
        await narrator.speak("Night falls again. Killers, open your eyes.")
        self._restart_nudge(game)

    async def _maybe_finish(self, game: MafiaGame) -> bool:
        winner = game.check_winner()
        if not winner:
            return False
        game.winner = winner
        game.phase = Phase.ENDED
        narrator = self._narrator()
        killers = ", ".join(k.name for k in game.killers())
        if winner == "citizens":
            facts = f"The killers ({killers}) are all gone. The town survives."
        else:
            facts = f"The killers ({killers}) now hold the town. Darkness wins."
        await narrator.cue(game, "win", facts)
        self._cancel_nudge(game.game_id)
        self._push_state(game)
        return True

    # --- host powers ---

    async def _ws_host_skip(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        result = self._require_host(conn, frame)
        if isinstance(result, dict):
            return result
        game = result
        if game.phase not in (Phase.NIGHT_KILLERS, Phase.NIGHT_DOCTOR, Phase.NIGHT_DETECTIVE):
            return self._err(frame, "Nothing to skip right now")
        await self._advance_night(game)
        self._push_state(game)
        self._restart_nudge(game)
        return {"type": "mafia.host.skip_phase.result", "ref": frame.get("id")}

    async def _ws_host_end_day(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        result = self._require_host(conn, frame)
        if isinstance(result, dict):
            return result
        game = result
        if game.phase is not Phase.DAY:
            return self._err(frame, "It is not daytime")
        await self._dusk(game, eliminated=None)
        self._push_state(game)
        return {"type": "mafia.host.end_day.result", "ref": frame.get("id")}

    async def _ws_host_remove(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        result = self._require_host(conn, frame)
        if isinstance(result, dict):
            return result
        game = result
        player_id = str(frame.get("player_id", ""))
        player = game.players.get(player_id)
        if player is None or not player.alive:
            return self._err(frame, "No such living player", 404)
        game.eliminate(player_id)
        narrator = self._narrator()
        await narrator.cue(
            game,
            "dusk",
            f"{player.name} has left the story. They were the {player.character}.",
        )
        if not await self._maybe_finish(game):
            if game.phase in (Phase.NIGHT_KILLERS, Phase.NIGHT_DOCTOR, Phase.NIGHT_DETECTIVE):
                await self._advance_night(game)
        self._push_state(game)
        return {"type": "mafia.host.remove_player.result", "ref": frame.get("id")}

    async def _ws_host_abort(self, conn: Any, frame: dict[str, Any]) -> dict[str, Any]:
        result = self._require_host(conn, frame)
        if isinstance(result, dict):
            return result
        game = result
        game.winner = "aborted"
        game.phase = Phase.ENDED
        self._cancel_nudge(game.game_id)
        self._push_state(game)
        self._games.pop(game.game_id, None)
        self._conns.pop(game.game_id, None)
        return {"type": "mafia.host.abort.result", "ref": frame.get("id")}

    # --- nudges ---

    def _cancel_nudge(self, game_id: str) -> None:
        task = self._nudge_tasks.pop(game_id, None)
        if task is not None:
            task.cancel()

    def _restart_nudge(self, game: MafiaGame) -> None:
        self._cancel_nudge(game.game_id)
        if game.phase not in (Phase.NIGHT_KILLERS, Phase.NIGHT_DOCTOR, Phase.NIGHT_DETECTIVE):
            return
        self._nudge_tasks[game.game_id] = asyncio.get_running_loop().create_task(
            self._nudge_loop(game.game_id), context=contextvars.copy_context()
        )

    async def _nudge_loop(self, game_id: str) -> None:
        while True:
            await asyncio.sleep(self._nudge_seconds)
            game = self._games.get(game_id)
            if game is None or game.phase not in (
                Phase.NIGHT_KILLERS,
                Phase.NIGHT_DOCTOR,
                Phase.NIGHT_DETECTIVE,
            ):
                return
            try:
                await self._narrator().cue(
                    game, "nudge", "Someone in the dark is taking their time."
                )
            except Exception:
                logger.exception("Mafia nudge failed")
```

Cleanup in `_ws_host_abort` note: final push happens BEFORE popping the game so players see the aborted state.

- [ ] **Step 4: Run tests**

Run: `uv run pytest std-plugins/mafia/tests/ -v`
Expected: PASS

- [ ] **Step 5: Commit (std-plugins repo)**

```bash
cd std-plugins
git add mafia/service.py mafia/tests/test_service_flow.py
git commit -m "feat(mafia): full game loop — night/dawn/day/dusk, host powers, nudges"
cd ..
```

---

### Task 11: AI tool — `/mafia` entry point from chat

**Repo:** gilbert-plugins.

**Files:**
- Modify: `mafia/service.py`
- Test: `mafia/tests/test_tool.py` (create)

**Interfaces:**
- Produces: `ToolProvider` surface on `MafiaService` — `tool_provider_name` property returning `"mafia"`, `get_tools()` returning one `ToolDefinition(name="mafia_open", description=..., required_role="everyone", slash_command="open", slash_help="Open the Mafia party game", parallel_safe=True)`, and `execute_tool("mafia_open", args)` returning a markdown string linking `[Open Mafia](/mafia)` plus any joinable lobby codes (phone-plugin precedent: root-relative SPA links in tool text).

- [ ] **Step 1: Write the failing test**

`mafia/tests/test_tool.py`:

```python
from __future__ import annotations

from typing import Any

import pytest

from gilbert.interfaces.tools import ToolProvider
from gilbert_plugin_mafia.game import MafiaGame
from gilbert_plugin_mafia.service import MafiaService


@pytest.fixture
async def svc() -> MafiaService:
    service = MafiaService()
    service._config = {"enabled": True}
    await service.on_config_changed(service._config)
    service._enabled = True
    return service


def test_is_tool_provider(svc: MafiaService) -> None:
    assert isinstance(svc, ToolProvider)
    tools = svc.get_tools()
    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == "mafia_open"
    assert tool.required_role == "everyone"
    assert tool.slash_command == "open"
    assert tool.slash_help


async def test_execute_links_page_and_lobbies(svc: MafiaService) -> None:
    text = await svc.execute_tool("mafia_open", {})
    assert "[Open Mafia](/mafia)" in text
    game = MafiaGame(host_user_id="u1", host_name="Cam")
    svc._games[game.game_id] = game
    text = await svc.execute_tool("mafia_open", {})
    assert game.join_code in text


async def test_unknown_tool_raises(svc: MafiaService) -> None:
    with pytest.raises(KeyError):
        await svc.execute_tool("nope", {})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest std-plugins/mafia/tests/test_tool.py -v`
Expected: FAIL (`get_tools` missing)

- [ ] **Step 3: Implement — add to `MafiaService`**

```python
    # --- ToolProvider ---

    @property
    def tool_provider_name(self) -> str:
        return "mafia"

    def get_tools(self, user_ctx: Any = None) -> list[ToolDefinition]:
        if not self._enabled:
            return []
        return [
            ToolDefinition(
                name="mafia_open",
                description=(
                    "Point users at the Mafia party game page. Call when someone "
                    "wants to play Mafia (werewolf-style social deduction). The game "
                    "itself is played at /mafia on each player's phone — this tool "
                    "only returns the link and any open lobby join codes."
                ),
                parameters=[],
                required_role="everyone",
                slash_command="open",
                slash_help="Open the Mafia party game",
                parallel_safe=True,
            )
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name != "mafia_open":
            raise KeyError(name)
        lines = ["Gather everyone and open **[Open Mafia](/mafia)** on each phone."]
        lobbies = [g for g in self._games.values() if g.phase is Phase.LOBBY]
        for g in lobbies:
            lines.append(f"- {g.host_name}'s game is open — join code **{g.join_code}**")
        return "\n".join(lines)
```

Add `ToolDefinition` to the `gilbert.interfaces.tools` import in `service.py`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest std-plugins/mafia/tests/ -v`
Expected: PASS

- [ ] **Step 5: Commit (std-plugins repo)**

```bash
cd std-plugins
git add mafia/service.py mafia/tests/test_tool.py
git commit -m "feat(mafia): mafia_open tool — chat/slash entry point to /mafia"
cd ..
```

---

### Task 12: Frontend — `/mafia` page (join, lobby, play)

**Repo:** gilbert-plugins. No frontend test runner covers plugin dirs (vitest globs `frontend/src` only) — verification is `npm run build` type-checking plus the manual smoke in Task 13.

**Files (all under `mafia/frontend/`):**
- Create: `package.json`, `types.ts`, `api.ts`, `panels.ts`, `MafiaPage.tsx`, `components/JoinGate.tsx`, `components/Lobby.tsx`, `components/CharacterCard.tsx`, `components/NightAction.tsx`, `components/VotePanel.tsx`, `components/GhostPanel.tsx`, `components/HostControls.tsx`, `components/StoryLog.tsx`

**Interfaces:**
- Consumes: `useWebSocket()` → `{ subscribe(eventType, handler) => unsubscribe, rpc<T>(frame, timeout?) => Promise<T>, connected }`; frames exactly as produced by Tasks 9-10; `registerPanel` from `@/lib/plugin-panels`.
- Conventions: session stored in `localStorage` under `mafia.session` as `{gameId, playerId, token}`; per style guide — data as props, callbacks for actions, one component per file, components ≤200 lines, page ≤300 lines, explicit return types, JSDoc on exports.

- [ ] **Step 1: Scaffolding + types**

`mafia/frontend/package.json`:

```json
{
  "name": "@gilbert-plugin/mafia-frontend",
  "private": true,
  "version": "1.0.0",
  "description": "Mafia party game SPA page — registered via panels.ts into the host's plugin route system.",
  "type": "module",
  "peerDependencies": {
    "react": "^19.0.0",
    "react-dom": "^19.0.0",
    "react-router-dom": "^7.0.0",
    "lucide-react": "^1.0.0"
  }
}
```

`mafia/frontend/types.ts`:

```typescript
/** Wire types for the mafia.* WS protocol — must mirror game.state_for(). */

export type PhaseKey =
  | "lobby"
  | "night_killers"
  | "night_doctor"
  | "night_detective"
  | "dawn"
  | "day"
  | "dusk"
  | "ended";

export type CharacterKey = "citizen" | "killer" | "doctor" | "detective";

export interface PlayerSummary {
  player_id: string;
  name: string;
  alive: boolean;
  is_host: boolean;
  character: CharacterKey | null;
}

export interface CheckResult {
  player_id: string;
  name: string;
  is_killer: boolean;
}

export interface YouState {
  player_id: string;
  name: string;
  alive: boolean;
  is_host: boolean;
  character: CharacterKey | null;
  partner_name: string | null;
  awaiting: "kill" | "kill_confirm" | "save" | "check" | null;
  kill_proposal: { target_id: string; target_name: string } | null;
  check_results: CheckResult[];
  ghost: { characters: Record<string, CharacterKey> } | null;
}

export interface GameState {
  game_id: string;
  phase: PhaseKey;
  night: number;
  theme_key: string;
  join_code: string;
  players: PlayerSummary[];
  story: string[];
  votes: Record<string, string>;
  majority_needed: number;
  winner: "" | "citizens" | "killers" | "aborted";
  you: YouState;
}

export interface MafiaSession {
  gameId: string;
  playerId: string;
  token: string;
}

export interface ActiveGame {
  game_id: string;
  join_code: string;
  host_name: string;
  phase: PhaseKey;
  player_count: number;
}
```

`mafia/frontend/api.ts`:

```typescript
import { useMemo } from "react";

import { useWebSocket } from "@/hooks/useWebSocket";

import type { ActiveGame, GameState } from "./types";

interface CreateResult {
  game_id: string;
  join_code: string;
  player_id: string;
  player_token: string;
  state: GameState;
}

interface JoinResult extends CreateResult {}

/** Typed WS RPC bindings for the mafia plugin. */
export function useMafiaApi() {
  const { rpc } = useWebSocket();
  return useMemo(
    () => ({
      create: (themeKey: string, themeText?: string) =>
        rpc<CreateResult>({ type: "mafia.game.create", theme_key: themeKey, theme_text: themeText ?? "" }),
      join: (joinCode: string, name: string) =>
        rpc<JoinResult>({ type: "mafia.game.join", join_code: joinCode, name }),
      resume: (gameId: string, playerToken: string) =>
        rpc<{ state: GameState }>({ type: "mafia.game.resume", game_id: gameId, player_token: playerToken }),
      activeGames: () => rpc<{ games: ActiveGame[] }>({ type: "mafia.games.active" }),
      start: (gameId: string) =>
        rpc<Record<string, unknown>>({ type: "mafia.game.start", game_id: gameId }, 120_000),
      nightAct: (gameId: string, playerToken: string, targetId: string) =>
        rpc<{ is_killer?: boolean }>(
          { type: "mafia.night.act", game_id: gameId, player_token: playerToken, target_id: targetId },
          120_000,
        ),
      vote: (gameId: string, playerToken: string, target: string | null) =>
        rpc<Record<string, unknown>>(
          { type: "mafia.vote.cast", game_id: gameId, player_token: playerToken, target },
          120_000,
        ),
      hostSkip: (gameId: string) =>
        rpc<Record<string, unknown>>({ type: "mafia.host.skip_phase", game_id: gameId }, 120_000),
      hostEndDay: (gameId: string) =>
        rpc<Record<string, unknown>>({ type: "mafia.host.end_day", game_id: gameId }, 120_000),
      hostRemove: (gameId: string, playerId: string) =>
        rpc<Record<string, unknown>>(
          { type: "mafia.host.remove_player", game_id: gameId, player_id: playerId },
          120_000,
        ),
      hostAbort: (gameId: string) =>
        rpc<Record<string, unknown>>({ type: "mafia.host.abort", game_id: gameId }),
    }),
    [rpc],
  );
}
```

(120s timeouts: narration beats speak aloud inside the RPC.)

`mafia/frontend/panels.ts`:

```typescript
/**
 * Side-effect import: register the mafia plugin's full-page UI.
 * Panel id mirrors plugin.py MafiaPlugin.ui_routes() — mounted at /mafia.
 */

import { registerPanel } from "@/lib/plugin-panels";

import { MafiaPage } from "./MafiaPage";

registerPanel("mafia.page", MafiaPage);
```

- [ ] **Step 2: Components**

Each component receives data + callbacks as props (no fetching inside). Exact contracts:

`components/JoinGate.tsx` — props `{ activeGames: ActiveGame[]; canCreate: boolean; onJoin(code: string, name: string): void; onCreate(themeKey: string, themeText: string): void; error: string | null }`. Renders: join-code + name inputs with a Join button; when `canCreate`, a "New game" section with theme radio list (presets `camping/mansion/cruise/space/western` with labels, `custom` + text input, `surprise`); lists `activeGames` lobby codes as tap-to-fill. When `!canCreate`, show "Ask a signed-in member of the household to create the game."

`components/Lobby.tsx` — props `{ state: GameState; onStart(): void }`. Shows join code huge (players read it off the Host's phone or TV), roster with host badge, player count vs. minimum (4), Start button only for `state.you.is_host`, disabled below 4 players with the reason.

`components/CharacterCard.tsx` — props `{ you: YouState }`. Full-width card: your Character name + one-line duty (`killer` shows `partner_name` when present; `detective` lists `check_results`). Render nothing when `you.character` is null.

`components/NightAction.tsx` — props `{ state: GameState; onPick(targetId: string): void; busy: boolean }`. Reads `state.you.awaiting`: `"kill"`/`"save"`/`"check"` → prompt line + tap-list of living players (exclude self for check; exclude killers' partner names never shown — the server list already governs validity, so render all living non-self players and let server errors surface); `"kill_confirm"` → banner "Your partner picked {state.you.kill_proposal.target_name} — tap them to confirm" with only that player tappable; `null` → "Keep your eyes closed…" idle screen (dark background, minimal light).

`components/VotePanel.tsx` — props `{ state: GameState; onVote(target: string | null): void; busy: boolean }`. Live tally: for each living player a row with vote count bar and the names of their voters (open voting), tap to vote, tap again to clear (send `null`), an Abstain row, "majority = {state.majority_needed}" header. Your current vote highlighted.

`components/GhostPanel.tsx` — props `{ state: GameState }`. "👻 You are dead — enjoy the show" + full character table from `state.you.ghost.characters` joined with player names, and the story log.

`components/HostControls.tsx` — props `{ state: GameState; onSkip(): void; onEndDay(): void; onRemove(playerId: string): void; onAbort(): void }`. Collapsed drawer at the bottom, only rendered for `state.you.is_host`: Skip phase (night phases only), End day (day only), Remove player (select from living), Abort (with a confirm tap).

`components/StoryLog.tsx` — props `{ story: string[] }`. Scrollable narration transcript, newest last, auto-scroll.

Write complete implementations following the design language of `std-plugins/model-manager/frontend/ModelManagerPage.tsx` (Technical Broadsheet: dark, hairlines, one amber accent) — night screens near-black by design.

- [ ] **Step 3: The page**

`mafia/frontend/MafiaPage.tsx` (≤300 lines) — responsibilities only: session persistence, RPC wiring, subscription, phase routing:

```typescript
import { useCallback, useEffect, useMemo, useState } from "react";

import { useWebSocket } from "@/hooks/useWebSocket";

import { useMafiaApi } from "./api";
import { CharacterCard } from "./components/CharacterCard";
import { GhostPanel } from "./components/GhostPanel";
import { HostControls } from "./components/HostControls";
import { JoinGate } from "./components/JoinGate";
import { Lobby } from "./components/Lobby";
import { NightAction } from "./components/NightAction";
import { StoryLog } from "./components/StoryLog";
import { VotePanel } from "./components/VotePanel";
import type { ActiveGame, GameState, MafiaSession } from "./types";

const SESSION_KEY = "mafia.session";

function loadSession(): MafiaSession | null {
  try {
    const raw = localStorage.getItem(SESSION_KEY);
    return raw ? (JSON.parse(raw) as MafiaSession) : null;
  } catch {
    return null;
  }
}

/** The /mafia page: join gate → lobby → night/day play → ghost/finale. */
export function MafiaPage(): JSX.Element {
  const api = useMafiaApi();
  const { subscribe, connected } = useWebSocket();
  const [session, setSession] = useState<MafiaSession | null>(loadSession);
  const [state, setState] = useState<GameState | null>(null);
  const [activeGames, setActiveGames] = useState<ActiveGame[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // canCreate: probe once — guests get a 403 creating, so gate on /auth/me
  const [canCreate, setCanCreate] = useState(false);

  // live pushes
  useEffect(
    () =>
      subscribe("mafia.state", (frame: Record<string, unknown>) => {
        const s = frame.state as GameState;
        if (!session || frame.game_id === session.gameId) setState(s);
      }),
    [subscribe, session],
  );

  // resume or browse on connect
  useEffect(() => {
    if (!connected) return;
    void (async () => {
      if (session) {
        try {
          const { state: s } = await api.resume(session.gameId, session.token);
          setState(s);
          return;
        } catch {
          localStorage.removeItem(SESSION_KEY);
          setSession(null);
        }
      }
      const { games } = await api.activeGames();
      setActiveGames(games);
    })();
  }, [connected]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    void fetch("/auth/me")
      .then((r) => r.json())
      .then((u: { user_id?: string }) =>
        setCanCreate(Boolean(u.user_id) && u.user_id !== "guest" && u.user_id !== "system"),
      )
      .catch(() => setCanCreate(false));
  }, []);

  const adopt = useCallback((gameId: string, playerId: string, token: string, s: GameState) => {
    const next = { gameId, playerId, token };
    localStorage.setItem(SESSION_KEY, JSON.stringify(next));
    setSession(next);
    setState(s);
    setError(null);
  }, []);

  const run = useCallback(async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, []);

  const handlers = useMemo(
    () =>
      session && state
        ? {
            onStart: () => run(() => api.start(session.gameId)),
            onPick: (t: string) => run(() => api.nightAct(session.gameId, session.token, t)),
            onVote: (t: string | null) => run(() => api.vote(session.gameId, session.token, t)),
            onSkip: () => run(() => api.hostSkip(session.gameId)),
            onEndDay: () => run(() => api.hostEndDay(session.gameId)),
            onRemove: (p: string) => run(() => api.hostRemove(session.gameId, p)),
            onAbort: () =>
              run(async () => {
                await api.hostAbort(session.gameId);
                localStorage.removeItem(SESSION_KEY);
                setSession(null);
                setState(null);
              }),
          }
        : null,
    [api, run, session, state],
  );

  if (!session || !state || !handlers) {
    return (
      <JoinGate
        activeGames={activeGames}
        canCreate={canCreate}
        error={error}
        onJoin={(code, name) =>
          run(async () => {
            const r = await api.join(code, name);
            adopt(r.game_id, r.player_id, r.player_token, r.state);
          })
        }
        onCreate={(themeKey, themeText) =>
          run(async () => {
            const r = await api.create(themeKey, themeText);
            adopt(r.game_id, r.player_id, r.player_token, r.state);
          })
        }
      />
    );
  }

  const { you } = state;
  const isNight = state.phase.startsWith("night");
  return (
    <div className="mx-auto flex max-w-md flex-col gap-4 p-4">
      {error && <div className="text-sm text-red-400">{error}</div>}
      {state.phase === "lobby" && <Lobby state={state} onStart={handlers.onStart} />}
      {state.phase !== "lobby" && <CharacterCard you={you} />}
      {!you.alive || state.winner ? (
        <GhostPanel state={state} />
      ) : (
        <>
          {isNight && <NightAction state={state} onPick={handlers.onPick} busy={busy} />}
          {state.phase === "day" && (
            <VotePanel state={state} onVote={handlers.onVote} busy={busy} />
          )}
          {(state.phase === "dawn" || state.phase === "dusk") && (
            <p className="text-center text-sm opacity-70">Listen to the narrator…</p>
          )}
          <StoryLog story={state.story} />
        </>
      )}
      <HostControls
        state={state}
        onSkip={handlers.onSkip}
        onEndDay={handlers.onEndDay}
        onRemove={handlers.onRemove}
        onAbort={handlers.onAbort}
      />
    </div>
  );
}
```

(Adjust `JSX.Element` → `React.ReactElement` if the host tsconfig requires; mirror model-manager's exact style.)

- [ ] **Step 4: Verify it builds**

Run from gilbert repo root: `npm install && npm run build --workspace frontend` (or the repo's documented SPA build command — check root `package.json` scripts; `frontend/package.json` has the vite build).
Expected: type-checks and bundles; `import.meta.glob` picks up `std-plugins/mafia/frontend/panels.ts` automatically.

- [ ] **Step 5: Commit (std-plugins repo)**

```bash
cd std-plugins
git add mafia/frontend/
git commit -m "feat(mafia): /mafia SPA page — join gate, lobby, night actions, voting, ghost view"
cd ..
```

---

### Task 13: Docs, audit, end-to-end verification

**Repo:** both.

**Files:**
- Modify: `std-plugins/README.md` (plugin table row + full detail section)
- Verify: `std-plugins/CONTEXT.md` (already updated), `std-plugins/docs/adr/0011-mafia-players-ephemeral-not-users.md` (already written — commit it if not yet committed)
- Modify (gilbert repo): `README.md` only if it enumerates bundled plugins/games (grep first)

- [ ] **Step 1: README section**

Add to the `std-plugins/README.md` table: `| mafia | mafia_game | In-person Mafia party game narrated by Gilbert |` (match existing column format exactly). Add a detail section following the format of neighbors:

```markdown
### mafia

In-person social-deduction party game. Gilbert is the narrator: players gather in one room,
a signed-in user creates a game at `/mafia` and shares the join code; everyone else joins from
their phone with just a name (no account — see ADR-0011). Gilbert speaks the story aloud
(requires the TTS service to be enabled; uses room speakers or falls back to the host's
browser speaker), wakes the killers/doctor/detective at night for secret on-screen picks,
and runs the open day vote. Strict-majority vote-outs; killers win at parity.

**Provides:** `mafia_game` service (WS RPCs under `mafia.*`, guest-callable; `mafia_open` AI tool / `/mafia.open`).

**Requires enabled:** `text_to_speech`.

**Configure** (Settings → Games → Mafia): `enabled` (off by default), `narrator_prompt`
(AI prompt), `ai_profile`, `speakers` (empty = default announce speakers), `announce_volume`
(70), `nudge_seconds` (45), `max_concurrent_games` (2).

Guests must be allowed (`auth.allow_guests`, on by default for LAN visitors) for account-less
players to join.
```

- [ ] **Step 2: Full test + lint + type pass**

```bash
uv run pytest -x -q
uv run ruff check src/ tests/ std-plugins/mafia/
uv run mypy src/
```
Expected: all pass. Fix anything that doesn't.

- [ ] **Step 3: validate-architecture audit**

Run the `validate-architecture` skill in audit mode scoped to the diff (both repos): layer imports (plugin imports only `gilbert.interfaces.*`), hardcoded prompts (only `narrator_prompt` ConfigParam; `_DEFAULT_NARRATOR_PROMPT` referenced only as its default), multi-user isolation (`self._games` keyed by game_id is the accepted pattern; nudge tasks pass `context=`), slash namespace present, README freshness.

- [ ] **Step 4: Manual smoke (the `verify` step)**

Start Gilbert (`./gilbert.sh start`), enable the Mafia service in Settings → Games, then with 4 browser windows (1 signed-in + 3 incognito/guest): create with theme "camping", join 3 guests, start, walk one full night → dawn → vote → finale. Confirm: narration is audible (or logged when no speakers), guests never see live characters, killer flow proposes/confirms, host controls work, reload mid-game resumes via localStorage.

- [ ] **Step 5: Commit + report**

```bash
cd std-plugins
git add README.md docs/adr/0011-mafia-players-ephemeral-not-users.md CONTEXT.md
git commit -m "docs(mafia): README inventory, ADR-0011, Games glossary"
cd ..
git add docs/plans/2026-07-01-mafia-game.md
git commit -m "docs: mafia game implementation plan"
```

Then stop: both branches ready for PRs (`feat/mafia-game` in each repo, submodule PR first, core PR references it and bumps the submodule pointer after the plugin PR merges). Do NOT push or open PRs without the user's go-ahead.
