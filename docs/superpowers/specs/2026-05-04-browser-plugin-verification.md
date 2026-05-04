# Browser Plugin Verification Findings

## 0.1 Workspace attachment lifecycle for tool-produced files

**No public "bytes-in, FileAttachment-out" helper exists.** The `_tool_attach_workspace_file` method (`src/gilbert/core/services/workspace.py:1878-2006`) is the canonical recipe, but it is a private tool executor that takes a `path` already on disk — it does not accept raw bytes. A plugin therefore composes the lifecycle out of the public `WorkspaceProvider` primitives.

**Capability protocol exists.** `WorkspaceProvider` lives at `src/gilbert/interfaces/workspace.py:10` and is `@runtime_checkable`. Std-plugins must use it via:

```python
from gilbert.interfaces.workspace import WorkspaceProvider
ws = resolver.get_capability("workspace")
if not isinstance(ws, WorkspaceProvider):
    return ToolResult(..., is_error=True)
```

(see `src/gilbert/core/services/ai.py:2966-2969` and `src/gilbert/web/routes/chat_uploads.py:104-119` for the canonical pattern). The protocol exposes `get_output_dir(user_id, conv_id) -> Path` (line 36) and `register_file(...) -> dict` (line 50) — exactly the two pieces a screenshot tool needs.

**Recipe for a PNG screenshot tool** (mirroring `_tool_attach_workspace_file` lines 1948-1996):

```python
user_id = arguments["_user_id"]                          # injected by AIService
conv_id = arguments["_conversation_id"]                  # ai.py:3328-3334, 3679-3689
out_dir = ws.get_output_dir(user_id, conv_id)            # creates outputs/
dest = out_dir / "screenshot.png"                        # de-dupe with -1, -2 if exists
dest.write_bytes(png_bytes)
entity = await ws.register_file(
    conversation_id=conv_id, user_id=user_id,
    category="output", filename=dest.name,
    rel_path=f"outputs/{dest.name}", media_type="image/png",
    size=len(png_bytes), created_by="ai",
)
attachment = FileAttachment(
    kind="image", name=dest.name, media_type="image/png",
    workspace_skill="workspace",                # literal string, not a skill name
    workspace_path=f"outputs/{dest.name}",      # POSIX, relative to workspace_root
    workspace_conv=conv_id,
    workspace_file_id=entity["_id"],
    size=len(png_bytes),
)
return ToolResult(tool_call_id="", content="Captured screenshot.",
                  attachments=(attachment,))
```

**Tool-args injection.** The AI service writes `_user_id` and `_conversation_id` into the arguments dict before dispatch (`src/gilbert/core/services/ai.py:3328-3334` and `:3679-3689`). The plugin's tool reads both directly; it does NOT declare them as `ToolParameter`s — they are stripped from the schema sent to the model and re-injected at execution time.

**FileAttachment shape.** `kind="image"` triggers Anthropic image-block rendering, but for reference-mode the bytes are loaded from disk at send time (`src/gilbert/interfaces/attachments.py:72-81`). `workspace_skill` is the literal `"workspace"` string (not the plugin name); `workspace_path` is POSIX and relative to `get_workspace_root()`; `workspace_conv` MUST be set so the download handler picks the conversation-scoped tree (`attachments.py:17-25`); `workspace_file_id` is the `_id` returned by `register_file()` and lets the download handler resolve via the registry instead of reconstructing the path.

## 0.2 Per-user UserContext propagation into ToolProvider

**`UserContext` reaches `ToolProvider.execute_tool` only through injected magic keys on the `arguments` dict.** The protocol signature in `src/gilbert/interfaces/tools.py:146` is `async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str` — no `user_ctx` parameter. The AI service performs the injection just before dispatch:

```python
# src/gilbert/core/services/ai.py:3326-3337
arguments: dict[str, Any] = dict(tc.arguments)
if user_ctx is not None:
    arguments["_user_id"] = user_ctx.user_id
    arguments["_user_name"] = user_ctx.display_name
    arguments["_user_roles"] = list(user_ctx.roles)
    if user_ctx.email:
        arguments["_user_email"] = user_ctx.email
if conv_id:
    arguments["_conversation_id"] = conv_id
arguments["_invocation_source"] = "ai"
if room_members is not None:
    arguments["_room_members"] = room_members
```

