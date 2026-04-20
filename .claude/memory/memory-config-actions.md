# Config Actions (Settings Page Action Buttons)

## Summary
Mechanism for services and backends to advertise one-click action
buttons on their settings page — used for things like "Test connection",
"Link Spotify for search", "Re-discover". Built on top of the existing
`Configurable` protocol.

## Details

### Dataclasses (`interfaces/configuration.py`)

- `ConfigAction` — describes a button. Fields: `key` (unique within the
  service), `label`, `description` (tooltip), `backend_action` (set
  automatically when a service merges backend-declared actions),
  `confirm` (optional prompt), `required_role` (defaults to `"admin"`).
- `ConfigActionResult` — the return value of invoking an action.
  Fields: `status: "ok" | "error" | "pending"`, `message` (toast),
  `open_url` (UI opens in a new tab), `followup_action` (the button
  relabels to "Continue" and the next click invokes this key), `data`
  (free-form JSON dict — see the `persist` side-channel below).

### Protocols

- `ConfigActionProvider` — `@runtime_checkable` Protocol for services
  that expose actions. Two methods: `config_actions()` returning a
  `list[ConfigAction]`, and `async invoke_config_action(key, payload)`
  returning a `ConfigActionResult`. Implementing the methods is
  sufficient — no inheritance required.
- `BackendActionProvider` — the parallel Protocol for backends. Same
  shape. Concrete backends typically implement `backend_actions()` as
  a `@classmethod` so the settings UI can list actions before the
  backend is initialized.

### Service forwarding helpers

`src/gilbert/core/services/_backend_actions.py` captures the "forward
to backend" pattern so every service-with-a-backend doesn't re-implement
it:

```python
from gilbert.core.services._backend_actions import (
    invoke_backend_action, merge_backend_actions,
)

def config_actions(self) -> list[ConfigAction]:
    with contextlib.suppress(ImportError):
        import gilbert.integrations.foo  # noqa: F401
    fallback_cls = FooBackend.registered_backends().get(self._backend_name)
    return merge_backend_actions(self._backend, fallback_cls)

async def invoke_config_action(self, key, payload):
    return await invoke_backend_action(self._backend, key, payload)
```

The `fallback_cls` probe lets the settings UI show action buttons even
when the service hasn't started yet — e.g. a disabled music service
should still advertise "Link Spotify for search" so admins can set it
up before enabling the service.

### ConfigurationService wiring

- `describe_categories()` adds an `actions: list[dict]` per section,
  populated from `svc.config_actions()` when the service is a
  `ConfigActionProvider`.
- New WS RPC `config.action.invoke` (`_ws_action_invoke`): validates
  the namespace, RBAC-checks against the action's `required_role`
  via `AccessControlProvider.get_role_level`, then calls
  `invoke_config_action(key, payload)` and returns a serialized
  `ConfigActionResult`.
- The `data["persist"]` side-channel: when a backend returns
  `ConfigActionResult.data = {"persist": {"settings.auth_token": ...}}`,
  the frontend picks it up and writes those values through the normal
  `config.section.set` RPC. This is how the Sonos link flow persists
  its SMAPI auth token without the backend needing a
  `ConfigurationReader`.

### Frontend

- `frontend/src/types/config.ts` — `ConfigActionMeta`,
  `ConfigActionResult`, `ConfigActionInvokeResponse` types; `actions?`
  added to `ConfigSection`.
- `frontend/src/hooks/useWsApi.ts` — `invokeConfigAction(namespace,
  key, payload)` wrapper.
- `frontend/src/components/settings/ConfigSection.tsx` — renders an
  "Actions" row below backend settings. Per-action state machine:
  idle → running → (ok|error|pending). On `pending`, if `open_url` is
  set it opens in a new tab and if `followup_action` is set the button
  relabels to "Continue" and remembers the next key to invoke. On the
  user's next click, the followup key is sent (bypassing the confirm
  prompt since the user already saw it). `ok` messages auto-clear after
  5s; errors stay up until the next click.

### Backends with test_connection (initial rollout)

All return ok/error + human-legible messages, making cheap upstream
calls (no writes, no large payloads):

- `anthropic_ai.py` — 1-message generate, reports model name
- `anthropic_vision.py` — 16-token text-only chat
- `elevenlabs_tts.py` — `list_voices()` (no synthesis credits used)
- `sonos_speaker.py` — reports how many S2 speakers the zeroconf browser has discovered and how many WebSocket connections are live
- `tavily_search.py` — 1-result search
- `ngrok_tunnel.py` — reports current `public_url` as `open_url`
- `gmail.py` — `users().getProfile("me")`, reports email + msg count
- `google_auth.py` — validates client_id/client_secret presence only
- `google_directory.py` — `users().list(maxResults=1)`
- `unifi/presence.py` — re-logs-in to each configured UniFi host,
  aggregates per-host results
- `unifi/doorbell.py` — client login + `list_cameras()`
- `sonos_music.py` — hits Spotify Web API `GET /me` to verify the refresh token is valid and the app config works

### Backends with non-test actions

- `sonos_music.py` has both `test_connection` and `link_spotify` (the
  two-phase SMAPI auth flow that drove the infrastructure design).

## Related
- [Configuration Service](memory-configuration-service.md) — hosts the
  WS RPC and merges the actions into the describe response.
- [Music Service](memory-music-service.md) — the feature that drove
  this mechanism; its `link_spotify` is the canonical multi-step flow.
