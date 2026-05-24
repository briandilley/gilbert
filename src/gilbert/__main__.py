"""Gilbert entrypoint — boots the application and runs the web server."""

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn
from fastapi import FastAPI

from gilbert.config import DATA_DIR
from gilbert.core.app import Gilbert
from gilbert.web import create_app

if TYPE_CHECKING:
    from gilbert.core.tls import CertInfo

logger = logging.getLogger(__name__)

PID_FILE = DATA_DIR / "gilbert.pid"

# Exit code used to signal "please restart me so ``uv sync`` can install
# deps a runtime-installed plugin brought with it." Picked to match
# ``EX_TEMPFAIL`` from ``sysexits.h`` — semantically "temporary failure,
# try again" — and to avoid colliding with 0 (clean), 1 (generic
# failure), 130 (SIGINT), or 143 (SIGTERM). ``gilbert.sh`` catches this
# exit code in its supervisor loop and re-runs ``uv sync`` before
# relaunching Gilbert.
RESTART_EXIT_CODE = 75

# Track signal count for force-exit
_signal_count = 0


def _build_http_server(web_app: FastAPI, gilbert: Gilbert) -> uvicorn.Server:
    cfg = uvicorn.Config(
        web_app,
        host=gilbert.config.web.host,
        port=gilbert.config.web.port,
        log_level="info",
        timeout_graceful_shutdown=10,
    )
    return uvicorn.Server(cfg)


def _build_https_server(
    web_app: FastAPI, gilbert: Gilbert, cert_info: "CertInfo"
) -> uvicorn.Server:
    cfg = uvicorn.Config(
        web_app,
        host=gilbert.config.web.host,
        port=gilbert.config.web.tls.https_port,
        log_level="info",
        timeout_graceful_shutdown=10,
        ssl_certfile=str(cert_info.cert_path),
        ssl_keyfile=str(cert_info.key_path),
    )
    return uvicorn.Server(cfg)


def _write_pid() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))


def _remove_pid() -> None:
    try:
        PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


async def main() -> None:
    global _signal_count

    gilbert = Gilbert.create()

    await gilbert.start()
    _write_pid()

    web_app: FastAPI = create_app(gilbert)

    servers: list[uvicorn.Server] = [_build_http_server(web_app, gilbert)]

    if gilbert.config.web.tls.enabled:
        try:
            from gilbert.core.tls import ensure_self_signed_cert

            cert_info = ensure_self_signed_cert(
                Path(gilbert.config.web.tls.cert_path),
                Path(gilbert.config.web.tls.key_path),
            )
            web_app.state.tls_info = cert_info
            servers.append(_build_https_server(web_app, gilbert, cert_info))
            logger.info(
                "HTTPS on https://%s:%d (cert valid until %s, SAN: %s)",
                gilbert.config.web.host,
                gilbert.config.web.tls.https_port,
                cert_info.not_valid_after.date(),
                ", ".join(cert_info.san_entries),
            )
        except Exception:
            logger.exception("Failed to set up TLS — running HTTP-only")

    # Disable uvicorn's own signal handling on every server; we manage it.
    # ``install_signal_handlers`` is a private-ish uvicorn API that
    # exists on ``Server`` at runtime but isn't in the type stubs.
    for s in servers:
        s.install_signal_handlers = lambda: None  # type: ignore[attr-defined]

    # Wire the shutdown hook so ``Gilbert.request_restart()`` can
    # actually stop the servers. Setting ``should_exit`` is the same
    # lever the signal handler uses below — this lets services request
    # a clean exit through the normal uvicorn path.
    def _shutdown() -> None:
        for s in servers:
            s.should_exit = True

    gilbert.set_shutdown_callback(_shutdown)

    def _handle_signal(signum: int, frame: object) -> None:
        global _signal_count
        _signal_count += 1
        if _signal_count >= 2:
            logger.warning("Forced shutdown (signal %d)", _signal_count)
            _remove_pid()
            os._exit(1)
        logger.info("Shutdown signal received — press Ctrl+C again to force quit")
        for s in servers:
            s.should_exit = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        await asyncio.gather(*(s.serve() for s in servers))
    finally:
        await gilbert.stop()
        _remove_pid()

    # If a service asked us to restart (via ``Gilbert.request_restart()``),
    # exit with the sentinel code so ``gilbert.sh``'s supervisor loop
    # re-runs ``uv sync`` and relaunches us. Raised as ``SystemExit`` so
    # it propagates out of ``asyncio.run``.
    if gilbert.restart_requested:
        logger.info("Exiting with code %d to trigger supervised restart", RESTART_EXIT_CODE)
        raise SystemExit(RESTART_EXIT_CODE)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit as exc:
        # Preserve the specific exit code (notably ``RESTART_EXIT_CODE``)
        # so the ``gilbert.sh`` supervisor sees it instead of whatever
        # asyncio's default cleanup would propagate.
        sys.exit(exc.code)