The same injection happens in the slash-command path at `ai.py:3679-3689`. Tools therefore read `arguments.get("_user_id")` and `arguments.get("_conversation_id")` directly. They do NOT declare these as `ToolParameter`s — the underscore prefix marks them as not part of the JSON Schema sent to the model.

**Caveat for `get_tools()`** — that method DOES receive `user_ctx` (`tools.py:131-144`), useful when the visible tool set varies per user. The browser plugin's tool list is uniform, so it can ignore `user_ctx` here.

**Return shape.** `execute_tool` returns `str | ToolOutput | ToolResult`; the AI service normalizes all three (`ai.py:3375-3400`). The browser plugin returns `ToolResult` for the screenshot tool (so it can attach a `FileAttachment`) and plain `str` for everything else.

**Implication for `BrowserService`.** Key the per-user `BrowserContext` cache by `arguments["_user_id"]`. Reject tool calls without that key (system/anonymous invocation) with an error string — there's no global "shared" context.

## 0.3 ConfigParam options for binary toggles + `restart_required` on Service plugins

**`ConfigParam` covers everything we need.** From `src/gilbert/interfaces/configuration.py:12-50`:

```python
@dataclass
class ConfigParam:
    key: str
    type: ToolParameterType
    description: str = ""
    default: Any = None
    restart_required: bool = False   # line 23
    sensitive: bool = False           # line 24 — masks in WS responses
    multiline: bool = False           # line 28 — STRING textarea in UI
    ai_prompt: bool = False           # line 34 — flags as AI-prompt for /author button
    choices: list[str] | None = None  # static dropdown options
    choices_from: str | None = None   # dynamic dropdown source ("ai_profiles", etc.)
```

`ToolParameterType` includes `BOOLEAN`, `INTEGER`, `NUMBER`, `STRING`, `ARRAY`, `OBJECT` (`tools.py:15-23`) — so all the `BrowserService` knobs map cleanly:

- `idle_timeout_seconds` → `INTEGER`, default `600`
- `max_concurrent_users` → `INTEGER`, default `8`
- `vnc_*` ints
- `extraction_prompt` / `login_form_heuristics_prompt` → `STRING` + `multiline=True` + `ai_prompt=True`

**Service-level `Configurable`.** Services (not just Backends) implement `Configurable` from `interfaces/configuration.py:103-`. The hooks: `config_namespace` (string property), `config_category` (UI grouping label), `config_params() -> list[ConfigParam]`, `on_config_changed(section: dict[str, Any]) -> None`. Cache values in `on_config_changed` onto `self._foo` and read those everywhere; never re-read defaults from constants. The pattern is the same as other Service-level Configurables (search `presence.py`, `notifications.py`, `agent.py`).

**`restart_required` semantics.** When True, the configuration service triggers `restart_service(name)` after `on_config_changed`. The browser plugin should mark `idle_timeout_seconds` and `max_concurrent_users` as live-tunable (no restart); `vnc_*` knobs likewise live-tunable since they're consulted at session-start time. The `extraction_prompt` and `login_form_heuristics_prompt` are also live-tunable — they're read fresh on each tool call.

**Sensitive handling.** Browser passwords are NOT going through ConfigParam — they're entity rows in `browser_credentials`, encrypted with the plugin-owned Fernet key (see 0.5). ConfigParam's `sensitive=True` masking is for single-value secrets like API keys.

## 0.4 Static-asset serving from a std-plugin directory

**The web layer is FastAPI.** `src/gilbert/web/__init__.py:16-127` builds the app and mounts routers from `src/gilbert/web/routes/*.py`. There is **no per-plugin static-mount hook** — plugins normally only reach the web layer through WS RPCs (the `WsHandlerProvider` protocol). Custom HTTP/WS routes have to be added under `src/gilbert/web/routes/` and wired up in `web/__init__.py:64-87`.

