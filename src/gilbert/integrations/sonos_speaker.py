"""Sonos speaker backend — speaker control via the SoCo library."""

import asyncio
import logging
from typing import Any

import soco
from soco import SoCo

from gilbert.interfaces.speaker import (
    PlaybackState,
    PlayRequest,
    SpeakerBackend,
    SpeakerGroup,
    SpeakerInfo,
)

logger = logging.getLogger(__name__)

# Grouping timing constants
_GROUP_SETTLE_SECONDS = 2.0
_GROUP_POLL_INTERVAL = 1.0
_GROUP_POLL_TIMEOUT = 5.0  # seconds to poll before retrying group command
_GROUP_MAX_ATTEMPTS = 5  # max times to retry the whole group operation

# Map SoCo transport states to our enum
_STATE_MAP: dict[str, PlaybackState] = {
    "PLAYING": PlaybackState.PLAYING,
    "PAUSED_PLAYBACK": PlaybackState.PAUSED,
    "STOPPED": PlaybackState.STOPPED,
    "TRANSITIONING": PlaybackState.TRANSITIONING,
}


def _speaker_id(device: SoCo) -> str:
    """Canonical speaker ID — use the UID which is stable."""
    return device.uid


def _speaker_info(device: SoCo) -> SpeakerInfo:
    """Build a SpeakerInfo from a SoCo device."""
    group = device.group
    coordinator = group.coordinator if group else device
    transport = device.get_current_transport_info()
    state_str = transport.get("current_transport_state", "STOPPED")

    return SpeakerInfo(
        speaker_id=_speaker_id(device),
        name=device.player_name,
        ip_address=device.ip_address,
        model=device.get_speaker_info().get("model_name", ""),
        group_id=group.uid if group else "",
        group_name=group.label if group else "",
        is_group_coordinator=(device.uid == coordinator.uid),
        volume=device.volume,
        state=_STATE_MAP.get(state_str, PlaybackState.STOPPED),
    )


def _find_device(devices: dict[str, SoCo], speaker_id: str) -> SoCo:
    """Find a device by speaker_id. Raises KeyError if not found."""
    device = devices.get(speaker_id)
    if device is None:
        raise KeyError(f"Speaker not found: {speaker_id}")
    return device


def _to_sonos_spotify_uri(spotify_uri: str) -> str:
    """Convert a ``spotify:track:ID`` URI to Sonos ``x-sonos-spotify:`` format.

    Sonos cannot play raw ``spotify:`` URIs via ``play_uri``. Wrapping in
    ``x-sonos-spotify:`` with the Spotify service ID lets Sonos resolve
    the track through its own Spotify account.
    """
    from soco.music_services import MusicService

    sid = MusicService("Spotify").service_id
    encoded = spotify_uri.replace(":", "%3a")
    return f"x-sonos-spotify:{encoded}?sid={sid}&flags=8224&sn=1"


