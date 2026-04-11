"""Event bus service — wraps EventBus as a discoverable service."""

import json
from typing import Any

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.events import Event, EventBus
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)


class EventBusService(Service):
    """Exposes an EventBus as a service with event_bus capability."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="event_bus",
            capabilities=frozenset({"event_bus", "pub_sub", "ai_tools"}),
        )

    @property
    def bus(self) -> EventBus:
        return self._bus

    # --- Configurable protocol ---

    @property
    def config_namespace(self) -> str:
        return "event_bus"

    @property
    def config_category(self) -> str:
        return "Infrastructure"

    def config_params(self) -> list[ConfigParam]:
        return []  # No configurable params — pure infrastructure

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        pass

    # --- ToolProvider protocol ---

    @property
    def tool_provider_name(self) -> str:
        return "event_bus"

    def get_tools(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="publish_event",
                description="Publish an event to the event bus. Subscribed handlers will be notified.",
                required_role="admin",
                parameters=[
                    ToolParameter(
                        name="event_type",
                        type=ToolParameterType.STRING,
                        description="The event type (e.g., 'user.reminder', 'automation.trigger').",
                    ),
                    ToolParameter(
                        name="data",
                        type=ToolParameterType.OBJECT,
                        description="Event payload data.",
                        required=False,
                    ),
                    ToolParameter(
                        name="source",
                        type=ToolParameterType.STRING,
                        description="Source of the event. Defaults to 'ai'.",
                        required=False,
                    ),
                ],
            ),
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        match name:
            case "publish_event":
                return await self._tool_publish_event(arguments)
            case _:
                raise KeyError(f"Unknown tool: {name}")

    async def _tool_publish_event(self, arguments: dict[str, Any]) -> str:
        event_type = arguments["event_type"]
        data = arguments.get("data", {})
        source = arguments.get("source", "ai")

        event = Event(event_type=event_type, data=data, source=source)
        await self._bus.publish(event)

        return json.dumps({
            "status": "ok",
            "event_type": event_type,
            "timestamp": event.timestamp.isoformat(),
        })
