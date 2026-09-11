import pytest

from voice.audio.capture import Source
from voice.config import Config
from voice.hotkey.desktop_shortcuts import ShortcutStoreError
from voice.hotkey.portal_listener import DIALOG_MESSAGE, NO_CAPTURE_MESSAGE
from voice.ui.settings import (ACTION_LABELS, ACTIONS, CHANGE, CHANGE_BUSY, CHANGE_STOPPED,
                               CHANGE_UNUSABLE, CHANGED_ON_DESKTOP, DESKTOP_DIALOG_OPEN,
                               DESKTOP_STORE, NEEDS_A_COMBINATION, NOT_OFFERED,
                               NOT_SET, PROFILE_TEMPLATES, SAVED_TO_DESKTOP,
                               SETTINGS_APP_MISSING, SETTINGS_APP_OPENED,
                               SHORTCUT_SETTINGS_PATH, UNKNOWN_TRIGGER, WHO_MANAGES,
                               SettingsDialog)


def make(qapp):
    cfg = Config.load()
    captures = []
    dlg = SettingsDialog(cfg, capture_key=captures.append,
                         sources=lambda: [Source("alsa_input.obsbot", "OBSBOT Tiny 3", True)])
    return cfg, dlg, captures


def test_loads_values_from_config(qapp):
    cfg, dlg, _ = make(qapp)
    assert dlg.hotkey_edit.text() == "KEY_F13"
    assert dlg.mode_combo.currentData() == "hold"
    assert dlg.language_combo.currentData() == "en"
    assert dlg.device_combo.itemText(1) == "OBSBOT Tiny 3 (default)"
    assert [dlg.profile_list.item(i).text() for i in range(dlg.profile_list.count())] == ["local", "openai", "groq", "openrouter"]


def test_edit_and_save_writes_config_and_emits(qapp):
    cfg, dlg, _ = make(qapp)
    fired = []
    dlg.saved.connect(lambda: fired.append(True))
    dlg.hotkey_edit.setText("KEY_RIGHTCTRL")
    dlg.mode_combo.setCurrentIndex(dlg.mode_combo.findData("toggle"))
    dlg.device_combo.setCurrentIndex(1)
    dlg.profile_list.setCurrentRow(1)                       # openai
    dlg.profile_form["api_key"].setText("sk-abc")
    dlg.profile_form["model"].setText("gpt-4o-mini-transcribe")
    dlg.save_button.click()
    assert fired == [True]
    again = Config.load()
    assert again.get("hotkeys.dictate") == "KEY_RIGHTCTRL"
    assert again.get("hotkeys.dictate_mode") == "toggle"
    assert again.get("audio.device") == "alsa_input.obsbot"
    assert again.get("stt.profiles.openai.api_key") == "sk-abc"
    assert again.get("stt.profiles.openai.model") == "gpt-4o-mini-transcribe"


def test_invalid_hotkey_blocks_save(qapp):
    cfg, dlg, _ = make(qapp)
    dlg.hotkey_edit.setText("KEY_BANANA")
    dlg.save_button.click()
    assert "KEY_BANANA" in dlg.error_label.text()
    assert Config.load().get("hotkeys.dictate") == "KEY_F13"


def test_capture_button_requests_key_and_fills_field(qapp):
    cfg, dlg, captures = make(qapp)
    dlg.capture_button.click()
    assert dlg.capture_button.text().startswith("Press")
    captures[0]("KEY_F14")                                   # listener thread would call this
    qapp.processEvents()
    assert dlg.hotkey_edit.text() == "KEY_F14"


def test_invalid_beam_size_blocks_save(qapp):
    cfg, dlg, _ = make(qapp)
    dlg.profile_list.setCurrentRow(0)                       # local
    fired = []
    dlg.saved.connect(lambda: fired.append(True))
    dlg.profile_form["beam_size"].setText("abc")
    dlg.save_button.click()
    assert "Search width" in dlg.error_label.text()
    assert fired == []
    assert Config.load().get("stt.profiles.local.beam_size") == 5


def test_add_profile_from_template_and_replacements_roundtrip(qapp):
    cfg, dlg, _ = make(qapp)
    dlg.add_profile_combo.setCurrentText("mistral")
    dlg.add_profile_button.click()
    assert dlg.profile_list.item(dlg.profile_list.count() - 1).text() == "mistral"
    assert dlg.profile_form["base_url"].text() == PROFILE_TEMPLATES["mistral"]["base_url"]
    dlg.replacements_table.setRowCount(1)
    dlg.set_replacement_row(0, "obs bot", "OBSBOT", "icase")
    dlg.save_button.click()
    again = Config.load()
    assert again.get("stt.profiles.mistral.backend") == "openai_compatible"
    assert again.get("dictionary.replacements") == [["obs bot", "OBSBOT", "icase"]]


def test_close_discards_edits_and_never_touches_the_callers_config(qapp):
    # The dialog edits a private Config loaded from the same file, so the daemon's
    # live Config keeps serving the settings that are actually in force.
    cfg, dlg, _ = make(qapp)
    dlg.hotkey_edit.setText("KEY_RIGHTCTRL")
    dlg.mode_combo.setCurrentIndex(dlg.mode_combo.findData("toggle"))
    dlg.close()
    assert cfg.get("hotkeys.dictate") == "KEY_F13"
    assert cfg.get("hotkeys.dictate_mode") == "hold"
    assert Config.load().get("hotkeys.dictate") == "KEY_F13"


def test_profile_edits_do_not_leak_into_the_callers_config_before_save(qapp):
    cfg, dlg, _ = make(qapp)
    dlg.profile_list.setCurrentRow(1)                     # openai
    dlg.profile_form["model"].setText("gpt-4o-mini-transcribe")
    dlg.add_profile_combo.setCurrentText("mistral")
    dlg.add_profile_button.click()
    assert cfg.get("stt.profiles.mistral") is None
    assert cfg.get("stt.profiles.openai.model") == "gpt-transcribe"


def test_reload_from_disk_repopulates_the_widgets(qapp):
    cfg, dlg, _ = make(qapp)
    dlg.hotkey_edit.setText("KEY_RIGHTCTRL")
    dlg.add_profile_combo.setCurrentText("mistral")
    dlg.add_profile_button.click()
    dlg.close()

    dlg.reload_from_disk()                                # what reopening does
    assert dlg.hotkey_edit.text() == "KEY_F13"
    assert [dlg.profile_list.item(i).text() for i in range(dlg.profile_list.count())] == \
        ["local", "openai", "groq", "openrouter"]


def test_save_writes_to_disk_and_the_caller_config_sees_it_after_reload(qapp):
    cfg, dlg, _ = make(qapp)
    dlg.hotkey_edit.setText("KEY_RIGHTCTRL")
    dlg.save_button.click()
    assert cfg.get("hotkeys.dictate") == "KEY_F13"        # still the old live value
    cfg.reload()                                          # what Daemon.apply_config does
    assert cfg.get("hotkeys.dictate") == "KEY_RIGHTCTRL"


def test_save_does_not_revert_a_profile_switched_elsewhere(qapp):
    # The dialog's Config is a whole-document snapshot taken when it opened, so a
    # profile switched from the tray or CLI meanwhile would be written back stale.
    cfg, dlg, _ = make(qapp)
    external = Config.load()
    external.set("stt.active", "groq")
    external.save()

    dlg.hotkey_edit.setText("KEY_RIGHTCTRL")
    dlg.save_button.click()

    again = Config.load()
    assert again.get("stt.active") == "groq"              # not reverted to the snapshot
    assert again.get("hotkeys.dictate") == "KEY_RIGHTCTRL"  # the dialog's own edit landed


def test_use_this_profile_wins_over_the_on_disk_value(qapp):
    cfg, dlg, _ = make(qapp)
    external = Config.load()
    external.set("stt.active", "groq")
    external.save()

    dlg.profile_list.setCurrentRow(1)                     # openai
    dlg.activate_button.click()
    dlg.save_button.click()
    assert Config.load().get("stt.active") == "openai"


def test_reload_from_disk_clears_the_activation_flag(qapp):
    cfg, dlg, _ = make(qapp)
    dlg.profile_list.setCurrentRow(1)
    dlg.activate_button.click()
    dlg.close()
    dlg.reload_from_disk()

    external = Config.load()
    external.set("stt.active", "groq")
    external.save()
    dlg.save_button.click()
    assert Config.load().get("stt.active") == "groq"      # the abandoned activation is gone


def test_portal_backend_explains_where_shortcuts_are_chosen(qapp):
    cfg = Config.load()
    dlg = SettingsDialog(cfg, capture_key=lambda cb: None,
                         sources=lambda: [Source("alsa_input.obsbot", "OBSBOT Tiny 3", True)],
                         backend="portal", shortcut_store=lambda: None)
    assert "your desktop" in (dlg.hotkey_owner_label.text()
                              + dlg.hotkey_help_text()).lower()
    plain = SettingsDialog(cfg, capture_key=lambda cb: None, sources=lambda: [])
    assert "your keyboard" in (plain.hotkey_owner_label.text()
                               + plain.hotkey_help_text()).lower()


def test_a_captured_message_is_shown_instead_of_being_typed_into_the_field(qapp):
    """The portal listener answers capture_next with a sentence, not a key name."""
    cfg, dlg, captures = make(qapp)
    dlg.capture_button.click()
    captures[0]("portal: change the shortcut in your desktop's settings")
    qapp.processEvents()
    from voice.ui.settings import CAPTURE_NOT_POSSIBLE

    assert dlg.hotkey_edit.text() == "KEY_F13"               # untouched
    # The listener's own sentence names the mechanism it is; what it means for
    # whoever pressed the button is the same every time, and that is shown.
    assert dlg.hotkey_status.text() == CAPTURE_NOT_POSSIBLE
    assert "portal" not in dlg.hotkey_status.text()
    assert dlg.error_label.text() == ""
    assert dlg.capture_button.text() == CHANGE


def test_save_does_not_revert_a_language_switched_elsewhere(qapp):
    """Same snapshot problem as stt.active: the toggle hotkey and the tray both
    change general.language while this dialog is open."""
    cfg, dlg, _ = make(qapp)
    external = Config.load()
    external.set("general.language", "sv")
    external.save()

    dlg.hotkey_edit.setText("KEY_RIGHTCTRL")
    dlg.save_button.click()

    again = Config.load()
    assert again.get("general.language") == "sv"             # not reverted to "en"
    assert again.get("hotkeys.dictate") == "KEY_RIGHTCTRL"   # the dialog's own edit landed


def test_the_language_combo_wins_over_the_on_disk_value(qapp):
    cfg, dlg, _ = make(qapp)
    external = Config.load()
    external.set("general.language", "sv")
    external.save()

    dlg.language_combo.setCurrentIndex(dlg.language_combo.findData("auto"))
    dlg.save_button.click()
    assert Config.load().get("general.language") == "auto"


def test_reload_from_disk_clears_the_language_flag(qapp):
    cfg, dlg, _ = make(qapp)
    dlg.language_combo.setCurrentIndex(dlg.language_combo.findData("auto"))
    dlg.close()
    dlg.reload_from_disk()

    external = Config.load()
    external.set("general.language", "sv")
    external.save()
    dlg.save_button.click()
    assert Config.load().get("general.language") == "sv"     # the abandoned edit is gone


# -- the portal backend has no keys to capture ---------------------------------
def portal_dialog(qapp, triggers=None, capture=lambda cb: None):
    """A portal dialog on a desktop whose shortcut store is not ours to write.

    The store is exercised on its own below; every other portal test would
    otherwise reach the real dconf on a GNOME machine.
    """
    cfg = Config.load()
    dlg = SettingsDialog(cfg, capture_key=capture,
                         sources=lambda: [Source("alsa_input.obsbot", "OBSBOT Tiny 3", True)],
                         backend="portal", triggers=triggers, shortcut_store=lambda: None)
    return cfg, dlg


