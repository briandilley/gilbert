# Self-Signed HTTPS on First Boot — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Gilbert auto-generates a self-signed TLS certificate on first boot and serves HTTPS in parallel with HTTP so browsers on the LAN can unlock `getUserMedia` for the voice-agent plugin.

**Architecture:** Pure-Python cert generator (`core/tls.py`) using the `cryptography` library writes cert + key to `.gilbert/credentials/` on first boot. `WebConfig.tls` adds bootstrap config. `__main__.py` runs two `uvicorn.Server` instances concurrently sharing the same FastAPI app — HTTP on the existing port, HTTPS on a new one. A public, allowlisted `/api/tls/*` route pair serves the cert + metadata to a "Set up HTTPS" SPA page with per-OS install instructions.

**Tech Stack:** Python 3.12, `cryptography` (already transitive), `uvicorn` (already direct), FastAPI, pytest, React + TypeScript + Vite + Tailwind (existing SPA stack).

**Spec:** `docs/superpowers/specs/2026-05-24-self-signed-https-design.md`

---

## File Structure

**Create:**
- `src/gilbert/core/tls.py` — cert generation utility (`ensure_self_signed_cert`, `CertInfo`, SAN detection helpers).
- `src/gilbert/web/routes/tls.py` — `/api/tls/cert.crt` + `/api/tls/info` endpoints.
- `tests/unit/core/test_tls.py` — cert generation unit tests.
- `tests/unit/web/test_tls_routes.py` — route unit tests.
- `tests/integration/test_https_boot.py` — end-to-end boot test.
- `frontend/src/api/tls.ts` — SPA API client (one `fetchTlsInfo()` call).
- `frontend/src/components/system/SetupHttpsPage.tsx` — the page.

**Modify:**
- `pyproject.toml` — promote `cryptography` from transitive to direct dep.
- `src/gilbert/config.py` — add `TlsConfig`, attach to `WebConfig`.
- `gilbert.yaml` — document the `web.tls.*` block.
- `src/gilbert/__main__.py` — dual uvicorn server flow.
- `src/gilbert/web/__init__.py` — include the new TLS router.
- `src/gilbert/web/auth.py` — add `/api/tls/cert.crt`, `/api/tls/info`, `/setup-https` to the public allowlist.
- `frontend/src/App.tsx` — register `/setup-https` route outside `ProtectedRoute`.
- `frontend/src/components/settings/SettingsPage.tsx` — link to `/setup-https` (if a settings nav structure exists; otherwise skip).

---

## Task 1: Promote `cryptography` to direct dep

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Inspect current `cryptography` usage**

Run: `uv run python -c "import cryptography; print(cryptography.__version__)"`
Expected: prints `46.0.6` (or similar) — confirms it's already installable, just not declared directly.

- [ ] **Step 2: Add `cryptography>=44` to `[project] dependencies` in `pyproject.toml`**

Find the dependencies list (likely near `"uvicorn[standard]>=0.43.0"`) and add:

```toml
"cryptography>=44.0.0",
```

Keep the list alphabetical if it already is; otherwise append.

- [ ] **Step 3: Re-sync the workspace**

Run: `uv sync`
Expected: succeeds; `cryptography` now appears as a direct dep in `uv.lock`'s top-level package set.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "deps: promote cryptography to direct dependency

Needed for the self-signed TLS cert generator in core/tls.py."
```

---

## Task 2: Cert generation module — failing tests first

**Files:**
- Create: `tests/unit/core/test_tls.py`

- [ ] **Step 1: Create the test file with the full suite (all tests will fail since `core/tls.py` doesn't exist yet)**

```python
"""Tests for gilbert.core.tls — self-signed certificate generation."""
from __future__ import annotations

import os
import socket
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from gilbert.core.tls import CertInfo, ensure_self_signed_cert


@pytest.fixture
def cert_paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "tls.crt", tmp_path / "tls.key"


