# Robust Screens — Design

**Date:** 2026-05-27
**Status:** Approved (design)

## Background

The "screens" feature turns browser tabs on monitors into push targets. A user
opens `/screens`, names the tab, and AI tools push documents / text / images to
the named screen over Server-Sent Events (SSE). Backend lives in
`src/gilbert/core/services/screens.py` (`ScreenService`) with HTTP routes in
`src/gilbert/web/routes/screens.py`; the frontend is a single component,
`frontend/src/components/screens/ScreensPage.tsx`, routed at `/screens` in
`frontend/src/App.tsx`.

The feature is currently broken and thin. This design makes it robust, removes a
dead concept, and adds an opt-in unauthenticated path for setting up a screen.

## Goals

1. Fix the bug where content never displays (spinner shows, nothing loads).
2. Replace the app navigation header on `/screens` with a slim title bar that
   names what is currently displayed.
3. Reach `/screens` from the System menu.
4. Add a setting to allow creating a screen without logging in; surface it on the
   login page and in the menu.
5. Remove the unused `default_url` concept.

## Non-goals

- Persisting displayed content across SSE reconnects (a reconnect resets the
  screen to idle). Separable; not in this change.
- Reworking the AI `display` tool's behavior or the temp-file lifecycle.

---

## 1. Bug fix: SSE event-name / payload mismatch

**Root cause.** The backend emits SSE events named `show_document`, `show_text`,
`show_images`, `clear`, `loading`, `error`. The frontend's `EventSource`
listeners are registered for `display_pdf`, `display_image`, `display_markdown`,
`display_url`, `idle`, `loading`, `error`. Only `loading` and `error` match — so
the spinner appears (loading matches) but no content event is ever handled.
Payload fields also differ: the frontend reads `data.url` while the backend sends
`serve_url`.

**Fix.** Align the frontend to the backend contract (the backend names are the
shared contract used by the AI `display` tool and the existing service tests).
After the change the frontend listens for, and renders:

| Event           | Payload                                                   | Render                                  |
|-----------------|-----------------------------------------------------------|-----------------------------------------|
| `show_document` | `{title, content_type: "pdf"\|"image"\|"other", serve_url}` | `pdf`/`other` → `<iframe src=serve_url>`; `image` → `<img src=serve_url>` |
| `show_text`     | `{title, content}`                                        | `<MarkdownContent content=...>`         |
| `show_images`   | `{title, images: [{url, caption?}]}`                      | image gallery (NEW — not handled today) |
| `clear`         | `{}`                                                      | back to idle                            |
| `loading`       | `{message}`                                               | spinner **+ message text beneath it**   |
| `show_error`    | `{message}`                                               | error view                              |

**Robustness rename.** The backend's `error` event collides with `EventSource`'s
native `error` (transport failures, fired on `onerror` and on an `"error"`
listener). Rename the server-pushed error event `error` → `show_error` on both
ends so the named-event handler and transport-error handler never alias. The
native transport error continues to be handled by `es.onerror`, which shows a
"reconnecting" indicator without clobbering already-displayed content.

## 2. Remove `default_url`

Delete every reference:

- `ConnectedScreen.default_url` field (`screens.py`).
- `ScreenService.connect(name, default_url=...)` → `connect(name)`.
- `_screens_stream` route `default_url` query param (`routes/screens.py`).
- `push_clear` no longer attaches `default_url` to the `clear` payload.
- `list_screens` no longer emits `default_url`.
- Frontend: remove `defaultUrl` state, the "Default URL" input, the
  `default_url` query-string param.
- Update `tests/unit/test_screen_service.py` references.

## 3. Remove the app header → slim title bar

**Routing.** Move `/screens` out of `<ProtectedRoute>`/`<AppShell>` in
`App.tsx`, making it a top-level route alongside `/auth/login` and
`/setup-https`. This removes the nav `TopBar` from the screen and lets the page
manage its own auth gating (see §4).

**Layout.** The connected page is a full-height `flex-col`:

```
┌────────────────────────────────┐
│  <title>                       │  slim title bar (border-b, ~h-10, truncate)
├────────────────────────────────┤
│                                │
│        <content area>          │  flex-1, overflow
│                                │
└────────────────────────────────┘
```

