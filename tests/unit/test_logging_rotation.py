"""Tests for log file rotation.

Regression: both file handlers were plain ``logging.FileHandler``, so log
files grew without bound. A single unreachable device logged ~17k warning
lines a day and produced a 780MB ``gilbert.log`` (plus a 734MB stderr log)
before anyone noticed.
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import pytest

from gilbert.config import LoggingConfig
from gilbert.core.logging import setup_logging


@pytest.fixture(autouse=True)
def _restore_logging() -> Any:
    """Keep these tests from leaving handlers on the root/ai loggers."""
    root = logging.getLogger()
    ai = logging.getLogger("gilbert.ai")
    saved = (list(root.handlers), root.level, list(ai.handlers), ai.level)
    yield
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
    ai.handlers[:] = saved[2]
    ai.setLevel(saved[3])


def _file_handlers(logger: logging.Logger) -> list[logging.Handler]:
    return [h for h in logger.handlers if isinstance(h, logging.FileHandler)]


def test_general_log_handler_rotates(tmp_path: Path) -> None:
    setup_logging(level="INFO", log_file=str(tmp_path / "gilbert.log"))

    handlers = _file_handlers(logging.getLogger())
    assert handlers, "expected a file handler on the root logger"
    for handler in handlers:
        assert isinstance(handler, RotatingFileHandler), (
            "the general log must rotate — an unbounded FileHandler is how "
            "a single repeating warning produced a 780MB log"
        )
        assert handler.maxBytes > 0
        assert handler.backupCount > 0


def test_ai_log_handler_rotates(tmp_path: Path) -> None:
    setup_logging(
        level="INFO",
        log_file=None,
        ai_log_file=str(tmp_path / "ai_calls.log"),
    )

    handlers = _file_handlers(logging.getLogger("gilbert.ai"))
    assert handlers, "expected a file handler on the gilbert.ai logger"
    for handler in handlers:
        assert isinstance(handler, RotatingFileHandler)
        assert handler.maxBytes > 0
        assert handler.backupCount > 0


def test_rotation_limits_are_configurable(tmp_path: Path) -> None:
    setup_logging(
        level="INFO",
        log_file=str(tmp_path / "gilbert.log"),
        max_bytes=1024,
        backup_count=2,
    )

    handler = _file_handlers(logging.getLogger())[0]
    assert isinstance(handler, RotatingFileHandler)
    assert handler.maxBytes == 1024
    assert handler.backupCount == 2


def test_rotation_actually_caps_total_size(tmp_path: Path) -> None:
    """The point of the exercise: writing far more than the cap must not
    leave a file far larger than the cap."""
    log_file = tmp_path / "gilbert.log"
    setup_logging(
        level="INFO",
        log_file=str(log_file),
        max_bytes=2048,
        backup_count=2,
    )
    logger = logging.getLogger("rotation-test")

    for i in range(2000):
        logger.warning("UniFi Protect ring poll failed: unreachable %d", i)

    for handler in _file_handlers(logging.getLogger()):
        handler.flush()

    written = sorted(tmp_path.glob("gilbert.log*"))
    total = sum(p.stat().st_size for p in written)
    # active file + backup_count rotations, each capped at maxBytes, plus
    # slack for the record that triggers a rollover.
    assert total < 2048 * 4, (
        f"rotation should cap total log size; got {total} bytes across "
        f"{[p.name for p in written]}"
    )
    assert len(written) > 1, "expected at least one rotated backup"


def test_logging_config_exposes_rotation_defaults() -> None:
    config = LoggingConfig()
    assert config.max_bytes > 0
    assert config.backup_count > 0
