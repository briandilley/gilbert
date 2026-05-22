import pytest
from gilbert.interfaces.speaker import SpeakerInfo, SpeakerGroup, split_speaker_id


def test_speaker_info_has_backend_name_default_empty():
    info = SpeakerInfo(speaker_id="x", name="X", ip_address="")
    assert info.backend_name == ""


def test_speaker_info_accepts_backend_name():
    info = SpeakerInfo(speaker_id="sonos:abc", name="Living Room", ip_address="", backend_name="sonos")
    assert info.backend_name == "sonos"


def test_speaker_group_has_backend_name_default_empty():
    g = SpeakerGroup(group_id="g", name="G", coordinator_id="c")
    assert g.backend_name == ""


def test_speaker_group_accepts_backend_name():
    g = SpeakerGroup(group_id="g", name="G", coordinator_id="c", backend_name="sonos")
    assert g.backend_name == "sonos"


def test_split_speaker_id_splits_on_first_colon():
    assert split_speaker_id("sonos:RINCON_AABBCC") == ("sonos", "RINCON_AABBCC")


def test_split_speaker_id_preserves_colons_in_native_id():
    assert split_speaker_id("browser:user:abc:def") == ("browser", "user:abc:def")


def test_split_speaker_id_raises_on_unprefixed():
    with pytest.raises(ValueError, match="must be namespaced"):
        split_speaker_id("RINCON_AABBCC")


def test_split_speaker_id_raises_on_empty():
    with pytest.raises(ValueError):
        split_speaker_id("")
