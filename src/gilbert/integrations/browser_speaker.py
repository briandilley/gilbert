"""Browser speaker backend — plays audio in the requesting user's SPA tab.

This backend turns each authenticated user's connected browser into a
private virtual speaker. ``play_uri`` publishes a ``speaker.browser.play``
event to the bus with ``data.user_id`` set to the current request user;
the WebSocket layer's ``can_see_speaker_browser_event`` filter delivers
the frame only to that user's connections, and the SPA plays the audio
inline in the active chat.

Useful for Gilbert deployments on a headless server (homelab, VM, NAS)
where Sonos isn't available and the host-machine ``local`` backend has
no audible output. The "speaker" is ephemeral and request-scoped — each
user always sees exactly one speaker (their own browser), regardless
of who else is connected.
"""

from __future__ import annotations

import logging

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.context import get_current_conversation_id, get_current_user
from gilbert.interfaces.events import Event, EventBus, EventBusProvider
from gilbert.interfaces.speaker import (
    PlaybackState,
    PlayRequest,
    SpeakerBackend,
    SpeakerInfo,
    to_browser_url,
)
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)

_SPEAKER_ID_PREFIX = "browser:"
_DEFAULT_VOLUME = 80


class BrowserSpeakerBackend(SpeakerBackend):
    """Plays audio in the requesting user's connected browser tab.

    Routing is per-request: ``list_speakers`` and ``play_uri`` read
    ``get_current_user()`` from the contextvar, so two concurrent
    chats by different users each land in the correct browser. No
    per-user state is held on the backend instance — that would
    violate the multi-user isolation rule.
    """

    backend_name = "browser"
    supports_repeat = False

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="display_name",
                type=ToolParameterType.STRING,
                description=(
                    "Friendly name shown for each user's virtual browser "
                    "speaker. Defaults to ``My Browser``."
                ),
                default="My Browser",
            ),
            ConfigParam(
                key="default_volume",
                type=ToolParameterType.INTEGER,
                description=(
                    "Default playback volume (0-100) when an announce "
                    "request doesn't specify one. The SPA applies this "
                    "as an HTMLAudioElement volume."
                ),
                default=_DEFAULT_VOLUME,
            ),
        ]

    def __init__(self) -> None:
        self._display_name = "My Browser"
        self._default_volume = _DEFAULT_VOLUME
        self._bus: EventBus | None = None
        self._active_connections: dict[str, dict[str, str]] = {}
        # user_id -> {conn_id: display_name_when_registered}
        self._conn_to_user: dict[str, str] = {}
        # conn_id -> user_id (reverse lookup for disconnect)

    # ── Capability injection ────────────────────────────────────────

    def set_event_bus_provider(self, provider: object) -> None:
        """Receive the event bus from SpeakerService.start().

        Typed as ``object`` to match the EventBusAwareSpeakerBackend
        protocol (which can't import EventBusProvider without a layer
        cycle). The runtime check below is the actual contract.
        """
        if isinstance(provider, EventBusProvider):
            self._bus = provider.bus

    # ── Activation tracking ─────────────────────────────────────────

    def activate(self, *, conn_id: str, user_id: str, display_name: str) -> None:
        """Register a connection as an active browser-speaker for a user.

        Idempotent. Calling with the same ``conn_id`` twice is a no-op.
        """
        self._active_connections.setdefault(user_id, {})[conn_id] = display_name
        self._conn_to_user[conn_id] = user_id

    def deactivate(self, *, conn_id: str) -> None:
        """Unregister a connection. No-op if conn_id is unknown."""
        user_id = self._conn_to_user.pop(conn_id, None)
        if user_id is None:
            return
        conns = self._active_connections.get(user_id)
        if conns is None:
            return
        conns.pop(conn_id, None)
        if not conns:
            self._active_connections.pop(user_id, None)

    # ── Lifecycle ───────────────────────────────────────────────────

    async def initialize(self, config: dict[str, object]) -> None:
        name = str(config.get("display_name", "") or "").strip()
        self._display_name = name or "My Browser"
        vol = config.get("default_volume")
        if isinstance(vol, int):
            self._default_volume = max(0, min(100, vol))
        if self._bus is None:
            # Not fatal — the backend still satisfies the ABC and can
            # be listed in the UI; play_uri will raise instead, which
            # surfaces the wiring problem at the point of use.
            logger.warning(
                "BrowserSpeakerBackend started without an event bus — "
                "playback will fail until SpeakerService wires one in."
            )
        logger.info(
            "Browser speaker backend initialized — display=%r default_volume=%d",
            self._display_name,
            self._default_volume,
        )

    async def close(self) -> None:
        self._bus = None

    # ── Discovery ──────────────────────────────────────────────────

    async def list_speakers(self) -> list[SpeakerInfo]:
        """Return one ``SpeakerInfo`` per user with at least one active connection.

        Role-based filtering happens upstream in ``SpeakerService.list_speakers``.
        """
        out: list[SpeakerInfo] = []
        for user_id, conns in self._active_connections.items():
            if not conns:
                continue
            display_name = next(iter(conns.values()))
            out.append(SpeakerInfo(
                speaker_id=user_id,
                name=f"{display_name}'s Browser",
                ip_address="",
            ))
        return out

    async def get_speaker(self, speaker_id: str) -> SpeakerInfo | None:
        """Return the SpeakerInfo for ``speaker_id`` if the user has an active
        registration, or ``None`` otherwise.

        ``speaker_id`` must have the form ``browser:<user_id>`` (as minted by
        ``list_speakers`` and ``SpeakerService.list_speakers``). The caller's
        per-user filtering in ``SpeakerService.list_speakers`` ensures
        non-admin users only see ``browser:<own_user_id>``.
        """
        if not speaker_id.startswith(_SPEAKER_ID_PREFIX):
            return None
        user_id = speaker_id[len(_SPEAKER_ID_PREFIX):]
        conns = self._active_connections.get(user_id)
        if not conns:
            return None
        display_name = next(iter(conns.values()))
        return SpeakerInfo(
            speaker_id=user_id,
            name=f"{display_name}'s Browser",
            ip_address="",
        )

    # ── Playback ───────────────────────────────────────────────────

    async def play_uri(self, request: PlayRequest) -> None:
        """Publish a ``speaker.browser.play`` event for the target user.

        RBAC (admin/SYSTEM can target any browser; regular users can only
        target their own) is enforced at the service layer by
        ``SpeakerService._check_browser_target_permissions`` before this
        backend is reached.  The backend itself does not double-gate.
        """
        if self._bus is None:
            raise RuntimeError(
                "Browser speaker backend has no event bus — was the "
                "speaker service wired correctly?"
            )

        user = get_current_user()
        target_user_id = self._resolve_target_user_id(request, user.user_id)
        if not target_user_id:
            raise RuntimeError(
                "Browser speaker: no target user — play_uri must be "
                "called inside an authenticated request context, and "
                "speaker_ids (if provided) must contain a non-empty user id."
            )

        volume = request.volume if request.volume is not None else self._default_volume
        volume = max(0, min(100, int(volume)))

        await self._bus.publish(
            Event(
                event_type="speaker.browser.play",
                data={
                    "user_id": target_user_id,
                    "conversation_id": get_current_conversation_id() or "",
                    "url": to_browser_url(request.uri),
                    "title": request.title,
                    "volume": volume,
                    "announce": request.announce,
                    "position_seconds": request.position_seconds,
                    "kind": request.kind,
                },
                source="speaker.browser",
            )
        )

    async def stop(self, speaker_ids: list[str] | None = None) -> None:
        if self._bus is None:
            return
        user = get_current_user()
        if not user.user_id or user.user_id == "system":
            return
        # Filter speaker_ids if provided — only stop if our user's
        # browser is in the target set (or no target set was given).
        if speaker_ids:
            our_id = f"{_SPEAKER_ID_PREFIX}{user.user_id}"
            if our_id not in speaker_ids:
                return
        await self._bus.publish(
            Event(
                event_type="speaker.browser.stop",
                data={"user_id": user.user_id},
                source="speaker.browser",
            )
        )

    async def get_volume(self, speaker_id: str) -> int:
        # Browsers don't report a tab's actual audio level back to us
        # cheaply; the SPA applies ``request.volume`` per clip. Return
        # the configured default so the UI has something meaningful.
        return self._default_volume

    async def set_volume(self, speaker_id: str, volume: int) -> None:
        # No-op: volume is per-clip via PlayRequest.volume. The setter
        # is kept to satisfy the ABC and so the announce flow's
        # default-volume logic still works.
        return

    async def get_playback_state(self, speaker_id: str) -> PlaybackState:
        # The backend has no visibility into when a tab finishes
        # playing — that would require a round-trip from the SPA.
        # Returning STOPPED is safe for the announce flow because
        # the speaker service already estimates MP3 duration and
        # sleeps for that long before restoring.
        return PlaybackState.STOPPED

    # ── Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _resolve_target_user_id(request: PlayRequest, caller_user_id: str) -> str:
        """Extract the target user id from PlayRequest.speaker_ids.

        The service-level ``_route_ids`` strips the ``"browser:"`` prefix
        before reaching the backend, so what we see here is the native id
        — which for the browser backend IS the user id.  Falls back to the
        caller when no target was set explicitly.
        """
        for sid in request.speaker_ids or ():
            if sid:
                return sid
        return caller_user_id
