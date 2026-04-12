"""Tests for the Sonos speaker backend — focused on the tricky bits."""

from types import SimpleNamespace
from typing import Any

from gilbert.integrations.sonos_speaker import _speaker_info
from gilbert.interfaces.speaker import PlaybackState


def _make_device(
    *,
    uid: str = "RINCON_AAA",
    player_name: str = "Kitchen",
    ip_address: str = "192.168.1.10",
    group: Any = None,
    transport_state: str = "STOPPED",
    model: str = "Sonos One",
    volume: int = 30,
) -> Any:
    """Build a SoCo-shaped mock device with the attributes _speaker_info reads."""
    device = SimpleNamespace()
    device.uid = uid
    device.player_name = player_name
    device.ip_address = ip_address
    device.group = group
    device.volume = volume
    device.get_current_transport_info = lambda: {
        "current_transport_state": transport_state,
    }
    device.get_speaker_info = lambda: {"model_name": model}
    return device


def _make_group(
    uid: str = "RINCON_GRP",
    label: str = "Living Zone",
    coordinator: Any = None,
) -> Any:
    group = SimpleNamespace()
    group.uid = uid
    group.label = label
    group.coordinator = coordinator
    return group


def test_speaker_info_with_normal_group() -> None:
    """Standard case: device is in a group with a valid coordinator."""
    coordinator = _make_device(uid="RINCON_CCC", player_name="Living Room")
    group = _make_group(coordinator=coordinator)

    device = _make_device(uid="RINCON_AAA", player_name="Kitchen", group=group)
    info = _speaker_info(device)

    assert info.speaker_id == "RINCON_AAA"
    assert info.name == "Kitchen"
    assert info.is_group_coordinator is False
    assert info.group_id == "RINCON_GRP"
    assert info.group_name == "Living Zone"
    assert info.state == PlaybackState.STOPPED


def test_speaker_info_coordinator_is_self() -> None:
    """Coordinator is the device itself — is_group_coordinator is True."""
    device = _make_device(uid="RINCON_XYZ", player_name="Office")
    group = _make_group(coordinator=device)
    device.group = group

    info = _speaker_info(device)
    assert info.is_group_coordinator is True


def test_speaker_info_with_no_group() -> None:
    """Standalone speaker (no group) — falls back to self as coordinator."""
    device = _make_device(uid="RINCON_AAA", player_name="Kitchen", group=None)
    info = _speaker_info(device)

    assert info.group_id == ""
    assert info.group_name == ""
    assert info.is_group_coordinator is True


def test_speaker_info_with_none_coordinator_does_not_crash() -> None:
    """Regression: during Sonos topology changes (e.g. just after an
    unjoin), ``group.coordinator`` can be transiently None. Before the
    fix this raised AttributeError on ``None.uid`` — now it gracefully
    falls back to the device itself as its own coordinator."""
    device = _make_device(uid="RINCON_AAA", player_name="Bedroom")
    group = _make_group(coordinator=None)  # transient None
    device.group = group

    # Previously: AttributeError: 'NoneType' object has no attribute 'uid'
    info = _speaker_info(device)

    # Fallback: treat the device as its own coordinator
    assert info.is_group_coordinator is True
    # Group metadata from the group object is still surfaced
    assert info.group_id == "RINCON_GRP"
    assert info.group_name == "Living Zone"
