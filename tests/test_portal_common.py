from types import SimpleNamespace

import pytest
from jeepney import HeaderFields

from voice.portal_common import (PortalError, call_with_response, new_token, portal_address,
                                 request_path)


def test_request_path_escapes_unique_name():
    assert request_path(":1.42", "tok1") == "/org/freedesktop/portal/desktop/request/1_42/tok1"


def test_new_token_is_unique_and_a_valid_object_path_element():
    tokens = {new_token() for _ in range(50)}
    assert len(tokens) == 50
    assert all(t.isidentifier() for t in tokens)      # object paths allow [A-Za-z0-9_] only


class _Reply:
    def __init__(self, kind="method_return", body=()):
        self.header = SimpleNamespace(message_type=SimpleNamespace(name=kind))
        self.body = body


class _Conn:
    """Enough of a jeepney connection to drive call_with_response."""

    def __init__(self, error=False, code=0):
        self.unique_name = ":1.5"
        self.error, self.code = error, code
        self.calls: list[tuple[str, tuple]] = []
        self.rules: list = []

    def send_and_get_reply(self, msg, timeout=None):
        member = msg.header.fields.get(HeaderFields.member)
        self.calls.append((member, msg.body))
        if member == "AddMatch":
            self.rules.append(msg.body[0])
            return _Reply()
        return _Reply("error" if self.error else "method_return", ("boom",))

    def filter(self, rule, **kwargs):
        self.rules.append(rule)
        return _Ctx()

    def recv_until_filtered(self, queue, timeout=None):
        return SimpleNamespace(body=(self.code, {"session_handle": ("o", "/s/1")}))


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_call_with_response_adds_a_handle_token_and_returns_the_response():
    conn = _Conn()
    address = portal_address("org.freedesktop.portal.GlobalShortcuts")
    code, results = call_with_response(conn, address, "CreateSession", "a{sv}", ({"x": ("s", "y")},))
    assert (code, results) == (0, {"session_handle": ("o", "/s/1")})
    member, body = conn.calls[-1]
    assert member == "CreateSession"
    token = body[-1]["handle_token"][1]
    assert body[-1]["x"] == ("s", "y")                       # caller options are preserved
    # The match rule must name the request path derived from that very token,
    # and must have been installed before the call went out.
    assert any(request_path(":1.5", token) in str(r) for r in conn.rules)
    assert conn.calls[0][0] == "AddMatch"


def test_call_with_response_raises_on_an_error_reply():
    with pytest.raises(PortalError, match="portal CreateSession failed"):
        call_with_response(_Conn(error=True), portal_address("x.y"), "CreateSession", "a{sv}", ({},))
