import os
import stat

import pytest

from voice import paths
from voice.config import DEFAULT_CONFIG, Config
from voice.ui.placement import POSITIONS


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


def test_the_default_config_calls_the_portal_triggers_a_first_run_preference():
    """The comment used to read like a setting. It is not one: on GNOME these
    keys are never applied at all, and a user who believes the comment spends
    the evening editing a field that cannot move their binding."""
    lines = DEFAULT_CONFIG.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("portal_dictate"))
    above = []
    row = start - 1
    while row >= 0 and lines[row].lstrip().startswith("#"):
        above.append(lines[row])
        row -= 1
    comment = " ".join(reversed(above)).lower()
    assert "first-run preference" in comment
    assert "gnome" in comment                       # where it is never applied
    assert "keyboard shortcuts" in comment          # and where the key is really set


def test_readme_shows_the_current_defaults():
    """The README prints config.toml verbatim; drift there misinforms every new user."""
    import re
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    block = re.search(r"```toml\n(# voice configuration.*?)```", readme, re.S)
    assert block, "the README no longer contains the default config block"
    assert block.group(1) == DEFAULT_CONFIG


LEGACY_CONFIG = """
[hotkeys]
dictate = "KEY_F13"
dictate_mode = "hold"
recall = ""
cancel = "KEY_ESC"

[stt]
active = "local"

[stt.profiles.local]
backend = "local"
model = "large-v3-turbo"

[audio]
max_seconds = 120
"""


def test_portal_trigger_falls_back_for_a_config_written_before_this_feature(isolated_xdg):
    """A pre-existing config.toml has no hotkeys.portal_* keys at all; falling back
    to the shipped default is what keeps an upgrade from binding nothing."""
    from voice import paths
    paths.config_file().write_text(LEGACY_CONFIG)
    cfg = Config.load()
    assert cfg.get("hotkeys.portal_dictate") is None
    assert cfg.portal_trigger("dictate") == "CTRL+space"
    assert cfg.portal_trigger("cancel") == ""
    assert [e for e in cfg.errors() if "portal" in e] == []


def test_portal_trigger_keeps_an_explicit_empty_value(isolated_xdg):
    cfg = Config.load()
    cfg.set("hotkeys.portal_dictate", "  ")
    assert cfg.portal_trigger("dictate") == ""


def test_errors_wants_a_dictate_trigger_whenever_the_portal_can_be_chosen(isolated_xdg):
    """backend = "auto" can resolve to portal, so an empty trigger must not pass."""
    cfg = Config.load()
    cfg.set("hotkeys.portal_dictate", "")
    for backend in ("auto", "portal"):
        cfg.set("hotkeys.backend", backend)
        assert any("hotkeys.portal_dictate" in e for e in cfg.errors()), backend
    cfg.set("hotkeys.backend", "evdev")
    assert [e for e in cfg.errors() if "portal_dictate" in e] == []


def test_defaults_carry_the_language_cycle_and_the_overlay(isolated_xdg):
    cfg = Config.load()
    assert cfg.get("general.languages") == ["en", "sv"]
    assert cfg.languages() == ["en", "sv"]
    assert cfg.get("ui.overlay") is True
    assert cfg.get("ui.overlay_position") == "bottom-center"
    assert cfg.get("ui.overlay_margin_x") == 0
    assert cfg.get("ui.overlay_margin_y") == 48
    assert cfg.get("ui.overlay_allow_fallback") is False
    # Off by default: it moves the pill where the compositor would not, and
    # charges for it in clicks that land on the padding around it.
    assert cfg.get("ui.overlay_pad_to_place") is False
    assert cfg.get("hotkeys.language_toggle") == ""
    assert cfg.portal_trigger("language_toggle") == ""
    assert cfg.errors() == []


@pytest.mark.parametrize("language,languages,expected", [
    ("en", ["en", "sv"], None),
    ("auto", ["auto", "sv"], None),
    ("EN", ["en"], None),                                  # case is not the point
    ("english", ["en"], "general.language"),
    ("e", ["en"], "general.language"),
    ("", ["en"], "general.language"),
    ("en", [], "general.languages"),
    ("en", "sv", "general.languages"),                     # not a list
    ("en", ["en", "svenska"], "general.languages"),
    ("en", ["en", 7], "general.languages"),
])
def test_language_settings_are_validated(isolated_xdg, language, languages, expected):
    cfg = Config.load()
    cfg.set("general.language", language)
    cfg.set("general.languages", languages)
    errs = [e for e in cfg.errors() if "language" in e]
    if expected is None:
        assert errs == []
    else:
        assert any(e.startswith(expected) for e in errs), errs


