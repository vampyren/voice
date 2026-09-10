import pytest
from evdev import ecodes as e

from voice.inject.keys import parse_chord


def test_parse_chord_modifiers_then_key():
    assert parse_chord("ctrl+shift+v") == [e.KEY_LEFTCTRL, e.KEY_LEFTSHIFT, e.KEY_V]
    assert parse_chord("Ctrl + V") == [e.KEY_LEFTCTRL, e.KEY_V]
    assert parse_chord("shift+insert") == [e.KEY_LEFTSHIFT, e.KEY_INSERT]
    assert parse_chord("super+KEY_F13") == [e.KEY_LEFTMETA, e.KEY_F13]


def test_parse_chord_rejects_unknown():
    with pytest.raises(ValueError, match="hyper"):
        parse_chord("hyper+v")


def test_parse_chord_rejects_an_empty_chord():
    for text in ("", "   ", "+", " + + "):
        with pytest.raises(ValueError, match="empty chord"):
            parse_chord(text)
