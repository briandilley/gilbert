# Browser Plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `std-plugins/browser/` plugin that gives Gilbert agents (and any other tool consumer) per-user headless Chrome access — navigation, DOM extraction, click/fill interaction, screenshots, AI-assisted page extraction — plus a credential manager UI and a VNC live-login flow for sites that demand interactive sign-in.

**Architecture:** A native Gilbert plugin built on Playwright. A `BrowserService` (registered through the plugin) owns one persistent `BrowserContext` per `(user_id, profile)` keyed by storage state on disk under `.gilbert/plugin-data/browser/users/<user_id>/`. The service implements `ToolProvider` (browser tools), `WsHandlerProvider` (credential CRUD + VNC session lifecycle), and `Configurable` (idle timeout, max contexts, login-form heuristics prompt). VNC live login spawns a separate **headed** Chromium under Xvfb + x11vnc + websockify, with the noVNC frontend served as a static asset under the existing web layer; once the user closes the session, the headed context's `storage_state` is exported and merged into the headless per-user context. Screenshots are persisted to the user's per-conversation workspace and returned as workspace-reference `FileAttachment` objects so they render inline in chat.

**Tech Stack:** Playwright (Python), Xvfb + x11vnc + websockify (host system packages, optional), noVNC client (vendored static), Fernet (cryptography lib, already a uv dep transitively or added) for at-rest credential encryption, existing Gilbert primitives (`ToolProvider`, `WsHandlerProvider`, `FileAttachment` workspace mode, `WorkspaceService.write_file_for_attachment`).

---

## File Structure

### Created

- `std-plugins/browser/plugin.yaml` — manifest, `provides: [browser]`, `requires: [workspace, configuration]`.
- `std-plugins/browser/plugin.py` — `BrowserPlugin.setup()` instantiates `BrowserService` and registers it.
- `std-plugins/browser/pyproject.toml` — declares `playwright>=1.45` (and `cryptography` if not transitively available).
- `std-plugins/browser/__init__.py` — empty package marker.
- `std-plugins/browser/browser_service.py` — `BrowserService(Service, ToolProvider, WsHandlerProvider, Configurable)`.
- `std-plugins/browser/context_pool.py` — `ContextPool` class managing per-user Playwright `BrowserContext` instances, idle eviction, persistent storage_state.
- `std-plugins/browser/tools.py` — tool descriptors and dispatchers for `browser_navigate`, `browser_get_text`, `browser_get_html`, `browser_click`, `browser_fill`, `browser_press`, `browser_select`, `browser_screenshot`, `browser_extract`, `browser_login`, `browser_close_tab`.
- `std-plugins/browser/credentials.py` — encrypted-at-rest credential store backed by an entity collection (`browser_credentials`). One row per `(user_id, site, username)`. AES-256-Fernet seal key derived from a per-installation secret in `.gilbert/config.yaml` (auto-generated on first run if missing).
- `std-plugins/browser/login_runner.py` — best-effort form auto-fill: given a credential row and a target URL, navigate to a configured login URL, fill username/password fields by selector or by AI-assisted heuristic, submit, return success boolean.
- `std-plugins/browser/vnc.py` — VNC session manager: spawns Xvfb (`:N`), x11vnc, websockify on free ports; launches a **headed** Chromium pointed at Xvfb DISPLAY; tracks `(session_id, user_id, port)` tuples; exports storage_state on close and tears down the stack.
- `std-plugins/browser/static/novnc/` — vendored noVNC client (subset: `vnc.html`, `core/*.js`, `app/*.js`).
- `std-plugins/browser/tests/conftest.py` — pytest plugin loader (clone of `tesseract/tests/conftest.py`).
- `std-plugins/browser/tests/test_credentials.py` — encryption round-trip, per-user isolation, list/delete.
- `std-plugins/browser/tests/test_context_pool.py` — get-or-create, idle eviction, storage_state persistence (uses `tmp_path`).
- `std-plugins/browser/tests/test_login_runner.py` — known-shape login forms, fallback when selectors miss.
- `std-plugins/browser/tests/test_vnc_session.py` — port allocation, lifecycle (subprocess fakes; no real Xvfb required).
- `std-plugins/browser/tests/test_tools.py` — tool dispatch with a Playwright `Page` fake; screenshot returns a workspace `FileAttachment`.
- `frontend/src/components/settings/BrowserCredentialsPanel.tsx` — list/add/edit/delete site credentials.
- `frontend/src/components/settings/BrowserVncSessionDialog.tsx` — modal with embedded noVNC iframe and a "Save & close" button.
- `frontend/src/types/browser.ts` — `BrowserCredential`, `BrowserVncSession`.
- `frontend/src/hooks/useWsApi.ts` — extended with browser RPCs.

### Modified

- `frontend/src/components/settings/SettingsPage.tsx` — add "Browser" section linking the credentials panel.
- `src/gilbert/web/web_server.py` (or whichever module owns static asset routes) — serve `std-plugins/browser/static/novnc/` at `/api/browser/novnc/<file>` so the modal can load the noVNC client; add a websockify proxy route `/api/browser/vnc/<session_id>/ws` that pipes to `127.0.0.1:<allocated_port>` after auth-checking the session against the calling user.
- `std-plugins/README.md` — add a `browser` row to the table and a full "Browser" detail section (deps, config keys, RBAC, VNC requirements).
- `std-plugins/CLAUDE.md` — no edits expected (pattern doc); update only if a new plugin idiom comes out of this work.

---

## Phase 0: Verification & Discovery (parallelizable, read-only)

These confirm the assumptions baked into Phases 1–6. Findings are written to `docs/superpowers/specs/2026-05-04-browser-plugin-verification.md`.

### Task 0: Create findings doc scaffold

**Files:**
- Create: `docs/superpowers/specs/2026-05-04-browser-plugin-verification.md`

- [ ] **Step 1: Write skeleton with one heading per Task 0.x finding**

```markdown
# Browser Plugin Verification Findings

## 0.1 Workspace attachment lifecycle for tool-produced files
TBD

## 0.2 Per-user UserContext propagation into ToolProvider
TBD

## 0.3 ConfigParam options for binary toggles + `restart_required` on Service plugins
TBD

## 0.4 Static-asset serving from a std-plugin directory
TBD

## 0.5 Encrypted-at-rest patterns already in core
TBD

## 0.6 Existing capability protocol candidates for the credential store
TBD

## 0.7 Playwright headless requirements (system packages, browser binary)
TBD
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-05-04-browser-plugin-verification.md
git commit -m "docs: scaffold browser-plugin verification findings"
```

### Task 0.1: Workspace attachment lifecycle

