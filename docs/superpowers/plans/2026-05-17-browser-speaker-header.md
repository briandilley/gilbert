# Browser Speaker Header Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the browser-speaker affordance from per-conversation chat bubbles + user pref to a global header control that doubles as opt-in registration. Add cross-user listing/targeting under role-based access control, magic AI-tool aliases for "my browser," and a client-side replay history.

**Architecture:** Per-user activation registration on `BrowserSpeakerBackend` (server) gates listability; `SpeakerService` wires new WS RPCs (`browser_speaker.activate` / `deactivate`), filters listings by role, and rejects cross-user targets for non-admins. Frontend uses a single context-backed hook (`useBrowserSpeaker`) that owns localStorage state, WS activation sync, and a singleton `<audio>` element. Chat-page audio bubbles are removed; all browser-speaker UI lives in a new `<BrowserSpeakerControl />` header component.

**Tech Stack:** Python 3.12+, uv, pytest, async (asyncio); React + TypeScript frontend; WS RPC over the existing `gilbert.web.ws_protocol` plumbing. All commits land on `feature/browser_speaker_backend` (PR #16) for a final squash-merge.

**Companion spec:** `docs/superpowers/specs/2026-05-17-browser-speaker-header-design.md` (commit `2088b5e`).

---

## File Structure

**Created:**
- `frontend/src/hooks/useBrowserSpeaker.tsx` — REPLACES the existing file; new shape (global store + history + activation sync).
- `frontend/src/components/layout/BrowserSpeakerControl.tsx` — new header component.
- `tests/unit/test_browser_speaker_activation.py` — backend activation tests.
- `tests/unit/test_speaker_service_browser_permissions.py` — role filter + cross-user gate tests.
- `tests/unit/test_resolve_speaker_name_my_browser.py` — magic alias tests.

**Modified:**
- `src/gilbert/integrations/browser_speaker.py` — activation state, `activate`/`deactivate`, multi-user `list_speakers`, echo gate now reads activation state.
- `src/gilbert/core/services/speaker.py` — new WS handlers, role-aware `list_speakers` filter, `_check_browser_target_permissions`, `_is_admin` helper, magic aliases in `resolve_speaker_name`, removal of `speaker.browser_echo` pref logic.
- `src/gilbert/core/services/users.py` — remove `speaker.browser_echo` ConfigParam if present.
- `frontend/src/components/layout/TopBar.tsx` — slot `<BrowserSpeakerControl />` into the right cluster.
- `frontend/src/components/chat/MessageList.tsx` — delete import + render of `BrowserAudioBubbles`.

**Deleted:**
- `frontend/src/hooks/useBrowserEchoPref.tsx`
- `frontend/src/components/chat/BrowserAudioBubbles.tsx`

**Tests updated:**
- `tests/unit/test_speaker_browser_echo.py` — adjust setup (no more pref mock; use activation state).
- `tests/unit/test_browser_speaker.py` — adjust `list_speakers` assertions for multi-user.
- `tests/unit/test_user_service.py` — remove any `speaker.browser_echo` pref assertions.

---

## Phase 1 — Server: BrowserSpeakerBackend activation tracking

### Task 1: Activation state + multi-user `list_speakers` on `BrowserSpeakerBackend`

**Files:**
- Modify: `src/gilbert/integrations/browser_speaker.py`
- Create: `tests/unit/test_browser_speaker_activation.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_browser_speaker_activation.py`:

```python
"""Tests for BrowserSpeakerBackend activation tracking."""
import pytest

from gilbert.integrations.browser_speaker import BrowserSpeakerBackend
from gilbert.interfaces.speaker import SpeakerInfo


@pytest.fixture
async def backend() -> BrowserSpeakerBackend:
    b = BrowserSpeakerBackend()
    await b.initialize({})
    return b


def test_activate_registers_connection_for_user(backend: BrowserSpeakerBackend) -> None:
    backend.activate(conn_id="c1", user_id="alice", display_name="Alice")
    assert "alice" in backend._active_connections
    assert "c1" in backend._active_connections["alice"]
    assert backend._active_connections["alice"]["c1"] == "Alice"
    assert backend._conn_to_user["c1"] == "alice"


def test_deactivate_removes_connection(backend: BrowserSpeakerBackend) -> None:
    backend.activate(conn_id="c1", user_id="alice", display_name="Alice")
    backend.deactivate(conn_id="c1")
    assert "alice" not in backend._active_connections
    assert "c1" not in backend._conn_to_user


def test_deactivate_unknown_conn_is_noop(backend: BrowserSpeakerBackend) -> None:
    backend.deactivate(conn_id="never-registered")
    assert backend._active_connections == {}


def test_activate_idempotent_on_repeated_calls(backend: BrowserSpeakerBackend) -> None:
    backend.activate(conn_id="c1", user_id="alice", display_name="Alice")
    backend.activate(conn_id="c1", user_id="alice", display_name="Alice")
    assert len(backend._active_connections["alice"]) == 1


def test_multiple_conns_per_user(backend: BrowserSpeakerBackend) -> None:
    backend.activate(conn_id="c1", user_id="alice", display_name="Alice")
    backend.activate(conn_id="c2", user_id="alice", display_name="Alice")
    backend.deactivate(conn_id="c1")
    # Still active because of c2
    assert "alice" in backend._active_connections
    assert set(backend._active_connections["alice"]) == {"c2"}


@pytest.mark.asyncio
async def test_list_speakers_empty_when_no_active_connections(backend: BrowserSpeakerBackend) -> None:
    assert await backend.list_speakers() == []


@pytest.mark.asyncio
async def test_list_speakers_returns_entry_per_active_user(backend: BrowserSpeakerBackend) -> None:
    backend.activate(conn_id="c1", user_id="alice", display_name="Alice")
    backend.activate(conn_id="c2", user_id="bob", display_name="Bob")
    result = await backend.list_speakers()
    by_id = {s.speaker_id: s for s in result}
    assert set(by_id) == {"alice", "bob"}
    assert by_id["alice"].name == "Alice's Browser"
    assert by_id["bob"].name == "Bob's Browser"


@pytest.mark.asyncio
async def test_list_speakers_drops_user_when_last_conn_deactivates(backend: BrowserSpeakerBackend) -> None:
    backend.activate(conn_id="c1", user_id="alice", display_name="Alice")
    backend.deactivate(conn_id="c1")
    assert await backend.list_speakers() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_browser_speaker_activation.py -v`

Expected: every test FAILS with `AttributeError` (no `activate` / `deactivate` / `_active_connections`).

- [ ] **Step 3: Add activation state + methods to `BrowserSpeakerBackend`**

In `src/gilbert/integrations/browser_speaker.py`:

Add to `__init__` (or wherever instance state is initialized):

```python
self._active_connections: dict[str, dict[str, str]] = {}
# user_id -> {conn_id: display_name_when_registered}

self._conn_to_user: dict[str, str] = {}
# conn_id -> user_id (reverse lookup for disconnect)
```

Add methods:

```python
def activate(self, *, conn_id: str, user_id: str, display_name: str) -> None:
    """Register a connection as an active browser-speaker for a user.

    Idempotent. Calling with the same ``conn_id`` twice is a no-op.
    """
    self._active_connections.setdefault(user_id, {})[conn_id] = display_name
    self._conn_to_user[conn_id] = user_id

def deactivate(self, *, conn_id: str) -> None:
    """Unregister a connection. No-op if conn_id is unknown."""
    user_id = self._conn_to_user.pop(conn_id, None)
    if user_id is None:
        return
    conns = self._active_connections.get(user_id)
    if conns is None:
        return
    conns.pop(conn_id, None)
    if not conns:
        self._active_connections.pop(user_id, None)
```

- [ ] **Step 4: Replace existing `list_speakers` with multi-user version**

Find the existing `list_speakers` (which today reads `get_current_user()` and returns one entry). Replace with:

```python
async def list_speakers(self) -> list[SpeakerInfo]:
    """Return one ``SpeakerInfo`` per user with at least one active connection.

    Role-based filtering happens upstream in ``SpeakerService.list_speakers``.
    """
    out: list[SpeakerInfo] = []
    for user_id, conns in self._active_connections.items():
        if not conns:
            continue
        display_name = next(iter(conns.values()))
        out.append(SpeakerInfo(
            speaker_id=user_id,
            name=f"{display_name}'s Browser",
            ip_address="",
        ))
    return out
```

Delete any `from gilbert.interfaces.context import get_current_user` if it's no longer used here.

- [ ] **Step 5: Verify tests pass**

Run: `uv run pytest tests/unit/test_browser_speaker_activation.py -v`

Expected: 8 PASS.

- [ ] **Step 6: Run the broader unit suite**

Run: `uv run pytest tests/unit/ -x -q`

Expected: PASS. **If `test_browser_speaker.py` or `test_speaker_browser_echo.py` fail** because they expected the old per-current-user `list_speakers` shape, leave them failing for now — Tasks 2/3 will adjust them. If the failures are unrelated, investigate.

For now, *only commit if `test_browser_speaker_activation.py` passes*. If the broader suite has pre-existing failures from this change in shape, that's expected mid-refactor — note them in the commit message.

- [ ] **Step 7: Commit**

```bash
git add src/gilbert/integrations/browser_speaker.py tests/unit/test_browser_speaker_activation.py
git commit -m "browser_speaker: per-user activation tracking + multi-user list_speakers"
```

---

## Phase 2 — Server: SpeakerService wiring

### Task 2: WS RPCs `browser_speaker.activate` / `deactivate` + disconnect cleanup

**Files:**
- Modify: `src/gilbert/core/services/speaker.py`
- Create / Modify: `tests/unit/test_speaker_service_browser_permissions.py` (start fresh here; subsequent tasks extend it)

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_speaker_service_browser_permissions.py`:

```python
"""Tests for SpeakerService browser RPCs, role filter, and permissions."""
from __future__ import annotations

import pytest
from typing import Any
from unittest.mock import MagicMock

from gilbert.core.services.speaker import SpeakerService
from gilbert.integrations.browser_speaker import BrowserSpeakerBackend
from gilbert.interfaces.auth import UserContext


def _make_admin() -> UserContext:
    return UserContext(user_id="admin1", display_name="Admin", email="", roles=frozenset({"admin"}))


def _make_user(uid: str = "alice") -> UserContext:
    return UserContext(user_id=uid, display_name=uid.title(), email="", roles=frozenset({"user"}))


@pytest.fixture
async def svc_with_browser_backend() -> SpeakerService:
    svc = SpeakerService()
    backend = BrowserSpeakerBackend()
    await backend.initialize({})
    svc._backends = {"browser": backend}
    return svc


# --- WS RPC handlers ---

@pytest.mark.asyncio
async def test_ws_activate_registers_connection_on_browser_backend(
    svc_with_browser_backend: SpeakerService,
) -> None:
    svc = svc_with_browser_backend
    conn = MagicMock()
    conn.connection_id = "c1"
    conn.user_id = "alice"
    conn.display_name = "Alice"
    close_callbacks: list[Any] = []
    conn.add_close_callback.side_effect = close_callbacks.append

    result = await svc._ws_browser_speaker_activate(conn, {})

    assert result == {"status": "ok"}
    backend = svc._backends["browser"]
    assert "alice" in backend._active_connections
    assert "c1" in backend._active_connections["alice"]
    # The handler registered a disconnect-cleanup callback
    assert len(close_callbacks) == 1


@pytest.mark.asyncio
async def test_ws_deactivate_removes_connection(
    svc_with_browser_backend: SpeakerService,
) -> None:
    svc = svc_with_browser_backend
    backend = svc._backends["browser"]
    backend.activate(conn_id="c1", user_id="alice", display_name="Alice")
    conn = MagicMock()
    conn.connection_id = "c1"
    conn.user_id = "alice"

    result = await svc._ws_browser_speaker_deactivate(conn, {})

    assert result == {"status": "ok"}
    assert "alice" not in backend._active_connections


@pytest.mark.asyncio
async def test_ws_activate_registers_close_callback_for_disconnect_cleanup(
    svc_with_browser_backend: SpeakerService,
) -> None:
    """When the WS connection drops, registration must vanish."""
    svc = svc_with_browser_backend
    backend = svc._backends["browser"]
    captured: list[Any] = []
    conn = MagicMock()
    conn.connection_id = "c1"
    conn.user_id = "alice"
    conn.display_name = "Alice"
    conn.add_close_callback.side_effect = captured.append

    await svc._ws_browser_speaker_activate(conn, {})
    assert "alice" in backend._active_connections

    # Simulate the connection closing
    captured[0]()
    assert "alice" not in backend._active_connections


@pytest.mark.asyncio
async def test_get_ws_handlers_exposes_browser_speaker_rpcs(
    svc_with_browser_backend: SpeakerService,
) -> None:
    handlers = svc_with_browser_backend.get_ws_handlers()
    assert "browser_speaker.activate" in handlers
    assert "browser_speaker.deactivate" in handlers
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/test_speaker_service_browser_permissions.py -v`

Expected: tests FAIL — handler methods don't exist.

- [ ] **Step 3: Implement the handlers + register them in `get_ws_handlers`**

In `src/gilbert/core/services/speaker.py`, add private methods:

```python
async def _ws_browser_speaker_activate(
    self, conn: Any, payload: dict[str, Any]
) -> dict[str, Any]:
    """Register the auth'd connection as an active browser-speaker."""
    backend = self._backends.get("browser")
    if backend is None:
        return {"status": "error", "error": "browser speaker backend not loaded"}
    conn_id = conn.connection_id
    user_id = conn.user_id or ""
    display_name = getattr(conn, "display_name", "") or user_id
    backend.activate(conn_id=conn_id, user_id=user_id, display_name=display_name)
    # Ensure registration vanishes when the WS drops, even if the
    # client never sends an explicit deactivate (tab closed).
    conn.add_close_callback(lambda: backend.deactivate(conn_id=conn_id))
    await self._refresh_cached_speakers()
    return {"status": "ok"}


async def _ws_browser_speaker_deactivate(
    self, conn: Any, payload: dict[str, Any]
) -> dict[str, Any]:
    backend = self._backends.get("browser")
    if backend is None:
        return {"status": "error", "error": "browser speaker backend not loaded"}
    backend.deactivate(conn_id=conn.connection_id)
    await self._refresh_cached_speakers()
    return {"status": "ok"}
```

Add them to `get_ws_handlers()` (line ~1313). The method currently returns a dict; extend it:

```python
def get_ws_handlers(self) -> dict[str, Any]:
    return {
        # ... existing entries (speaker.info, etc.) preserved ...
        "browser_speaker.activate": self._ws_browser_speaker_activate,
        "browser_speaker.deactivate": self._ws_browser_speaker_deactivate,
    }
```

- [ ] **Step 4: Verify tests pass**

Run: `uv run pytest tests/unit/test_speaker_service_browser_permissions.py -v`

Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/speaker.py tests/unit/test_speaker_service_browser_permissions.py
git commit -m "speaker: WS RPCs for browser_speaker.activate / deactivate + close-callback cleanup"
```

---

### Task 3: Role-aware `list_speakers` filter

**Files:**
- Modify: `src/gilbert/core/services/speaker.py`
- Modify: `tests/unit/test_speaker_service_browser_permissions.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_speaker_service_browser_permissions.py`:

```python
from gilbert.interfaces.context import set_current_user


@pytest.mark.asyncio
async def test_list_speakers_admin_sees_all_browser_entries(
    svc_with_browser_backend: SpeakerService,
) -> None:
    svc = svc_with_browser_backend
    backend = svc._backends["browser"]
    backend.activate(conn_id="c1", user_id="alice", display_name="Alice")
    backend.activate(conn_id="c2", user_id="bob", display_name="Bob")

    set_current_user(_make_admin())
    speakers = await svc.list_speakers()

    browser_ids = {s.speaker_id for s in speakers if s.backend_name == "browser"}
    assert browser_ids == {"browser:alice", "browser:bob"}


@pytest.mark.asyncio
async def test_list_speakers_non_admin_sees_only_own_browser(
    svc_with_browser_backend: SpeakerService,
) -> None:
    svc = svc_with_browser_backend
    backend = svc._backends["browser"]
    backend.activate(conn_id="c1", user_id="alice", display_name="Alice")
    backend.activate(conn_id="c2", user_id="bob", display_name="Bob")

    set_current_user(_make_user("alice"))
    speakers = await svc.list_speakers()

    browser_ids = {s.speaker_id for s in speakers if s.backend_name == "browser"}
    assert browser_ids == {"browser:alice"}


@pytest.mark.asyncio
async def test_list_speakers_system_user_sees_all_browser_entries(
    svc_with_browser_backend: SpeakerService,
) -> None:
    svc = svc_with_browser_backend
    backend = svc._backends["browser"]
    backend.activate(conn_id="c1", user_id="alice", display_name="Alice")
    backend.activate(conn_id="c2", user_id="bob", display_name="Bob")

    set_current_user(UserContext.SYSTEM)
    speakers = await svc.list_speakers()

    browser_ids = {s.speaker_id for s in speakers if s.backend_name == "browser"}
    assert browser_ids == {"browser:alice", "browser:bob"}
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/test_speaker_service_browser_permissions.py::test_list_speakers_non_admin_sees_only_own_browser -v`

Expected: FAIL — non-admin currently sees both browsers (no filter applied yet).

- [ ] **Step 3: Add `_is_admin` helper**

In `src/gilbert/core/services/speaker.py`, add the helper (mirroring `InboxService._is_admin` at `src/gilbert/core/services/inbox.py:445`):

```python
def _is_admin(self, user_ctx: UserContext) -> bool:
    """Resolve whether the user has admin-level access.

    Uses ``AccessControlProvider`` if available, otherwise falls
    back to checking for ``"admin"`` in the user's roles. SYSTEM
    counts as admin.
    """
    if user_ctx.user_id == UserContext.SYSTEM.user_id:
        return True
    if self._access_control is not None:
        return self._access_control.get_effective_level(user_ctx) <= 0
    return "admin" in user_ctx.roles
```

Add `self._access_control: AccessControlProvider | None = None` to `__init__` if not already present. Wire it in `start()` via `resolver.get_capability("access_control")` — mirror the inbox service's wiring (read `inbox.py` to confirm the resolver pattern).

If `_access_control` wiring is non-trivial, the fallback `"admin" in roles` is sufficient for this task — the access control provider gets wired later only if `_is_admin` needs to honor finer role gradations.

Import `UserContext` from `gilbert.interfaces.auth` and the access control provider type if you use it.

- [ ] **Step 4: Filter `list_speakers` by role**

Locate `SpeakerService.list_speakers` (the multi-backend merge from Task 10 of the multi-backend plan). At the bottom, before returning, add the filter:

```python
async def list_speakers(self) -> list[SpeakerInfo]:
    # ... existing merge logic ...
    user = get_current_user()
    if self._is_admin(user):
        return merged
    return [
        s for s in merged
        if s.backend_name != "browser" or s.speaker_id == f"browser:{user.user_id}"
    ]
```

(`get_current_user` is from `gilbert.interfaces.context` per the multi-backend refactor.)

- [ ] **Step 5: Verify tests pass**

Run: `uv run pytest tests/unit/test_speaker_service_browser_permissions.py -v`

Expected: 7 PASS.

Run: `uv run pytest tests/unit/ -x -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/gilbert/core/services/speaker.py tests/unit/test_speaker_service_browser_permissions.py
git commit -m "speaker: role-aware filter on list_speakers — admin sees all browsers, others see own"
```

---

### Task 4: Cross-user permission gate on dispatch

**Files:**
- Modify: `src/gilbert/core/services/speaker.py`
- Modify: `tests/unit/test_speaker_service_browser_permissions.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_speaker_service_browser_permissions.py`:

```python
@pytest.mark.asyncio
async def test_play_on_speakers_non_admin_rejects_other_user_browser(
    svc_with_browser_backend: SpeakerService,
) -> None:
    svc = svc_with_browser_backend
    backend = svc._backends["browser"]
    backend.activate(conn_id="c1", user_id="bob", display_name="Bob")

    set_current_user(_make_user("alice"))
    with pytest.raises(PermissionError, match="another user"):
        await svc.play_on_speakers(
            uri="http://example.com/x.mp3",
            speaker_ids=["browser:bob"],
        )


@pytest.mark.asyncio
async def test_play_on_speakers_admin_accepts_other_user_browser(
    svc_with_browser_backend: SpeakerService,
) -> None:
    svc = svc_with_browser_backend
    backend = svc._backends["browser"]
    backend.activate(conn_id="c1", user_id="bob", display_name="Bob")

    set_current_user(_make_admin())
    # Should not raise. Backend will publish an event but that's external.
    await svc.play_on_speakers(
        uri="http://example.com/x.mp3",
        speaker_ids=["browser:bob"],
    )


@pytest.mark.asyncio
async def test_play_on_speakers_non_admin_accepts_own_browser(
    svc_with_browser_backend: SpeakerService,
) -> None:
    svc = svc_with_browser_backend
    backend = svc._backends["browser"]
    backend.activate(conn_id="c1", user_id="alice", display_name="Alice")

    set_current_user(_make_user("alice"))
    await svc.play_on_speakers(
        uri="http://example.com/x.mp3",
        speaker_ids=["browser:alice"],
    )


@pytest.mark.asyncio
async def test_play_on_speakers_system_user_bypasses_check(
    svc_with_browser_backend: SpeakerService,
) -> None:
    svc = svc_with_browser_backend
    backend = svc._backends["browser"]
    backend.activate(conn_id="c1", user_id="bob", display_name="Bob")

    set_current_user(UserContext.SYSTEM)
    await svc.play_on_speakers(
        uri="http://example.com/x.mp3",
        speaker_ids=["browser:bob"],
    )
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/test_speaker_service_browser_permissions.py::test_play_on_speakers_non_admin_rejects_other_user_browser -v`

Expected: FAIL — no permission check exists yet.

- [ ] **Step 3: Add `_check_browser_target_permissions`**

In `src/gilbert/core/services/speaker.py`:

```python
def _check_browser_target_permissions(self, target_ids: list[str]) -> None:
    """Reject cross-user browser targets unless the caller is admin.

    No-op if no ``browser:*`` IDs are in the target list. Admins
    (and the SYSTEM user) bypass the check entirely.
    """
    user = get_current_user()
    if self._is_admin(user):
        return
    for sid in target_ids:
        if sid.startswith("browser:"):
            _, target_user = split_speaker_id(sid)
            if target_user != user.user_id:
                raise PermissionError(
                    f"You can only target your own browser; "
                    f"{sid!r} belongs to another user."
                )
```

- [ ] **Step 4: Call from every dispatch method that accepts speaker targets**

Audit dispatch methods. After resolved-target-list is in hand (post-`resolve_names`, pre-`_route_ids`), call `self._check_browser_target_permissions(target_ids)`.

Methods to gate:
- `play_on_speakers`
- `stop_speakers`
- `set_volume`
- `get_volume`
- `get_playback_state`
- `get_now_playing` (the variant that takes a speaker_name/id, not the fallback variant)
- `play_queue_on_speakers`
- `enqueue_on_speakers`
- `set_repeat_on_speakers`
- `prepare_speakers`
- `announce` (the high-level helper, after it resolves its default-speaker list)

For single-speaker methods, wrap the ID in a one-element list for the check.

Example for `play_on_speakers`:

```python
async def play_on_speakers(
    self, *, uri, speaker_names=None, speaker_ids=None,
    volume=None, title="", announce=False, position_seconds=None,
) -> None:
    resolved_ids = await self._resolve_target_ids(speaker_names, speaker_ids)
    self._check_browser_target_permissions(resolved_ids)  # ← add here
    if not resolved_ids:
        return
    grouped = self._route_ids(resolved_ids)
    # ... existing fan-out
```

Example for a single-target method:

```python
async def set_volume(self, speaker_id: str, level: int) -> None:
    self._check_browser_target_permissions([speaker_id])  # ← add here
    backend, native = self._route_id(speaker_id)
    return await backend.set_volume(native, level)
```

- [ ] **Step 5: Update AI tool wrappers to surface `PermissionError` as JSON**

For each `_tool_*` method that calls one of the gated dispatch methods, wrap the call in a try/except for `PermissionError` and return a clean error JSON:

```python
async def _tool_play_on_speakers(self, ...) -> str:
    try:
        await self.play_on_speakers(...)
    except PermissionError as exc:
        return json.dumps({"status": "error", "error": str(exc)})
    except ValueError as exc:  # existing cross-backend grouping error
        return json.dumps({"status": "error", "error": str(exc)})
    return json.dumps({"status": "ok"})
```

Apply to: `_tool_play_on_speakers`, `_tool_stop_speakers`, `_tool_announce`, `_tool_set_volume`, etc. Audit by grepping `_tool_` methods in `speaker.py`.

- [ ] **Step 6: Verify tests pass**

Run: `uv run pytest tests/unit/test_speaker_service_browser_permissions.py -v`

Expected: 11 PASS.

Run: `uv run pytest tests/unit/ -x -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/gilbert/core/services/speaker.py tests/unit/test_speaker_service_browser_permissions.py
git commit -m "speaker: gate cross-user browser targets — non-admins can only target their own"
```

---

### Task 5: Magic "my browser" aliases in `resolve_speaker_name`

**Files:**
- Modify: `src/gilbert/core/services/speaker.py`
- Create: `tests/unit/test_resolve_speaker_name_my_browser.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_resolve_speaker_name_my_browser.py`:

```python
"""Tests for the magic 'my browser' aliases in resolve_speaker_name."""
from __future__ import annotations

import pytest

from gilbert.core.services.speaker import SpeakerService
from gilbert.integrations.browser_speaker import BrowserSpeakerBackend
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.context import set_current_user


@pytest.fixture
async def svc() -> SpeakerService:
    s = SpeakerService()
    backend = BrowserSpeakerBackend()
    await backend.initialize({})
    s._backends = {"browser": backend}
    return s


def _user(uid: str = "alice") -> UserContext:
    return UserContext(user_id=uid, display_name=uid.title(), email="", roles=frozenset({"user"}))


@pytest.mark.parametrize("alias", ["my browser", "my speaker", "for me", "me"])
@pytest.mark.asyncio
async def test_my_browser_aliases_resolve_to_current_user(
    svc: SpeakerService, alias: str
) -> None:
    set_current_user(_user("alice"))
    assert await svc.resolve_speaker_name(alias) == "browser:alice"


@pytest.mark.parametrize("alias", ["My Browser", "MY SPEAKER", "  for me  ", "Me"])
@pytest.mark.asyncio
async def test_my_browser_aliases_case_and_whitespace_insensitive(
    svc: SpeakerService, alias: str
) -> None:
    set_current_user(_user("alice"))
    assert await svc.resolve_speaker_name(alias) == "browser:alice"


@pytest.mark.asyncio
async def test_my_browser_with_no_current_user_returns_none(
    svc: SpeakerService,
) -> None:
    """When SYSTEM is the caller (no real user), 'my browser' has no referent."""
    set_current_user(UserContext.SYSTEM)
    # SYSTEM has user_id == UserContext.SYSTEM.user_id (which is the sentinel),
    # not a real one. The alias should return None or the sentinel id — design
    # decision: return None so the caller hits "unknown speaker" cleanly.
    result = await svc.resolve_speaker_name("my browser")
    # The implementer can choose: return None, OR return the SYSTEM id (which
    # is fine if downstream gracefully handles it). For now we accept either
    # falsy-or-system-id-prefixed result.
    if result is not None:
        assert result == f"browser:{UserContext.SYSTEM.user_id}"


@pytest.mark.asyncio
async def test_my_browser_does_not_consult_backend_or_storage(
    svc: SpeakerService, monkeypatch
) -> None:
    """The aliases must short-circuit before any backend / storage call."""
    backend = svc._backends["browser"]
    called: list[Any] = []
    async def fake_list_speakers() -> list[Any]:
        called.append("list_speakers")
        return []
    monkeypatch.setattr(backend, "list_speakers", fake_list_speakers)

    set_current_user(_user("alice"))
    await svc.resolve_speaker_name("my browser")
    assert called == [], "resolve_speaker_name('my browser') must not hit the backend"
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/test_resolve_speaker_name_my_browser.py -v`

Expected: FAIL — the aliases aren't recognized; tests get `None` or wrong values.

- [ ] **Step 3: Add the alias branch**

In `src/gilbert/core/services/speaker.py`, at the top of `SpeakerService.resolve_speaker_name`:

```python
_MY_BROWSER_ALIASES = frozenset({"my browser", "my speaker", "for me", "me"})


async def resolve_speaker_name(self, name: str) -> str | None:
    # Magic aliases — resolve to the caller's own browser regardless of
    # whether they're actually active. Downstream dispatch is a silent
    # no-op for inactive browser targets, which is the right behavior.
    if name.strip().lower() in _MY_BROWSER_ALIASES:
        user = get_current_user()
        if user and user.user_id:
            return f"browser:{user.user_id}"
        return None
    # ... existing logic unchanged
```

Define `_MY_BROWSER_ALIASES` as a module-level constant near the top of the file (alongside other constants).

- [ ] **Step 4: Verify tests pass**

Run: `uv run pytest tests/unit/test_resolve_speaker_name_my_browser.py -v`

Expected: 9 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/core/services/speaker.py tests/unit/test_resolve_speaker_name_my_browser.py
git commit -m "speaker: 'my browser' / 'for me' aliases resolve to caller's own browser"
```

---

## Phase 3 — Server: echo refactor

### Task 6: Replace `speaker.browser_echo` pref with activation gate

**Files:**
- Modify: `src/gilbert/core/services/speaker.py`
- Modify: `src/gilbert/core/services/users.py` (if it has a `speaker.browser_echo` ConfigParam — verify by grep)
- Modify: `tests/unit/test_speaker_browser_echo.py`
- Modify: `tests/unit/test_user_service.py` (if it asserts on the pref)

- [ ] **Step 1: Inventory the existing pref logic**

Run:

```bash
grep -rn "speaker\.browser_echo\|_BROWSER_ECHO_PREF_KEY\|browser_echo" src/ tests/ 2>&1
```

Note the hits. The pref currently lives at:
- `src/gilbert/core/services/speaker.py:49` — `_BROWSER_ECHO_PREF_KEY = "speaker.browser_echo"`
- `src/gilbert/core/services/speaker.py:_browser_echo_should_fire` — reads the pref via `UserPrefReader`
- Possibly in `src/gilbert/core/services/users.py` — a ConfigParam declaration

- [ ] **Step 2: Write the new gate test**

Append to `tests/unit/test_speaker_browser_echo.py` (or replace the existing pref-based tests with these):

```python
@pytest.mark.asyncio
async def test_echo_fires_only_when_user_has_active_registration(
    speaker_service_browser_echo,
) -> None:
    """No registered tab = no echo, regardless of any old pref."""
    svc = speaker_service_browser_echo  # caller = alice
    browser = svc._backends["browser"]
    # No activation yet
    events_before = list(svc._event_bus_provider.bus.published)

    await svc.play_on_speakers(
        uri="http://example.com/x.mp3",
        speaker_ids=["sonos:living"],
    )

    echo_events = [
        e for e in svc._event_bus_provider.bus.published[len(events_before):]
        if e.event_type == "speaker.browser.play" and e.source == "speaker.echo"
    ]
    assert echo_events == [], "Echo must NOT fire when caller has no active browser registration"


@pytest.mark.asyncio
async def test_echo_fires_when_user_has_active_registration(
    speaker_service_browser_echo,
) -> None:
    svc = speaker_service_browser_echo  # caller = alice
    browser = svc._backends["browser"]
    browser.activate(conn_id="c1", user_id="alice", display_name="Alice")

    events_before = list(svc._event_bus_provider.bus.published)
    await svc.play_on_speakers(
        uri="http://example.com/x.mp3",
        speaker_ids=["sonos:living"],
    )

    echo_events = [
        e for e in svc._event_bus_provider.bus.published[len(events_before):]
        if e.event_type == "speaker.browser.play" and e.source == "speaker.echo"
        and e.data.get("user_id") == "alice"
    ]
    assert len(echo_events) == 1
```

Update / delete existing `speaker_service_browser_echo` fixture to not seed the pref (no more pref). Activation registration is the gate.

- [ ] **Step 3: Verify failure**

Run: `uv run pytest tests/unit/test_speaker_browser_echo.py -v`

Expected: at least one of the new tests fails (the gate currently checks the pref, not activation).

- [ ] **Step 4: Replace the pref check with the activation check**

In `src/gilbert/core/services/speaker.py`:

(a) Delete `_BROWSER_ECHO_PREF_KEY` constant (line ~49).

(b) Update `_browser_echo_should_fire`:

```python
async def _browser_echo_should_fire(self) -> bool:
    """Gate for fan-out to the caller's browser.

    Fires when ALL of:
    - the event bus is wired
    - the primary backend isn't ``browser`` (would double-play)
    - the caller is a real user with an active browser registration
    """
    if self._event_bus_provider is None:
        return False
    if self._primary_backend == "browser":
        return False
    user = get_current_user()
    if not user or not user.user_id:
        return False
    browser = self._backends.get("browser")
    if browser is None:
        return False
    return bool(browser._active_connections.get(user.user_id))
```

(c) Remove the `users_svc` / `UserPrefReader` plumbing if it's no longer used elsewhere in `speaker.py`. Grep `self._users_svc` to check; if only used for the echo gate, delete the import + the attribute + the `start()` wiring.

- [ ] **Step 5: Remove the ConfigParam in `users.py` if present**

Run:

```bash
grep -n "browser_echo\|speaker\.browser_echo" src/gilbert/core/services/users.py 2>&1
```

If any `ConfigParam` references `speaker.browser_echo`, delete those declarations. If `users.py` doesn't reference it, skip.

- [ ] **Step 6: Update or delete pref-related tests**

In `tests/unit/test_user_service.py`: grep for `browser_echo` and delete or update those test cases.

- [ ] **Step 7: Verify**

Run: `uv run pytest tests/unit/ -x -q`

Expected: PASS.

Run: `grep -rn "_BROWSER_ECHO_PREF_KEY\|speaker\.browser_echo" src/ 2>&1` — should return ZERO hits.

- [ ] **Step 8: Commit**

```bash
git add src/gilbert/core/services/speaker.py src/gilbert/core/services/users.py tests/
git commit -m "speaker: replace browser_echo pref with activation-registration gate"
```

---

## Phase 4 — Frontend: hook + control

### Task 7: `useBrowserSpeaker` hook (full replacement)

**Files:**
- Delete: `frontend/src/hooks/useBrowserEchoPref.tsx`
- Modify (full replace): `frontend/src/hooks/useBrowserSpeaker.tsx`

- [ ] **Step 1: Delete the old echo-pref hook**

```bash
git rm frontend/src/hooks/useBrowserEchoPref.tsx
```

Also grep for any imports of `useBrowserEchoPref` — there shouldn't be any after the chat-cleanup task, but if there are, those need updating in Task 9.

- [ ] **Step 2: Write the new hook**

Replace `frontend/src/hooks/useBrowserSpeaker.tsx` contents with:

```tsx
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useWebSocket } from "@/hooks/useWebSocket";
import { useWsApi } from "@/hooks/useWsApi";
import { useEventBus } from "@/hooks/useEventBus";

