"""Tests for ``POST /api/telnyx/messages/webhook``.

The route is a thin dispatcher — it resolves the
``telnyx_messaging_webhook`` capability off the live Gilbert instance
and hands the raw JSON to it. These tests verify the routing layer
behaves correctly under the three states it can land in: no plugin
loaded, plugin loaded + capability resolves cleanly, plugin loaded
but its handler raises.

The Telnyx webhook contract is "200-everything-back" — non-2xx means
Telnyx will retry, and we'd rather log a bug than back up their
queue. All three states return 200; the body's ``status`` field
distinguishes them.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gilbert.web.routes.telnyx_webhooks import router as telnyx_router


class _CapableEndpoint:
    """Stub that satisfies the ``MessagingWebhookEndpoint`` Protocol
    (the runtime_checkable shape is just one method).
    """

    def __init__(self) -> None:
        self.delivered: list[dict[str, Any]] = []
        self.raise_on_deliver = False

    async def deliver_webhook_event(self, payload: dict[str, object]) -> None:
        if self.raise_on_deliver:
            raise RuntimeError("simulated handler error")
        self.delivered.append(dict(payload))


def _make_app(endpoint: Any | None) -> FastAPI:
    """Build a FastAPI test app where ``app.state.gilbert`` exposes a
    service_manager whose ``get_capability`` returns the test
    endpoint."""
    app = FastAPI()

    class _SM:
        def get_capability(self, name: str) -> Any:
            if name == "telnyx_messaging_webhook":
                return endpoint
            return None

    app.state.gilbert = SimpleNamespace(service_manager=_SM())
    app.include_router(telnyx_router)
    return app


def test_messages_webhook_dispatches_to_capability() -> None:
    endpoint = _CapableEndpoint()
    client = TestClient(_make_app(endpoint))

    payload = {
        "data": {
            "event_type": "message.received",
            "payload": {"id": "m1", "text": "hi"},
        }
    }
    resp = client.post("/api/telnyx/messages/webhook", json=payload)

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert endpoint.delivered == [payload]


def test_messages_webhook_returns_no_plugin_when_capability_absent() -> None:
    """Plugin unloaded / messaging disabled — route still 200s with
    ``status: no_plugin`` so Telnyx doesn't retry-storm us."""
    client = TestClient(_make_app(None))
    resp = client.post(
        "/api/telnyx/messages/webhook", json={"data": {}}
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "no_plugin"}


def test_messages_webhook_swallows_handler_error_and_returns_200() -> None:
    """If the plugin's deliverer raises, the route logs but returns
    200. Telnyx's retries on 5xx would flood us with the same event
    over and over for a transient bug."""
    endpoint = _CapableEndpoint()
    endpoint.raise_on_deliver = True
    client = TestClient(_make_app(endpoint))
    resp = client.post(
        "/api/telnyx/messages/webhook",
        json={"data": {"event_type": "message.received"}},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "error"}


def test_messages_webhook_returns_bad_request_on_non_json_body() -> None:
    endpoint = _CapableEndpoint()
    client = TestClient(_make_app(endpoint))
    resp = client.post(
        "/api/telnyx/messages/webhook",
        content=b"not json at all",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "bad_request"}
    assert endpoint.delivered == []
