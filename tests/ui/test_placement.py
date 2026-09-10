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
    placement_note,
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
