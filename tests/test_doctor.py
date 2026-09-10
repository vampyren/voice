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
