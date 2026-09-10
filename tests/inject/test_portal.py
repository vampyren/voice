import pytest

from voice import paths
from voice.inject.portal import TokenStore, request_path, select_devices_options


def test_token_store_roundtrip(isolated_xdg):
    s = TokenStore()
    assert s.load() is None
    s.save("abc")
    assert s.load() == "abc"
    assert paths.portal_token_file().read_text() == "abc"
    s.clear()
    assert s.load() is None


def test_request_path_escapes_unique_name():
    assert request_path(":1.42", "tok1") == "/org/freedesktop/portal/desktop/request/1_42/tok1"


def test_select_devices_options_include_token_only_when_present():
    opts = select_devices_options(None)
    assert opts["types"] == ("u", 1) and opts["persist_mode"] == ("u", 2) and "restore_token" not in opts
    assert select_devices_options("t")["restore_token"] == ("s", "t")


@pytest.mark.boundary
def test_real_portal_sends_harmless_chord():
    """Sends Shift press/release through the portal. First run shows the desktop's permission dialog."""
    from evdev import ecodes as e
    from voice.inject.portal import PortalKeySender, portal_available
    assert portal_available()
    sender = PortalKeySender()
    sender.send_chord([e.KEY_LEFTSHIFT])
    assert TokenStore().load()          # restore token persisted after Start
