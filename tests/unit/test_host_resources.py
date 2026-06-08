"""Tests for the host-resources capability.

Covers the vendor-free ``LocalHostResources`` probe with ``psutil`` and
``nvidia-smi`` mocked at the module boundary (never requires a real GPU),
plus the ``HostResourcesService`` capability advertisement and delegation.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from gilbert.core.services.host_resources import HostResourcesService
from gilbert.integrations.host_resources import LocalHostResources
from gilbert.interfaces.host_resources import (
    GPUInfo,
    HostResources,
    HostResourcesBackend,
    HostResourcesProvider,
)
from gilbert.interfaces.service import Service, ServiceResolver

_MOD = "gilbert.integrations.host_resources"

# nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits
# emits "<name>, <MiB>" rows, one per GPU.
_NVIDIA_OK = "NVIDIA GeForce RTX 3090, 24576\n"
_MIB = 1024 * 1024


def _vm(total: int, available: int) -> SimpleNamespace:
    return SimpleNamespace(total=total, available=available)


# ── interfaces ────────────────────────────────────────────────────


def test_host_resources_has_gpu_property() -> None:
    assert HostResources(
        total_ram_bytes=1,
        available_ram_bytes=1,
        gpus=(GPUInfo(name="x", total_vram_bytes=None),),
    ).has_gpu
    assert not HostResources(total_ram_bytes=1, available_ram_bytes=1, gpus=()).has_gpu


def test_local_backend_is_registered() -> None:
    assert HostResourcesBackend.registered_backends().get("local") is LocalHostResources


# ── LocalHostResources.probe ──────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_populated_shape() -> None:
    """RAM numbers come from psutil; a parsed GPU carries VRAM in bytes."""
    with (
        patch(
            f"{_MOD}.psutil.virtual_memory",
            return_value=_vm(total=32 * _MIB * 1024, available=16 * _MIB * 1024),
        ),
        patch(f"{_MOD}.shutil.which", return_value="/usr/bin/nvidia-smi"),
        patch(
            f"{_MOD}.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout=_NVIDIA_OK, stderr=""),
        ),
    ):
        result = await LocalHostResources().probe()

    assert isinstance(result, HostResources)
    assert result.total_ram_bytes == 32 * _MIB * 1024
    assert result.available_ram_bytes == 16 * _MIB * 1024
    assert result.has_gpu
    assert len(result.gpus) == 1
    gpu = result.gpus[0]
    assert gpu.name == "NVIDIA GeForce RTX 3090"
    assert gpu.total_vram_bytes == 24576 * _MIB


@pytest.mark.asyncio
async def test_probe_no_nvidia_smi_binary_returns_no_gpus() -> None:
    """nvidia-smi missing → gpus=() and never raises."""
    with (
        patch(
            f"{_MOD}.psutil.virtual_memory",
            return_value=_vm(total=8 * _MIB * 1024, available=4 * _MIB * 1024),
        ),
        patch(f"{_MOD}.shutil.which", return_value=None),
    ):
        result = await LocalHostResources().probe()

    assert result.gpus == ()
    assert not result.has_gpu
    assert result.total_ram_bytes == 8 * _MIB * 1024


@pytest.mark.asyncio
async def test_probe_nvidia_smi_errors_returns_no_gpus() -> None:
    """Any nvidia-smi failure path (non-zero exit, timeout, exception) → gpus=()."""
    cases: list[dict[str, Any]] = [
        {"return_value": SimpleNamespace(returncode=9, stdout="", stderr="boom")},
        {"side_effect": subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=3)},
        {"side_effect": OSError("nope")},
    ]
    for run_kwargs in cases:
        with (
            patch(
                f"{_MOD}.psutil.virtual_memory",
                return_value=_vm(total=1024, available=512),
            ),
            patch(f"{_MOD}.shutil.which", return_value="/usr/bin/nvidia-smi"),
            patch(f"{_MOD}.subprocess.run", **run_kwargs),
        ):
            result = await LocalHostResources().probe()

        assert result.gpus == (), f"expected no gpus for {run_kwargs!r}"
        assert not result.has_gpu


@pytest.mark.asyncio
async def test_probe_gpu_with_unparseable_vram_reports_unknown() -> None:
    """A GPU row whose VRAM value can't be parsed → total_vram_bytes is None."""
    rows = "NVIDIA A100, [N/A]\nNVIDIA T4, 16384\n"
    with (
        patch(
            f"{_MOD}.psutil.virtual_memory",
            return_value=_vm(total=1024, available=512),
        ),
        patch(f"{_MOD}.shutil.which", return_value="/usr/bin/nvidia-smi"),
        patch(
            f"{_MOD}.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout=rows, stderr=""),
        ),
    ):
        result = await LocalHostResources().probe()

    assert len(result.gpus) == 2
    assert result.gpus[0].name == "NVIDIA A100"
    assert result.gpus[0].total_vram_bytes is None
    assert result.gpus[1].name == "NVIDIA T4"
    assert result.gpus[1].total_vram_bytes == 16384 * _MIB


# ── HostResourcesService ──────────────────────────────────────────


class _StubBackend(HostResourcesBackend):
    """Stub backend that returns a fixed HostResources without probing."""

    _SENTINEL = HostResources(
        total_ram_bytes=42,
        available_ram_bytes=21,
        gpus=(GPUInfo(name="stub", total_vram_bytes=7),),
    )

    async def probe(self) -> HostResources:
        return self._SENTINEL


class _StubResolver(ServiceResolver):
    def get_capability(self, capability: str) -> Service | None:
        return None

    def require_capability(self, capability: str) -> Service:
        raise LookupError(capability)

    def get_all(self, capability: str) -> list[Service]:
        return []


def test_service_advertises_host_resources_capability() -> None:
    info = HostResourcesService().service_info()
    assert info.name == "host_resources"
    assert "host_resources" in info.capabilities


@pytest.mark.asyncio
async def test_service_implements_provider_protocol_and_delegates() -> None:
    svc = HostResourcesService()
    # Force the service to resolve our stub backend instead of "local".
    with patch.object(
        HostResourcesBackend,
        "registered_backends",
        classmethod(lambda cls: {"local": _StubBackend}),
    ):
        await svc.start(_StubResolver())

    assert isinstance(svc, HostResourcesProvider)
    result = await svc.get_host_resources()
    assert result is _StubBackend._SENTINEL
    assert result.total_ram_bytes == 42
