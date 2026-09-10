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


def started(listener, timeout=2.0):
    """start() opens the session on the listener thread, so tests wait for it."""
    assert wait_for(lambda: listener.devices_ok() is not None, timeout), "listener never finished starting"
    return listener


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
                 create_error=None, open_delay=0.0, drop_after_bind=False, subscribe_error=False):
        self.unique_name = ":1.99"
        self.bind_code, self.create_code = bind_code, create_code
        self.version, self.registry, self.configure_error = version, registry, configure_error
        self.create_error = create_error
        self.open_delay, self.drop_after_bind = open_delay, drop_after_bind
        self.subscribe_error = subscribe_error
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
        if member == "AddMatch" and self.subscribe_error and "Response" not in str(msg.body[0]):
            return _Reply("error", ("match rule refused",))   # the signal subscription, not a Request
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
            time.sleep(self.open_delay)       # the portal's permission dialog is slow
            if member == "CreateSession":
                return SimpleNamespace(body=(self.create_code, {"session_handle": ("o", SESSION)}))
            return SimpleNamespace(body=(self.bind_code, {}))
        if self.drop_after_bind:
            raise ConnectionResetError("portal went away")
        with self._lock:
            if self._signals:
                return self._signals.popleft()
        time.sleep(0.005)               # the real connection blocks until the timeout
        raise TimeoutError

    def close(self):
        self.closed = True


def make(shortcuts=None, on_event=None, on_ready=None, **kwargs):
    conn = FakeConn(**kwargs)
    events: list[tuple[str, str]] = []
    listener = PortalListener(on_event or (lambda name, kind: events.append((name, kind))),
                              shortcuts if shortcuts is not None else {"dictate": "CTRL+space"},
                              bus_factory=lambda bus="SESSION": conn, on_ready=on_ready)
    return listener, conn, events


# -- binding -------------------------------------------------------------------
def test_bind_shortcuts_sends_every_configured_id_with_its_trigger():
    listener, conn, _ = make({"dictate": "CTRL+space", "cancel": "CTRL+ALT+c"})
    try:
        listener.start()
        started(listener)
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
        started(listener)
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
    started(listener)
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
    started(listener)
    assert listener.devices_ok() is False
    listener.stop()


def test_a_missing_app_id_is_reported_with_the_fix(caplog):
    listener, conn, _ = make(create_error="An app id is required")
    listener.start()
    started(listener)
    try:
        assert listener.devices_ok() is False
        assert "install.sh" in caplog.text and f"{APP_ID}.desktop" in caplog.text
    finally:
        listener.stop()


def test_no_configured_trigger_is_a_bind_failure_not_a_silent_no_op():
    """Binding an empty list succeeds at the portal, so it must be refused here:
    otherwise an upgraded config reports healthy while no key does anything."""
    listener, conn, _ = make({})
    listener.start()
    started(listener)
    try:
        assert listener.devices_ok() is False
        assert conn.calls == []                              # not even a session was opened
    finally:
        listener.stop()


def test_start_does_not_block_on_the_portals_permission_dialog():
    """The compositor's dialog answers BindShortcuts; blocking start() on it would
    leave the daemon with no tray and no event loop until the user clicks."""
    listener, _, _ = make(open_delay=0.4)
    begin = time.time()
    listener.start()
    elapsed = time.time() - begin
    try:
        assert elapsed < 0.2, f"start() blocked for {elapsed:.2f}s"
        assert listener.devices_ok() is None                  # still deciding
        started(listener)
        assert listener.devices_ok() is True
    finally:
        listener.stop()


def test_on_ready_reports_the_outcome_once_the_portal_answers():
    seen: list[bool] = []
    listener, _, _ = make(on_ready=seen.append)
    listener.start()
    started(listener)
    listener.stop()
    assert seen == [True]

    denied: list[bool] = []
    listener, _, _ = make(bind_code=1, on_ready=denied.append)
    listener.start()
    started(listener)
    listener.stop()
    assert denied == [False]


def test_a_lost_portal_connection_stops_reporting_healthy():
    """A portal restart kills the connection; status must not keep saying "ok"."""
    listener, conn, _ = make(drop_after_bind=True)
    listener.start()
    started(listener)
    try:
        assert wait_for(lambda: listener.devices_ok() is False)
    finally:
        listener.stop()


def test_a_failed_signal_subscription_is_a_bind_failure():
    """Without the match rule no Activated can ever arrive, so reporting a healthy
    backend would leave `voice status` saying ok while every hotkey is dead."""
    ready: list[bool] = []
    listener, _, _ = make(on_ready=ready.append, subscribe_error=True)
    listener.start()
    started(listener)
    try:
        assert listener.devices_ok() is False
        assert ready == [False]
        assert wait_for(lambda: not any(t.name == "portal-listener" for t in threading.enumerate()))
    finally:
        listener.stop()


def test_quitting_while_the_permission_dialog_is_open_says_nothing():
    """stop() during the compositor's dialog must not fire the "not registered"
    notification: the user quit, the desktop did not refuse anything."""
    ready: list[bool] = []
    listener, _, _ = make(on_ready=ready.append, open_delay=0.3)
    listener.start()
    time.sleep(0.02)                                         # thread is inside _open
    begin = time.time()
    listener.stop()
    assert time.time() - begin < 1.5, "stop() stalled while quitting"
    assert wait_for(lambda: True, timeout=0.4) and ready == [], f"announced {ready} after stop()"


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
        started(listener)
        assert conn.bodies("Register") == [(APP_ID, {})]
    finally:
        listener.stop()


def test_registration_is_skipped_when_the_host_registry_is_absent():
    listener, conn, _ = make(registry=False)
    try:
        listener.start()
        started(listener)
        assert conn.bodies("Register") == []
        assert listener.devices_ok() is True                  # absent registry is not an error
    finally:
        listener.stop()


# -- capture -------------------------------------------------------------------
def test_capture_next_explains_itself_when_the_portal_is_too_old():
    listener, conn, _ = make(version=1)
    try:
        listener.start()
        started(listener)
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
        started(listener)
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
        started(listener)
        got: list[str] = []
        listener.capture_next(got.append)
        assert got == [NO_CAPTURE_MESSAGE]
    finally:
        listener.stop()


def test_capture_next_stands_down_while_the_binding_is_still_pending():
    """While the compositor's dialog is up, the listener thread is using the
    connection without the lock (_open takes none), so a second user of it would
    drive the same jeepney parser and socket from two threads."""
    listener, conn, _ = make(version=2, open_delay=0.5)
    listener.start()
    try:
        assert wait_for(lambda: conn.bodies("BindShortcuts"), timeout=2)   # sent, answer pending
        assert listener.devices_ok() is None
        got: list[str] = []
        listener.capture_next(got.append)
        assert got == [NO_CAPTURE_MESSAGE]
        assert conn.bodies("ConfigureShortcuts") == []
        started(listener)                                                  # bind resolved
        got.clear()
        listener.capture_next(got.append)
        assert conn.bodies("ConfigureShortcuts") and got[0] != NO_CAPTURE_MESSAGE
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
