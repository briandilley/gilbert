"""Tests for the PushNotificationBackend interface."""

from __future__ import annotations

from typing import Any

import pytest

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.notifications import NotificationUrgency
from gilbert.interfaces.push_notifications import (
    PushDeliveryResult,
    PushDeliveryStatus,
    PushDestination,
    PushMessage,
    PushNotificationBackend,
)
from gilbert.interfaces.tools import ToolParameterType


class _FakeBackend(PushNotificationBackend):
    backend_name = "fake-iface-test"

    @classmethod
    def destination_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="topic",
                type=ToolParameterType.STRING,
                description="Test topic",
                default="",
            ),
        ]

    async def initialize(self, config: dict[str, Any]) -> None:
        return None

    async def close(self) -> None:
        return None

    async def send(
        self,
        destination: PushDestination,
        message: PushMessage,
    ) -> PushDeliveryResult:
        return PushDeliveryResult(status=PushDeliveryStatus.DELIVERED, message="ok")


def test_subclass_is_registered() -> None:
    backends = PushNotificationBackend.registered_backends()
    assert "fake-iface-test" in backends
    assert backends["fake-iface-test"] is _FakeBackend


def test_destination_params_is_classmethod() -> None:
    # Must be callable on the class itself (no instance required).
    params = _FakeBackend.destination_params()
    assert isinstance(params, list)
    assert params[0].key == "topic"


def test_default_runtime_data_is_empty() -> None:
    inst = _FakeBackend()
    assert inst.runtime_data() == {}


def test_default_invoke_backend_action_returns_unknown_action() -> None:
    inst = _FakeBackend()
    import asyncio

    result = asyncio.run(inst.invoke_backend_action("nope", {}))
    assert result.status == "error"
    assert "Unknown action" in result.message


def test_push_message_carries_metadata() -> None:
    msg = PushMessage(
        title="Gilbert",
        body="hi",
        urgency=NotificationUrgency.NORMAL,
        source="agent",
        notification_id="n_123",
    )
    assert msg.title == "Gilbert"
    assert msg.urgency is NotificationUrgency.NORMAL
    assert msg.source_ref is None


@pytest.mark.parametrize(
    "status",
    [
        PushDeliveryStatus.DELIVERED,
        PushDeliveryStatus.REJECTED,
        PushDeliveryStatus.TRANSIENT_ERROR,
        PushDeliveryStatus.DISABLED,
    ],
)
def test_status_round_trips(status: PushDeliveryStatus) -> None:
    r = PushDeliveryResult(status=status, message="m")
    assert r.status is status
    assert r.message == "m"

