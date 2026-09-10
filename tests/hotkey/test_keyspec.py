import pytest
from evdev import ecodes as e

from voice.hotkey.keyspec import KeySpec, Tracker, keyspec_name, parse_keyspec


def test_parse_single_and_combo():
    assert parse_keyspec("KEY_F13") == KeySpec(frozenset({e.KEY_F13}), "KEY_F13")
    combo = parse_keyspec("KEY_LEFTMETA+KEY_SPACE")
    assert combo.codes == frozenset({e.KEY_LEFTMETA, e.KEY_SPACE})


def test_parse_is_case_and_space_tolerant():
    assert parse_keyspec(" key_leftctrl + key_v ").codes == frozenset({e.KEY_LEFTCTRL, e.KEY_V})


def test_parse_rejects_unknown_key():
    with pytest.raises(ValueError, match="KEY_BANANA"):
        parse_keyspec("KEY_BANANA")


def test_parse_empty_is_inert():
    assert parse_keyspec("").codes == frozenset()


def test_keyspec_name_round_trips():
    assert keyspec_name(e.KEY_RIGHTCTRL) == "KEY_RIGHTCTRL"


def test_tracker_single_key_press_release():
    t = Tracker({"dictate": parse_keyspec("KEY_F13")})
    assert t.feed(e.KEY_F13, 1) == [("dictate", "press")]
    assert t.feed(e.KEY_F13, 2) == []              # autorepeat ignored
    assert t.feed(e.KEY_F13, 0) == [("dictate", "release")]


def test_tracker_combo_requires_all_keys_and_releases_on_any():
    t = Tracker({"dictate": parse_keyspec("KEY_LEFTMETA+KEY_SPACE")})
    assert t.feed(e.KEY_LEFTMETA, 1) == []
    assert t.feed(e.KEY_SPACE, 1) == [("dictate", "press")]
    assert t.feed(e.KEY_LEFTMETA, 0) == [("dictate", "release")]
    assert t.feed(e.KEY_SPACE, 0) == []            # already released


def test_tracker_held_and_modifiers():
    t = Tracker({})
    t.feed(e.KEY_LEFTSHIFT, 1)
    assert t.modifiers_held()
    assert e.KEY_LEFTSHIFT in t.held()
    t.feed(e.KEY_LEFTSHIFT, 0)
    assert not t.modifiers_held()


def test_tracker_empty_spec_never_fires():
    t = Tracker({"recall": parse_keyspec("")})
    assert t.feed(e.KEY_A, 1) == []


def test_tracker_set_specs_replaces_bindings():
    t = Tracker({"dictate": parse_keyspec("KEY_F13")})
    t.set_specs({"dictate": parse_keyspec("KEY_F14")})
    assert t.feed(e.KEY_F13, 1) == []
    assert t.feed(e.KEY_F14, 1) == [("dictate", "press")]