**Files:**
- Read: `src/gilbert/core/services/workspace.py` (esp. `_tool_attach_workspace_file`, `write_file_for_attachment` if present)
- Read: `src/gilbert/interfaces/attachments.py`
- Read: `src/gilbert/interfaces/tools.py` (`ToolResult.attachments`)
- Modify: `docs/superpowers/specs/2026-05-04-browser-plugin-verification.md` (replace TBD under 0.1)

- [ ] **Step 1: Document the exact public method (or pattern) a non-core tool uses to write a file into a per-conversation workspace and produce a `FileAttachment` of `kind="image"`**

Capture: the exact method name and signature, whether it requires an existing conversation id (and where in tool arguments it lives), whether it auto-registers the file in the file registry, and the exact `FileAttachment` field shape (workspace_skill, workspace_path, workspace_conv, workspace_file_id) we should produce. If no public helper exists and we must replicate the inlined logic, note that explicitly — this drives whether `browser_screenshot` calls a workspace API or copies its own bytes into the right path.

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-05-04-browser-plugin-verification.md
git commit -m "docs(browser): finding 0.1 workspace attachment lifecycle"
```

### Task 0.2: UserContext propagation into ToolProvider

**Files:**
- Read: `src/gilbert/interfaces/tools.py`
- Read: `src/gilbert/core/services/ai.py` (search `execute_tool`, `UserContext`)
- Read: `src/gilbert/core/services/agent.py` (how the agent threads its UserContext through chat)
- Modify: `docs/superpowers/specs/2026-05-04-browser-plugin-verification.md` (replace TBD under 0.2)

- [ ] **Step 1: Document how a `ToolProvider.execute_tool` learns which user invoked it**

Specifically: whether `arguments` already carries `_user_id` / `_conversation_id`, whether the AIService injects user context into tool calls, and whether there's a `UserContext` parameter or thread-local. The finding decides how `BrowserService` keys its per-user `BrowserContext` cache. Cite the exact lines.

- [ ] **Step 2: Commit**

### Task 0.3: ConfigParam options + restart semantics

**Files:**
- Read: `src/gilbert/interfaces/configuration.py` (`ConfigParam`, `ToolParameterType`)
- Read: an existing service-level Configurable: `src/gilbert/core/services/notifications.py` or `agent.py`
- Modify: `docs/superpowers/specs/2026-05-04-browser-plugin-verification.md` (replace TBD under 0.3)

- [ ] **Step 1: Confirm ConfigParam supports BOOLEAN type, integer ranges, multiline+ai_prompt, and that `restart_required=True` triggers a service restart on change**

Note any missing types we need (e.g., dropdown for "Browser engine"). For Service-level `Configurable` (vs `Backend`), confirm the right base class hooks (`config_namespace`, `config_category`, `config_params()`, `on_config_changed()`).

- [ ] **Step 2: Commit**

### Task 0.4: Static-asset serving from std-plugins

**Files:**
- Read: `src/gilbert/web/web_server.py` (or whichever module mounts SPA assets)
- Read: `src/gilbert/interfaces/web.py` if it exists
- Modify: `docs/superpowers/specs/2026-05-04-browser-plugin-verification.md` (replace TBD under 0.4)

- [ ] **Step 1: Document how to serve files from a std-plugin directory (the noVNC client) and how to add a custom HTTP/WS route for the websockify proxy**

Identify: (a) is there a per-plugin "static dir" hook, or do we need to add a new web route that does `FileResponse` against a path inside the plugin? (b) is there a websocket-proxy primitive we can reuse, or does the route need to do its own `httpx`/`aiohttp` ws-tunnel? (c) where does auth gating happen for these routes — `acl.py` declarations + middleware, or per-route guards? Capture exact paths and decorators.

- [ ] **Step 2: Commit**

### Task 0.5: Encrypted-at-rest patterns

**Files:**
- `grep -rn "Fernet\|cryptography\|encrypt" /home/assistant/gilbert/src/gilbert/`
- Read: `src/gilbert/core/services/configuration.py` (`sensitive` masking, secret storage)
- Modify: `docs/superpowers/specs/2026-05-04-browser-plugin-verification.md` (replace TBD under 0.5)

- [ ] **Step 1: Document whether Gilbert already has a symmetric secret it could reuse for browser credential encryption, or whether the plugin must mint its own**

If reuse: cite the exact secret-loading code (`.gilbert/config.yaml` key name, generation-on-first-run path). If not: document the proposed shape — generate a Fernet key on first plugin start, store it under the bootstrap config (NOT in entity storage, NOT in tracked files), reuse on subsequent starts.

- [ ] **Step 2: Commit**

### Task 0.6: Capability protocol for credentials

**Files:**
- Read: `src/gilbert/interfaces/credentials.py`
- `grep -rn "CredentialStore\|credential.*Reader\|capability=\"credentials\"" /home/assistant/gilbert/src/gilbert/`
- Modify: `docs/superpowers/specs/2026-05-04-browser-plugin-verification.md` (replace TBD under 0.6)

- [ ] **Step 1: Determine whether the credential store should be a generalised `CredentialStoreService` or remain plugin-internal**

`presence.py` and `doorbell.py` both `requires` a `credentials` capability. Find the producer; document its interface. If a generalised store already exists, the browser plugin should consume it instead of standing up `credentials.py`. If no producer exists, scope a minimal in-plugin store (per CLAUDE.md "shared data lives in interfaces/" rule, only hoist the credential entity model into `interfaces/` if a *second* consumer is imminent — otherwise keep the encryption + entity collection plugin-local).

- [ ] **Step 2: Commit**

### Task 0.7: Playwright host requirements

**Files:**
- Modify: `docs/superpowers/specs/2026-05-04-browser-plugin-verification.md` (replace TBD under 0.7)

- [ ] **Step 1: Document the host-side install steps and platform constraints**

Capture: (a) `playwright install chromium` is required after `uv sync` to fetch the actual browser binary, and how we surface that requirement (gilbert.sh, README, runtime warning); (b) on Linux the host needs `libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2` for headless; the VNC headed path additionally needs `xvfb x11vnc websockify`. Decide whether we ship a `gilbert.sh browser-doctor` subcommand in this plan or just document.

- [ ] **Step 2: Commit**

---

## Phase 1: Plugin Skeleton + Headless Browser Pool

End state: `BrowserService` registered, per-user persistent `BrowserContext` keyed by `user_id`, idle eviction, no tools yet. Tests cover the pool.

### Task 1: Plugin scaffolding

**Files:**
- Create: `std-plugins/browser/__init__.py`
- Create: `std-plugins/browser/plugin.yaml`
- Create: `std-plugins/browser/plugin.py`
- Create: `std-plugins/browser/pyproject.toml`
- Create: `std-plugins/browser/tests/conftest.py`

- [ ] **Step 1: Write `plugin.yaml`**

```yaml
name: browser
version: "1.0.0"
description: "Headless Chrome browser access for AI tools — navigate, click, screenshot, login flows, VNC live login"