def test_the_portal_backend_edits_its_triggers_instead_of_capturing_keys(qapp):
    """The compositor keeps the keystroke, so "Capture key" can only ever answer
    with a sentence - which landed in the red error label and read as a failure."""
    cfg, dlg = portal_dialog(qapp)
    assert set(dlg.portal_edits) == {"dictate", "recall", "cancel", "language_toggle"}
    assert dlg.portal_edits["dictate"].text() == "CTRL+space"
    assert dlg.capture_button.isHidden() is True
    assert dlg.error_label.text() == ""                 # the explanation is a note,
    assert "desktop" in dlg.hotkey_owner_label.text()   # not an error


def test_the_evdev_backend_still_captures_keys(qapp):
    cfg, dlg, _ = make(qapp)
    assert dlg.portal_edits == {}
    assert dlg.capture_button.isHidden() is False


def test_saving_writes_the_portal_triggers(qapp):
    cfg, dlg = portal_dialog(qapp)
    dlg.portal_edits["dictate"].setText("CTRL+ALT+d")
    dlg.portal_edits["language_toggle"].setText("CTRL+SHIFT+l")
    dlg.save_button.click()
    assert dlg.error_label.text() == ""
    again = Config.load()
    assert again.get("hotkeys.portal_dictate") == "CTRL+ALT+d"
    assert again.get("hotkeys.portal_language_toggle") == "CTRL+SHIFT+l"


def test_an_empty_portal_dictate_trigger_is_refused(qapp):
    cfg, dlg = portal_dialog(qapp)
    dlg.portal_edits["dictate"].setText("   ")
    dlg.save_button.click()
    assert dlg.error_label.text().startswith(ACTION_LABELS["dictate"])
    assert Config.load().get("hotkeys.portal_dictate") == "CTRL+space"
    assert "hotkeys" not in dlg.error_label.text()


# -- the desktop's key, beside the field that can only ask for one -------------
def test_the_portal_tab_shows_the_key_the_desktop_actually_holds(qapp):
    """The fields are a first-run wish and on GNOME are never applied at all, so
    a tab that shows only them is a tab that lies about what is bound."""
    cfg, dlg = portal_dialog(qapp, triggers=lambda: {"dictate": "F13", "recall": ""})
    assert set(dlg.portal_effective) == set(dlg.portal_edits)
    assert dlg.portal_effective["dictate"].text() == "F13"
    assert dlg.portal_effective["recall"].text() == NOT_SET     # registered, no key
    assert dlg.portal_effective["cancel"].text() == NOT_SET     # never bound at all


def test_the_effective_trigger_is_unknown_before_the_desktop_answers(qapp):
    cfg, dlg = portal_dialog(qapp, triggers=lambda: {})
    assert UNKNOWN_TRIGGER in dlg.portal_effective["dictate"].text()
    cfg, plain = portal_dialog(qapp)                                # no accessor at all
    assert UNKNOWN_TRIGGER in plain.portal_effective["dictate"].text()


def test_the_effective_triggers_are_re_read_when_the_window_is_reopened(qapp):
    """The dialog is built once and reused, so a key assigned between two visits
    must land on the second one."""
    held = {"dictate": ""}
    cfg, dlg = portal_dialog(qapp, triggers=lambda: dict(held))
    assert dlg.portal_effective["dictate"].text() == NOT_SET
    held["dictate"] = "F13"
    dlg.refresh_effective_triggers()
    assert "F13" in dlg.portal_effective["dictate"].text()


def test_the_portal_fields_say_who_owns_the_key_and_what_save_does(qapp):
    """They used to say "first-run preference, editing here changes nothing",
    which was true and useless; Save now writes the desktop's own store."""
    cfg, dlg = portal_dialog(qapp, triggers=lambda: {"dictate": "F13"})
    note = dlg.hotkey_owner_label.text().lower()
    assert "your desktop" in note and "change" in note
    assert "change" in dlg.hotkey_help_text().lower()


def test_the_shortcut_settings_button_opens_the_desktops_own_dialog(qapp, monkeypatch):
    """Portal version 2 (KDE) has a reconfigure dialog; the listener opens it and
    answers with the sentence to show."""
    spawned = []
    monkeypatch.setattr("voice.ui.settings._spawn", spawned.append)
    cfg, dlg = portal_dialog(qapp, triggers=lambda: {"dictate": "F13"},
                             capture=lambda cb: cb(DIALOG_MESSAGE))
    dlg.shortcuts_button.click()
    qapp.processEvents()
    assert dlg.hotkey_status.text() == DESKTOP_DIALOG_OPEN
    assert DIALOG_MESSAGE not in dlg.hotkey_status.text()    # written for whoever wrote it
    assert spawned == []                              # nothing else was needed


def test_the_shortcut_settings_button_falls_back_to_the_desktops_settings_app(qapp, monkeypatch):
    """GNOME has no reconfigure dialog, so the button opens the panel that does
    hold the key instead of leaving the user with a sentence."""
    spawned = []
    monkeypatch.setattr("voice.ui.settings._spawn", spawned.append)
    monkeypatch.setattr("voice.ui.settings.shutil.which",
                        lambda name: "/usr/bin/" + name if name == "gnome-control-center" else None)
    cfg, dlg = portal_dialog(qapp, triggers=lambda: {"dictate": ""},
                             capture=lambda cb: cb(NO_CAPTURE_MESSAGE))
    dlg.shortcuts_button.click()
    qapp.processEvents()
    assert spawned == [["gnome-control-center", "keyboard"]]
    assert dlg.hotkey_status.text() == SETTINGS_APP_OPENED


def test_the_shortcut_settings_button_finds_the_kde_panel_too(qapp, monkeypatch):
    spawned = []
    monkeypatch.setattr("voice.ui.settings._spawn", spawned.append)
    monkeypatch.setattr("voice.ui.settings.shutil.which",
                        lambda name: "/usr/bin/" + name if name == "systemsettings" else None)
    cfg, dlg = portal_dialog(qapp, capture=lambda cb: cb(NO_CAPTURE_MESSAGE))
    dlg.shortcuts_button.click()
    qapp.processEvents()
    assert spawned == [["systemsettings", "kcm_keys"]]


def test_with_no_settings_app_installed_the_button_says_where_to_click(qapp, monkeypatch):
    monkeypatch.setattr("voice.ui.settings._spawn",
                        lambda cmd: pytest.fail("nothing to start"))
    monkeypatch.setattr("voice.ui.settings.shutil.which", lambda name: None)
    cfg, dlg = portal_dialog(qapp, capture=lambda cb: cb(NO_CAPTURE_MESSAGE))
    dlg.shortcuts_button.click()
    qapp.processEvents()
    assert dlg.hotkey_status.text() == SETTINGS_APP_MISSING
    assert SHORTCUT_SETTINGS_PATH in dlg.hotkey_status.text()


def test_a_settings_app_that_will_not_start_says_so_instead_of_vanishing(qapp, monkeypatch):
    def boom(command):
        raise OSError("no such file")

    monkeypatch.setattr("voice.ui.settings._spawn", boom)
    monkeypatch.setattr("voice.ui.settings.shutil.which", lambda name: "/usr/bin/" + name)
    cfg, dlg = portal_dialog(qapp, capture=lambda cb: cb(NO_CAPTURE_MESSAGE))
    dlg.shortcuts_button.click()
    qapp.processEvents()
    assert "no such file" in dlg.hotkey_status.text()
    assert SHORTCUT_SETTINGS_PATH in dlg.hotkey_status.text()


def test_the_portal_tab_still_shows_the_evdev_key_fields(qapp):
    """They are what applies if hotkeys.backend goes back to evdev, and the hint
    says so - a screenshot of the tab is what caught them going missing."""
    cfg, dlg = portal_dialog(qapp, triggers=lambda: {"dictate": "F13"})
    assert dlg.hotkey_edit.parentWidget() is not None       # actually in the layout
    assert dlg.hotkey_edit.text() == "KEY_F13"
    assert dlg.capture_button.parentWidget() is not None
    assert dlg.capture_button.isHidden() is True            # nothing to capture here


def test_the_evdev_tab_keeps_its_capture_button_and_gains_nothing(qapp):
    """The evdev rendering is untouched by all of this: it has a real key to
    capture and no desktop holding anything."""
    cfg, dlg, _ = make(qapp)
    assert dlg.portal_effective == {}
    assert dlg.shortcuts_button is None
    assert dlg.capture_button.isHidden() is False
    assert dlg.capture_button is dlg.change_buttons["dictate"]


# -- a profile per language -----------------------------------------------------
def _with_map(mapping: dict, **settings) -> None:
    external = Config.load()
    external.set("general.language_profiles", mapping)
    for key, value in settings.items():
        external.set(key.replace("__", "."), value)
    external.save()


def test_the_general_tab_lists_one_profile_row_per_language(qapp):
    cfg, dlg, _ = make(qapp)
    table = dlg.language_profile_table
    # Named, not coded: "en" is a thing a config file says, not a language.
    assert [table.item(r, 0).text() for r in range(table.rowCount())] == ["English", "Swedish"]
    combo = dlg.language_profile_combos["sv"]
    assert [combo.itemText(i) for i in range(combo.count())] == [
        "(keep current)", "local", "openai", "groq", "openrouter"]
    assert combo.currentData() == ""            # a shipped config maps nothing


def test_the_table_shows_a_map_that_is_already_configured(qapp):
    _with_map({"sv": "openai"})
    cfg, dlg, _ = make(qapp)
    assert dlg.language_profile_combos["sv"].currentData() == "openai"
    assert dlg.language_profile_combos["en"].currentData() == ""


def test_saving_writes_the_map_and_omits_the_kept_rows(qapp):
    cfg, dlg, _ = make(qapp)
    combo = dlg.language_profile_combos["sv"]
    combo.setCurrentIndex(combo.findData("openai"))
    dlg.save_button.click()
    again = Config.load()
    assert again.get("general.language_profiles") == {"sv": "openai"}
    assert again.errors() == []


def test_saving_an_empty_map_keeps_the_commented_example(qapp):
    """Nobody who ignores this feature should lose the hint that explains it."""
    cfg, dlg, _ = make(qapp)
    dlg.save_button.click()
    again = Config.load()
    assert again.get("general.language_profiles") == {}
    assert '# sv = "local-swedish"' in again.path.read_text()


def test_picking_a_language_here_activates_its_mapped_profile(qapp):
    _with_map({"sv": "openai"})
    cfg, dlg, _ = make(qapp)
    dlg.language_combo.setCurrentIndex(dlg.language_combo.findData("sv"))
    dlg.save_button.click()
    again = Config.load()
    assert again.get("general.language") == "sv"
    assert again.get("stt.active") == "openai"
    assert dlg.active_label.text() == "Active profile: openai"


def test_a_language_nobody_touched_here_does_not_move_the_profile(qapp):
    """Save must not re-apply the map to a language this dialog did not change:
    the owner may have picked another profile by hand since."""
    _with_map({"sv": "openai"}, general__language="sv", stt__active="groq")
    cfg, dlg, _ = make(qapp)
    dlg.hotkey_edit.setText("KEY_RIGHTCTRL")
    dlg.save_button.click()
    assert Config.load().get("stt.active") == "groq"


def test_use_this_profile_wins_over_the_map(qapp):
    _with_map({"sv": "openai"})
    cfg, dlg, _ = make(qapp)
    dlg.profile_list.setCurrentRow(2)                  # groq
    dlg.activate_button.click()
    dlg.language_combo.setCurrentIndex(dlg.language_combo.findData("sv"))
    dlg.save_button.click()
    again = Config.load()
    assert again.get("general.language") == "sv"
    assert again.get("stt.active") == "groq"           # the explicit choice stands


def test_a_profile_added_from_a_template_can_be_mapped_at_once(qapp):
    """The README's two steps are one visit: add local-swedish, then map sv to it."""
    cfg, dlg, _ = make(qapp)
    combo = dlg.language_profile_combos["sv"]
    combo.setCurrentIndex(combo.findData("openai"))          # a choice already made here
    dlg.add_profile_combo.setCurrentText("local-swedish")
    dlg.add_profile_button.click()

    combo = dlg.language_profile_combos["sv"]                # rebuilt with the new profile
    assert combo.findData("local-swedish") > 0
    assert combo.currentData() == "openai"                   # the choice survived the rebuild
    combo.setCurrentIndex(combo.findData("local-swedish"))
    dlg.save_button.click()
    again = Config.load()
    assert again.get("general.language_profiles") == {"sv": "local-swedish"}
    assert again.errors() == []