def _load_cert(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def test_generates_cert_when_missing(cert_paths: tuple[Path, Path]) -> None:
    cert_path, key_path = cert_paths
    info = ensure_self_signed_cert(cert_path, key_path)
    assert cert_path.exists()
    assert key_path.exists()
    assert isinstance(info, CertInfo)
    assert info.cert_path == cert_path
    assert info.key_path == key_path
    # Cert is parseable and self-signed.
    cert = _load_cert(cert_path)
    assert cert.subject == cert.issuer


def test_san_includes_localhost_and_loopback_ips(cert_paths: tuple[Path, Path]) -> None:
    cert_path, key_path = cert_paths
    ensure_self_signed_cert(cert_path, key_path)
    cert = _load_cert(cert_path)
    san_ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    dns_names = [n.value for n in san_ext.get_values_for_type(x509.DNSName)]
    ip_strs = [str(ip) for ip in san_ext.get_values_for_type(x509.IPAddress)]
    assert "localhost" in dns_names
    assert "127.0.0.1" in ip_strs
    assert "::1" in ip_strs


def test_san_includes_hostname_and_outbound_ip(cert_paths: tuple[Path, Path]) -> None:
    cert_path, key_path = cert_paths
    with patch("gilbert.core.tls.socket.gethostname", return_value="test-host"), \
         patch("gilbert.core.tls._detect_outbound_ip", return_value="192.168.1.42"):
        ensure_self_signed_cert(cert_path, key_path)
    cert = _load_cert(cert_path)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    dns_names = [n.value for n in san.get_values_for_type(x509.DNSName)]
    ip_strs = [str(ip) for ip in san.get_values_for_type(x509.IPAddress)]
    assert "test-host" in dns_names
    assert "test-host.local" in dns_names
    assert "192.168.1.42" in ip_strs


def test_key_file_permissions_are_0600(cert_paths: tuple[Path, Path]) -> None:
    cert_path, key_path = cert_paths
    ensure_self_signed_cert(cert_path, key_path)
    mode = stat.S_IMODE(key_path.stat().st_mode)
    assert mode == 0o600, f"key file mode is {oct(mode)}, expected 0o600"


def test_cert_file_world_readable(cert_paths: tuple[Path, Path]) -> None:
    cert_path, key_path = cert_paths
    ensure_self_signed_cert(cert_path, key_path)
    mode = stat.S_IMODE(cert_path.stat().st_mode)
    # World-read bit set.
    assert mode & 0o004, f"cert file mode {oct(mode)} is not world-readable"


def test_idempotent_when_existing_cert_is_valid(cert_paths: tuple[Path, Path]) -> None:
    cert_path, key_path = cert_paths
    info1 = ensure_self_signed_cert(cert_path, key_path)
    mtime_cert = cert_path.stat().st_mtime_ns
    mtime_key = key_path.stat().st_mtime_ns

    # Sleep enough that a regeneration would produce a different mtime.
    time.sleep(0.05)
    info2 = ensure_self_signed_cert(cert_path, key_path)

    assert cert_path.stat().st_mtime_ns == mtime_cert
    assert key_path.stat().st_mtime_ns == mtime_key
    assert info1.sha256_fingerprint == info2.sha256_fingerprint


def test_regenerates_when_cert_near_expiry(cert_paths: tuple[Path, Path]) -> None:
    cert_path, key_path = cert_paths
    # First, get a real cert + key on disk, then rewrite the cert
    # part with a manually-built short-expiry cert that re-uses the
    # same key (so the regen path can tell "valid PEM, expiring soon").
    ensure_self_signed_cert(cert_path, key_path)
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    soon = datetime.now(timezone.utc) + timedelta(days=3)
    near_expiry = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "x")]))
        .issuer_name(x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "x")]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(soon)
        .sign(key, hashes.SHA256())
    )
    cert_path.write_bytes(near_expiry.public_bytes(serialization.Encoding.PEM))
    info = ensure_self_signed_cert(cert_path, key_path)
    # Should have regenerated — new cert valid for ~10 years.
    assert info.not_valid_after > datetime.now(timezone.utc) + timedelta(days=365 * 9)


def test_regenerates_when_cert_corrupt(cert_paths: tuple[Path, Path]) -> None:
    cert_path, key_path = cert_paths
    cert_path.write_bytes(b"not a certificate")
    key_path.write_bytes(b"not a key")
    info = ensure_self_signed_cert(cert_path, key_path)
    assert info.not_valid_after > datetime.now(timezone.utc) + timedelta(days=365 * 9)
    # Files were rewritten with parseable content.
    _load_cert(cert_path)


