# Self-Signed HTTPS on First Boot

**Status:** Design approved
**Date:** 2026-05-24
**Owner:** Brian Dilley

## Problem

Browsers gate `navigator.mediaDevices.getUserMedia` (and the entire Media Capture API) behind the **secure context** requirement: HTTPS, `http://localhost`, or `http://127.0.0.1`. On any other origin — for example `http://192.168.1.42:8000` from a phone on the LAN — `navigator.mediaDevices` is `undefined` and there's no permissions dialog the user can click "allow" on.

This blocks the voice-agent plugin's mic capture for the realistic use case: open Gilbert on a phone or another laptop pointed at the home server's LAN IP. The user can't fix it with a setting in Gilbert today — they have to terminate TLS in front of the app themselves.

## Goal

Gilbert generates a self-signed TLS certificate on first boot and serves HTTPS in parallel with HTTP, so any browser on the LAN can reach an HTTPS origin (and unlock `getUserMedia`) after a one-time "trust this cert" step. Non-browser LAN clients (Sonos, IoT) keep using the existing HTTP port unchanged.

## Non-goals

- **No HTTP→HTTPS redirect.** Would break IoT / Sonos / non-HTTPS LAN clients (see `feedback_tunnel_allowlist`).
- **No public-CA integration.** No Let's Encrypt, no ACME. Self-signed only.
- **No trust-store automation.** Gilbert never tries to write to OS keychains. The user installs the cert manually on each device.
- **No cert auto-renewal scheduler.** Regeneration happens only on boot, when the existing cert is near expiry (< 7 days remaining).
- **No background re-detection of LAN IPs.** SAN list is fixed at cert generation time. Adding a new IP later requires deleting the cert files and restarting.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | HTTP and HTTPS in parallel, same FastAPI app, different ports | Phones use HTTPS for mic; Sonos/IoT keep HTTP. Zero breakage. |
| 2 | SAN list auto-detected at generation time | Phone-on-LAN works out of the box; user doesn't have to know about SAN configuration. |
| 3 | 10-year validity | Matches mkcert. Trust survives a decade per device. |
| 4 | Cert generation in-process via `cryptography` | Already a transitive dep; promote to direct. No openssl shell-out. |
| 5 | Two `uvicorn.Server` instances in `asyncio.gather` | Cleanest dual-listener pattern; uvicorn explicitly supports this. |
| 6 | "Set up HTTPS" SPA page reachable without auth | Chicken-and-egg: user can't auth before trusting the cert. Page contains only the public cert + install instructions. |
| 7 | TLS failure during boot degrades to HTTP-only, doesn't crash | Gilbert must keep running even if cert gen hits some weird filesystem case. |

## Architecture

### New module: `src/gilbert/core/tls.py`

Pure utility, layered at `core/` (depends only on stdlib + `cryptography`; no imports from `core/services/`, `integrations/`, `web/`, or `storage/`).

```python
@dataclass(frozen=True)
class CertInfo:
    cert_path: Path
    key_path: Path
    not_valid_after: datetime
    san_entries: list[str]
    sha256_fingerprint: str

def ensure_self_signed_cert(cert_path: Path, key_path: Path) -> CertInfo:
    """Return existing cert if present and valid; otherwise generate, write, and return."""
```

Behavior:

1. If both files exist, load them. If parseable and `not_valid_after - now > 7 days`, return them. Otherwise treat as missing.
2. Generate a fresh 2048-bit RSA key + self-signed X.509 cert with:
   - **Subject CN:** `Gilbert (self-signed)`
   - **Validity:** 10 years from now
   - **SAN list** (computed at generation time):
     - DNS: `localhost`, `socket.gethostname()`, `<hostname>.local`
     - IP: `127.0.0.1`, `::1`, plus every non-loopback IPv4/IPv6 address discovered via:
       - `socket.getaddrinfo(socket.gethostname(), None)` for hostname-resolved addresses
       - The UDP-trick (`socket.connect(("8.8.8.8", 53))` then `getsockname()`) to find the primary outbound LAN IP even if hostname doesn't resolve back
     - Deduplicated; invalid entries dropped silently
   - **keyUsage:** `digitalSignature, keyEncipherment`
   - **extKeyUsage:** `serverAuth`
   - **basicConstraints:** `CA:FALSE`
