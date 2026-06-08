"""Vendor-free local host-resources probe.

Reports total/available RAM via ``psutil`` and best-effort GPU presence +
per-GPU VRAM via the system ``nvidia-smi`` binary. Dependency-light: only
``psutil`` plus the stdlib (``subprocess`` / ``shutil``). The probe is
localhost-only and best-effort — on any GPU-detection failure it reports
no GPU rather than raising or fabricating a number (ADR-0020).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess

import psutil

from gilbert.interfaces.host_resources import (
    GPUInfo,
    HostResources,
    HostResourcesBackend,
)

logger = logging.getLogger(__name__)

# nvidia-smi emits VRAM in MiB with ``--units`` stripped via ``nounits``.
_MIB = 1024 * 1024

# Generous cap so a wedged driver can't stall the probe (and the caller's
# event loop, though we run in a thread) indefinitely.
_NVIDIA_SMI_TIMEOUT_S = 3.0


class LocalHostResources(HostResourcesBackend):
    """Localhost RAM + best-effort NVIDIA GPU probe."""

    backend_name = "local"

    async def probe(self) -> HostResources:
        """Probe the host. Blocking work runs in a worker thread."""
        return await asyncio.to_thread(self._probe_blocking)

    def _probe_blocking(self) -> HostResources:
        vm = psutil.virtual_memory()
        return HostResources(
            total_ram_bytes=int(vm.total),
            available_ram_bytes=int(vm.available),
            gpus=self._probe_gpus(),
        )

    def _probe_gpus(self) -> tuple[GPUInfo, ...]:
        """Best-effort NVIDIA GPU detection. Never raises.

        Returns ``()`` when nvidia-smi is absent, errors, or times out —
        meaning "no GPU detected," not "error." A detected GPU whose VRAM
        can't be parsed gets ``total_vram_bytes=None`` ("unknown").
        """
        if shutil.which("nvidia-smi") is None:
            return ()

        try:
            proc = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=name,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=_NVIDIA_SMI_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("nvidia-smi probe failed: %s", exc)
            return ()

        if proc.returncode != 0:
            logger.debug("nvidia-smi exited %s: %s", proc.returncode, proc.stderr.strip())
            return ()

        gpus: list[GPUInfo] = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            # Rows look like ``NVIDIA GeForce RTX 3090, 24576``.
            name, _, vram_raw = line.partition(",")
            name = name.strip()
            if not name:
                continue
            gpus.append(GPUInfo(name=name, total_vram_bytes=_parse_vram_mib(vram_raw)))
        return tuple(gpus)


def _parse_vram_mib(raw: str) -> int | None:
    """Parse an nvidia-smi MiB VRAM value into bytes, or None if unparseable.

    nvidia-smi emits ``[N/A]`` / ``[Not Supported]`` for cards that don't
    report memory; those (and any non-numeric value) become "unknown".
    """
    value = raw.strip()
    try:
        return int(value) * _MIB
    except ValueError:
        return None
