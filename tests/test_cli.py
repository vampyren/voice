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


def test_status_reports_unknown_keyboard_state_distinct_from_no_access(isolated_xdg, capsys):
    srv = ipc.Server(lambda r: {"ok": True, "state": "idle", "profile": "local", "backend": "fake",
                                "last_error": None, "keyboard": None})
    srv.start()
    try:
        assert main(["status"]) == 0
        out = capsys.readouterr().out
        assert "keyboard: unknown" in out
        assert "NO ACCESS" not in out
    finally:
        srv.stop()


def test_daemon_subcommand_is_noop_when_already_running(isolated_xdg, monkeypatch):
    seen = []

    def handler(req):
        seen.append(req)
        return {"ok": True}

    def fail_if_called():
        raise AssertionError("a second daemon must not be started while one is already running")

    monkeypatch.setattr("voice.daemon.main", fail_if_called)
    srv = ipc.Server(handler)
    srv.start()
    try:
        assert main(["daemon"]) == 0
    finally:
        srv.stop()
    assert seen and seen[-1]["cmd"] == "settings"
