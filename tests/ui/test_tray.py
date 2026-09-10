from voice.ui.icons import icon_for, pixmap_for
from voice.ui.tray import Tray


def test_icons_exist_and_differ_per_state(qapp):
    imgs = {s: pixmap_for(s, 32).toImage() for s in ("idle", "recording", "transcribing", "error")}
    assert all(not i.isNull() for i in imgs.values())
    assert imgs["idle"] != imgs["recording"] != imgs["transcribing"]
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
