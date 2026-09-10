import pytest

from voice import ipc
from voice.cli import main


def test_status_without_daemon_exits_1(isolated_xdg, capsys):
    assert main(["status"]) == 1
    assert "not running" in capsys.readouterr().err


def test_commands_are_forwarded_to_daemon(isolated_xdg, capsys):
    seen = []

    def handler(req):
        seen.append(req)
        return {"ok": True, "state": "idle", "profile": "local", "backend": "fake", "last_error": None}

    srv = ipc.Server(handler)
    srv.start()
    try:
        assert main(["toggle"]) == 0
        assert main(["profile", "openai"]) == 0
        assert main(["status"]) == 0
        out = capsys.readouterr().out
        assert "idle" in out and "local" in out
    finally:
        srv.stop()
    assert [r["cmd"] for r in seen] == ["toggle", "profile", "status"]
    assert seen[1]["name"] == "openai"


def test_error_reply_exits_1(isolated_xdg, capsys):
    srv = ipc.Server(lambda r: {"ok": False, "error": "no such profile"})
    srv.start()
    try:
        assert main(["profile", "x"]) == 1
        assert "no such profile" in capsys.readouterr().err
    finally:
        srv.stop()
