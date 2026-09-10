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


def test_status_prints_the_active_hotkey_backend(isolated_xdg, capsys):
    srv = ipc.Server(lambda r: {"ok": True, "state": "idle", "profile": "local", "backend": "fake",
                                "last_error": None, "keyboard": True, "hotkey_backend": "portal"})
    srv.start()
    try:
        assert main(["status"]) == 0
        assert "hotkeys:   portal" in capsys.readouterr().out    # padded to "shortcuts:"
    finally:
        srv.stop()


def test_language_is_forwarded_and_the_result_printed(isolated_xdg, capsys):
    seen = []

    def handler(req):
        seen.append(req)
        if req["cmd"] == "status":                 # what `next` reads the result back with
            return {"ok": True, "language": "en"}
        return {"ok": True, "language": "sv" if req["code"] == "sv" else "auto"}

    srv = ipc.Server(handler)
    srv.start()
    try:
        assert main(["language", "sv"]) == 0
        assert main(["language", "next"]) == 0
    finally:
        srv.stop()
    assert [(r["cmd"], r.get("code")) for r in seen] == [
        ("language", "sv"), ("status", None), ("language", "next")]
    out = capsys.readouterr().out.splitlines()
    assert out == ["language: sv", "language: auto"]


def test_status_prints_the_active_language(isolated_xdg, capsys):
    srv = ipc.Server(lambda r: {"ok": True, "state": "idle", "profile": "local", "backend": "fake",
                                "last_error": None, "keyboard": True, "language": "sv"})
    srv.start()
    try:
        assert main(["status"]) == 0
        assert "language: sv" in capsys.readouterr().out
    finally:
        srv.stop()


def test_status_prints_what_the_overlay_is_doing(isolated_xdg, capsys):
    srv = ipc.Server(lambda r: {"ok": True, "state": "idle", "profile": "local", "backend": "fake",
                                "last_error": None, "keyboard": True,
                                "overlay": "disabled: no layer-shell"})
    srv.start()
    try:
        assert main(["status"]) == 0
        assert "overlay:  disabled: no layer-shell" in capsys.readouterr().out
    finally:
        srv.stop()


def test_a_pending_next_waits_until_the_language_actually_moves(isolated_xdg, capsys):
    """`next` is resolved on the daemon's Qt thread, so its reply cannot name
    the language and a single `status` read back races that thread: live, the
    CLI printed "en" while the daemon had already moved to "sv"."""
    seen = []
    answers = iter(["en", "en", "sv"])      # before the call, one racing read, then the switch
    current = {"language": "en"}

    def handler(req):
        seen.append(req["cmd"])
        if req["cmd"] == "status":
            current["language"] = next(answers, current["language"])
            return {"ok": True, "language": current["language"]}
        return {"ok": True, "language": "pending"}

    srv = ipc.Server(handler)
    srv.start()
    try:
        assert main(["language", "next"]) == 0
    finally:
        srv.stop()
    assert seen == ["status", "language", "status", "status"]
    assert capsys.readouterr().out.splitlines() == ["language: sv"]


def test_a_pending_next_that_never_moves_prints_what_it_last_read(isolated_xdg, capsys, monkeypatch):
    """A toggle can legitimately be a no-op (a one-entry cycle, an unusable
    entry). The poll is bounded and prints the language it last saw."""
    monkeypatch.setattr("voice.cli.LANGUAGE_POLL_TIMEOUT_S", 0.0)
    seen = []

    def handler(req):
        seen.append(req["cmd"])
        if req["cmd"] == "status":
            return {"ok": True, "language": "en"}
        return {"ok": True, "language": "pending"}

    srv = ipc.Server(handler)
    srv.start()
    try:
        assert main(["language", "next"]) == 0
    finally:
        srv.stop()
    assert seen == ["status", "language", "status"]
    assert capsys.readouterr().out.splitlines() == ["language: en"]


def test_a_named_language_needs_no_second_round_trip(isolated_xdg, capsys):
    seen = []
    srv = ipc.Server(lambda r: seen.append(r["cmd"]) or {"ok": True, "language": "sv"})
    srv.start()
    try:
        assert main(["language", "sv"]) == 0
    finally:
        srv.stop()
    assert seen == ["language"]
    assert capsys.readouterr().out.splitlines() == ["language: sv"]


