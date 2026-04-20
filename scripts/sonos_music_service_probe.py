#!/usr/bin/env python3
"""Probe a Sonos speaker for music-service account info.

Targets the "Failed to enqueue track" error from loadContent. The
hypothesis is that the local WebSocket loadContent API wants an
``accountId`` (the ``sn_X`` serial of the linked Spotify account)
that our request is currently omitting. aiosonos doesn't expose any
way to discover this, so we hit Sonos's legacy HTTP endpoints on
port 1400 — these are the same endpoints SoCo uses, and they dump
the household's linked music-service accounts as plain XML.

What gets probed:

1. ``http://<ip>:1400/status/accounts`` — lists every linked service
   account with its Type (service id), SerialNum (``sn_X``),
   Nickname, UN/encrypted OAuth token fields, etc. The SerialNum of
   the Spotify (Type=2311 historically, or 3079 for S2 Spotify) row
   is what ``loadContent.id.accountId`` expects.

2. ``http://<ip>:1400/status/zp`` — a fat status dump that includes
   HouseholdId, ZoneName, software version, services list, and
   ``<MusicSurroundAppts>`` — useful sanity info.

3. S2 WebSocket ``send_command`` calls against namespaces aiosonos
   doesn't wrap, to see what else the speaker exposes:
   - ``households:1`` / ``getHouseholdConfig``
   - ``playbackMetadata:1`` / ``getMetadataStatus`` (wrapped — used
     here just to confirm connectivity)

Usage:
    uv run python scripts/sonos_music_service_probe.py [ip]

With no argument, probes the first speaker found via zeroconf.
"""

from __future__ import annotations

import asyncio
import ssl
import sys
from xml.etree import ElementTree as ET

import aiohttp
import httpx
from aiosonos import SonosLocalApiClient
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

_LOCAL_API_KEY = "123e4567-e89b-12d3-a456-426655440000"
_SONOS_SERVICE_TYPE = "_sonos._tcp.local."
_DISCOVERY_SETTLE = 3.0


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def _discover_first() -> str | None:
    """Return the IP of the first Sonos found via zeroconf, or None."""
    found: list[str] = []

    def on_change(
        zc: object, service_type: str, name: str, state_change: ServiceStateChange
    ) -> None:
        if state_change not in (
            ServiceStateChange.Added,
            ServiceStateChange.Updated,
        ):
            return
        asyncio.create_task(_resolve(zc, service_type, name))

    async def _resolve(zc: object, service_type: str, name: str) -> None:
        info = AsyncServiceInfo(service_type, name)
        try:
            ok = await info.async_request(zc, 2500)
        except Exception:
            return
        if not ok or not info.addresses:
            return
        ip = ".".join(str(b) for b in info.addresses[0])
        if ip not in found:
            found.append(ip)

    zc = AsyncZeroconf()
    browser = AsyncServiceBrowser(
        zc.zeroconf, _SONOS_SERVICE_TYPE, handlers=[on_change]
    )
    await asyncio.sleep(_DISCOVERY_SETTLE)
    await browser.async_cancel()
    await zc.async_close()

    return found[0] if found else None


