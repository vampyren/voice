"""The helper's stdin protocol, tested without GTK (it imports `gi` lazily)."""
import json

import pytest

from voice.ui.overlay import (
    REDUCED_MOTION_ENV,
    apply_message,
    build_parser,
    parse_line,
    reduced_motion,
)
from voice.ui.overlay_model import AMP, BAR_FLOOR, OverlayModel


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture
def model():
    return OverlayModel(clock=Clock())


def test_the_helper_module_does_not_need_gi():
    import voice.ui.overlay as overlay
    assert "gi" not in dir(overlay), "gi must stay inside main(), not at import time"


# -- parsing --------------------------------------------------------------

def test_a_json_object_per_line_is_parsed():
    assert parse_line('{"state": "recording"}\n') == {"state": "recording"}


@pytest.mark.parametrize("line", ["", "   \n", "not json", "[1, 2]", '"recording"', "null"])
def test_junk_lines_are_dropped_rather_than_raising(line):
    assert parse_line(line) is None


# -- applying -------------------------------------------------------------

def test_a_state_message_moves_the_model(model):
    assert apply_message(model, {"state": "recording"}) is True
    assert model.state == "recording" and model.visible is True


def test_a_level_message_feeds_the_waveform(model):
    apply_message(model, {"state": "recording"})
    for _ in range(30):
        apply_message(model, {"level": 1.0})
    assert model.bar_heights == pytest.approx(list(AMP))


def test_a_level_arriving_as_a_string_is_still_a_number(model):
    apply_message(model, {"state": "recording"})
    assert apply_message(model, {"level": "0.5"}) is True
    assert model.bar_heights[10] > AMP[10] * BAR_FLOOR


def test_an_unusable_level_is_ignored(model, caplog):
    apply_message(model, {"state": "recording"})
    with caplog.at_level("WARNING"):
        assert apply_message(model, {"level": "loud"}) is False
    assert "bad level" in caplog.text
    assert model.bar_heights == pytest.approx([a * BAR_FLOOR for a in AMP])


def test_an_unknown_state_is_logged_and_survived(model, caplog):
    with caplog.at_level("WARNING"):
        assert apply_message(model, {"state": "wobbling"}) is False
    assert "wobbling" in caplog.text
    assert model.state == "hidden"


def test_an_error_message_carries_its_text(model):
    apply_message(model, {"state": "error", "text": "pw-record: no such target"})
    assert model.state == "error"
    assert model.text == "pw-record: no such target"


def test_a_language_message_updates_the_badge(model):
    assert apply_message(model, {"language": "sv"}) is True
    assert model.badge_text == "SV"


def test_a_language_and_a_notice_can_arrive_together(model):
    apply_message(model, {"state": "recording"})
    apply_message(model, {"language": "sv", "state": "notice"})
    assert model.state == "notice"
    assert model.text == "EN → SV"                # the language is applied first
    assert model.badge_text == "SV"


def test_an_explicit_notice_text_wins(model):
    apply_message(model, {"state": "notice", "text": "EN → DE"})
    assert model.text == "EN → DE"


def test_hiding_is_just_another_message(model):
    apply_message(model, {"state": "recording"})
    apply_message(model, {"state": "hidden"})
    assert model.visible is False


def test_an_empty_message_changes_nothing(model):
    assert apply_message(model, {}) is False


# -- settings -------------------------------------------------------------

def test_reduced_motion_follows_gtk_by_default(monkeypatch):
    monkeypatch.delenv(REDUCED_MOTION_ENV, raising=False)
    assert reduced_motion(gtk_setting=True) is False
    assert reduced_motion(gtk_setting=False) is True
    assert reduced_motion(gtk_setting=None) is False        # unknown: keep motion


@pytest.mark.parametrize("value,expected", [("1", True), ("true", True), ("YES", True),
                                            ("0", False), ("no", False)])
def test_the_environment_overrides_gtk(monkeypatch, value, expected):
    monkeypatch.setenv(REDUCED_MOTION_ENV, value)
    assert reduced_motion(gtk_setting=not expected) is expected


def test_the_command_line_defaults_match_the_design():
    args = build_parser().parse_args([])
    assert (args.height, args.position, args.margin, args.lang) == (44, "bottom", 48, "en")
    assert build_parser().parse_args(["--lang", "sv", "--position", "top"]).position == "top"


# -- the whole protocol end to end ----------------------------------------

def test_a_dictation_session_read_line_by_line(model):
    session = [
        {"state": "recording"},
        *({"level": lvl} for lvl in (0.2, 0.8, 0.5)),
        {"state": "transcribing"},
        {"state": "done"},
        {"state": "hidden"},
    ]
    seen = []
    for message in session:
        apply_message(model, parse_line(json.dumps(message)))
        seen.append(model.state)
    assert seen == ["recording"] * 4 + ["transcribing", "done", "hidden"]
