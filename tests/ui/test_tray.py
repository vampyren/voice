import itertools

from voice.ui.icons import icon_for, pixmap_for
from voice.ui.tray import Tray


def test_icons_exist_and_differ_per_state(qapp):
    imgs = {s: pixmap_for(s, 32).toImage() for s in ("idle", "recording", "transcribing", "error")}
    assert all(not i.isNull() for i in imgs.values())
    for a, b in itertools.combinations(imgs, 2):
        assert imgs[a] != imgs[b], f"{a} and {b} icons are identical"
    assert not icon_for("idle").isNull()


def test_tray_state_updates_tooltip_and_actions_flow(qapp):
    got = []
    tray = Tray(on_action=got.append)
    tray.set_state("recording", "listening")
    assert "recording" in tray.icon.toolTip() and "listening" in tray.icon.toolTip()
    tray.set_profiles(["local", "openai"], "openai")
    names = [a.text() for a in tray.profile_menu.actions()]
    assert names == ["local", "openai"]
    assert [a.isChecked() for a in tray.profile_menu.actions()] == [False, True]
    tray.profile_menu.actions()[0].trigger()
    tray.action("recall").trigger()
    tray.action("quit").trigger()
    assert got == ["profile:local", "recall", "quit"]
    tray.state_changed.emit("error", "boom")
    qapp.processEvents()
    assert "error" in tray.icon.toolTip()


def test_tray_language_submenu_is_a_radio_list_of_codes(qapp):
    got = []
    tray = Tray(on_action=got.append)
    tray.set_languages(["en", "sv", "auto"], "sv")
    labels = [a.text() for a in tray.language_menu.actions()]
    assert labels == ["EN", "SV", "AUTO"]
    assert [a.isChecked() for a in tray.language_menu.actions()] == [False, True, False]
    tray.language_menu.actions()[0].trigger()
    assert got == ["language:en"]

    tray.set_languages(["en", "sv"], "en")            # rebuilt, not appended to
    assert [a.text() for a in tray.language_menu.actions()] == ["EN", "SV"]
    assert [a.isChecked() for a in tray.language_menu.actions()] == [True, False]
