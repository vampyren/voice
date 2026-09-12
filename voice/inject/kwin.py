"""Ask KWin which window has the keyboard, without installing anything.

KWin will not answer that over plain D-Bus. `org.kde.KWin.queryWindowInfo`
looks like it does and is an interactive window picker - it waits for the user
to click, with the cursor as a crosshair (measured on Plasma 6: untouched it
times out with no output; it answers only after a click). See
`voice.inject.window` for that finding.

What KWin *will* do is run a script, in-process, with the full scripting API -
and `workspace.activeWindow` is right there. That is how `kdotool` does it too.
A script has no way to return a value, so it hands one back the only way it
can: `callDBus` out to a bus name. This module's own connection already has a
unique name, so the script calls straight back to us - nothing to register,
nothing to clash with, and no well-known name to leave behind.

The whole exchange is D-Bus, from a connection this process already knows how
to open. There is no `qdbus`, no shell, and therefore none of what a shell
brought with it: no subprocess in the paste path, no timeout that kills a
pipeline's first process while the rest keeps running, no orphans.

The script itself is five lines and is written here rather than shipped as a
data file, so that what runs inside the compositor is visible next to the code
that runs it.
"""
from __future__ import annotations

import logging
import os
import secrets
import tempfile
import time

from jeepney import DBusAddress, Message, MessageType, new_error, new_method_call, new_method_return

log = logging.getLogger(__name__)

#: KWin's scripting service. `/Scripting` loads and unloads; each loaded script
#: gets its own object at `/Scripting/Script<id>` which is what actually runs.
KWIN_BUS = "org.kde.KWin"
SCRIPTING = DBusAddress("/Scripting", bus_name=KWIN_BUS,
                        interface="org.kde.kwin.Scripting")

#: How long to wait for the script to call back. It runs inside the compositor
#: and answers in milliseconds; a second is a bound on something wedged, not a
#: budget anything is expected to use.
REPLY_TIMEOUT_S = 1.0

#: The interface and method the script calls back on. Arbitrary - we are both
#: ends of it - but named so that anything watching the bus can tell what it is.
CALLBACK_INTERFACE = "io.github.vampyren.voice.KWin"
CALLBACK_METHOD = "ActiveWindow"

#: `resourceClass` is what `inject.terminal_classes` is written in - Konsole
#: reports `org.kde.konsole`, which is exactly the form the default config
#: lists. `activeWindow` is null when nothing is focused, which is a real
#: answer and not an error: it means "nobody has the keyboard".
_SCRIPT = """\
var w = workspace.activeWindow;
callDBus("%(bus)s", "%(path)s", "%(interface)s", "%(method)s",
         w ? String(w.resourceClass) : "");
"""


def script_source(unique_name: str, path: str = "/") -> str:
    """The script KWin will run, addressed back to `unique_name`."""
    return _SCRIPT % {"bus": unique_name, "path": path,
                      "interface": CALLBACK_INTERFACE, "method": CALLBACK_METHOD}


class KWinScriptError(RuntimeError):
    """KWin refused to load or run the script."""


def _call(conn, address: DBusAddress, method: str, signature: str, args: tuple,
          timeout: float):
    reply = conn.send_and_get_reply(new_method_call(address, method, signature, args),
                                    timeout=timeout)
    if reply.header.message_type is MessageType.error:
        raise KWinScriptError(f"{method} failed: {reply.body}")
    return reply.body


def active_window_class(conn, timeout: float = REPLY_TIMEOUT_S) -> str | None:
    """The focused window's class per KWin, or None if it would not say.

    None covers every way of not getting an answer - KWin is not there, the
    script would not load, nothing called back in time. It never means "no
    window is focused": that comes back as an empty string, because it is a
    real answer and the caller should not retry it as a failure.
    """
    name = f"voice_{secrets.token_hex(4)}"
    source = script_source(conn.unique_name)
    handle, path = tempfile.mkstemp(prefix="voice-kwin-", suffix=".js")
    try:
        with os.fdopen(handle, "w") as out:
            out.write(source)
        # loadScript answers with the id of the object that runs it. KWin
        # refuses a name it already holds, hence the random one per call.
        script_id = _call(conn, SCRIPTING, "loadScript", "ss", (path, name), timeout)[0]
        runner = DBusAddress(f"/Scripting/Script{script_id}", bus_name=KWIN_BUS,
                             interface="org.kde.kwin.Script")
        try:
            _call(conn, runner, "run", "", (), timeout)
            return _await_callback(conn, timeout)
        finally:
            try:
                _call(conn, SCRIPTING, "unloadScript", "s", (name,), timeout)
            except Exception:
                # A script left loaded is inert - it only runs when run() is
                # called - but it holds a name, and this one is random, so the
                # cost of failing to clean up is a little memory in KWin.
                log.debug("could not unload the KWin script %s", name, exc_info=True)
    except KWinScriptError as exc:
        log.debug("KWin would not answer: %s", exc)
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _await_callback(conn, timeout: float) -> str | None:
    """Wait for the script's `callDBus` to arrive, and answer it.

    The script's call is answered even though nothing reads the reply: an
    unanswered method call leaves KWin waiting for its own timeout, and logs.
    """
    # A real deadline, not a per-message budget: anything else calling us would
    # otherwise reset the timeout every time and hold the paste open.
    ends_at = time.monotonic() + timeout
    while True:
        left = ends_at - time.monotonic()
        if left <= 0:
            return None
        try:
            message = conn.receive(timeout=left)
        except TimeoutError:
            return None
        header = message.header
        if (header.message_type is MessageType.method_call
                and header.fields.get(3) == CALLBACK_METHOD):      # MEMBER
            try:
                conn.send(new_method_return(message))
            except Exception:
                log.debug("could not acknowledge the KWin callback", exc_info=True)
            found = message.body[0] if message.body else ""
            return str(found)
        if header.message_type is MessageType.method_call:
            # Something else called us. Refuse it rather than leave it hanging:
            # this connection exists to receive one specific callback.
            try:
                conn.send(new_error(message, f"{CALLBACK_INTERFACE}.Unsupported"))
            except Exception:
                log.debug("could not refuse an unexpected call", exc_info=True)


class KWinWindowReader:
    """Reads the focused window from KWin, over a connection of its own.

    Its own, and not the daemon's other ones, because this connection
    *receives* a method call - the script's callback - and pulling messages off
    a connection something else is using would take replies that were not ours.

    Opened on first use and kept: a dictation asks once, and reconnecting per
    paste would put a bus round trip where there is no need for one. A
    connection that has died is dropped rather than retried in place, so the
    next call opens a fresh one instead of failing for ever.
    """

    def __init__(self, connect=None):
        self._connect, self._conn = connect, None

    def _open(self):
        if self._connect is not None:
            return self._connect()
        from jeepney.io.blocking import open_dbus_connection
        return open_dbus_connection(bus="SESSION")

    def __call__(self) -> str | None:
        if self._conn is None:
            try:
                self._conn = self._open()
            except Exception:
                log.debug("no session bus for the KWin reader", exc_info=True)
                return None
        try:
            return active_window_class(self._conn)
        except Exception:
            log.debug("the KWin reader's connection failed; dropping it", exc_info=True)
            self.close()
            return None

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                log.debug("could not close the KWin reader's connection", exc_info=True)
