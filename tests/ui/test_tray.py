import itertools

import pytest
from PySide6.QtGui import QColor

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


#: The sizes a panel actually asks a tray icon for.
PANEL_SIZES = (16, 22, 24, 32, 48)
STATES = ("idle", "recording", "transcribing", "injecting", "error")


def content_box(image, alpha=8):
    """(x0, y0, x1, y1) of the drawn pixels, or None if nothing was drawn."""
    xs, ys = [], []
    for y in range(image.height()):
        for x in range(image.width()):
            if image.pixelColor(x, y).alpha() > alpha:
                xs.append(x)
                ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("size", PANEL_SIZES)
def test_the_tray_icon_fills_its_square(qapp, state, size):
    """The owner's "it's tiny compared to other icons in the menu".

    A panel scales the whole square to its icon slot, so margin inside the
    pixmap is margin the glyph never gets back. Stock icons leave a hair of
    padding, not a quarter of the square.
    """
    box = content_box(pixmap_for(state, size).toImage())
    assert box is not None, f"{state} drew nothing at {size} px"
    x0, y0, x1, y1 = box
    width, height = x1 - x0 + 1, y1 - y0 + 1
    assert width >= 0.8 * size, f"{state} at {size}px is {width}px wide, not {0.8 * size}"
    assert height >= 0.8 * size, f"{state} at {size}px is {height}px tall, not {0.8 * size}"


@pytest.mark.parametrize("size", PANEL_SIZES)
def test_no_state_is_drawn_smaller_than_any_other(qapp, size):
    """State is colour, never a ring, so every state fills the same square.

    A ring round the glyph cost it a quarter of its size, which is how the
    transcribing icon came to read as a dot beside its stock neighbours. If a
    state ever shrinks again, this fails.
    """
    boxes = {}
    for state in ("idle", "recording", "transcribing", "injecting", "error"):
        box = content_box(pixmap_for(state, size).toImage())
        assert box is not None, f"{state} drew nothing at {size}px"
        x0, y0, x1, y1 = box
        boxes[state] = (x1 - x0 + 1, y1 - y0 + 1)

    widths = {w for w, _ in boxes.values()}
    heights = {h for _, h in boxes.values()}
    assert max(widths) - min(widths) <= 1, f"widths differ by state: {boxes}"
    assert max(heights) - min(heights) <= 1, f"heights differ by state: {boxes}"


def _around(x, y, size, reach=2):
    for dx in range(-reach, reach + 1):
        for dy in range(-reach, reach + 1):
            if 0 <= x + dx < size and 0 <= y + dy < size:
                yield x + dx, y + dy


def test_the_microphone_is_still_a_microphone_at_sixteen_pixels(qapp):
    """Capsule, cradle, stem and base all have to survive the smallest size.

    Measured in rows: the capsule is a solid block near the top, the cradle
    is two separate arms lower down, and the base is a wide bar at the bottom.
    """
    image = pixmap_for("idle", 16).toImage()
    box = content_box(image)
    assert box is not None
    x0, y0, x1, y1 = box

    def runs(y):
        """How many separate horizontal runs of drawn pixels are in row `y`."""
        drawn = [image.pixelColor(x, y).alpha() > 8 for x in range(16)]
        return sum(1 for i, on in enumerate(drawn) if on and not (i and drawn[i - 1]))

    assert runs(y0 + 1) == 1, "the capsule is not a solid block near the top"
    assert runs((y0 + y1) // 2 + 2) == 2, "the cradle's two arms have merged"
    assert runs(y1) == 1, "the base is not one bar"
    assert sum(image.pixelColor(x, y1).alpha() > 8 for x in range(16)) >= 5, "the base is a stub"
