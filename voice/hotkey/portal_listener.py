"""Hotkeys through xdg-desktop-portal's GlobalShortcuts interface.

The evdev listener reads /dev/input, which is empty of real keystrokes in a
remote-desktop session and unreadable without the udev rule. The portal instead
asks the compositor to bind a shortcut and pushes `Activated`/`Deactivated`
signals at us, so it works wherever the desktop itself works.

The public surface is deliberately identical to `EvdevListener`
(start/stop/capture_next/held/modifiers_held/devices_ok) - the daemon swaps one
for the other and changes nothing else. `shortcut_state`/`effective_triggers`
are the two portal-only extras: only this backend can be registered with the
desktop and yet have no key attached.

**The desktop owns the trigger.** `preferred_trigger` is a first-run wish, not a
setting: sending it again for a shortcut id the portal already knows makes GNOME
50 drop the key the user assigned (its stored entry comes back with no
`shortcuts` member at all) while still answering BindShortcuts with success. So
ListShortcuts decides, per id, whether we may express a preference at all - and
an answer that cannot distinguish "never seen" from "seen and assigned" counts
as a no. Measured against GNOME 50 (Registry.Register, CreateSession,
ListShortcuts, nothing bound): the reply is `{'shortcuts': ('a(sa{sv})', [])}`
while dconf holds three assigned shortcuts for the same app id, because the
call is scoped to the session and the session is necessarily new. An empty
listing is therefore no evidence at all, and on such a desktop we never express
a preference - the user assigns the key in their keyboard settings, and
`shortcut_state()` says so until they do.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

from jeepney import HeaderFields, MatchRule, new_method_call
from jeepney.bus_messages import message_bus
from jeepney.io.blocking import open_dbus_connection

from voice import APP_ID
from voice.portal_common import PortalError, call_with_response, new_token, portal_address

log = logging.getLogger(__name__)

INTERFACE = "org.freedesktop.portal.GlobalShortcuts"
REGISTRY_INTERFACE = "org.freedesktop.host.portal.Registry"
SHORTCUTS = portal_address(INTERFACE)
PROPS = SHORTCUTS.with_interface("org.freedesktop.DBus.Properties")
INTROSPECTABLE = SHORTCUTS.with_interface("org.freedesktop.DBus.Introspectable")
REGISTRY = portal_address(REGISTRY_INTERFACE)

DESCRIPTIONS = {
    "dictate": "Voice dictation",
    "recall": "Re-insert the last dictation",
    "cancel": "Cancel the current recording",
}
#: ConfigureShortcuts (the desktop's own rebinding dialog) arrived in version 2.
CONFIGURE_VERSION = 2
#: What the portal made of our shortcuts, once it has answered.
STATE_BOUND = "bound"
STATE_UNASSIGNED = "unassigned"
STATE_DENIED = "denied"
#: How an empty trigger reads in a log line, `voice status` and `voice doctor`.
NO_TRIGGER = "no key assigned"
NO_CAPTURE_MESSAGE = "portal: change the shortcut in your desktop's settings"
DIALOG_MESSAGE = "portal: choose the shortcut in the dialog your desktop just opened"
#: How long a receive blocks before the loop re-checks the stop flag.
RECV_SLICE_S = 0.5
#: ConfigureShortcuts returns its Request handle at once; only the dialog is slow.
CALL_TIMEOUT_S = 5
#: capture_next runs on the Qt thread, so its round trip is kept short.
CONFIGURE_TIMEOUT_S = 2
#: ListShortcuts shows no dialog, so it must never wait like one.
LIST_TIMEOUT_S = 10
#: The `a(sa{sv})` member both ListShortcuts and BindShortcuts answer with, and
#: the per-shortcut key holding the trigger the desktop actually bound.
SHORTCUTS_RESULT = "shortcuts"
TRIGGER_KEY = "trigger_description"


def _unwrap(value):
    """jeepney hands back a{sv} members as (signature, value) pairs."""
    return value[1] if isinstance(value, tuple) and len(value) == 2 else value


def shortcut_triggers(results: dict | None) -> dict[str, str] | None:
    """`{shortcut id: effective trigger}` from a portal Response, or None.

    None means the portal said nothing about triggers (an older backend, or a
    bare vardict) - which is not the same as "no key assigned", so the caller
    asks again rather than reporting the shortcut dead.
    """
    member = (results or {}).get(SHORTCUTS_RESULT)
    if member is None:
        return None
    out: dict[str, str] = {}
    for entry in _unwrap(member) or ():
        try:
            sid, options = entry[0], entry[1]
        except (TypeError, IndexError, KeyError):
            continue
        out[str(sid)] = str(_unwrap((options or {}).get(TRIGGER_KEY)) or "")
    return out


class PortalListener:
    """Binds shortcuts through the portal and reports press/release events.

    `shortcuts` maps a shortcut id ("dictate", "recall", "cancel") to an XDG
    trigger string such as "CTRL+space". The session is created once in start()
    and kept for the daemon's lifetime, so the compositor asks the user only once.
    """

    def __init__(self, on_event: Callable[[str, str], None], shortcuts: dict[str, str],
                 bus_factory: Callable = open_dbus_connection, app_id: str = APP_ID,
                 on_ready: Callable[[str], None] | None = None):
        self._on_event = on_event
        self._on_ready = on_ready
        self._shortcuts = dict(shortcuts)
        self._bus_factory = bus_factory
        self._app_id = app_id
        self._conn = None
        self._session: str | None = None
        self._version = 1
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._bound: bool | None = None
        self._triggers: dict[str, str] = {}
        self._active: set[str] = set()
        self._state_lock = threading.Lock()
        # The receive thread owns the connection; capture_next borrows it between
        # two receives. One jeepney connection must never be used from two threads
        # at once, so every use outside start()/stop() takes this.
        self._bus_lock = threading.Lock()

    # -- public (EvdevListener interface) ---------------------------------
    def start(self) -> None:
        """Return at once; the session is opened on the listener thread.

        BindShortcuts is the call the compositor answers with its permission
        dialog, which can take as long as the user does. Opening synchronously
        would leave the daemon with no tray and no Qt event loop until then, so
        devices_ok() stays None until the portal has answered and `on_ready` (if
        given) reports the outcome as one of the STATE_* words.
        """
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="portal-listener", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=RECV_SLICE_S * 2)
            self._thread = None
        self._close()

    def capture_next(self, callback: Callable[[str], None]) -> None:
        """Ask the desktop to show its own rebinding dialog; we never see raw keys.

        The callback gets a sentence for the user rather than a key name - the
        settings dialog shows it instead of writing it into the hotkey field.

        `_bound is not True` covers the window while the compositor's permission
        dialog is up: _open() is then driving this connection from the listener
        thread without _bus_lock (taking it there would park the Qt thread for as
        long as the dialog stays open), and jeepney does no locking of its own.
        """
        if (self._conn is None or self._session is None or self._bound is not True
                or self._version < CONFIGURE_VERSION):
            callback(NO_CAPTURE_MESSAGE)
            return
        try:
            options = {"handle_token": ("s", new_token())}
            with self._bus_lock:
                reply = self._conn.send_and_get_reply(
                    new_method_call(SHORTCUTS, "ConfigureShortcuts", "osa{sv}",
                                    (self._session, "", options)), timeout=CONFIGURE_TIMEOUT_S)
            if reply.header.message_type.name == "error":
                raise PortalError(f"ConfigureShortcuts failed: {reply.body}")
        except Exception as exc:
            log.info("portal cannot open the shortcut dialog: %s", exc)
            callback(NO_CAPTURE_MESSAGE)
            return
        callback(DIALOG_MESSAGE)

    def held(self) -> frozenset[str]:
        """The shortcut ids currently activated (the portal exposes no key codes)."""
        with self._state_lock:
            return frozenset(self._active)

    def modifiers_held(self) -> bool:
        # The compositor consumed the whole chord before telling us, so there is
        # nothing left held that could corrupt an injected paste.
        return False

    def devices_ok(self) -> bool | None:
        """True once a key is actually attached, False if not, None before start().

        A shortcut the desktop registered without a trigger is not usable, so it
        is not "ok": saying otherwise is exactly how a dead hotkey read as healthy.
        """
        state = self.shortcut_state()
        return None if state is None else state == STATE_BOUND

    def shortcut_state(self) -> str | None:
        """One of STATE_*, or None before the portal has answered."""
        with self._state_lock:
            if self._bound is None:
                return None
            if not self._bound:
                return STATE_DENIED
            return STATE_UNASSIGNED if not all(self._triggers.values()) else STATE_BOUND

    def effective_triggers(self) -> dict[str, str]:
        """The trigger the desktop actually holds per shortcut id ("" = none)."""
        with self._state_lock:
            return dict(self._triggers)

    # -- setup --------------------------------------------------------------
    @staticmethod
    def _signal_rule() -> MatchRule:
        return MatchRule(type="signal", interface=INTERFACE)

    def _subscribe(self) -> None:
        """Ask the bus for the shortcut signals.

        Done inside _open() so a single path decides `_bound`: without the match
        rule no Activated can ever arrive, and reporting a healthy backend then
        would leave `voice status` saying "ok" while every hotkey is dead.
        """
        reply = self._conn.send_and_get_reply(message_bus.AddMatch(self._signal_rule()),
                                              timeout=CALL_TIMEOUT_S)
        if reply.header.message_type.name == "error":
            raise PortalError(f"could not subscribe to portal shortcut signals: {reply.body}")

    def _open(self) -> None:
        if not self._shortcuts:
            # BindShortcuts with an empty list succeeds, which would report a
            # healthy backend that can never fire.
            raise PortalError("no portal shortcut trigger configured (hotkeys.portal_dictate is empty)")
        self._conn = self._bus_factory(bus="SESSION")
        self._subscribe()
        self._register_app_id()
        self._version = self._portal_version()
        try:
            code, results = call_with_response(
                self._conn, SHORTCUTS, "CreateSession", "a{sv}",
                ({"session_handle_token": ("s", new_token())},))
        except PortalError as exc:
            # The commonest first-run failure by far, and the message the portal
            # sends ("An app id is required") says nothing about the cause.
            if "app id" in str(exc).lower():
                raise PortalError(f"{exc}; the portal resolves our app id through an installed "
                                  f"desktop entry named '{self._app_id}.desktop' whose Exec exists "
                                  f"- run install.sh") from exc
            raise
        if code != 0:
            raise PortalError(f"portal CreateSession denied (response {code})")
        self._session = results["session_handle"][1]
        known = self._list_shortcuts() or None
        code, bound = call_with_response(
            self._conn, SHORTCUTS, "BindShortcuts", "oa(sa{sv})sa{sv}",
            (self._session, self._bindings(known), "", {}))
        if code != 0:
            self._bound = False
            raise PortalError(f"portal BindShortcuts denied (response {code}); "
                              f"allow '{self._app_id}' to take a global shortcut")
        triggers = shortcut_triggers(bound)
        if triggers is None:
            # The reply said nothing about triggers, which is not the same as
            # "none assigned". Ask outright rather than guess either way.
            triggers = self._list_shortcuts() or {}
        with self._state_lock:
            self._bound = True
            self._triggers = {sid: triggers.get(sid, "") for sid in self._shortcuts}
        log.info("portal shortcuts registered: %s", self._describe_triggers())

    def _describe_triggers(self) -> str:
        return ", ".join(f"{sid}={trigger or NO_TRIGGER}"
                         for sid, trigger in self.effective_triggers().items())

    def _list_shortcuts(self) -> dict[str, str] | None:
        """What the portal already holds for us, or None when it cannot say.

        None is the safe answer: `_bindings` then expresses no preference at all,
        because re-requesting a trigger is what destroys an existing assignment.
        """
        try:
            code, results = call_with_response(
                self._conn, SHORTCUTS, "ListShortcuts", "oa{sv}", (self._session, {}),
                timeout=LIST_TIMEOUT_S)
        except Exception as exc:
            log.info("portal ListShortcuts unavailable (%s); binding without a preferred "
                     "trigger so any key you already assigned survives", exc)
            return None
        if code != 0:
            log.info("portal ListShortcuts refused (response %s); binding without a "
                     "preferred trigger", code)
            return None
        return shortcut_triggers(results)

    def _bindings(self, known: dict[str, str] | None) -> list[tuple[str, dict]]:
        """One entry per configured id; `preferred_trigger` only where it is safe.

        `known is None` - the portal could not be asked, or answered with an
        empty session listing that says nothing about what it holds - counts
        every id as known: the cost of not asking for a trigger is one trip to
        Keyboard Settings, the cost of asking wrongly is the user's binding.
        """
        out = []
        for sid, trigger in self._shortcuts.items():
            options = {"description": ("s", DESCRIPTIONS.get(sid, f"voice {sid}"))}
            if trigger and known is not None and sid not in known:
                options["preferred_trigger"] = ("s", trigger)
            out.append((sid, options))
        return out

    def _register_app_id(self) -> None:
        """Tell the portal who we are; non-sandboxed apps get no app id otherwise.

        Optional in every sense: the interface only exists on portal >= 1.18, and a
        failure just means the desktop labels the shortcut less prettily.
        """
        try:
            reply = self._conn.send_and_get_reply(new_method_call(INTROSPECTABLE, "Introspect"),
                                                  timeout=CALL_TIMEOUT_S)
            xml = reply.body[0] if reply.body else ""
            if reply.header.message_type.name == "error" or REGISTRY_INTERFACE not in xml:
                log.debug("no %s on this portal; skipping app id registration", REGISTRY_INTERFACE)
                return
            reply = self._conn.send_and_get_reply(
                new_method_call(REGISTRY, "Register", "sa{sv}", (self._app_id, {})),
                timeout=CALL_TIMEOUT_S)
            if reply.header.message_type.name == "error":
                log.debug("portal Registry.Register refused: %s", reply.body)
        except Exception:
            log.debug("portal app id registration skipped", exc_info=True)

    def _portal_version(self) -> int:
        try:
            reply = self._conn.send_and_get_reply(
                new_method_call(PROPS, "Get", "ss", (INTERFACE, "version")), timeout=CALL_TIMEOUT_S)
            if reply.header.message_type.name == "error":
                return 1
            return int(reply.body[0][1])
        except Exception:
            log.debug("could not read the GlobalShortcuts version", exc_info=True)
            return 1

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                log.debug("closing the portal connection failed", exc_info=True)
        self._conn, self._session = None, None

    # -- thread -------------------------------------------------------------
    def _run(self) -> None:
        try:
            self._open()
        except Exception as exc:
            # No hotkeys is bad, but a daemon that refuses to start is worse: the
            # user still has the tray and the CLI, and devices_ok() says why.
            # Quitting mid-open lands here too (stop() closes the socket under
            # us): that is not a portal problem and must stay quiet.
            log.log(logging.DEBUG if self._stop.is_set() else logging.ERROR,
                    "portal global shortcuts unavailable: %s", exc)
            self._bound = False
            self._close()
            self._announce()
            return
        self._announce()
        self._listen()

    def _announce(self) -> None:
        # Shutting down: the user quit, the desktop refused nothing. Announcing
        # here pops a critical "shortcut not registered" notification on quit.
        if self._on_ready is None or self._stop.is_set():
            return
        try:
            self._on_ready(self.shortcut_state() or STATE_DENIED)
        except Exception:
            log.exception("hotkey readiness callback failed")

    def _listen(self) -> None:
        # stop() closes the connection and drops it, and this thread can be
        # anywhere - including right here, on its way into the loop. Take the
        # connection once and give up if it has already gone.
        conn = self._conn
        if conn is None or self._stop.is_set():
            return
        # bufsize: a chord pressed while the loop is busy must not drop its release.
        with conn.filter(self._signal_rule(), bufsize=64) as queue:
            while not self._stop.is_set():
                try:
                    with self._bus_lock:
                        msg = conn.recv_until_filtered(queue, timeout=RECV_SLICE_S)
                except TimeoutError:
                    continue
                except Exception:
                    if not self._stop.is_set():
                        # The portal restarted or the bus dropped us. Nothing
                        # reconnects, so stop claiming the hotkeys still work.
                        log.warning("portal shortcut connection lost; hotkeys are dead until restart",
                                    exc_info=True)
                        self._bound = False
                    return
                try:
                    self._dispatch(msg)
                except Exception:
                    # A failing handler must never take this thread down: every
                    # hotkey would go dead for the rest of the session.
                    log.exception("hotkey handler failed for %s", getattr(msg, "body", msg))

    def _dispatch(self, msg) -> None:
        kind = {"Activated": "press", "Deactivated": "release"}.get(
            msg.header.fields.get(HeaderFields.member))
        if kind is None:                       # ShortcutsChanged and friends
            return
        session, shortcut_id = msg.body[0], msg.body[1]
        if session != self._session or shortcut_id not in self._shortcuts:
            return
        with self._state_lock:
            if kind == "press":
                self._active.add(shortcut_id)
            else:
                self._active.discard(shortcut_id)
        self._on_event(shortcut_id, kind)