def test_rebuilding_the_table_leaves_no_stray_combo_behind(qapp):
    """A replaced cell widget is only scheduled for deletion, and until that runs
    it paints over the first cell of the table."""
    from PySide6.QtWidgets import QComboBox

    cfg, dlg, _ = make(qapp)
    table = dlg.language_profile_table
    assert len(table.viewport().findChildren(QComboBox)) == table.rowCount() == 2
    dlg.add_profile_combo.setCurrentText("local-swedish")
    dlg.add_profile_button.click()
    assert len(table.viewport().findChildren(QComboBox)) == table.rowCount() == 2
    dlg.reload_from_disk()
    assert len(table.viewport().findChildren(QComboBox)) == table.rowCount() == 2


def test_saving_keeps_a_mapping_for_a_language_the_table_cannot_show(qapp):
    """The table only has rows for general.languages; a map may name others
    (`de`, or `auto`, which the Language combo offers), and Save must not eat
    them just because there was no row for them."""
    _with_map({"en": "local", "de": "openai", "auto": "groq"})
    cfg, dlg, _ = make(qapp)
    dlg.hotkey_edit.setText("KEY_RIGHTCTRL")
    dlg.save_button.click()
    again = Config.load()
    assert again.get("general.language_profiles") == {"en": "local", "de": "openai",
                                                     "auto": "groq"}
    assert dlg.error_label.text() == ""


def test_setting_a_row_back_to_keep_current_clears_that_mapping(qapp):
    _with_map({"en": "local", "sv": "openai"})
    cfg, dlg, _ = make(qapp)
    combo = dlg.language_profile_combos["sv"]
    combo.setCurrentIndex(combo.findData(""))
    dlg.save_button.click()
    assert Config.load().get("general.language_profiles") == {"en": "local"}


def test_a_profile_another_language_chose_does_not_follow_the_language_picked_here(qapp):
    """The toggle hotkey switched to Swedish *and* its model while this dialog sat
    open. Choosing English here must not leave the Swedish model transcribing it."""
    _with_map({"sv": "openai"}, general__language="auto")
    cfg, dlg, _ = make(qapp)                       # opened on auto, profile local
    external = Config.load()
    external.set("general.language", "sv")
    external.set("stt.active", "openai")           # what the daemon writes for sv
    external.save()

    dlg.language_combo.setCurrentIndex(dlg.language_combo.findData("en"))
    dlg.save_button.click()
    again = Config.load()
    assert again.get("general.language") == "en"
    assert again.get("stt.active") == "local"      # not the profile sv chose


def test_an_external_language_switch_still_carries_its_paired_profile(qapp):
    """The other half of the guard above: when the dialog did *not* choose a
    language, the pair the daemon wrote must survive intact - dropping the
    profile leaves Swedish transcribed with the English model."""
    _with_map({"sv": "openai"})                     # language en, active local
    cfg, dlg, _ = make(qapp)
    external = Config.load()
    external.set("general.language", "sv")
    external.set("stt.active", "openai")            # the daemon writes the pair
    external.save()

    dlg.hotkey_edit.setText("KEY_RIGHTCTRL")        # unrelated edit, language untouched
    dlg.save_button.click()
    again = Config.load()
    assert again.get("general.language") == "sv"
    assert again.get("stt.active") == "openai"


def test_a_second_language_change_in_the_same_dialog_still_moves_the_profile(qapp):
    """Save does not close the window, so the flags it sets must not outlive it:
    the second switch used to keep the first language's model."""
    _with_map({"en": "local", "sv": "openai"})
    cfg, dlg, _ = make(qapp)
    dlg.language_combo.setCurrentIndex(dlg.language_combo.findData("sv"))
    dlg.save_button.click()
    first = Config.load()
    assert (first.get("general.language"), first.get("stt.active")) == ("sv", "openai")

    dlg.language_combo.setCurrentIndex(dlg.language_combo.findData("en"))
    dlg.save_button.click()
    again = Config.load()
    assert (again.get("general.language"), again.get("stt.active")) == ("en", "local")


def test_after_a_save_the_dialog_still_follows_an_external_language_switch(qapp):
    """A dialog that saved once must keep carrying over what the toggle hotkey
    writes; reverting the language alone would break it away from its profile."""
    _with_map({"en": "local", "sv": "openai"})
    cfg, dlg, _ = make(qapp)
    dlg.language_combo.setCurrentIndex(dlg.language_combo.findData("sv"))
    dlg.save_button.click()

    external = Config.load()                    # the toggle hotkey, writing the pair
    external.set("general.language", "en")
    external.set("stt.active", "local")
    external.save()

    dlg.max_seconds.setValue(99)                # an unrelated edit, saved
    dlg.save_button.click()
    again = Config.load()
    assert again.get("general.language") == "en"
    assert again.get("stt.active") == "local"
    assert again.get("audio.max_seconds") == 99


def test_a_hidden_mapping_whose_profile_is_gone_does_not_block_saving(qapp):
    """An entry for a language with no row, naming a profile that is gone, is a
    config error - and unreachable from the dialog, so it would block every save."""
    seed = Config.load()
    seed.set("general.languages", ["en"])
    seed.set("general.language_profiles", {"sv": "local-swedish"})   # never defined
    seed.save()

    cfg, dlg, _ = make(qapp)
    dlg.max_seconds.setValue(99)
    dlg.save_button.click()
    again = Config.load()
    assert dlg.error_label.text() == ""
    assert again.get("audio.max_seconds") == 99
    assert again.get("general.language_profiles") == {}    # the dangling entry is dropped
    assert again.errors() == []


def test_the_general_tab_loads_the_text_insertion_mode(qapp):
    cfg, dlg, _ = make(qapp)
    assert dlg.inject_mode_combo.currentData() == "paste"
    assert [dlg.inject_mode_combo.itemData(i) for i in range(dlg.inject_mode_combo.count())] \
        == ["paste", "clipboard"]


def test_saving_writes_the_text_insertion_mode(qapp):
    cfg, dlg, _ = make(qapp)
    dlg.inject_mode_combo.setCurrentIndex(dlg.inject_mode_combo.findData("clipboard"))
    dlg.save_button.click()
    assert dlg.error_label.text() == ""
    assert Config.load().get("inject.mode") == "clipboard"


def test_save_does_not_revert_an_inject_mode_changed_elsewhere(qapp):
    """Same snapshot problem as stt.active and general.language."""
    cfg, dlg, _ = make(qapp)
    external = Config.load()
    external.set("inject.mode", "clipboard")
    external.save()

    dlg.hotkey_edit.setText("KEY_RIGHTCTRL")
    dlg.save_button.click()

    again = Config.load()
    assert again.get("inject.mode") == "clipboard"           # not reverted to "paste"
    assert again.get("hotkeys.dictate") == "KEY_RIGHTCTRL"   # the dialog's own edit landed
    assert dlg.inject_mode_combo.currentData() == "clipboard"  # and it shows what was saved


def test_the_inject_mode_combo_wins_over_the_on_disk_value(qapp):
    # Picking here has to beat a value written elsewhere afterwards, exactly as
    # the language combo does. Qt emits nothing when the shown value is picked
    # again, so the pick has to be a real change - as it is for a user who came
    # to this dialog to change the mode.
    cfg, dlg, _ = make(qapp)
    dlg.inject_mode_combo.setCurrentIndex(dlg.inject_mode_combo.findData("clipboard"))

    external = Config.load()
    external.set("inject.mode", "paste")
    external.save()

    dlg.save_button.click()
    assert Config.load().get("inject.mode") == "clipboard"


def test_reload_from_disk_clears_the_inject_mode_flag(qapp):
    cfg, dlg, _ = make(qapp)
    dlg.inject_mode_combo.setCurrentIndex(dlg.inject_mode_combo.findData("clipboard"))
    dlg.close()
    dlg.reload_from_disk()

    external = Config.load()
    external.set("inject.mode", "clipboard")
    external.save()
    dlg.inject_mode_combo.setCurrentIndex(dlg.inject_mode_combo.findData("paste"))
    dlg.reload_from_disk()
    dlg.save_button.click()
    assert Config.load().get("inject.mode") == "clipboard"   # the abandoned edit is gone


# -- where the pill sits --------------------------------------------------

def _with_config(qapp, **values):
    """A dialog opened on a config that already holds `values`."""
    cfg = Config.load()
    for key, value in values.items():
        cfg.set(key, value)
    cfg.save()
    return make(qapp)


def _drag_pill_to(dlg, position, margin_x, margin_y):
    """As if the owner had dragged the pill there in the preview."""
    dlg.pill_placer.set_placement(position, margin_x, margin_y)
    dlg.pill_placer.placement_changed.emit()


def test_the_general_tab_loads_the_pill_placement(qapp):
    cfg, dlg, _ = _with_config(qapp, **{"ui.overlay_position": "top-right",
                                        "ui.overlay_margin_x": 12,
                                        "ui.overlay_margin_y": 60})
    assert dlg.pill_placer.placement() == ("top-right", 12, 60)
    assert "top-right" in dlg.pill_placement_label.text()
    assert "12" in dlg.pill_placement_label.text()
    assert "60" in dlg.pill_placement_label.text()


def test_the_preview_starts_on_the_shipped_placement(qapp):
    cfg, dlg, _ = make(qapp)
    assert dlg.pill_placer.placement() == ("bottom-center", 0, 48)
    width, height = dlg.pill_placer.screen_size()
    assert width > 0 and height > 0


def test_an_older_position_shows_as_the_placement_it_means(qapp):
    cfg, dlg, _ = _with_config(qapp, **{"ui.overlay_position": "top"})
    assert dlg.pill_placer.placement() == ("top-center", 0, 48)


def test_saving_writes_the_placement_the_pill_was_dragged_to(qapp):
    cfg, dlg, _ = make(qapp)
    _drag_pill_to(dlg, "middle-left", 24, 0)
    assert "middle-left" in dlg.pill_placement_label.text()
    dlg.save_button.click()
    assert dlg.error_label.text() == ""
    again = Config.load()
    assert again.get("ui.overlay_position") == "middle-left"
    assert (again.get("ui.overlay_margin_x"), again.get("ui.overlay_margin_y")) == (24, 0)
    assert again.errors() == []


