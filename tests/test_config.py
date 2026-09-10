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
