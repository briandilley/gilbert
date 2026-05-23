# Feature 09: Google Credentials — Any-Account Access without Workspace

## Summary

Refactors the `google` std-plugin's credential plumbing so Gilbert's Gmail,
Calendar, Drive, and Tasks data backends work with an **ordinary, free
`@gmail.com` account** through OAuth: no Google Workspace, no Admin console,
no domain-wide delegation. A single **plugin-internal**
`google_credentials` module sits between each backend's `initialize()` and
`google.oauth2`, resolving one of three credential **modes**:

- **`oauth_bot`** *(default for new setup)* — one ordinary Google account,
  linked once through Google's OAuth consent screen. The refresh token is
  persisted in backend config and silently refreshed. This is the required
  mode for Gmail-as-its-own-mailbox and the recommended path for personal
  Google accounts.
- **`delegated_service_account`** *(legacy, unchanged)* — today's Workspace +
  domain-wide-delegation path (`creds.with_subject(delegated_user)`), kept for
  backwards compatibility. Legacy configs with `service_account_json` plus
  `delegated_user` infer this mode when `credential_mode` is absent.
- **`shared_service_account`** — a service-account identity with no
  `with_subject()` delegation. Supported only by Calendar and Drive, where
  Google lets a user share a calendar or folder with the service-account
  email address. Gmail and Tasks return an actionable unsupported-mode error.

OAuth setup is surfaced through existing `ConfigAction` buttons:
`connect_google`, `connect_google_complete`, and `test_connection`. Actions
return `data.persist` values that the UI applies to the current unsaved form
before the admin clicks Save.

The change is small and safe by construction: it extracts existing credential
branching into a named resolver, adds one additive auth mode, and changes
**no existing user-visible behaviour** — a legacy `service_account_json` +
`delegated_user` config keeps working byte-for-byte. It introduces **no new
core ABC, no capability protocol, no new core service**; the abstraction is
plugin-internal because all three consumers (Gmail, Drive, the future
Calendar backend) live in the same `google` plugin and `core/` must stay
vendor-free. The single core-repo touch is one thin web route used *only* by
the optional web-redirect OAuth path.

This unblocks spec #01. `01-calendar.md`'s Summary already asserts
`GoogleCalendarBackend` is "hosted inside the existing `google` std-plugin so
it shares the OAuth machinery already wired up there for Gmail and Drive" —
that machinery does **not** exist yet. #09 builds it and defines the contract
#01's backend consumes, so #01 ships against a real foundation instead of an
over-claim.

## Motivation

The `google` plugin's three data backends all hard-require a Google
**service-account JSON key** *and* **domain-wide delegation**:

- `gmail.py:152-157` — `service_account.Credentials.from_service_account_info(sa_info, scopes=...)`
  then `creds = creds.with_subject(delegated_user)`.
- `gdrive_documents.py:159-164` — the identical pattern.
- `google_directory.py` — same again (intentionally untouched; see Out of
  scope).

Domain-wide delegation is a Google **Workspace** feature. Authorizing a
service account to impersonate a user (`with_subject()`) requires an
administrator to register the SA's client ID with explicit scopes in the
**Workspace Admin console** (Security → API Controls → Domain-wide
delegation). A plain `@gmail.com` consumer account **has no Admin console and
cannot grant domain-wide delegation at all**. The practical consequence:
today, a home user with a personal Gmail account literally cannot use
Gilbert's inbox or Drive knowledge ingestion. The integration — despite
being framed as "self-contained" — is gated on being a paying Workspace
customer with admin rights.

There is a latent escape hatch already in the code: when `delegated_user` is
empty, both backends *skip* the `with_subject()` call (`gmail.py:156`,
`gdrive_documents.py:163` — `if delegated_user:`), leaving a plain
service-account identity. A service-account identity is itself a Google
principal with its own email address; a free user can *share* a Calendar or a
Drive folder with that email through the normal consumer sharing UI, exactly
as they would with a person. That path works on free accounts **today** — but
it is undocumented, untested, has no UX, and `std-plugins/README.md` actively
steers users the other way (it lists `delegated_user` as a plain field with
no hint that empty has meaning). One further bug obscures it: `gmail.py:133`
defaults `delegated_user` to `email_address`, so leaving the field blank in
the UI *still* silently attempts delegation. #09 turns the accident into the
primary, documented, validated path and fixes that default.

**Why now:** spec #01 (Calendar) is about to land on `feature/01-calendar`.
Its Summary and Motivation both assert the `google` plugin "already owns OAuth
credentials, service-account JSON, and domain-wide delegation, so the
marginal cost of a Calendar backend is low." Two of those three are real; the
*OAuth* machinery and any non-Workspace story are not. #01's
`GoogleCalendarBackend` config block copies the `service_account_json` +
`delegated_user` shape verbatim — so if #01 ships first, Calendar inherits the
exact Workspace lock-in this spec exists to remove, and #01's own deferred
"if we ever add user-OAuth credentials we'll need a refresh-token store"
becomes a hard, unmet dependency. Shipping #09 first means #01's backend
consumes the shared resolver and #01's Summary stops being aspirational.

## Scope

### In scope

- A plugin-internal `google_credentials` module in `std-plugins/google/`
  (a `GoogleCredentialMode` enum, a `GoogleCredentialSpec` frozen dataclass,
  a `build_google_credentials(spec)` factory, OAuth-bot helpers, and a
  `share_with_email(spec)` extractor). **Not** a core ABC — it is
  Google-specific glue consumed only inside the `google` plugin (see
  §Architecture → Layer Decisions for the rationale).
- Three credential modes: `shared_service_account` (primary), `oauth_bot`
  (fallback), `delegated_service_account` (legacy, unchanged).
- Refactor of `GmailBackend.initialize()` and
  `GoogleDriveDocumentBackend.initialize()` to obtain credentials via the
  resolver instead of inlining `from_service_account_info` / `with_subject`.
  The transport-rebuild paths (`_rebuild_service`) become OAuth-token-refresh
  aware.
- OAuth-bot linking flow: **loopback redirect with PKCE as the default**
  (an in-process, ephemeral `127.0.0.1` listener owned by the plugin — no
  public URL, no core route); automatic upgrade to a **tunnel-hosted web
  redirect** when a `TunnelBackend` is configured (mirrors
  `GoogleAuthBackend.get_callback_url`); **manual code paste** as the
  headless fallback (the shape the Sonos/Spotify backend already uses).
  Refresh token persisted to backend config; access token auto-refreshed.
