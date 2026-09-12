"""Asking KWin which window has the keyboard, through a script it runs itself.

The exchange is driven by a fake connection here. A real one needs Plasma, and
that test is marked `boundary` at the bottom.
"""
import pytest
from jeepney import DBusAddress, HeaderFields, Message, MessageType, new_method_call

from voice.inject.kwin import (CALLBACK_INTERFACE, CALLBACK_METHOD, KWinScriptError,
                               active_window_class, script_source)


def _callback(body, member=CALLBACK_METHOD):
    """A message shaped like the script's `callDBus` arriving on our connection."""
    call = new_method_call(
        DBusAddress("/", bus_name=":1.999", interface=CALLBACK_INTERFACE), member, "s", body)
    call.header.message_type = MessageType.method_call
    return call


def _reply(body=()):
    message = Message.__new__(Message)
    message.header = type("H", (), {"message_type": MessageType.method_return,
                                    "fields": {}})()
    message.body = body
    return message


class FakeConn:
    """Answers the calls `active_window_class` makes, in order."""

    unique_name = ":1.123"

    def __init__(self, load_id=0, incoming=None, fail=None):
        self.calls, self.sent, self._incoming = [], [], list(incoming or [])
        self._load_id, self._fail = load_id, fail or {}

    def send_and_get_reply(self, message, timeout=None):
        member = message.header.fields[HeaderFields.member]
        self.calls.append(member)
        if member in self._fail:
            raise self._fail[member]
        return _reply((self._load_id,) if member == "loadScript" else ())

    def receive(self, timeout=None):
        if not self._incoming:
            raise TimeoutError
        return self._incoming.pop(0)

    def send(self, message):
        self.sent.append(message)


def test_the_script_addresses_its_answer_back_to_this_connection():
    source = script_source(":1.123")
    assert 'callDBus(":1.123", "/"' in source
    assert "workspace.activeWindow" in source
    assert CALLBACK_METHOD in source and CALLBACK_INTERFACE in source


def test_the_script_never_asks_for_a_window_to_be_clicked():
    """The whole point: `queryWindowInfo` is a picker, this is a query."""
    assert "queryWindowInfo" not in script_source(":1.1")


def test_the_focused_window_comes_back():
    conn = FakeConn(incoming=[_callback(("org.kde.konsole",))])
    assert active_window_class(conn) == "org.kde.konsole"


def test_the_script_is_loaded_run_and_unloaded():
    conn = FakeConn(incoming=[_callback(("org.kde.konsole",))])
    active_window_class(conn)
    assert conn.calls == ["loadScript", "run", "unloadScript"], conn.calls


def test_the_script_is_unloaded_even_when_it_never_answers():
    """A script left loaded holds a name in KWin for the rest of its run."""
    conn = FakeConn(incoming=[])
    assert active_window_class(conn, timeout=0.01) is None
    assert "unloadScript" in conn.calls


def test_nothing_focused_is_an_answer_not_a_failure():
    """An empty class means "nobody has the keyboard" - a fact, not a timeout.

    The caller treats None as "this desktop will not say" and stops asking; it
    must not reach that state because a window was merely unfocused.
    """
    conn = FakeConn(incoming=[_callback(("",))])
    assert active_window_class(conn) == ""


def test_kwin_refusing_to_load_is_not_an_exception_to_the_caller():
    conn = FakeConn(fail={"loadScript": KWinScriptError("no such interface")})
    assert active_window_class(conn) is None


def test_the_callback_is_answered_so_kwin_is_not_left_waiting():
    conn = FakeConn(incoming=[_callback(("org.kde.konsole",))])
    active_window_class(conn)
    assert conn.sent, "the script's call was never replied to"
    assert conn.sent[0].header.message_type is MessageType.method_return


def test_someone_elses_call_is_refused_rather_than_taken_as_the_answer():
    conn = FakeConn(incoming=[_callback(("nonsense",), member="SomethingElse"),
                              _callback(("org.kde.konsole",))])
    assert active_window_class(conn) == "org.kde.konsole"
    assert conn.sent[0].header.message_type is MessageType.error


@pytest.mark.boundary
def test_against_a_real_kwin():
    """Needs a live Plasma session. `pytest -m boundary` on a KDE machine."""
    from jeepney.io.blocking import open_dbus_connection

    conn = open_dbus_connection(bus="SESSION")
    try:
        found = active_window_class(conn)
    finally:
        conn.close()
    assert found is not None, "KWin did not answer; is this a Plasma session?"
    assert found == "" or "." in found or found.isalnum(), found


# -- the long-lived reader ---------------------------------------------------

def test_the_reader_opens_one_connection_and_keeps_it():
    from voice.inject.kwin import KWinWindowReader

    opened = []

    def connect():
        conn = FakeConn(incoming=[_callback(("org.kde.konsole",))])
        opened.append(conn)
        return conn

    reader = KWinWindowReader(connect=connect)
    assert reader() == "org.kde.konsole"
    opened[0]._incoming = [_callback(("firefox",))]
    assert reader() == "firefox"
    assert len(opened) == 1, "a connection was opened per call"


def test_a_bus_that_will_not_open_is_not_an_error_to_the_caller():
    from voice.inject.kwin import KWinWindowReader

    def refuse():
        raise OSError("no session bus here")

    assert KWinWindowReader(connect=refuse)() is None


def test_a_connection_that_dies_is_replaced_rather_than_kept():
    from voice.inject.kwin import KWinWindowReader

    class Dead(FakeConn):
        def send_and_get_reply(self, message, timeout=None):
            raise ConnectionResetError("the bus went away")

    conns = [Dead(), FakeConn(incoming=[_callback(("org.kde.konsole",))])]
    reader = KWinWindowReader(connect=lambda: conns.pop(0))

    assert reader() is None, "a dead connection has no answer"
    assert reader() == "org.kde.konsole", "the next call must open a fresh one"


def test_unrelated_traffic_cannot_hold_the_paste_open():
    """The budget is a deadline, not a fresh allowance per message.

    Anything else calling this connection would otherwise reset the timeout
    every time, and the paste would wait behind it.
    """
    import time

    noise = [_callback((str(n),), member="Noise") for n in range(500)]
    conn = FakeConn(incoming=noise)
    started = time.monotonic()

    assert active_window_class(conn, timeout=0.05) is None
    assert time.monotonic() - started < 1.0, "the deadline did not hold"