const STORAGE_KEY = "browser_speaker.enabled";
const HISTORY_LIMIT = 10;

export interface PlayItem {
  id: string;
  url: string;
  title: string;
  volume: number;       // 0-100
  receivedAt: number;
}

interface BrowserSpeakerStore {
  enabled: boolean;
  history: PlayItem[];
  lastPlayed: PlayItem | null;
  isPlaying: boolean;
  setEnabled: (v: boolean) => void;
  replay: (id: string) => void;
  clearHistory: () => void;
}

const BrowserSpeakerContext = createContext<BrowserSpeakerStore | null>(null);

function readPersistedEnabled(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "true";
  } catch {
    return false;
  }
}

function persistEnabled(v: boolean): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, v ? "true" : "false");
  } catch {
    // Storage may be unavailable (private mode, quota); ignore.
  }
}

export function BrowserSpeakerProvider({ children }: { children: ReactNode }) {
  const [enabled, setEnabledState] = useState<boolean>(readPersistedEnabled);
  const [history, setHistory] = useState<PlayItem[]>([]);
  const [lastPlayed, setLastPlayed] = useState<PlayItem | null>(null);
  const [isPlaying, setIsPlaying] = useState(false);
  const { connected } = useWebSocket();
  const api = useWsApi();
  const bus = useEventBus();

  // Singleton <audio> element kept across renders.
  const audioRef = useRef<HTMLAudioElement | null>(null);
  if (audioRef.current === null && typeof document !== "undefined") {
    const el = document.createElement("audio");
    el.preload = "metadata";
    el.style.display = "none";
    document.body.appendChild(el);
    audioRef.current = el;
    el.addEventListener("ended", () => setIsPlaying(false));
    el.addEventListener("pause", () => setIsPlaying(false));
  }

  // Activation sync — fire activate/deactivate to the server based on
  // [enabled, connected]. Re-activates on reconnect if enabled is true
  // (server-side registration dies with the connection).
  const lastSyncedRef = useRef<boolean>(false);
  useEffect(() => {
    let cancelled = false;
    const want = enabled && connected;
    if (want === lastSyncedRef.current) return;
    lastSyncedRef.current = want;
    (async () => {
      try {
        if (want) {
          await api.rpc("browser_speaker.activate", {});
        } else {
          await api.rpc("browser_speaker.deactivate", {});
        }
      } catch {
        // Treat failures as transient; next state change retries.
      }
      if (cancelled) return;
    })();
    return () => {
      cancelled = true;
    };
  }, [enabled, connected, api]);

  // Event subscription — append to history, autoplay if enabled.
  useEffect(() => {
    const handler = (evt: { event_type: string; data: Record<string, unknown> }) => {
      if (evt.event_type !== "speaker.browser.play") return;
      const url = String(evt.data["url"] ?? "");
      if (!url) return;
      const item: PlayItem = {
        id: String(evt.data["event_id"] ?? `${Date.now()}-${Math.random()}`),
        url,
        title: String(evt.data["title"] ?? ""),
        volume: Number(evt.data["volume"] ?? 80),
        receivedAt: Date.now(),
      };
      setHistory((prev) => [item, ...prev].slice(0, HISTORY_LIMIT));
      setLastPlayed(item);
      if (enabled && audioRef.current) {
        const el = audioRef.current;
        el.src = url;
        el.volume = Math.max(0, Math.min(1, item.volume / 100));
        setIsPlaying(true);
        el.play().catch(() => setIsPlaying(false));
      }
    };
    return bus.subscribe("speaker.browser.play", handler);
  }, [bus, enabled]);

  const setEnabled = useCallback((v: boolean) => {
    persistEnabled(v);
    setEnabledState(v);
    if (!v && audioRef.current) {
      audioRef.current.pause();
      setIsPlaying(false);
    }
  }, []);

  const replay = useCallback(
    (id: string) => {
      const el = audioRef.current;
      if (!el) return;
      const item = history.find((h) => h.id === id);
      if (!item) return;
      el.src = item.url;
      el.volume = Math.max(0, Math.min(1, item.volume / 100));
      setIsPlaying(true);
      el.play().catch(() => setIsPlaying(false));
    },
    [history],
  );

  const clearHistory = useCallback(() => setHistory([]), []);

  // Cleanup on unmount.
  useEffect(() => {
    return () => {
      const el = audioRef.current;
      if (el) {
        el.pause();
        el.remove();
        audioRef.current = null;
      }
      if (lastSyncedRef.current) {
        // Best-effort deactivate; the WS may already be gone.
        try {
          api.rpc("browser_speaker.deactivate", {});
        } catch {
          // ignore
        }
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const store = useMemo<BrowserSpeakerStore>(
    () => ({
      enabled,
      history,
      lastPlayed,
      isPlaying,
      setEnabled,
      replay,
      clearHistory,
    }),
    [enabled, history, lastPlayed, isPlaying, setEnabled, replay, clearHistory],
  );

  return (
    <BrowserSpeakerContext.Provider value={store}>
      {children}
    </BrowserSpeakerContext.Provider>
  );
}

export function useBrowserSpeaker(): BrowserSpeakerStore {
  const ctx = useContext(BrowserSpeakerContext);
  if (ctx === null) {
    throw new Error("useBrowserSpeaker must be used inside <BrowserSpeakerProvider>");
  }
  return ctx;
}
```

(Adjust the imports to match the actual `useWebSocket` / `useWsApi` / `useEventBus` APIs. If `useEventBus` doesn't exist, look at how `BrowserAudioBubbles.tsx` currently subscribes via `useBrowserSpeakerClips` and use the same mechanism.)

- [ ] **Step 3: Wire the provider into `AppShell`**

Find `frontend/src/components/layout/AppShell.tsx`. Wrap its children with `<BrowserSpeakerProvider>` near the top of the auth'd tree (after the auth check but before main content):

```tsx
import { BrowserSpeakerProvider } from "@/hooks/useBrowserSpeaker";

// inside the component:
return (
  <BrowserSpeakerProvider>
    {/* existing tree */}
  </BrowserSpeakerProvider>
);
```

- [ ] **Step 4: Type-check**

Run: `cd frontend && npx tsc --noEmit 2>&1 | tail -20`

Expected: clean. If `useEventBus` or `useWsApi.rpc` signatures don't match, adjust the calls until they do.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/hooks/useBrowserSpeaker.tsx frontend/src/components/layout/AppShell.tsx
git rm frontend/src/hooks/useBrowserEchoPref.tsx
git commit -m "browser-speaker(frontend): unified hook with localStorage + activation sync + history"
```

---

### Task 8: `<BrowserSpeakerControl />` header component

**Files:**
- Create: `frontend/src/components/layout/BrowserSpeakerControl.tsx`
- Modify: `frontend/src/components/layout/TopBar.tsx`

- [ ] **Step 1: Write the component**

Create `frontend/src/components/layout/BrowserSpeakerControl.tsx`:

```tsx
import { Volume2Icon, VolumeXIcon, PlayIcon, XIcon } from "lucide-react";
import { useState } from "react";
import { useBrowserSpeaker } from "@/hooks/useBrowserSpeaker";
import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

export function BrowserSpeakerControl() {
  const { enabled, setEnabled, history, isPlaying, replay, clearHistory } =
    useBrowserSpeaker();
  const [open, setOpen] = useState(false);

  const Icon = enabled ? Volume2Icon : VolumeXIcon;

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        render={
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Browser speaker"
            title={enabled ? "Browser speaker on" : "Browser speaker off"}
          />
        }
      >
        <Icon
          className={cn(
            "size-4",
            enabled ? "text-foreground" : "text-muted-foreground",
            isPlaying && "animate-pulse",
          )}
        />
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 p-0">
        <div className="flex items-center justify-between p-3 border-b">
          <Label className="text-sm">Receive audio on this tab</Label>
          <Switch checked={enabled} onCheckedChange={setEnabled} />
        </div>
        <div className="max-h-64 overflow-y-auto">
          {history.length === 0 ? (
            <div className="p-4 text-center text-xs text-muted-foreground">
              Nothing has played yet.
            </div>
          ) : (
            <ul className="divide-y">
              {history.map((item) => (
                <li
                  key={item.id}
                  className="flex items-center gap-2 px-3 py-2"
                >
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    onClick={() => replay(item.id)}
                    aria-label={`Replay ${item.title || "audio"}`}
                  >
                    <PlayIcon className="size-3.5" />
                  </Button>
                  <div className="flex-1 min-w-0">
                    <div className="text-xs truncate">
                      {item.title || "Audio clip"}
                    </div>
                    <div className="text-[10px] text-muted-foreground">
                      {timeAgo(item.receivedAt)}
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
        {history.length > 0 && (
          <div className="p-2 border-t">
            <Button
              variant="ghost"
              size="sm"
              className="w-full"
              onClick={clearHistory}
            >
              <XIcon className="size-3.5" />
              Clear history
            </Button>
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
}

function timeAgo(ms: number): string {
  const diff = Date.now() - ms;
  const sec = Math.floor(diff / 1000);
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  return `${hr}h ago`;
}
```

(If `Popover` / `Switch` / `Label` components don't exist at those paths, look for the actual UI primitives in `frontend/src/components/ui/` and adjust imports.)

- [ ] **Step 2: Slot into `TopBar`**

In `frontend/src/components/layout/TopBar.tsx`, around line 141-143 (the right-side cluster), add the new component between `<PluginPanelSlot slot="header.widgets" />` and `<NotificationBell />`:

```tsx
import { BrowserSpeakerControl } from "./BrowserSpeakerControl";

// inside the right cluster:
<PluginPanelSlot slot="header.widgets" />
<BrowserSpeakerControl />
<NotificationBell />
```

- [ ] **Step 3: Type-check**

Run: `cd frontend && npx tsc --noEmit 2>&1 | tail -10`

Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/layout/BrowserSpeakerControl.tsx frontend/src/components/layout/TopBar.tsx
git commit -m "browser-speaker(frontend): <BrowserSpeakerControl /> in header with toggle + history popover"
```

---

## Phase 5 — Frontend: chat cleanup

### Task 9: Remove `BrowserAudioBubbles` from chat

**Files:**
- Delete: `frontend/src/components/chat/BrowserAudioBubbles.tsx`
- Modify: `frontend/src/components/chat/MessageList.tsx`

- [ ] **Step 1: Delete the file**

```bash
git rm frontend/src/components/chat/BrowserAudioBubbles.tsx
```

- [ ] **Step 2: Remove import + render in `MessageList.tsx`**

In `frontend/src/components/chat/MessageList.tsx`:

(a) Delete line 3: `import { BrowserAudioBubbles } from "./BrowserAudioBubbles";`

(b) Delete lines 138-140:

```tsx
{conversationId ? (
  <BrowserAudioBubbles conversationId={conversationId} />
) : null}
```

- [ ] **Step 3: Verify no orphan references**

```bash
grep -rn "BrowserAudioBubbles\|useBrowserSpeakerClips" frontend/src/ 2>&1
```

Expected: no hits. If `useBrowserSpeakerClips` is referenced anywhere else (it was the older export from `useBrowserSpeaker.tsx`), those callers need to migrate to `useBrowserSpeaker()`. Audit and fix.

- [ ] **Step 4: Type-check**

Run: `cd frontend && npx tsc --noEmit 2>&1 | tail -10`

Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/chat/MessageList.tsx
git commit -m "chat(frontend): remove BrowserAudioBubbles — browser audio lives in header now"
```

---

## Phase 6 — Docs + AI tool descriptions

### Task 10: AI tool descriptions + architecture doc

**Files:**
- Modify: `src/gilbert/core/services/speaker.py` (tool descriptions)
- Modify: `src/gilbert/core/services/music.py` (tool descriptions, if speaker-targeting tools mention names)
- Modify: `docs/architecture/speaker-system.md`

- [ ] **Step 1: Update AI tool descriptions**

Run:

```bash
grep -nB 1 'ToolDefinition\|speaker_names\|target_speakers' src/gilbert/core/services/speaker.py src/gilbert/core/services/music.py 2>&1 | head -60
```

For each tool that targets speakers by name, append a one-liner to the description (or parameter description):

> Pass `"my browser"`, `"my speaker"`, or `"for me"` to target the caller's own browser tab.

Apply to: `announce`, `play_on_speakers`, `stop_speakers`, `set_speaker_volume`, `play_music`, `add_to_queue`, etc. Read each `ToolDefinition` description string and append the tip where speaker targeting is the focus.

- [ ] **Step 2: Update `docs/architecture/speaker-system.md`**

Add or update a section near the multi-backend doc (added in the multi-backend plan's Task 20):

```markdown
## Browser speaker activation model

A user's browser tab is a speaker **only while two conditions hold**:

1. The user toggled "Receive audio on this tab" on (header control, persisted in localStorage).
2. The tab's WebSocket connection is alive.

The tab signals activation by sending `browser_speaker.activate` on connect (when the toggle is on); the server stores `(user_id, connection_id, display_name)` in `BrowserSpeakerBackend._active_connections`. When the connection drops, a `WsConnection.add_close_callback` fires to clean up. When the user flips the toggle off, the tab sends `browser_speaker.deactivate`.

`BrowserSpeakerBackend.list_speakers()` enumerates all users with at least one active registration. `SpeakerService.list_speakers()` then filters by role — non-admins see only their own `browser:<self>` entry; admins see all. The same role check (`_check_browser_target_permissions`) gates dispatch: non-admins can only target their own browser; admins can target any.

The browser-speaker echo gate (mirroring a non-browser primary play to the caller's browser) is enabled iff the caller has an active registration. The retired `speaker.browser_echo` user-metadata pref is replaced by the activation state — the toggle IS the opt-in.

### Magic name aliases

`SpeakerService.resolve_speaker_name` recognizes a small set of aliases that resolve to the caller's own browser without consulting storage:

- `my browser`
- `my speaker`
- `for me`
- `me`

These are case- and whitespace-insensitive. AI tool descriptions mention them so the model can use natural phrasing.
```

- [ ] **Step 3: Final test pass**

```bash
uv run pytest tests/ -x -q
uv run mypy src/gilbert/core/services/speaker.py src/gilbert/integrations/browser_speaker.py
cd frontend && npx tsc --noEmit
```

Expected: all clean.

- [ ] **Step 4: Commit**

```bash
git add src/gilbert/core/services/speaker.py src/gilbert/core/services/music.py docs/architecture/speaker-system.md
git commit -m "docs: browser-speaker activation model + 'my browser' aliases in AI tools"
```

---

## Final checkpoint

After Task 10:

1. Full suite green: `uv run pytest -x -q`.
2. Architecture audit clean: invoke the `validate-architecture` skill in audit mode.
3. Manual smoke test:
   - Boot Gilbert; sign in as admin.
   - Open the SPA; verify the header speaker icon is muted (default off).
   - Click the icon → popover opens → toggle on → icon becomes solid.
   - In another tab as a non-admin user, toggle on; verify each tab plays its own audio.
   - From a chat as admin, try targeting "my browser" → resolves to your browser, plays.
   - From a chat as non-admin, try targeting "Alice's Browser" (where Alice is the admin) → permission error returned to the AI tool.
   - Refresh the tab → toggle stays on (localStorage); reactivation happens automatically.
   - Trigger several plays; click history items → local replay works without server roundtrip.
4. Push to jereanon's fork; verify PR #16 is MERGEABLE + CLEAN.
5. `/merge-pr 16` — runs audit + squash-merge into main.

---

## Self-review

**Spec coverage:**

- §1 (Architecture overview) → Tasks 1–9.
- §2 (Frontend data flow) → Task 7.
- §3 (Server-side flow): activation tracking (Task 1), WS RPCs (Task 2), `list_speakers` filter (Task 3), permission gate (Task 4).
- §4 (My-browser + chat cleanup): aliases (Task 5), chat cleanup (Task 9), echo gate refactor (Task 6), AI tool descriptions (Task 10).
- §5 (Testing): test files created across Tasks 1–6.
- §6 (Non-goals): not implemented (correct — they're explicit exclusions).
- §7 (Phasing): mirrored in the 10-task structure.

**Placeholder scan:** no TBD/TODO. Two implementer-discretion notes:
- Task 7 says "Adjust the imports to match the actual `useWebSocket` / `useWsApi` / `useEventBus` APIs" — that's a verify-during-implementation note, not a placeholder. The hook shape is fully specified.
- Task 10 says "Read each `ToolDefinition` description string and append the tip" — same: full guidance, but the implementer adapts to the actual text of each tool description.

**Type consistency:**
- `_active_connections: dict[str, dict[str, str]]` (Task 1) used in `_browser_echo_should_fire` (Task 6) — same shape.
- `_check_browser_target_permissions(target_ids: list[str])` (Task 4) called consistently with namespaced IDs.
- `_is_admin(user_ctx)` (Task 3) used in Task 4 too — same signature.
- `useBrowserSpeaker()` return shape matches the `<BrowserSpeakerControl />` consumer (Task 8).
- `PlayItem` shape in Task 7 used unchanged in Task 8.

All consistent.
