import os
import stat

import pytest

from voice import paths
from voice.config import DEFAULT_CONFIG, Config


def test_load_creates_default_file_with_0600(isolated_xdg):
    cfg = Config.load()
    assert cfg.path == paths.config_file()
    assert cfg.path.exists()
    assert stat.S_IMODE(cfg.path.stat().st_mode) == 0o600
    assert cfg.get("hotkeys.dictate") == "KEY_F13"
    assert cfg.get("stt.active") == "local"


def test_set_and_save_preserves_comments(isolated_xdg):
    cfg = Config.load()
    cfg.set("hotkeys.dictate", "KEY_RIGHTCTRL")
    cfg.set("general.language", "sv")
    cfg.save()
    text = cfg.path.read_text()
    assert 'dictate = "KEY_RIGHTCTRL"' in text
    assert "# any evdev key" in text          # comment survived
    again = Config.load()
    assert again.get("hotkeys.dictate") == "KEY_RIGHTCTRL"
    assert again.get("general.language") == "sv"


def test_get_missing_returns_default(isolated_xdg):
    cfg = Config.load()
    assert cfg.get("nope.missing", 42) == 42


def test_stt_profile_returns_active_profile_dict(isolated_xdg):
    cfg = Config.load()
    cfg.set("stt.active", "openai")
    name, profile = cfg.stt_profile()
    assert name == "openai"
    assert profile["backend"] == "openai_compatible"
    assert profile["base_url"] == "https://api.openai.com/v1"


def test_secret_prefers_inline_then_env(isolated_xdg, monkeypatch):
    cfg = Config.load()
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    assert cfg.secret({"api_key_env": "OPENAI_API_KEY"}) == "from-env"
    assert cfg.secret({"api_key": "inline", "api_key_env": "OPENAI_API_KEY"}) == "inline"
    assert cfg.secret({}) is None


def test_errors_reports_bad_values(isolated_xdg):
    cfg = Config.load()
    cfg.set("hotkeys.dictate_mode", "sometimes")
    cfg.set("stt.active", "ghost")
    errs = cfg.errors()
    assert any("dictate_mode" in e for e in errs)
    assert any("ghost" in e for e in errs)


def test_load_with_invalid_toml_raises_clear_error(isolated_xdg):
    paths.config_file().write_text("this = [unclosed")
    with pytest.raises(ValueError, match="config.toml"):
        Config.load()


def test_errors_reports_unparseable_paste_chords(isolated_xdg):
    cfg = Config.load()
    cfg.set("inject.paste_chord", "hyper+v")
    cfg.set("inject.terminal_chord", "ctrl+shift+nope")
    errs = cfg.errors()
    assert any("inject.paste_chord" in e and "hyper" in e for e in errs)
    assert any("inject.terminal_chord" in e and "nope" in e for e in errs)


def test_errors_accepts_the_default_chords(isolated_xdg):
    assert [e for e in Config.load().errors() if "chord" in e] == []


def test_save_is_atomic_and_leaves_no_temp_file(isolated_xdg):
    cfg = Config.load()
    cfg.set("general.language", "sv")
    cfg.save()
    assert stat.S_IMODE(cfg.path.stat().st_mode) == 0o600
    assert sorted(p.name for p in cfg.path.parent.iterdir()) == [cfg.path.name]
    assert Config.load().get("general.language") == "sv"


def test_a_failed_save_leaves_the_previous_file_intact(isolated_xdg, monkeypatch):
    import voice.config as config_mod

    cfg = Config.load()
    original = cfg.path.read_text()
    cfg.set("general.language", "sv")

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(config_mod.os, "replace", boom)
    with pytest.raises(OSError):
        cfg.save()
    assert cfg.path.read_text() == original                       # never half-written
    assert sorted(p.name for p in cfg.path.parent.iterdir()) == [cfg.path.name]


def test_config_accessors_are_serialised(isolated_xdg, monkeypatch):
    # The settings dialog (Qt thread), the IPC handler and the pipeline worker all
    # reach the same Config; a read must not observe a half-applied write.
    import threading

    import voice.config as config_mod

    entered, release, read_done = threading.Event(), threading.Event(), threading.Event()
    cfg = Config.load()
    real_dumps = config_mod.tomlkit.dumps

    def slow_dumps(doc):
        entered.set()
        release.wait(5)
        return real_dumps(doc)

    monkeypatch.setattr(config_mod.tomlkit, "dumps", slow_dumps)
    saver = threading.Thread(target=cfg.save, daemon=True)
    saver.start()
    assert entered.wait(2)

    reader = threading.Thread(target=lambda: (cfg.get("general.language"), read_done.set()), daemon=True)
    reader.start()
    assert not read_done.wait(0.3)      # blocked behind the in-flight save
    release.set()
    assert read_done.wait(2)
    saver.join(2)
    reader.join(2)


def test_errors_reports_an_empty_paste_chord(isolated_xdg):
    cfg = Config.load()
    cfg.set("inject.paste_chord", "")
    assert any("inject.paste_chord" in e and "empty chord" in e for e in cfg.errors())


def test_save_closes_the_temp_descriptor_when_chmod_fails(isolated_xdg, monkeypatch):
    # mkstemp hands back a raw fd; anything raising before it is wrapped in a file
    # object leaks it for the life of the daemon.
    import voice.config as config_mod

    cfg = Config.load()
    original = cfg.path.read_text()
    captured = {}
    real_mkstemp = config_mod.tempfile.mkstemp

    def spy_mkstemp(*a, **kw):
        fd, tmp = real_mkstemp(*a, **kw)
        captured["fd"], captured["tmp"] = fd, tmp
        return fd, tmp

    def boom(*a, **kw):
        raise OSError("chmod refused")

    monkeypatch.setattr(config_mod.tempfile, "mkstemp", spy_mkstemp)
    monkeypatch.setattr(config_mod.os, "fchmod", boom)
    cfg.set("general.language", "sv")
    with pytest.raises(OSError, match="chmod refused"):
        cfg.save()

    with pytest.raises(OSError):
        os.fstat(captured["fd"])                  # descriptor closed, not leaked
    assert not os.path.exists(captured["tmp"])
    assert cfg.path.read_text() == original


def test_defaults_include_the_portal_hotkey_settings(isolated_xdg):
    cfg = Config.load()
    assert cfg.get("hotkeys.backend") == "auto"
    assert cfg.get("hotkeys.portal_dictate") == "CTRL+space"
    assert cfg.get("hotkeys.portal_recall") == ""
    assert cfg.get("hotkeys.portal_cancel") == ""
    assert cfg.errors() == []


def test_errors_rejects_an_unknown_hotkey_backend(isolated_xdg):
    cfg = Config.load()
    cfg.set("hotkeys.backend", "wayland-magic")
    assert any("hotkeys.backend" in e and "auto" in e for e in cfg.errors())


def test_errors_wants_a_portal_trigger_when_the_portal_backend_is_forced(isolated_xdg):
    cfg = Config.load()
    cfg.set("hotkeys.backend", "portal")
    cfg.set("hotkeys.portal_dictate", "")
    assert any("hotkeys.portal_dictate" in e for e in cfg.errors())
    cfg.set("hotkeys.backend", "evdev")
    assert [e for e in cfg.errors() if "portal_dictate" in e] == []


def test_readme_shows_the_current_defaults():
    """The README prints config.toml verbatim; drift there misinforms every new user."""
    import re
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    block = re.search(r"```toml\n(# voice configuration.*?)```", readme, re.S)
    assert block, "the README no longer contains the default config block"
    assert block.group(1) == DEFAULT_CONFIG
