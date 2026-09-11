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
from voice.ui.overlay_model import BAR_FLOOR, FINISH, TAPER, OverlayModel
from voice.ui.placement import normalise_position


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
    assert max(model.bar_heights) == pytest.approx(1.0)
    assert min(model.bar_heights) == pytest.approx(1.0 - TAPER)


def test_a_level_arriving_as_a_string_is_still_a_number(model):
    apply_message(model, {"state": "recording"})
    assert apply_message(model, {"level": "0.5"}) is True
    assert model.bar_heights[10] > BAR_FLOOR


def test_an_unusable_level_is_ignored(model, caplog):
    apply_message(model, {"state": "recording"})
    with caplog.at_level("WARNING"):
        assert apply_message(model, {"level": "loud"}) is False
    assert "bad level" in caplog.text
    assert max(model.bar_heights) == pytest.approx(BAR_FLOOR)


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
    assert (args.height, args.position, args.lang) == (44, "bottom-center", "en")
    assert (args.margin_x, args.margin_y) == (0, 48)
    assert build_parser().parse_args(
        ["--lang", "sv", "--position", "top-right"]).position == "top-right"
    assert build_parser().parse_args(["--margin-x", "-12"]).margin_x == -12


@pytest.mark.parametrize("legacy,expected", [("bottom", "bottom-center"), ("top", "top-center")])
def test_the_helper_still_takes_the_two_old_positions(legacy, expected):
    """A daemon and a helper are upgraded together, but a hand-run command line
    and an older unit are not."""
    args = build_parser().parse_args(["--position", legacy])
    assert normalise_position(args.position) == expected


def test_a_position_the_parser_does_not_know_is_refused_at_the_command_line():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--position", "sideways"])


# -- the layer surface ----------------------------------------------------

class FakeShell:
    """gtk4-layer-shell, recording what the helper asks of it."""

    class Edge:
        TOP, BOTTOM, LEFT, RIGHT = "TOP", "BOTTOM", "LEFT", "RIGHT"

    class Layer:
        OVERLAY = "OVERLAY"

    class KeyboardMode:
        NONE = "NONE"

    def __init__(self):
        self.window = None
        self.layer = None
        self.keyboard = None
        self.exclusive = None
        self.anchored: dict[str, bool] = {}
        self.margins: dict[str, int] = {}

    def init_for_window(self, window):
        self.window = window

    def set_layer(self, window, layer):
        self.layer = layer

    def set_anchor(self, window, edge, on):
        self.anchored[edge] = on

    def set_margin(self, window, edge, px):
        self.margins[edge] = px

    def set_keyboard_mode(self, window, mode):
        self.keyboard = mode

    def set_exclusive_zone(self, window, zone):
        self.exclusive = zone


@pytest.mark.parametrize("position,anchored,margins", [
    ("top-left", {"TOP", "LEFT"}, {"TOP": 48, "LEFT": 12}),
    ("top-center", {"TOP"}, {"TOP": 48}),
    ("top-right", {"TOP", "RIGHT"}, {"TOP": 48, "RIGHT": 12}),
    ("middle-left", {"LEFT"}, {"LEFT": 12}),
    ("middle-center", set(), {}),
    ("middle-right", {"RIGHT"}, {"RIGHT": 12}),
    ("bottom-left", {"BOTTOM", "LEFT"}, {"BOTTOM": 48, "LEFT": 12}),
    ("bottom-center", {"BOTTOM"}, {"BOTTOM": 48}),
    ("bottom-right", {"BOTTOM", "RIGHT"}, {"BOTTOM": 48, "RIGHT": 12}),
])
def test_each_placement_becomes_its_anchors_and_margins_on_the_surface(position, anchored, margins):
    from voice.ui.overlay import init_layer_shell

    shell, window = FakeShell(), object()
    init_layer_shell(shell, window, position, margin_x=12, margin_y=48)
    assert shell.window is window
    assert {edge for edge, on in shell.anchored.items() if on} == anchored
    # Every edge is stated, not only the anchored ones: an unstated anchor is a
    # default, and a default is not something this helper should rely on.
    assert set(shell.anchored) == {"TOP", "BOTTOM", "LEFT", "RIGHT"}
    assert shell.margins == margins
    assert (shell.layer, shell.keyboard, shell.exclusive) == ("OVERLAY", "NONE", -1)


def test_the_surface_is_still_a_focus_refusing_overlay_whatever_the_placement():
    from voice.ui.overlay import init_layer_shell

    shell = FakeShell()
    init_layer_shell(shell, object(), "middle-center", margin_x=0, margin_y=0)
    assert (shell.layer, shell.keyboard, shell.exclusive) == ("OVERLAY", "NONE", -1)


# -- no layer shell: the placement cannot be honoured ---------------------

def test_a_plain_window_says_once_that_it_cannot_be_placed(caplog):
    from voice.ui.overlay import warn_about_placement

    with caplog.at_level("WARNING", logger="voice.ui.overlay"):
        warn_about_placement("top-right", 0, 48)
    assert "top-right" in caplog.text and "ui.overlay_position" in caplog.text


def test_a_plain_window_at_the_default_placement_says_nothing(caplog):
    from voice.ui.overlay import warn_about_placement

    with caplog.at_level("WARNING", logger="voice.ui.overlay"):
        warn_about_placement("bottom-center", 0, 48)
    assert caplog.text == ""


