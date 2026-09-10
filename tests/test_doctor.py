from voice.doctor import Check, run_checks, run_doctor


def test_run_checks_collects_results_and_catches_exceptions():
    def boom():
        raise RuntimeError("kaput")
    checks = run_checks({"a": lambda: (True, "fine"), "b": boom})
    assert checks == [Check("a", True, "fine"), Check("b", False, "kaput")]


def test_run_doctor_exit_code_depends_on_required(monkeypatch, capsys):
    monkeypatch.setattr("voice.doctor.default_probes", lambda: {
        "portal": lambda: (True, "v2"), "wl-clipboard": lambda: (True, ""), "pw-record": lambda: (True, ""),
        "keyboard access": lambda: (True, "2 devices"), "config": lambda: (True, ""), "cuda": lambda: (False, "no gpu")})
    assert run_doctor() == 0
    out = capsys.readouterr().out
    assert "✔ portal" in out and "✘ cuda" in out and "optional" in out
    monkeypatch.setattr("voice.doctor.default_probes", lambda: {"portal": lambda: (False, "missing")})
    assert run_doctor() == 1


def test_wl_clipboard_probe_reports_which_binary_is_missing(monkeypatch):
    from voice.doctor import default_probes

    monkeypatch.setattr("voice.doctor.shutil.which", lambda b: "/usr/bin/wl-copy" if b == "wl-copy" else None)
    ok, detail = default_probes()["wl-clipboard"]()
    assert ok is False
    assert detail == "install wl-clipboard (missing: wl-paste)"

    monkeypatch.setattr("voice.doctor.shutil.which", lambda b: f"/usr/bin/{b}")
    ok, detail = default_probes()["wl-clipboard"]()
    assert ok is True and detail == "wl-copy/wl-paste"


def test_hotkey_backend_probe_explains_why_the_portal_was_chosen(monkeypatch, isolated_xdg):
    from voice.doctor import default_probes

    monkeypatch.setattr("voice.doctor._readable_keyboards", lambda: True)
    monkeypatch.setattr("voice.daemon.has_local_seat", lambda: False)
    ok, detail = default_probes()["hotkey backend"]()
    assert ok is True                                  # informational: never fails the run
    assert detail == "portal (no local seat)"

    monkeypatch.setattr("voice.doctor._readable_keyboards", lambda: False)
    assert default_probes()["hotkey backend"]()[1] == "portal (no readable keyboards, no local seat)"

    monkeypatch.setattr("voice.daemon.has_local_seat", lambda: True)
    monkeypatch.setattr("voice.doctor._readable_keyboards", lambda: True)
    assert default_probes()["hotkey backend"]()[1] == "evdev"


def test_hotkey_backend_probe_reports_a_forced_setting(monkeypatch, isolated_xdg):
    from voice.config import Config
    from voice.doctor import default_probes

    cfg = Config.load()
    cfg.set("hotkeys.backend", "portal")
    cfg.save()
    monkeypatch.setattr("voice.doctor._readable_keyboards", lambda: True)
    monkeypatch.setattr("voice.daemon.has_local_seat", lambda: True)
    assert default_probes()["hotkey backend"]() == (True, "portal (set in config)")


def test_hotkey_backend_probe_does_not_call_a_bogus_setting_deliberate(monkeypatch, isolated_xdg):
    """An unusable hotkeys.backend falls back to auto, so it must not read as chosen."""
    from voice.config import Config
    from voice.doctor import default_probes

    cfg = Config.load()
    cfg.set("hotkeys.backend", "telepathy")
    cfg.save()
    monkeypatch.setattr("voice.doctor._readable_keyboards", lambda: True)
    monkeypatch.setattr("voice.daemon.has_local_seat", lambda: False)
    assert default_probes()["hotkey backend"]() == (True, "portal (no local seat)")