def test_nudging_the_pill_with_the_keyboard_reaches_the_config(qapp):
    """The whole path, driven the way a user drives it: a key on the preview,
    then Save."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    cfg, dlg, _ = _with_config(qapp, **{"ui.overlay_position": "bottom-right",
                                        "ui.overlay_margin_x": 10,
                                        "ui.overlay_margin_y": 10})
    dlg.show()
    QTest.qWaitForWindowExposed(dlg)
    QTest.keyClick(dlg.pill_placer, Qt.Key.Key_Up)
    QTest.keyClick(dlg.pill_placer, Qt.Key.Key_Left, Qt.KeyboardModifier.ShiftModifier)
    assert dlg.pill_placer.placement() == ("bottom-right", 20, 11)
    dlg.save_button.click()
    again = Config.load()
    assert again.get("ui.overlay_position") == "bottom-right"
    assert (again.get("ui.overlay_margin_x"), again.get("ui.overlay_margin_y")) == (20, 11)
    dlg.close()


def test_a_placement_no_one_touched_is_written_back_unchanged(qapp):
    """Opening the window and pressing Save must not move the pill."""
    cfg, dlg, _ = _with_config(qapp, **{"ui.overlay_position": "middle-center",
                                        "ui.overlay_margin_x": 0,
                                        "ui.overlay_margin_y": 48})
    dlg.save_button.click()
    again = Config.load()
    assert again.get("ui.overlay_position") == "middle-center"
    assert (again.get("ui.overlay_margin_x"), again.get("ui.overlay_margin_y")) == (0, 48)


def test_save_does_not_revert_a_placement_changed_elsewhere(qapp):
    """`voice` has no CLI for this, but a hand edit while the window sits open is
    the same snapshot problem as inject.mode."""
    cfg, dlg, _ = make(qapp)
    external = Config.load()
    external.set("ui.overlay_position", "bottom-right")
    external.set("ui.overlay_margin_x", 30)
    external.save()

    dlg.hotkey_edit.setText("KEY_RIGHTCTRL")
    dlg.save_button.click()

    again = Config.load()
    assert again.get("ui.overlay_position") == "bottom-right"
    assert again.get("ui.overlay_margin_x") == 30
    assert again.get("hotkeys.dictate") == "KEY_RIGHTCTRL"
    # and the preview shows what was really saved
    assert dlg.pill_placer.placement() == ("bottom-right", 30, 48)
    assert "bottom-right" in dlg.pill_placement_label.text()


def test_a_placement_dragged_here_wins_over_the_on_disk_value(qapp):
    cfg, dlg, _ = make(qapp)
    _drag_pill_to(dlg, "top-center", 0, 20)

    external = Config.load()
    external.set("ui.overlay_position", "bottom-right")
    external.save()

    dlg.save_button.click()
    again = Config.load()
    assert again.get("ui.overlay_position") == "top-center"
    assert again.get("ui.overlay_margin_y") == 20


def test_reload_from_disk_clears_the_placement_flag(qapp):
    cfg, dlg, _ = make(qapp)
    _drag_pill_to(dlg, "top-left", 5, 5)
    dlg.reload_from_disk()

    external = Config.load()
    external.set("ui.overlay_position", "middle-right")
    external.save()
    dlg.save_button.click()
    assert Config.load().get("ui.overlay_position") == "middle-right"   # the abandoned edit is gone


def test_a_nonsense_placement_on_disk_does_not_block_a_save(qapp):
    """errors() rejects it, so the dialog has to show something sane and write
    that back rather than refusing every save until the file is hand-fixed."""
    cfg, dlg, _ = _with_config(qapp, **{"ui.overlay_position": "sideways"})
    assert dlg.pill_placer.placement() == ("bottom-center", 0, 48)
    dlg.save_button.click()
    assert dlg.error_label.text() == ""
    assert Config.load().get("ui.overlay_position") == "bottom-center"


# -- the trigger fields write the desktop's own store --------------------------
class FakeStore:
    """Stands in for the GNOME shortcut store: records, never runs dconf."""

    name = "GNOME"

    def __init__(self, fail: str = ""):
        self.writes: list[dict] = []
        self._fail = fail

    def write(self, triggers: dict) -> str:
        self.writes.append(dict(triggers))
        if self._fail:
            raise ShortcutStoreError(self._fail)
        return f"Saved to {self.name}'s own shortcut store: dictate = <Control>d."


def portal_dialog_with_store(qapp, store, triggers=None):
    cfg = Config.load()
    dlg = SettingsDialog(cfg, capture_key=lambda cb: None,
                         sources=lambda: [], backend="portal", triggers=triggers,
                         shortcut_store=lambda: store)
    return cfg, dlg


def test_saving_a_trigger_writes_it_to_the_desktops_own_store(qapp):
    """The whole point of item 1: an edited field has to reach the desktop, or
    it changes nothing at all on GNOME."""
    store = FakeStore()
    cfg, dlg = portal_dialog_with_store(qapp, store)
    dlg.portal_edits["dictate"].setText("CTRL+ALT+d")
    dlg.save_button.click()
    assert store.writes == [{"dictate": "CTRL+ALT+d", "recall": "", "cancel": "",
                             "language_toggle": ""}]
    assert Config.load().get("hotkeys.portal_dictate") == "CTRL+ALT+d"
    assert dlg.error_label.text() == ""                  # success is not an error
    # The store's own sentence names its ids and its spelling of the key; the
    # window says the same thing in words, and logs that one.
    assert dlg.hotkey_status.text() == SAVED_TO_DESKTOP


def test_a_successful_write_asks_for_a_rebind(qapp):
    """Without a rebind the listener keeps the old key until the daemon restarts."""
    store = FakeStore()
    cfg, dlg = portal_dialog_with_store(qapp, store)
    rebound = []
    dlg.shortcuts_rebound.connect(lambda: rebound.append(True))
    dlg.portal_edits["dictate"].setText("CTRL+ALT+d")
    dlg.save_button.click()
    assert rebound == [True]


def test_a_refused_write_lands_in_the_red_label_and_asks_for_no_rebind(qapp):
    store = FakeStore(fail="/org/gnome/... is not stored yet")
    cfg, dlg = portal_dialog_with_store(qapp, store)
    rebound = []
    dlg.shortcuts_rebound.connect(lambda: rebound.append(True))
    dlg.portal_edits["dictate"].setText("CTRL+ALT+d")
    dlg.save_button.click()
    assert "not stored yet" in dlg.error_label.text()
    assert rebound == []
    # The config was still saved: the preference is ours to keep either way.
    assert Config.load().get("hotkeys.portal_dictate") == "CTRL+ALT+d"


def test_a_desktop_with_no_such_store_is_left_alone(qapp):
    """KDE keeps its shortcuts elsewhere and has its own dialog for them."""
    cfg, dlg = portal_dialog_with_store(qapp, None)
    dlg.portal_edits["dictate"].setText("CTRL+ALT+d")
    dlg.save_button.click()
    assert dlg.error_label.text() == ""
    assert Config.load().get("hotkeys.portal_dictate") == "CTRL+ALT+d"


def test_the_evdev_backend_never_writes_the_desktops_store(qapp):
    store = FakeStore()
    cfg = Config.load()
    dlg = SettingsDialog(cfg, capture_key=lambda cb: None, sources=lambda: [],
                         shortcut_store=lambda: store)
    dlg.hotkey_edit.setText("KEY_F14")
    dlg.save_button.click()
    assert store.writes == []


def test_a_save_that_is_refused_never_reaches_the_desktop(qapp):
    """A config the daemon would reject must not leave the desktop rebound to a
    key nothing listens for."""
    store = FakeStore()
    cfg, dlg = portal_dialog_with_store(qapp, store)
    dlg.portal_edits["dictate"].setText("   ")           # errors() refuses an empty one
    dlg.save_button.click()
    assert store.writes == []
    assert dlg.error_label.text().startswith(ACTION_LABELS["dictate"])


def test_a_store_that_blows_up_is_reported_rather_than_crashing_the_window(qapp):
    class Exploding:
        name = "GNOME"

        def write(self, triggers):
            raise RuntimeError("dconf went away")

    cfg, dlg = portal_dialog_with_store(qapp, Exploding())
    dlg.portal_edits["dictate"].setText("CTRL+ALT+d")
    dlg.save_button.click()
    assert "dconf went away" in dlg.error_label.text()


def test_the_hotkeys_tab_says_what_the_key_does_and_who_owns_it(qapp):
    """The owner: "the text in yellow especially for KDE means what??". The tab
    says who manages the keys and what the button will do - in that order, in
    words, with no mechanism named anywhere."""
    cfg, dlg = portal_dialog_with_store(qapp, FakeStore())
    words = (dlg.hotkey_owner_label.text() + " " + dlg.hotkey_help_text()).lower()
    assert "your desktop" in words                         # who owns the key
    assert "change" in words                               # and what the button does
    assert ACTION_LABELS["dictate"] in [label for _name, label in ACTIONS]


# -- and whether this desktop will honour it at all -----------------------------
def test_the_placer_says_when_this_desktop_places_the_window_itself(qapp):
    """The owner dragged the pill and reported "it doesn't work, always in the
    middle". It is a GTK window on GNOME: nothing can move it, and the only
    record of that was a log line nobody sees."""
    from voice.ui.placement import NO_LAYER_SHELL_NOTE

    cfg, dlg, _ = make(qapp)
    assert dlg.placement_warning.isVisibleTo(dlg) is False      # nothing known yet
    dlg.set_layer_shell(False)
    assert dlg.placement_warning.isVisibleTo(dlg) is True
    assert NO_LAYER_SHELL_NOTE in dlg.placement_warning.text()
    # The setting is still recorded: it applies on the owner's KDE machine.
    assert dlg.pill_placer.isEnabled() is True


def test_a_desktop_that_can_place_it_says_nothing(qapp):
    cfg, dlg, _ = make(qapp)
    dlg.set_layer_shell(True)
    assert dlg.placement_warning.isVisibleTo(dlg) is False
    dlg.set_layer_shell(None)                                   # unknown again
    assert dlg.placement_warning.isVisibleTo(dlg) is False


def test_the_warning_survives_a_reload_of_the_window(qapp):
    """Reopening re-reads the file; what the desktop can do has not changed."""
    cfg, dlg, _ = make(qapp)
    dlg.set_layer_shell(False)
    dlg.reload_from_disk()
    assert dlg.placement_warning.isVisibleTo(dlg) is True


# -- showing the owner where the pill would land -------------------------------
def _previewing(qapp, reply=None, **values):
    """A dialog whose preview requests are recorded instead of shown."""
    asked = []

    def preview(position, margin_x, margin_y):
        asked.append((position, margin_x, margin_y))
        return reply if reply is not None else {"ok": True, "seconds": 5.0}

    cfg = Config.load()
    for key, value in values.items():
        cfg.set(key, value)
    cfg.save()
    dlg = SettingsDialog(Config.load(), capture_key=lambda cb: None, sources=lambda: [],
                         preview_pill=preview)
    return dlg, asked


def test_dropping_the_pill_asks_the_daemon_to_show_it(qapp):
    """The owner's "can it show the position on the desktop for say 5 sec when I
    drop it in settings so I see where it would show?"."""
    from voice.ui.settings import PREVIEW_SHOWING

    dlg, asked = _previewing(qapp)
    _drag_pill_to(dlg, "top-right", 12, 30)
    assert asked == []                                  # not until the drag settles
    dlg.preview_timer.timeout.emit()                    # the pause after the drop
    assert asked == [("top-right", 12, 30)]
    assert PREVIEW_SHOWING.split("…")[0] in dlg.preview_note.text()
    assert dlg.error_label.text() == ""                 # it is not an error


def test_a_run_of_nudges_asks_once(qapp):
    """Arrow keys fire per keystroke; a helper restarted per keystroke would
    flicker across the screen."""
    dlg, asked = _previewing(qapp)
    for _ in range(5):
        _drag_pill_to(dlg, "top-left", 0, 0)
    assert asked == []
    assert dlg.preview_timer.isActive() is True
    dlg.preview_timer.timeout.emit()
    assert asked == [("top-left", 0, 0)]


def test_merely_opening_the_window_shows_nothing(qapp):
    dlg, asked = _previewing(qapp, **{"ui.overlay_position": "middle-left"})
    assert asked == []
    assert dlg.preview_timer.isActive() is False
    dlg.reload_from_disk()
    assert asked == []


def test_a_refused_preview_says_why_without_alarming_anyone(qapp):
    dlg, asked = _previewing(qapp, reply={"ok": False, "error": "not while a dictation is running"})
    _drag_pill_to(dlg, "top-left", 0, 0)
    dlg.preview_timer.timeout.emit()
    assert "dictating" in dlg.preview_note.text()
    assert dlg.error_label.text() == ""


def test_a_window_with_no_daemon_behind_it_just_does_not_preview(qapp):
    cfg, dlg, _ = make(qapp)                            # no preview callback at all
    _drag_pill_to(dlg, "top-left", 0, 0)
    assert dlg.preview_timer.isActive() is False
    dlg.preview_timer.timeout.emit()                    # and firing it is harmless
    assert dlg.preview_note.text() == ""


def test_a_preview_that_blows_up_does_not_take_the_window_with_it(qapp):
    def boom(position, margin_x, margin_y):
        raise RuntimeError("the daemon went away")

    dlg = SettingsDialog(Config.load(), capture_key=lambda cb: None, sources=lambda: [],
                         preview_pill=boom)
    _drag_pill_to(dlg, "top-left", 0, 0)
    dlg.preview_timer.timeout.emit()
    assert "went away" in dlg.preview_note.text()


# -- the shape of the window itself --------------------------------------------
def _labels(dlg):
    from PySide6.QtWidgets import QLabel

    return [w for w in dlg.findChildren(QLabel) if w.text().strip()]


