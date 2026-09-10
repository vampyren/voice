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

    # -- public -----------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
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
    def _run(self) -> None:
        sel = selectors.DefaultSelector()
        sel.register(self._wake_r, selectors.EVENT_READ, data=None)
        devices: dict[int, object] = {}
        known_nodes: set[str] = set()

        def rescan() -> None:
            nonlocal known_nodes
            for dev in self._factory():
                if dev.fileno() not in devices:
                    devices[dev.fileno()] = dev
                    sel.register(dev.fileno(), selectors.EVENT_READ, data=dev)
                    log.info("listening on %s (%s)", dev.path, dev.name)
            self._devices_ok = bool(devices)
            known_nodes = set(os.listdir(INPUT_DIR)) if os.path.isdir(INPUT_DIR) else set()

        rescan()
        while not self._stop.is_set():
            for key, _ in sel.select(timeout=RESCAN_SECONDS):
                if key.data is None:
                    os.read(self._wake_r, 1)
                    continue
                dev = key.data
                try:
                    for ev in dev.read():
                        if ev.type == ecodes.EV_KEY:
                            self._handle(ev.code, ev.value)
                except OSError:
                    log.info("device gone: %s", getattr(dev, "path", "?"))
                    sel.unregister(key.fd)
                    devices.pop(key.fd, None)
                    self._devices_ok = bool(devices)
            if os.path.isdir(INPUT_DIR) and set(os.listdir(INPUT_DIR)) != known_nodes:
                rescan()
        for dev in devices.values():
            try:
                dev.close()
            except Exception:
                pass
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