async def _fetch_text(url: str) -> tuple[int, str]:
    """HTTP GET returning (status, body). Sonos legacy port 1400 is HTTP."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
        return resp.status_code, resp.text


def _pretty_xml(body: str) -> str:
    """Reindent XML for readable printing; fall back to raw on parse error."""
    try:
        tree = ET.fromstring(body)
        ET.indent(tree, space="  ")
        return ET.tostring(tree, encoding="unicode")
    except ET.ParseError:
        return body


def _summarize_accounts(body: str) -> list[dict[str, str]]:
    """Pull each <Account> element into a dict so we can spotlight Spotify.

    Structure (observed):
        <ZPSupportInfo>
          <Accounts LastUpdateDevice="…">
            <Account Type="2311" SerialNum="4" ...>
              <UN>user@example.com</UN>
              <MD>1</MD>
              <NN>Nickname</NN>
              ...
            </Account>
          </Accounts>
        </ZPSupportInfo>

    ``Type`` is the Sonos music-service numeric id; Spotify has
    historically used 2311, but S2 firmware sometimes reports 3079 for
    Spotify containers. ``SerialNum`` is the ``sn_X`` account id used
    by ``loadContent``.
    """
    try:
        tree = ET.fromstring(body)
    except ET.ParseError:
        return []
    out: list[dict[str, str]] = []
    for account in tree.iter("Account"):
        entry = dict(account.attrib)
        for child in account:
            entry[child.tag] = (child.text or "").strip()
        out.append(entry)
    return out


async def _try_ws_commands(ip: str) -> None:
    """Open an aiosonos WS and dump a handful of introspection commands."""
    print("=" * 72)
    print(f"WebSocket introspection (aiosonos via {ip})")
    print("=" * 72)
    try:
        async with aiohttp.ClientSession() as session:
            client = SonosLocalApiClient(ip, session)
            await client.connect()
            api = client.api
            print(
                f"connected: household_id={client.household_id} "
                f"player_id={client.player_id}"
            )

            probes: list[tuple[str, str, dict]] = [
                # (namespace, command, kwargs) — wrapped in try/except
                # each so one failure doesn't abort the rest. Commands
                # use the ``namespace:version`` convention aiosonos
                # applies internally via send_command. We pass without
                # the version and let send_command append ``:1``.
                ("households", "getHouseholdConfig", {"householdId": client.household_id}),
                ("households", "getMusicServices", {"householdId": client.household_id}),
                ("households", "getAccounts", {"householdId": client.household_id}),
                ("musicService", "list", {}),
                ("musicService", "listAccounts", {}),
                ("accounts", "list", {}),
                ("playerSettings", "getAll", {"playerId": client.player_id}),
            ]

            for namespace, command, kwargs in probes:
                print(f"\n--- {namespace}:1 / {command} ---")
                try:
                    result = await api.send_command(
                        namespace=namespace, command=command, **kwargs
                    )
                    print(repr(result)[:2000])
                except Exception as exc:  # noqa: BLE001
                    print(f"FAILED: {type(exc).__name__}: {exc}")

            await client.disconnect()
    except Exception as exc:  # noqa: BLE001
        print(f"WS connect failed: {type(exc).__name__}: {exc}")


async def main() -> int:
    if len(sys.argv) > 1:
        ip = sys.argv[1]
    else:
        print("No IP given — discovering via zeroconf…", flush=True)
        ip = await _discover_first()
        if not ip:
            print("No Sonos speakers found on the LAN.")
            return 1
        print(f"  using {ip}")

    print()
    print("=" * 72)
    print(f"HTTP status endpoints on http://{ip}:1400")
    print("=" * 72)

    # /status/accounts is the historical source of truth but newer S2
    # firmware (94.x+) returns empty. Fall back to the other status
    # and XML description endpoints in the hope that one of them still
    # lists the service registrations.
    for path in (
        "/status/accounts",
        "/status/zp",
        "/status/topology",
        "/xml/device_description.xml",
        "/xml/musicservices.xml",
        "/xml/services.xml",
    ):
        url = f"http://{ip}:1400{path}"
        print(f"\n--- GET {url} ---")
        try:
            status, body = await _fetch_text(url)
        except Exception as exc:  # noqa: BLE001
            print(f"FAILED: {type(exc).__name__}: {exc}")
            continue
        if status != 200:
            print(f"HTTP {status}")
            print(body[:500])
            continue
        print(_pretty_xml(body))
        if path == "/status/accounts":
            accounts = _summarize_accounts(body)
            print("\n>>> account summary:")
            if not accounts:
                print("    (none parsed)")
            for acct in accounts:
                type_id = acct.get("Type", "?")
                serial = acct.get("SerialNum", "?")
                nn = acct.get("NN", "")
                un = acct.get("UN", "")
                md = acct.get("MD", "")
                print(
                    f"    Type={type_id}  SerialNum(sn_{serial})  "
                    f"NN={nn!r}  UN={un!r}  MD={md!r}"
                )
            print(
                "\n>>> for loadContent.id.accountId, use the SerialNum of "
                "the Spotify row, prefixed with 'sn_' (e.g. SerialNum=3 → "
                "accountId='sn_3')."
            )

    await _probe_soap_music_services(ip)
    return 0


async def _probe_soap_music_services(ip: str) -> None:
    """Call MusicServices:1 SOAP to look up installed services + account serials.

    Two actions are interesting:

    - ``ListAvailableServices`` returns an XML blob listing every music
      service the speaker knows about, with ID, name, version, etc.
    - ``GetSessionId`` given a service ID returns session info that, in
      current firmware, appears to still be the only way to pull the
      linked account's serial number (``sn_X``) on S2. The cloud API
      doesn't expose it; ``/status/accounts`` returns empty.

    Both are on the classic UPnP SOAP endpoint at
    ``http://<ip>:1400/MusicServices/Control`` — port 1400, not 1443.
    Auth is open on-LAN, same as AVTransport.
    """
    print()
    print("=" * 72)
    print(f"SOAP MusicServices:1 on http://{ip}:1400/MusicServices/Control")
    print("=" * 72)

    control_url = f"http://{ip}:1400/MusicServices/Control"

    def soap_envelope(action: str, body_xml: str) -> str:
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            "<s:Body>"
            f'<u:{action} xmlns:u="urn:schemas-upnp-org:service:MusicServices:1">'
            f"{body_xml}"
            f"</u:{action}>"
            "</s:Body>"
            "</s:Envelope>"
        )

    async def soap_call(action: str, body_xml: str) -> tuple[int, str]:
        envelope = soap_envelope(action, body_xml)
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"urn:schemas-upnp-org:service:MusicServices:1#{action}"',
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(control_url, content=envelope, headers=headers)
            return resp.status_code, resp.text

    # 1) List available services — tells us the numeric id for Spotify
    print("\n--- ListAvailableServices ---")
    try:
        status, body = await soap_call("ListAvailableServices", "")
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {type(exc).__name__}: {exc}")
        body = ""
        status = 0
    else:
        print(f"HTTP {status}")
        # The response wraps the real payload as escaped XML inside an
        # <AvailableServiceDescriptorList> text node. Pull it out and
        # scan for the Spotify row.
        print(body[:2000] + ("…" if len(body) > 2000 else ""))
        if body:
            try:
                root = ET.fromstring(body)
                # Namespace-agnostic scan for the descriptor field.
                for elem in root.iter():
                    if elem.tag.endswith("AvailableServiceDescriptorList"):
                        inner = (elem.text or "").strip()
                        if inner:
                            print("\n>>> AvailableServiceDescriptorList (unescaped):")
                            try:
                                inner_tree = ET.fromstring(inner)
                                ET.indent(inner_tree, space="  ")
                                print(ET.tostring(inner_tree, encoding="unicode")[:3000])
                            except ET.ParseError:
                                print(inner[:3000])
                        break
            except ET.ParseError:
                pass

    # 2) Try GetSessionId for Spotify (service ID 9). If this returns a
    # SessionId we can probably extract the account serial from it or
    # pair it with a known-good SMAPI URI. If it 500s we know the
    # speaker doesn't have Spotify linked via SMAPI (which would fully
    # explain why loadContent can't enqueue the track either — the
    # mobile-app Spotify Connect binding is a separate path).
    print("\n--- GetSessionId (ServiceId=9 = Spotify) ---")
    try:
        status, body = await soap_call(
            "GetSessionId", "<ServiceId>9</ServiceId><Username></Username>"
        )
        print(f"HTTP {status}")
        print(body[:2000])
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {type(exc).__name__}: {exc}")

    # 3) Also try the undocumented ``ListAvailableServices`` variant that
    # some S2 firmwares expose with an additional ``AccountId`` hint.
    # No-op on older firmware; informative on newer.
    print("\n--- DeviceProperties.GetHouseholdID (cross-reference) ---")
    dev_url = f"http://{ip}:1400/DeviceProperties/Control"
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        '<u:GetHouseholdID xmlns:u="urn:schemas-upnp-org:service:DeviceProperties:1"/>'
        "</s:Body>"
        "</s:Envelope>"
    )
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                dev_url,
                content=envelope,
                headers={
                    "Content-Type": 'text/xml; charset="utf-8"',
                    "SOAPAction": '"urn:schemas-upnp-org:service:DeviceProperties:1#GetHouseholdID"',
                },
            )
            print(f"HTTP {resp.status_code}")
            print(resp.text[:1000])
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
