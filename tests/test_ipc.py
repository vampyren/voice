import pytest

from voice import paths
from voice.ipc import IPCError, Server, is_running, send


def test_roundtrip_and_stale_socket_cleanup(isolated_xdg):
    paths.socket_path().write_text("stale")
    srv = Server(lambda req: {"ok": True, "echo": req["cmd"]})
    srv.start()
    try:
        assert is_running()
        assert send({"cmd": "status"}) == {"ok": True, "echo": "status"}
    finally:
        srv.stop()
    assert not paths.socket_path().exists()
    assert not is_running()


def test_handler_exception_becomes_error_reply(isolated_xdg):
    def boom(req):
        raise RuntimeError("bad")
    srv = Server(boom)
    srv.start()
    try:
        reply = send({"cmd": "x"})
        assert reply["ok"] is False and "bad" in reply["error"]
    finally:
        srv.stop()


def test_send_without_daemon_raises(isolated_xdg):
    with pytest.raises(IPCError, match="not running"):
        send({"cmd": "status"})