- An **admin-only in-app setup wizard** — a plugin-shipped SPA panel under
  `std-plugins/google/frontend/`, declared via `Plugin.ui_panels()`
  (`required_role="admin"`): deep links to the GCP Console, exact ordered
  steps per mode, a live **Test connection** validator (built on the
  existing per-backend `ConfigAction` mechanism) that surfaces the
  share-with email, and a read-only **Status** tab.
- Documentation: root `README.md`, `std-plugins/README.md` (google row,
  the three modes, the plaintext-at-rest gap note), `std-plugins/CLAUDE.md`
  (only if a new plugin convention is set), and the adjacent
  `docs/architecture/*.md`.

### Out of scope (explicit non-goals for this PR)

- **`google_directory` non-Workspace support.** The Workspace Directory
  backend stays service-account + domain-wide-delegation. It syncs a
  *Workspace domain's* user list — a concept with no free-account analogue.
  Explicit non-goal; not refactored, not deprecated.
- **Encryption-at-rest for `backend_config` secrets.** OAuth refresh tokens
  and SA JSON remain plaintext in SQLite, the same shape Gmail uses today.
  This is a **locked, deferred cross-cutting decision**
  (`OPEN_QUESTIONS.md` → "Decisions Locked Before Spec PR → Encryption-at-rest:
  DEFER"; #01 §Open Questions #8). #09 is the first new credential surface in
  the initiative, so it **documents** the gap in `std-plugins/README.md` and
  does not relitigate it.
- **Per-user OAuth** (each Gilbert user links their *own* Google account).
  v1 is operator-scoped: one shared SA *or* one OAuth-bot account per
  backend, configured by an admin. Per-user OAuth is a different feature with
  its own isolation model — recorded as a designed-for future opt-in, not
  built here. The resolver contract is forward-compatible with it (a future
  per-user token store is just another producer of a `GoogleCredentialSpec`).
- **Shipping a Gilbert-owned, Google-verified OAuth client.** v1 requires the
  operator to create their own GCP client (the wizard makes this a guided
  ~5-minute task). Whether the project should publish a verified,
  CASA-audited client so end users do *zero* GCP setup is a
  governance/maintainer decision (100-user unverified cap; ongoing security
  assessment; the project becomes Google's registered data controller) → it
  goes to `OPEN_QUESTIONS.md`, not into this PR.
- **The Calendar backend itself.** `GoogleCalendarBackend` is owned by spec
  #01. #09 only defines and ships the resolver contract #01's `initialize()`
  will call, and imposes one documented requirement on #01 (the
  `calendarList().insert` enrolment step — see §Architecture). The two
  implementation PRs are sequenced so #09's resolver lands before #01's
  backend (see §Open Questions for the cross-spec reconciliation).
- **A new core ABC / capability protocol / core service / ACL prefix.** The
  resolver is plugin-internal; credential entry, validation, and OAuth
  connect ride the existing per-backend `ConfigParam` + `ConfigAction`
  mechanism. No `src/gilbert/interfaces/` or `src/gilbert/core/` change. The
  *only* `src/gilbert/` touch is one thin web route for the optional
  web-redirect path (see Open Questions #6 for the defer option).
- **Migrating existing deployments off delegation.** A working Workspace +
  delegation config keeps working unchanged (`delegated_service_account`
  mode). No forced migration, no v1 deprecation warning.

## Architecture

### Layer Decisions

| Module | Layer | Justification |
|---|---|---|
| `std-plugins/google/google_credentials.py` | plugin-internal | **New.** Holds `GoogleCredentialMode`, `GoogleCredentialSpec`, `build_google_credentials()`, the OAuth-bot helpers, and `share_with_email()`. Lives in the plugin — **not** `src/gilbert/interfaces/` — because all three consumers (`gmail.py`, `gdrive_documents.py`, the future `google_calendar.py`) are in the *same* `google` plugin, so a sibling internal module is the correct, rule-compliant placement (CLAUDE.md Layer Rule 7: plugins may import their own internal modules). Putting Google-specific credential logic in `core/interfaces/` would violate "only vendor-free abstractions live in core; every third-party integration is a std-plugin." It is a pure value→`Credentials` function plus stdlib + `google.*` + `httpx`; no imports from `gilbert.core/integrations/web/storage`. |
| `std-plugins/google/gmail.py` | plugin | **Modified.** Credential construction (currently `gmail.py:131-160`) routes through the resolver. Still imports only `gilbert.interfaces.*`, `._google_retry`, `.google_credentials`. |
| `std-plugins/google/gdrive_documents.py` | plugin | **Modified.** Same refactor (`gdrive_documents.py:150-167`). |
| `std-plugins/google/plugin.py` | plugin | **Modified.** Gains `GooglePlugin.ui_panels()` returning the wizard `UIPanel`. No new `provides` (the resolver is internal glue, not a registered backend). This is the plugin's first `ui_panels()` and first `frontend/` tree. |
| `std-plugins/google/frontend/*` | plugin | **New.** The wizard SPA panel + `panels.ts` + api hook + types, per `std-plugins/CLAUDE.md` frontend rules. Core SPA never imports it; it mounts via a `<PluginPanelSlot>`. |
| `src/gilbert/web/routes/integrations.py` | `web/` | **New, one thin route**, used *only* by the optional `oauth_bot` web-redirect path. Parses `code`/`state`, calls one plugin-resolved method, formats a redirect — identical discipline to the existing `web/routes/auth.py:106-156` callback. No token exchange, no URL building, no secret handling in the route. **Loopback (default) and manual-paste need zero core change.** May be deferred (Open Questions #6). |
| `src/gilbert/` core | core | **Unchanged.** No new ABC, no capability protocol, no core service, no ACL change. |

The resolver is deliberately **not** a registry-backed `Backend`: it has no
lifecycle, no config namespace, and is consumed *inside* concrete backends —
so the universal backend pattern (`__init_subclass__` registry +
`backend_config_params()`) does not apply. It is a frozen dataclass + a
dispatch function, the simplest swappable shape.

### New / Modified Interfaces

No `src/gilbert/interfaces/` change. The plugin-internal contract:

```python
# std-plugins/google/google_credentials.py
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # google.auth is a plugin dep, never imported at module load
    from google.auth.credentials import Credentials


class GoogleCredentialMode(StrEnum):
    SHARED_SERVICE_ACCOUNT = "shared_service_account"      # primary
    DELEGATED_SERVICE_ACCOUNT = "delegated_service_account"  # legacy Workspace
    OAUTH_BOT = "oauth_bot"                                  # fallback


@dataclass(frozen=True)
class GoogleCredentialSpec:
    """Everything needed to mint a google.auth Credentials object.

    A backend builds this from its own ConfigParams (so each backend keeps
    its own scopes) and hands it to ``build_google_credentials``. Field
    relevance is mode-dependent; irrelevant fields stay empty.
    """

    mode: GoogleCredentialMode
    scopes: tuple[str, ...]
    # service-account modes
    service_account_info: dict[str, Any] = field(default_factory=dict)
    delegated_user: str = ""                       # DELEGATED only
    # oauth_bot mode
    client_id: str = ""
    client_secret: str = ""
    refresh_token: str = ""
    token_uri: str = "https://oauth2.googleapis.com/token"


def build_google_credentials(spec: GoogleCredentialSpec) -> "Credentials":
    """Resolve a spec into a google.auth Credentials object.

    SHARED_SERVICE_ACCOUNT  → service_account.Credentials
        .from_service_account_info(info, scopes=...) — NO with_subject.
        The locked PRIMARY model: the user shared their calendar / Drive
        folder *with the SA's email*; the SA acts as itself, no Workspace.
    DELEGATED_SERVICE_ACCOUNT → same, then .with_subject(delegated_user).
        Legacy Workspace back-compat (today's behaviour).
    OAUTH_BOT → google.oauth2.credentials.Credentials with refresh_token +
        client_id/secret + token_uri; google-auth auto-refreshes.
    """


def share_with_email(spec: GoogleCredentialSpec) -> str | None:
    """The address users must share their Calendar/Drive with.

    SHARED/DELEGATED: the SA JSON's ``client_email``.
    OAUTH_BOT: the connected bot account address (resolved post-link).
    """


# OAuth-bot helpers (httpx, mirrors google_auth.py's exchange shape):
#   make_pkce_pair() -> (verifier, challenge_s256)
#   build_authorization_url(client_id, redirect_uri, scopes, state, challenge)
#   exchange_code(code, verifier, redirect_uri, client_id, client_secret)
#   credentials_from_refresh_token(refresh_token, client_id, client_secret)
```

**Verification of the "already supported when `delegated_user` is empty"
claim.** Confirmed against the real code. `gmail.py:152-157` and
`gdrive_documents.py:159-164` call `from_service_account_info(...)` and only
then `if delegated_user: creds = creds.with_subject(delegated_user)`. So a
plain SA identity (no `with_subject`) — exactly what a non-Workspace share
grants — already authenticates today. The gap is *not* in credential
construction; it is (a) the undocumented, untested, no-UX nature of that
path, and (b) `gmail.py:133`'s `config.get("delegated_user", self._email_address)`
silently re-enabling delegation when the field is left blank. #09's value is
making the no-delegation path the **explicit, default, validated** mode and
fixing that default.

**Documented requirement carried to spec #01 (the `calendarList` gotcha).** A
calendar shared *with* a service account does **not** appear in
`service.calendarList().list()` — that endpoint returns only calendars on the
authenticated principal's own list, and a fresh SA's list is empty. #09
imposes this requirement on the future `GoogleCalendarBackend`: under
`shared_service_account`, before listing/reading a shared calendar the
backend MUST call `service.calendarList().insert(body={"id": calendar_id}).execute()`
(idempotent — treat a 409 "Already Exists" as success). Drive's analogous
case is already handled: `gdrive_documents.py` addresses an explicit
`folder_id` with `includeItemsFromAllDrives=True` / `supportsAllDrives=True`,
so a folder shared with the SA is reachable with no enrolment step. Gmail is
unaffected — a free Gmail inbox cannot be shared to an SA at all, which is
precisely why the `oauth_bot` fallback exists.

### New Service(s)

**None.** Justified against the established pattern:

1. **No per-user token-ownership problem.** v1's `oauth_bot` is *one*
   operator-level account acting as its own mailbox — a single
   operator-scoped credential, not a per-end-user grant. Per-user Google
   identity for *Gilbert login* is a separate concern already owned by
   `GoogleAuthBackend` (`google_auth.py`). There is no shared token a second
   consumer needs a mediating service for.
2. **The persist/refresh/link pattern already exists with zero core
   surface.** `std-plugins/sonos/sonos_music.py` runs a complete operator
   OAuth-bot flow self-contained in a backend: a `redirect_uri` ConfigParam,
   a `link_*` / `link_*_complete` two-phase `ConfigAction` pair, httpx token
   exchange + refresh, and persistence via the existing
   `ConfigActionResult.data["persist"]` side-channel that the Settings UI
   folds into unsaved form state. No `CredentialService`, no capability
   protocol, no `core/services/` change was needed for Spotify; Google's
   OAuth-bot path is the same shape.
3. **Layer rules discourage a service here.** A core
   `GoogleCredentialsService` would have to know about a vendor; `core/`
   may not depend on Google specifics. Clean placement = plugin-internal
   resolver + the plugin's own `ConfigAction`s.
4. **Refresh is not a service concern.** `google.oauth2.credentials.Credentials`
   auto-refreshes given `refresh_token` + client pair + `token_uri`; the
   existing `_google_retry.call_with_retry` already rebuilds the service on
   transport errors.

### New / Modified Backend(s)

#### `gmail.py`

Current credential block (`gmail.py:131-160`, condensed):

```python
self._email_address = config.get("email_address", "")
sa_json = config.get("service_account_json", "")
delegated_user = config.get("delegated_user", self._email_address)  # <- silent delegation
creds = service_account.Credentials.from_service_account_info(sa_info, scopes=scopes)
if delegated_user:
    creds = creds.with_subject(delegated_user)
self._creds = creds
```

After — mode is explicit, not inferred from a blank field, and credential
construction is one call:

```python
from .google_credentials import (
    GoogleCredentialMode, GoogleCredentialSpec, build_google_credentials,
)

mode = _resolve_mode(config)  # see Backwards compat for the legacy inference
scopes = ("https://www.googleapis.com/auth/gmail.modify",
          "https://www.googleapis.com/auth/gmail.send")

if mode is GoogleCredentialMode.OAUTH_BOT:
    spec = GoogleCredentialSpec(
        mode=mode, scopes=scopes,
        client_id=config.get("oauth_client_id", ""),
        client_secret=config.get("oauth_client_secret", ""),
        refresh_token=config.get("oauth_refresh_token", ""),
    )
else:
    spec = GoogleCredentialSpec(
        mode=mode, scopes=scopes,
        service_account_info=json.loads(sa_json) if isinstance(sa_json, str) else sa_json,
        delegated_user=config.get("delegated_user", ""),   # NOTE: no email_address fallback
    )
self._creds = build_google_credentials(spec)
self._service = await asyncio.to_thread(self._build_service)
```

**What stays:** the entire fetch/send/mark surface; the
`_build_service`/`_rebuild_service`/`call_with_retry` plumbing (the `_creds`
caching contract is preserved — it now holds whichever `Credentials` subtype
the resolver returned; both are accepted by
`discovery.build(credentials=...)`). `_rebuild_service` gains
refresh-awareness for the `oauth_bot` case. A new
`connect_google` / `connect_google_complete` `ConfigAction` pair is added
(mirroring Sonos `link_spotify`), persisting the new refresh token via
`data={"persist": {"settings.oauth_refresh_token": ...}}`. The existing
`test_connection` action is upgraded (see Configuration / wizard).

#### `gdrive_documents.py`

Identical refactor (`gdrive_documents.py:150-167`). Note Drive already
correctly defaults `delegated_user` to `""` (`gdrive_documents.py:151`) — no
silent-delegation bug to fix here. `folder_id` is unchanged; under
`shared_service_account` the operator shares the Drive folder with the SA
email and Drive's existing `supportsAllDrives` query params make it reachable
with no enrolment call. **Caveat (documented in code + README):** a bare
service account has 0 bytes of My-Drive quota, so `upload_document` /
`delete_document` are SA-limited; current Knowledge usage is read-only
ingestion and is unaffected (see Open Questions #4).

### Multi-Backend Aggregation

Largely **N/A** — #09 changes *how a backend authenticates*, not how backends
aggregate. One note: a single credential serves multiple backends. With
`shared_service_account`, the same SA JSON pasted into both the Gmail and
Drive (and future Calendar) configs authenticates all three, provided the
operator shared the relevant resource with that one SA email. With
`oauth_bot`, one ordinary account's refresh token similarly covers Gmail +
Drive + Calendar. Each backend instance independently builds its own
`GoogleCredentialSpec` (with its own scopes) from its own `backend_config`,
exactly as the inbox/knowledge services already pass
`dict(mailbox.backend_config)` into `backend.initialize()`. There is no
service-level aggregator to change.

### Multi-User & RBAC

#09 makes the **shared-identity** model explicit. Whoever configures the
credential wires up *one* identity that **every** Gilbert user's Gmail/Drive
operations run as. This is categorically different from per-user OAuth and the
RBAC posture must make the blast radius explicit and confine the controls.

- **Identity sourcing is unchanged.** The Google backends already source
  identity from a *single configured credential*, not from the calling
  `UserContext` (`gmail.py:145-160`, `gdrive_documents.py:150-164`). Every
  call runs as that one principal regardless of which user triggered the AI
  turn. #09 only swaps *how the credential is obtained*, never *who calls run
  as*. The shared-identity model is the **status quo, made explicit and
  operator-configurable** — not a new isolation primitive.
- **Connecting/managing the credential is admin-only.** The wizard panel is
  declared `UIPanel(panel_id="google.connect", slot="settings.integrations",
  required_role="admin")` — `required_role` is server-filtered via the auth
  context, so a `user`-role session never receives the panel. The credential
  values remain `sensitive=True` `ConfigParam`s (masked in WS/SPA responses;
  masking, **not** encryption — see Security).
- **What `user` role sees:** never the wizard, SA JSON, OAuth secret, refresh
  token, or health detail. It *does* (by design) transitively use Gmail/Drive
  features that run as the shared identity. This is the intended trust model,
  not a leak — but it must be stated plainly in operator copy.
- **Trust / blast-radius statement** (must render verbatim in the wizard's
  intro step and the std-plugins README google section):

  > The credential you configure here is **shared**. Every Gilbert user who
  > can use the Gmail or Drive features will read and write **this** Google
  > account's mail and files, acting as **this** identity. There is no
  > per-user separation. Use a dedicated automation account, not a personal
  > mailbox. With the **service-account** model, users grant access by
  > *sharing specific calendars/Drive items* with the SA email, which bounds
  > exposure to exactly what they share; with the **OAuth-bot** model, the
  > bot account's *entire* mailbox is exposed.

- **Per-user OAuth** is explicitly out of v1 and designed-for as a future
  opt-in (a future per-user token store keyed by `owner_user_id` produces the
  same `GoogleCredentialSpec`; the backends' only change is the credential
  lookup). Recorded in Open Questions so reviewers see the seam is
  deliberate.

### Configuration

ConfigParam set for the **Gmail** and **Drive** backends after this change
(Calendar inherits the identical block when spec #01 lands). Existing keys are
preserved verbatim; new keys are additive; all `restart_required=True` to
match the existing SA params (backend re-instantiation already happens on
save). `credential_mode` uses `choices=(...)` so the Settings UI renders a
dropdown. **No `ai_prompt` param is introduced** — there are no AI prompt
strings anywhere in the credential path (explicitly checked against skill
Category 5).

| Key | Type | Sensitive | Multiline | Default | Purpose |
|---|---|---|---|---|---|
| `credential_mode` | STRING (choices) | no | no | `shared_service_account` | `shared_service_account` \| `delegated_service_account` \| `oauth_bot`. |
| `email_address` *(Gmail)* | STRING | no | no | — | Mailbox to monitor/send from. **Existing.** |
| `service_account_json` | STRING | **yes** | **yes** | — | SA key JSON. Both SA modes. **Existing (unchanged).** |
| `delegated_user` | STRING | no | no | `""` | Workspace subject; honored only in `delegated_service_account`. **Existing** (Gmail's `email_address` fallback removed — see Migration). |
| `folder_id` *(Drive)* | STRING | no | no | — | Drive folder / Shared Drive id. **Existing.** |
| `oauth_client_id` | STRING | **yes** | no | `""` | OAuth client id (Desktop-type for loopback). `oauth_bot` only. **New.** |
| `oauth_client_secret` | STRING | **yes** | no | `""` | OAuth client secret. `oauth_bot` only. **New.** |
| `oauth_refresh_token` | STRING | **yes** | no | `""` | Long-lived refresh token; auto-populated by the Connect Google action. `oauth_bot` only. **New.** Plaintext at rest (deferred — see Migration). |
| `oauth_redirect_uri` | STRING | no | no | `http://127.0.0.1:0/oauth2callback` | Loopback (ephemeral port) for the default Desktop+PKCE flow; set to the tunnel public URL for web-redirect. **New.** |

### Events

None. #09 emits and consumes no event-bus events.

### AI Tools

**None — deliberate and rule-driven.** Credential setup is admin
configuration, not an agent capability. In this codebase a `ToolProvider`'s
`get_tools()` feeds **both** the AI tool registry and the slash registry —
there is no per-tool "hide-from-AI" flag on `ToolDefinition` — so exposing any
tool here would expose secret-adjacent surface to the model. The actual Google
*capabilities* (inbox read/reply, Drive search) already exist as the
inbox/documents service tools and are unaffected by this credential refactor.
**No slash command in v1** either: status is a tab in the wizard panel
(zero AI-surface risk). A future admin `/google status` command is recorded
as a deferred Open Question.

### UI / Frontend

An admin-only **setup wizard**, mounted via the existing settings-category
slot machinery: `SettingsPage.tsx` renders `<PluginPanelSlot
slot={`settings.${category.toLowerCase()}`} />` for the active category. #09
pins the google plugin's `config_category = "Integrations"`, resolving the
slot to **`settings.integrations`** (the `account.extensions` slot is wrong
here — that is the *per-user* Account page; this is shared-identity *admin*
config).

`Plugin.ui_panels()` returns:

```python
UIPanel(
    panel_id="google.connect",
    slot="settings.integrations",
    label="Google connection",
    description="Connect Gilbert to Google with an ordinary account",
    required_role="admin",
)
```

Plugin frontend layout (all under `std-plugins/google/frontend/`; core's Vite
`import.meta.glob` auto-collects `panels.ts`; core never imports plugin TSX):

```
std-plugins/google/frontend/
    panels.ts              # registerPanel("google.connect", GoogleConnectWizard)
    GoogleConnectWizard.tsx
    steps/{ChooseMode,GcpSetup,ServiceAccount,OAuthConnect,Validate,ShareInstruction}.tsx
    StatusTab.tsx          # read-only mode + share-with email + health
    api.ts                 # useGoogleConnectApi() over rpc() from useWebSocket
    types.ts
    package.json           # npm workspace member; peerDeps react / @tanstack/react-query
```

Step flow:

1. **Choose mode** — *Service account (recommended)* vs *OAuth bot account*,
   each card carrying the one-line trade-off and the blast-radius statement
   (rendered here, not buried).
2a. **GCP setup (SA path)** — numbered steps with **deep links** into the GCP
   Console (create/select project, enable Gmail + Drive APIs, create a
   service account, download a JSON key). Deep-link URLs are built by a pure
   helper in `google_credentials` (`build_setup_guide(mode)`), not hardcoded
   in TSX — URL building stays out of the presentation layer.
2b. **OAuth client setup (bot path)** — create a **Desktop-type** OAuth client
   (loopback default) or note the web-redirect alternative; the
   publishing-status caveat (Security) renders **inline here**.
3. **Provide credential** — SA: a multiline paste box (mirrors the existing
   `service_account_json` ConfigParam). OAuth-bot: a **Connect with Google**
   button starting the loopback flow.
4. **Live validate** — calls the upgraded `test_connection` action which makes
   a *real* Google API call (`users().getProfile` / Drive `about.get`) with
   the just-supplied credential, reporting success or the precise error.
5. **Share instruction** — on success, prominently display the
   **share-with-this-email** address with a copy button and a one-paragraph
   "what to do next" (each user shares their Calendar/Drive items with this
   email). Worded so spec #01 can extend it for Calendar without rework.

The wizard never builds Google URLs and never touches tokens itself — it
drives the named per-backend `ConfigAction`s and renders results.

#### OAuth-bot connection flow

Reuses the proven exchange shape from `google_auth.py` (httpx POST to
`https://oauth2.googleapis.com/token`, `access_type=offline`), with three
differences: keep the **refresh token**, loopback redirect by default, store
the credential as the shared bot identity (not a Gilbert session). Logic lives
in `google_credentials` helpers called by the Gmail/Drive `ConfigAction`s —
**not** in `google_auth.py` (different contract) and **not** in the web route.

- **Default — loopback (Desktop client + PKCE + state).** The Connect action
  binds an ephemeral `127.0.0.1` listener (`bind(("127.0.0.1", 0))`),
  generates a PKCE `code_verifier`/`code_challenge` (S256) and a single-use
  `state` (held in memory, never persisted, never sent to the SPA), and
  returns the Google authorization URL. The browser hits the local listener;
  the connector validates `state`, exchanges the code (with `code_verifier`),
  and persists the `refresh_token`. **No web route involved.**
- **Alternative — web-redirect via TunnelBackend.** When a tunnel is
  configured, `redirect_uri = tunnel.public_url_for("/integrations/google/oauth/callback")`
  (exactly how `GoogleAuthBackend.get_callback_url()` already resolves it).
  Google redirects to the one thin core route, which only parses
  `code`/`state` and calls the plugin's `connect_google_complete`. PKCE +
  `state` still used.
- **Headless fallback — manual paste.** Operator opens the auth URL on any
  machine and pastes the resulting `code` back into the wizard (the exact
  shape Sonos/Spotify already uses). Zero infrastructure.

### Dependencies

**No new Python dependency for credentials.** `google.oauth2.credentials.Credentials`
ships inside `google-auth` (already a dep). The OAuth-bot flow is hand-rolled
with `httpx` to match house style: `google_auth.py` already does the
authorization-code → token exchange with `httpx`; Sonos does the full
authorize/exchange/refresh by hand. Pulling in `google-auth-oauthlib` (which
drags `requests` + `oauthlib`, a blocking stack) to do what `httpx` already
does would break consistency. PKCE is ~10 lines of `secrets` + `hashlib` +
`base64` — no library. **One housekeeping change:** `httpx` is currently used
by `google_auth.py` undeclared (transitive); since the plugin now uses it in
more non-test code, **add `httpx` explicitly to
`std-plugins/google/pyproject.toml`** (declare what you depend on). Frontend:
the new `frontend/package.json` is an npm workspace member with peerDeps on
the host SPA libs (no new runtime dep in core).

## Tool Profile Integration

**N/A.** Mirroring `01-calendar.md`'s posture ("No new profiles … No
`ai_call` assignment"): #09 is pure credential plumbing plus a settings-page
wizard. It registers no `ToolDefinition`s, declares no `ai_tools` provider,
adds no `ConfigParam(ai_prompt=True)`, and touches no seeded profile
(`light`/`standard`/`advanced`). The downstream Gmail/Drive/Calendar tools are
owned by their services and specs; their profile membership is unchanged.

## Migration / Compatibility

### Existing code touched

| File | Repo | Change |
|---|---|---|
| `std-plugins/google/gmail.py` | submodule | `initialize()` (131-160) routes through the resolver; `_rebuild_service` refresh-aware; new mode/OAuth ConfigParams; new `connect_google`/`connect_google_complete` actions; upgraded `test_connection`; **fix the silent-delegation default at line 133**. |
| `std-plugins/google/gdrive_documents.py` | submodule | Same refactor (150-167); no delegation-default bug; write-under-SA caveat documented in code. |
| `std-plugins/google/google_credentials.py` | submodule | **New** resolver + spec + enum + OAuth helpers + `share_with_email`. |
| `std-plugins/google/plugin.py` | submodule | Add `GooglePlugin.ui_panels()`; pin `config_category="Integrations"`. No new `provides`. |
| `std-plugins/google/pyproject.toml` | submodule | Add explicit `httpx` dependency. |
| `std-plugins/google/frontend/*` | submodule | **New** wizard panel tree (see UI). |
| `src/gilbert/web/routes/integrations.py` | root | **New**, one thin route — web-redirect path only (Open Questions #6: defer-able). |
| `std-plugins/README.md`, root `README.md`, `docs/specs/OPEN_QUESTIONS.md` | submodule / root | Docs (see Documentation freshness). |

### Backwards compat

The load-bearing constraint, satisfied **without a data migration**. Old
`backend_config` rows have `service_account_json` set, `delegated_user`
empty-or-set, and **no** `credential_mode`. Picking `shared_service_account`
blindly for an old config that *intended* delegation would silently drop
`with_subject` and break a working Workspace deployment. Resolution — derive
the legacy mode at read time, in `initialize()`, before building the spec:

```python
raw = config.get("credential_mode")
if raw is None:  # pre-#09 config — infer from legacy fields
    raw = "delegated_service_account" if config.get("delegated_user") else "shared_service_account"
```

- Old config with `delegated_user` set ⇒ `delegated_service_account` ⇒
  `with_subject()` still called ⇒ **identical behaviour to today**.
- Old config without `delegated_user` ⇒ `shared_service_account` ⇒ no
  `with_subject()` ⇒ **identical to today** (it never delegated anyway —
  confirmed against `gmail.py:156`/`gdrive_documents.py:163`).
- The one intentional, documented change: Gmail no longer defaults
  `delegated_user` to `email_address`, so a Gmail config that relied on that
  *implicit* self-delegation must now set `delegated_user` explicitly or (the
  correct non-Workspace answer) run as `shared_service_account`. This is the
  bug the spec exists to fix; called out in the README.

This is a read-time mapping, **not** a stored rewrite — a rollback to pre-#09
code reads the same rows correctly (it ignores the unknown `credential_mode`
key it never wrote).

### DB migrations

**None.** The OAuth-bot path adds **no new entity collection**.
`oauth_refresh_token` is just another `sensitive=True` key inside the
**existing** per-instance `backend_config` blob (the same dict already
persisted per mailbox / per source), written via the established
`ConfigActionResult.data["persist"]` side-channel and committed on Save —
exactly the Spotify mechanism, which required zero migration. The generic
schemaless entity store needs no migration for a new key (CLAUDE.md: "New
entity types require no migrations"). Multi-user keying is inherited, not new
(operator-level single account; same per-account `backend_config` keyed by
the owning mailbox/source `_id`).

**Encryption at rest: explicitly deferred.** `oauth_refresh_token` and
`service_account_json` sit plaintext in `.gilbert/gilbert.db`, identical to
the existing Gmail SA JSON and Sonos/Spotify refresh token. This is the
project-wide gap locked in `OPEN_QUESTIONS.md` (2026-05-09). `sensitive=True`
masks values in responses but is not encryption. Documented (not silently
shipped) in `std-plugins/README.md` with the same mitigations spec #01 lists
(`0600` on the DB file owned by the run user; dedicated low-privilege
account; rotate/revoke periodically).

## Testing Strategy

Per the project rule (don't mock the thing under test; refactor for
testability). The resolver is extracted specifically so credential-mode
resolution becomes a pure, network-free unit instead of an untestable branch
inside `initialize()`.

### Test files

- `std-plugins/google/tests/test_google_credentials.py` — unit tests for the
  resolver: mode selection (empty-`delegated_user` → `shared_service_account`;
  set → `delegated_service_account`; explicit override; legacy `None`
  inference), `share_with_email` (parse `client_email` from SA JSON), PKCE
  challenge/verifier generation, authorization-URL builder, refresh decision.
  No network. Only `from_service_account_info` and the token endpoint are
  stubbed — never the resolver itself.
- `std-plugins/google/tests/test_google_oauth_flow.py` — the OAuth-bot
  exchange + refresh against a **fake token endpoint** (local httpx-mockable
  handler returning canned `{access_token, refresh_token, expires_in}` — the
  technique Sonos's Spotify flow is tested with). Covers first link, silent
  refresh, refresh-token-revoked (`invalid_grant`), loopback-port-in-use.
- `std-plugins/google/tests/test_gmail.py` / `test_gdrive_documents.py`
  *(existing, extended)* — assert refactored `initialize()` builds a working
  service in each mode (with `discovery.build` as a `MagicMock`), and a
  **regression guard**: a legacy `service_account_json` + `delegated_user`
  config still calls `with_subject()` exactly as before.

### Real vs. mocked

| Test file | Mocked | Real |
|---|---|---|
| `test_google_credentials.py` | `from_service_account_info` (sentinel creds); the OAuth token HTTP endpoint. | The resolver, mode-selection, PKCE/state, SA-email extraction, all config→mode mapping. |
| `test_google_oauth_flow.py` | The Google token endpoint (local fake); the loopback bind only where port-collision is simulated. | The code↔token exchange + refresh, PKCE round-trip, refresh-token persistence. |
| `test_gmail.py` / `test_gdrive_documents.py` | `googleapiclient.discovery.build`; the credential constructor. | `initialize()` wiring through the real resolver in each mode; the legacy-path regression assertion. |

No test contacts a real Google API or account. The live validator is
exercised via the backend's existing `test_connection` action with the Google
client mocked, asserting the result message contains the share-with email for
`shared_service_account` and a precise diagnostic for the wrong-key case.

### Edge cases to cover

1. Empty `delegated_user` → `shared_service_account`, no `with_subject()`.
2. Legacy config (`service_account_json` + non-empty `delegated_user`) →
   `delegated_service_account`, `with_subject()` called — byte-for-byte
   behaviour assertion.
3. OAuth access token expired, refresh token valid → transparent refresh; the
   rebuilt service uses the new access token.
4. Loopback preferred port in use → ephemeral-port fallback reflected in the
   auth URL; no bindable port → clear actionable error, not a stack trace.
5. `oauth_bot` with no tunnel → loopback used (default); with a tunnel →
   web-redirect via `public_url_for`.
6. SA credentials valid but nothing shared yet → validator reports
   "credentials OK, but nothing is shared with `<sa-email>` yet — share your
   Calendar/Drive with that address," not a generic failure (the single most
   common operator mistake for the primary mode).
7. Wrong key pasted (malformed JSON / OAuth-client-secret JSON where an SA key
   is expected / wrong project) → validator distinguishes "this isn't an SA
   key" from "Google rejected this key," each with a one-line fix.
8. OAuth refresh token revoked (`invalid_grant`) → backend reports unhealthy
   with "re-link the Google account in Settings," not an opaque exception.

## Open Questions / Risks

1. **(a) One account → many calendars vs. spec #01's explicit non-goal.**
   `01-calendar.md` lists "Multi-calendar aggregation per account" as a
   non-goal (one `CalendarAccount` → one calendar id). Under
   `shared_service_account`, N people each share *their* calendar with the
   *one* SA, so one credential fronts many calendars. These are reconcilable:
   #01's non-goal is about *aggregating many calendars into one account row*,
   while #09's shared SA still maps to **one `CalendarAccount` per shared
   calendar** (each sharer owns their own row, all rows reuse the same SA
   key). **Recommendation:** keep #01's one-account-one-calendar invariant;
   document in #01 that `service_account_json` may be reused across many
   account rows and that the SA's `calendarList` (post-`insert` enrolment)
   will enumerate every shared calendar — the backend must address one
   `calendar_id` per account and must not implicitly aggregate. Needs the #01
   author to land the doc note; #09's resolver must merge before #01's
   backend.
2. **(b) Should Gilbert ship its own Google-verified OAuth client?** Would let
   end users do zero GCP setup; costs the 100-user unverified cap, full OAuth
   verification + annual CASA assessment for the restricted Gmail/Drive/
   Calendar scopes, and the project becoming Google's registered data
   controller. Governance/maintainer call only. v1 = operator-owned clients.
3. **(c) Refresh-token longevity posture.** A "Testing" OAuth client issues
   refresh tokens that **expire after 7 days** for non-Workspace accounts;
   "In production / unverified" issues non-expiring tokens (still shows the
   unverified-app interstitial, capped at 100 users). **Recommendation:** the
   wizard must mandate "In production" publishing status and explicitly warn
   about the 7-day trap (the single most likely "worked yesterday, broke
   today" support issue). `shared_service_account` has no such expiry — that,
   plus bounded exposure, is *why it is the recommended primary*.
4. **(d) Drive service-account My-Drive quota is 0.** A bare SA has 0 bytes of
   personal Drive, so SA-owned `files.create`/upload fails. Verified: live
   Knowledge usage is read-only ingestion of files in a *shared* folder owned
   by the user — does **not** consume SA quota, so v1 is unaffected.
   `upload_document`/`delete_document` are SA-limited under shared mode;
   document the read-only-under-SA caveat in `std-plugins/README.md`.
5. **(e) Gmail under a service account is impossible on free accounts.** Gmail
   has no "share my inbox with a service account" primitive. On a free
   account the *only* mailbox path is `oauth_bot`. **Recommendation:** accept
   for v1; state plainly in the wizard and README ("free-account Gmail =
   OAuth-bot, mandatory; shared-SA is Calendar/Drive only"). Not engineerable
   away — a Google API fact.
6. **(f) Web-redirect needs one thin core route — include in v1 or defer?**
   Loopback (default) + manual-paste are 100% plugin-contained (zero
   `src/gilbert/` change). The web-redirect convenience needs one thin route
   in `src/gilbert/web/routes/`. **Recommendation:** include it (the user
   asked for "support both"; it is small and mirrors `auth.py`); the
   alternative is to ship loopback + manual-paste in v1 and add web-redirect
   in v1.1, keeping v1 entirely within the submodule.
7. **(g) `ConfigAction` vs. a plugin `Service` for the wizard backend.** v1
   uses the existing per-backend `ConfigAction` mechanism (no new Service, no
   ACL change) — consistent with Sonos. If a unified cross-backend wizard
   surface proves awkward on per-backend actions, a plugin `Service` (with
   `slash_namespace`, not advertising `ai_tools`) is the documented fallback;
   recorded so the seam is deliberate.
8. **Loopback on a headless host.** The loopback default assumes a browser can
   reach `127.0.0.1:<port>` on the Gilbert host. Fully headless + no tunnel →
   manual-paste (shipped in v1 as the third option). No new infra.

### docs/specs/OPEN_QUESTIONS.md — proposed delta

A `### #09-google-oauth` block is appended under each of the three existing
severity headings, matching that file's format (see the actual diff in this
PR). High-impact: (b) verified-client governance, (a) cross-spec calendar
reconciliation. Medium-impact: (c) refresh-token longevity, (d) Drive SA
quota, (e) Gmail-needs-OAuth. Low-impact: per-user OAuth future, (f)
web-redirect core-route defer decision, (g) ConfigAction-vs-Service and the
deferred `/google status` slash command, manual-paste-is-in-v1.

## Implementation Plan (Step-by-Step)

Interface-first; each step independently testable. The std-plugins submodule
two-PR sequence is explicit. **This spec PR contains only
`docs/specs/09-google-oauth.md` + the `OPEN_QUESTIONS.md` delta**; the steps
below describe the *implementation* PRs that follow spec approval.

1. **Resolver contract.** `std-plugins/google/google_credentials.py`:
   `GoogleCredentialMode`, `GoogleCredentialSpec`, `build_google_credentials`,
   `share_with_email`, `_resolve_mode`, OAuth helpers, `build_setup_guide`.
   Plugin-internal; imports only stdlib + `google.*` + `httpx`. `__all__`.
2. **Resolver unit tests** (`test_google_credentials.py`) — pure, no network.
3. **OAuth-bot flow + tests** — loopback/PKCE + tunnel-web-redirect +
   manual-paste (model on `google_auth.py` URL building and Sonos
   exchange/persist). `test_google_oauth_flow.py` against a fake endpoint.
4. **Refactor `GmailBackend.initialize()`** through the resolver; make
   `_rebuild_service` refresh-aware; **fix the line-133 silent-delegation
   default**; extend `test_gmail.py` incl. the legacy regression assertion.
5. **Refactor `GoogleDriveDocumentBackend.initialize()`** identically;
   refresh-aware rebuild; document the write-under-SA caveat in code.
6. **Validator upgrade** — extend each backend's `test_connection`
   `ConfigAction` to surface the share-with email and the edge-case-6/7
   diagnostics.
7. **New ConfigParams + Connect actions** — `credential_mode` selector + the
   OAuth keys; `connect_google`/`connect_google_complete` action pair.
8. **Wizard SPA panel** — `std-plugins/google/frontend/`; `panels.ts`,
   wizard + steps + Status tab, plugin-local api hook/types;
   `GooglePlugin.ui_panels()`; pin `config_category="Integrations"`; add
   `frontend/package.json` to the root npm workspace.
9. **Thin core route** (if Open Questions #6 = include) —
   `src/gilbert/web/routes/integrations.py` `GET
   /integrations/google/oauth/callback`, mirroring `auth.py`'s thin callback.
10. **Docs in the submodule** — `std-plugins/README.md` (google row + three
    modes + plaintext-at-rest gap) and `std-plugins/CLAUDE.md` (only if a new
    convention is set), in the same submodule branch.
11. **Submodule PR (PR-A)** → `briandilley/gilbert-plugins:main` — steps 1–8,
    10 (everything under `std-plugins/google/`). Full plugin suite green.
12. **Pointer-bump PR (PR-B)** → `briandilley/gilbert:main`, *after PR-A
    merges* — bump the `std-plugins` submodule pointer; land step 9 (thin
    route, if included); root `README.md`; `docs/specs/OPEN_QUESTIONS.md`
    delta; any touched `docs/architecture/*.md`. PR-B's tests run with the
    new submodule pointer (root `pyproject.toml` `testpaths` picks up the
    submodule tests).
13. **Smoke** — `uv run pytest`, `uv run mypy src/`, `uv run ruff check`,
    then end-to-end against a real free `@gmail.com`: shared-SA Drive (share a
    folder, ingest), OAuth-bot Gmail (link, poll, send), legacy delegated
    config unchanged.

Steps 1–7 are testable before any UI exists. **PR-B cannot be opened until
PR-A is merged** (the pointer must reference a real commit on
`gilbert-plugins:main`).

## File Manifest

Paths under `std-plugins/google/` live in the **`briandilley/gilbert-plugins`
submodule repo** (PR-A). Root-repo paths are PR-B. *(This spec PR adds only
the two `docs/specs/` files below.)*

### New files

| File | Repo | Purpose |
|---|---|---|
| `docs/specs/09-google-oauth.md` | root | **This spec** (this PR). |
| `std-plugins/google/google_credentials.py` | submodule | Resolver, spec, enum, OAuth helpers, `share_with_email`, `build_setup_guide`. |
| `std-plugins/google/tests/test_google_credentials.py` | submodule | Mode/SA-email/PKCE unit tests (no network). |
| `std-plugins/google/tests/test_google_oauth_flow.py` | submodule | OAuth-bot exchange + refresh vs a fake endpoint. |
| `std-plugins/google/frontend/panels.ts` | submodule | `registerPanel` side-effect. |
| `std-plugins/google/frontend/GoogleConnectWizard.tsx` (+ `steps/`, `StatusTab.tsx`) | submodule | Admin guided wizard. |
| `std-plugins/google/frontend/api.ts`, `types.ts`, `package.json` | submodule | Plugin-local hook/types; workspace member. |
| `src/gilbert/web/routes/integrations.py` | root | One thin web-redirect callback route (Open Questions #6: defer-able). |

### Modified files

| File | Repo | Change |
|---|---|---|
| `docs/specs/OPEN_QUESTIONS.md` | root | Append the `### #09-google-oauth` blocks (this PR). |
| `std-plugins/google/gmail.py` | submodule | Resolver routing; refresh-aware rebuild; new ConfigParams + Connect actions; upgraded validator; fix line-133 default. |
| `std-plugins/google/gdrive_documents.py` | submodule | Resolver routing; refresh-aware rebuild; new ConfigParams; write-under-SA caveat. |
| `std-plugins/google/plugin.py` | submodule | `ui_panels()`; pin `config_category="Integrations"`. |
| `std-plugins/google/pyproject.toml` | submodule | Add explicit `httpx`. |
| `std-plugins/README.md` | submodule | Google row + three modes + plaintext-at-rest gap. |
| `std-plugins/CLAUDE.md` | submodule | Only if a new plugin convention is set. |
| `std-plugins` (submodule pointer) | root | Bump to PR-A's merge commit (PR-B). |
| `README.md` (root) | root | "No Workspace needed" integration/setup story; update the google row. |

`src/gilbert/` core is **not** modified beyond the single optional thin web
route; **no** new core ABC, capability protocol, core service, or layer-rule
change — so root `CLAUDE.md` is **not** modified.

### Documentation freshness

Per the `validate-architecture` skill, **stale docs are regressions**, shipped
in the same PR as the code that invalidates them:

- **`README.md` (root)** — **MUST change (PR-B).** The integration narrative
  and google row currently imply Workspace. Add the "no Workspace needed"
  story and the shared-SA / OAuth-bot modes.
- **`std-plugins/README.md`** — **MUST change (PR-A).** The google
  **Configure** table lists `delegated_user` with no hint that empty =
  shared-identity. Document all three modes + the **plaintext-at-rest gap
  note** (first new credential surface in the initiative; the locked decision
  requires the gap be documented here when it ships).
- **`std-plugins/CLAUDE.md`** — **Conditional (PR-A).** Only if the wizard
  establishes a reusable plugin-frontend convention beyond what is already
  documented; a standard `ui_panels()` + `registerPanel` panel needs no
  change.
- **`CLAUDE.md` (root)** — **No change.** No capability protocol, no layer
  rule added (resolver is plugin-internal).
- **`docs/architecture/`** — no dedicated google/auth/credentials doc exists.
  Check the two adjacent docs for drift and update only if they assert the
  old credential model: **`inbox-service.md`** (Gmail is its reference
  backend — add a pointer to the new modes if it describes SA+delegation) and
  **`knowledge-service.md`** (Drive backend — note the read-only-under-SA
  caveat if it discusses Drive write). A new
  `docs/architecture/google-credentials.md` is *optional*, recommended only
  if a reviewer judges the three-mode model non-obvious enough for a
  deep-dive — not a mandatory freshness item.
