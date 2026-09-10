from voice.audio.capture import Source
from voice.config import Config
from voice.ui.settings import PROFILE_TEMPLATES, SettingsDialog


def make(qapp):
    cfg = Config.load()
    captures = []
    dlg = SettingsDialog(cfg, capture_key=captures.append,
                         sources=lambda: [Source("alsa_input.obsbot", "OBSBOT Tiny 3", True)])
    return cfg, dlg, captures


def test_loads_values_from_config(qapp):
    cfg, dlg, _ = make(qapp)
    assert dlg.hotkey_edit.text() == "KEY_F13"
    assert dlg.mode_combo.currentText() == "hold"
    assert dlg.language_combo.currentData() == "en"
    assert dlg.device_combo.itemText(1) == "OBSBOT Tiny 3 (default)"
    assert [dlg.profile_list.item(i).text() for i in range(dlg.profile_list.count())] == ["local", "openai", "groq", "openrouter"]


def test_edit_and_save_writes_config_and_emits(qapp):
    cfg, dlg, _ = make(qapp)
    fired = []
    dlg.saved.connect(lambda: fired.append(True))
    dlg.hotkey_edit.setText("KEY_RIGHTCTRL")
    dlg.mode_combo.setCurrentText("toggle")
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
    assert "beam_size" in dlg.error_label.text()
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
    dlg.mode_combo.setCurrentText("toggle")
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
                         backend="portal")
    assert "desktop" in dlg.hotkey_hint.text().lower()
    plain = SettingsDialog(cfg, capture_key=lambda cb: None, sources=lambda: [])
    assert "evdev" in plain.hotkey_hint.text().lower()


def test_a_captured_message_is_shown_instead_of_being_typed_into_the_field(qapp):
    """The portal listener answers capture_next with a sentence, not a key name."""
    cfg, dlg, captures = make(qapp)
    dlg.capture_button.click()
    captures[0]("portal: change the shortcut in your desktop's settings")
    qapp.processEvents()
    assert dlg.hotkey_edit.text() == "KEY_F13"               # untouched
    assert "portal" in dlg.error_label.text()
    assert dlg.capture_button.text() == "Capture key"


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
def portal_dialog(qapp):
    cfg = Config.load()
    dlg = SettingsDialog(cfg, capture_key=lambda cb: None,
                         sources=lambda: [Source("alsa_input.obsbot", "OBSBOT Tiny 3", True)],
                         backend="portal")
    return cfg, dlg


def test_the_portal_backend_edits_its_triggers_instead_of_capturing_keys(qapp):
    """The compositor keeps the keystroke, so "Capture key" can only ever answer
    with a sentence - which landed in the red error label and read as a failure."""
    cfg, dlg = portal_dialog(qapp)
    assert set(dlg.portal_edits) == {"dictate", "recall", "cancel", "language_toggle"}
    assert dlg.portal_edits["dictate"].text() == "CTRL+space"
    assert dlg.capture_button.isHidden() is True
    assert dlg.error_label.text() == ""                 # the explanation is a hint,
    assert "desktop" in dlg.hotkey_hint.text()          # not an error


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
    assert "portal_dictate" in dlg.error_label.text()
    assert Config.load().get("hotkeys.portal_dictate") == "CTRL+space"


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
    assert [table.item(r, 0).text() for r in range(table.rowCount())] == ["en", "sv"]
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
