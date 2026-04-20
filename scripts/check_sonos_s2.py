#!/usr/bin/env python3
"""Verify every Sonos speaker on the LAN supports the S2 local API.

aiosonos (Gilbert's Sonos backend) speaks the Sonos S2 local WebSocket
API on port 1443. S1-generation speakers (Play:1, Play:3, Play:5 gen 1,
Connect gen 1, ...) don't expose that endpoint — they only speak the
legacy UPnP/SOAP protocol the old soco-based backend used.

This script discovers every Sonos player on the LAN via zeroconf/mDNS
and probes the S2 info endpoint on each. Output summarises which
speakers are S2-ready.

Usage:
    uv run python scripts/check_sonos_s2.py

Exit code 0 if every discovered speaker is S2; 1 if any are not.
"""

from __future__ import annotations

import asyncio
import ssl
import sys
from dataclasses import dataclass

import httpx
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

# Well-known Sonos local-API key. Published; every third-party S2 client
# sends it verbatim — not a secret, not tied to any account. Gates the
# endpoint against casual abuse, nothing more.
_LOCAL_API_KEY = "123e4567-e89b-12d3-a456-426655440000"

_SONOS_SERVICE_TYPE = "_sonos._tcp.local."
_LOCAL_API_URL = "https://{ip}:1443/api/v1/players/local/info"
_DISCOVERY_SETTLE = 4.0
_PROBE_TIMEOUT = 5.0


@dataclass
class ProbeResult:
    name: str
    ip: str
    model: str
    uid: str
    is_s2: bool
    household_id: str = ""
    software_version: str = ""
    api_version: str = ""
    error: str = ""


async def probe(ip: str, name: str, model: str, uid: str) -> ProbeResult:
    """Hit the S2-only local API info endpoint and report what came back."""
    # Sonos speakers ship with self-signed certs — verifying them doesn't
    # add security on a LAN control plane and would just break every
    # connection. aiosonos itself doesn't verify.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    url = _LOCAL_API_URL.format(ip=ip)
    headers = {"X-Sonos-Api-Key": _LOCAL_API_KEY}

    try:
        async with httpx.AsyncClient(verify=ctx, timeout=_PROBE_TIMEOUT) as client:
            resp = await client.get(url, headers=headers)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return ProbeResult(
            name=name,
            ip=ip,
            model=model,
            uid=uid,
            is_s2=False,
            error="port 1443 unreachable (likely S1 or firewall)",
        )
    except httpx.ReadTimeout:
        return ProbeResult(
            name=name,
            ip=ip,
            model=model,
            uid=uid,
            is_s2=False,
            error="timed out reading response",
        )
    except Exception as exc:  # noqa: BLE001 — report any unexpected error verbatim
        return ProbeResult(
            name=name,
            ip=ip,
            model=model,
            uid=uid,
            is_s2=False,
            error=f"{type(exc).__name__}: {exc}",
        )

    if resp.status_code != 200:
        return ProbeResult(
            name=name,
            ip=ip,
            model=model,
            uid=uid,
            is_s2=False,
            error=f"HTTP {resp.status_code} from S2 info endpoint",
        )

    try:
        data = resp.json()
    except ValueError:
        return ProbeResult(
            name=name,
            ip=ip,
            model=model,
            uid=uid,
            is_s2=False,
            error="non-JSON response from S2 info endpoint",
        )

    device = data.get("device") or {}
    return ProbeResult(
        name=name or str(device.get("name", "") or "Unknown"),
        ip=ip,
        model=model or str(device.get("model", "") or ""),
        uid=uid or str(data.get("playerId", "") or ""),
        is_s2=True,
        household_id=str(data.get("householdId", "")),
        software_version=str(data.get("softwareVersion", "")),
        api_version=str(data.get("apiVersion", "")),
    )


async def discover_speakers() -> list[tuple[str, str, str, str]]:
    """Return ``[(name, ip, model, uid)]`` for every Sonos on the LAN.

    Uses zeroconf to watch for ``_sonos._tcp.local.`` advertisements.
    Most Sonos speakers respond within 1–2 seconds; we wait
    ``_DISCOVERY_SETTLE`` to give slower ones time.

    Returns empty-string fields for name/model/uid because the
    zeroconf record doesn't carry them — the caller's probe step
    fills them in from the S2 info endpoint. Stable order by IP so
    repeat runs diff cleanly.
    """
    found: dict[str, tuple[str, str, str, str]] = {}

    def on_change(
        zeroconf: object,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        # Zeroconf invokes handlers with keyword arguments — parameter
        # name is load-bearing.
        if state_change not in (
            ServiceStateChange.Added,
            ServiceStateChange.Updated,
        ):
            return
        asyncio.create_task(_resolve(zeroconf, service_type, name))

    async def _resolve(zeroconf: object, service_type: str, name: str) -> None:
        info = AsyncServiceInfo(service_type, name)
        try:
            ok = await info.async_request(zeroconf, 2500)
        except Exception:
            return
        if not ok or not info.addresses:
            return
        # First IPv4 address from the packed-bytes list.
        ip = ".".join(str(b) for b in info.addresses[0])
        if ip not in found:
            # Name/model/uid populated later by the probe step.
            found[ip] = ("", ip, "", "")

    zc = AsyncZeroconf()
    browser = AsyncServiceBrowser(
        zc.zeroconf, _SONOS_SERVICE_TYPE, handlers=[on_change]
    )
    await asyncio.sleep(_DISCOVERY_SETTLE)
    await browser.async_cancel()
    await zc.async_close()

    return sorted(found.values(), key=lambda r: tuple(int(x) for x in r[1].split(".")))


async def main() -> int:
    print("Discovering Sonos speakers on the LAN via zeroconf…", flush=True)
    speakers = await discover_speakers()

    if not speakers:
        print("  No speakers found. Check that you're on the same LAN as the speakers")
        print("  and that multicast/mDNS isn't blocked.")
        return 1

    print(
        f"  Found {len(speakers)} speaker(s). Probing the S2 local API on each…\n"
    )

    probes = await asyncio.gather(
        *(probe(ip, name, model, uid) for name, ip, model, uid in speakers)
    )

    # Fixed-width columns so the output is scannable.
    name_w = max(len("NAME"), max(len(p.name) for p in probes))
    ip_w = max(len("IP"), max(len(p.ip) for p in probes))
    model_w = max(len("MODEL"), max(len(p.model) for p in probes))

    print(
        f"  {'NAME':<{name_w}}  {'IP':<{ip_w}}  {'MODEL':<{model_w}}  S2   VERSION"
    )
    print("  " + "-" * (name_w + ip_w + model_w + 20))
    for p in probes:
        tag = "✓" if p.is_s2 else "✗"
        version = p.software_version or p.error
        print(
            f"  {p.name:<{name_w}}  {p.ip:<{ip_w}}  {p.model:<{model_w}}  {tag}    {version}"
        )

    s2_count = sum(1 for p in probes if p.is_s2)
    s1_count = len(probes) - s2_count

    print()
    if s1_count == 0:
        print(
            f"All {s2_count} speaker(s) expose the S2 local API — "
            "safe to use the aiosonos-based backend."
        )
        return 0

    print(
        f"{s2_count} of {len(probes)} speaker(s) are S2-ready; "
        f"{s1_count} are not:"
    )
    for p in probes:
        if not p.is_s2:
            print(f"  - {p.name} ({p.model} @ {p.ip}): {p.error}")
    print()
    print(
        "Those speakers won't work under the aiosonos backend. Replace "
        "the hardware to regain support — Gilbert no longer ships soco."
    )
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