@pytest.mark.parametrize("backend", ["evdev", "portal"])
def test_no_tab_shows_a_paragraph_of_prose(qapp, backend):
    """The owner: "the huge text pieces could be a question mark or something
    the user clicks to show more information". Nothing on a tab may be an essay;
    the detail lives behind the "?" beside the control it explains."""
    from voice.ui.settings import MAX_INLINE_TEXT

    cfg = Config.load()
    dlg = SettingsDialog(cfg, capture_key=lambda cb: None, sources=lambda: [],
                         backend=backend, shortcut_store=lambda: None)
    too_long = [label.text() for label in _labels(dlg) if len(label.text()) > MAX_INLINE_TEXT]
    assert too_long == [], f"{len(too_long)} paragraph(s) still on a tab"


def test_the_long_explanations_are_behind_help_buttons(qapp):
    cfg, dlg, _ = make(qapp)
    assert "pill_position" in dlg.help_buttons
    assert "anchor" in dlg.help_buttons["pill_position"].help_text
    assert "hotkeys" in dlg.help_buttons
    assert "profile_per_language" in dlg.help_buttons
    for button in dlg.help_buttons.values():
        assert button.text() == "?"
        assert button.toolTip()                       # hovering says it too
        assert button.width() <= 24 and button.height() <= 24


def test_a_help_button_shows_its_text_when_clicked(qapp, monkeypatch):
    shown = []
    monkeypatch.setattr("voice.ui.settings.QToolTip.showText",
                        lambda point, text, *a, **k: shown.append(text))
    cfg, dlg, _ = make(qapp)
    dlg.help_buttons["pill_position"].click()
    assert shown and "anchor" in shown[0]


@pytest.mark.parametrize("backend", ["evdev", "portal"])
def test_every_control_survived_the_polish(qapp, backend):
    """This is presentation only: the same widgets, in a tidier arrangement."""
    cfg = Config.load()
    dlg = SettingsDialog(cfg, capture_key=lambda cb: None, sources=lambda: [],
                         backend=backend, shortcut_store=lambda: None)
    for name in ("language_combo", "notifications_combo", "inject_mode_combo", "pill_placer",
                 "pill_placement_label", "placement_warning", "preview_note",
                 "language_profile_table", "hotkey_edit", "capture_button", "mode_combo",
                 "recall_edit", "cancel_edit", "language_toggle_edit", "hotkey_owner_label",
                 "hotkey_status", "advanced_button", "advanced_box",
                 "device_combo", "max_seconds",
                 "profile_list", "add_profile_combo", "add_profile_button", "activate_button",
                 "active_label", "replacements_table", "hotwords_edit",
                 "error_label", "save_button"):
        widget = getattr(dlg, name)
        assert widget is not None, name
        assert widget.parentWidget() is not None, f"{name} is not in the layout"
    tabs = dlg.findChildren(__import__("PySide6.QtWidgets", fromlist=["QTabWidget"]).QTabWidget)[0]
    assert [tabs.tabText(i) for i in range(tabs.count())] == [
        "General", "Hotkeys", "Audio", "Transcription", "Dictionary"]


def test_the_form_labels_line_up_in_one_column(qapp):
    """A column that moves per row is what makes a window look thrown together."""
    from PySide6.QtWidgets import QFormLayout

    cfg, dlg, _ = make(qapp)
    dlg.resize(dlg.sizeHint())
    dlg.show()
    qapp.processEvents()
    for form in dlg.findChildren(QFormLayout):
        # The labels are right-aligned against the field column, so it is their
        # right edge that has to be one line down the form, not their left.
        edges = set()
        for row in range(form.rowCount()):
            item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            if item is not None and item.widget() is not None and item.widget().text():
                edges.add(item.widget().geometry().right())
        assert len(edges) <= 1, f"labels end at {sorted(edges)}"
    dlg.close()


def test_the_profile_table_shows_every_language_without_scrolling(qapp, isolated_xdg):
    """Two languages must both be visible: the table is sized, not scrolled.

    A scrollbar appearing inside a fixed-height table steals the height the
    second row needs, which is how this shipped showing a row and a half.
    """
    cfg = Config.load()
    cfg.set("general.languages", ["en", "sv"])
    cfg.save()
    dlg = SettingsDialog(cfg, capture_key=lambda cb: None, sources=lambda: [])
    table = dlg.language_profile_table
    assert table.rowCount() == 2
    needed = table.horizontalHeader().height() + sum(
        table.rowHeight(r) for r in range(table.rowCount()))
    assert table.height() >= needed, (
        f"table is {table.height()}px for {needed}px of header and rows")
    assert table.horizontalScrollBar().isVisibleTo(table) is False


# -- legible on whatever theme the desktop is running ---------------------------
def dark_palette(derive: bool = True):
    """A dark theme, as a palette.

    `derive` fills in the shade roles (Mid, Dark, Midlight...) the way a real
    dark theme does - which is what made `palette(mid)` dark grey on dark grey
    and started this - and gives the disabled group its own grey, as every real
    theme does. The flat one is a palette built by hand: it leaves the shade
    roles light, `PlaceholderText` black, and the disabled `WindowText` equal to
    the ordinary one, so a fix that trusts any single role is caught by one of
    the two.
    """
    from PySide6.QtGui import QColor, QPalette

    palette = QPalette(QColor("#353535")) if derive else QPalette()
    for role, colour in ((QPalette.ColorRole.Window, "#2b2b2b"),
                         (QPalette.ColorRole.Base, "#2b2b2b"),
                         (QPalette.ColorRole.WindowText, "#dcdcdc"),
                         (QPalette.ColorRole.Text, "#dcdcdc"),
                         (QPalette.ColorRole.Button, "#353535"),
                         (QPalette.ColorRole.ButtonText, "#dcdcdc"),
                         (QPalette.ColorRole.Highlight, "#3584e4"),
                         (QPalette.ColorRole.HighlightedText, "#ffffff")):
        palette.setColor(role, QColor(colour))
    if derive:
        # setColor sets every group at once, which would leave the disabled
        # text the same colour as the ordinary text - something no real theme
        # does, and it would hide the rung of the ladder that reads that role.
        for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text):
            palette.setColor(QPalette.ColorGroup.Disabled, role, QColor("#6e7173"))
    return palette


def palettes(qapp):
    """The three themes every piece of dim text has to survive."""
    from PySide6.QtGui import QPalette

    return {"light": QPalette(qapp.palette()),
            "dark": dark_palette(derive=True),
            "flat dark": dark_palette(derive=False)}


@pytest.fixture
def themed(qapp):
    """Builds the window under a given palette, and puts the old one back."""
    from PySide6.QtGui import QPalette

    original = QPalette(qapp.palette())
    built = []

    def build(palette):
        qapp.setPalette(palette)
        dlg = SettingsDialog(Config.load(), capture_key=lambda cb: None, sources=lambda: [])
        dlg.setPalette(palette)
        built.append(dlg)
        return dlg

    yield build
    for dlg in built:
        dlg.close()
    qapp.setPalette(original)


@pytest.mark.parametrize("theme", ["light", "dark", "flat dark"])
def test_dim_text_is_legible_on_every_theme(qapp, theme):
    """The owner runs a dark desktop and reported "the help text is invisible
    now": a grey taken from `palette(mid)` is dark grey on a dark window.

    Measured against the hardest surface the palette offers, because the style
    does not paint `Window` behind a caption - it paints a panel it derives from
    `Button`, a shade further out than anything the palette names.
    """
    from voice.ui.settings import (MIN_CONTRAST, conservative_background, contrast_ratio,
                                   secondary_text_colour)

    palette = palettes(qapp)[theme]
    behind = conservative_background(palette)
    ratio = contrast_ratio(secondary_text_colour(palette), behind)
    assert ratio >= MIN_CONTRAST, f"{theme}: dim text at {ratio:.2f}:1 against {behind.name()}"


def test_the_bar_is_the_one_for_text_people_have_to_read(qapp):
    """3:1 is the floor for incidental text and it left no margin at all: the
    worst caption measured 3.14:1 on the real pixels behind it."""
    from voice.ui.settings import MIN_CONTRAST

    assert MIN_CONTRAST >= 4.5


@pytest.mark.parametrize("theme", ["light", "dark", "flat dark"])
def test_the_error_line_is_legible_on_every_theme(qapp, theme):
    """Red on a mid-grey window is a signal nobody can see."""
    from voice.ui.settings import (MIN_CONTRAST, conservative_background, contrast_ratio,
                                   error_text_colour)

    palette = palettes(qapp)[theme]
    behind = conservative_background(palette)
    ratio = contrast_ratio(error_text_colour(palette), behind)
    assert ratio >= MIN_CONTRAST, f"{theme}: the error line at {ratio:.2f}:1"


def test_the_captions_and_the_question_marks_follow_the_theme(qapp, themed):
    """Not one fixed grey that happens to suit one desktop: two themes, two
    colours, each readable on the window it is painted on."""
    from PySide6.QtGui import QPalette

    from voice.ui.settings import MIN_CONTRAST, conservative_background, contrast_ratio

    seen = {}
    for theme in ("light", "dark"):
        palette = palettes(qapp)[theme]
        dlg = themed(palette)
        behind = conservative_background(palette)
        caption = dlg.pill_placement_label.palette().color(QPalette.ColorRole.WindowText)
        glyph = dlg.help_buttons["pill_position"].glyph_colour
        for what, colour in (("caption", caption), ("?", glyph)):
            ratio = contrast_ratio(colour, behind)
            assert ratio >= MIN_CONTRAST, f"{theme} {what} at {ratio:.2f}:1"
        seen[theme] = (caption.name(), glyph.name())
    assert seen["light"] != seen["dark"], f"the same colour on both themes: {seen}"


def test_the_window_writes_no_colour_of_its_own(qapp):
    """A colour written into a stylesheet cannot follow a theme; that is the
    whole defect. What is left in the static styles is layout, not colour."""
    import re

    from voice.ui.settings import DIALOG_STYLE

    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", DIALOG_STYLE)
    assert "color" not in DIALOG_STYLE


@pytest.mark.parametrize("how", ["the whole application", "this window"])
def test_a_theme_change_while_the_window_is_open_is_followed(qapp, themed, how):
    """A desktop that switches to dark at sunset must not leave the window's
    dim text in yesterday's colour.

    Both ways it can arrive. A desktop changes the application's palette, which
    reaches every widget on its own; a palette set on the window itself does
    not, because this dialog has a stylesheet and a stylesheet stops Qt passing
    a palette down the tree - even one with no colour in it. The captions had to
    be told by hand, and this is the case that proves they are.
    """
    from PySide6.QtGui import QPalette

    from voice.ui.settings import MIN_CONTRAST, conservative_background, contrast_ratio

    dlg = themed(palettes(qapp)["light"])
    dark = palettes(qapp)["dark"]
    if how == "the whole application":
        qapp.setPalette(dark)
    else:
        dlg.setPalette(dark)
    qapp.processEvents()
    behind = conservative_background(dark)
    caption = dlg.pill_placement_label.palette().color(QPalette.ColorRole.WindowText)
    glyph = dlg.help_buttons["pill_position"].glyph_colour
    assert contrast_ratio(caption, behind) >= MIN_CONTRAST, "the caption kept the old theme"
    assert contrast_ratio(glyph, behind) >= MIN_CONTRAST, "the \"?\" kept the old theme"


# -- and saying what the preview actually did -----------------------------------
def test_a_refusal_says_what_the_owner_can_do_about_it(qapp):
    """"not while a dictation is running" is the daemon's wording, not words to
    read in a settings window while wondering why nothing happened."""
    from voice.ui.settings import PREVIEW_REFUSALS

    dlg, asked = _previewing(qapp, reply={"ok": False,
                                          "error": "not while a dictation is running"})
    _drag_pill_to(dlg, "top-left", 0, 0)
    dlg.preview_timer.timeout.emit()
    assert dlg.preview_note.text() == PREVIEW_REFUSALS["not while a dictation is running"]
    assert "dictating" in dlg.preview_note.text()
    assert "try again" in dlg.preview_note.text()


