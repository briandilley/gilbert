"""Tiny CORS-correct MCP proxy for Gilbert's browser bridge.

Use this when you want to expose a local MCP server to Gilbert's
``MCP → Local`` page during development. Most popular CORS proxies
(``local-cors-proxy`` in particular) handle basic preflights fine
but don't expose the ``Mcp-Session-Id`` response header to browser
JavaScript — without that header visible, the MCP streamable-HTTP
handshake falls apart: every request becomes a new un-initialized
session, the server hangs, and Gilbert's browser bridge times out.

This proxy forwards any POST to a target MCP URL, passes through
every header the MCP spec cares about in both directions, and sets
``Access-Control-Expose-Headers: Mcp-Session-Id`` so the browser can
actually read the session id.

It's a dev tool, not a production thing: ``allow_origins=["*"]`` and
no authentication. Run it on your own machine next to the MCP
server you want to bridge, point Gilbert at the proxy URL, and tear
it down when you're done.

Usage:
    uv run --with httpx --with starlette --with uvicorn \\
        python scripts/mcp_cors_proxy.py \\
        --target http://localhost:6010/mcp \\
        --port 6011

Then add a local entry in Gilbert's ``MCP → Local`` page pointing at
``http://localhost:6011/mcp``.
"""

from __future__ import annotations

import argparse
import sys

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

# Headers we must not forward upstream or downstream — they confuse
# httpx or invalidate the response (content-length becomes wrong after
# a content encoding change, etc).
HOP_BY_HOP = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
})

TARGET_URL: str = ""


async def proxy(request: Request) -> Response:
    body = await request.body()
    fwd_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP
    }
    async with httpx.AsyncClient(timeout=120) as client:
        upstream = await client.post(
            TARGET_URL, content=body, headers=fwd_headers,
        )
    back_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in HOP_BY_HOP
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=back_headers,
        media_type=upstream.headers.get("content-type"),
    )


def build_app(target_url: str) -> Starlette:
    global TARGET_URL
    TARGET_URL = target_url
    return Starlette(
        routes=[Route("/mcp", proxy, methods=["POST"])],
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_methods=["POST", "OPTIONS"],
                # Allow every header the browser bridge sends. `*` is
                # easier than enumerating, but the spec doesn't allow
                # `*` together with credentials — we use
                # `credentials: "omit"` on the browser side so this is
                # fine.
                allow_headers=["*"],
                # Critical: expose MCP session header so browser JS
                # can read it. Without this the handshake silently
                # fails.
                expose_headers=["mcp-session-id", "Mcp-Session-Id"],
                max_age=86400,
            ),
        ],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--target", required=True,
        help="Upstream MCP URL, e.g. http://localhost:6010/mcp",
    )
    parser.add_argument(
        "--port", type=int, default=6011,
        help="Port to listen on (default 6011)",
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="Host to bind to (default 127.0.0.1)",
    )
    args = parser.parse_args()
    app = build_app(args.target)
    print(
        f"[mcp_cors_proxy] forwarding POST http://{args.host}:{args.port}/mcp "
        f"→ {args.target}",
        file=sys.stderr,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
