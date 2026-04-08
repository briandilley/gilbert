"""Slack integration — Socket Mode bot that routes DMs and mentions through AI.

Connects to Slack via Socket Mode (WebSocket), receives DMs and @mentions,
routes them through Gilbert's AIService, and posts responses back.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections import OrderedDict
from typing import Any

from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver

logger = logging.getLogger(__name__)


class _BoundedSet:
    """A set with a maximum size. Oldest entries are evicted first."""

    def __init__(self, maxlen: int) -> None:
        self._data: OrderedDict[str, None] = OrderedDict()
        self._maxlen = maxlen

    def __contains__(self, item: str) -> bool:
        return item in self._data

    def add(self, item: str) -> None:
        if item in self._data:
            self._data.move_to_end(item)
            return
        if len(self._data) >= self._maxlen:
            self._data.popitem(last=False)
        self._data[item] = None


class SlackService(Service):
    """Slack Socket Mode bot that bridges Slack conversations to Gilbert AI.

    Handles DMs, @mentions, and thread replies where the bot is participating.
    """

    def __init__(
        self,
        bot_credential: str,
        app_credential: str,
    ) -> None:
        self._bot_credential = bot_credential
        self._app_credential = app_credential

        self._ai: Any = None
        self._user_svc: Any = None
        self._resolver: ServiceResolver | None = None

        # In-memory conversation mapping
        self._channel_conversations: dict[str, str] = {}  # channel_id -> conv_id (DMs)
        self._thread_conversations: dict[str, str] = {}  # channel:thread_ts -> conv_id

        # Deduplication: track processed message timestamps
        self._processed: _BoundedSet = _BoundedSet(1000)

        # Cache of threads where we know the bot is NOT participating
        self._ignored_threads: _BoundedSet = _BoundedSet(500)

        self._bot_user_id: str = ""
        self._task: asyncio.Task[None] | None = None

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="slack",
            capabilities=frozenset({"slack"}),
            requires=frozenset({"ai_chat"}),
            optional=frozenset({"credentials", "users"}),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        try:
            from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
            from slack_bolt.async_app import AsyncApp
        except ImportError:
            logger.warning(
                "slack-bolt not installed — Slack service disabled. "
                "Install with: uv add slack-bolt"
            )
            return

        self._resolver = resolver
        self._ai = resolver.require_capability("ai_chat")
        self._user_svc = resolver.get_capability("users")

        # Resolve credentials
        cred_svc = resolver.get_capability("credentials")
        if cred_svc is None:
            logger.error("Slack service requires credentials service")
            return

        from gilbert.interfaces.credentials import ApiKeyCredential

        bot_cred = cred_svc.get(self._bot_credential)
        if not isinstance(bot_cred, ApiKeyCredential):
            logger.error(
                "Slack bot credential '%s' not found or not api_key type",
                self._bot_credential,
            )
            return

        app_cred = cred_svc.get(self._app_credential)
        if not isinstance(app_cred, ApiKeyCredential):
            logger.error(
                "Slack app credential '%s' not found or not api_key type",
                self._app_credential,
            )
            return

        bot_token = bot_cred.api_key
        app_token = app_cred.api_key

        # Build the Slack Bolt app
        app = AsyncApp(token=bot_token)

        # Get our bot user ID for mention stripping
        try:
            auth_result = await app.client.auth_test()
            self._bot_user_id = auth_result.get("user_id", "")
            logger.info("Slack bot user ID: %s", self._bot_user_id)
        except Exception:
            logger.exception("Failed to get Slack bot identity")
            return

        # Register event handlers
        @app.event("message")
        async def handle_message(event: dict[str, Any], say: Any) -> None:
            await self._handle_message_event(event, say)

        @app.event("app_mention")
        async def handle_mention(event: dict[str, Any], say: Any) -> None:
            await self._handle_message_event(event, say)

        # Start Socket Mode in a background task
        handler = AsyncSocketModeHandler(app, app_token)
        self._task = asyncio.create_task(self._run_handler(handler))
        logger.info("Slack service started (bot_credential=%s)", self._bot_credential)

    async def _run_handler(self, handler: Any) -> None:
        """Run the Socket Mode handler. Logs errors but doesn't crash."""
        try:
            await handler.start_async()
        except asyncio.CancelledError:
            await handler.close_async()
        except Exception:
            logger.exception("Slack Socket Mode handler crashed")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        logger.info("Slack service stopped")

    # -- Event handling --

    async def _handle_message_event(self, event: dict[str, Any], say: Any) -> None:
        """Process a Slack message event (DM, mention, or thread reply)."""
        # Skip bot messages and message edits/deletes
        if event.get("bot_id") or event.get("subtype"):
            return

        ts = event.get("ts", "")
        channel = event.get("channel", "")
        thread_ts = event.get("thread_ts")
        text = event.get("text", "")
        user_id = event.get("user", "")

        # Deduplication
        if ts in self._processed:
            return
        self._processed.add(ts)

        # Determine channel type from event
        channel_type = event.get("channel_type", "")

        # Is this a DM?
        is_dm = channel_type == "im"

        # Is this a mention?
        is_mention = self._bot_user_id and f"<@{self._bot_user_id}>" in text

        # Is this a thread reply where we're participating?
        is_thread_reply = False
        if thread_ts and not is_dm and not is_mention:
            thread_key = f"{channel}:{thread_ts}"
            if thread_key in self._ignored_threads:
                return
            is_thread_reply = await self._bot_in_thread(say, channel, thread_ts)
            if not is_thread_reply:
                self._ignored_threads.add(thread_key)
                return

        if not is_dm and not is_mention and not is_thread_reply:
            return

        # Strip bot mention from text
        if self._bot_user_id:
            text = re.sub(rf"<@{re.escape(self._bot_user_id)}>", "", text).strip()

        if not text:
            return

        # Resolve user
        user_ctx = await self._resolve_user(say, user_id)

        # Determine conversation ID
        if is_dm:
            conversation_id = self._channel_conversations.get(channel)
        else:
            thread_key = f"{channel}:{thread_ts or ts}"
            conversation_id = self._thread_conversations.get(thread_key)

        # Run through AI
        try:
            response_text, conv_id, _ui = await self._ai.chat(
                user_message=text,
                conversation_id=conversation_id,
                user_ctx=user_ctx,
                ai_call="human_chat",
            )
        except Exception:
            logger.exception("AI chat failed for Slack message ts=%s", ts)
            await say(
                text="Sorry, I encountered an error processing your message.",
                thread_ts=thread_ts or ts,
            )
            return

        # Store conversation mapping
        if is_dm:
            self._channel_conversations[channel] = conv_id
        else:
            thread_key = f"{channel}:{thread_ts or ts}"
            self._thread_conversations[thread_key] = conv_id

        # Reply in thread (or same DM channel)
        reply_thread = thread_ts or ts if not is_dm else None
        await say(text=response_text, thread_ts=reply_thread)

    async def _bot_in_thread(self, say: Any, channel: str, thread_ts: str) -> bool:
        """Check if the bot has participated in a thread."""
        try:
            # Use the underlying Slack client from the say callable
            client = say.__self__ if hasattr(say, "__self__") else None
            if client is None:
                return False

            # Fetch thread replies
            result = await client.conversations_replies(
                channel=channel,
                ts=thread_ts,
                limit=100,
            )
            messages = result.get("messages", [])
            for msg in messages:
                if msg.get("bot_id") or msg.get("user") == self._bot_user_id:
                    return True
        except Exception:
            logger.debug("Failed to check thread participation: %s:%s", channel, thread_ts)
        return False

    async def _resolve_user(self, say: Any, slack_user_id: str) -> UserContext:
        """Resolve a Slack user ID to a Gilbert UserContext."""
        display_name = slack_user_id
        email = ""

        # Try to fetch Slack user profile
        try:
            client = say.__self__ if hasattr(say, "__self__") else None
            if client is not None:
                result = await client.users_info(user=slack_user_id)
                profile = result.get("user", {}).get("profile", {})
                display_name = (
                    profile.get("display_name")
                    or profile.get("real_name")
                    or display_name
                )
                email = profile.get("email", "")
        except Exception:
            logger.debug("Failed to fetch Slack profile for %s", slack_user_id)

        # Try email match against UserService
        if email and self._user_svc is not None:
            try:
                user = await self._user_svc.get_user_by_email(email)
                if user is not None:
                    return UserContext(
                        user_id=user.get("_id", email),
                        email=user.get("email", email),
                        display_name=user.get("display_name", display_name),
                        roles=frozenset(user.get("roles", ["user"])),
                        provider="slack",
                    )
            except Exception:
                logger.debug("User lookup failed for %s", email)

        # Fallback
        return UserContext(
            user_id=f"slack:{slack_user_id}",
            email=email,
            display_name=display_name,
            roles=frozenset({"user"}),
            provider="slack",
        )