def test_a_refusal_nobody_wrote_words_for_is_still_shown(qapp):
    """The other refusal - no pill to show - must not vanish into a log line."""
    dlg, asked = _previewing(qapp, reply={"ok": False,
                                          "error": "the recording pill is off (ui.overlay = false)"})
    _drag_pill_to(dlg, "top-left", 0, 0)
    dlg.preview_timer.timeout.emit()
    assert "ui.overlay = false" in dlg.preview_note.text()


@pytest.mark.parametrize("reply", [{"ok": True, "seconds": 5.0},
                                   {"ok": False, "error": "not while a dictation is running"}])
def test_the_note_clears_itself_so_nothing_stale_lingers(qapp, reply):
    """The pill is gone after five seconds; a line still saying it is showing is
    a line the owner will act on."""
    dlg, asked = _previewing(qapp, reply=reply)
    _drag_pill_to(dlg, "top-left", 0, 0)
    dlg.preview_timer.timeout.emit()
    assert dlg.preview_note.text() != ""
    assert dlg.preview_note_timer.isActive() is True
    dlg.preview_note_timer.timeout.emit()
    assert dlg.preview_note.text() == ""


def test_a_new_drag_drops_the_note_left_by_the_last_one(qapp):
    """The note describes a placement; the moment that placement changes it is
    about something that is no longer on screen."""
    dlg, asked = _previewing(qapp)
    _drag_pill_to(dlg, "top-left", 0, 0)
    dlg.preview_timer.timeout.emit()
    assert dlg.preview_note.text() != ""
    _drag_pill_to(dlg, "bottom-right", 4, 4)
    assert dlg.preview_note.text() == ""


def test_both_notes_show_at_once_without_contradicting_each_other(qapp):
    """On GNOME the pill does appear - just not where it was dropped. Saying
    "showing it there" beside "your desktop places it itself" is worse than
    saying nothing."""
    from voice.ui.placement import NO_LAYER_SHELL_NOTE
    from voice.ui.settings import PREVIEW_SHOWING_ANYWHERE

    dlg, asked = _previewing(qapp)
    dlg.set_layer_shell(False)
    _drag_pill_to(dlg, "top-left", 0, 0)
    dlg.preview_timer.timeout.emit()
    assert dlg.placement_warning.text() == NO_LAYER_SHELL_NOTE
    assert dlg.placement_warning.isVisibleTo(dlg) is True
    assert dlg.preview_note.text() == PREVIEW_SHOWING_ANYWHERE
    assert "there" not in dlg.preview_note.text()


def test_every_note_under_the_placer_fits_its_line(qapp):
    """Three notes that each wrap to three lines is the wall of text this window
    was just dug out of - and the owner's desktop font is bigger than this one's,
    so a note that only just fits here does not fit there."""
    from PySide6.QtGui import QFontMetrics

    from voice.ui.placement import NO_LAYER_SHELL_NOTE, placement_summary
    from voice.ui.settings import (PREVIEW_CANNOT, PREVIEW_REFUSALS, PREVIEW_REFUSED,
                                   PREVIEW_SHOWING, PREVIEW_SHOWING_ANYWHERE)

    from voice.ui.settings import MARGIN

    cfg, dlg, _ = make(qapp)
    label = dlg.placement_warning
    # The narrowest the window is allowed to be, less the dialog's margins and
    # the group box's - measured, not laid out, so it does not turn on whatever
    # the widest tab happens to want today. A fifth is left over for the larger
    # font the owner runs: a note that only just fits here wraps there.
    room = (dlg.minimumWidth() - 4 * MARGIN) * 0.8
    metrics = QFontMetrics(label.font())
    notes = [NO_LAYER_SHELL_NOTE, PREVIEW_SHOWING, PREVIEW_SHOWING_ANYWHERE, PREVIEW_CANNOT,
             placement_summary("bottom-right", 2000, 2000),
             PREVIEW_REFUSED.format(error="the recording pill is off (ui.overlay = false)"),
             *PREVIEW_REFUSALS.values()]
    too_wide = [(n, metrics.horizontalAdvance(n)) for n in notes
                if metrics.horizontalAdvance(n) > room]
    assert too_wide == [], f"{room:.0f}px of line: {too_wide}"


@pytest.mark.parametrize("reply", [{"ok": True},                       # says nothing
                                   {"ok": True, "seconds": "soon"},    # says nonsense
                                   {"ok": True, "seconds": 0}])        # says nothing useful
def test_a_reply_that_will_not_say_how_long_still_clears_the_note(qapp, reply):
    """The note is set from a Qt slot: a daemon a version ahead may not be able
    to raise out of one, and may not leave a line up for ever either."""
    from voice.ui.settings import ASSUMED_PREVIEW_SECONDS

    dlg, asked = _previewing(qapp, reply=reply)
    _drag_pill_to(dlg, "top-left", 0, 0)
    dlg.preview_timer.timeout.emit()
    assert dlg.preview_note.text() != ""
    assert dlg.preview_note_timer.interval() == int(ASSUMED_PREVIEW_SECONDS * 1000)
    dlg.preview_note_timer.timeout.emit()
    assert dlg.preview_note.text() == ""


def _ink_on_paper(image):
    """(paper, ink, contrast) for a rendered widget, off its pixels.

    The paper is the colour most of the crop is; the ink is the colour furthest
    from it in luminance that is not a stray antialiased pixel. This is the only
    honest way to check contrast - the crop has to come out of a render of the
    whole window, because a widget grabbed on its own paints its palette's
    background rather than the panel the style really put behind it, and looking
    at the picture does not work either: an image viewer that normalises
    contrast shows 1.5:1 text as perfectly readable.
    """
    import collections

    from PySide6.QtGui import QColor

    from voice.ui.settings import _luminance, contrast_ratio

    counts = collections.Counter()
    for y in range(image.height()):
        for x in range(image.width()):
            counts[image.pixelColor(x, y).rgb()] += 1
    paper = QColor.fromRgb(counts.most_common(1)[0][0])
    ink, furthest = paper, 0.0
    for rgb in counts:
        # The furthest pixel from the paper, however few there are of it: on a
        # "?" barely any pixel is fully covered, and taking only the ones that
        # repeat would measure the antialiasing rather than the colour.
        colour = QColor.fromRgb(rgb)
        gap = abs(_luminance(colour) - _luminance(paper))
        if gap > furthest:
            ink, furthest = colour, gap
    return paper, ink, contrast_ratio(paper, ink)


def _dim_things(dlg, page):
    """Every piece of secondary text on `page`: the captions and the "?"s."""
    from PySide6.QtWidgets import QLabel

    from voice.ui.settings import HelpButton

    things = [(w, w.text()[:44]) for w in page.findChildren(QLabel)
              if w.text().strip() and w.property("caption") and w.isVisibleTo(dlg)]
    things += [(w, '"?"') for w in page.findChildren(HelpButton) if w.isVisibleTo(dlg)]
    return things


@pytest.mark.parametrize("theme", ["light", "dark", "flat dark"])
def test_every_dim_thing_on_every_tab_is_legible_in_every_theme(qapp, themed, theme):
    """The end of the argument: not what the palette says, not what the picture
    looks like, but the contrast between the pixels of every caption and every
    "?" and the pixels the style actually painted behind them - on all five
    tabs, in each theme.
    """
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QTabWidget

    from voice.ui.settings import MIN_CONTRAST

    dlg = themed(palettes(qapp)[theme])
    dlg.resize(600, 620)
    dlg.show()
    qapp.processEvents()
    tabs = dlg.findChildren(QTabWidget)[0]
    failures, worst = [], (99.0, "")
    for index in range(tabs.count()):
        tabs.setCurrentIndex(index)
        qapp.processEvents()
        shot = dlg.grab().toImage()
        for widget, what in _dim_things(dlg, tabs.widget(index)):
            corner = widget.mapTo(dlg, QPoint(0, 0))
            crop = shot.copy(corner.x(), corner.y(), widget.width(), widget.height())
            paper, ink, ratio = _ink_on_paper(crop)
            where = f"{tabs.tabText(index)}: {what} ({ink.name()} on {paper.name()})"
            worst = min(worst, (ratio, where))
            if ratio < MIN_CONTRAST:
                failures.append(f"{ratio:.2f}:1  {where}")
    assert tabs.count() == 5 and worst[1], "no dim text was measured at all"
    assert failures == [], f"worst {worst[0]:.2f}:1 at {worst[1]}\n" + "\n".join(failures)


# -- one list, one button per key ------------------------------------------------
def _press(qapp, widget, key, modifiers=None):
    """One key press, delivered the way a desktop would deliver it."""
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    modifiers = Qt.KeyboardModifier.NoModifier if modifiers is None else modifiers
    qapp.sendEvent(widget, QKeyEvent(QEvent.Type.KeyPress, key, modifiers))
    qapp.processEvents()


def _visible_words(dlg):
    """Every word this window puts on screen, with the widget it belongs to.

    Labels, buttons, group titles, combo entries, table headers and the grey
    text inside an empty field - everything a person reads, whether or not the
    tab it sits on is the one in front.
    """
    from PySide6.QtWidgets import (QAbstractButton, QComboBox, QGroupBox, QLabel, QLineEdit,
                                   QTableWidget, QTabWidget)

    found = []
    for widget in dlg.findChildren(QLabel):
        found.append((widget, widget.text()))
    for widget in dlg.findChildren(QAbstractButton):
        found.append((widget, widget.text()))
    for widget in dlg.findChildren(QGroupBox):
        found.append((widget, widget.title()))
    for widget in dlg.findChildren(QComboBox):
        found += [(widget, widget.itemText(i)) for i in range(widget.count())]
    for widget in dlg.findChildren(QLineEdit):
        found.append((widget, widget.placeholderText()))
    for widget in dlg.findChildren(QTabWidget):
        found += [(widget, widget.tabText(i)) for i in range(widget.count())]
    for widget in dlg.findChildren(QTableWidget):
        for col in range(widget.columnCount()):
            item = widget.horizontalHeaderItem(col)
            found.append((widget, item.text() if item else ""))
    return [(widget, text) for widget, text in found if text.strip()]


def _hotkeys_dialog(qapp, route, triggers=None, capture=lambda cb: None, store=None):
    """A window on each of the three machines this tab has to work on."""
    cfg = Config.load()
    backend = "evdev" if route == "capture" else "portal"
    return cfg, SettingsDialog(cfg, capture_key=capture, sources=lambda: [],
                               backend=backend, triggers=triggers,
                               shortcut_store=lambda: store)


@pytest.mark.parametrize("route", ["capture", "desktop-store", "desktop-dialog"])
def test_every_action_has_one_row_with_its_key_and_one_button(qapp, route):
    """The owner: "I don't see a record button". One list of what the keys do,
    each row with the key on it and one button that changes it."""
    store = FakeStore() if route == "desktop-store" else None
    cfg, dlg = _hotkeys_dialog(qapp, route, triggers=lambda: {"dictate": "CTRL+space"},
                               store=store)
    assert [name for name, _label in ACTIONS] == ["dictate", "cancel", "language_toggle",
                                                  "recall"]
    assert set(dlg.key_labels) == set(dlg.change_buttons) == set(ACTION_LABELS)
    from PySide6.QtWidgets import QTabWidget

    dlg.show()
    dlg.findChildren(QTabWidget)[0].setCurrentIndex(1)
    qapp.processEvents()
    for name in ACTION_LABELS:
        assert dlg.change_buttons[name].text() == CHANGE
        # On the tab, not merely built: the dictate row lost its button to the
        # Advanced section once, because a widget has only one parent.
        assert dlg.change_buttons[name].isVisible(), name
        assert dlg.key_labels[name].isVisible() and dlg.key_labels[name].text()
    shown = dlg.key_labels["dictate"].text()
    assert shown == ("F13" if route == "capture" else "Ctrl+Space")
    dlg.close()


def test_the_row_shows_the_key_the_way_a_person_writes_it(qapp):
    cfg, dlg, _ = make(qapp)
    dlg.hotkey_edit.setText("KEY_LEFTMETA+KEY_SPACE")
    assert dlg.key_labels["dictate"].text() == "Super+Space"
    dlg.cancel_edit.setText("KEY_ESC")
    assert dlg.key_labels["cancel"].text() == "Escape"
    dlg.recall_edit.setText("")
    assert dlg.key_labels["recall"].text() == NOT_SET