provides:
  - browser

requires: []

depends_on: []
```

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[project]
name = "gilbert-plugin-browser"
version = "1.0.0"
description = "Headless Chrome browser tools for Gilbert"
requires-python = ">=3.12"
dependencies = [
    "playwright>=1.45",
    # Fernet for at-rest credential encryption. cryptography is already
    # pulled in transitively by Anthropic SDK / httpx, but we list it
    # explicitly so the resolver pins a version we test against.
    "cryptography>=42",
]

[tool.uv]
package = false
```

- [ ] **Step 3: Write `plugin.py` (skeleton, no service yet)**

```python
"""Browser plugin — registers the BrowserService."""

from __future__ import annotations

from gilbert.interfaces.plugin import Plugin, PluginContext, PluginMeta


class BrowserPlugin(Plugin):
    def metadata(self) -> PluginMeta:
        return PluginMeta(
            name="browser",
            version="1.0.0",
            description="Headless Chrome browser tools for Gilbert",
            provides=["browser"],
            requires=[],
        )

    async def setup(self, context: PluginContext) -> None:
        from .browser_service import BrowserService

        service = BrowserService(
            data_dir=context.data_dir,
            storage=context.storage,
        )
        context.services.register(service)
        self._service = service

    async def teardown(self) -> None:
        if hasattr(self, "_service"):
            await self._service.stop()


def create_plugin() -> Plugin:
    return BrowserPlugin()
```