def test_the_fallback_window_warns_about_focus_and_about_the_placement(monkeypatch, caplog):
    """Both sentences, once each, on the path that really shows a plain window."""
    import voice.ui.overlay as overlay

    monkeypatch.setattr(overlay, "_load_gtk", lambda: (object(), object(), object(), None))
    monkeypatch.setattr(overlay, "_Pill", lambda *a, **kw: type("P", (), {"run": lambda self: 0})())
    with caplog.at_level("WARNING", logger="voice.ui.overlay"):
        assert overlay.main(["--position", "middle-right", "--margin-x", "20"]) == 0
    assert caplog.text.count("take keyboard focus") == 1
    assert caplog.text.count("middle-right") == 1


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


@pytest.mark.parametrize("text", [None, "Copied · Ctrl+V"])
def test_a_done_after_transcribing_runs_the_fill_to_the_end(text):
    """However the dictation ends - inserted, or only copied to the clipboard -
    the daemon says `done` the same way, and the pill finishes the fill first."""
    clock = Clock()
    model = OverlayModel(clock=clock)
    assert apply_message(model, {"state": "transcribing"}) is True
    clock.t += 1.0
    model.tick()
    caught = model.sweep[0]
    assert 0.0 < caught < 1.0
    message = {"state": "done"} if text is None else {"state": "done", "text": text}
    assert apply_message(model, message) is True
    assert model.finishing is True
    assert model.sweep[0] == pytest.approx(caught), "it carries on from where it was"
    clock.t += FINISH / 2
    model.tick()
    assert model.finishing is True and caught < model.sweep[0] < 1.0
    clock.t += FINISH                           # well past the 0.2 s completion
    model.tick()
    assert model.sweep == pytest.approx((1.0, 1.0))
    assert model.finishing is False and model.text == text


def test_the_finish_message_runs_the_fill_to_the_end_before_the_pill_hides():
    """What the daemon sends when it is about to unmap the pill for the paste
    chord: finish the fill first, so nothing takes a half-drawn line off screen."""
    clock = Clock()
    model = OverlayModel(clock=clock)
    apply_message(model, {"state": "transcribing"})
    clock.t += 1.0
    model.tick()
    caught = model.sweep[0]
    assert 0.0 < caught < 1.0
    assert apply_message(model, {"finish": True}) is True
    assert model.finishing is True
    assert model.sweep[0] == pytest.approx(caught)
    clock.t += FINISH
    model.tick()
    assert model.sweep == pytest.approx((1.0, 1.0)), "complete before it goes off screen"
    apply_message(model, {"state": "hidden"})              # the chord goes out
    clock.t += 0.3
    apply_message(model, {"state": "done"})                # and the pill comes back
    assert model.state == "done" and model.finishing is False


def test_a_finish_with_no_fill_on_screen_is_ignored(model, caplog):
    with caplog.at_level("WARNING"):
        assert apply_message(model, {"finish": True}) is False
    assert model.finishing is False


# -- review findings ------------------------------------------------------

@pytest.mark.parametrize("value", [None, 42, ["sv"], {"code": "sv"}, True])
def test_a_language_that_is_not_a_string_is_refused(model, caplog, value):
    with caplog.at_level("WARNING"):
        assert apply_message(model, {"language": value}) is False
    assert model.badge_text == "EN"
    assert "language" in caplog.text


def test_the_helper_can_insist_on_layer_shell():
    from voice.ui.overlay import layer_shell_exit_code
    assert layer_shell_exit_code(shell=None, require=False) is None
    assert layer_shell_exit_code(shell=object(), require=True) is None
    assert layer_shell_exit_code(shell=None, require=True) == 2
    assert build_parser().parse_args([]).require_layer_shell is False
    assert build_parser().parse_args(["--require-layer-shell"]).require_layer_shell is True


# -- layer-shell: installed is not the same as usable ----------------------
def _stub_gi(monkeypatch, layer_shell="absent"):
    """A fake `gi` whose Gtk4LayerShell namespace is absent, or present and
    (un)supported by the compositor - GNOME has no zwlr_layer_shell_v1."""
    import sys
    import types

    gi = types.ModuleType("gi")
    repo = types.ModuleType("gi.repository")
    repo.Gtk, repo.Gdk, repo.GLib = object(), object(), object()

    def require_version(name, version):
        if name == "Gtk4LayerShell" and layer_shell == "absent":
            raise ValueError("Namespace Gtk4LayerShell not available")

    gi.require_version = require_version
    if layer_shell != "absent":
        shell = types.SimpleNamespace()
        if layer_shell != "no is_supported":
            shell.is_supported = lambda: layer_shell == "supported"
        repo.Gtk4LayerShell = shell
    gi.repository = repo
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repo)
    return repo


@pytest.mark.parametrize("case,usable", [
    ("absent", False),
    ("unsupported", False),          # installed, but this compositor has no protocol
    ("supported", True),
    ("no is_supported", True),       # an older binding: we cannot tell, so trust it
])
def test_layer_shell_counts_as_present_only_when_the_compositor_supports_it(
        monkeypatch, case, usable):
    from voice.ui.overlay import _load_gtk

    repo = _stub_gi(monkeypatch, case)
    _, _, _, shell = _load_gtk()
    assert (shell is not None) is usable
    if usable and case != "absent":
        assert shell is repo.Gtk4LayerShell


def test_an_unsupported_layer_shell_says_so_rather_than_looking_absent(monkeypatch, caplog):
    from voice.ui.overlay import _load_gtk

    _stub_gi(monkeypatch, "unsupported")
    with caplog.at_level("INFO", logger="voice.ui.overlay"):
        _load_gtk()
    assert "compositor" in caplog.text
