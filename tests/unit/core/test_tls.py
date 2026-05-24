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
