"""Tests for WebSocket protocol — visibility, subscriptions, frame dispatch."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.events import Event
from gilbert.web.ws_protocol import (
    WsConnection,
    WsConnectionManager,
    can_see_event,
    dispatch_frame,
    get_event_visibility_level,
)


# --- Event visibility ---


class TestEventVisibility:
    def test_presence_is_user(self) -> None:
        assert get_event_visibility_level("presence.arrived") == 100

    def test_doorbell_is_everyone(self) -> None:
        assert get_event_visibility_level("doorbell.ring") == 200

    def test_greeting_is_everyone(self) -> None:
        assert get_event_visibility_level("greeting.announced") == 200

    def test_timer_is_user(self) -> None:
        assert get_event_visibility_level("timer.fired") == 100

    def test_chat_is_everyone(self) -> None:
        assert get_event_visibility_level("chat.message.created") == 200

    def test_inbox_is_admin(self) -> None:
        assert get_event_visibility_level("inbox.message.received") == 0

    def test_radio_dj_is_everyone(self) -> None:
        assert get_event_visibility_level("radio_dj.started") == 200

    def test_service_is_admin(self) -> None:
        assert get_event_visibility_level("service.started") == 0

    def test_config_is_admin(self) -> None:
        assert get_event_visibility_level("config.changed") == 0

    def test_acl_is_admin(self) -> None:
        assert get_event_visibility_level("acl.updated") == 0

    def test_unknown_defaults_to_user(self) -> None:
        assert get_event_visibility_level("some.random.event") == 100

    def test_admin_can_see_everything(self) -> None:
        assert can_see_event(0, "service.started")
        assert can_see_event(0, "chat.message.created")
        assert can_see_event(0, "presence.arrived")

    def test_user_sees_user_and_everyone(self) -> None:
        assert not can_see_event(100, "service.started")
        assert can_see_event(100, "chat.message.created")
        assert can_see_event(100, "presence.arrived")

    def test_everyone_sees_only_everyone(self) -> None:
        assert not can_see_event(200, "service.started")
        assert can_see_event(200, "chat.message.created")  # chat is everyone now
        assert not can_see_event(200, "presence.arrived")  # presence is user now

    def test_system_bypasses_all(self) -> None:
        assert can_see_event(-1, "service.started")
        assert can_see_event(-1, "config.changed")


# --- Subscription matching ---


class TestSubscriptions:
    def _conn(self, level: int = 100, patterns: set[str] | None = None) -> WsConnection:
        user = UserContext(user_id="test", email="", display_name="Test", roles=frozenset({"user"}))
        manager = MagicMock(spec=WsConnectionManager)
        conn = WsConnection(user, level, manager)
        if patterns is not None:
            conn.subscriptions = patterns
        return conn

    def test_wildcard_matches_all(self) -> None:
        conn = self._conn()
        assert conn.matches_subscription("chat.message.created")
        assert conn.matches_subscription("presence.arrived")

    def test_specific_pattern(self) -> None:
        conn = self._conn(patterns={"chat.*"})
        assert conn.matches_subscription("chat.message.created")
        assert not conn.matches_subscription("presence.arrived")

    def test_empty_subscriptions_match_nothing(self) -> None:
        conn = self._conn(patterns=set())
        assert not conn.matches_subscription("chat.message.created")

    def test_multiple_patterns(self) -> None:
        conn = self._conn(patterns={"chat.*", "presence.*"})
        assert conn.matches_subscription("chat.message.created")
        assert conn.matches_subscription("presence.arrived")
        assert not conn.matches_subscription("service.started")


# --- Chat event content filtering ---


class TestChatFiltering:
    def _conn(self, user_id: str = "user1", conv_ids: set[str] | None = None) -> WsConnection:
        user = UserContext(user_id=user_id, email="", display_name="User", roles=frozenset({"user"}))
        manager = MagicMock(spec=WsConnectionManager)
        conn = WsConnection(user, 100, manager)
        if conv_ids:
            conn.shared_conv_ids = conv_ids
        return conn

    def test_non_chat_events_pass(self) -> None:
        conn = self._conn()
        event = Event(event_type="presence.arrived", data={"user_id": "x"})
        assert conn.can_see_chat_event(event)

    def test_member_sees_own_conv(self) -> None:
        conn = self._conn(conv_ids={"conv1"})
        event = Event(event_type="chat.message.created", data={"conversation_id": "conv1"})
        assert conn.can_see_chat_event(event)

    def test_non_member_blocked(self) -> None:
        conn = self._conn(conv_ids={"conv1"})
        event = Event(event_type="chat.message.created", data={"conversation_id": "conv2"})
        assert not conn.can_see_chat_event(event)

    def test_visible_to_filters(self) -> None:
        conn = self._conn(user_id="user1", conv_ids={"conv1"})
        event = Event(event_type="chat.message.created", data={
            "conversation_id": "conv1", "visible_to": ["user2"],
        })
        assert not conn.can_see_chat_event(event)

    def test_join_event_updates_membership(self) -> None:
        conn = self._conn(user_id="user1")
        event = Event(event_type="chat.member.joined", data={
            "conversation_id": "conv1", "user_id": "user1",
        })
        assert conn.can_see_chat_event(event)
        assert "conv1" in conn.shared_conv_ids


# --- Connection manager dispatch ---


class TestConnectionManager:
    async def test_dispatches_to_eligible_connections(self) -> None:
        manager = WsConnectionManager()

        admin_user = UserContext(user_id="admin", email="", display_name="Admin", roles=frozenset({"admin"}))
        guest_user = UserContext(user_id="guest", email="", display_name="Guest", roles=frozenset({"everyone"}))

        admin_conn = WsConnection(admin_user, 0, manager)
        guest_conn = WsConnection(guest_user, 200, manager)

        manager.register(admin_conn)
        manager.register(guest_conn)

        # Admin event
        event = Event(event_type="service.started", data={"name": "test"}, source="test")
        await manager._dispatch_event(event)

        # Admin should have it, guest should not
        assert not admin_conn.queue.empty()
        assert guest_conn.queue.empty()

        # Clear admin queue
        admin_conn.queue.get_nowait()

        # Everyone event (chat is everyone-visible now)
        event2 = Event(event_type="chat.message.created", data={"conversation_id": ""}, source="test")
        await manager._dispatch_event(event2)

        assert not admin_conn.queue.empty()
        assert not guest_conn.queue.empty()


# --- Frame dispatch ---


class TestFrameDispatch:
    def _conn(self, level: int = 100) -> WsConnection:
        user = UserContext(user_id="test", email="", display_name="Test", roles=frozenset({"user"}))
        manager = MagicMock(spec=WsConnectionManager)
        from gilbert.web.ws_protocol import _rpc_handlers
        manager._handlers = dict(_rpc_handlers)
        manager._gilbert = None  # no ACL service → fall through to defaults
        return WsConnection(user, level, manager)

    async def test_subscribe(self) -> None:
        conn = self._conn()
        result = await dispatch_frame(conn, {
            "type": "gilbert.sub.add", "id": "1", "patterns": ["chat.*"],
        })
        assert result["ok"] is True
        assert "chat.*" in conn.subscriptions

    async def test_unsubscribe(self) -> None:
        conn = self._conn()
        conn.subscriptions = {"*", "chat.*"}
        result = await dispatch_frame(conn, {
            "type": "gilbert.sub.remove", "id": "2", "patterns": ["*"],
        })
        assert result["ok"] is True
        assert "*" not in conn.subscriptions
        assert "chat.*" in conn.subscriptions

    async def test_list_subscriptions(self) -> None:
        conn = self._conn()
        result = await dispatch_frame(conn, {"type": "gilbert.sub.list", "id": "3"})
        assert result["subscriptions"] == ["*"]

    async def test_ping_pong(self) -> None:
        conn = self._conn()
        result = await dispatch_frame(conn, {"type": "gilbert.ping"})
        assert result["type"] == "gilbert.pong"

    async def test_unknown_type_returns_error(self) -> None:
        conn = self._conn()
        result = await dispatch_frame(conn, {"type": "unknown.frame", "id": "4"})
        assert result["type"] == "gilbert.error"
        assert result["code"] == 400

    async def test_peer_publish_requires_role(self) -> None:
        conn = self._conn()  # level 100 (user), not peer/admin
        result = await dispatch_frame(conn, {
            "type": "gilbert.peer.publish",
            "id": "5",
            "event_type": "test.event",
            "data": {},
        })
        assert result["code"] == 403
