"""Tests for the log-redaction filter.

Spec §6.4 / §6.7 / §16.5 mandate that the keys ``code``, ``state``,
``Authorization``, ``webhook_url``, and ``oauth_*`` never appear as
plaintext values in log output. The filter sits at the root logger
so every handler (console, file, AI log) emits redacted records.
"""

from __future__ import annotations

import logging

import pytest

from gilbert.core.logging import (
    RedactingFilter,
    _redact_text,
    get_redacting_filter,
)

# ── _redact_text unit cases ─────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,sentinel",
    [
        ("got code=abc123 from provider", "abc123"),
        ('payload {"state": "stt-xyz789"}', "stt-xyz789"),
        ("Authorization: Bearer eyJ0eXA.live.token", "eyJ0eXA.live.token"),
        ("webhook_url=https://example/webhook/health/abc", "https://example/webhook/health/abc"),
        ("oauth_access_token=verylongsecret", "verylongsecret"),
        ("oauth_refresh_token: 'refresh_secret_789'", "refresh_secret_789"),
        ("password=hunter2", "hunter2"),
        ("token=plaintext_secret", "plaintext_secret"),
    ],
)
def test_redact_text_strips_sensitive_values(raw: str, sentinel: str) -> None:
    out = _redact_text(raw)
    assert sentinel not in out, f"{sentinel!r} survived redaction in {out!r}"
    assert "[redacted]" in out


def test_redact_text_idempotent() -> None:
    once = _redact_text("code=abc state=xyz Authorization: Bearer secret")
    twice = _redact_text(once)
    assert once == twice


def test_redact_text_no_match_passes_through() -> None:
    assert _redact_text("nothing sensitive here") == "nothing sensitive here"


# ── Filter end-to-end via caplog ────────────────────────────────────


@pytest.fixture
def filtered_logger(caplog: pytest.LogCaptureFixture) -> logging.Logger:
    """Return a logger with the redacting filter installed.

    pytest-asyncio's caplog adds its own handler; we install the
    filter on the logger itself so every record passing through is
    redacted before caplog records the message.
    """
    logger = logging.getLogger(f"test_redaction.{id(caplog)}")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    f = get_redacting_filter()
    if f not in logger.filters:
        logger.addFilter(f)
    # caplog.records is populated regardless of handler attachment, but
    # we have to make sure the filter runs before caplog observes the
    # record. Adding the filter at the logger level achieves that.
    return logger


@pytest.mark.parametrize(
    "key,sentinel",
    [
        ("code", "alpha-code-789"),
        ("state", "beta-state-456"),
        ("Authorization", "Bearer gamma-auth-123"),
        ("webhook_url", "https://example.com/wh/secret-token-456"),
        ("oauth_access_token", "epsilon-access-321"),
    ],
)
def test_log_redaction_each_key_produces_redacted(
    filtered_logger: logging.Logger,
    caplog: pytest.LogCaptureFixture,
    key: str,
    sentinel: str,
) -> None:
    """For each of the spec-listed keys, log a record with that key
    in BOTH the message string AND in ``extra``. Assert the captured
    output contains ``[redacted]`` and NEVER the actual sentinel."""
    caplog.set_level(logging.DEBUG, logger=filtered_logger.name)
    filtered_logger.info(
        "delivery: %s=%s",
        key,
        sentinel,
        extra={key: sentinel},
    )
    rendered = "\n".join(rec.getMessage() for rec in caplog.records)
    record_attrs = " ".join(
        str(getattr(rec, key, ""))
        for rec in caplog.records
        if hasattr(rec, key)
    )
    full_text = rendered + " " + record_attrs
    assert sentinel not in full_text, (
        f"Sentinel {sentinel!r} survived in log output: {full_text!r}"
    )
    assert "[redacted]" in full_text


def test_log_redaction_does_not_break_non_sensitive_logging(
    filtered_logger: logging.Logger,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=filtered_logger.name)
    filtered_logger.info("ordinary message with %s args", "42")
    rendered = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "ordinary message with 42 args" in rendered


def test_log_redaction_filter_is_singleton() -> None:
    """Multiple calls return the same instance — addFilter is then
    idempotent across handlers."""
    a = get_redacting_filter()
    b = get_redacting_filter()
    assert a is b
    assert isinstance(a, RedactingFilter)


def test_log_redaction_does_not_raise_on_malformed_record(
    filtered_logger: logging.Logger,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A redaction bug must NEVER swallow the underlying log call."""
    caplog.set_level(logging.DEBUG, logger=filtered_logger.name)

    class _Weird:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    # Should NOT raise — the filter swallows internal exceptions.
    filtered_logger.info("with weird %s arg", _Weird())
    # And the record was still emitted.
    assert any(
        "with weird" in rec.getMessage() for rec in caplog.records
    )
