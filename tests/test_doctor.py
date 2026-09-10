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


def test_overlay_probe_reports_the_helper_and_what_it_found(monkeypatch, isolated_xdg):
    from voice.doctor import default_probes
    from voice.ui.overlay_client import HelperProbe

    monkeypatch.setattr("voice.doctor.probe_helper", lambda: HelperProbe(
        ["/usr/bin/python3", "-m", "voice.ui.overlay"], ("gtk4",)))
    ok, detail = default_probes()["overlay"]()
    assert ok is True                                  # informational: never fails the run
    assert detail == ("enabled, helper via /usr/bin/python3 "
                      "(gtk4 ok, layer-shell absent - the pill stays off)")

    monkeypatch.setattr("voice.doctor.probe_helper", lambda: HelperProbe(
        ["/usr/bin/python3", "-m", "voice.ui.overlay"], ("gtk4", "layer-shell")))
    assert default_probes()["overlay"]()[1] == ("enabled, helper via /usr/bin/python3 "
                                                "(gtk4 ok, layer-shell ok)")


def test_overlay_probe_reports_a_missing_helper_and_a_switched_off_pill(monkeypatch, isolated_xdg):
    from voice.config import Config
    from voice.doctor import default_probes
    from voice.ui.overlay_client import HelperProbe

    monkeypatch.setattr("voice.doctor.probe_helper", lambda: HelperProbe(None, (), "no PyGObject"))
    assert default_probes()["overlay"]() == (True, "unavailable: no PyGObject")

    cfg = Config.load()
    cfg.set("ui.overlay", False)
    cfg.save()
    assert default_probes()["overlay"]() == (True, "disabled (ui.overlay = false)")


def test_overlay_probe_separates_an_unusable_layer_shell_from_a_missing_one(monkeypatch, isolated_xdg):
    """gtk4-layer-shell installed on GNOME still cannot make a layer surface:
    saying "layer-shell ok" there sends the owner looking in the wrong place."""
    from voice.config import Config
    from voice.doctor import default_probes
    from voice.ui.overlay_client import HelperProbe

    monkeypatch.setattr("voice.doctor.probe_helper", lambda: HelperProbe(
        ["/usr/bin/python3", "-m", "voice.ui.overlay"], ("gtk4", "layer-shell-unsupported")))
    detail = default_probes()["overlay"]()[1]
    assert "layer-shell: installed but unsupported by this compositor" in detail
    assert "the pill stays off" in detail

    cfg = Config.load()
    cfg.set("ui.overlay_allow_fallback", True)
    cfg.save()
    detail = default_probes()["overlay"]()[1]
    assert "layer-shell: installed but unsupported by this compositor" in detail
    assert "it will take focus" in detail


def test_the_probe_tells_an_unsupported_layer_shell_from_a_present_one():
    from voice.ui.overlay_client import HelperProbe

    unsupported = HelperProbe(["x"], ("gtk4", "layer-shell-unsupported"))
    assert unsupported.layer_shell is False
    assert unsupported.layer_shell_unsupported is True
    present = HelperProbe(["x"], ("gtk4", "layer-shell"))
    assert present.layer_shell is True and present.layer_shell_unsupported is False


def test_language_profiles_probe_lists_the_map(isolated_xdg):
    from voice.config import Config
    from voice.doctor import default_probes

    cfg = Config.load()
    cfg.set("general.language_profiles", {"en": "local", "sv": "openai"})
    cfg.save()
    assert default_probes()["language profiles"]() == (True, "en → local, sv → openai")


def test_language_profiles_probe_says_when_nothing_is_mapped(isolated_xdg):
    from voice.doctor import default_probes
    assert default_probes()["language profiles"]() == (
        True, "none: general.language_profiles is empty")


def test_language_profiles_probe_marks_a_profile_that_is_gone(isolated_xdg):
    """Informational, not required: `config` already fails on the same map."""
    from voice.config import Config
    from voice.doctor import default_probes

    cfg = Config.load()
    cfg.set("general.language_profiles", {"sv": "local-swedish"})
    cfg.save()
    assert default_probes()["language profiles"]() == (True, "sv → local-swedish (not defined)")
    assert "language profiles" not in __import__("voice.doctor", fromlist=["REQUIRED"]).REQUIRED


def _portal_here(monkeypatch):
    """Make the backend probe resolve to the portal without touching devices."""
    monkeypatch.setattr("voice.doctor._readable_keyboards", lambda: True)
    monkeypatch.setattr("voice.daemon.has_local_seat", lambda: False)


def test_portal_shortcuts_probe_reports_the_trigger_the_desktop_stored(monkeypatch, isolated_xdg):
    """GNOME keeps the trigger the user confirmed and does not follow a later
    change to hotkeys.portal_dictate; doctor is where that becomes visible."""
    from voice.doctor import GNOME_SHORTCUTS_KEY, default_probes

    _portal_here(monkeypatch)
    monkeypatch.setattr("voice.doctor.shutil.which", lambda b: f"/usr/bin/{b}")
    seen = []

    def fake_run(argv, **kwargs):
        seen.append(argv)
        return type("R", (), {"returncode": 0, "stdout": "[('dictate', {'shortcuts': <['F14']>})]\n"})()

    monkeypatch.setattr("voice.doctor.subprocess.run", fake_run)
    ok, detail = default_probes()["portal shortcuts"]()
    assert ok is True                                  # informational: never fails the run
    assert seen == [["dconf", "read", GNOME_SHORTCUTS_KEY]]
    assert "[('dictate', {'shortcuts': <['F14']>})]" in detail
    assert GNOME_SHORTCUTS_KEY in detail
    assert "portal shortcuts" not in __import__("voice.doctor", fromlist=["REQUIRED"]).REQUIRED


def test_portal_shortcuts_probe_says_where_to_change_it_without_dconf(monkeypatch, isolated_xdg):
    from voice.doctor import GNOME_SHORTCUTS_KEY, default_probes

    _portal_here(monkeypatch)
    monkeypatch.setattr("voice.doctor.shutil.which", lambda b: None)
    monkeypatch.setattr("voice.doctor.subprocess.run", _never_run)
    ok, detail = default_probes()["portal shortcuts"]()
    assert ok is True
    assert GNOME_SHORTCUTS_KEY in detail and "KDE" in detail


def test_portal_shortcuts_probe_is_quiet_on_the_evdev_backend(monkeypatch, isolated_xdg):
    monkeypatch.setattr("voice.doctor._readable_keyboards", lambda: True)
    monkeypatch.setattr("voice.daemon.has_local_seat", lambda: True)
    monkeypatch.setattr("voice.doctor.subprocess.run", _never_run)
    from voice.doctor import default_probes
    ok, detail = default_probes()["portal shortcuts"]()
    assert ok is True and "evdev" in detail


def test_the_stored_trigger_is_not_reported_when_dconf_has_no_key(monkeypatch, isolated_xdg):
    """A KDE machine (or a portal shortcut never confirmed) has no such key."""
    from voice.doctor import GNOME_SHORTCUTS_KEY, default_probes

    _portal_here(monkeypatch)
    monkeypatch.setattr("voice.doctor.shutil.which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr("voice.doctor.subprocess.run",
                        lambda argv, **kw: type("R", (), {"returncode": 0, "stdout": "\n"})())
    detail = default_probes()["portal shortcuts"]()[1]
    assert GNOME_SHORTCUTS_KEY in detail and "KDE" in detail


def _never_run(argv, **kwargs):
    raise AssertionError(f"doctor must not run {argv} here")
