"""Where the pill sits: the nine placements, and the anchors they become."""
import pytest

from voice.ui.placement import (
    DEFAULT_MARGIN_X,
    DEFAULT_MARGIN_Y,
    DEFAULT_POSITION,
    LEGACY_POSITIONS,
    MARGIN_LIMIT,
    POSITIONS,
    anchors,
    clamp_margin,
    normalise_position,
    pill_origin,
    placement_at,
    placement_note,
    placement_summary,
)


def test_there_are_exactly_nine_placements():
    assert len(POSITIONS) == 9
    assert set(POSITIONS) == {f"{v}-{h}" for v in ("top", "middle", "bottom")
                              for h in ("left", "center", "right")}
    assert DEFAULT_POSITION == "bottom-center"


@pytest.mark.parametrize("position,expected", [
    ("top-left", {"top": 48, "left": 12}),
    ("top-center", {"top": 48}),
    ("top-right", {"top": 48, "right": 12}),
    ("middle-left", {"left": 12}),
    ("middle-center", {}),
    ("middle-right", {"right": 12}),
    ("bottom-left", {"bottom": 48, "left": 12}),
    ("bottom-center", {"bottom": 48}),
    ("bottom-right", {"bottom": 48, "right": 12}),
])
def test_each_placement_anchors_to_its_own_edges_with_its_own_margins(position, expected):
    """An edge that is not anchored centres the pill on that axis, and has no
    margin: there is nothing to be a distance from."""
    assert anchors(position, margin_x=12, margin_y=48) == expected


def test_margins_default_to_none_at_all():
    assert anchors("bottom-right") == {"bottom": 0, "right": 0}


def test_a_negative_margin_pushes_the_pill_past_its_edge():
    assert anchors("bottom-center", margin_y=-10) == {"bottom": -10}


@pytest.mark.parametrize("legacy,expected", [("bottom", "bottom-center"), ("top", "top-center")])
def test_the_two_old_values_still_name_a_placement(legacy, expected):
    assert LEGACY_POSITIONS[legacy] == expected
    assert normalise_position(legacy) == expected


@pytest.mark.parametrize("position", POSITIONS)
def test_a_full_placement_is_returned_unchanged(position):
    assert normalise_position(position) == position
    assert normalise_position(f"  {position.upper()} ") == position


@pytest.mark.parametrize("junk", ["sideways", "", None, 3, "bottom-middle", "center-bottom", True])
def test_nonsense_falls_back_to_the_default_rather_than_hiding_the_pill(junk):
    assert normalise_position(junk) == DEFAULT_POSITION


def test_a_placement_the_module_does_not_know_has_no_anchors_of_its_own():
    """anchors() is fed normalised values; a stray one must not raise on the
    helper's start-up path."""
    assert anchors("sideways", margin_x=1, margin_y=2) == {"bottom": 2}


@pytest.mark.parametrize("value,expected", [
    (0, 0), (48, 48), (-10, -10), (MARGIN_LIMIT, MARGIN_LIMIT), (-MARGIN_LIMIT, -MARGIN_LIMIT),
    (MARGIN_LIMIT + 1, MARGIN_LIMIT), (-MARGIN_LIMIT - 5, -MARGIN_LIMIT),
])
def test_a_margin_is_clamped_to_the_sane_range(value, expected):
    assert clamp_margin(value, DEFAULT_MARGIN_Y) == expected


@pytest.mark.parametrize("junk", [None, "48", 1.5, True, [48]])
def test_a_margin_that_is_not_a_whole_number_falls_back_to_the_default(junk):
    assert clamp_margin(junk, DEFAULT_MARGIN_Y) == DEFAULT_MARGIN_Y
    assert clamp_margin(junk, DEFAULT_MARGIN_X) == DEFAULT_MARGIN_X


def test_the_default_placement_needs_nothing_said_about_it():
    assert placement_note(DEFAULT_POSITION, DEFAULT_MARGIN_X, DEFAULT_MARGIN_Y) is None


@pytest.mark.parametrize("position,mx,my", [
    ("top-right", 0, 48), ("bottom-center", 0, 120), ("bottom-center", 16, 48)])
def test_a_placement_that_was_asked_for_is_named_when_it_cannot_be_applied(position, mx, my):
    note = placement_note(position, mx, my)
    assert note is not None
    assert position in note and "ui.overlay_position" in note
    assert f"{mx}" in note and f"{my}" in note


