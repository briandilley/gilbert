"""Tests for /api/tls/* routes."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
        not_valid_after=datetime.now(UTC) + timedelta(days=365 * 10),
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


def test_cert_routes_are_in_auth_allowlist() -> None:
    """If these aren't in the allowlist, the auth middleware
    redirects pre-auth requests away from the cert routes, which
    defeats the whole point."""
    from gilbert.web.auth import _PUBLIC_EXACT
    assert "/api/tls/cert.crt" in _PUBLIC_EXACT
    assert "/api/tls/info" in _PUBLIC_EXACT
    assert "/setup-https" in _PUBLIC_EXACT