@pytest.mark.parametrize("bound,expected", [
    (True, "shortcuts: bound"),
    (False, "shortcuts: NOT BOUND (accept the desktop's shortcut dialog)"),
    (None, "shortcuts: waiting for the desktop"),
])
def test_status_talks_about_shortcuts_on_the_portal_backend(isolated_xdg, capsys, bound, expected):
    """With the portal there is no keyboard to have access to: the desktop
    either bound our shortcuts or it did not."""
    srv = ipc.Server(lambda r: {"ok": True, "state": "idle", "profile": "local", "backend": "fake",
                                "last_error": None, "keyboard": bound, "language": "en",
                                "hotkey_backend": "portal"})
    srv.start()
    try:
        assert main(["status"]) == 0
    finally:
        srv.stop()
    out = capsys.readouterr().out
    assert expected in out
    assert "keyboard:" not in out


def test_status_still_reports_keyboard_access_on_the_evdev_backend(isolated_xdg, capsys):
    srv = ipc.Server(lambda r: {"ok": True, "state": "idle", "profile": "local", "backend": "fake",
                                "last_error": None, "keyboard": False, "language": "en",
                                "hotkey_backend": "evdev"})
    srv.start()
    try:
        assert main(["status"]) == 0
    finally:
        srv.stop()
    out = capsys.readouterr().out
    assert "keyboard: NO ACCESS" in out
    assert "shortcuts:" not in out


@pytest.mark.parametrize("state,triggers,expected", [
    ("bound", {"dictate": "F13"}, "shortcuts: bound (dictate=F13)"),
    ("unassigned", {"dictate": ""},
     "shortcuts: registered, no key assigned \u2014 assign it in your desktop's keyboard settings"),
    ("unassigned", {"dictate": "", "language_toggle": "F14"},
     "no key assigned \u2014 assign it in your desktop's keyboard settings (dictate)"),
    ("denied", {}, "shortcuts: NOT BOUND (accept the desktop's shortcut dialog)"),
])
def test_status_separates_a_bound_shortcut_from_one_with_no_key(isolated_xdg, capsys, state,
                                                                triggers, expected):
    """"Registered" is not "bound": GNOME answers BindShortcuts with success and
    attaches no key, and reporting that as bound is why two keys did nothing."""
    srv = ipc.Server(lambda r: {"ok": True, "state": "idle", "profile": "local", "backend": "fake",
                                "last_error": None, "keyboard": state == "bound", "language": "en",
                                "hotkey_backend": "portal", "shortcut_state": state,
                                "shortcut_triggers": triggers})
    srv.start()
    try:
        assert main(["status"]) == 0
    finally:
        srv.stop()
    assert expected in capsys.readouterr().out


def _value_columns(out: str) -> list[int]:
    """The column each printed value starts in, one entry per status line."""
    columns = []
    for line in out.splitlines():
        label, sep, rest = line.partition(":")
        assert sep, f"not a status row: {line!r}"
        columns.append(len(label) + 1 + len(rest) - len(rest.lstrip()))
    return columns


@pytest.mark.parametrize("backend", ["evdev", "portal"])
def test_status_values_line_up_whichever_rows_are_printed(isolated_xdg, capsys, backend):
    """Every label was padded to 10 columns, but "shortcuts:" is 10 characters
    itself, so the portal rendering put its value one column right of the rest."""
    srv = ipc.Server(lambda r: {"ok": True, "state": "idle", "profile": "local",
                                "backend": "fake", "language": "en", "keyboard": True,
                                "overlay": "running", "hotkey_backend": backend,
                                "last_error": "boom"})
    srv.start()
    try:
        assert main(["status"]) == 0
    finally:
        srv.stop()
    out = capsys.readouterr().out
    assert len(set(_value_columns(out))) == 1, out


def test_status_names_the_language_that_chose_the_profile(isolated_xdg, capsys):
    srv = ipc.Server(lambda r: {"ok": True, "state": "idle", "profile": "local-swedish",
                                "profile_language": "sv", "backend": "fake", "last_error": None})
    srv.start()
    try:
        assert main(["status"]) == 0
        assert "profile:  local-swedish (for sv)" in capsys.readouterr().out
    finally:
        srv.stop()


def test_status_does_not_invent_a_language_for_a_hand_picked_profile(isolated_xdg, capsys):
    srv = ipc.Server(lambda r: {"ok": True, "state": "idle", "profile": "groq",
                                "profile_language": None, "backend": "fake", "last_error": None})
    srv.start()
    try:
        assert main(["status"]) == 0
        out = capsys.readouterr().out
        assert "profile:  groq\n" in out and "(for" not in out
    finally:
        srv.stop()