# -- the geometry the settings preview drags the pill around in -----------

SCREEN = (1920, 1080)
PILL = (280, 44)


@pytest.mark.parametrize("position,margin_x,margin_y,expected", [
    ("top-left", 0, 0, (0, 0)),
    ("top-center", 0, 48, (820, 48)),
    ("top-right", 16, 16, (1624, 16)),
    ("middle-left", 24, 0, (24, 518)),
    ("middle-center", 0, 0, (820, 518)),
    ("middle-right", 24, 0, (1616, 518)),
    ("bottom-left", 0, 48, (0, 988)),
    ("bottom-center", 0, 48, (820, 988)),
    ("bottom-right", 12, 12, (1628, 1024)),
])
def test_a_placement_becomes_the_pills_top_left_corner(position, margin_x, margin_y, expected):
    assert pill_origin(position, margin_x, margin_y, SCREEN, PILL) == expected


def test_a_centred_half_ignores_its_margin_on_screen_too():
    assert pill_origin("middle-center", 500, 500, SCREEN, PILL) == (820, 518)


@pytest.mark.parametrize("point,expected", [
    ((0, 0), ("top-left", 0, 0)),
    ((1640, 0), ("top-right", 0, 0)),
    ((0, 1036), ("bottom-left", 0, 0)),
    ((1640, 1036), ("bottom-right", 0, 0)),
    ((820, 518), ("middle-center", 0, 0)),
    ((820, 988), ("bottom-center", 0, 48)),
    ((60, 30), ("top-left", 60, 30)),
    ((1600, 1000), ("bottom-right", 40, 36)),
])
def test_a_dropped_pill_becomes_the_anchor_it_is_nearest_and_the_gap_it_left(point, expected):
    assert placement_at(*point, SCREEN, PILL) == expected


@pytest.mark.parametrize("position,margin_x,margin_y", [
    ("top-left", 60, 30), ("top-right", 12, 0), ("bottom-left", 0, 48),
    ("bottom-right", 100, 100), ("top-center", 0, 90), ("middle-left", 70, 0),
])
def test_dropping_a_pill_where_a_placement_put_it_gives_that_placement_back(
        position, margin_x, margin_y):
    """The preview shows a placement and reads one back; the two must agree, or
    opening the settings window and pressing Save would move the pill."""
    origin = pill_origin(position, margin_x, margin_y, SCREEN, PILL)
    assert placement_at(*origin, SCREEN, PILL) == (position, margin_x, margin_y)


def test_a_drop_within_the_tolerance_snaps_to_the_anchor():
    assert placement_at(9, 1030, SCREEN, PILL, snap=24) == ("bottom-left", 0, 0)
    assert placement_at(812, 510, SCREEN, PILL, snap=24) == ("middle-center", 0, 0)


def test_a_drop_outside_the_tolerance_keeps_the_gap_it_was_dropped_with():
    assert placement_at(90, 900, SCREEN, PILL, snap=24) == ("bottom-left", 90, 136)


def test_without_a_tolerance_nothing_is_snapped():
    """Arrow-key nudges ask for no snapping: a nudge that snapped back would
    look like a key that does nothing."""
    assert placement_at(1, 1035, SCREEN, PILL, snap=0) == ("bottom-left", 1, 1)


def test_a_pill_dropped_past_the_far_edge_is_still_a_sane_margin():
    """Nothing in the settings window can drop it there, but a hand-edited
    config can, and the arithmetic must not hand back a margin errors() rejects."""
    assert placement_at(-500, 5000, SCREEN, PILL) == ("bottom-left", -500, -MARGIN_LIMIT)


# -- saying it in words ---------------------------------------------------

@pytest.mark.parametrize("placement,expected", [
    (("top-right", 12, 60), "top-right - 12 px from the right, 60 px from the top"),
    (("bottom-center", 0, 48), "bottom-center - 48 px from the bottom"),
    (("middle-left", 24, 99), "middle-left - 24 px from the left"),
    (("middle-center", 5, 5), "middle-center - centred both ways"),
    (("bottom", 0, 48), "bottom-center - 48 px from the bottom"),
])
def test_a_placement_reads_back_as_a_sentence(placement, expected):
    """The settings window shows this beside the preview: the margin that does
    nothing on a centred half is not mentioned, because it does nothing."""
    assert placement_summary(*placement) == expected
