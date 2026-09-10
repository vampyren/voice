import threading
import time
from collections import deque
from types import SimpleNamespace

import pytest
from jeepney import HeaderFields, new_signal

from voice import APP_ID
from voice.hotkey import portal_listener
from voice.hotkey.portal_listener import NO_CAPTURE_MESSAGE, SHORTCUTS, PortalListener

SESSION = "/org/freedesktop/portal/desktop/session/1_99/voice1"
OTHER_SESSION = "/org/freedesktop/portal/desktop/session/1_99/other"
WITH_REGISTRY = ('<node><interface name="org.freedesktop.host.portal.Registry">'
                 '<method name="Register"/></interface></node>')
WITHOUT_REGISTRY = '<node><interface name="org.freedesktop.portal.GlobalShortcuts"/></node>'


def wait_for(pred, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.005)
    return False


class _Reply:
    def __init__(self, kind="method_return", body=()):
        self.header = SimpleNamespace(message_type=SimpleNamespace(name=kind))
        self.body = body


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    """A jeepney blocking connection just real enough for PortalListener.

    CreateSession/BindShortcuts answer through the Request.Response path (a
    `recv_until_filtered` right after the call); everything else answers
    directly. `emit()` queues a GlobalShortcuts signal for the listener thread.
    """

    def __init__(self, *, bind_code=0, create_code=0, version=1, registry=True, configure_error=False,
                 create_error=None):
        self.unique_name = ":1.99"
        self.bind_code, self.create_code = bind_code, create_code
        self.version, self.registry, self.configure_error = version, registry, configure_error
        self.create_error = create_error
        self.calls: list[tuple[str, tuple]] = []
        self.closed = False
        self._pending: str | None = None
        self._signals: deque = deque()
        self._lock = threading.Lock()

    # -- test helpers ----------------------------------------------------
    def emit(self, member: str, shortcut_id: str = "dictate", session: str = SESSION) -> None:
        with self._lock:
            self._signals.append(new_signal(SHORTCUTS, member, "osta{sv}", (session, shortcut_id, 12345, {})))

    def bodies(self, member: str) -> list[tuple]:
        with self._lock:
            return [body for name, body in self.calls if name == member]

    # -- connection ------------------------------------------------------
    def send_and_get_reply(self, msg, timeout=None):
        member = msg.header.fields.get(HeaderFields.member)
        with self._lock:
            self.calls.append((member, msg.body))
        if member == "Introspect":
            return _Reply(body=(WITH_REGISTRY if self.registry else WITHOUT_REGISTRY,))
        if member == "Get":
            return _Reply(body=(("u", self.version),))
        if member == "ConfigureShortcuts":
            return _Reply("error" if self.configure_error else "method_return", ("/req/cfg",))
        if member == "CreateSession" and self.create_error:
            return _Reply("error", (self.create_error,))
        if member in ("CreateSession", "BindShortcuts"):
            self._pending = member
            return _Reply(body=("/req/1",))
        return _Reply()

    def filter(self, rule, **kwargs):
        return _Ctx()

    def recv_until_filtered(self, queue, timeout=None):
        if self._pending is not None:
            member, self._pending = self._pending, None
            if member == "CreateSession":
                return SimpleNamespace(body=(self.create_code, {"session_handle": ("o", SESSION)}))
            return SimpleNamespace(body=(self.bind_code, {}))
        with self._lock:
            if self._signals:
                return self._signals.popleft()
        time.sleep(0.005)               # the real connection blocks until the timeout
        raise TimeoutError

    def close(self):
        self.closed = True


def make(shortcuts=None, on_event=None, **kwargs):
    conn = FakeConn(**kwargs)
    events: list[tuple[str, str]] = []
    listener = PortalListener(on_event or (lambda name, kind: events.append((name, kind))),
                              shortcuts if shortcuts is not None else {"dictate": "CTRL+space"},
                              bus_factory=lambda bus="SESSION": conn)
    return listener, conn, events


# -- binding -------------------------------------------------------------------
def test_bind_shortcuts_sends_every_configured_id_with_its_trigger():
    listener, conn, _ = make({"dictate": "CTRL+space", "cancel": "CTRL+ALT+c"})
    try:
        listener.start()
        session, shortcuts, parent, options = conn.bodies("BindShortcuts")[0]
        assert session == SESSION
        assert parent == ""                                  # no parent window: the daemon has none
        assert "handle_token" in options
        assert [sid for sid, _ in shortcuts] == ["dictate", "cancel"]
        opts = dict(shortcuts)
        assert opts["dictate"]["preferred_trigger"] == ("s", "CTRL+space")
        assert opts["cancel"]["preferred_trigger"] == ("s", "CTRL+ALT+c")
        assert opts["dictate"]["description"][1] and opts["cancel"]["description"][1]
        assert listener.devices_ok() is True
    finally:
        listener.stop()


def test_create_session_asks_for_its_own_handle_tokens():
    listener, conn, _ = make()
    try:
        listener.start()
        options = conn.bodies("CreateSession")[0][0]
        assert set(options) == {"handle_token", "session_handle_token"}
    finally:
        listener.stop()


def test_devices_ok_is_unknown_before_start():
    listener, _, _ = make()
    assert listener.devices_ok() is None


def test_denied_binding_leaves_the_backend_unusable():
    listener, conn, events = make(bind_code=1)
    listener.start()
    try:
        assert listener.devices_ok() is False
        assert conn.closed                                   # nothing left half-open
        conn.emit("Activated")
        assert not wait_for(lambda: events, timeout=0.2)
    finally:
        listener.stop()


