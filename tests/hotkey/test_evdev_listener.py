import threading
import time

from evdev import ecodes as e

from voice.hotkey.evdev_listener import EvdevListener
from voice.hotkey.keyspec import Tracker, parse_keyspec


class FakeDevice:
    """Stands in for evdev.InputDevice: has a fileno via a pipe and yields queued events."""

    def __init__(self, path="/dev/input/event99"):
        import os
        self.path = path
        self.name = "fake kbd"
        self._r, self._w = os.pipe()
        self._queue = []
        self._lock = threading.Lock()

    def fileno(self):
        return self._r

    def push(self, code, value):
        import os
        with self._lock:
            self._queue.append((code, value))
        os.write(self._w, b"x")

    def read(self):
        import os
        os.read(self._r, 1)
        with self._lock:
            items, self._queue = self._queue, []
        for code, value in items:
            yield type("Ev", (), {"type": e.EV_KEY, "code": code, "value": value})()

    def close(self):
        import os
        os.close(self._r)
        os.close(self._w)


def wait_for(pred, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_listener_forwards_press_and_release_events():
    dev = FakeDevice()
    got = []
    tracker = Tracker({"dictate": parse_keyspec("KEY_F13")})
    listener = EvdevListener(tracker, lambda n, k: got.append((n, k)), device_factory=lambda: [dev])
    listener.start()
    try:
        dev.push(e.KEY_F13, 1)
        assert wait_for(lambda: got == [("dictate", "press")])
        dev.push(e.KEY_F13, 0)
        assert wait_for(lambda: got == [("dictate", "press"), ("dictate", "release")])
        assert listener.devices_ok()
    finally:
        listener.stop()


def test_capture_next_reports_name_and_swallows_event():
    dev = FakeDevice()
    got, captured = [], []
    tracker = Tracker({"dictate": parse_keyspec("KEY_F13")})
    listener = EvdevListener(tracker, lambda n, k: got.append((n, k)), device_factory=lambda: [dev])
    listener.start()
    try:
        listener.capture_next(captured.append)
        dev.push(e.KEY_F13, 1)
        dev.push(e.KEY_F13, 0)
        assert wait_for(lambda: captured == ["KEY_F13"])
        time.sleep(0.05)
        assert got == []                      # not forwarded while capturing
        dev.push(e.KEY_F13, 1)
        assert wait_for(lambda: got == [("dictate", "press")])
    finally:
        listener.stop()


def test_no_devices_marks_not_ok_and_stop_is_clean():
    listener = EvdevListener(Tracker({}), lambda n, k: None, device_factory=lambda: [])
    listener.start()
    assert listener.devices_ok() is False   # initial scan is synchronous: no need to wait
    listener.stop()


def test_callback_exception_does_not_kill_the_listener_thread():
    dev = FakeDevice()
    got = []

    def on_event(name, kind):
        got.append((name, kind))
        if len(got) == 1:
            raise RuntimeError("handler boom")

    tracker = Tracker({"dictate": parse_keyspec("KEY_F13")})
    listener = EvdevListener(tracker, on_event, device_factory=lambda: [dev])
    listener.start()
    try:
        dev.push(e.KEY_F13, 1)
        assert wait_for(lambda: got == [("dictate", "press")])
        dev.push(e.KEY_F13, 0)
        # The raising press must not have taken the thread down with it.
        assert wait_for(lambda: got == [("dictate", "press"), ("dictate", "release")])
    finally:
        listener.stop()
