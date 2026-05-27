"""Tests for the sslip.io internal-URL backend."""

import pytest

from gilbert.integrations.sslip_internal_url import SslipInternalUrlBackend


async def test_resolve_uses_ip_override_and_dashes_host() -> None:
    backend = SslipInternalUrlBackend()
    url = await backend.resolve(
        8443, "https", {"ip_override": "192.168.1.50", "dns_suffix": "sslip.io"}
    )
    assert url == "https://192-168-1-50.sslip.io:8443"


async def test_resolve_default_suffix() -> None:
    backend = SslipInternalUrlBackend()
    url = await backend.resolve(8443, "https", {"ip_override": "10.0.0.2"})
    assert url == "https://10-0-0-2.sslip.io:8443"


async def test_resolve_honors_alternate_suffix() -> None:
    backend = SslipInternalUrlBackend()
    url = await backend.resolve(
        8000, "http", {"ip_override": "10.0.0.2", "dns_suffix": "nip.io"}
    )
    assert url == "http://10-0-0-2.nip.io:8000"


@pytest.mark.parametrize(
    "scheme,port",
    [("https", 443), ("http", 80)],
)
async def test_resolve_elides_default_ports(scheme: str, port: int) -> None:
    backend = SslipInternalUrlBackend()
    url = await backend.resolve(port, scheme, {"ip_override": "192.168.1.50"})
    assert url == f"{scheme}://192-168-1-50.sslip.io"


async def test_resolve_auto_detects_when_no_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gilbert.integrations.sslip_internal_url.detect_outbound_ip",
        lambda: "172.16.0.9",
    )
    backend = SslipInternalUrlBackend()
    url = await backend.resolve(8443, "https", {})
    assert url == "https://172-16-0-9.sslip.io:8443"


async def test_resolve_raises_when_no_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gilbert.integrations.sslip_internal_url.detect_outbound_ip",
        lambda: None,
    )
    backend = SslipInternalUrlBackend()
    with pytest.raises(RuntimeError, match="outbound LAN IP"):
        await backend.resolve(8443, "https", {})


async def test_test_resolve_action_reports_host() -> None:
    backend = SslipInternalUrlBackend()
    result = await backend.invoke_backend_action(
        "test_resolve", {"config": {"ip_override": "192.168.1.50"}}
    )
    assert result.status == "ok"
    assert result.data["host"] == "192-168-1-50.sslip.io"
    assert result.data["ip"] == "192.168.1.50"


async def test_test_resolve_action_errors_without_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "gilbert.integrations.sslip_internal_url.detect_outbound_ip",
        lambda: None,
    )
    backend = SslipInternalUrlBackend()
    result = await backend.invoke_backend_action("test_resolve", {"config": {}})
    assert result.status == "error"


async def test_unknown_action() -> None:
    backend = SslipInternalUrlBackend()
    result = await backend.invoke_backend_action("nope", {})
    assert result.status == "error"


def test_backend_is_registered() -> None:
    assert "sslip" in SslipInternalUrlBackend.registered_backends()
