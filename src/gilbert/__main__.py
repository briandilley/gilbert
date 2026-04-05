"""Gilbert entrypoint — boots the application and runs the web server."""

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import uvicorn

from gilbert.config import DATA_DIR, load_config
from gilbert.core.app import Gilbert
from gilbert.web import create_app

logger = logging.getLogger(__name__)

PID_FILE = DATA_DIR / "gilbert.pid"


def _write_pid() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))


def _remove_pid() -> None:
    try:
        PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


async def main() -> None:
    config = load_config()
    gilbert = Gilbert(config)

    await gilbert.start()
    _write_pid()

    web_app = create_app(gilbert)

    uv_config = uvicorn.Config(
        web_app,
        host=config.web.host,
        port=config.web.port,
        log_level="info",
    )
    server = uvicorn.Server(uv_config)

    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _handle_signal() -> None:
        logger.info("Shutdown signal received")
        shutdown_event.set()
        server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    try:
        await server.serve()
    finally:
        await gilbert.stop()
        _remove_pid()


if __name__ == "__main__":
    asyncio.run(main())