3. Write `cert_path` mode `0644`, `key_path` mode `0600`. Atomic write via tempfile + `os.replace` so a crash mid-write can't leave a half-cert.
4. Return `CertInfo` for boot logging and the SPA download endpoint.

### Bootstrap config: extend `WebConfig`

`src/gilbert/config.py:116`:

```python
class TlsConfig(BaseModel):
    enabled: bool = True
    https_port: int = 8443
    cert_path: str = ".gilbert/credentials/tls.crt"
    key_path: str = ".gilbert/credentials/tls.key"

class WebConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    tls: TlsConfig = TlsConfig()
```

Default storage in `.gilbert/credentials/` keeps key material in the gitignored data dir (consistent with `feedback_no_private_data`). `tls.enabled: false` in `.gilbert/config.yaml` is the escape hatch.

Update `gilbert.yaml` with the `tls:` block + comments documenting the defaults.

### Dual uvicorn in `src/gilbert/__main__.py`

Today's flow runs a single `uvicorn.Server`. New flow:

```python
servers: list[uvicorn.Server] = [build_http_server(web_app, cfg)]
if cfg.web.tls.enabled:
    try:
        cert_info = ensure_self_signed_cert(
            Path(cfg.web.tls.cert_path), Path(cfg.web.tls.key_path)
        )
        servers.append(build_https_server(web_app, cfg, cert_info))
        log.info("HTTPS listening on https://%s:%d (cert valid until %s, SAN: %s)",
                 cfg.web.host, cfg.web.tls.https_port,
                 cert_info.not_valid_after.date(), ", ".join(cert_info.san_entries))
    except Exception:
        log.exception("Failed to set up TLS — running HTTP-only")

gilbert.set_shutdown_callback(
    lambda: [setattr(s, "should_exit", True) for s in servers]
)

await asyncio.gather(*(s.serve() for s in servers))
```

Both servers serve the same `web_app` instance — no app-state divergence between them. Shutdown callback flips `should_exit` on all servers so the existing signal handler still works.

### Cert download routes

New file: `src/gilbert/web/routes/tls.py`. Two endpoints:

| Route | Returns | Notes |
|---|---|---|
| `GET /api/tls/cert.crt` | Cert file bytes | `Content-Type: application/x-x509-ca-cert`, `Content-Disposition: attachment; filename="gilbert.crt"`. 404 if `tls.enabled=False` or file missing. |
| `GET /api/tls/info` | `{san: [...], not_valid_after: ISO8601, sha256_fingerprint: "AB:CD:..."}` | Read from `CertInfo` cached on `app.state`. 404 if disabled. |

**Auth posture:** both routes are added to the auth-middleware allowlist in `src/gilbert/web/auth.py`. The cert is public; the user can't authenticate until they've trusted the cert; therefore the routes that bootstrap that trust must be reachable unauthenticated. The key is never exposed by any route.

`CertInfo` is computed once at boot and stashed on `app.state.tls_info` so the routes don't re-read the file on every hit.

### "Set up HTTPS" SPA page

New top-level route in the SPA: `/setup-https`. Top-level (not under `/settings`) so it's reachable without an authenticated session. Linked from the Settings nav as well, for convenience once the user is logged in.

Layout (single-screen, no tabs):

- Header explaining why this exists (mic access requires HTTPS).
- Cert summary panel: SHA-256 fingerprint, expiry date, SAN list (rendered from `/api/tls/info`).
- Big "Download gilbert.crt" button hitting `/api/tls/cert.crt`.
- Five collapsible `<details>` sections — one per OS — with the exact install steps:
  - **macOS:** Keychain Access → drag cert → set to "Always Trust" for SSL.
  - **iOS / iPadOS:** AirDrop or email the .crt → install profile → Settings → General → About → Certificate Trust Settings → enable.
  - **Android:** Settings → Security → Encryption & credentials → Install a certificate → CA certificate.
  - **Windows:** Double-click the .crt → Install Certificate → Local Machine → Trusted Root Certification Authorities.
  - **Linux (Chrome / Firefox):** Chrome — `chrome://certificate-manager` → Authorities → Import. Firefox — Settings → Privacy & Security → Certificates → View Certificates → Authorities → Import.

