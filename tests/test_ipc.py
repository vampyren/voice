import socket
import threading
import time

import pytest

from voice import ipc, paths
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


def test_silent_connection_does_not_wedge_the_server(isolated_xdg, monkeypatch):
    # A client that connects and never writes must not block the single-threaded
    # accept loop forever - the server drops it after CLIENT_READ_TIMEOUT_S and
    # keeps serving the next connection.
    monkeypatch.setattr(ipc, "CLIENT_READ_TIMEOUT_S", 0.5)
    srv = Server(lambda req: {"ok": True, "echo": req["cmd"]})
    srv.start()
    silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        silent.connect(str(paths.socket_path()))
        # Never send anything on `silent` - the server's read must time out on
        # its own rather than hanging until this socket is closed.
        reply = send({"cmd": "status"}, timeout=8)
        assert reply == {"ok": True, "echo": "status"}
    finally:
        silent.close()
        srv.stop()


def test_is_running_is_true_while_the_only_handler_thread_is_busy(isolated_xdg):
    # A listening socket accepts into the backlog even while the single-threaded
    # server is stuck in a handler, so a busy daemon must never read as "not running".
    entered, release = threading.Event(), threading.Event()

    def slow(req):
        entered.set()
        release.wait(5)
        return {"ok": True}

    srv = Server(slow)
    srv.start()
    caller = threading.Thread(target=lambda: send({"cmd": "status"}, timeout=10), daemon=True)
    try:
        caller.start()
        assert entered.wait(2)
        began = time.monotonic()
        assert is_running() is True
        assert time.monotonic() - began < 0.5
    finally:
        release.set()
        caller.join(timeout=5)
        srv.stop()


def test_server_start_refuses_to_unlink_a_live_socket(isolated_xdg):
    srv = Server(lambda req: {"ok": True, "echo": req.get("cmd")})
    srv.start()
    try:
        with pytest.raises(IPCError, match="already running"):
            Server(lambda req: {"ok": True}).start()
        assert send({"cmd": "ping"}) == {"ok": True, "echo": "ping"}   # first server untouched
    finally:
        srv.stop()
