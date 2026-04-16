"""Gilbert web server — FastAPI app factory."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from gilbert.core.app import Gilbert

_HERE = Path(__file__).parent
_SPA_DIR = _HERE / "spa"


def create_app(gilbert: Gilbert) -> FastAPI:
    """Create the FastAPI application wired to a running Gilbert instance."""
    app = FastAPI(title="Gilbert", docs_url=None, redoc_url=None)

    # Store gilbert instance for route access
    app.state.gilbert = gilbert

    # WebSocket connection manager — single bus subscription, dispatches to all clients
    from gilbert.web.ws_protocol import WsConnectionManager

    ws_manager = WsConnectionManager()
    app.state.ws_manager = ws_manager

    @app.on_event("startup")
    async def _start_ws_manager() -> None:
        ws_manager.subscribe_to_bus(gilbert)

    @app.on_event("shutdown")
    async def _stop_ws_manager() -> None:
        ws_manager.shutdown()

    @app.on_event("shutdown")
    async def _stop_mcp_server_http() -> None:
        """Tear down the lazily-constructed MCP HTTP app if it was
        ever built. Without this its internal
        ``StreamableHTTPSessionManager.run`` async generator gets
        GC'd from a non-owning task on process exit, tripping
        anyio's cross-task cancel-scope check and printing a noisy
        ``BaseExceptionGroup`` to stderr."""
        http_app = getattr(app.state, "mcp_http_app", None)
        if http_app is not None:
            try:
                await http_app.stop()
            except Exception:
                pass

    # Auth middleware (works even when auth is disabled — falls through to SYSTEM)
    from gilbert.web.auth import AuthMiddleware

    app.add_middleware(AuthMiddleware)

    # Serve generated output files (TTS audio, etc.) so speakers can fetch them
    from gilbert.core.output import OUTPUT_DIR

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/output", StaticFiles(directory=str(OUTPUT_DIR)), name="output")

    # Routes
    from gilbert.web.routes.auth import router as auth_router
    from gilbert.web.routes.chat import router as chat_router
    from gilbert.web.routes.chat_uploads import router as chat_uploads_router
    from gilbert.web.routes.documents import router as documents_router
    from gilbert.web.routes.inbox import router as inbox_router
    from gilbert.web.routes.mcp import mcp_asgi_endpoint
    from gilbert.web.routes.screens import router as screens_router
    from gilbert.web.routes.websocket import router as ws_router

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(auth_router)
    app.include_router(chat_router)
    app.include_router(chat_uploads_router)
    app.include_router(documents_router)
    app.include_router(inbox_router)
    app.include_router(screens_router)
    app.include_router(ws_router)

    # MCP server endpoint — raw ASGI, not a FastAPI route (see
    # ``routes/mcp.py`` for why). Mounted at ``/api/mcp`` so it
    # doesn't collide with the SPA route at ``/mcp/*``; external
    # MCP clients configure this URL as their server endpoint.
    # Registered via the underlying starlette router so FastAPI's
    # response wrapping doesn't double-send after the streaming
    # session manager finishes.
    from starlette.routing import Route as _StarletteRoute

    app.router.routes.append(
        _StarletteRoute(
            "/api/mcp",
            endpoint=mcp_asgi_endpoint,
            methods=["GET", "POST", "DELETE", "OPTIONS"],
            include_in_schema=False,
        ),
    )

    # --- API routes (JSON only, for the SPA) ---
    from gilbert.web.routes.api import router as api_router

    app.include_router(api_router)

    # --- SPA serving ---
    if _SPA_DIR.exists():
        app.mount(
            "/assets",
            StaticFiles(directory=str(_SPA_DIR / "assets")),
            name="spa_assets",
        )

        @app.get("/{full_path:path}")
        async def spa_fallback(request: Request, full_path: str) -> Response:
            """Serve the SPA index.html for all unmatched routes."""
            index = _SPA_DIR / "index.html"
            if index.exists():
                return FileResponse(str(index), media_type="text/html")
            return Response(status_code=404)

    return app
