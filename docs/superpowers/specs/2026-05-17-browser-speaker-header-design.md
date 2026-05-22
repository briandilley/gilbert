# Browser Speaker Header Control — Design

**Date:** 2026-05-17
**Branch:** `feature/browser_speaker_backend` (bundled into PR #16)
**Companion specs:** [`2026-05-17-multi-backend-speakers-design.md`](2026-05-17-multi-backend-speakers-design.md)
**Status:** Design approved; ready for implementation planning.

## Goal

Move the browser-speaker affordance from "per-conversation chat bubbles + a user-metadata pref" to "a global header control that doubles as an opt-in registration for the user's tab as a speaker." Add cross-user listing/targeting under role-based access control, magic AI-tool aliases for "my browser," and a client-side replay history.

## Decisions captured during brainstorming

| Decision | Choice |
|---|---|
| Toggle-off semantics | Browser disappears from `list_speakers()` entirely — not a speaker when toggle is off |
| Toggle persistence | `localStorage` per device/browser (server-side `speaker.browser_echo` pref retired) |
| Header UI | Single icon button → popover with toggle + history list |
| History scope | Global per-tab, last 10 plays, in-memory only (no server persistence) |
| Replay action | Plays locally in the tab only; no server roundtrip / re-broadcast |
| Cross-user visibility in lists | Admin sees all active browsers; non-admin sees only their own |
| Cross-user targeting | Admin can play to anyone's browser; non-admin can only target their own |
| "My browser" resolution | Magic aliases at the resolver layer (`my browser`, `my speaker`, `for me`, `me`) |
| Chat-page audio bubbles | Removed entirely — browser-speaker plays only surface in the header history |

## Layer / pattern context

Header control lives in the core SPA (`frontend/src/`); `BrowserSpeakerBackend` is a core integration in `src/gilbert/integrations/`; the new WS RPCs hang off `SpeakerService` in `src/gilbert/core/services/`. No plugin involvement — entirely core territory.

The activation model parallels Gilbert's existing browser-bridged MCP pattern (per the README: "session-ephemeral, strictly private to the owning user… disappear the moment the tab closes"): the tab announces itself on connect, the server tracks a per-user registration set, the registration vanishes on disconnect.

---

## 1. Architecture overview

### Frontend

- **`<BrowserSpeakerControl />`** — new component in the header cluster (`frontend/src/components/layout/TopBar.tsx`), slotted between `<PluginPanelSlot slot="header.widgets" />` and `<NotificationBell />`. Renders an icon button whose visual state reflects on/off + currently-playing; clicking opens a popover.
- **`useBrowserSpeaker()`** — global context-backed store at the `AppShell` root, replacing the existing `useBrowserEchoPref` and `useBrowserSpeakerClips` hooks.

### Server-side

- **Two new WS RPCs** on `SpeakerService`: `browser_speaker.activate`, `browser_speaker.deactivate`.
- **`BrowserSpeakerBackend`** holds a per-user registration map keyed by connection id; on disconnect (event-bus subscription) the entry is cleaned up.
- **`SpeakerService.list_speakers()`** post-merge filter by caller's role.
- **`SpeakerService` dispatch helpers** check `browser:<target>` against caller's user_id and role before fan-out.
- **`SpeakerService.resolve_speaker_name`** gains a magic-alias branch.

### Removed

- `frontend/src/hooks/useBrowserEchoPref.tsx`
- `frontend/src/components/chat/BrowserAudioBubbles.tsx`
- The render of those bubbles in `frontend/src/components/chat/MessageList.tsx:138-140` and the corresponding import
- Server-side `speaker.browser_echo` user-metadata pref (no migration — PR #16 hasn't shipped)

---

## 2. Frontend data flow

### `useBrowserSpeaker()` store

```ts
interface PlayItem {
  id: string;         // event id (or generated uuid)
  url: string;
  title: string;
  volume: number;     // 0-100 (matches existing server payload)
  receivedAt: number; // Date.now()
}

interface BrowserSpeakerStore {
  enabled: boolean;
  history: PlayItem[];   // newest first, capped at 10
  lastPlayed: PlayItem | null;
  isPlaying: boolean;
  setEnabled(v: boolean): void;
  replay(id: string): void;
  clearHistory(): void;
}
```

### Initialization

- `enabled` from `localStorage["browser_speaker.enabled"] === "true"`; default `false`.
- `history` always starts empty.
- A singleton `<audio>` element is created once via `useRef` and mounted via a portal into `document.body`. All plays (live and replay) use this one element — automatic "stop previous when next arrives."

### Activation sync

A `useEffect` watches `[enabled, connected]`. Whenever both transition to `true`, the hook calls `browser_speaker.activate`. On either transitioning to `false`, calls `browser_speaker.deactivate`. On WS reconnect with `enabled === true`, the activation is re-sent (server-side registration is lost when the connection dies).

### Event handling

The hook subscribes to `speaker.browser.play` events (existing event-bus surface). Each event:

1. Pushes a `PlayItem` to `history` (newest first, splice to 10).
2. Updates `lastPlayed`, sets `isPlaying = true`.
3. If `enabled === true`, plays through the singleton `<audio>`.
4. The `<audio>` element's `onended` handler sets `isPlaying = false`.

If `enabled === false`, the user shouldn't be receiving these events at all (server-side registration is the gate). The hook still drops them client-side as a belt-and-suspenders against race conditions.

### Replay

`replay(id)` looks up the item in `history`, reuses the singleton `<audio>` (`.src = item.url; .volume = item.volume / 100; .play()`). No server roundtrip.

### Cleanup

On unmount (e.g., logout), the hook deactivates if it was active. The singleton `<audio>` is removed via the portal cleanup.

### Header button display state

| State | Visual |
|---|---|
| `enabled=false` | Muted/dimmed speaker icon |
| `enabled=true` AND `!isPlaying` | Solid speaker icon |
| `enabled=true` AND `isPlaying` | Speaker icon + pulse animation |

Popover content (opened on click): a toggle switch at top labeled "Receive audio on this tab," a divider, a scrolling list of `history` items (each: title, time-ago, play button); empty state is "Nothing has played yet." A "Clear history" link at the bottom when history is non-empty.

---

## 3. Server-side flow

### 3.1 New WS RPCs on `SpeakerService`

```python
# get_ws_handlers() additions
{
    "browser_speaker.activate": self._ws_browser_speaker_activate,
    "browser_speaker.deactivate": self._ws_browser_speaker_deactivate,
}
```

Both handlers read `conn.user_id`, `conn.connection_id`, and `conn.display_name` (or fall back to `user_id` if no display name). Payload is empty — registration is implicit in the auth'd connection. Returns `{"status": "ok"}`.

### 3.2 `BrowserSpeakerBackend` state

```python
self._active_connections: dict[str, dict[str, str]] = {}
# user_id -> {conn_id: display_name_when_registered}

self._conn_to_user: dict[str, str] = {}
# reverse map for O(1) disconnect cleanup

def activate(self, *, conn_id: str, user_id: str, display_name: str) -> None:
    self._active_connections.setdefault(user_id, {})[conn_id] = display_name
    self._conn_to_user[conn_id] = user_id

def deactivate(self, *, conn_id: str) -> None:
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

### 3.3 `list_speakers()` — multi-user

```python
async def list_speakers(self) -> list[SpeakerInfo]:
    out: list[SpeakerInfo] = []
    for user_id, conns in self._active_connections.items():
        if not conns:
            continue
        display_name = next(iter(conns.values()))
        out.append(SpeakerInfo(
            speaker_id=user_id,                   # SpeakerService prefixes "browser:"
            name=f"{display_name}'s Browser",
            ip_address="",
        ))
    return out
```

Behavior change from PR #16's prior shape: the backend no longer reads `get_current_user()` to filter. It returns all active users. Role-based filtering moves up to `SpeakerService`.

### 3.4 Disconnect cleanup

`BrowserSpeakerBackend` subscribes (in `set_event_bus_provider`) to the `ws.connection.closed` event type. Each event's payload includes `conn_id`; the backend calls `self.deactivate(conn_id=conn_id)`.

Implementer verifies the actual event type/payload during execution by inspecting `gilbert.web.ws_protocol` for the disconnect publication. If no such event exists today, alternative: the WS connection's close hook fires a callback registered by SpeakerService. Either path is acceptable; the contract is "registration vanishes within one event-loop tick of connection close."

### 3.5 `SpeakerService.list_speakers()` — role-aware filter

```python
async def list_speakers(self) -> list[SpeakerInfo]:
    merged = ...  # existing multi-backend merge
    user = get_current_user()
    if _is_admin(user):
        return merged
    return [
        s for s in merged
        if s.backend_name != "browser" or s.speaker_id == f"browser:{user.user_id}"
    ]
```

`_is_admin(user)` uses whatever role-check API `UserContext` exposes. `UserContext.SYSTEM` counts as admin.

### 3.6 `SpeakerService` — role-gated cross-user targeting

```python
def _check_browser_target_permissions(self, target_ids: list[str]) -> None:
    user = get_current_user()
    if _is_admin(user):
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

Called at the top of every speaker-targeting dispatch method:
- `play_on_speakers`
- `stop_speakers`
- `set_volume` / `get_volume`
- `get_playback_state` / `get_now_playing`
- `play_queue_on_speakers` / `enqueue_on_speakers` / `set_repeat_on_speakers`
- `prepare_speakers` (used by announce)
- `announce` (the high-level helper)

AI tool wrappers catch `PermissionError` and return a clean JSON error so the model can recover.

System-context calls bypass the check.

---

## 4. Magic name resolution + chat cleanup

### 4.1 "My browser" aliases

```python
_MY_BROWSER_ALIASES = frozenset({"my browser", "my speaker", "for me", "me"})

async def resolve_speaker_name(self, name: str) -> str | None:
    if name.strip().lower() in _MY_BROWSER_ALIASES:
        user = get_current_user()
        if user and user.user_id:
            return f"browser:{user.user_id}"
        return None
    # ... existing logic continues
```

Returns the namespaced ID directly. The downstream dispatch fan-out handles "what if the target user isn't active right now" by publishing the event anyway — if no tab is registered, no one hears it (silent no-op, which is the desired behavior for unreliable inactive targets).

### 4.2 AI tool description updates

Every speaker-targeting tool's `description` and the `speaker_names` parameter's description get a one-liner:

> *Tip: pass `"my browser"`, `"my speaker"`, or `"for me"` to target the caller's own browser tab.*

Audit during implementation by grepping `ToolDefinition(...)` instances that take speaker name parameters in `speaker.py`, `music.py`, and any other service surface that targets speakers.

### 4.3 Chat-page cleanup

Three deletions:

1. `frontend/src/components/chat/BrowserAudioBubbles.tsx` — entire file.
2. `frontend/src/components/chat/MessageList.tsx:3` — the import.
3. `frontend/src/components/chat/MessageList.tsx:138-140` — the render block.

### 4.4 Echo gate simplification

In `SpeakerService._browser_echo_should_fire` and `_maybe_echo_to_browser`:

- **Remove:** the `speaker.browser_echo` user-pref check (the pref no longer exists).
- **Keep:** "primary backend isn't browser" check.
- **Keep:** "caller's browser already in explicit target set" check (Task 14 from the multi-backend spec).
- **Add:** "caller has an active browser registration" check — gate on `BrowserSpeakerBackend._active_connections.get(caller.user_id)` being non-empty. If the user has no active tab, no echo.

The new gate eliminates the need for a separate pref entirely — activation status IS the opt-in.

### 4.5 `users` service cleanup

The `speaker.browser_echo` ConfigParam (if it was added to the users service in PR #16's earlier commits) is removed. Any `UserPrefReader.is_pref_set("speaker.browser_echo", user_id)` calls are deleted at their call sites.

---

## 5. Testing strategy

### 5.1 New server-side tests

**`tests/unit/test_browser_speaker_activation.py`**
- `test_activate_registers_connection_for_user`
- `test_deactivate_removes_connection`
- `test_disconnect_cleans_up_via_event_bus`
- `test_list_speakers_returns_entry_per_active_user`
- `test_list_speakers_empty_when_no_active_connections`
- `test_activate_idempotent_on_repeated_calls`

**`tests/unit/test_speaker_service_browser_permissions.py`**
- `test_list_speakers_admin_sees_all_browser_entries`
- `test_list_speakers_non_admin_sees_only_own_browser`
- `test_play_on_speakers_non_admin_rejects_other_user_browser`
- `test_play_on_speakers_admin_accepts_other_user_browser`
- `test_play_on_speakers_non_admin_accepts_own_browser`
- `test_play_on_speakers_system_user_bypasses_check`

**`tests/unit/test_resolve_speaker_name_my_browser.py`**
- `test_my_browser_alias_resolves_to_current_user`
- `test_my_speaker_my_browser_for_me_me_all_resolve` (parametrized)
- `test_my_browser_with_no_current_user_returns_none`
- `test_my_browser_case_insensitive`
- `test_my_browser_does_not_consult_storage`

### 5.2 Existing tests updated

- `tests/unit/test_speaker_browser_echo.py` — drop the `browser_echo` pref-mock setup; replace with activation-registration setup. Echo now gates on active registration.
- `tests/unit/test_browser_speaker.py` — `list_speakers()` now returns entries per active user, not per current user. Adjust assertions.
- `tests/unit/test_user_service.py` — remove any `speaker.browser_echo` pref assertions.

### 5.3 Frontend tests

Gilbert doesn't have a frontend test harness wired today, so verification is manual:

- Two tabs same user → both register, both receive plays.
- Two tabs different users (one admin, one regular) → admin sees both in list_speakers, regular sees only their own.
- Toggle off in tab A → that user vanishes from list_speakers (if no other tab); toggle back on → re-registers.
- Tab refresh with toggle on → localStorage persists; tab re-activates on reconnect.
- Click history items → local replay, no server roundtrip.

### 5.4 Validation

After implementation, run:
- `uv run pytest tests/ -x -q`
- `cd frontend && npx tsc --noEmit`
- Invoke the `validate-architecture` skill in audit mode.

---

## 6. Non-goals

Explicitly out of scope:

- Cross-tab state sync within one user (independent tabs is fine).
- Server-side history persistence (in-memory per tab only).
- Granular role gradations beyond admin / non-admin.
- Mute-but-still-listable mode (rejected during clarification).
- Other chat-audio surfaces (only browser-speaker bubbles being removed; broader chat-audio redesign is a separate concern).
- Stereo cross-user grouping (already out of scope from the multi-backend spec).
- Mobile-specific UX changes (header inherits existing mobile cluster behavior).

---

## 7. Phasing

One PR (bundled into PR #16). Suggested commit boundaries:

1. **Server: activation tracking** — `BrowserSpeakerBackend._active_connections`, `activate`/`deactivate` methods, disconnect event subscription, multi-user `list_speakers()` return shape.
2. **Server: SpeakerService wiring** — new WS RPCs, role-aware list filtering, cross-user permission gate, `my browser` aliases in `resolve_speaker_name`, AI tool description updates.
3. **Server: echo refactor** — drop the user-metadata pref, gate echo on activation registration.
4. **Frontend: `useBrowserSpeaker` hook + activation sync** — new hook replaces `useBrowserEchoPref` and `useBrowserSpeakerClips`, wired into `AppShell` provider.
5. **Frontend: `<BrowserSpeakerControl />`** — header component, popover with toggle + history.
6. **Frontend: chat cleanup** — delete `BrowserAudioBubbles`, remove its render in `MessageList`.
7. **Docs** — update `docs/architecture/speaker-system.md` to reflect the new model.

Each commit ships green. After commit 7, smoke-test manually, then bundle into PR #16's squash-merge.

---

## 8. Open questions

None remaining — all resolved during brainstorming.
