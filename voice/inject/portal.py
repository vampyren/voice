"""xdg-desktop-portal RemoteDesktop keyboard injection (works on KDE and GNOME)."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from jeepney import new_method_call
from jeepney.io.blocking import open_dbus_connection

from voice import APP_ID, paths
from voice.inject.keys import KeySendError
from voice.portal_common import PortalError, call_with_response, new_token, portal_address

log = logging.getLogger(__name__)

PORTAL = portal_address("org.freedesktop.portal.RemoteDesktop")
PROPS = PORTAL.with_interface("org.freedesktop.DBus.Properties")
KEYBOARD = 1
PERSIST_UNTIL_REVOKED = 2


def select_devices_options(token: str | None) -> dict:
    opts = {"types": ("u", KEYBOARD), "persist_mode": ("u", PERSIST_UNTIL_REVOKED)}
    if token:
        opts["restore_token"] = ("s", token)
    return opts


class TokenStore:
    def __init__(self, path: Path | None = None):
        self._path = path or paths.portal_token_file()

    def load(self) -> str | None:
        return self._path.read_text().strip() or None if self._path.exists() else None

    def save(self, token: str) -> None:
        self._path.write_text(token)
        self._path.chmod(0o600)

    def clear(self) -> None:
        if self._path.exists():
            self._path.unlink()


def portal_available(bus_factory: Callable = open_dbus_connection) -> bool:
    try:
        conn = bus_factory(bus="SESSION")
        try:
            reply = conn.send_and_get_reply(new_method_call(PROPS, "Get", "ss", (PORTAL.interface, "version")))
            return int(reply.body[0][1]) >= 2
        finally:
            conn.close()
    except Exception as exc:
        log.info("portal unavailable: %s", exc)
        return False


class PortalKeySender:
    name = "portal"

    def __init__(self, token_store: TokenStore | None = None, bus_factory: Callable = open_dbus_connection):
        self._tokens = token_store or TokenStore()
        self._bus_factory = bus_factory
        self._conn = None
        self._session: str | None = None

    def available(self) -> bool:
        return portal_available(self._bus_factory)

    # -- request/response helper -----------------------------------------
    def _call(self, method: str, signature: str, args: tuple) -> dict:
        try:
            code, results = call_with_response(self._conn, PORTAL, method, signature, args)
        except PortalError as exc:
            raise KeySendError(str(exc)) from exc
        if code != 0:
            raise KeySendError(f"portal {method} denied (response {code}); allow '{APP_ID}' in system settings")
        return results

    def _open(self) -> None:
        try:
            self._conn = self._bus_factory(bus="SESSION")
            results = self._call("CreateSession", "a{sv}", ({"session_handle_token": ("s", new_token())},))
            self._session = results["session_handle"][1]
            self._call("SelectDevices", "oa{sv}", (self._session, select_devices_options(self._tokens.load())))
            results = self._call("Start", "osa{sv}", (self._session, "", {}))
        except BaseException:
            # A half-open session (denied dialog, dropped bus) must not be left
            # behind: send_chord would then skip _open() and notify into nothing.
            self.close()
            raise
        token = results.get("restore_token")
        if token:
            self._tokens.save(token[1])
        log.info("portal remote desktop session ready")

    def _notify(self, keycode: int, state: int) -> None:
        # jeepney returns error replies rather than raising, so an injection into a
        # revoked or dead session would otherwise read as success. RuntimeError (not
        # KeySendError) so send_chord's retry path closes and re-opens the session once.
        reply = self._conn.send_and_get_reply(
            new_method_call(PORTAL, "NotifyKeyboardKeycode", "oa{sv}iu", (self._session, {}, keycode, state)))
        if reply.header.message_type.name == "error":
            raise RuntimeError(f"portal NotifyKeyboardKeycode failed: {reply.body}")

    def send_chord(self, keycodes: list[int]) -> None:
        for attempt in (1, 2):
            try:
                if self._session is None:
                    self._open()
                for code in keycodes:
                    self._notify(code, 1)
                    time.sleep(0.01)
                for code in reversed(keycodes):
                    self._notify(code, 0)
                    time.sleep(0.01)
                return
            except KeySendError:
                raise
            except Exception as exc:               # session died (suspend, portal restart): retry once
                log.warning("portal send failed (attempt %d): %s", attempt, exc)
                self.close()
                if attempt == 2:
                    raise KeySendError(f"portal keyboard injection failed: {exc}") from exc

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn, self._session = None, None
