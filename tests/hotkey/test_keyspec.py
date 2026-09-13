import threading

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


def test_set_specs_preserves_an_in_flight_press_of_an_unchanged_binding():
    # A config reload while dictate is held must not swallow the release, or
    # hold-to-talk records until max_seconds.
    t = Tracker({"dictate": parse_keyspec("KEY_F13"), "cancel": parse_keyspec("KEY_ESC")})
    assert t.feed(e.KEY_F13, 1) == [("dictate", "press")]
    t.set_specs({"dictate": parse_keyspec("KEY_F13"), "cancel": parse_keyspec("KEY_F15")})
    assert t.feed(e.KEY_F13, 0) == [("dictate", "release")]


def test_set_specs_drops_the_press_when_the_binding_changed():
    t = Tracker({"dictate": parse_keyspec("KEY_F13")})
    assert t.feed(e.KEY_F13, 1) == [("dictate", "press")]
    t.set_specs({"dictate": parse_keyspec("KEY_F14")})
    assert t.feed(e.KEY_F13, 0) == []          # the old key is no longer bound
    assert t.feed(e.KEY_F14, 1) == [("dictate", "press")]


def test_set_specs_removing_a_held_binding_does_not_raise():
    t = Tracker({"dictate": parse_keyspec("KEY_F13")})
    t.feed(e.KEY_F13, 1)
    t.set_specs({})                            # binding removed entirely mid-press
    assert t.feed(e.KEY_F13, 0) == []


class _BlockingSpecs(dict):
    """A specs mapping whose iteration blocks, holding set_specs mid-update."""

    def __init__(self, mapping, entered, release):
        super().__init__(mapping)
        self._entered, self._release = entered, release

    def items(self):
        self._entered.set()
        self._release.wait(5)
        return super().items()


def test_feed_and_set_specs_are_mutually_exclusive():
    # feed() runs on the evdev thread while set_specs() runs on the Qt thread:
    # they must not interleave inside the tracker's state.
    entered, release, fed = threading.Event(), threading.Event(), threading.Event()
    t = Tracker({"dictate": parse_keyspec("KEY_F13")})

    reloader = threading.Thread(
        target=lambda: t.set_specs(_BlockingSpecs({"dictate": parse_keyspec("KEY_F14")}, entered, release)),
        daemon=True)
    reloader.start()
    assert entered.wait(2)

    events = []
    feeder = threading.Thread(target=lambda: (events.extend(t.feed(e.KEY_F13, 1)), fed.set()), daemon=True)
    feeder.start()
    assert not fed.wait(0.3)                   # blocked behind the in-progress set_specs

    release.set()
    assert fed.wait(2)
    reloader.join(timeout=2)
    feeder.join(timeout=2)
    assert events == []                        # F13 is no longer bound once the reload landed


def test_a_code_with_several_names_gives_one_name_as_a_string():
    """evdev maps ten codes to more than one name, and hands them back as a
    *tuple* on the installed version - the check was for a list.

    The tuple went straight through into a Qt Signal(str):
        _pythonToCppCopy: Cannot copy-convert 0x... (tuple) to C++.
        the listener could not capture a key:
    so those keys could not be assigned, and the message said nothing.
    """
    from evdev import ecodes

    from voice.hotkey.keyspec import keyspec_name

    aliased = [code for code, names in ecodes.KEY.items() if not isinstance(names, str)]
    assert aliased, "this evdev has no aliased codes; the guard still has to hold"
    for code in aliased:
        name = keyspec_name(code)
        assert isinstance(name, str), f"{code} gave {name!r}"
        assert name.startswith(("KEY_", "BTN_")), name
        # And the name it picks has to be one the config can parse back.
        from voice.hotkey.keyspec import parse_keyspec

        assert parse_keyspec(name).codes == {code}


def test_an_unknown_code_still_gives_a_string():
    from voice.hotkey.keyspec import keyspec_name

    assert keyspec_name(999999) == "KEY_999999"
