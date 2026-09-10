"""Shared jeepney plumbing for xdg-desktop-portal's request/response pattern.

Every portal method that can show a dialog returns a Request object path and
answers later with a `Response` signal on it. Both the RemoteDesktop injector
(`voice.inject.portal`) and the GlobalShortcuts hotkey listener
(`voice.hotkey.portal_listener`) drive that same handshake, so it lives here
once: derive the request path from our unique bus name and the handle_token we
chose, subscribe *before* the call (the reply can beat the AddMatch otherwise),
then wait for the Response.
"""
from __future__ import annotations

import logging
import secrets

from jeepney import DBusAddress, MatchRule, Message, new_method_call
from jeepney.bus_messages import message_bus

log = logging.getLogger(__name__)

PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_BUS = "org.freedesktop.portal.Desktop"
REQUEST_INTERFACE = "org.freedesktop.portal.Request"
RESPONSE_TIMEOUT = 120


class PortalError(Exception):
    """A portal method answered with a D-Bus error reply."""


def portal_address(interface: str) -> DBusAddress:
    return DBusAddress(PORTAL_PATH, bus_name=PORTAL_BUS, interface=interface)


def request_path(unique_name: str, token: str) -> str:
    return f"{PORTAL_PATH}/request/{unique_name.lstrip(':').replace('.', '_')}/{token}"


def new_token() -> str:
    """A handle_token: unique per request, and never reused across daemon runs."""
    return "voice" + secrets.token_hex(4)


def call_with_response(conn, address: DBusAddress, method: str, signature: str, args: tuple,
                       timeout: float = RESPONSE_TIMEOUT) -> tuple[int, dict]:
    """Call a portal method and return its `(response code, results)`.

    `args` must end with the options dict; a fresh handle_token is added to it so
    the Request path is known before the call goes out. Raises PortalError if the
    method itself fails (jeepney returns error replies rather than raising).
    """
    token = new_token()
    path = request_path(conn.unique_name, token)
    rule = MatchRule(type="signal", interface=REQUEST_INTERFACE, member="Response", path=path)
    conn.send_and_get_reply(message_bus.AddMatch(rule))
    with conn.filter(rule) as queue:
        opts = dict(args[-1])
        opts["handle_token"] = ("s", token)
        reply = conn.send_and_get_reply(new_method_call(address, method, signature, (*args[:-1], opts)))
        if reply.header.message_type.name == "error":
            raise PortalError(f"portal {method} failed: {reply.body}")
        msg: Message = conn.recv_until_filtered(queue, timeout=timeout)
    code, results = msg.body
    return code, results
