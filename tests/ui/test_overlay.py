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
