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

from gilbert.interfaces.context import get_current_conversation_id, get_current_user
from gilbert.interfaces.configuration import ConfigParam
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

    # ── Capability injection ────────────────────────────────────────

    def set_event_bus_provider(self, provider: object) -> None:
        """Receive the event bus from SpeakerService.start().

        Typed as ``object`` to match the EventBusAwareSpeakerBackend
        protocol (which can't import EventBusProvider without a layer
        cycle). The runtime check below is the actual contract.
        """
        if isinstance(provider, EventBusProvider):
            self._bus = provider.bus

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
        """Return exactly one speaker — the calling user's browser.

        The contextvar identifies who's asking, so different users
        each see their own browser entry. System / unauthenticated
        contexts return an empty list (nothing to address).
        """
        speaker = self._speaker_for_current_user()
        return [speaker] if speaker is not None else []

    async def get_speaker(self, speaker_id: str) -> SpeakerInfo | None:
        speaker = self._speaker_for_current_user()
        if speaker is None or speaker.speaker_id != speaker_id:
            return None
        return speaker

    def _speaker_for_current_user(self) -> SpeakerInfo | None:
        user = get_current_user()
        if not user.user_id or user.user_id == "system":
            return None
        return SpeakerInfo(
            speaker_id=f"{_SPEAKER_ID_PREFIX}{user.user_id}",
            name=self._display_name,
            ip_address="",
            model="browser",
            volume=self._default_volume,
            state=PlaybackState.STOPPED,
        )

    # ── Playback ───────────────────────────────────────────────────

    async def play_uri(self, request: PlayRequest) -> None:
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
                "speaker_ids (if provided) must be ``browser:<user_id>``."
            )

        # Strict per-user scoping: a user can only target their own
        # browser. Admins can be allowed later; for now reject cross-
        # user playback to keep private chats genuinely private.
        if user.user_id and user.user_id != target_user_id:
            raise PermissionError(
                "Browser speaker: a user can only play to their own browser "
                f"(caller={user.user_id!r}, target={target_user_id!r})."
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
        """Extract the target user id from PlayRequest.speaker_ids, or
        fall back to the calling user when no explicit target was set.

        Speaker ids carry the form ``browser:<user_id>``. The SpeakerService
        resolves names to ids before reaching the backend, so any
        ``speaker_ids`` we see here are either the canonical id we
        minted in ``list_speakers`` or junk we should ignore.
        """
        for sid in request.speaker_ids or ():
            if sid.startswith(_SPEAKER_ID_PREFIX):
                return sid[len(_SPEAKER_ID_PREFIX) :]
        return caller_user_id

