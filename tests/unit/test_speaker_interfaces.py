from gilbert.interfaces.speaker import SpeakerInfo, SpeakerGroup


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