def test_languages_falls_back_for_a_config_written_before_the_toggle(isolated_xdg):
    paths.config_file().write_text('[general]\nlanguage = "sv"\n')
    cfg = Config.load()
    assert cfg.languages() == ["sv"]           # the one language it knows about
    assert [e for e in cfg.errors() if "languages" in e] == []


def test_language_profiles_are_empty_until_the_user_uncomments_them(isolated_xdg):
    """The shipped table is a commented example: an upgrade must change nothing."""
    cfg = Config.load()
    assert cfg.get("general.language_profiles") == {}
    assert cfg.language_profiles() == {}
    assert cfg.profile_for_language("sv") is None
    assert cfg.errors() == []
    text = cfg.path.read_text()
    assert '# en = "local"' in text
    assert '# sv = "local-swedish"' in text


def test_language_profiles_map_languages_to_profiles(isolated_xdg):
    cfg = Config.load()
    cfg.set("general.language_profiles", {"EN": "local", "sv": "openai"})
    assert cfg.language_profiles() == {"en": "local", "sv": "openai"}
    assert cfg.profile_for_language("SV") == "openai"
    assert cfg.profile_for_language("de") is None
    assert cfg.errors() == []


@pytest.mark.parametrize("mapping,expected", [
    ({}, None),
    ({"en": "local", "sv": "openai"}, None),
    ({"auto": "local"}, None),                             # "auto" may map too
    ({"sv": "ghost"}, "general.language_profiles.sv"),     # unknown profile
    ({"sv": 7}, "general.language_profiles.sv"),           # not even a name
    ({"svenska": "local"}, "general.language_profiles"),   # not a language code
])
def test_language_profile_map_is_validated(isolated_xdg, mapping, expected):
    cfg = Config.load()
    cfg.set("general.language_profiles", mapping)
    errs = [e for e in cfg.errors() if "language_profiles" in e]
    if expected is None:
        assert errs == []
    else:
        assert any(e.startswith(expected) for e in errs), errs


def test_a_language_profile_map_that_is_not_a_table_is_rejected(isolated_xdg):
    cfg = Config.load()
    cfg.set("general.language_profiles", "local")
    assert any("general.language_profiles must be a table" in e for e in cfg.errors())


def test_an_empty_mapping_value_means_no_profile_for_that_language(isolated_xdg):
    """`sv = ""` is a hand edit saying "leave the profile alone"; the daemon
    already reads it that way, so validation must not reject the file for it."""
    cfg = Config.load()
    cfg.set("general.language_profiles", {"sv": "  "})
    assert cfg.language_profiles() == {}
    assert cfg.profile_for_language("sv") is None
    assert [e for e in cfg.errors() if "language_profiles" in e] == []


def test_the_pill_is_hidden_for_the_paste_by_default(isolated_xdg):
    """The fix, not the fallback: the owner keeps both the pill and auto-paste."""
    cfg = Config.load()
    assert cfg.get("inject.pill_focus") == "hide"
    assert cfg.get("inject.pill_settle_ms") == 150


def test_errors_rejects_an_unknown_pill_focus(isolated_xdg):
    cfg = Config.load()
    cfg.set("inject.pill_focus", "telepathy")
    assert any("inject.pill_focus" in e and "telepathy" in e for e in cfg.errors())


def test_errors_accepts_every_pill_focus_and_a_file_without_the_key(isolated_xdg):
    cfg = Config.load()
    for choice in ("hide", "clipboard", "paste"):
        cfg.set("inject.pill_focus", choice)
        assert [e for e in cfg.errors() if "inject.pill_focus" in e] == []
    doc = cfg._doc
    del doc["inject"]["pill_focus"]
    assert [e for e in cfg.errors() if "inject.pill_focus" in e] == []


def test_errors_rejects_a_settle_that_is_not_a_time(isolated_xdg):
    cfg = Config.load()
    for bad in ("soon", -20):
        cfg.set("inject.pill_settle_ms", bad)
        assert any("inject.pill_settle_ms" in e for e in cfg.errors()), bad
    cfg.set("inject.pill_settle_ms", 0)
    assert [e for e in cfg.errors() if "inject.pill_settle_ms" in e] == []


def test_default_inject_mode_is_paste(isolated_xdg):
    assert Config.load().get("inject.mode") == "paste"


def test_errors_rejects_an_unknown_inject_mode(isolated_xdg):
    cfg = Config.load()
    cfg.set("inject.mode", "telepathy")
    assert any("inject.mode" in e and "telepathy" in e for e in cfg.errors())


