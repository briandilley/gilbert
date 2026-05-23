"""Logging setup — colored console output and file logging."""

import logging
import re
import sys
from pathlib import Path
from typing import Any

# ANSI color codes
COLORS = {
    "DEBUG": "\033[36m",  # cyan
    "INFO": "\033[32m",  # green
    "WARNING": "\033[33m",  # yellow
    "ERROR": "\033[31m",  # red
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


# ── Redacting filter ──────────────────────────────────────────────────
#
# Spec §6.4 / §6.7 mandate that sensitive field names — OAuth ``code``
# / ``state``, the ``Authorization`` header, ``webhook_url``, and any
# ``oauth_*`` field — never appear in plaintext in log output. The
# defenses are layered: the health route never logs these directly,
# but exception text from httpx (``response.raise_for_status``) and
# similar paths can leak them via formatted strings.
#
# This filter scans every log record's message, args, and ``extra``
# attrs for those keys + sensitive URL query parameters and replaces
# the value with ``[redacted]``. Installed once on the root logger so
# it covers every handler (console, file, AI log).

_SENSITIVE_KEY_PATTERN = (
    r"token|secret|password|code|state|authorization|"
    r"webhook_url|oauth_[a-z_]+"
)
_SENSITIVE_KEY_RE = re.compile(
    rf"^({_SENSITIVE_KEY_PATTERN})$", re.IGNORECASE
)

# ``key=value`` / ``key: value`` / ``"key": "value"`` / ``'key': 'value'``
# patterns — covers serialized dicts, query strings, and exception text.
_KV_PATTERNS = [
    # Quoted JSON-style: "key": "value" or 'key': 'value'.
    # Group 1 is the opening quote; \1 backreferences it on close.
    re.compile(
        rf'(["\'])(?:{_SENSITIVE_KEY_PATTERN})\1\s*[:=]\s*'
        r'(["\'])(?P<value>[^"\']+)\2',
        re.IGNORECASE,
    ),
    # Bare key, quoted value: key="secret" or key='secret'.
    re.compile(
        rf"\b(?:{_SENSITIVE_KEY_PATTERN})\s*[:=]\s*"
        r"(['\"])(?P<value>[^'\"]+)\1",
        re.IGNORECASE,
    ),
    # Query string / kwarg / header style: key=value, key: value
    # (unquoted, value runs until whitespace / & / , / ; / )).
    re.compile(
        rf"\b(?:{_SENSITIVE_KEY_PATTERN})\s*[:=]\s*"
        r"(?P<value>[^\s&,;)\"']+)",
        re.IGNORECASE,
    ),
]

# Bearer token in an Authorization header: ``Bearer <secret>``.
_BEARER_RE = re.compile(r"(Bearer\s+)([A-Za-z0-9._\-]+)", re.IGNORECASE)


def _redact_text(text: str) -> str:
    """Strip sensitive values from ``text``.

    Idempotent: applying twice yields the same string. Defends against
    keys nested inside JSON, query strings, and freeform exception
    messages. Bearer tokens are redacted FIRST (before generic key=value
    patterns) so ``Authorization: Bearer <secret>`` is masked entirely
    rather than collapsing into ``Authorization: [redacted] <secret>``.
    """
    if not text:
        return text
    out = _BEARER_RE.sub(r"\1[redacted]", text)
    for pat in _KV_PATTERNS:
        out = pat.sub(
            lambda m: m.group(0).replace(m.group("value"), "[redacted]"),
            out,
        )
    return out


def _redact_value(value: Any) -> Any:
    """Recursively redact a value that may be a string / dict / list."""
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {
            k: ("[redacted]" if _SENSITIVE_KEY_RE.fullmatch(str(k)) else _redact_value(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        coerced = [_redact_value(v) for v in value]
        return tuple(coerced) if isinstance(value, tuple) else coerced
    return value


class RedactingFilter(logging.Filter):
    """Mask sensitive fields in log records before they reach handlers.

    Scans:
    - ``record.msg`` (the format string or pre-rendered message)
    - ``record.args`` (positional / dict-style ``%`` args)
    - ``record.__dict__`` for ``extra=...`` keys with sensitive names

    Mutates each in place and always returns ``True`` so the record
    continues through the handler chain. Never raises — a redaction
    bug must NOT silence the underlying log call.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # Render the message once (msg % args) so the redactor sees
            # the actual log line, then collapse args to () so handlers
            # don't double-format. ``record.getMessage()`` performs the
            # standard ``%`` substitution; we capture, redact, and store.
            try:
                rendered = record.getMessage()
            except Exception:
                rendered = str(record.msg) if record.msg is not None else ""
            redacted = _redact_text(rendered)
            record.msg = redacted
            record.args = ()
            # ``extra=`` kwargs become attributes on the record dict.
            for attr, value in list(record.__dict__.items()):
                if attr in _RECORD_RESERVED_ATTRS:
                    continue
                if _SENSITIVE_KEY_RE.fullmatch(attr):
                    record.__dict__[attr] = "[redacted]"
                else:
                    record.__dict__[attr] = _redact_value(value)
        except Exception:
            # Never break logging because of a redaction bug.
            pass
        return True


# Reserved LogRecord attributes that should never be mutated by the
# redactor — set by the logging framework itself, not user-supplied.
_RECORD_RESERVED_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "asctime",
        "taskName",
    }
)


# Singleton: ``logging.Filter`` is de-duped by identity, so attaching
# the same instance to multiple handlers / loggers is safe.
_REDACTING_FILTER = RedactingFilter()


def get_redacting_filter() -> RedactingFilter:
    """Return the process-singleton redactor for ad-hoc handler setup."""
    return _REDACTING_FILTER


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

    # Sensitive-field redactor — installed at the root so every
    # handler (console, file, AI log) gets redacted records. Spec
    # §6.4 / §6.7 mandate masking for code, state, Authorization,
    # webhook_url, oauth_*. Filter is idempotent.
    if _REDACTING_FILTER not in root.filters:
        root.addFilter(_REDACTING_FILTER)

    # Console handler — colored output to stderr
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(
        ColorFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s — %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    console.addFilter(_REDACTING_FILTER)
    root.addHandler(console)

    # General file handler
    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(str(path))
        file_handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        file_handler.addFilter(_REDACTING_FILTER)
        root.addHandler(file_handler)

    # AI API call log — separate file for AI-specific logging
    if ai_log_file:
        path = Path(ai_log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        ai_handler = logging.FileHandler(str(path))
        ai_handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        ai_handler.addFilter(_REDACTING_FILTER)
        ai_logger = logging.getLogger("gilbert.ai")
        ai_logger.addHandler(ai_handler)
        ai_logger.setLevel(logging.DEBUG)  # always capture AI calls in detail

    # Per-logger level overrides
    if loggers:
        for logger_name, log_level in loggers.items():
            resolved = getattr(logging, log_level.upper(), None)
            if resolved is not None:
                logging.getLogger(logger_name).setLevel(resolved)