**Decision for the browser plugin.** Add a new `src/gilbert/web/routes/browser.py` module that:

1. Mounts `StaticFiles(directory=<path-to-novnc>)` for the noVNC client at `/api/browser/novnc/`. The vendored client lives at `std-plugins/browser/static/novnc/`; the route resolves the absolute path via `gilbert.plugins` lookup (or `Path("std-plugins/browser/static/novnc").resolve()` against `app.state.gilbert.repo_root`).
2. Defines a FastAPI websocket endpoint at `/api/browser/vnc/{session_id}/ws` that:
   - Pulls `app.state.gilbert` (`web/__init__.py:21`).
   - Resolves the `browser` capability via the gilbert service manager.
   - Calls a `validate_session_for_user(session_id, request_user_id)` method on `BrowserService` to authorize the connection.
   - Pipes bytes both ways between the client websocket and a TCP socket on `127.0.0.1:<websockify_port>` returned by `BrowserService.get_websockify_port(session_id)`. Use `asyncio.open_connection` and two `asyncio.create_task`s for the two pipe directions.

**Auth.** `AuthMiddleware` (`web/auth.py`, mounted at `web/__init__.py:55`) tags every request with a `UserContext` on `request.state`; FastAPI websocket endpoints get the same context via `websocket.state` after middleware runs. The route reads `request.state.user_ctx.user_id` and passes it to `validate_session_for_user`. RBAC for the route itself is at user level (matches `acl.py` policy for `notification.*` and `agent.*`); the per-session ownership check is the real gate.

**Existing pattern to mirror.** `src/gilbert/web/routes/screens.py:65-73` shows the canonical `FileResponse` plus auth-resolved-path pattern; `src/gilbert/web/routes/websocket.py` shows how the WS RPC router pulls `gilbert` out of `app.state` (so the new browser route can do the same to reach the service).