def test_a_dead_bus_leaves_the_backend_unusable_without_raising():
    def boom(bus="SESSION"):
        raise ConnectionError("no session bus")

    listener = PortalListener(lambda name, kind: None, {"dictate": "CTRL+space"}, bus_factory=boom)
    listener.start()
    assert listener.devices_ok() is False
    listener.stop()


def test_a_missing_app_id_is_reported_with_the_fix(caplog):
    listener, conn, _ = make(create_error="An app id is required")
    listener.start()
    try:
        assert listener.devices_ok() is False
        assert "install.sh" in caplog.text and f"{APP_ID}.desktop" in caplog.text
    finally:
        listener.stop()


# -- signals -------------------------------------------------------------------
def test_activated_then_deactivated_become_press_and_release():
    listener, conn, events = make()
    try:
        listener.start()
        conn.emit("Activated")
        assert wait_for(lambda: events == [("dictate", "press")])
        assert listener.held() == frozenset({"dictate"})
        conn.emit("Deactivated")
        assert wait_for(lambda: events == [("dictate", "press"), ("dictate", "release")])
        assert listener.held() == frozenset()
    finally:
        listener.stop()


def test_signals_from_another_session_or_an_unbound_id_are_ignored():
    listener, conn, events = make()
    try:
        listener.start()
        conn.emit("Activated", session=OTHER_SESSION)
        conn.emit("Activated", shortcut_id="somebody-elses")
        conn.emit("Activated")                               # the real one, queued last
        assert wait_for(lambda: events == [("dictate", "press")])
        assert events == [("dictate", "press")]
    finally:
        listener.stop()


def test_a_failing_callback_does_not_kill_the_listener():
    seen: list[tuple[str, str]] = []

    def on_event(name, kind):
        seen.append((name, kind))
        if len(seen) == 1:
            raise RuntimeError("handler boom")

    listener, conn, _ = make(on_event=on_event)
    try:
        listener.start()
        conn.emit("Activated")
        conn.emit("Deactivated")
        assert wait_for(lambda: len(seen) == 2)
    finally:
        listener.stop()


def test_modifiers_held_is_always_false():
    listener, _, _ = make()
    assert listener.modifiers_held() is False


def test_stop_joins_the_thread_and_closes_the_connection():
    listener, conn, _ = make()
    listener.start()
    listener.stop()
    assert conn.closed
    assert not any(t.name == "portal-listener" for t in threading.enumerate())


# -- app id --------------------------------------------------------------------
def test_app_id_is_registered_when_the_host_registry_exists():
    listener, conn, _ = make()
    try:
        listener.start()
        assert conn.bodies("Register") == [(APP_ID, {})]
    finally:
        listener.stop()


def test_registration_is_skipped_when_the_host_registry_is_absent():
    listener, conn, _ = make(registry=False)
    try:
        listener.start()
        assert conn.bodies("Register") == []
        assert listener.devices_ok() is True                  # absent registry is not an error
    finally:
        listener.stop()


# -- capture -------------------------------------------------------------------
def test_capture_next_explains_itself_when_the_portal_is_too_old():
    listener, conn, _ = make(version=1)
    try:
        listener.start()
        got: list[str] = []
        listener.capture_next(got.append)
        assert got == [NO_CAPTURE_MESSAGE]
        assert conn.bodies("ConfigureShortcuts") == []
    finally:
        listener.stop()


def test_capture_next_opens_the_desktops_dialog_when_supported():
    listener, conn, _ = make(version=2)
    try:
        listener.start()
        got: list[str] = []
        listener.capture_next(got.append)
        session, parent, options = conn.bodies("ConfigureShortcuts")[0]
        assert (session, parent) == (SESSION, "") and "handle_token" in options
        assert got and got[0] != NO_CAPTURE_MESSAGE and got[0].startswith("portal:")
    finally:
        listener.stop()


def test_capture_next_falls_back_when_configure_shortcuts_errors():
    listener, conn, _ = make(version=2, configure_error=True)
    try:
        listener.start()
        got: list[str] = []
        listener.capture_next(got.append)
        assert got == [NO_CAPTURE_MESSAGE]
    finally:
        listener.stop()


def test_capture_next_without_a_session_still_answers():
    listener, _, _ = make()
    got: list[str] = []
    listener.capture_next(got.append)                        # never started
    assert got == [NO_CAPTURE_MESSAGE]


# -- boundary ------------------------------------------------------------------
@pytest.mark.boundary
def test_real_portal_delivers_a_global_shortcut(capsys):
    """Binds Ctrl+Space through the real portal and waits for the owner to press it.

    The desktop shows its shortcut dialog on the first run; accept it. Skips (does
    not fail) if nothing arrives, so an unattended run cannot go red.
    """
    events: list[tuple[str, str]] = []
    listener = PortalListener(lambda name, kind: events.append((name, kind)), {"dictate": "CTRL+space"})
    listener.start()
    try:
        if listener.devices_ok() is False:
            pytest.skip(f"the portal refused to bind: install the desktop entry "
                        f"({APP_ID}.desktop, Exec must exist) with ./install.sh, then rerun")
        with capsys.disabled():
            print("\n>>> press Ctrl+Space within 15 s <<<", flush=True)
            if not wait_for(lambda: events, timeout=15):
                pytest.skip("no Ctrl+Space press arrived within 15 s")
        assert events[0] == ("dictate", "press")
        assert wait_for(lambda: ("dictate", "release") in events, timeout=5)
    finally:
        listener.stop()


def test_module_exposes_the_evdev_listener_interface():
    from voice.hotkey.evdev_listener import EvdevListener
    for name in ("start", "stop", "capture_next", "held", "modifiers_held", "devices_ok"):
        assert callable(getattr(portal_listener.PortalListener, name)), name
        assert hasattr(EvdevListener, name)