@pytest.mark.parametrize("route", ["capture", "desktop-store", "desktop-dialog"])
def test_the_top_sentence_says_who_manages_the_keys(qapp, route):
    """One line, in plain words, so a user on any desktop knows who to argue
    with - and no second set of fields to wonder about."""
    store = FakeStore() if route == "desktop-store" else None
    cfg, dlg = _hotkeys_dialog(qapp, route, store=store)
    assert dlg.hotkey_owner_label.text() == WHO_MANAGES[route]
    if route == "capture":
        assert "your keyboard" in dlg.hotkey_owner_label.text().lower()
    else:
        assert "your desktop" in dlg.hotkey_owner_label.text().lower()


def test_change_takes_the_next_key_where_voice_reads_the_keyboard(qapp):
    captures = []
    cfg, dlg = _hotkeys_dialog(qapp, "capture", capture=captures.append)
    dlg.change_buttons["cancel"].click()
    assert dlg.change_buttons["cancel"].text() == "Press a key…"
    assert "press the key" in dlg.hotkey_status.text().lower()
    captures[0]("KEY_F14")                       # the listener thread answers
    qapp.processEvents()
    assert dlg.cancel_edit.text() == "KEY_F14"
    assert dlg.key_labels["cancel"].text() == "F14"
    assert dlg.change_buttons["cancel"].text() == CHANGE
    dlg.save_button.click()
    assert Config.load().get("hotkeys.cancel") == "KEY_F14"


