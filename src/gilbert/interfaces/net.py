"""Network utilities shared across layers.

Vendor-free, stdlib only — safe to import from ``interfaces/``,
``core/``, and ``integrations/`` alike (e.g. the self-signed TLS cert
generator and the sslip.io internal-URL backend both need to know the
host's outbound LAN IP).
"""

from __future__ import annotations

import socket


def detect_outbound_ip() -> str | None:
    """Return the primary outbound IPv4 the OS would use to reach an
    external destination, without actually sending any traffic.

    Returns ``None`` when it can't be determined (e.g. no network).
    On multi-homed hosts this picks the interface with the default
    route — callers that need a specific NIC should accept an override.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 53))
        return str(sock.getsockname()[0])
    except OSError:
        return None
    finally:
        sock.close()