class SonosSpeaker(SpeakerBackend):
    """Sonos speaker backend using the SoCo library."""

    backend_name = "sonos"

    def __init__(self) -> None:
        self._devices: dict[str, SoCo] = {}

    async def initialize(self, config: dict[str, object]) -> None:
        await self._discover()
        logger.info("Sonos backend initialized — %d speakers found", len(self._devices))

    async def close(self) -> None:
        self._devices.clear()

    # --- Discovery ---

    async def _discover(self) -> None:
        """Discover Sonos speakers on the network."""
        devices = await asyncio.to_thread(soco.discover)
        self._devices = {}
        if devices:
            for device in devices:
                self._devices[_speaker_id(device)] = device

    async def list_speakers(self) -> list[SpeakerInfo]:
        await self._discover()
        return await asyncio.to_thread(self._list_speakers_sync)

    def _list_speakers_sync(self) -> list[SpeakerInfo]:
        return [_speaker_info(d) for d in self._devices.values()]

    async def get_speaker(self, speaker_id: str) -> SpeakerInfo | None:
        device = self._devices.get(speaker_id)
        if device is None:
            await self._discover()
            device = self._devices.get(speaker_id)
        if device is None:
            return None
        return await asyncio.to_thread(_speaker_info, device)

    # --- Playback ---

    async def play_uri(self, request: PlayRequest) -> None:
        target_ids = request.speaker_ids or list(self._devices.keys())
        if not target_ids:
            raise ValueError("No speakers available")

        # Find the coordinator — after topology is prepared by the speaker
        # service, the first target is standalone or the group coordinator.
        coordinator = await self._find_coordinator(target_ids)

        # Set volume if requested
        if request.volume is not None:
            for sid in target_ids:
                dev = self._devices.get(sid)
                if dev:
                    await asyncio.to_thread(setattr, dev, "volume", request.volume)

        # Play — Spotify URIs need conversion to Sonos format
        title = request.title or ""
        uri = request.uri

        if uri.startswith("spotify:"):
            uri = await asyncio.to_thread(_to_sonos_spotify_uri, uri)
        await asyncio.to_thread(coordinator.play_uri, uri, title=title)

        # Seek to position if requested
        if request.position_seconds is not None and request.position_seconds > 0:
            pos = int(request.position_seconds)
            timestamp = f"{pos // 3600}:{(pos % 3600) // 60:02d}:{pos % 60:02d}"
            await asyncio.sleep(0.3)  # brief pause for transport to start
            await asyncio.to_thread(coordinator.seek, timestamp)

        logger.info("Playing %s on %s", request.uri, coordinator.player_name)

    async def _find_coordinator(self, target_ids: list[str]) -> SoCo:
        """Find the group coordinator for the given speakers.

        If the speakers are grouped, returns the coordinator. If a single
        standalone speaker, returns that device.
        """
        device = _find_device(self._devices, target_ids[0])
        group = await asyncio.to_thread(lambda: device.group)
        return group.coordinator if group else device

    async def clear_queue(self, speaker_ids: list[str] | None = None) -> None:
        """Clear the playback queue on the specified speakers."""
        targets = speaker_ids or list(self._devices.keys())
        for sid in targets:
            device = self._devices.get(sid)
            if device:
                try:
                    await asyncio.to_thread(device.clear_queue)
                except Exception:
                    logger.debug("Failed to clear queue on %s", sid)

    async def stop(self, speaker_ids: list[str] | None = None) -> None:
        targets = speaker_ids or list(self._devices.keys())
        for sid in targets:
            device = self._devices.get(sid)
            if device:
                await asyncio.to_thread(device.stop)

    # --- Volume ---

    async def get_playback_state(self, speaker_id: str) -> PlaybackState:
        device = _find_device(self._devices, speaker_id)
        transport = await asyncio.to_thread(device.get_current_transport_info)
        state_str = transport.get("current_transport_state", "STOPPED")
        return _STATE_MAP.get(state_str, PlaybackState.STOPPED)

    async def get_volume(self, speaker_id: str) -> int:
        device = _find_device(self._devices, speaker_id)
        return await asyncio.to_thread(lambda: device.volume)

    async def set_volume(self, speaker_id: str, volume: int) -> None:
        device = _find_device(self._devices, speaker_id)
        clamped = max(0, min(100, volume))
        await asyncio.to_thread(setattr, device, "volume", clamped)

    # --- Grouping ---

    @property
    def supports_grouping(self) -> bool:
        return True

    async def list_groups(self) -> list[SpeakerGroup]:
        await self._discover()
        return await asyncio.to_thread(self._list_groups_sync)

    def _list_groups_sync(self) -> list[SpeakerGroup]:
        seen_group_ids: set[str] = set()
        groups: list[SpeakerGroup] = []
        for device in self._devices.values():
            group = device.group
            if group is None or group.uid in seen_group_ids:
                continue
            seen_group_ids.add(group.uid)
            groups.append(SpeakerGroup(
                group_id=group.uid,
                name=group.label,
                coordinator_id=_speaker_id(group.coordinator),
                member_ids=[_speaker_id(m) for m in group.members],
            ))
        return groups

    async def group_speakers(self, speaker_ids: list[str]) -> SpeakerGroup:
        if len(speaker_ids) < 2:
            raise ValueError("Need at least 2 speakers to form a group")

        target_set = set(speaker_ids)

        # Check if already correctly grouped
        result = await self._check_group_state(speaker_ids)
        if result:
            logger.info(
                "Speakers already grouped as '%s' — no changes needed",
                result.name,
            )
            return result

        # Retry loop: attempt to form the group, poll until formed,
        # re-attempt if it doesn't converge within the poll timeout
        for attempt in range(1, _GROUP_MAX_ATTEMPTS + 1):
            await self._apply_group_changes(speaker_ids, target_set)

            # Poll until the group is formed or timeout
            result = await self._poll_until_grouped(
                speaker_ids, _GROUP_POLL_TIMEOUT,
            )
            if result:
                logger.info(
                    "Speaker group formed: '%s' with %d members",
                    result.name, len(result.member_ids),
                )
                return result

            logger.warning(
                "Group not formed after attempt %d/%d — retrying",
                attempt, _GROUP_MAX_ATTEMPTS,
            )

        raise RuntimeError(
            f"Failed to form speaker group after {_GROUP_MAX_ATTEMPTS} "
            f"attempts with {len(speaker_ids)} speakers"
        )

    async def ungroup_speakers(self, speaker_ids: list[str]) -> None:
        changed = False
        for sid in speaker_ids:
            device = self._devices.get(sid)
            if not device:
                continue
            group = await asyncio.to_thread(lambda d=device: d.group)
            if group and len(group.members) > 1:
                await asyncio.to_thread(device.unjoin)
                changed = True
        if changed:
            await asyncio.sleep(_GROUP_SETTLE_SECONDS)
            logger.info("Ungrouped %d speakers", len(speaker_ids))

    # --- Private grouping helpers ---

    async def _check_group_state(
        self, speaker_ids: list[str],
    ) -> SpeakerGroup | None:
        """Check if the target speakers are already in the correct group.

        Returns the SpeakerGroup if all target speakers are in the same
        group with no extra members. Returns None otherwise.
        """
        target_set = set(speaker_ids)

        def check() -> SpeakerGroup | None:
            first = self._devices.get(speaker_ids[0])
            if first is None:
                return None
            group = first.group
            if group is None:
                return None
            group_uids = {_speaker_id(m) for m in group.members}
            if group_uids == target_set:
                return SpeakerGroup(
                    group_id=group.uid,
                    name=group.label,
                    coordinator_id=_speaker_id(group.coordinator),
                    member_ids=[_speaker_id(m) for m in group.members],
                )
            return None

        return await asyncio.to_thread(check)

    async def _apply_group_changes(
        self, speaker_ids: list[str], target_set: set[str],
    ) -> None:
        """Apply the minimal changes to form the desired group.

        Figures out which speakers need to be unjoined from other groups
        and which need to join the coordinator. Avoids touching speakers
        that are already correct.
        """
        coordinator = _find_device(self._devices, speaker_ids[0])

        def compute_changes() -> tuple[list[SoCo], list[SoCo]]:
            """Returns (to_unjoin, to_join) lists."""
            to_unjoin: list[SoCo] = []
            to_join: list[SoCo] = []
            coord_group = coordinator.group
            coord_group_uids = (
                {_speaker_id(m) for m in coord_group.members}
                if coord_group else {_speaker_id(coordinator)}
            )

            for sid in speaker_ids:
                device = self._devices.get(sid)
                if device is None:
                    continue
                if device is coordinator:
                    # Coordinator: unjoin if it's in a group with
                    # non-target members
                    if coord_group and len(coord_group.members) > 1:
                        extras = coord_group_uids - target_set
                        if extras:
                            # Unjoin the extras, not the coordinator
                            for m in coord_group.members:
                                mid = _speaker_id(m)
                                if mid in extras:
                                    to_unjoin.append(m)
                    continue
                # Non-coordinator: check if already in coordinator's group
                if _speaker_id(device) in coord_group_uids:
                    continue
                # Needs to leave its current group and join ours
                dev_group = device.group
                if dev_group and len(dev_group.members) > 1:
                    to_unjoin.append(device)
                to_join.append(device)

            return to_unjoin, to_join

        to_unjoin, to_join = await asyncio.to_thread(compute_changes)

        # Unjoin speakers that are in wrong groups
        if to_unjoin:
            await asyncio.gather(*(
                asyncio.to_thread(d.unjoin) for d in to_unjoin
            ))
            await asyncio.sleep(_GROUP_SETTLE_SECONDS)
            logger.debug("Unjoined %d speakers from other groups", len(to_unjoin))

        # Join speakers to the coordinator
        if to_join:
            await asyncio.gather(*(
                asyncio.to_thread(d.join, coordinator) for d in to_join
            ))
            await asyncio.sleep(_GROUP_SETTLE_SECONDS)
            logger.debug("Joined %d speakers to coordinator", len(to_join))

    async def _poll_until_grouped(
        self, speaker_ids: list[str], timeout: float,
    ) -> SpeakerGroup | None:
        """Poll until the target speakers are in the correct group.

        Returns the SpeakerGroup if formed within timeout, None otherwise.
        """
        elapsed = 0.0
        while elapsed < timeout:
            await self._discover()
            result = await self._check_group_state(speaker_ids)
            if result:
                return result
            await asyncio.sleep(_GROUP_POLL_INTERVAL)
            elapsed += _GROUP_POLL_INTERVAL
        return None
