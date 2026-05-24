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
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address, IPv6Address, ip_address
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

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
    now = datetime.now(UTC)
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
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
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
    if not_after - datetime.now(UTC) < _NEAR_EXPIRY:
        logger.warning("Existing TLS cert expires %s — regenerating", not_after.date())
        return None

    san_strings: list[str] = []
    try:
        san_ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        san_strings.extend(san_ext.get_values_for_type(x509.DNSName))
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
    deduped: list[str] = []
    for n in dns_names:
        if n not in seen:
            seen.add(n)
            deduped.append(n)
    dns_names = deduped

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
        return str(sock.getsockname()[0])
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