- Title bar text: **content title** while displaying; **screen name** while idle,
  loading, or error.
- Content area states: setup (centered card) · idle ("Waiting for content…") ·
  loading (spinner + message) · display (pdf/image/markdown/gallery) · error.
- Setup state shows only the centered card (screen-name input + Connect), no
  title bar yet, and no default-URL field.

## 4. Setting + access model

**Config.** Add `allow_guest_screens: bool` (default `False`) to
`ScreenService.config_params()` (namespace `screens`, category `Infrastructure`).
Load it in `start()` and `on_config_changed()`; expose a read-only
`allow_guest_screens` property for the web layer.

**Backend enforcement.** `GET /screens/stream` rejects with **403** when the
request's `UserContext` has no roles (unauthenticated `SYSTEM`) **and**
`allow_guest_screens` is off. Logged-in users and local guests (role `everyone`)
pass; with the setting on, anyone passes. The endpoint stays in the middleware
allowlist so the rejection is a clean JSON 403 rather than a 302→login HTML
redirect (which would break the `EventSource`).

**Public info endpoint.** Add `GET /screens/info` → `{enabled,
allow_guest_screens}`, added to `_PUBLIC_EXACT` in `web/auth.py`. Consumed by the
login page and the screens page to decide UI without authentication.

**Page gating** (ScreensPage, now outside `ProtectedRoute`), using `useAuth` +
`/screens/info`:

| Condition                                    | Shows                                   |
|----------------------------------------------|-----------------------------------------|
| `!enabled`                                   | "Screens are disabled."                 |
| logged-in or local guest (`user` truthy)     | setup form                              |
| logged-out and `allow_guest_screens`         | setup form                              |
| logged-out and `!allow_guest_screens`        | "Sign in to set up a screen" + login link |

## 5. Login-page button

In `LoginPage.tsx`, fetch `/screens/info`. When `enabled && allow_guest_screens`,
render a **"Set up a screen"** button (below the login methods) that navigates to
`/screens`.

## 6. System → Screens menu item

In `web_api.py` `_ws_dashboard_get`, add to the `system` group's `items`:

```python
{
    "label": "Screens",
    "description": "Remote display screens",
    "url": "/screens",
    "icon": "monitor",
    "required_role": screens_role,        # see below
    "requires_capability": "screen_display",
}
```

`screens_role` is computed from the screens service before the nav list is built:
`"everyone"` when `allow_guest_screens` is on, else `"user"`. Effects:

- Setting off → visible to logged-in users/admins (normal menu entry).
- Setting on → also visible to local guests (who receive nav only when the global
  guest policy is on).
- `requires_capability: "screen_display"` hides it when the service is disabled
  (relies on the existing `svc.enabled` check in the nav `_visible` filter).

## Testing

- `tests/unit/test_screen_service.py`: drop `default_url` assertions; assert
  `push_clear` payload no longer includes `default_url`; assert `list_screens`
  shape; add `allow_guest_screens` config load + `on_config_changed`.
- New route tests: `/screens/info` payload; `/screens/stream` 403 when
  unauthenticated and setting off, 200/stream when setting on or authenticated.
- Frontend: `tsc`/build passes; manual smoke that each event type renders.

## Files touched

**Backend**
- `src/gilbert/core/services/screens.py` — remove `default_url`; rename `error`
  event → `show_error`; add `allow_guest_screens` config + property.
- `src/gilbert/web/routes/screens.py` — drop `default_url` param; add
  `/screens/info`; enforce gate on `/screens/stream`.
- `src/gilbert/web/auth.py` — add `/screens/info` to `_PUBLIC_EXACT`.
- `src/gilbert/core/services/web_api.py` — add System → Screens nav item with
  dynamic `required_role`.

**Frontend**
- `frontend/src/App.tsx` — move `/screens` to a top-level route.
- `frontend/src/components/screens/ScreensPage.tsx` — title bar layout, corrected
  event listeners + payloads, loading message, gallery, page gating, remove
  default-URL.
- `frontend/src/components/auth/LoginPage.tsx` — "Set up a screen" button.

**Tests**
- `tests/unit/test_screen_service.py` and new route tests.
