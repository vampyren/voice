from types import SimpleNamespace

import pytest
from jeepney import HeaderFields

from voice import paths
from voice.inject.keys import KeySendError
from voice.inject.portal import PortalKeySender, TokenStore, select_devices_options


def test_token_store_roundtrip(isolated_xdg):
    s = TokenStore()
    assert s.load() is None
    s.save("abc")
    assert s.load() == "abc"
    assert paths.portal_token_file().read_text() == "abc"
    s.clear()
    assert s.load() is None


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


# -- fake D-Bus plumbing for PortalKeySender ------------------------------------
class _Reply:
    def __init__(self, kind="method_return", body=()):
        self.header = SimpleNamespace(message_type=SimpleNamespace(name=kind))
        self.body = body


class _Queue:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    """Minimal stand-in for a jeepney blocking connection.

    `notify_error` makes NotifyKeyboardKeycode answer with an error-type reply
    (jeepney returns those instead of raising); `start_code` is the portal
    Request.Response code for Start (non-zero = the user denied the dialog).
    """

    def __init__(self, notify_error=False, start_code=0):
        self.unique_name = ":1.99"
        self.notify_error, self.start_code = notify_error, start_code
        self.notify_calls: list[tuple[int, int]] = []
        self.closed = False
        self._last_member: str | None = None

    def send_and_get_reply(self, msg):
        member = msg.header.fields.get(HeaderFields.member)
        if member == "AddMatch":
            return _Reply()
        self._last_member = member
        if member == "NotifyKeyboardKeycode":
            self.notify_calls.append((msg.body[2], msg.body[3]))
            return _Reply("error" if self.notify_error else "method_return", ("dead session",))
        return _Reply()

    def filter(self, rule):
        return _Queue()

    def recv_until_filtered(self, queue, timeout=0):
        results = {"CreateSession": {"session_handle": ("o", "/portal/session/1")},
                   "SelectDevices": {}, "Start": {"restore_token": ("s", "tok")}}[self._last_member]
        code = self.start_code if self._last_member == "Start" else 0
        return SimpleNamespace(body=(code, results))

    def close(self):
        self.closed = True


def _factory(conns):
    made = []

    def make(bus="SESSION"):
        conn = conns[len(made)] if len(made) < len(conns) else conns[-1]
        made.append(conn)
        return conn

    return make, made


def test_error_reply_from_notify_fails_the_chord_after_one_reopen(isolated_xdg):
    conns = [FakeConn(notify_error=True), FakeConn(notify_error=True)]
    make, made = _factory(conns)
    sender = PortalKeySender(bus_factory=make)
    with pytest.raises(KeySendError, match="portal keyboard injection failed"):
        sender.send_chord([42])
    assert len(made) == 2                    # opened once, reopened once, then gave up
    assert all(c.closed for c in conns)
    assert sender._session is None


def test_error_reply_recovers_on_the_second_session(isolated_xdg):
    first, second = FakeConn(notify_error=True), FakeConn()
    make, made = _factory([first, second])
    sender = PortalKeySender(bus_factory=make)
    sender.send_chord([42, 47])
    assert len(made) == 2
    assert first.closed and not second.closed
    assert second.notify_calls == [(42, 1), (47, 1), (47, 0), (42, 0)]


def test_denied_start_dialog_clears_the_session(isolated_xdg):
    conn = FakeConn(start_code=1)
    make, made = _factory([conn])
    sender = PortalKeySender(bus_factory=make)
    with pytest.raises(KeySendError, match="denied"):
        sender.send_chord([42])
    assert len(made) == 1                    # a denial is final: no reopen attempt
    assert sender._session is None and sender._conn is None and conn.closed