def test_atomic_write_preserves_original_on_crash(cert_paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    cert_path, key_path = cert_paths
    # Place a known-good cert first.
    info_original = ensure_self_signed_cert(cert_path, key_path)
    original_bytes = cert_path.read_bytes()

    # Force a near-expiry to trigger regen on the next call, then
    # crash inside the write path.
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    soon = datetime.now(timezone.utc) + timedelta(days=3)
    short = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "x")]))
        .issuer_name(x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "x")]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(soon)
        .sign(key, hashes.SHA256())
    )
    cert_path.write_bytes(short.public_bytes(serialization.Encoding.PEM))

    real_replace = os.replace
    def crashing_replace(src: str, dst: str) -> None:
        raise OSError("simulated crash")
    monkeypatch.setattr("gilbert.core.tls.os.replace", crashing_replace)

    with pytest.raises(OSError):
        ensure_self_signed_cert(cert_path, key_path)

    # Original cert untouched (still the short-expiry one we wrote);
    # no half-written cert at final path.
    assert cert_path.read_bytes() == short.public_bytes(serialization.Encoding.PEM)
    # No leftover .tmp files in the directory.
    leftovers = [p for p in cert_path.parent.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_cert_validity_is_ten_years(cert_paths: tuple[Path, Path]) -> None:
    cert_path, key_path = cert_paths
    info = ensure_self_signed_cert(cert_path, key_path)
    delta = info.not_valid_after - datetime.now(timezone.utc)
    # 10 years, with a day of slop.
    assert timedelta(days=365 * 10 - 2) <= delta <= timedelta(days=365 * 10 + 2)


def test_sha256_fingerprint_format(cert_paths: tuple[Path, Path]) -> None:
    cert_path, key_path = cert_paths
    info = ensure_self_signed_cert(cert_path, key_path)
    # AB:CD:EF:... — 32 hex pairs joined by colons.
    parts = info.sha256_fingerprint.split(":")
    assert len(parts) == 32
    assert all(len(p) == 2 and int(p, 16) >= 0 for p in parts)
```

- [ ] **Step 2: Run the suite to confirm it fails with `ModuleNotFoundError`**

Run: `uv run pytest tests/unit/core/test_tls.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'gilbert.core.tls'`.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/core/test_tls.py
git commit -m "test(tls): failing tests for self-signed cert generator"
```

---

## Task 3: Implement `core/tls.py`

**Files:**
- Create: `src/gilbert/core/tls.py`

- [ ] **Step 1: Write the module**

```python
"""Self-signed TLS certificate generation.

Used at boot to give Gilbert a working ``https://`` listener so
browsers on the LAN can satisfy the secure-context requirement
needed by ``navigator.mediaDevices.getUserMedia`` (mic / camera).

Pure utility: no service plumbing, no imports from
``core/services/``, ``integrations/``, ``web/``, or ``storage/``.
Depends only on stdlib + ``cryptography``.
"""
from __future__ import annotations

import logging
import os
import socket
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from ipaddress import IPv4Address, IPv6Address, ip_address
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

logger = logging.getLogger(__name__)

# Regenerate if the existing cert expires within this window.
_NEAR_EXPIRY = timedelta(days=7)
# How long fresh certs are valid for (matches mkcert's default).
_VALIDITY = timedelta(days=365 * 10)
# RSA key size for the leaf. 2048 is the modern minimum.
_KEY_SIZE = 2048


@dataclass(frozen=True)
class CertInfo:
    """Metadata about the active server certificate."""

    cert_path: Path
    key_path: Path
    not_valid_after: datetime
    san_entries: list[str]
    sha256_fingerprint: str


def ensure_self_signed_cert(cert_path: Path, key_path: Path) -> CertInfo:
    """Return existing cert + key if present and valid; otherwise generate.

    Args:
        cert_path: Destination for the PEM-encoded certificate.
        key_path: Destination for the PEM-encoded private key.

    Returns:
        ``CertInfo`` describing whichever cert is now on disk.
    """
    existing = _load_existing(cert_path, key_path)
    if existing is not None:
        logger.info(
            "Using existing TLS cert at %s (valid until %s)",
            cert_path,
            existing.not_valid_after.date(),
        )
        return existing

    logger.info("Generating self-signed TLS cert at %s", cert_path)
    cert_path.parent.mkdir(parents=True, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=_KEY_SIZE)
    san_list, san_strings = _build_san_list()
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Gilbert (self-signed)")]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Gilbert (self-signed)")]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + _VALIDITY)
        .add_extension(x509.SubjectAlternativeName(san_list), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    cert_bytes = cert.public_bytes(serialization.Encoding.PEM)
    key_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    _atomic_write(cert_path, cert_bytes, mode=0o644)
    _atomic_write(key_path, key_bytes, mode=0o600)

    fingerprint = ":".join(f"{b:02X}" for b in cert.fingerprint(hashes.SHA256()))
    info = CertInfo(
        cert_path=cert_path,
        key_path=key_path,
        not_valid_after=cert.not_valid_after_utc,
        san_entries=san_strings,
        sha256_fingerprint=fingerprint,
    )
    logger.info(
        "Generated TLS cert (SHA256=%s, expires=%s, SAN=%s)",
        fingerprint,
        info.not_valid_after.date(),
        ", ".join(san_strings),
    )
    return info


def _load_existing(cert_path: Path, key_path: Path) -> CertInfo | None:
    if not (cert_path.exists() and key_path.exists()):
        return None
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    except Exception:
        logger.warning("Existing TLS cert/key is unreadable; regenerating", exc_info=True)
        return None

    not_after = cert.not_valid_after_utc
    if not_after - datetime.now(timezone.utc) < _NEAR_EXPIRY:
        logger.warning("Existing TLS cert expires %s — regenerating", not_after.date())
        return None

    san_strings: list[str] = []
    try:
        san_ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        san_strings.extend(n.value for n in san_ext.get_values_for_type(x509.DNSName))
        san_strings.extend(str(ip) for ip in san_ext.get_values_for_type(x509.IPAddress))
    except x509.ExtensionNotFound:
        pass

    fingerprint = ":".join(f"{b:02X}" for b in cert.fingerprint(hashes.SHA256()))
    return CertInfo(
        cert_path=cert_path,
        key_path=key_path,
        not_valid_after=not_after,
        san_entries=san_strings,
        sha256_fingerprint=fingerprint,
    )


def _build_san_list() -> tuple[list[x509.GeneralName], list[str]]:
    """Return (cryptography SAN objects, human-readable strings)."""
    dns_names: list[str] = ["localhost"]
    ip_addrs: list[IPv4Address | IPv6Address] = [
        ip_address("127.0.0.1"),
        ip_address("::1"),
    ]

    hostname = socket.gethostname()
    if hostname and hostname != "localhost":
        dns_names.append(hostname)
        dns_names.append(f"{hostname}.local")

    # Hostname-resolved addresses (whatever the OS thinks "this machine" is).
    try:
        for info in socket.getaddrinfo(hostname, None):
            try:
                addr = ip_address(info[4][0])
            except ValueError:
                continue
            if not addr.is_loopback and addr not in ip_addrs:
                ip_addrs.append(addr)
    except OSError:
        pass

    # Primary outbound IP — what other LAN devices see when reaching us.
    outbound = _detect_outbound_ip()
    if outbound is not None:
        try:
            addr = ip_address(outbound)
            if not addr.is_loopback and addr not in ip_addrs:
                ip_addrs.append(addr)
        except ValueError:
            pass

    # Dedupe DNS names while preserving order.
    seen: set[str] = set()
    dns_names = [n for n in dns_names if not (n in seen or seen.add(n))]

    sans: list[x509.GeneralName] = [x509.DNSName(n) for n in dns_names]
    sans.extend(x509.IPAddress(a) for a in ip_addrs)
    san_strings = dns_names + [str(a) for a in ip_addrs]
    return sans, san_strings


def _detect_outbound_ip() -> str | None:
    """Return the primary outbound IPv4 the OS would use for an external
    destination, without actually sending any traffic."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 53))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def _atomic_write(path: Path, content: bytes, *, mode: int) -> None:
    """Write ``content`` to ``path`` atomically with the requested mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
```

- [ ] **Step 2: Run the test suite**

Run: `uv run pytest tests/unit/core/test_tls.py -v`
Expected: all tests pass.

- [ ] **Step 3: Lint + type check**

Run: `uv run ruff check src/gilbert/core/tls.py tests/unit/core/test_tls.py`
Expected: no errors.

Run: `uv run mypy src/gilbert/core/tls.py`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add src/gilbert/core/tls.py
git commit -m "feat(tls): self-signed cert generator

Generates a 10-year RSA cert with SAN coverage for localhost, the
machine hostname (+ .local), loopback IPs, hostname-resolved
addresses, and the primary outbound LAN IP. Idempotent when an
existing valid cert is on disk; regenerates near expiry or when
files are corrupt. Atomic writes (tempfile + os.replace) so a
crash mid-write never leaves a half-cert."
```

---

## Task 4: Bootstrap config — `TlsConfig`

**Files:**
- Modify: `src/gilbert/config.py` (after `WebConfig` at line 116)
- Modify: `gilbert.yaml` (the `web:` block)

- [ ] **Step 1: Add `TlsConfig` and attach to `WebConfig`**

In `src/gilbert/config.py`, replace the existing `WebConfig` (line 116):

```python
class TlsConfig(BaseModel):
    """TLS / HTTPS configuration."""

    enabled: bool = True
    https_port: int = 8443
    cert_path: str = ".gilbert/credentials/tls.crt"
    key_path: str = ".gilbert/credentials/tls.key"


class WebConfig(BaseModel):
    """Web server configuration."""

    host: str = "0.0.0.0"
    port: int = 8000
    tls: TlsConfig = TlsConfig()
```

(Put `TlsConfig` immediately before `WebConfig` so the reference is in scope.)

- [ ] **Step 2: Document the block in `gilbert.yaml`**

Find the `web:` block (currently `host` + `port`) and replace with:

```yaml
web:
  host: "0.0.0.0"
  port: 8000
  # HTTPS on a self-signed cert so phones / other LAN browsers can
  # unlock getUserMedia (mic). Disable by setting enabled: false in
  # .gilbert/config.yaml if you terminate TLS elsewhere. Cert files
  # auto-generated on first boot; delete them and restart to force
  # regeneration (e.g. after the host IP changes).
  tls:
    enabled: true
    https_port: 8443
    cert_path: .gilbert/credentials/tls.crt
    key_path: .gilbert/credentials/tls.key
```

- [ ] **Step 3: Sanity-check the config loads**

Run: `uv run python -c "from gilbert.config import load_config; c = load_config(); print(c.web.tls.enabled, c.web.tls.https_port)"`
Expected: prints `True 8443`.

- [ ] **Step 4: Commit**

```bash
git add src/gilbert/config.py gilbert.yaml
git commit -m "config(web): add TlsConfig bootstrap block

enabled/https_port/cert_path/key_path. Defaults: HTTPS on, port
8443, cert + key under .gilbert/credentials/."
```

---

## Task 5: Dual uvicorn in `__main__.py`

**Files:**
- Modify: `src/gilbert/__main__.py`

- [ ] **Step 1: Refactor `main()` to support two `uvicorn.Server` instances**

Replace the existing single-server block (currently lines 52–94) with:

```python
    web_app = create_app(gilbert)

    servers: list[uvicorn.Server] = [_build_http_server(web_app, gilbert)]

    if gilbert.config.web.tls.enabled:
        try:
            from gilbert.core.tls import ensure_self_signed_cert

            cert_info = ensure_self_signed_cert(
                Path(gilbert.config.web.tls.cert_path),
                Path(gilbert.config.web.tls.key_path),
            )
            web_app.state.tls_info = cert_info
            servers.append(_build_https_server(web_app, gilbert, cert_info))
            logger.info(
                "HTTPS on https://%s:%d (cert valid until %s, SAN: %s)",
                gilbert.config.web.host,
                gilbert.config.web.tls.https_port,
                cert_info.not_valid_after.date(),
                ", ".join(cert_info.san_entries),
            )
        except Exception:
            logger.exception("Failed to set up TLS — running HTTP-only")

    # Disable uvicorn's own signal handling on every server; we manage it.
    for s in servers:
        s.install_signal_handlers = lambda: None  # type: ignore[attr-defined]

    gilbert.set_shutdown_callback(
        lambda: [setattr(s, "should_exit", True) for s in servers]
    )

    def _handle_signal(signum: int, frame: object) -> None:
        global _signal_count
        _signal_count += 1
        if _signal_count >= 2:
            logger.warning("Forced shutdown (signal %d)", _signal_count)
            _remove_pid()
            os._exit(1)
        logger.info("Shutdown signal received — press Ctrl+C again to force quit")
        for s in servers:
            s.should_exit = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        await asyncio.gather(*(s.serve() for s in servers))
    finally:
        await gilbert.stop()
        _remove_pid()
```

Then add the two builder helpers above `main()`:

```python
def _build_http_server(web_app: object, gilbert: Gilbert) -> uvicorn.Server:
    cfg = uvicorn.Config(
        web_app,
        host=gilbert.config.web.host,
        port=gilbert.config.web.port,
        log_level="info",
        timeout_graceful_shutdown=10,
    )
    return uvicorn.Server(cfg)


def _build_https_server(
    web_app: object, gilbert: Gilbert, cert_info: "CertInfo"
) -> uvicorn.Server:
    cfg = uvicorn.Config(
        web_app,
        host=gilbert.config.web.host,
        port=gilbert.config.web.tls.https_port,
        log_level="info",
        timeout_graceful_shutdown=10,
        ssl_certfile=str(cert_info.cert_path),
        ssl_keyfile=str(cert_info.key_path),
    )
    return uvicorn.Server(cfg)
```

Add the import at the top of the file:

```python
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gilbert.core.tls import CertInfo
```

- [ ] **Step 2: Smoke-boot Gilbert**

Run: `./gilbert.sh start &` then `sleep 5 && curl -sk https://localhost:8443/health && curl -s http://localhost:8000/health`
Expected: both return `{"status":"ok"}`. Then stop with `./gilbert.sh stop` (or the equivalent in your `gilbert.sh`).

If the script is interactive in your setup, just run `uv run python -m gilbert` directly in a separate shell instead.

- [ ] **Step 3: Verify the cert files were created**

Run: `ls -l .gilbert/credentials/tls.*`
Expected: `tls.crt` (mode `-rw-r--r--`) and `tls.key` (mode `-rw-------`).

- [ ] **Step 4: Commit**

```bash
git add src/gilbert/__main__.py
git commit -m "feat(__main__): run HTTP and HTTPS uvicorn servers in parallel

HTTPS listens on web.tls.https_port (default 8443) using the cert
generated by core.tls.ensure_self_signed_cert. Same FastAPI app
serves both listeners. TLS failure during boot degrades to
HTTP-only — Gilbert keeps running."
```

---

## Task 6: Cert download routes — failing tests first

**Files:**
- Create: `tests/unit/web/test_tls_routes.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for /api/tls/* routes."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gilbert.core.tls import CertInfo
from gilbert.web.routes.tls import router as tls_router


def _make_app(cert_info: CertInfo | None) -> FastAPI:
    app = FastAPI()
    app.state.tls_info = cert_info
    app.include_router(tls_router)
    return app


@pytest.fixture
def cert_on_disk(tmp_path: Path) -> CertInfo:
    cert_path = tmp_path / "tls.crt"
    key_path = tmp_path / "tls.key"
    cert_path.write_bytes(b"-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n")
    key_path.write_bytes(b"-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n")
    return CertInfo(
        cert_path=cert_path,
        key_path=key_path,
        not_valid_after=datetime.now(timezone.utc) + timedelta(days=365 * 10),
        san_entries=["localhost", "192.168.1.42", "127.0.0.1"],
        sha256_fingerprint=":".join(["AB"] * 32),
    )


def test_download_returns_cert_bytes(cert_on_disk: CertInfo) -> None:
    app = _make_app(cert_on_disk)
    resp = TestClient(app).get("/api/tls/cert.crt")
    assert resp.status_code == 200
    assert resp.content == cert_on_disk.cert_path.read_bytes()
    assert resp.headers["content-type"].startswith("application/x-x509-ca-cert")
    assert "attachment" in resp.headers["content-disposition"]
    assert "gilbert.crt" in resp.headers["content-disposition"]


def test_download_404_when_tls_disabled() -> None:
    resp = TestClient(_make_app(None)).get("/api/tls/cert.crt")
    assert resp.status_code == 404


def test_info_returns_json_shape(cert_on_disk: CertInfo) -> None:
    resp = TestClient(_make_app(cert_on_disk)).get("/api/tls/info")
    assert resp.status_code == 200
    body = resp.json()
    assert body["san"] == ["localhost", "192.168.1.42", "127.0.0.1"]
    assert body["not_valid_after"].startswith(
        cert_on_disk.not_valid_after.date().isoformat()
    )
    assert body["sha256_fingerprint"] == cert_on_disk.sha256_fingerprint


def test_info_404_when_tls_disabled() -> None:
    resp = TestClient(_make_app(None)).get("/api/tls/info")
    assert resp.status_code == 404


def test_key_file_is_not_served(cert_on_disk: CertInfo) -> None:
    """Sanity: there is no route that exposes the private key."""
    client = TestClient(_make_app(cert_on_disk))
    for path in ("/api/tls/tls.key", "/api/tls/key", "/api/tls/private", "/api/tls/cert.key"):
        assert client.get(path).status_code == 404
```

- [ ] **Step 2: Run to confirm failure**

Run: `uv run pytest tests/unit/web/test_tls_routes.py -v`
Expected: `ModuleNotFoundError: No module named 'gilbert.web.routes.tls'`.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/web/test_tls_routes.py
git commit -m "test(tls): failing route tests"
```

---

## Task 7: Implement the TLS routes

**Files:**
- Create: `src/gilbert/web/routes/tls.py`
- Modify: `src/gilbert/web/__init__.py`

- [ ] **Step 1: Write `src/gilbert/web/routes/tls.py`**

```python
"""Public routes that expose the server's TLS certificate.

These are intentionally **unauthenticated** — a user can't log in
to Gilbert until they've trusted the cert, so the routes that
bootstrap that trust must be reachable without a session. Only
the public half (the cert PEM) and metadata are served; the
private key is never touched here.

The corresponding allowlist entries live in ``gilbert.web.auth``.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

router = APIRouter(prefix="/api/tls", tags=["tls"])


def _tls_info(request: Request) -> Any:
    info = getattr(request.app.state, "tls_info", None)
    if info is None:
        raise HTTPException(status_code=404, detail="TLS disabled")
    return info


@router.get("/cert.crt")
async def download_cert(request: Request) -> FileResponse:
    info = _tls_info(request)
    if not info.cert_path.exists():
        raise HTTPException(status_code=404, detail="cert file missing")
    return FileResponse(
        path=str(info.cert_path),
        media_type="application/x-x509-ca-cert",
        filename="gilbert.crt",
    )


@router.get("/info")
async def get_info(request: Request) -> JSONResponse:
    info = _tls_info(request)
    return JSONResponse(
        {
            "san": list(info.san_entries),
            "not_valid_after": info.not_valid_after.isoformat(),
            "sha256_fingerprint": info.sha256_fingerprint,
        }
    )
```

- [ ] **Step 2: Wire the router in `src/gilbert/web/__init__.py`**

In the imports block at the top of `create_app` (after the other `from gilbert.web.routes.* import router as *_router` lines), add:

```python
    from gilbert.web.routes.tls import router as tls_router
```

In the include block (next to the other `app.include_router(...)` calls), add:

```python
    app.include_router(tls_router)
```

- [ ] **Step 3: Run the route tests**

Run: `uv run pytest tests/unit/web/test_tls_routes.py -v`
Expected: all tests pass.

- [ ] **Step 4: Lint**

Run: `uv run ruff check src/gilbert/web/routes/tls.py src/gilbert/web/__init__.py tests/unit/web/test_tls_routes.py`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add src/gilbert/web/routes/tls.py src/gilbert/web/__init__.py
git commit -m "feat(web): /api/tls/cert.crt + /api/tls/info routes

Serves the active server certificate (public half) and metadata
for the Setup HTTPS SPA page. Intentionally unauthenticated —
allowlisting in auth.py follows in the next commit."
```

---

## Task 8: Allowlist the TLS routes + setup page

**Files:**
- Modify: `src/gilbert/web/auth.py` (lines 35–62 area)

- [ ] **Step 1: Add the new entries**

In `src/gilbert/web/auth.py`, find `_PUBLIC_EXACT` (line 35) and add `/api/tls/info`, `/api/tls/cert.crt`, and `/setup-https`:

```python
_PUBLIC_EXACT = (
    "/auth/login",
    "/auth/logout",
    "/auth/session",
    "/auth/me",
    "/api/auth/methods",
    "/api/tls/info",
    "/api/tls/cert.crt",
    "/screens",
    "/setup-https",
)
```

`/setup-https` is the SPA page (served via the SPA fallback in `web/__init__.py`). It needs to be reachable pre-auth because trusting the cert is a precondition for ever authenticating over HTTPS.

- [ ] **Step 2: Write a test that asserts the routes work unauthenticated**

Append to `tests/unit/web/test_tls_routes.py`:

```python
def test_cert_routes_are_in_auth_allowlist() -> None:
    """If these aren't in the allowlist, the auth middleware
    redirects pre-auth requests away from the cert routes, which
    defeats the whole point."""
    from gilbert.web.auth import _PUBLIC_EXACT
    assert "/api/tls/cert.crt" in _PUBLIC_EXACT
    assert "/api/tls/info" in _PUBLIC_EXACT
    assert "/setup-https" in _PUBLIC_EXACT
```

- [ ] **Step 3: Run the suite**

Run: `uv run pytest tests/unit/web/test_tls_routes.py -v`
Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add src/gilbert/web/auth.py tests/unit/web/test_tls_routes.py
git commit -m "auth: allowlist /api/tls/* and /setup-https

Pre-auth access is intentional — these routes bootstrap the
trust users need before they can authenticate over HTTPS."
```

---

## Task 9: Integration test — HTTPS boot end-to-end

**Files:**
- Create: `tests/integration/test_https_boot.py`

- [ ] **Step 1: Write the test**

```python
"""End-to-end HTTPS boot test."""
from __future__ import annotations

import asyncio
import socket
import ssl
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from gilbert.core.tls import ensure_self_signed_cert


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@asynccontextmanager
async def _running_server(cert_path: Path, key_path: Path, port: int):
    app = FastAPI()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        ssl_certfile=str(cert_path),
        ssl_keyfile=str(key_path),
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[attr-defined]
    task = asyncio.create_task(server.serve())
    # Wait for startup.
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "uvicorn HTTPS server failed to start"
    try:
        yield
    finally:
        server.should_exit = True
        await task


@pytest.mark.asyncio
async def test_https_listener_serves_traffic(tmp_path: Path) -> None:
    cert_path = tmp_path / "tls.crt"
    key_path = tmp_path / "tls.key"
    ensure_self_signed_cert(cert_path, key_path)
    port = _free_port()

    async with _running_server(cert_path, key_path, port):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        async with httpx.AsyncClient(verify=ctx) as client:
            resp = await client.get(f"https://127.0.0.1:{port}/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/integration/test_https_boot.py -v`
Expected: pass. (The repo already has `pytest-asyncio` with `asyncio_mode = "auto"`, so the `@pytest.mark.asyncio` decorator is belt-and-suspenders.)

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_https_boot.py
git commit -m "test(tls): integration test for HTTPS uvicorn boot

Builds a real cert via ensure_self_signed_cert, starts uvicorn
with ssl_certfile/ssl_keyfile, hits /health over HTTPS with cert
verification disabled."
```

---

## Task 10: SPA API client for TLS info

**Files:**
- Create: `frontend/src/api/tls.ts`

- [ ] **Step 1: Look at an existing API client for the shape**

Run: `ls frontend/src/api/`
Pick one (e.g. `auth.ts`) and skim it to mirror the conventions — `fetch` helper, error handling, return-shape style.

- [ ] **Step 2: Write `frontend/src/api/tls.ts`**

```typescript
export interface TlsInfo {
  san: string[];
  not_valid_after: string;
  sha256_fingerprint: string;
}

export async function fetchTlsInfo(): Promise<TlsInfo | null> {
  const resp = await fetch("/api/tls/info", { credentials: "same-origin" });
  if (resp.status === 404) return null;
  if (!resp.ok) throw new Error(`fetchTlsInfo failed: ${resp.status}`);
  return (await resp.json()) as TlsInfo;
}

export function certDownloadUrl(): string {
  return "/api/tls/cert.crt";
}
```

- [ ] **Step 3: Type check the SPA**

Run: `cd frontend && npm run typecheck` (or `npx tsc --noEmit` — whichever the repo uses; check `frontend/package.json` scripts).
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/tls.ts
git commit -m "feat(spa): tls API client

fetchTlsInfo() + certDownloadUrl() for the upcoming Setup HTTPS page."
```

---

## Task 11: SPA "Set up HTTPS" page

**Files:**
- Create: `frontend/src/components/system/SetupHttpsPage.tsx`

- [ ] **Step 1: Write the component**

```tsx
import { useEffect, useState } from "react";
import { fetchTlsInfo, certDownloadUrl, type TlsInfo } from "@/api/tls";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";

export function SetupHttpsPage() {
  const [info, setInfo] = useState<TlsInfo | null | "loading">("loading");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchTlsInfo()
      .then(setInfo)
      .catch((err) => {
        setError(err instanceof Error ? err.message : String(err));
        setInfo(null);
      });
  }, []);

  if (info === "loading") return <div className="p-6">Loading…</div>;

  return (
    <div className="mx-auto max-w-3xl space-y-6 p-6">
      <header>
        <h1 className="text-2xl font-semibold">Set up HTTPS</h1>
        <p className="text-muted-foreground mt-1">
          Gilbert generated a self-signed certificate so browsers can grant microphone
          and camera access on the LAN. Trust the certificate on each device once.
        </p>
      </header>

      {info === null && (
        <Card>
          <CardContent className="pt-6">
            HTTPS is disabled or the certificate isn't available.
            {error && <div className="text-destructive mt-2">{error}</div>}
          </CardContent>
        </Card>
      )}

      {info && info !== "loading" && (
        <>
          <Card>
            <CardHeader>
              <CardTitle>Certificate</CardTitle>
              <CardDescription>Active server certificate</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3 text-sm">
              <div>
                <div className="text-muted-foreground">SHA-256 fingerprint</div>
                <code className="font-mono break-all">{info.sha256_fingerprint}</code>
              </div>
              <div>
                <div className="text-muted-foreground">Valid until</div>
                <div>{new Date(info.not_valid_after).toLocaleDateString()}</div>
              </div>
              <div>
                <div className="text-muted-foreground">Covers</div>
                <div className="flex flex-wrap gap-2 pt-1">
                  {info.san.map((s) => (
                    <code
                      key={s}
                      className="bg-muted rounded px-1.5 py-0.5 font-mono text-xs"
                    >
                      {s}
                    </code>
                  ))}
                </div>
              </div>
              <div className="pt-2">
                <a href={certDownloadUrl()} download="gilbert.crt">
                  <Button>Download gilbert.crt</Button>
                </a>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Install on this device</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3 text-sm">
              <OsSection title="macOS">
                <ol className="list-decimal space-y-1 pl-5">
                  <li>Open Keychain Access.</li>
                  <li>Drag <code>gilbert.crt</code> into the <em>System</em> keychain.</li>
                  <li>Double-click the new entry → expand <em>Trust</em> → set <em>When using this certificate</em> to <em>Always Trust</em>.</li>
                  <li>Close and re-authenticate. Restart the browser.</li>
                </ol>
              </OsSection>
              <OsSection title="iOS / iPadOS">
                <ol className="list-decimal space-y-1 pl-5">
                  <li>AirDrop or email the <code>gilbert.crt</code> file to the device, then tap it.</li>
                  <li>Open <em>Settings → General → VPN &amp; Device Management</em>. Tap the downloaded profile → Install.</li>
                  <li>Open <em>Settings → General → About → Certificate Trust Settings</em>.</li>
                  <li>Toggle on <em>Gilbert (self-signed)</em>.</li>
                </ol>
              </OsSection>
              <OsSection title="Android">
                <ol className="list-decimal space-y-1 pl-5">
                  <li>Download <code>gilbert.crt</code> on the device.</li>
                  <li>Open <em>Settings → Security &amp; privacy → More security settings → Encryption &amp; credentials → Install a certificate → CA certificate</em>.</li>
                  <li>Acknowledge the warning and pick the file.</li>
                </ol>
              </OsSection>
              <OsSection title="Windows">
                <ol className="list-decimal space-y-1 pl-5">
                  <li>Double-click <code>gilbert.crt</code> → Install Certificate.</li>
                  <li>Choose <em>Local Machine</em> → Next.</li>
                  <li>Pick <em>Place all certificates in the following store</em> → Browse → <em>Trusted Root Certification Authorities</em>.</li>
                  <li>Finish. Restart the browser.</li>
                </ol>
              </OsSection>
              <OsSection title="Linux (Chrome / Firefox)">
                <ol className="list-decimal space-y-1 pl-5">
                  <li>
                    <strong>Chrome</strong>: visit <code>chrome://certificate-manager</code> → <em>Authorities</em> → Import <code>gilbert.crt</code> → check <em>Trust this certificate for identifying websites</em>.
                  </li>
                  <li>
                    <strong>Firefox</strong>: <em>Settings → Privacy &amp; Security → Certificates → View Certificates → Authorities → Import</em>. Trust for identifying websites.
                  </li>
                </ol>
              </OsSection>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}

function OsSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <details className="rounded border p-3">
      <summary className="cursor-pointer font-medium">{title}</summary>
      <div className="pt-2">{children}</div>
    </details>
  );
}
```

If `@/components/ui/card` or `Button` lives elsewhere in this codebase, adjust the import. Skim `frontend/src/components/ui/` to confirm.

- [ ] **Step 2: Type check**

Run: `cd frontend && npm run typecheck` (or whichever script the repo uses).
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/system/SetupHttpsPage.tsx
git commit -m "feat(spa): Setup HTTPS page

Displays cert fingerprint, expiry, and SAN coverage; provides a
gilbert.crt download button; collapsible per-OS install steps
for macOS, iOS, Android, Windows, and Linux."
```

---

## Task 12: Wire `/setup-https` route into the SPA

**Files:**
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: Register the route outside `ProtectedRoute`**

In `frontend/src/App.tsx`, find the import block and add:

```tsx
import { SetupHttpsPage } from "@/components/system/SetupHttpsPage";
```

Find the existing line `<Route path="/auth/login" element={<LoginPage />} />` and add a sibling line right after it (still outside the `<Route element={<ProtectedRoute />}>` block):

```tsx
      <Route path="/setup-https" element={<SetupHttpsPage />} />
```

This keeps the page reachable without a session — same posture as the login page.

- [ ] **Step 2: Smoke-test in a browser**

Boot Gilbert (`./gilbert.sh start` or the equivalent). Open `http://localhost:8000/setup-https` (no auth needed). Verify:

- The page loads.
- The SAN list, fingerprint, and expiry date show up (fetched from `/api/tls/info`).
- The Download button downloads a `.crt` file.
- Each OS section expands.

Stop Gilbert.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/App.tsx
git commit -m "feat(spa): /setup-https route, reachable without auth

Mounted alongside /auth/login, outside ProtectedRoute, so users
can fetch the cert before they're able to authenticate over HTTPS."
```

---

## Task 13: Architecture audit + final cleanup

- [ ] **Step 1: Run the architecture validation**

Run: invoke the `validate-architecture` skill on this branch's diff.

Expected verdict: clean. Hot spots to re-check personally if the skill flags anything:

- `core/tls.py` imports only stdlib + `cryptography`.
- `web/routes/tls.py` doesn't embed business logic — it reads from `app.state.tls_info` and serves a file. No SAN computation, no cert generation here.
- `__main__.py` is the composition root; importing `core.tls` and uvicorn types is fine.
- SPA page lives in `frontend/src/components/system/`, not a plugin.

- [ ] **Step 2: Run the full test suite**

Run: `uv run pytest`
Expected: pass (including everything you didn't touch).

- [ ] **Step 3: Lint + type check the full Python tree**

Run: `uv run ruff check src/ tests/`
Run: `uv run mypy src/`
Expected: no errors introduced.

- [ ] **Step 4: Update docs**

- README freshness: the spec said HTTPS is now on by default. Skim `README.md` for any "Gilbert listens on http://…" line that's now misleading; update it to mention HTTPS on 8443.
- The Setup HTTPS page itself is the primary documentation; no separate doc needed.

- [ ] **Step 5: Final commit (if any docs changed)**

```bash
git add README.md
git commit -m "docs(readme): note HTTPS on 8443 alongside HTTP"
```

---

## Verification checklist (post-implementation)

Boot Gilbert and verify by hand:

- [ ] `.gilbert/credentials/tls.crt` and `tls.key` exist with `0644` / `0600` perms.
- [ ] `curl -sk https://localhost:8443/health` returns `{"status":"ok"}`.
- [ ] `curl -s http://localhost:8000/health` still returns `{"status":"ok"}`.
- [ ] `curl -sk https://localhost:8443/api/tls/info | jq` shows the SAN list, expiry, and fingerprint.
- [ ] Browser hits `https://<your-LAN-IP>:8443/`, shows the cert warning, allows click-through, lands on Gilbert.
- [ ] After trusting the cert (per the Setup HTTPS page instructions for your OS), the warning is gone and `navigator.mediaDevices` is defined in the browser console.
- [ ] Voice-agent page can request the microphone on the now-HTTPS origin.
- [ ] Setting `web.tls.enabled: false` in `.gilbert/config.yaml` and restarting: HTTP keeps working, HTTPS port is not bound, `/setup-https` page shows "HTTPS is disabled."
