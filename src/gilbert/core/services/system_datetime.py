"""System datetime service — exposes the current wall-clock time as a tool.

Foundational tool for any AI workflow that needs to anchor itself in time:
computing a date range from natural-language phrases ("last week", "today"),
filling in ``YYYY-MM-DD`` parameters for date-aware tools, or including the
date in spoken output. Lives in core because it's universal — every plugin
or agent can chain through this rather than each one implementing its own
clock-reading helper.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)

logger = logging.getLogger(__name__)


class SystemDatetimeService(Service):
    """Expose ``system_datetime``: the current date, time, day of week,
    and timezone, in one tool call. No external dependencies, no state."""

    def __init__(self) -> None:
        self._resolver: ServiceResolver | None = None

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="system-datetime",
            capabilities=frozenset({"ai_tools"}),
            requires=frozenset(),
            optional=frozenset(),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver
        logger.info("System datetime service started")

    async def stop(self) -> None:
        pass

    # --- ToolProvider ---

    @property
    def tool_provider_name(self) -> str:
        return "system"

    def get_tools(self, user_ctx: UserContext | None = None) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="system_datetime",
                description=(
                    "Return the current date, time, day of week, and "
                    "timezone. Call this whenever you need to anchor "
                    "anything in time: computing a date range from "
                    "phrases like 'today' / 'this week' / 'last "
                    "Monday' / 'October' / 'Q3', filling in a "
                    "YYYY-MM-DD argument for another tool that needs "
                    "one, or including the date in spoken output. "
                    "Pass the optional `timezone` argument (IANA "
                    "name like 'America/Los_Angeles' or 'UTC') to "
                    "see the time as it would appear in a different "
                    "zone — defaults to the host's local timezone."
                ),
                parameters=[
                    ToolParameter(
                        name="timezone",
                        type=ToolParameterType.STRING,
                        description=(
                            "IANA timezone name (e.g. "
                            "'America/Los_Angeles', 'UTC', "
                            "'Europe/London'). Defaults to the "
                            "host's local timezone."
                        ),
                        required=False,
                    ),
                ],
                required_role="everyone",
                slash_command="datetime",
                slash_group="system",
                slash_help="Show the current date, time, and day of week.",
                parallel_safe=True,
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name != "system_datetime":
            raise KeyError(f"Unknown tool: {name}")

        tz_arg = (arguments.get("timezone") or "").strip()
        utc_now = datetime.now(UTC)

        if tz_arg:
            try:
                local = utc_now.astimezone(ZoneInfo(tz_arg))
            except ZoneInfoNotFoundError:
                return (
                    f"Unknown timezone '{tz_arg}'. Use an IANA name "
                    "like 'America/Los_Angeles' or 'UTC'."
                )
        else:
            # ``astimezone()`` with no argument resolves to the host's
            # local zone, which is what most callers want.
            local = utc_now.astimezone()

        tz_label = local.tzname() or local.strftime("%z") or "local"
        lines = [
            f"Date: {local.strftime('%Y-%m-%d')}",
            f"Day: {local.strftime('%A')}",
            f"Time: {local.strftime('%H:%M:%S')}",
            f"Timezone: {tz_label}",
            f"Local ISO: {local.isoformat(timespec='seconds')}",
            f"UTC ISO: {utc_now.isoformat(timespec='seconds')}",
        ]
        return "\n".join(lines)
