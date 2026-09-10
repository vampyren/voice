"""The settings window's drag-to-place preview.

Every test below drives a 320x180 preview of a 1600x900 screen, so one widget
pixel is exactly five screen pixels and the arithmetic in the assertions is
arithmetic, not a rounding guess.
"""
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

from voice.ui.pill_placer import PILL_SIZE, SNAP_PX, PillPlacer

SCREEN = (1600, 900)
WIDGET = (320, 180)          # the same aspect, so the screen fills the widget
SCALE = 5                    # screen pixels per widget pixel


@pytest.fixture
def placer(qapp):
    widget = PillPlacer(screen=SCREEN)
    widget.resize(*WIDGET)
    widget.show()
    QTest.qWaitForWindowExposed(widget)
    return widget


def widget_point(x, y):
    """A screen point, in the widget's coordinates."""
    return QPoint(round(x / SCALE), round(y / SCALE))


def test_it_shows_the_placement_it_is_given(placer):
    placer.set_placement("top-right", 24, 12)
    assert placer.placement() == ("top-right", 24, 12)


def test_showing_a_placement_is_not_a_user_choice(placer):
    """Loading the dialog must not look like the owner moved the pill."""
    moved = []
    placer.placement_changed.connect(lambda: moved.append(True))
    placer.set_placement("middle-center", 0, 0)
    assert moved == []


def test_a_screen_it_cannot_ask_about_is_a_sane_one(qapp):
    """Qt hands back an empty geometry on some headless setups; a zero-sized
    screen would divide by zero on the first paint."""
    widget = PillPlacer(screen=(0, 0))
    assert widget.screen_size() == (1920, 1080)
    assert widget.sizeHint().width() > widget.sizeHint().height()


def test_dragging_the_pill_into_a_corner_gives_that_corner(placer):
    placer.set_placement("bottom-center", 0, 48)
    # Press on the pill, then drag well past the top-left corner: the pill
    # stops at the edge, and a corner is an anchor.
    origin = placer.pill_origin()
    press = widget_point(origin[0] + PILL_SIZE[0] // 2, origin[1] + PILL_SIZE[1] // 2)
    QTest.mousePress(placer, Qt.MouseButton.LeftButton, pos=press)
    # Never QPoint(0, 0): QTest reads a null point as "the centre of the widget".
    QTest.mouseMove(placer, QPoint(2, 2))
    QTest.mouseRelease(placer, Qt.MouseButton.LeftButton, pos=QPoint(2, 2))
    assert placer.placement() == ("top-left", 0, 0)


def test_dragging_to_the_far_corner_gives_the_far_corner(placer):
    origin = placer.pill_origin()
    press = widget_point(origin[0] + PILL_SIZE[0] // 2, origin[1] + PILL_SIZE[1] // 2)
    QTest.mousePress(placer, Qt.MouseButton.LeftButton, pos=press)
    QTest.mouseRelease(placer, Qt.MouseButton.LeftButton, pos=QPoint(*WIDGET))
    assert placer.placement() == ("bottom-right", 0, 0)


def test_a_drop_short_of_an_anchor_is_that_anchor_and_the_gap(placer):
    """bottom-center at 0/48 puts the pill's corner at (660, 808) on a 1600x900
    screen. Pressing its centre - widget (160, 166), screen (800, 830) - grabs
    it 140/22 in from that corner, so releasing at widget (60, 40), screen
    (300, 200), leaves the corner at (160, 178): both well past the snapping
    tolerance, so both are kept as margins."""
    placer.set_placement("bottom-center", 0, 48)
    assert placer.pill_origin() == (660, 808)
    QTest.mousePress(placer, Qt.MouseButton.LeftButton, pos=QPoint(160, 166))
    QTest.mouseRelease(placer, Qt.MouseButton.LeftButton, pos=QPoint(60, 40))
    assert placer.placement() == ("top-left", 160, 178)


def test_a_drop_just_off_an_anchor_snaps_to_it(placer):
    """The same drag, released a couple of widget pixels from the corner: within
    the tolerance, so the margins are zero rather than "nearly zero"."""
    placer.set_placement("bottom-center", 0, 48)
    QTest.mousePress(placer, Qt.MouseButton.LeftButton, pos=QPoint(160, 166))
    QTest.mouseRelease(placer, Qt.MouseButton.LeftButton,
                       pos=QPoint(140 // SCALE + 2, 22 // SCALE + 2))
    assert placer.placement() == ("top-left", 0, 0)
    assert SNAP_PX * SCALE >= 10                      # the tolerance those 2px live in


def test_a_drag_tells_the_dialog_once_it_is_over(placer):
    moved = []
    placer.placement_changed.connect(lambda: moved.append(placer.placement()))
    QTest.mousePress(placer, Qt.MouseButton.LeftButton, pos=QPoint(160, 166))
    QTest.mouseMove(placer, QPoint(40, 40))
    QTest.mouseRelease(placer, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
    assert len(moved) == 1 and moved[0] == placer.placement()


@pytest.mark.parametrize("key,modifier,expected", [
    (Qt.Key.Key_Left, Qt.KeyboardModifier.NoModifier, ("bottom-right", 21, 40)),
    (Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier, ("bottom-right", 19, 40)),
    (Qt.Key.Key_Up, Qt.KeyboardModifier.NoModifier, ("bottom-right", 20, 41)),
    (Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier, ("bottom-right", 20, 39)),
    (Qt.Key.Key_Left, Qt.KeyboardModifier.ShiftModifier, ("bottom-right", 30, 40)),
    (Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier, ("bottom-right", 20, 30)),
])
def test_an_arrow_key_nudges_the_pill_by_a_pixel_and_shift_by_ten(placer, key, modifier, expected):
    placer.set_placement("bottom-right", 20, 40)
    QTest.keyClick(placer, key, modifier)
    assert placer.placement() == expected


def test_a_nudge_tells_the_dialog(placer):
    placer.set_placement("bottom-right", 20, 40)
    moved = []
    placer.placement_changed.connect(lambda: moved.append(True))
    QTest.keyClick(placer, Qt.Key.Key_Left)
    assert moved == [True]


def test_a_nudge_into_the_edge_stops_at_the_edge(placer):
    """Not a margin of -1, which is off screen and reads as a bug."""
    placer.set_placement("top-left", 0, 0)
    QTest.keyClick(placer, Qt.Key.Key_Left)
    QTest.keyClick(placer, Qt.Key.Key_Up)
    assert placer.placement() == ("top-left", 0, 0)


def test_a_key_that_is_not_a_nudge_is_left_alone(placer):
    placer.set_placement("bottom-right", 20, 40)
    QTest.keyClick(placer, Qt.Key.Key_A)
    assert placer.placement() == ("bottom-right", 20, 40)


def test_the_pill_is_drawn_where_the_placement_says(placer):
    """A DOM-free surface: the only way to know it draws is to draw it."""
    placer.set_placement("top-left", 0, 0)
    image = placer.grab().toImage()
    assert (image.width(), image.height()) >= WIDGET
    on_pill = image.pixelColor(6, 3)
    empty = image.pixelColor(WIDGET[0] - 6, WIDGET[1] - 6)
    assert on_pill != empty, "the pill is not drawn at the top-left corner"

    placer.set_placement("bottom-right", 0, 0)
    moved = placer.grab().toImage()
    assert moved.pixelColor(6, 3) == empty, "the pill did not leave the corner"
    assert moved.pixelColor(WIDGET[0] - 6, WIDGET[1] - 6) != empty, \
        "the pill is not drawn at the bottom-right corner"