**Layer-rule note.** The browser route in `src/gilbert/web/routes/` imports concrete classes only via `app.state.gilbert.services.get_capability("browser")` and `isinstance(svc, BrowserSessionLookup)` against a small `@runtime_checkable Protocol` declared in `src/gilbert/interfaces/browser.py` (we'll add that). That keeps the web layer dependent on `interfaces/`, not on `std-plugins/browser/`.

## 0.5 Encrypted-at-rest patterns already in core

**No existing Fernet/cryptography usage.** A `grep -rn "Fernet\|cryptography\|encrypt"` over `src/` and `std-plugins/` returns nothing. Gilbert does not currently encrypt anything at rest — the SQLite database under `.gilbert/gilbert.db` is plaintext, with `sensitive=True` ConfigParam values relying on filesystem permissions plus WS-response masking (`configuration.py:584-600`).

**Decision: plugin-local key, generated on first start.** The browser plugin owns its own Fernet key:

- Path: `<plugin_data>/fernet.key` where `<plugin_data>` is `context.data_dir` from `PluginContext` (resolves to `.gilbert/plugin-data/browser/fernet.key`).
- First start: if the file is absent, generate via `Fernet.generate_key()`, write atomically (`os.open(path, O_WRONLY|O_CREAT|O_EXCL, 0o600)` then `write` then `close`), chmod 600.
- Subsequent starts: read bytes, instantiate `Fernet(key)`.
- The key file is gitignored by virtue of the existing `.gilbert/` blanket ignore rule.

**Why not piggyback on the bootstrap config?** `.gilbert/config.yaml` is bootstrap-only and not the right home for a per-plugin secret; the plugin already has a per-installation `data_dir` for exactly this kind of state. Keeping the key plugin-local also means a future credential service can reuse the same pattern without conflicting.

**Threat model captured.** Plaintext-at-rest for credentials is rejected because the SQLite DB is the easiest exfiltration target during backup/migration. With Fernet, the DB rows are inert without `fernet.key`, raising the bar from "open the DB" to "open the DB AND read a 0o600 file in the same data dir." This isn't airtight (an attacker on the host can read both), but it ensures the credential rows are useless if the DB leaks alone.

**Caveat to document in the README.** Losing `fernet.key` makes all browser credentials unrecoverable. Backup procedure: include `<data_dir>/browser/` in any `.gilbert/` backup, or re-enter passwords through the Settings UI.

## 0.6 Existing capability protocol candidates for the credential store

**There is no `credentials` capability producer.** Even though `presence.py:66` and `doorbell.py:66` declare `"credentials"` in their `optional` capability set, no service in `src/gilbert/core/services/` or `std-plugins/` registers `"credentials"` as a capability — `grep -rn "capabilities.*credentials\|provides.*credential\|CredentialStore"` returns no producers. The credential MODELS at `src/gilbert/interfaces/credentials.py` (`ApiKeyCredential`, `ApiKeyPairCredential`, `UsernamePasswordCredential`) exist, but no service wraps them.

**Decision: keep credentials plugin-local.** Per CLAUDE.md "shared data lives in `interfaces/`" applies only when a *second* consumer exists. There is no second consumer today, so:

- The `browser_credentials` entity collection lives in the plugin's namespaced storage (`PluginContext.storage`, auto-prefixed `gilbert.plugin.browser`).
- The `BrowserCredential` dataclass lives in `std-plugins/browser/credentials.py`.
- `CredentialStore` exposes `save / get / list_for_user / delete / list_sites` methods directly on the service object — no capability protocol registration.
- WS RPCs go through the plugin's own `WsHandlerProvider` and gate access via the calling user's `UserContext` (already attached to the WS connection).

**Future hoist path.** When a second consumer wants password storage (e.g., a hypothetical SMTP plugin), the right move is to extract a `CredentialStoreService` in `core/services/` declaring `capabilities=frozenset({"credentials"})`, hoist `BrowserCredential` to a `interfaces/credentials.py` extension, and have the browser plugin become a thin client. We don't pre-build that abstraction now.

**Implication.** The plugin imports directly from `gilbert.interfaces.credentials` for the `UsernamePasswordCredential` model if it wants type alignment, but isn't required to. Internally we use a richer dataclass that includes per-site selectors and label.

## 0.7 Playwright headless requirements (system packages, browser binary)

**Two-step install.**

1. `uv sync` (run from Gilbert root) installs the Playwright Python package via the std-plugins workspace member and resolves it into the shared venv. This does NOT install the Chromium browser binary.
2. `uv run playwright install chromium` downloads the actual browser binary into Playwright's per-user cache (typically `~/.cache/ms-playwright/`). Required once per host.

**Headless host packages (Linux).** Playwright's bundled Chromium needs these shared libs at runtime:

```
libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdbus-1-3
libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2
libgbm1 libpango-1.0-0 libcairo2 libasound2
```

`uv run playwright install-deps chromium` will install them on Debian/Ubuntu (sudo required). On Arch/RHEL the user installs the equivalent packages manually. macOS and Windows have everything bundled.

**VNC live-login extra packages.** The headed-browser path additionally needs:

```
xvfb x11vnc websockify
```

Each is a standard apt/dnf package. We do NOT bundle these — the README documents the install steps and the plugin emits a clear error string if the binaries are missing on PATH.

**Surface to the user.**

- `std-plugins/README.md` browser section lists both install commands as required steps.
- `gilbert.sh browser-doctor` (Phase 7, optional) sanity-checks PATH for `playwright`, the chromium binary inside the cache, and (for VNC) `Xvfb` / `x11vnc` / `websockify`. Prints PASS/FAIL per line, exits non-zero on any FAIL.
- `BrowserService.start()` does NOT crash if `playwright install chromium` hasn't been run; it logs a warning and lets the first tool call return a descriptive error. This avoids breaking Gilbert startup for users who installed the plugin but haven't yet provisioned the browser binary.

**Resource budget.** A headless Chromium uses roughly 100-150 MB resident per BrowserContext under typical workloads. With `max_concurrent_users=8` (the default), worst-case memory is ~1.2 GB. Document this in the README and let operators tune down on small hosts.
