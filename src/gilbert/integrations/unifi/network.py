"""UniFi Network integration — WiFi client tracking and device-to-person mapping."""

import logging
import re
from dataclasses import dataclass
from typing import Any

from gilbert.integrations.unifi.client import UniFiClient

logger = logging.getLogger(__name__)

# Device-only names that should not be treated as person names
_DEVICE_ONLY_NAMES = frozenset({
    "iphone", "ipad", "macbook", "android", "pixel", "galaxy", "samsung",
    "oneplus", "huawei", "xiaomi", "oppo", "motorola", "lg", "nokia",
    "chromebook", "surface", "thinkpad", "dell", "hp", "lenovo",
    "echo", "alexa", "google-home", "nest", "sonos", "roku", "firestick",
    "unknown", "generic", "wireless", "mobile", "phone", "tablet", "laptop",
})

# Pattern for possessive device names like "Brian's iPhone" or "Brian Dilley's Phone"
_POSSESSIVE_RE = re.compile(r"^(.+?)(?:'s?\s)", re.IGNORECASE)

# Pattern for "FirstLast-Device" hostnames like "BrianDilley-iPhone"
_CAMEL_SPLIT_RE = re.compile(r"([A-Z][a-z]+)")


# UniFi dev_family values that correspond to phones/mobile devices.
# These are used to filter network presence to only phones.
_PHONE_DEV_FAMILIES: frozenset[int] = frozenset({
    9,   # iPhone
    14,  # Samsung phone
    77,  # Android phone
})


@dataclass(frozen=True)
class WifiClient:
    """A connected WiFi client with optional person attribution."""

    mac: str
    hostname: str
    device_name: str
    person: str
    rssi: int
    ap_name: str
    last_seen: str  # ISO 8601 timestamp or epoch string
    is_wired: bool
    is_phone: bool = False


class UniFiNetwork:
    """Queries the UniFi Network controller for connected WiFi clients."""

    def __init__(
        self,
        client: UniFiClient,
        device_person_map: dict[str, str] | None = None,
    ) -> None:
        self._client = client
        self._device_person_map: dict[str, str] = {
            k.lower(): v for k, v in (device_person_map or {}).items()
        }

    async def get_connected_clients(self) -> list[WifiClient]:
        """Get all connected clients with person attribution where possible."""
        data = await self._client.get("/proxy/network/api/s/default/stat/sta")
        if data is None:
            return []

        clients_raw: list[dict[str, Any]] = data.get("data", [])
        results: list[WifiClient] = []

        for c in clients_raw:
            mac = c.get("mac", "").lower()
            hostname = c.get("hostname", "")
            device_name = c.get("name", "")
            is_wired = c.get("is_wired", False)
            rssi = c.get("rssi", 0)
            ap_name = c.get("ap_name", "")
            last_seen = str(c.get("last_seen", ""))
            dev_family = c.get("dev_family", 0)

            person = self._resolve_person(mac, device_name, hostname)

            results.append(WifiClient(
                mac=mac,
                hostname=hostname,
                device_name=device_name,
                person=person,
                rssi=rssi,
                ap_name=ap_name,
                last_seen=last_seen,
                is_wired=is_wired,
                is_phone=dev_family in _PHONE_DEV_FAMILIES,
            ))

        matched = [r for r in results if r.person and not r.is_wired]
        logger.debug(
            "WiFi scan: %d clients, %d wireless, %d matched to people",
            len(results),
            sum(1 for r in results if not r.is_wired),
            len(matched),
        )
        return results

    async def get_people_on_network(self) -> dict[str, list[WifiClient]]:
        """Get wireless phone clients grouped by person name.

        Only phones (not laptops, desktops, IoT, etc.) count toward
        human presence — a laptop on the network doesn't mean the
        person is physically present.
        """
        clients = await self.get_connected_clients()
        people: dict[str, list[WifiClient]] = {}
        for c in clients:
            if c.person and not c.is_wired and c.is_phone:
                people.setdefault(c.person, []).append(c)
        return people

    def _resolve_person(self, mac: str, device_name: str, hostname: str) -> str:
        """Resolve a person name from MAC, device name, or hostname."""
        # Priority 1: explicit MAC mapping
        person = self._device_person_map.get(mac, "")
        if person:
            return person

        # Priority 2: extract from device name
        if device_name:
            person = extract_person_from_device_name(device_name)
            if person:
                return person

        # Priority 3: extract from hostname
        if hostname:
            person = extract_person_from_hostname(hostname)
            if person:
                return person

        return ""


def extract_person_from_device_name(name: str) -> str:
    """Extract a person's name from a UniFi device name.

    Examples:
        "Brian Dilley's Phone" → "Brian"
        "Brian's iPhone" → "Brian"
        "Chris Pixel 8" → "Chris"
    """
    # Try possessive pattern first: "Someone's Device"
    m = _POSSESSIVE_RE.match(name)
    if m:
        candidate = m.group(1).split()[0]  # First word of the possessive owner
        if _is_valid_person_name(candidate):
            return candidate

    # Try "FirstName DeviceType" pattern (e.g., "Matt iPhone 15")
    parts = name.split()
    if len(parts) >= 2:
        candidate = parts[0]
        if _is_valid_person_name(candidate):
            # Accept if any remaining word looks like a device type
            if any(d in name.lower() for d in ("phone", "iphone", "pixel", "galaxy", "ipad",
                                                 "macbook", "laptop", "watch", "tablet")):
                return candidate

    return ""


def extract_person_from_hostname(hostname: str) -> str:
    """Extract a person's name from a device hostname.

    Examples:
        "Greggs-iPhone" → "Gregg"
        "BrianDilley-iPhone" → "Brian"
        "Gregg-Snowdens-iPhone" → "Gregg"
    """
    # Split on hyphens
    parts = hostname.split("-")
    if not parts:
        return ""

    candidate = parts[0]

    # Handle CamelCase: "BrianDilley" → ["Brian", "Dilley"]
    camel_parts = _CAMEL_SPLIT_RE.findall(candidate)
    if len(camel_parts) >= 2:
        candidate = camel_parts[0]

    # Strip possessive 's' or 's
    if candidate.lower().endswith("s") and len(candidate) > 3:
        # Only strip if it looks possessive (e.g., "Greggs" → "Gregg")
        bare = candidate[:-1]
        if bare[0].isupper():
            candidate = bare

    if _is_valid_person_name(candidate):
        return candidate

    return ""


def _is_valid_person_name(name: str) -> bool:
    """Check if a string looks like a person's first name."""
    if len(name) < 2:
        return False
    if not name[0].isupper():
        return False
    if name.lower() in _DEVICE_ONLY_NAMES:
        return False
    # Must be alphabetic
    if not name.isalpha():
        return False
    return True
