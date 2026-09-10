"""Background thread reading keyboards via evdev and feeding the Tracker."""
from __future__ import annotations

import logging
import os
import selectors
import threading
from typing import Callable, Iterable

import evdev
from evdev import ecodes

from voice.hotkey.keyspec import Tracker, keyspec_name

log = logging.getLogger(__name__)
INPUT_DIR = "/dev/input"
RESCAN_SECONDS = 2.0


def _is_keyboard_like(dev: evdev.InputDevice) -> bool:
    keys = dev.capabilities().get(ecodes.EV_KEY, [])
    return ecodes.KEY_A in keys or ecodes.BTN_SIDE in keys


def list_keyboards() -> list[evdev.InputDevice]:
    found: list[evdev.InputDevice] = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
        except (PermissionError, OSError) as exc:
            log.debug("skip %s: %s", path, exc)
            continue
        if _is_keyboard_like(dev):
            found.append(dev)
        else:
            dev.close()
    return found


class EvdevListener:
    def __init__(
        self,
        tracker: Tracker,
        on_event: Callable[[str, str], None],
        device_factory: Callable[[], list] | None = None,
    ):
        self._tracker = tracker
        self._on_event = on_event
        self._factory = device_factory or list_keyboards
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake_r, self._wake_w = os.pipe()
        self._capture: Callable[[str], None] | None = None
        self._lock = threading.Lock()
        self._devices_ok: bool | None = None
        self._sel: selectors.BaseSelector | None = None
        self._devices: dict[int, object] = {}
        self._known_nodes: set[str] = set()

    # -- public -----------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        self._sel = selectors.DefaultSelector()
        self._sel.register(self._wake_r, selectors.EVENT_READ, data=None)
        self._devices = {}
        self._known_nodes = set()
        self._rescan()  # synchronous: so devices_ok() is accurate the instant start() returns
        self._thread = threading.Thread(target=self._run, name="evdev-listener", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        os.write(self._wake_w, b"x")
        if self._thread:
            self._thread.join(timeout=2)

    def capture_next(self, callback: Callable[[str], None]) -> None:
        with self._lock:
            self._capture = callback

    def held(self):
        return self._tracker.held()

    def modifiers_held(self) -> bool:
        return self._tracker.modifiers_held()

    def devices_ok(self) -> bool | None:
        return self._devices_ok

    # -- thread -----------------------------------------------------------
    @staticmethod
    def _close(dev: object) -> None:
        try:
            dev.close()
        except Exception:
            log.debug("closing %s failed", getattr(dev, "path", "?"), exc_info=True)

    def _rescan(self) -> None:
        # Dedupe on the device node, not the fd: the factory hands back freshly
        # opened InputDevices every time, so a fileno check registered each
        # keyboard again on every /dev/input change - leaking fds and delivering
        # each keystroke once per registration.
        registered = {getattr(d, "path", None) for d in self._devices.values()}
        for dev in self._factory():
            path = getattr(dev, "path", None)
            if path in registered:
                self._close(dev)
                continue
            self._devices[dev.fileno()] = dev
            registered.add(path)
            self._sel.register(dev.fileno(), selectors.EVENT_READ, data=dev)
            log.info("listening on %s (%s)", path, dev.name)
        self._devices_ok = bool(self._devices)
        self._known_nodes = set(os.listdir(INPUT_DIR)) if os.path.isdir(INPUT_DIR) else set()

    def _run(self) -> None:
        sel = self._sel
        while not self._stop.is_set():
            for key, _ in sel.select(timeout=RESCAN_SECONDS):
                if key.data is None:
                    os.read(self._wake_r, 1)
                    continue
                dev = key.data
                try:
                    for ev in dev.read():
                        if ev.type == ecodes.EV_KEY:
                            try:
                                self._handle(ev.code, ev.value)
                            except Exception:
                                # A failing callback must never take this thread down:
                                # every hotkey would go dead for the rest of the session.
                                log.exception("hotkey handler failed for code %s value %s", ev.code, ev.value)
                except OSError:
                    log.info("device gone: %s", getattr(dev, "path", "?"))
                    sel.unregister(key.fd)
                    self._devices.pop(key.fd, None)
                    self._devices_ok = bool(self._devices)
                    self._close(dev)       # release the fd so a re-plug can be re-opened
            if os.path.isdir(INPUT_DIR) and set(os.listdir(INPUT_DIR)) != self._known_nodes:
                self._rescan()
        for dev in self._devices.values():
            self._close(dev)
        sel.close()

    def _handle(self, code: int, value: int) -> None:
        with self._lock:
            cb = self._capture
            if cb is not None and value == 1:
                self._capture = None
        if cb is not None and value == 1:
            cb(keyspec_name(code))
            return
        if cb is None and self._capture is None:
            for name, kind in self._tracker.feed(code, value):
                self._on_event(name, kind)
