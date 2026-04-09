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

# Time to wait for a group to form/settle before verifying
_GROUP_SETTLE_SECONDS = 2.0
_GROUP_VERIFY_RETRIES = 3
_GROUP_RETRY_DELAY = 1.0

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

        coordinator = await self._ensure_speaker_topology(target_ids)

        # Set volume if requested
        if request.volume is not None:
            for sid in target_ids:
                dev = self._devices.get(sid)
                if dev:
                    await asyncio.to_thread(setattr, dev, "volume", request.volume)

        # Play — Spotify URIs need conversion to Sonos format with DIDL metadata
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

    async def _ensure_speaker_topology(self, target_ids: list[str]) -> SoCo:
        """Ensure the target speakers are in the correct topology.

        - Single speaker: unjoin from any existing group so it plays solo.
        - Multiple speakers: form a group with exactly those speakers.

        Returns the coordinator device to use for playback.
        """
        if len(target_ids) == 1:
            device = _find_device(self._devices, target_ids[0])
            # Unjoin if part of a multi-member group
            group = await asyncio.to_thread(lambda: device.group)
            if group and len(group.members) > 1:
                await asyncio.to_thread(device.unjoin)
                await asyncio.sleep(_GROUP_SETTLE_SECONDS)
                logger.info(
                    "Unjoined %s from group for solo playback",
                    device.player_name,
                )
            return device

        group = await self.group_speakers(target_ids)
        coordinator = self._devices.get(group.coordinator_id)
        if coordinator is None:
            raise RuntimeError(
                f"Group coordinator not found: {group.coordinator_id}"
            )
        return coordinator

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

        devices = [_find_device(self._devices, sid) for sid in speaker_ids]

        # Check if they're already grouped together
        if await self._already_grouped(devices):
            group = devices[0].group
            logger.info("Speakers already grouped as '%s' — skipping regroup", group.label)
            return SpeakerGroup(
                group_id=group.uid,
                name=group.label,
                coordinator_id=_speaker_id(group.coordinator),
                member_ids=[_speaker_id(m) for m in group.members],
            )

        # Use the first device as the coordinator
        coordinator = devices[0]

        # Only unjoin devices that are in the target set — don't disrupt other groups
        await asyncio.to_thread(self._unjoin_target_devices, devices)

        # Join the rest to the coordinator
        for device in devices[1:]:
            await asyncio.to_thread(device.join, coordinator)

        # Wait for the group to settle
        await asyncio.sleep(_GROUP_SETTLE_SECONDS)

        # Verify the group formed
        group = await self._verify_group(coordinator, speaker_ids)
        logger.info("Speaker group formed: '%s' with %d members", group.name, len(group.member_ids))
        return group

    async def ungroup_speakers(self, speaker_ids: list[str]) -> None:
        for sid in speaker_ids:
            device = self._devices.get(sid)
            if device:
                await asyncio.to_thread(device.unjoin)
        logger.info("Ungrouped %d speakers", len(speaker_ids))

    # --- Private helpers ---

    @staticmethod
    def _unjoin_target_devices(devices: list[SoCo]) -> None:
        """Unjoin only the target devices from their current groups.

        Does NOT touch other speakers that aren't in our target set.
        """
        for device in devices:
            group = device.group
            if group and len(group.members) > 1:
                # Only unjoin if this device is part of a group
                device.unjoin()

    async def _already_grouped(self, devices: list[SoCo]) -> bool:
        """Check if all devices are already in the same group with exactly these members."""
        def check() -> bool:
            if not devices:
                return False
            first_group = devices[0].group
            if first_group is None:
                return False
            target_uids = {d.uid for d in devices}
            group_uids = {m.uid for m in first_group.members}
            return target_uids == group_uids

        return await asyncio.to_thread(check)

    async def _verify_group(self, coordinator: SoCo, expected_ids: list[str]) -> SpeakerGroup:
        """Verify the group formed correctly, retrying if needed."""
        expected = set(expected_ids)

        for attempt in range(_GROUP_VERIFY_RETRIES):
            await self._discover()
            coord = self._devices.get(_speaker_id(coordinator))
            if coord is None:
                coord = coordinator

            group = await asyncio.to_thread(lambda: coord.group)
            if group is not None:
                actual = {_speaker_id(m) for m in group.members}
                if expected.issubset(actual):
                    return SpeakerGroup(
                        group_id=group.uid,
                        name=group.label,
                        coordinator_id=_speaker_id(group.coordinator),
                        member_ids=[_speaker_id(m) for m in group.members],
                    )

            if attempt < _GROUP_VERIFY_RETRIES - 1:
                logger.debug(
                    "Group not yet formed (attempt %d/%d), retrying...",
                    attempt + 1, _GROUP_VERIFY_RETRIES,
                )
                await asyncio.sleep(_GROUP_RETRY_DELAY)

        # Return best-effort group info even if verification is incomplete
        group = await asyncio.to_thread(lambda: coord.group)
        if group:
            logger.warning("Group may not have fully formed — proceeding anyway")
            return SpeakerGroup(
                group_id=group.uid,
                name=group.label,
                coordinator_id=_speaker_id(group.coordinator),
                member_ids=[_speaker_id(m) for m in group.members],
            )

        raise RuntimeError("Failed to form speaker group after retries")
