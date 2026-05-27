"""Tests for the InternalUrlService (backend-agnostic + sslip wiring)."""

from typing import Any

from gilbert.core.services.internal_url import InternalUrlService
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver


class _FakeConfig(Service):
    """Minimal ConfigurationReader-satisfying service for tests."""

    def __init__(self, sections: dict[str, dict[str, Any]]) -> None:
        self._sections = sections

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(name="configuration", capabilities=frozenset({"configuration"}))

    def get(self, path: str) -> Any:
        return None

    def get_section(self, namespace: str) -> dict[str, Any]:
        return dict(self._sections.get(namespace, {}))

    def get_section_safe(self, namespace: str) -> dict[str, Any]:
        return self.get_section(namespace)

    async def set(self, path: str, value: Any) -> dict[str, Any]:
        return {}


class _Resolver(ServiceResolver):
    def __init__(self, services: dict[str, Service]) -> None:
        self._services = services

    def get_capability(self, capability: str) -> Service | None:
        return self._services.get(capability)

    def require_capability(self, capability: str) -> Service:
        svc = self.get_capability(capability)
        if svc is None:
            raise LookupError(capability)
        return svc

    def get_all(self, capability: str) -> list[Service]:
        svc = self.get_capability(capability)
        return [svc] if svc else []


def _resolver(internal_url_section: dict[str, Any], web_section: dict[str, Any]) -> _Resolver:
    return _Resolver(
        {"configuration": _FakeConfig({"internal_url": internal_url_section, "web": web_section})}
    )


def test_service_info() -> None:
    svc = InternalUrlService()
    info = svc.service_info()
    assert info.name == "internal_url"
    assert "internal_url" in info.capabilities
    assert info.toggleable is True


def test_config_params_include_backend_and_sslip_settings() -> None:
    svc = InternalUrlService()
    keys = [p.key for p in svc.config_params()]
    assert "backend" in keys
    assert "settings.dns_suffix" in keys
    assert "settings.ip_override" in keys


async def test_disabled_service_is_inert() -> None:
    svc = InternalUrlService()
    await svc.start(_resolver({"enabled": False}, {"tls": {"enabled": True}}))
    assert svc.internal_url == ""
    assert svc.internal_url_for("/x") == ""


async def test_enabled_service_resolves_https_url() -> None:
    svc = InternalUrlService()
    section = {
        "enabled": True,
        "backend": "sslip",
        "settings": {"ip_override": "192.168.1.50"},
    }
    web = {"port": 8000, "tls": {"enabled": True, "https_port": 8443}}
    await svc.start(_resolver(section, web))
    assert svc.internal_url == "https://192-168-1-50.sslip.io:8443"
    assert (
        svc.internal_url_for("auth/callback")
        == "https://192-168-1-50.sslip.io:8443/auth/callback"
    )


async def test_enabled_service_uses_http_port_when_tls_disabled() -> None:
    svc = InternalUrlService()
    section = {"enabled": True, "settings": {"ip_override": "10.0.0.2"}}
    web = {"port": 8000, "tls": {"enabled": False, "https_port": 8443}}
    await svc.start(_resolver(section, web))
    assert svc.internal_url == "http://10-0-0-2.sslip.io:8000"


async def test_unknown_backend_raises() -> None:
    svc = InternalUrlService()
    section = {"enabled": True, "backend": "does-not-exist"}
    try:
        await svc.start(_resolver(section, {"tls": {"enabled": True}}))
    except ValueError as exc:
        assert "does-not-exist" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown backend")


async def test_resolve_failure_leaves_service_inert(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "gilbert.integrations.sslip_internal_url.detect_outbound_ip",
        lambda: None,
    )
    svc = InternalUrlService()
    # No ip_override + detection returns None → backend raises → caught.
    section = {"enabled": True, "settings": {}}
    await svc.start(_resolver(section, {"tls": {"enabled": True}}))
    assert svc.internal_url == ""
