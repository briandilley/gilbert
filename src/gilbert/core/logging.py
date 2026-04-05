"""Logging setup — colored console output and file logging."""

import logging
import sys
from pathlib import Path

# ANSI color codes
COLORS = {
    "DEBUG": "\033[36m",     # cyan
    "INFO": "\033[32m",      # green
    "WARNING": "\033[33m",   # yellow
    "ERROR": "\033[31m",     # red
    "CRITICAL": "\033[1;31m",  # bold red
}
RESET = "\033[0m"


class ColorFormatter(logging.Formatter):
    """Formatter that adds ANSI colors to log level names."""

    def __init__(self, fmt: str | None = None, datefmt: str | None = None) -> None:
        super().__init__(fmt, datefmt)

    def format(self, record: logging.LogRecord) -> str:
        color = COLORS.get(record.levelname, "")
        record.levelname = f"{color}{record.levelname:<8}{RESET}"
        return super().format(record)


def setup_logging(
    level: str = "INFO",
    log_file: str | None = None,
    ai_log_file: str | None = None,
    loggers: dict[str, str] | None = None,
) -> None:
    """Configure the logging system.

    Args:
        level: Root log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        log_file: Path to the general log file. None disables file logging.
        ai_log_file: Path to the AI API call log file. None disables.
        loggers: Per-logger level overrides (e.g., {"httpx": "WARNING"}).
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Clear any existing handlers
    root.handlers.clear()

    # Console handler — colored output to stderr
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(ColorFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    ))
    root.addHandler(console)

    # General file handler
    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(str(path))
        file_handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        root.addHandler(file_handler)

    # AI API call log — separate file for AI-specific logging
    if ai_log_file:
        path = Path(ai_log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        ai_handler = logging.FileHandler(str(path))
        ai_handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        ai_logger = logging.getLogger("gilbert.ai")
        ai_logger.addHandler(ai_handler)
        ai_logger.setLevel(logging.DEBUG)  # always capture AI calls in detail

    # Per-logger level overrides
    if loggers:
        for logger_name, log_level in loggers.items():
            resolved = getattr(logging, log_level.upper(), None)
            if resolved is not None:
                logging.getLogger(logger_name).setLevel(resolved)