def test_change_hands_the_new_combination_to_the_desktop(qapp):
    """Where the desktop owns the keys but keeps them somewhere we may write,
    the button reads the combination here and gives it to the desktop at once."""
    from PySide6.QtCore import Qt

    store = FakeStore()
    rebound = []
    cfg, dlg = _hotkeys_dialog(qapp, "desktop-store", triggers=lambda: {"dictate": "CTRL+space"},
                               store=store)
    dlg.shortcuts_rebound.connect(lambda: rebound.append(True))
    dlg.change_buttons["dictate"].click()
    assert dlg.change_buttons["dictate"].text() == "Press the keys…"
    assert "press the keys" in dlg.hotkey_status.text().lower()
    _press(qapp, dlg, Qt.Key.Key_Control, Qt.KeyboardModifier.ControlModifier)   # half of one
    assert dlg.change_buttons["dictate"].text() == "Press the keys…"
    _press(qapp, dlg, Qt.Key.Key_D,
           Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
    assert store.writes == [{"dictate": "CTRL+ALT+D", "cancel": "", "language_toggle": "",
                             "recall": ""}]
    assert rebound == [True]
    assert dlg.key_labels["dictate"].text() == "Ctrl+Alt+D"
    assert dlg.hotkey_status.text() == CHANGED_ON_DESKTOP.format(
        key="Ctrl+Alt+D", what=ACTION_LABELS["dictate"].lower())
    assert dlg.change_buttons["dictate"].text() == CHANGE
    dlg.save_button.click()
    assert Config.load().get("hotkeys.portal_dictate") == "CTRL+ALT+D"


def test_a_key_the_desktop_took_is_kept_without_a_save(qapp):
    """Change… then Close (not Save) used to throw the change away: the field
    was repopulated from the file, which still held the old trigger, while the
    row showed the desktop's new one - so the tab contradicted itself, and the
    next Save for any reason pushed the stale trigger back over the user's key.
    """
    from PySide6.QtCore import Qt

    store = FakeStore()
    desktop = {"dictate": "CTRL+space"}
    cfg, dlg = _hotkeys_dialog(qapp, "desktop-store", triggers=lambda: dict(desktop),
                               store=store)
    dlg.change_buttons["dictate"].click()
    _press(qapp, dlg, Qt.Key.Key_D,
           Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
    assert Config.load().get("hotkeys.portal_dictate") == "CTRL+ALT+D"
    desktop["dictate"] = "CTRL+ALT+D"           # the desktop holds it from now on
    dlg.close()                                 # Close, not Save
    dlg.reload_from_disk()                      # which is what reopening it does

    assert dlg.portal_edits["dictate"].text() == "CTRL+ALT+D"   # both halves of the tab
    assert dlg.key_labels["dictate"].text() == "Ctrl+Alt+D"     # say the same thing
    dlg.save_button.click()                     # and a later save cannot revert it
    assert Config.load().get("hotkeys.portal_dictate") == "CTRL+ALT+D"


def test_a_key_the_desktop_refused_is_not_kept(qapp):
    """The file follows the desktop, so a write that did not happen writes
    nothing here either."""
    from PySide6.QtCore import Qt

    store = FakeStore(fail="that shortcut is not stored yet")
    cfg, dlg = _hotkeys_dialog(qapp, "desktop-store", triggers=lambda: {"dictate": "CTRL+space"},
                               store=store)
    dlg.change_buttons["dictate"].click()
    _press(qapp, dlg, Qt.Key.Key_D, Qt.KeyboardModifier.ControlModifier)
    assert Config.load().get("hotkeys.portal_dictate") == "CTRL+space"


def test_escape_leaves_the_key_as_it_was(qapp):
    from PySide6.QtCore import Qt

    store = FakeStore()
    cfg, dlg = _hotkeys_dialog(qapp, "desktop-store", triggers=lambda: {"dictate": "CTRL+space"},
                               store=store)
    dlg.change_buttons["dictate"].click()
    _press(qapp, dlg, Qt.Key.Key_Escape)
    assert store.writes == []
    assert dlg.key_labels["dictate"].text() == "Ctrl+Space"
    assert dlg.change_buttons["dictate"].text() == CHANGE
    assert dlg.hotkey_status.text() == CHANGE_STOPPED
    _press(qapp, dlg, Qt.Key.Key_D, Qt.KeyboardModifier.ControlModifier)   # no longer listening
    assert store.writes == []


#: What may be handed to the desktop as a global shortcut, and what may not.
#: A bare key here is a key taken away from every other window on the machine -
#: and "Change…" grabs the whole application, so a user who clicks it and then
#: types anywhere in this window used to rebind to whatever they typed.
@pytest.mark.parametrize("key,modifiers,trigger", [
    ("Key_A", "ControlModifier", "CTRL+A"),
    ("Key_D", "ControlModifier|AltModifier", "CTRL+ALT+D"),
    ("Key_Space", "ControlModifier", "CTRL+Space"),
    ("Key_F13", None, "F13"),                    # a function key needs no modifier
    ("Key_Insert", None, "Ins"),                 # nor does a key nobody types with
    ("Key_A", None, None),                       # a bare letter: never
    ("Key_1", None, None),                       # nor a bare digit
    ("Key_Space", None, None),                   # nor the space bar itself
    ("Key_Return", None, None),
    ("Key_Backspace", None, None),
    # Punctuation. Qt hands us the bare character, which GTK cannot parse, so
    # the trigger has to carry the keysym name instead - and `+` in particular
    # is not even separable in our own "CTRL+plus" syntax as a character.
    ("Key_Comma", "ControlModifier", "CTRL+comma"),
    ("Key_Period", "ControlModifier", "CTRL+period"),
    ("Key_BracketLeft", "MetaModifier", "SUPER+bracketleft"),
    ("Key_Minus", "ControlModifier", "CTRL+minus"),
    ("Key_Plus", "ControlModifier", "CTRL+plus"),
    ("Key_Slash", "ControlModifier", "CTRL+slash"),
    # And a punctuation key on its own is still a key somebody types with.
    ("Key_Comma", None, None),
    ("Key_Slash", None, None),
])
def test_only_a_combination_that_can_be_a_global_shortcut_is_handed_over(
        qapp, key, modifiers, trigger):
    from PySide6.QtCore import Qt

    flags = Qt.KeyboardModifier.NoModifier
    for word in (modifiers or "").split("|"):
        if word:
            flags |= getattr(Qt.KeyboardModifier, word)
    store = FakeStore()
    cfg, dlg = _hotkeys_dialog(qapp, "desktop-store", triggers=lambda: {"dictate": "CTRL+space"},
                               store=store)
    dlg.change_buttons["dictate"].click()
    _press(qapp, dlg, getattr(Qt.Key, key), flags)
    if trigger is None:
        assert store.writes == [], f"{key} must not become a global shortcut"
        assert dlg.hotkey_status.text() == NEEDS_A_COMBINATION
        # Still waiting: a refusal that let go of the keyboard would leave the
        # user having pressed a key and been given nothing to press instead.
        assert dlg.change_buttons["dictate"].text() == CHANGE_BUSY[DESKTOP_STORE]
        assert dlg.key_labels["dictate"].text() == "Ctrl+Space"
    else:
        assert store.writes == [{"dictate": trigger, "cancel": "", "language_toggle": "",
                                 "recall": ""}]
        assert dlg.change_buttons["dictate"].text() == CHANGE


def test_a_refused_key_leaves_the_next_one_still_being_listened_for(qapp):
    """The grab is application-wide, so giving up on a refusal would strand the
    user: they pressed a key, nothing happened, and nothing is waiting either."""
    from PySide6.QtCore import Qt

    store = FakeStore()
    cfg, dlg = _hotkeys_dialog(qapp, "desktop-store", triggers=lambda: {"dictate": "CTRL+space"},
                               store=store)
    dlg.change_buttons["dictate"].click()
    _press(qapp, dlg, Qt.Key.Key_A)                              # refused
    # "Volume Up" is Qt's own spelling and no keysym at all, so it stays refused.
    _press(qapp, dlg, Qt.Key.Key_VolumeUp, Qt.KeyboardModifier.ControlModifier)
    assert dlg.hotkey_status.text() == CHANGE_UNUSABLE           # and refused again
    assert store.writes == []
    _press(qapp, dlg, Qt.Key.Key_D, Qt.KeyboardModifier.ControlModifier)
    assert store.writes == [{"dictate": "CTRL+D", "cancel": "", "language_toggle": "",
                             "recall": ""}]
    assert dlg.change_buttons["dictate"].text() == CHANGE


def test_escape_still_ends_a_change_that_has_refused_a_key(qapp):
    """Whatever else the grab does, Escape has to get the user out of it."""
    from PySide6.QtCore import Qt

    store = FakeStore()
    cfg, dlg = _hotkeys_dialog(qapp, "desktop-store", triggers=lambda: {"dictate": "CTRL+space"},
                               store=store)
    dlg.change_buttons["dictate"].click()
    _press(qapp, dlg, Qt.Key.Key_A)
    _press(qapp, dlg, Qt.Key.Key_Escape)
    assert dlg.hotkey_status.text() == CHANGE_STOPPED
    assert dlg.change_buttons["dictate"].text() == CHANGE
    _press(qapp, dlg, Qt.Key.Key_D, Qt.KeyboardModifier.ControlModifier)
    assert store.writes == []                    # no longer listening


def test_a_desktop_that_refuses_the_key_leaves_the_row_on_the_truth(qapp):
    """The row is what the desktop holds; a write that failed did not move it."""
    from PySide6.QtCore import Qt

    store = FakeStore(fail="that shortcut is not stored yet")
    cfg, dlg = _hotkeys_dialog(qapp, "desktop-store", triggers=lambda: {"dictate": "CTRL+space"},
                               store=store)
    dlg.change_buttons["dictate"].click()
    _press(qapp, dlg, Qt.Key.Key_D, Qt.KeyboardModifier.ControlModifier)
    assert "not stored yet" in dlg.error_label.text()
    assert dlg.key_labels["dictate"].text() == "Ctrl+Space"


def test_change_opens_the_desktops_own_window_where_it_insists(qapp, monkeypatch):
    monkeypatch.setattr("voice.ui.settings._spawn", lambda cmd: pytest.fail("nothing to start"))
    cfg, dlg = _hotkeys_dialog(qapp, "desktop-dialog", triggers=lambda: {"dictate": "CTRL+space"},
                               capture=lambda cb: cb(DIALOG_MESSAGE))
    dlg.change_buttons["language_toggle"].click()
    qapp.processEvents()
    assert dlg.hotkey_status.text() == DESKTOP_DIALOG_OPEN
    assert dlg.change_buttons["language_toggle"].text() == CHANGE


def test_no_row_repeats_its_key_under_the_field(qapp):
    """The dim "desktop: Press <Control>space" under every field is gone: the
    key is on the row once, and nowhere else."""
    from PySide6.QtWidgets import QLabel, QTabWidget

    cfg, dlg = _hotkeys_dialog(qapp, "desktop-store", triggers=lambda: {"dictate": "CTRL+space"},
                               store=FakeStore())
    dlg.show()
    dlg.findChildren(QTabWidget)[0].setCurrentIndex(1)
    qapp.processEvents()
    on_screen = [w.text() for w in dlg.findChildren(QLabel)
                 if w.text().strip() and w.isVisible()]
    assert on_screen.count("Ctrl+Space") == 1
    assert not [text for text in on_screen if text.lower().startswith("desktop:")]
    dlg.close()


def test_the_advanced_section_is_collapsed_and_still_round_trips(qapp):
    cfg, dlg = _hotkeys_dialog(qapp, "desktop-store", triggers=lambda: {"dictate": "CTRL+space"},
                               store=FakeStore())
    assert dlg.advanced_button.isChecked() is False
    assert dlg.advanced_box.isHidden() is True          # collapsed until it is asked for
    dlg.advanced_button.setChecked(True)
    assert dlg.advanced_box.isHidden() is False
    dlg.portal_edits["recall"].setText("CTRL+SHIFT+r")
    dlg.hotkey_edit.setText("KEY_F14")
    dlg.save_button.click()
    again = Config.load()
    assert again.get("hotkeys.portal_recall") == "CTRL+SHIFT+r"
    assert again.get("hotkeys.dictate") == "KEY_F14"
    dlg.reload_from_disk()
    assert dlg.portal_edits["recall"].text() == "CTRL+SHIFT+r"
    assert dlg.hotkey_edit.text() == "KEY_F14"


def test_all_four_keyboard_keys_reach_the_config(qapp):
    """The language toggle had no field at all, so the one key the daemon binds
    that nobody could see stayed unset for ever."""
    cfg, dlg, _ = make(qapp)
    dlg.language_toggle_edit.setText("KEY_F15")
    dlg.recall_edit.setText("KEY_F16")
    dlg.save_button.click()
    again = Config.load()
    assert again.get("hotkeys.language_toggle") == "KEY_F15"
    assert again.get("hotkeys.recall") == "KEY_F16"


def test_a_key_that_will_not_parse_says_which_row_to_fix(qapp):
    cfg, dlg, _ = make(qapp)
    dlg.cancel_edit.setText("KEY_BANANA")
    dlg.save_button.click()
    assert dlg.error_label.text().startswith(ACTION_LABELS["cancel"])
    assert "hotkeys." not in dlg.error_label.text()


# -- nothing on screen is written for whoever wrote it ---------------------------
INTERNAL = ("evdev", "portal", "dconf", "backend", "hotkeys.", "ui.", "inject.", "stt.")


@pytest.mark.parametrize("route", ["capture", "desktop-store", "desktop-dialog"])
def test_nothing_this_window_says_names_anything_internal(qapp, route):
    """The owner: "You need to think as a normal user, not a tech developer!"

    Walked over the whole window rather than asserted per label, so the net
    catches the next tab as well as these five.
    """
    store = FakeStore() if route == "desktop-store" else None
    cfg, dlg = _hotkeys_dialog(qapp, route, triggers=lambda: {"dictate": "CTRL+space"},
                               store=store)
    dlg.set_layer_shell(False)                      # the warning that can appear
    said = [(widget, text) for widget, text in _visible_words(dlg)
            if any(word in text.lower() for word in INTERNAL)]
    assert said == [], [f"{text!r} on {type(widget).__name__}" for widget, text in said]


def test_the_help_behind_the_question_marks_is_written_for_a_person(qapp):
    cfg, dlg = _hotkeys_dialog(qapp, "desktop-store", store=FakeStore())
    texts = [button.help_text for button in dlg.help_buttons.values()]
    assert texts and all(button.toolTip() for button in dlg.help_buttons.values())
    said = [text for text in texts if any(word in text.lower() for word in INTERNAL)]
    assert said == []
    assert "/org/" not in " ".join(texts) and "gtk4" not in " ".join(texts).lower()


# -- no text is cut off, at any size the window can take -------------------------
def _too_small_for_its_text(dlg):
    """Every label that cannot show all of its text where it has been put.

    Two ways for that to happen, and the window has had both: a wrapping label
    given one row's height by a form layout, and a label whose box is wider than
    what is left of the window it sits in.
    """
    from PySide6.QtCore import QPoint, QRect
    from PySide6.QtWidgets import QLabel

    bad = []
    for label in dlg.findChildren(QLabel):
        if not label.text().strip() or not label.isVisible() or label.width() <= 0:
            continue
        needed = (label.heightForWidth(label.width()) if label.wordWrap()
                  else label.sizeHint().height())
        if needed > label.height() + 1:
            bad.append(f"{label.text()[:50]!r} needs {needed}px, has {label.height()}px")
            continue
        if not label.wordWrap() and label.sizeHint().width() > label.width() + 1:
            bad.append(f"{label.text()[:50]!r} needs {label.sizeHint().width()}px across, "
                       f"has {label.width()}px")
            continue
        box = QRect(label.mapTo(dlg, QPoint(0, 0)), label.size())
        ancestor = label.parentWidget()
        while ancestor is not None and ancestor is not dlg:
            frame = QRect(ancestor.mapTo(dlg, QPoint(0, 0)), ancestor.size())
            if not frame.contains(box):
                bad.append(f"{label.text()[:50]!r} is cut off by its "
                           f"{type(ancestor).__name__}")
                break
            ancestor = ancestor.parentWidget()
    return bad


@pytest.mark.parametrize("route", ["capture", "desktop-store", "desktop-dialog"])
@pytest.mark.parametrize("size", ["default", "minimum"])
def test_no_text_is_cut_off_on_any_tab_at_any_size(qapp, route, size):
    """A grey paragraph clipped mid-sentence is what the owner photographed, and
    the longest thing this window can say is the one that did it."""
    from PySide6.QtWidgets import QTabWidget

    store = FakeStore() if route == "desktop-store" else None
    cfg, dlg = _hotkeys_dialog(qapp, route, triggers=lambda: {"dictate": "CTRL+space"},
                               store=store)
    dlg.set_layer_shell(False)
    # The longest line any of these ever hold, so the measurement is of the
    # worst case rather than of an empty label.
    dlg.hotkey_status.setText(
        "Saved to your desktop: start and stop dictation now uses Ctrl+Alt+D, and the other "
        "three are unchanged - a sentence about as long as this one ever gets.")
    dlg.error_label.setText("Something went wrong while writing this, and the reason is a "
                            "long one that has to be read to the end.")
    dlg.advanced_button.setChecked(True)
    dlg.show()
    qapp.processEvents()
    dlg.resize(dlg.sizeHint() if size == "default" else dlg.minimumSizeHint())
    qapp.processEvents()
    tabs = dlg.findChildren(QTabWidget)[0]
    cut = []
    for index in range(tabs.count()):
        tabs.setCurrentIndex(index)
        qapp.processEvents()
        cut += [f"{tabs.tabText(index)}: {line}" for line in _too_small_for_its_text(dlg)]
    dlg.close()
    assert cut == [], "\n".join(cut)


def test_a_shortcut_the_desktop_has_never_heard_of_says_what_to_do(qapp):
    """The store skips an id nobody registered, in silence: on the owner's own
    machine three of the four are exactly that, so "Change…" would have looked
    broken on three rows out of four."""
    from PySide6.QtCore import Qt

    store = FakeStore()
    cfg, dlg = _hotkeys_dialog(qapp, "desktop-store", triggers=lambda: {"dictate": "CTRL+space"},
                               store=store)
    dlg.change_buttons["language_toggle"].click()
    _press(qapp, dlg, Qt.Key.Key_L,
           Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert store.writes == []                      # there was nothing to change
    assert dlg.hotkey_status.text() == NOT_OFFERED
    assert dlg.portal_edits["language_toggle"].text() == "CTRL+SHIFT+L"
    dlg.save_button.click()                        # which is what asking for it is
    assert Config.load().get("hotkeys.portal_language_toggle") == "CTRL+SHIFT+L"


def test_a_key_that_arrives_after_the_change_was_given_up_is_dropped(qapp):
    """The listener answers whenever the user presses something, which can be
    long after the button was pressed again to stop waiting."""
    captures = []
    cfg, dlg = _hotkeys_dialog(qapp, "capture", capture=captures.append)
    dlg.change_buttons["cancel"].click()
    dlg.change_buttons["cancel"].click()             # stopped waiting
    assert dlg.hotkey_status.text() == CHANGE_STOPPED
    captures[0]("KEY_F14")                           # and only then a key
    qapp.processEvents()
    assert dlg.cancel_edit.text() == "KEY_ESC"       # untouched
    assert dlg.key_labels["cancel"].text() == "Escape"


def test_a_desktop_store_that_vanished_still_says_something(qapp):
    """A button that does nothing and says nothing is the whole complaint."""
    from PySide6.QtCore import Qt

    from voice.ui.settings import CHANGE_REFUSED

    stores = [FakeStore(), None]                 # gone by the time it is written
    cfg = Config.load()
    dlg = SettingsDialog(cfg, capture_key=lambda cb: None, sources=lambda: [],
                         backend="portal", triggers=lambda: {"dictate": "CTRL+space"},
                         shortcut_store=lambda: stores.pop(0))
    dlg.change_buttons["dictate"].click()
    _press(qapp, dlg, Qt.Key.Key_D, Qt.KeyboardModifier.ControlModifier)
    assert dlg.hotkey_status.text() == CHANGE_REFUSED
    assert dlg.key_labels["dictate"].text() == "Ctrl+Space"


def test_the_desktops_own_spelling_of_a_shortcut_reads_like_the_others():
    """The portal answers "Press <Control>space"; the row must not show that.

    Both spellings reach the same label, so both have to come out as a person
    writes the key, or the tab reads half in the desktop's language.
    """
    from voice.ui.settings import pretty_trigger

    assert pretty_trigger("Press <Control>space") == "Ctrl+Space"
    assert pretty_trigger("<Shift><Control>c") == "Shift+Ctrl+C"
    assert pretty_trigger("CTRL+space") == "Ctrl+Space"     # our own spelling, unchanged
    assert pretty_trigger("F13") == "F13"
    assert pretty_trigger("") == ""


def test_advanced_sits_under_the_shortcuts_it_expands(qapp, isolated_xdg):
    """Pinned to the bottom of the tab it reads as belonging to nothing."""
    from PySide6.QtWidgets import QGroupBox, QTabWidget

    dlg = SettingsDialog(Config.load(), capture_key=lambda cb: None, sources=lambda: [])
    dlg.resize(660, 520)
    tabs = dlg.findChild(QTabWidget)
    tabs.setCurrentIndex(1)                       # Hotkeys
    qapp.processEvents()
    group = next(g for g in dlg.findChildren(QGroupBox) if g.title() == "Shortcuts")
    gap = (dlg.advanced_button.mapTo(dlg, dlg.advanced_button.rect().topLeft()).y()
           - group.mapTo(dlg, group.rect().bottomLeft()).y())
    assert 0 <= gap < 100, f"Advanced sits {gap}px below the group it expands"


def test_words_to_listen_for_roundtrip(qapp):
    """The vocabulary is a list in the file and a comma-separated line in the
    window; blanks a user leaves between commas are not words."""
    cfg, dlg, _ = make(qapp)
    assert dlg.hotwords_edit.text() == ""
    dlg.hotwords_edit.setText(" Hollyland Lark, Keychron , , ")
    dlg.save_button.click()
    assert Config.load().get("dictionary.hotwords") == ["Hollyland Lark", "Keychron"]
    _, reopened, _ = make(qapp)
    assert reopened.hotwords_edit.text() == "Hollyland Lark, Keychron"