def test_errors_accepts_both_inject_modes_and_a_file_without_the_key(isolated_xdg):
    cfg = Config.load()
    for mode in ("paste", "clipboard"):
        cfg.set("inject.mode", mode)
        assert [e for e in cfg.errors() if "inject.mode" in e] == []
    # A config written before this option existed has no key at all and stays valid.
    doc = cfg._doc
    del doc["inject"]["mode"]
    assert [e for e in cfg.errors() if "inject.mode" in e] == []


# -- where the pill sits ---------------------------------------------------

@pytest.mark.parametrize("position", POSITIONS)
def test_every_placement_passes_validation(isolated_xdg, position):
    cfg = Config.load()
    cfg.set("ui.overlay_position", position)
    assert [e for e in cfg.errors() if "overlay_position" in e] == []


@pytest.mark.parametrize("legacy,expected", [("bottom", "bottom-center"), ("top", "top-center")])
def test_a_config_written_before_the_nine_placements_still_validates(isolated_xdg, legacy, expected):
    """"bottom" and "top" are what older files hold; they must not become errors."""
    cfg = Config.load()
    cfg.set("ui.overlay_position", legacy)
    assert [e for e in cfg.errors() if "overlay_position" in e] == []
    assert cfg.overlay_placement() == (expected, 0, 48)


@pytest.mark.parametrize("junk", ["sideways", "bottom-middle", "", 3, True])
def test_a_placement_that_is_not_one_of_the_nine_is_an_error(isolated_xdg, junk):
    cfg = Config.load()
    cfg.set("ui.overlay_position", junk)
    assert any("ui.overlay_position" in e for e in cfg.errors()), cfg.errors()


@pytest.mark.parametrize("key", ["ui.overlay_margin_x", "ui.overlay_margin_y"])
@pytest.mark.parametrize("junk", ["48", 1.5, True, 2001, -2001, [1]])
def test_a_margin_must_be_a_whole_number_of_sane_pixels(isolated_xdg, key, junk):
    cfg = Config.load()
    cfg.set(key, junk)
    assert any(key in e for e in cfg.errors()), cfg.errors()


@pytest.mark.parametrize("key", ["ui.overlay_margin_x", "ui.overlay_margin_y"])
@pytest.mark.parametrize("value", [0, 48, -2000, 2000, 137])
def test_a_margin_inside_the_range_is_accepted(isolated_xdg, key, value):
    cfg = Config.load()
    cfg.set(key, value)
    assert [e for e in cfg.errors() if key in e] == []


@pytest.mark.parametrize("junk", ["true", 1, 0, "yes", [True]])
def test_the_padding_switch_must_be_a_boolean(isolated_xdg, junk):
    cfg = Config.load()
    cfg.set("ui.overlay_pad_to_place", junk)
    assert any("ui.overlay_pad_to_place" in e for e in cfg.errors()), cfg.errors()


@pytest.mark.parametrize("value", [True, False])
def test_the_padding_switch_takes_either_boolean(isolated_xdg, value):
    cfg = Config.load()
    cfg.set("ui.overlay_pad_to_place", value)
    assert [e for e in cfg.errors() if "overlay_pad_to_place" in e] == []


def test_a_file_with_no_placement_keys_leaves_the_pill_where_it_was(isolated_xdg):
    """An upgraded install keeps the pill exactly where it was."""
    paths.config_file().write_text(LEGACY_CONFIG)
    cfg = Config.load()
    assert cfg.get("ui.overlay_position") is None
    assert cfg.get("ui.overlay_pad_to_place") is None
    assert cfg.overlay_placement() == ("bottom-center", 0, 48)
    assert [e for e in cfg.errors() if "overlay_" in e] == []


def test_the_placement_is_read_back_normalised(isolated_xdg):
    cfg = Config.load()
    cfg.set("ui.overlay_position", " TOP-RIGHT ")
    cfg.set("ui.overlay_margin_x", 24)
    cfg.set("ui.overlay_margin_y", -12)
    assert cfg.overlay_placement() == ("top-right", 24, -12)


def test_a_broken_placement_still_reads_back_as_something_showable(isolated_xdg):
    """errors() says no, but the daemon must never be handed a position the
    helper would refuse to start with."""
    cfg = Config.load()
    cfg.set("ui.overlay_position", "sideways")
    cfg.set("ui.overlay_margin_y", 99999)
    assert cfg.overlay_placement() == ("bottom-center", 0, 2000)