Implementation lives in `frontend/src/pages/SetupHttpsPage.tsx` + nav wiring in core's SPA (this is a core feature, not a plugin).

## Failure modes

| Failure | Behavior |
|---|---|
| `ensure_self_signed_cert` raises (disk full, missing perms, hostname resolution error) | Log with stack trace; log "HTTPS disabled — running HTTP only"; continue boot. Never crash. |
| Cert files exist but are unreadable (manual tampering with perms) | Same as above. User can `rm` the files and restart to regenerate. |
| HTTPS port already in use | Uvicorn raises during `serve()`; `asyncio.gather` propagates. Treated the same as today's HTTP port collision — exit with a clear log line. |
| Cert is within 7 days of expiry on boot | Regenerated. Existing per-device trust breaks once; users re-trust. Boot log warns for the week leading up to expiry. |
| User replaces the cert with mkcert / a real LE cert | Works — `ensure_self_signed_cert` checks validity and returns existing files untouched. The SPA page displays whatever's in the cert. |
| User adds a new LAN IP after first boot | Cert won't cover it. The Setup HTTPS page documents: "If your IP changed, delete `.gilbert/credentials/tls.{crt,key}` and restart to regenerate." |
| `/api/tls/cert.crt` requested while disabled | 404 with body explaining TLS is disabled. |

## Testing

### Unit — `tests/unit/core/test_tls.py`

- Fresh generation writes a parseable cert + key when files are absent.
- SAN list contains the expected DNS names and IP addresses (stub `socket.gethostname` and the UDP-trick socket so the test is deterministic).
- Key file permissions are `0600`; cert file is world-readable (`0644`).
- Idempotency: second call with valid existing files returns the same paths; cert/key file mtimes unchanged.
- Near-expiry regen: write a cert with `not_valid_after = now + 3 days`, call ensure, assert it was regenerated.
- Corrupt cert file regenerates cleanly.
- Atomic write: monkeypatch the write to crash after the tempfile is created — verify the original cert (if any) survives, no half-written file at the final path.

### Unit — `tests/unit/web/test_tls_routes.py`

- `GET /api/tls/cert.crt` returns the file bytes with the right Content-Type and Content-Disposition.
- `GET /api/tls/info` returns the expected JSON shape (`san`, `not_valid_after`, `sha256_fingerprint`).
- Both routes 404 when `tls.enabled=False`.
- Both routes reachable without an authenticated session (verifies allowlist wiring).
- Path-traversal smoke test: the cert handler reads only the configured `cert_path`, not from a path derived from request input.

### Integration — `tests/integration/test_https_boot.py`

- Boot Gilbert with `tls.enabled=True` and a temp cert dir, hit `https://localhost:<port>/health` with `ssl._create_unverified_context`, assert 200.
- Boot with `tls.enabled=False`, assert the HTTPS port is NOT listening but HTTP is.

## Layer compliance

Sanity-check against `validate-architecture` before code lands. Expected hot spots:

- **`core/tls.py`** must import only stdlib + `cryptography`. No imports from `core/services/`, `integrations/`, `web/`, or `storage/`.
- **`web/routes/tls.py`** must not embed business logic — it just reads `app.state.tls_info` and serves the file. SAN computation and cert generation stay in `core/tls.py`.
- **`__main__.py`** is the composition root and may import `core.tls` and concrete uvicorn types — that's fine.
- **SPA page** lives in `frontend/src/pages/`, not a plugin (TLS is a core bootstrap concern, not a plugin feature).

## Migration

- First boot after upgrade: cert auto-generated to `.gilbert/credentials/tls.{crt,key}`. HTTPS comes up on port 8443 alongside the existing HTTP listener. No user action required.
- Boot log surfaces the HTTPS URL and SAN list so the user knows the new origin.
- Existing HTTP usage (bookmarks, Sonos, IoT) is unaffected.
- Disable via `.gilbert/config.yaml` `web.tls.enabled: false` if anything goes wrong.

## Open questions

None — all design choices confirmed during brainstorming.
