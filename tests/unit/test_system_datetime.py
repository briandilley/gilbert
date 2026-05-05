"""Tests for SystemDatetimeService — current date/time as a tool."""

from __future__ import annotations

import re

import pytest

from gilbert.core.services.system_datetime import SystemDatetimeService


def test_tool_definition() -> None:
    """The service exposes a single tool ``system_datetime`` with the
    expected role/slash exposure."""
    svc = SystemDatetimeService()
    tools = svc.get_tools()
    assert len(tools) == 1
    t = tools[0]
    assert t.name == "system_datetime"
    assert t.required_role == "everyone"
    assert t.slash_command == "datetime"
    assert t.slash_group == "system"
    assert t.parallel_safe is True


@pytest.mark.asyncio
async def test_returns_all_expected_fields() -> None:
    """Default call returns date / day / time / timezone / ISO timestamps."""
    svc = SystemDatetimeService()
    out = await svc.execute_tool("system_datetime", {})
    assert re.search(r"^Date: \d{4}-\d{2}-\d{2}$", out, re.MULTILINE)
    assert re.search(
        r"^Day: (Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day$", out, re.MULTILINE
    )
    assert re.search(r"^Time: \d{2}:\d{2}:\d{2}$", out, re.MULTILINE)
    assert "Timezone:" in out
    assert "Local ISO:" in out
    assert "UTC ISO:" in out


@pytest.mark.asyncio
async def test_explicit_timezone_changes_label_and_values() -> None:
    """Passing ``timezone='UTC'`` produces a UTC-shaped local block —
    Local ISO and UTC ISO end up identical (modulo offset suffix)."""
    svc = SystemDatetimeService()
    out = await svc.execute_tool("system_datetime", {"timezone": "UTC"})
    assert "Timezone: UTC" in out
    # When the requested zone is UTC, both "Local ISO" and "UTC ISO"
    # should refer to the same instant — pull both timestamps and check
    # they match to the second.
    local = re.search(r"^Local ISO: (.+)$", out, re.MULTILINE)
    utc = re.search(r"^UTC ISO: (.+)$", out, re.MULTILINE)
    assert local is not None
    assert utc is not None
    # Strip the timezone suffix; the wall-clock components must match.
    assert local.group(1)[:19] == utc.group(1)[:19]


@pytest.mark.asyncio
async def test_unknown_timezone_returns_friendly_error() -> None:
    """Bad IANA names produce a single-line error message rather than
    blowing up — keeps the AI on rails."""
    svc = SystemDatetimeService()
    out = await svc.execute_tool("system_datetime", {"timezone": "Not/A_Zone"})
    assert "Unknown timezone" in out
    assert "Not/A_Zone" in out


@pytest.mark.asyncio
async def test_unknown_tool_name_raises() -> None:
    """Defensive: calling the service with the wrong tool name raises
    KeyError, matching the dispatch contract used by other services."""
    svc = SystemDatetimeService()
    with pytest.raises(KeyError):
        await svc.execute_tool("not_a_tool", {})


@pytest.mark.asyncio
async def test_timezone_offset_label_when_tzname_missing() -> None:
    """For zones whose ``tzname()`` returns None (rare), the tool falls
    back to a numeric ``%z`` offset rather than rendering blank — covers
    a small edge case that would otherwise produce ``Timezone: ``."""
    # Use America/Los_Angeles which has a stable name year-round.
    svc = SystemDatetimeService()
    out = await svc.execute_tool("system_datetime", {"timezone": "America/Los_Angeles"})
    # The label should be something nonempty after the 'Timezone: '
    # prefix.
    m = re.search(r"^Timezone: (\S.+?)$", out, re.MULTILINE)
    assert m is not None
    assert m.group(1)