- [ ] **Step 4: Write `tests/conftest.py` (clone of tesseract's)**

Read `std-plugins/tesseract/tests/conftest.py` first and copy verbatim with `gilbert_plugin_tesseract` → `gilbert_plugin_browser`.

- [ ] **Step 5: Commit**

```bash
git add std-plugins/browser/
git commit -m "browser: scaffold plugin (plugin.yaml, pyproject, plugin.py, conftest)"
```

### Task 2: ContextPool — failing test

**Files:**
- Create: `std-plugins/browser/tests/test_context_pool.py`

- [ ] **Step 1: Write the failing test**

```python
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from gilbert_plugin_browser.context_pool import ContextPool


@pytest.mark.asyncio
async def test_pool_creates_one_context_per_user(tmp_path: Path):
    playwright = MagicMock()
    chromium = MagicMock()
    browser = AsyncMock()
    context_a = AsyncMock()
    context_b = AsyncMock()
    browser.new_context.side_effect = [context_a, context_b]
    chromium.launch = AsyncMock(return_value=browser)
    playwright.chromium = chromium

    pool = ContextPool(
        data_dir=tmp_path,
        playwright=playwright,
        idle_timeout_seconds=60,
    )
    await pool.start()

    ctx_a1 = await pool.get_for_user("user-a")
    ctx_a2 = await pool.get_for_user("user-a")
    ctx_b = await pool.get_for_user("user-b")

    assert ctx_a1 is ctx_a2
    assert ctx_a1 is not ctx_b
    assert browser.new_context.call_count == 2

    await pool.stop()
```

- [ ] **Step 2: Run — expect ImportError**

```bash
uv run pytest std-plugins/browser/tests/test_context_pool.py::test_pool_creates_one_context_per_user -v
```

Expected: ImportError on `gilbert_plugin_browser.context_pool`.

- [ ] **Step 3: Commit the failing test**

### Task 3: ContextPool — minimal pass

**Files:**
- Create: `std-plugins/browser/context_pool.py`

- [ ] **Step 1: Implement just enough for the test**

```python
"""Per-user Playwright BrowserContext pool with idle eviction.

One BrowserContext per (user_id) keyed map. Each user's storage_state
is persisted under ``<data_dir>/users/<user_id>/state.json`` so cookies
and localStorage survive plugin restarts. Inactive contexts are closed
after ``idle_timeout_seconds`` of no use.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class _Entry:
    context: Any
    last_used: float = field(default_factory=time.monotonic)


class ContextPool:
    def __init__(
        self,
        data_dir: Path,
        playwright: Any,
        idle_timeout_seconds: int = 600,
    ) -> None:
        self._data_dir = data_dir
        self._pw = playwright
        self._idle_timeout = idle_timeout_seconds
        self._browser: Any | None = None
        self._entries: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task[None] | None = None
        self._stopped = False

    async def start(self) -> None:
        self._browser = await self._pw.chromium.launch(headless=True)

    async def stop(self) -> None:
        self._stopped = True
        if self._reaper is not None:
            self._reaper.cancel()
        for entry in self._entries.values():
            await self._save_state(entry)
            await entry.context.close()
        self._entries.clear()
        if self._browser is not None:
            await self._browser.close()
            self._browser = None

    async def get_for_user(self, user_id: str) -> Any:
        async with self._lock:
            entry = self._entries.get(user_id)
            if entry is None:
                entry = _Entry(context=await self._create_context(user_id))
                self._entries[user_id] = entry
            entry.last_used = time.monotonic()
            return entry.context

    async def _create_context(self, user_id: str) -> Any:
        state_path = self._user_state_path(user_id)
        kwargs: dict[str, Any] = {}
        if state_path.exists():
            kwargs["storage_state"] = str(state_path)
        return await self._browser.new_context(**kwargs)

    async def _save_state(self, entry: _Entry) -> None:
        # Best-effort persistence; errors logged, not raised.
        try:
            await entry.context.storage_state(path=str(self._user_state_path_for(entry)))
        except Exception:
            logger.exception("Failed to persist storage_state")

    def _user_state_path(self, user_id: str) -> Path:
        p = self._data_dir / "users" / user_id
        p.mkdir(parents=True, exist_ok=True)
        return p / "state.json"

    def _user_state_path_for(self, entry: _Entry) -> Path:
        for uid, e in self._entries.items():
            if e is entry:
                return self._user_state_path(uid)
        raise RuntimeError("entry not in pool")
```

- [ ] **Step 2: Run — expect PASS**

- [ ] **Step 3: Commit**

### Task 4: ContextPool — idle eviction

**Files:**
- Modify: `std-plugins/browser/tests/test_context_pool.py`
- Modify: `std-plugins/browser/context_pool.py`

- [ ] **Step 1: Add failing eviction test**

```python
@pytest.mark.asyncio
async def test_pool_evicts_idle_contexts(tmp_path: Path, monkeypatch):
    fake_now = [1000.0]
    monkeypatch.setattr("time.monotonic", lambda: fake_now[0])

    playwright = MagicMock()
    browser = AsyncMock()
    context = AsyncMock()
    browser.new_context = AsyncMock(return_value=context)
    playwright.chromium.launch = AsyncMock(return_value=browser)

    pool = ContextPool(tmp_path, playwright, idle_timeout_seconds=60)
    await pool.start()
    await pool.get_for_user("u")
    fake_now[0] += 120  # past idle window

    await pool._reap_once()

    assert "u" not in pool._entries
    context.close.assert_awaited()
```

- [ ] **Step 2: Run — expect FAIL (`_reap_once` not defined)**

- [ ] **Step 3: Add `_reap_once` and the periodic task in `start()`**

```python
async def start(self) -> None:
    self._browser = await self._pw.chromium.launch(headless=True)
    self._reaper = asyncio.create_task(self._reap_loop())

async def _reap_loop(self) -> None:
    while not self._stopped:
        await asyncio.sleep(self._idle_timeout / 4 or 15)
        await self._reap_once()

async def _reap_once(self) -> None:
    cutoff = time.monotonic() - self._idle_timeout
    async with self._lock:
        stale = [uid for uid, e in self._entries.items() if e.last_used < cutoff]
        for uid in stale:
            entry = self._entries.pop(uid)
            await self._save_state_for(uid, entry)
            try:
                await entry.context.close()
            except Exception:
                logger.exception("close on idle eviction failed")

async def _save_state_for(self, user_id: str, entry: _Entry) -> None:
    try:
        await entry.context.storage_state(path=str(self._user_state_path(user_id)))
    except Exception:
        logger.exception("Failed to persist storage_state for user %s", user_id)
```

(Replace `_save_state` / `_user_state_path_for` with the cleaner `_save_state_for`. Update `stop()` to use it.)

- [ ] **Step 4: Run both tests — expect PASS**

- [ ] **Step 5: Commit**

### Task 5: BrowserService skeleton

**Files:**
- Create: `std-plugins/browser/browser_service.py`
- Modify: `std-plugins/browser/plugin.py` (already imports the service — just verify import path resolves)

- [ ] **Step 1: Write the failing test**

`std-plugins/browser/tests/test_service_lifecycle.py`:

```python
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from gilbert_plugin_browser.browser_service import BrowserService


@pytest.mark.asyncio
async def test_service_starts_and_stops(tmp_path: Path):
    storage = MagicMock()
    svc = BrowserService(data_dir=tmp_path, storage=storage)

    fake_pw = MagicMock()
    fake_pw.chromium.launch = AsyncMock()
    fake_pw_cm = AsyncMock()
    fake_pw_cm.start = AsyncMock(return_value=fake_pw)
    fake_pw_cm.stop = AsyncMock()

    with patch("gilbert_plugin_browser.browser_service.async_playwright",
               return_value=fake_pw_cm):
        resolver = MagicMock()
        resolver.optional_capability.return_value = None
        await svc.start(resolver)
        await svc.stop()

    fake_pw_cm.start.assert_awaited()
    fake_pw_cm.stop.assert_awaited()
```

- [ ] **Step 2: Run — expect ImportError, then add the service**

```python
"""BrowserService — Gilbert service that owns a Playwright instance and a
per-user BrowserContext pool, exposes browser tools, and serves the VNC
live-login flow."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver

from .context_pool import ContextPool

logger = logging.getLogger(__name__)


class BrowserService(Service):
    slash_namespace = "browser"

    def __init__(self, *, data_dir: Path, storage: Any) -> None:
        self._data_dir = data_dir
        self._storage = storage
        self._pw_cm: Any | None = None
        self._pw: Any | None = None
        self._pool: ContextPool | None = None

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="browser",
            capabilities=frozenset({"browser"}),
            requires=frozenset(),
            optional=frozenset({"workspace", "configuration", "credentials"}),
            ai_calls=(),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        self._pw_cm = async_playwright()
        self._pw = await self._pw_cm.start()
        self._pool = ContextPool(
            data_dir=self._data_dir,
            playwright=self._pw,
            idle_timeout_seconds=600,
        )
        await self._pool.start()

    async def stop(self) -> None:
        if self._pool is not None:
            await self._pool.stop()
            self._pool = None
        if self._pw_cm is not None:
            await self._pw_cm.stop()
            self._pw_cm = None
            self._pw = None
```

- [ ] **Step 3: Run — expect PASS**

- [ ] **Step 4: Commit**

---

## Phase 2: Read-Only Browser Tools

End state: agents can navigate, scrape text/HTML, take screenshots that render in chat.

### Task 6: `browser_navigate` — failing test

**Files:**
- Create: `std-plugins/browser/tests/test_tools.py`

- [ ] **Step 1: Write the failing test**

```python
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from gilbert_plugin_browser.browser_service import BrowserService


@pytest.mark.asyncio
async def test_browser_navigate_returns_title_and_url(tmp_path: Path):
    svc = BrowserService(data_dir=tmp_path, storage=MagicMock())

    page = AsyncMock()
    page.title = AsyncMock(return_value="Example Domain")
    page.url = "https://example.com/"
    page.goto = AsyncMock()
    context = AsyncMock()
    context.new_page = AsyncMock(return_value=page)
    pool = AsyncMock()
    pool.get_for_user = AsyncMock(return_value=context)
    svc._pool = pool

    result = await svc.execute_tool(
        "browser_navigate",
        {"url": "https://example.com/", "_user_id": "u1"},
    )
    page.goto.assert_awaited_with("https://example.com/", wait_until="load")
    assert "Example Domain" in result.content
    assert "https://example.com/" in result.content
```

- [ ] **Step 2: Run — expect AttributeError on `execute_tool`**

- [ ] **Step 3: Implement `browser_navigate` in `BrowserService`**

Add `tool_provider_name = "browser"`, `get_tools()` returning `_NAVIGATE_TOOL`, and `execute_tool` dispatching by name. The first tool reuses a single `Page` per (user, "default tab"); track it in `self._pages: dict[str, Any]`.

```python
from gilbert.interfaces.tools import (
    Tool,
    ToolParameter,
    ToolParameterType,
    ToolResult,
)

_NAVIGATE_TOOL = Tool(
    name="browser_navigate",
    description="Navigate the browser to a URL. Returns the resolved URL and page title.",
    parameters=(
        ToolParameter(
            name="url",
            type=ToolParameterType.STRING,
            description="Absolute URL to navigate to.",
            required=True,
        ),
    ),
)
```

`execute_tool`:

```python
async def execute_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
    user_id = str(arguments.get("_user_id") or "")
    if not user_id:
        return ToolResult(tool_call_id="", content="error: missing user context")
    if name == "browser_navigate":
        return await self._tool_navigate(user_id, arguments)
    return ToolResult(tool_call_id="", content=f"unknown tool: {name}")

async def _tool_navigate(self, user_id: str, args: dict[str, Any]) -> ToolResult:
    url = str(args.get("url", "")).strip()
    if not url:
        return ToolResult(tool_call_id="", content="error: url required")
    page = await self._get_or_create_page(user_id)
    await page.goto(url, wait_until="load")
    title = await page.title()
    return ToolResult(
        tool_call_id="",
        content=f"Loaded {page.url} — {title}",
    )

async def _get_or_create_page(self, user_id: str):
    page = self._pages.get(user_id)
    if page is None:
        ctx = await self._pool.get_for_user(user_id)
        page = await ctx.new_page()
        self._pages[user_id] = page
    return page
```

Initialize `self._pages: dict[str, Any] = {}` in `__init__`.

- [ ] **Step 4: Run — expect PASS**

- [ ] **Step 5: Commit**

### Task 7: `browser_get_text` and `browser_get_html`

**Files:**
- Modify: `std-plugins/browser/browser_service.py`
- Modify: `std-plugins/browser/tests/test_tools.py`

- [ ] **Step 1: Add failing tests for text and html extraction (with truncation)**

```python
@pytest.mark.asyncio
async def test_browser_get_text_strips_whitespace(tmp_path: Path):
    svc = _service_with_page(tmp_path, body_text="  hello  \n\n  world  ")
    out = await svc.execute_tool("browser_get_text", {"_user_id": "u"})
    assert "hello" in out.content
    assert "world" in out.content
    # Whitespace collapsed but content order preserved.
    assert out.content.index("hello") < out.content.index("world")
```

(`_service_with_page` is a small helper at the top of the test file that returns a service with a fake page whose `inner_text` returns `body_text`.)

- [ ] **Step 2: Implement both tools**

- `browser_get_text` — `await page.locator("body").inner_text()`, collapse whitespace runs, truncate to 50_000 chars with a `…[truncated]` marker.
- `browser_get_html` — `await page.content()`, truncate to 200_000 chars.

Add a CSS-selector-scoped variant: optional `selector` argument; when set, scope to first matching element instead of `body`.

- [ ] **Step 3: Run — expect PASS**

- [ ] **Step 4: Commit**

### Task 8: `browser_screenshot` returning `FileAttachment`

**Files:**
- Modify: `std-plugins/browser/browser_service.py`
- Modify: `std-plugins/browser/tests/test_tools.py`

The exact persistence path comes from finding 0.1. The test below assumes a `WorkspaceWriter`-style capability with `write_attachment(user_id, conv_id, name, bytes, media_type)` returning a `FileAttachment` — adjust to match what 0.1 documents.

- [ ] **Step 1: Failing test**

```python
@pytest.mark.asyncio
async def test_browser_screenshot_returns_image_attachment(tmp_path: Path):
    workspace = AsyncMock()
    fake_attachment = FileAttachment(
        kind="image",
        name="screenshot.png",
        media_type="image/png",
        workspace_skill="workspace",
        workspace_path="ai/screenshot.png",
        workspace_conv="conv-1",
    )
    workspace.write_attachment = AsyncMock(return_value=fake_attachment)

    svc = _service_with_page(tmp_path)
    svc._workspace = workspace
    svc._pages["u"].screenshot = AsyncMock(return_value=b"\x89PNG...")

    result = await svc.execute_tool(
        "browser_screenshot",
        {"_user_id": "u", "_conversation_id": "conv-1"},
    )

    assert len(result.attachments) == 1
    assert result.attachments[0].kind == "image"
    workspace.write_attachment.assert_awaited()
```

- [ ] **Step 2: Implement using the workspace API documented in 0.1**

Resolve the workspace capability in `start()` via `resolver.optional_capability("workspace")` and store on `self._workspace`. In `_tool_screenshot`:

```python
async def _tool_screenshot(self, user_id: str, args: dict[str, Any]) -> ToolResult:
    if self._workspace is None:
        return ToolResult(tool_call_id="", content="error: workspace service unavailable")
    conv_id = str(args.get("_conversation_id") or "")
    full_page = bool(args.get("full_page", False))
    page = await self._get_or_create_page(user_id)
    png = await page.screenshot(full_page=full_page, type="png")
    name = f"browser-{int(time.time())}.png"
    attachment = await self._workspace.write_attachment(
        user_id=user_id,
        conv_id=conv_id,
        name=name,
        data=png,
        media_type="image/png",
        kind="image",
    )
    return ToolResult(
        tool_call_id="",
        content=f"Captured screenshot ({len(png)} bytes). The user will see it inline.",
        attachments=(attachment,),
    )
```

If finding 0.1 says no public helper exists, the implementation copies the inlined logic from `workspace.py` instead of calling `write_attachment` — see 0.1 for the exact field shape.

- [ ] **Step 3: Run — expect PASS**

- [ ] **Step 4: End-to-end smoke test (manual)**

Run Gilbert, ask the agent: *"Open https://example.com and take a screenshot."* Confirm the chat renders the image. Document any gaps in the verification doc.

- [ ] **Step 5: Commit**

---

## Phase 3: Interaction Tools

### Task 9: `browser_click`, `browser_fill`, `browser_press`, `browser_select`

**Files:**
- Modify: `std-plugins/browser/browser_service.py`
- Modify: `std-plugins/browser/tests/test_tools.py`

- [ ] **Step 1: Failing tests for each (use `page.locator(selector).click()` etc.)**

Cover at minimum:
- `browser_click` — calls `page.locator(selector).click(timeout=15000)`
- `browser_fill` — calls `page.locator(selector).fill(value)`
- `browser_press` — calls `page.keyboard.press(key)`
- `browser_select` — calls `page.locator(selector).select_option(value)`

Each tool returns a brief confirmation in `ToolResult.content`. On Playwright `TimeoutError`, return `error: selector not found within 15s`.

- [ ] **Step 2: Implement**

- [ ] **Step 3: Run — expect PASS**

- [ ] **Step 4: Commit**

### Task 10: Tool group declarations + RBAC

**Files:**
- Modify: `std-plugins/browser/browser_service.py`
- Modify: `src/gilbert/interfaces/acl.py` (declare `browser.` namespace at user level if not already implicit)

- [ ] **Step 1: Mark interaction tools as serial-only**

Read `interfaces/tools.py` for the parallel-safety annotation pattern (it's a class-level attribute or per-Tool flag — verify with finding 0.3's spirit). Group:

- Read-only (parallel-safe): `browser_navigate`, `browser_get_text`, `browser_get_html`, `browser_screenshot`.
- Write/destructive (serial-only): `browser_click`, `browser_fill`, `browser_press`, `browser_select`, `browser_close_tab`.

The constraint exists because they all share one `Page` per user — concurrent clicks would race. If `interfaces/tools.py` doesn't expose a parallel-safety knob, document the gap in the verification doc and serialize internally with an `asyncio.Lock` per user.

- [ ] **Step 2: RBAC declaration**

Confirm `interfaces/acl.py` puts the `browser.` tool namespace at user level (default 100). Privileged sub-tools added in Phase 4 (credential management, VNC sessions) need higher levels — declare them explicitly when introduced.

- [ ] **Step 3: Commit**

---

## Phase 4: Credential Manager

### Task 11: Credential entity + encryption

**Files:**
- Create: `std-plugins/browser/credentials.py`
- Create: `std-plugins/browser/tests/test_credentials.py`

- [ ] **Step 1: Failing test — round-trip and per-user isolation**

```python
import pytest
from pathlib import Path

from gilbert_plugin_browser.credentials import CredentialStore, BrowserCredential


@pytest.mark.asyncio
async def test_credential_round_trip(tmp_path: Path, fake_storage):
    store = CredentialStore(storage=fake_storage, key_path=tmp_path / "key")
    await store.start()
    cred = BrowserCredential(
        user_id="u1",
        site="example.com",
        label="Main account",
        username="alice",
        password="s3cret",
        login_url="https://example.com/login",
    )
    saved = await store.save(cred)
    loaded = await store.get(saved.id, "u1")
    assert loaded.username == "alice"
    assert loaded.password == "s3cret"


@pytest.mark.asyncio
async def test_users_cannot_read_each_others_credentials(tmp_path, fake_storage):
    store = CredentialStore(storage=fake_storage, key_path=tmp_path / "key")
    await store.start()
    saved = await store.save(BrowserCredential(
        user_id="u1", site="x", label="", username="a", password="p", login_url="",
    ))
    with pytest.raises(PermissionError):
        await store.get(saved.id, "u2")
```

(`fake_storage` is a fixture — minimal in-memory entity store the tests reuse; copy from another std-plugin's tests if one exists.)

- [ ] **Step 2: Implement `CredentialStore`**

```python
"""Encrypted-at-rest browser credential store.

One row per (user_id, site, username) under entity collection
``browser_credentials``. Username and password fields are sealed with a
Fernet key kept at ``<plugin_data>/key`` (mode 600). The key is generated
on first start if absent.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet


@dataclass
class BrowserCredential:
    user_id: str
    site: str
    label: str
    username: str
    password: str
    login_url: str
    id: str = ""
    username_selector: str = ""
    password_selector: str = ""
    submit_selector: str = ""


class CredentialStore:
    def __init__(self, storage: Any, key_path: Path) -> None:
        self._storage = storage
        self._key_path = key_path
        self._fernet: Fernet | None = None

    async def start(self) -> None:
        if self._key_path.exists():
            key = self._key_path.read_bytes()
        else:
            key = Fernet.generate_key()
            self._key_path.parent.mkdir(parents=True, exist_ok=True)
            self._key_path.write_bytes(key)
            os.chmod(self._key_path, 0o600)
        self._fernet = Fernet(key)

    async def save(self, cred: BrowserCredential) -> BrowserCredential:
        assert self._fernet is not None
        row_id = cred.id or secrets.token_urlsafe(12)
        row = {
            "_id": row_id,
            "user_id": cred.user_id,
            "site": cred.site,
            "label": cred.label,
            "login_url": cred.login_url,
            "username_selector": cred.username_selector,
            "password_selector": cred.password_selector,
            "submit_selector": cred.submit_selector,
            "username_enc": self._fernet.encrypt(cred.username.encode()).decode(),
            "password_enc": self._fernet.encrypt(cred.password.encode()).decode(),
        }
        await self._storage.upsert("browser_credentials", row)
        cred.id = row_id
        return cred

    async def get(self, cred_id: str, user_id: str) -> BrowserCredential:
        assert self._fernet is not None
        row = await self._storage.get_one("browser_credentials", {"_id": cred_id})
        if row is None:
            raise KeyError(cred_id)
        if row["user_id"] != user_id:
            raise PermissionError("not your credential")
        return BrowserCredential(
            id=row["_id"],
            user_id=row["user_id"],
            site=row["site"],
            label=row.get("label", ""),
            username=self._fernet.decrypt(row["username_enc"]).decode(),
            password=self._fernet.decrypt(row["password_enc"]).decode(),
            login_url=row.get("login_url", ""),
            username_selector=row.get("username_selector", ""),
            password_selector=row.get("password_selector", ""),
            submit_selector=row.get("submit_selector", ""),
        )

    async def list_for_user(self, user_id: str) -> list[BrowserCredential]:
        rows = await self._storage.find("browser_credentials", {"user_id": user_id})
        return [
            BrowserCredential(
                id=r["_id"],
                user_id=r["user_id"],
                site=r["site"],
                label=r.get("label", ""),
                username=self._fernet.decrypt(r["username_enc"]).decode(),
                # Passwords are never returned to the UI list endpoint —
                # only the per-id `get` reveals them, and even then we
                # gate it via WS RPC on user-level RBAC. The internal
                # `_tool_login` resolves credentials on the server side.
                password="",
                login_url=r.get("login_url", ""),
            )
            for r in rows
        ]

    async def delete(self, cred_id: str, user_id: str) -> None:
        cred = await self.get(cred_id, user_id)  # auth check
        await self._storage.delete("browser_credentials", {"_id": cred.id})
```

(If finding 0.6 says a generic `CredentialStoreService` already exists, replace this whole file with thin call-throughs to that capability. The shape above is the fallback.)

- [ ] **Step 3: Run — expect PASS**

- [ ] **Step 4: Wire `CredentialStore` into `BrowserService.start()`**

```python
self._creds = CredentialStore(
    storage=self._storage,
    key_path=self._data_dir / "fernet.key",
)
await self._creds.start()
```

- [ ] **Step 5: Commit**

### Task 12: WS RPCs for credentials

**Files:**
- Modify: `std-plugins/browser/browser_service.py` (`WsHandlerProvider`)
- Create: `std-plugins/browser/tests/test_ws_credentials.py`

- [ ] **Step 1: Failing test for `browser.credentials.list` / `.save` / `.delete`**

Each RPC takes the calling `user_id` from the websocket connection, not from arguments. Save returns the new id; list omits the password; delete is idempotent.

- [ ] **Step 2: Implement `ws_handlers()`**

```python
def ws_handlers(self) -> list[WsHandler]:
    return [
        WsHandler("browser.credentials.list", self._ws_list_credentials),
        WsHandler("browser.credentials.save", self._ws_save_credential),
        WsHandler("browser.credentials.delete", self._ws_delete_credential),
    ]
```

Each handler validates the user, calls `self._creds`, and returns `{"ok": True, ...}`.

- [ ] **Step 3: Run — expect PASS**

- [ ] **Step 4: Commit**

### Task 13: Settings UI — credentials list

**Files:**
- Create: `frontend/src/types/browser.ts`
- Create: `frontend/src/components/settings/BrowserCredentialsPanel.tsx`
- Modify: `frontend/src/hooks/useWsApi.ts`
- Modify: `frontend/src/components/settings/SettingsPage.tsx`

- [ ] **Step 1: Types**

```typescript
export type BrowserCredential = {
  id: string;
  site: string;
  label: string;
  username: string;
  login_url: string;
};

export type BrowserVncSession = {
  id: string;
  vnc_url: string;
  expires_at: string;
};
```

- [ ] **Step 2: WS API hooks**

```typescript
listBrowserCredentials: () => callRpc<{ ok: boolean; credentials: BrowserCredential[] }>("browser.credentials.list", {}),
saveBrowserCredential: (cred: Partial<BrowserCredential> & { password?: string }) =>
  callRpc<{ ok: boolean; id: string }>("browser.credentials.save", cred),
deleteBrowserCredential: (id: string) =>
  callRpc<{ ok: boolean }>("browser.credentials.delete", { id }),
```

- [ ] **Step 3: `BrowserCredentialsPanel` — table with rows (label, site, username, edit, delete) + "Add credential" button → `BrowserCredentialDialog` (label, site, login URL, username, password — inputs only, no JSON paste anywhere)**

Use the existing shadcn `Dialog`/`Input`/`Button` primitives — match the look of `AuthorPromptDialog.tsx`.

- [ ] **Step 4: Add a "Browser" section in `SettingsPage.tsx` that mounts the panel**

- [ ] **Step 5: Manual smoke — open Settings → Browser, add a credential, see it in the list, edit, delete**

- [ ] **Step 6: Commit**

### Task 14: `browser_login` tool

**Files:**
- Create: `std-plugins/browser/login_runner.py`
- Modify: `std-plugins/browser/browser_service.py`
- Modify: `std-plugins/browser/tests/test_tools.py`
- Create: `std-plugins/browser/tests/test_login_runner.py`

- [ ] **Step 1: Failing tests**

```python
@pytest.mark.asyncio
async def test_login_runner_fills_known_selectors(monkeypatch):
    page = AsyncMock()
    runner = LoginRunner(page)
    await runner.run(BrowserCredential(
        user_id="u", site="x", label="", username="alice", password="p",
        login_url="https://x.test/login",
        username_selector="#email",
        password_selector="#password",
        submit_selector="#submit",
    ))
    page.goto.assert_awaited_with("https://x.test/login", wait_until="load")
    page.locator.assert_any_call("#email")
    page.locator.assert_any_call("#password")
```

- [ ] **Step 2: Implement `LoginRunner`**

When all three selectors are set, do the obvious thing: navigate, fill, click submit, wait for navigation. When selectors are blank, fall back to common heuristics (`input[type=email]`, `input[type=password]`, `button[type=submit]`). If still nothing matches, return a "no login form detected — use VNC" error string.

- [ ] **Step 3: Tool: `browser_login`**

Tool args: `credential_id`. Tool body: load credential (user-scoped), open page, run `LoginRunner`, return success or descriptive error. The tool deliberately does not accept username/password as inline arguments — only saved credentials may be used.

- [ ] **Step 4: Commit**

---

## Phase 5: VNC Live Login

The single most fragile phase. End state: user clicks "Log in interactively" on a credential row, modal opens with embedded noVNC, user logs into the site, closes the modal, cookies are saved into their headless context.

### Task 15: VNC session manager

**Files:**
- Create: `std-plugins/browser/vnc.py`
- Create: `std-plugins/browser/tests/test_vnc_session.py`

- [ ] **Step 1: Failing test (subprocess fakes)**

```python
@pytest.mark.asyncio
async def test_vnc_session_allocates_ports_and_starts_processes(tmp_path, monkeypatch):
    started: list[list[str]] = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        started.append(list(args))
        proc = MagicMock()
        proc.terminate = MagicMock()
        proc.wait = AsyncMock()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        fake_create_subprocess_exec)

    mgr = VncSessionManager(data_dir=tmp_path)
    session = await mgr.start_session(user_id="u", target_url="https://x.test/login")
    try:
        assert session.session_id
        assert session.websockify_port > 0
        # Xvfb, x11vnc, websockify, Chromium each spawned exactly once.
        cmd_names = [s[0].split("/")[-1] for s in started]
        assert "Xvfb" in cmd_names
        assert "x11vnc" in cmd_names
        assert "websockify" in cmd_names
        assert any(name in cmd_names for name in ("chromium", "google-chrome"))
    finally:
        await mgr.stop_session(session.session_id)
```

- [ ] **Step 2: Implement `VncSessionManager`**

Single-class state machine: allocate a free X display via `:N` scan + lockfile, allocate a free TCP port for x11vnc and another for websockify, spawn Xvfb, then x11vnc bound to that display, then websockify bridging to x11vnc's port, then headed Chromium with `DISPLAY=:N`. Save `session_id → (proc handles, port, user_id, started_at, headed_context_path)`. `stop_session` exports storage_state from the headed context, terminates the four processes in reverse order, and merges the exported state into the user's headless storage_state.

The merge step: load both JSONs, append cookies (dedup by `(name, domain, path)`, prefer the headed copy on conflict), union localStorage origins, write to the headless path. Document the exact merge rules in the verification doc.

Lifecycle guards: max 2 concurrent VNC sessions per user, max 5 server-wide, idle timeout 15 minutes — all configurable via `ConfigParam`.

- [ ] **Step 3: Run — expect PASS**

- [ ] **Step 4: Commit**

### Task 16: WS RPCs + websocket proxy route

**Files:**
- Modify: `std-plugins/browser/browser_service.py`
- Modify: `src/gilbert/web/web_server.py` (or wherever 0.4 says custom routes go)

- [ ] **Step 1: WS RPCs**

- `browser.vnc.start` — body `{credential_id?: string, target_url?: string}` → `{ok, session_id, vnc_url}`. The `vnc_url` points at `/api/browser/vnc/<session_id>/ws` for the noVNC client to connect to.
- `browser.vnc.stop` — body `{session_id}` → `{ok}`. Server-side this triggers the storage_state export + merge.
- `browser.vnc.list` — list active sessions for the calling user.

- [ ] **Step 2: HTTP route serving the noVNC client**

`GET /api/browser/novnc/<path:filename>` → `FileResponse` rooted at `std-plugins/browser/static/novnc/`. Read 0.4 for the existing static-mount pattern.

- [ ] **Step 3: Websocket proxy route**

`/api/browser/vnc/<session_id>/ws`: authenticate the calling user against the session record, then proxy bytes to/from `127.0.0.1:<websockify_port>`. The proxy code lives in the web layer (it needs the WS framework) but consults the BrowserService via a capability check for session ownership.

If finding 0.4 says we don't have a websocket-proxy primitive, the simplest implementation is a hand-rolled `aiohttp.WSMsgType` loop that opens a raw TCP socket to localhost:port and pipes both directions until either closes.

- [ ] **Step 4: Commit**

### Task 17: VNC dialog UI

**Files:**
- Create: `frontend/src/components/settings/BrowserVncSessionDialog.tsx`
- Modify: `frontend/src/components/settings/BrowserCredentialsPanel.tsx` (add "Log in via VNC" action per row)

- [ ] **Step 1: Dialog implementation**

When opened with `{credential?, targetUrl?}`:
1. Calls `browser.vnc.start` to get `session_id` and `vnc_url`.
2. Renders the noVNC client in an `<iframe src={`/api/browser/novnc/vnc.html?path=${encodeURIComponent(vnc_url)}`}/>` taking the full dialog body.
3. Shows a "Done" button that calls `browser.vnc.stop({session_id})` and closes the dialog.
4. On unmount or page navigation, sends `browser.vnc.stop` as a best-effort cleanup.

Sizing: at least 1024×768 inside the iframe, with a confirmation banner *"This is a real browser running on the server. Don't enter passwords for sites you don't own."*

- [ ] **Step 2: Wire up the row action**

Each credential row gets a "Log in interactively" button that opens the dialog with that credential. Successful close shows a toast and triggers a re-render so the UI can show "Last logged in: just now" if we later add that column.

- [ ] **Step 3: Manual smoke — log into a real site (e.g., a test account on github.com), confirm cookies persist by then asking the agent to fetch the logged-in page**

- [ ] **Step 4: Commit**

---

## Phase 6: AI-Assisted Extraction (optional polish)

### Task 18: `browser_extract`

**Files:**
- Modify: `std-plugins/browser/browser_service.py`
- Modify: `std-plugins/browser/tests/test_tools.py`

- [ ] **Step 1: Failing test with a fake `ai_chat` capability**

The tool takes `instruction` and `json_schema`, grabs the rendered `inner_text` (body), constructs a one-shot AI call with system prompt = the user-configurable `extraction_prompt` ConfigParam (default: a sensible instruction-following template), user message = `instruction + "\n\n" + body[:30_000]`, expects the model to return JSON matching the schema. Validate, return JSON or error string.

- [ ] **Step 2: Implement**

Add `ai_chat` to `optional` capabilities. Skip the tool registration entirely when no AI service is available. Mark the tool serial-only (it shares the page).

The system prompt MUST be a `ConfigParam(multiline=True, ai_prompt=True)` per CLAUDE.md.

- [ ] **Step 3: Commit**

---

## Phase 7: Configuration, Docs, Memory

### Task 19: ConfigParams

**Files:**
- Modify: `std-plugins/browser/browser_service.py`

- [ ] **Step 1: Implement `Configurable`**

```python
def config_params(cls) -> list[ConfigParam]:
    return [
        ConfigParam(key="idle_timeout_seconds", type=ToolParameterType.INTEGER, default=600,
                    description="Close a user's browser context after this many idle seconds."),
        ConfigParam(key="max_concurrent_users", type=ToolParameterType.INTEGER, default=8,
                    description="Hard cap on simultaneous browser contexts (server-wide)."),
        ConfigParam(key="vnc_idle_timeout_seconds", type=ToolParameterType.INTEGER, default=900),
        ConfigParam(key="vnc_max_concurrent_per_user", type=ToolParameterType.INTEGER, default=2),
        ConfigParam(key="vnc_max_concurrent_total", type=ToolParameterType.INTEGER, default=5),
        ConfigParam(key="extraction_prompt", type=ToolParameterType.STRING,
                    multiline=True, ai_prompt=True,
                    default=_DEFAULT_EXTRACTION_PROMPT),
        ConfigParam(key="login_form_heuristics_prompt", type=ToolParameterType.STRING,
                    multiline=True, ai_prompt=True,
                    default=_DEFAULT_LOGIN_HEURISTICS_PROMPT),
    ]
```

- [ ] **Step 2: `on_config_changed` cached on `self._foo`** per CLAUDE.md.

- [ ] **Step 3: Commit**

### Task 20: README + memory

**Files:**
- Modify: `std-plugins/README.md`
- Create: `.claude/memory/memory-browser-plugin.md` (in std-plugins repo) and add a line to `.claude/memory/MEMORIES.md`

- [ ] **Step 1: README — add a `browser` row to the plugin table**

Columns: name, provides, third-party deps (`playwright>=1.45`, `cryptography>=42`), OS deps (`xvfb`, `x11vnc`, `websockify` for VNC live login; Chromium browser binary via `playwright install chromium`).

- [ ] **Step 2: README — full "Browser" section**

Cover: tool list (read-only vs interaction vs login), credential manager UI location, VNC live-login instructions, RBAC defaults, ConfigParam list with defaults, post-`uv sync` step `uv run playwright install chromium`, how the agent automatically uses these tools.

- [ ] **Step 3: Memory file — gotchas captured**

Specifically: per-page-per-user serialization, storage_state merge rules between headed VNC session and headless context, why the credential password column never round-trips through the UI, where the Fernet key lives.

- [ ] **Step 4: Commit**

### Task 21: `gilbert.sh` browser-doctor (optional based on 0.7)

**Files:**
- Modify: `gilbert.sh` (only if 0.7 says we should)

- [ ] **Step 1: Add `browser-doctor` subcommand**

Checks: `playwright --version`, `chromium` browser presence (`playwright install chromium --dry-run` parsing or equivalent), `Xvfb`/`x11vnc`/`websockify` on PATH. Print PASS/FAIL per line and exit non-zero on any FAIL. Document in README.

- [ ] **Step 2: Commit**

---

## Final Validation

- [ ] `uv run pytest std-plugins/browser/tests/ -v` — all green.
- [ ] `uv run mypy src/` — no new errors.
- [ ] `uv run ruff check std-plugins/browser/` — clean.
- [ ] Manual end-to-end:
  - [ ] Agent: *"Open https://example.com, take a screenshot, then read the headline."* — image renders inline, headline returned as text.
  - [ ] Settings → Browser → add a credential for a test site — round-trip save/edit/delete works, password never appears in any list response.
  - [ ] Settings → Browser → "Log in interactively" — modal opens, noVNC loads, can interact with the embedded Chromium, log into a real site, close → cookies persist (verified by asking the agent to fetch the logged-in page).
  - [ ] `browser_login` with a saved credential against a known-shape login page — succeeds.
  - [ ] Idle timeout: leave for 11 minutes, confirm the user's context was closed (log line); next tool call recreates from saved storage_state.
- [ ] README and memory in sync with shipped behavior.
