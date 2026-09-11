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
from voice.ui.overlay_model import BAR_FLOOR, TAPER, OverlayModel
from voice.ui.placement import POSITIONS, normalise_position


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


def _fake_main(monkeypatch):
    import voice.ui.overlay as overlay

    monkeypatch.setattr(overlay, "_load_gtk", lambda: (object(), object(), object(), None))
    monkeypatch.setattr(overlay, "_Pill", lambda *a, **kw: type("P", (), {"run": lambda self: 0})())
    return overlay


def test_the_fallback_window_warns_about_focus_and_about_the_placement(monkeypatch, caplog):
    """Both sentences, once each, on the path that really shows a plain window."""
    overlay = _fake_main(monkeypatch)
    with caplog.at_level("WARNING", logger="voice.ui.overlay"):
        assert overlay.main(["--position", "middle-right", "--margin-x", "20"]) == 0
    assert caplog.text.count("take keyboard focus") == 1
    assert caplog.text.count("middle-right") == 1


def test_a_padded_fallback_window_says_it_is_approximating_the_placement(monkeypatch, caplog):
    """The old warning says the placement does nothing here. With the padding on
    it does something, so saying otherwise would send the owner looking for a
    bug in a setting that just worked."""
    overlay = _fake_main(monkeypatch)
    with caplog.at_level("INFO", logger="voice.ui.overlay"):
        assert overlay.main(["--position", "middle-right", "--margin-x", "20",
                             "--pad-to-place"]) == 0
    assert caplog.text.count("take keyboard focus") == 1
    assert "do nothing here" not in caplog.text
    assert "middle-right" in caplog.text and "ui.overlay_pad_to_place" in caplog.text


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


# -- no layer shell: padding the window to place the pill -----------------
# The compositor centres the *window*, not the pill drawn in it. So make the
# window bigger than the pill and draw the pill against the edge the placement
# names: the centre of a 271x540 window is still the centre of the screen, and
# a pill at the bottom of it sits a quarter-screen below that.

SCREEN = (1920, 1080)
PILL = (271, 44)


@pytest.mark.parametrize("position,window,origin", [
    ("top-left", (960, 540), (0, 0)),
    ("top-center", (271, 540), (0, 0)),
    ("top-right", (960, 540), (689, 0)),
    ("middle-left", (960, 44), (0, 0)),
    ("middle-center", (271, 44), (0, 0)),
    ("middle-right", (960, 44), (689, 0)),
    ("bottom-left", (960, 540), (0, 496)),
    ("bottom-center", (271, 540), (0, 496)),
    ("bottom-right", (960, 540), (689, 496)),
])
def test_the_padded_window_and_the_pill_in_it_for_each_anchor(position, window, origin):
    from voice.ui.overlay import padded_window

    assert padded_window(position, 0, 48, SCREEN, PILL) == (window, origin)


@pytest.mark.parametrize("position,window,origin", [
    ("top-left", (519, 479), (0, 0)),
    ("bottom-right", (520, 480), (249, 436)),
    ("middle-center", (271, 44), (0, 0)),
    ("top-center", (271, 479), (0, 0)),
    ("middle-left", (519, 44), (0, 0)),
])
def test_the_margins_decide_how_much_padding_is_spent(position, window, origin):
    """Margins the cap can afford: 700 in from the side, 300 from the top."""
    from voice.ui.overlay import padded_window

    assert padded_window(position, 700, 300, SCREEN, PILL) == (window, origin)


@pytest.mark.parametrize("position", POSITIONS)
@pytest.mark.parametrize("margins", [(0, 48), (700, 300), (0, 0)])
def test_an_uncapped_window_puts_the_pill_exactly_where_the_placement_says(position, margins):
    """The reason this works: the window is centred, so the pill's distance from
    the centre of the window is its distance from the centre of the screen."""
    from voice.ui.placement import pill_origin

    from voice.ui.overlay import padded_window

    margin_x, margin_y = margins
    (width, height), (x, y) = padded_window(position, margin_x, margin_y, SCREEN, PILL,
                                            fraction=1.0)
    left, top = (SCREEN[0] - width) // 2, (SCREEN[1] - height) // 2
    assert (left + x, top + y) == pill_origin(position, margin_x, margin_y, SCREEN, PILL)


def test_the_window_never_takes_more_than_half_the_screen():
    from voice.ui.overlay import PAD_SCREEN_FRACTION, padded_window

    assert PAD_SCREEN_FRACTION <= 0.5
    (width, height), origin = padded_window("bottom-center", 0, 48, (800, 600), PILL)
    assert (width, height) == (271, 300)
    assert origin == (0, 256)


def test_a_screen_smaller_than_the_pill_gets_no_padding_at_all():
    from voice.ui.overlay import padded_window

    assert padded_window("bottom-right", 0, 48, (200, 40), PILL) == (PILL, (0, 0))


def test_a_negative_margin_pushes_the_pill_out_only_as_far_as_the_screen():
    """A layer surface can hang the pill off the edge; a centred window cannot,
    and half a pill over the side would look like a bug, not a setting."""
    from voice.ui.overlay import padded_window

    (width, height), (x, y) = padded_window("bottom-right", -40, -40, SCREEN, PILL)
    left, top = (SCREEN[0] - width) // 2, (SCREEN[1] - height) // 2
    assert left + x + PILL[0] <= SCREEN[0] and top + y + PILL[1] <= SCREEN[1]
    assert left + x >= 0 and top + y >= 0


def test_the_padded_window_is_never_smaller_than_the_pill():
    from voice.ui.overlay import padded_window

    for position in POSITIONS:
        for screen in [(1920, 1080), (800, 600), (200, 100), (1, 1)]:
            (width, height), (x, y) = padded_window(position, 0, 48, screen, PILL)
            assert (width, height) >= PILL
            assert 0 <= x <= width - PILL[0] and 0 <= y <= height - PILL[1]


# -- asking the display how big the screen is -----------------------------

class FakeMonitor:
    def __init__(self, width, height):
        self._geometry = type("Rect", (), {"width": width, "height": height})()

    def get_geometry(self):
        return self._geometry


def _fake_gdk(monitor):
    import types

    monitors = types.SimpleNamespace(get_item=lambda i: monitor)
    display = types.SimpleNamespace(get_monitors=lambda: monitors)
    return types.SimpleNamespace(Display=types.SimpleNamespace(get_default=lambda: display))


def test_the_screen_size_comes_from_the_monitor():
    from voice.ui.overlay import monitor_size

    assert monitor_size(_fake_gdk(FakeMonitor(2560, 1440))) == (2560, 1440)


@pytest.mark.parametrize("monitor", [None, FakeMonitor(0, 0)])
def test_a_display_that_will_not_say_falls_back_to_a_conservative_screen(monitor):
    from voice.ui.overlay import FALLBACK_SCREEN, monitor_size

    assert monitor_size(_fake_gdk(monitor)) == FALLBACK_SCREEN


def test_no_display_at_all_falls_back_rather_than_raising():
    import types

    from voice.ui.overlay import FALLBACK_SCREEN, monitor_size

    gdk = types.SimpleNamespace(Display=types.SimpleNamespace(get_default=lambda: None))
    assert monitor_size(gdk) == FALLBACK_SCREEN


# -- the helper's own window, with GTK faked out --------------------------

class FakeSurface:
    """A GdkSurface that either takes an input region or cannot convert one."""

    def __init__(self, regions_work=True):
        self.regions_work = regions_work
        self.regions = []

    def set_input_region(self, region):
        if not self.regions_work:
            raise TypeError("Couldn't find foreign struct converter for 'cairo.Region'")
        self.regions.append(region)


class FakeWindow:
    def __init__(self):
        self.child = None
        self.visible = False
        self.surface = FakeSurface()
        self.css_classes = []

    def set_decorated(self, on): self.decorated = on
    def set_resizable(self, on): self.resizable = on
    def set_can_focus(self, on): self.can_focus = on
    def set_title(self, title): self.title = title
    def add_css_class(self, name): self.css_classes.append(name)
    def set_child(self, child): self.child = child
    def set_visible(self, on): self.visible = on
    def get_visible(self): return self.visible
    def get_mapped(self): return self.visible
    def get_surface(self): return self.surface
    def present(self): self.visible = True


class FakePicture:
    def __init__(self):
        self.paintable = None
        self.size_request = None

    def set_can_shrink(self, on): pass
    def set_content_fit(self, fit): pass
    def set_paintable(self, paintable): self.paintable = paintable
    def set_size_request(self, w, h): self.size_request = (w, h)


class FakeTexture:
    def __init__(self, width, height, data, stride):
        self.width, self.height, self.data, self.stride = width, height, data, stride


def _fake_toolkit(monitor=(1920, 1080)):
    """Just enough Gtk/Gdk/GLib for `_Pill` to build a window and a frame."""
    import types

    gtk = types.SimpleNamespace(
        Settings=types.SimpleNamespace(get_default=lambda: types.SimpleNamespace(
            get_property=lambda name: True)),
        Window=FakeWindow, Picture=FakePicture,
        ContentFit=types.SimpleNamespace(FILL="fill"),
        CssProvider=lambda: types.SimpleNamespace(load_from_data=lambda data: None),
        StyleContext=types.SimpleNamespace(add_provider_for_display=lambda *a: None),
        STYLE_PROVIDER_PRIORITY_APPLICATION=600)
    gdk = _fake_gdk(FakeMonitor(*monitor))
    gdk.MemoryTexture = types.SimpleNamespace(
        new=lambda w, h, fmt, data, stride: FakeTexture(w, h, data, stride))
    gdk.MemoryFormat = types.SimpleNamespace(B8G8R8A8_PREMULTIPLIED="bgra")
    glib = types.SimpleNamespace(
        MainLoop=lambda: types.SimpleNamespace(run=lambda: None, quit=lambda: None),
        Bytes=types.SimpleNamespace(new=lambda data: data),
        idle_add=lambda *a: None, timeout_add=lambda *a: 1,
        SOURCE_REMOVE=False, SOURCE_CONTINUE=True)
    return gtk, gdk, glib


def _recording_pill(argv=(), layer_shell=None, monitor=(1920, 1080)):
    pytest.importorskip("cairo")           # the helper draws with pycairo
    from voice.ui.overlay import _Pill

    gtk, gdk, glib = _fake_toolkit(monitor)
    pill = _Pill(build_parser().parse_args(list(argv)), gtk, gdk, glib, layer_shell)
    pill.model.set_state("recording")
    return pill


def test_a_layer_shell_surface_is_left_exactly_the_size_of_the_pill():
    """Anchored properly, the padding would only be in the way."""
    from voice.ui.overlay_draw import natural_width

    pill = _recording_pill(layer_shell=FakeShell())
    pill._render()
    width = natural_width(pill.model, 44)
    assert (pill.view.paintable.width, pill.view.paintable.height) == (width, 44)
    assert pill.view.size_request == (width, 44)
    assert pill.window.surface.regions == [], "no region is set where none is needed"


def test_a_plain_window_is_padded_so_the_pill_lands_near_the_placement():
    from voice.ui.overlay import padded_window
    from voice.ui.overlay_draw import natural_width

    pill = _recording_pill(["--pad-to-place", "--position", "bottom-center",
                            "--margin-y", "48"])
    pill._render()
    width = natural_width(pill.model, 44)
    window, origin = padded_window("bottom-center", 0, 48, (1920, 1080), (width, 44))
    assert window == (width, 540) and origin == (0, 496)
    assert (pill.view.paintable.width, pill.view.paintable.height) == window
    assert pill.view.size_request == window


def test_a_plain_window_is_the_size_of_the_pill_unless_the_padding_is_asked_for():
    """The padding moves the pill, and charges for it in clicks; off by default."""
    from voice.ui.overlay_draw import natural_width

    assert build_parser().parse_args([]).pad_to_place is False
    assert build_parser().parse_args(["--pad-to-place"]).pad_to_place is True
    pill = _recording_pill()
    pill._render()
    width = natural_width(pill.model, 44)
    assert (pill.view.paintable.width, pill.view.paintable.height) == (width, 44)


def test_the_padding_lets_clicks_through_by_asking_for_the_pills_input_region():
    cairo = pytest.importorskip("cairo")
    from voice.ui.overlay_draw import natural_width

    pill = _recording_pill(["--pad-to-place"])
    pill._render()
    width = natural_width(pill.model, 44)
    assert len(pill.window.surface.regions) == 1
    region = pill.window.surface.regions[0]
    assert isinstance(region, cairo.Region)
    rect = region.get_rectangle(0)
    assert (rect.x, rect.y, rect.width, rect.height) == (0, 496, width, 44)


def test_a_display_that_cannot_take_an_input_region_still_shows_the_pill(caplog):
    """PyGObject needs its cairo foreign-struct support (python3-gi-cairo) to
    convert a region; without it the padding takes clicks and the pill still runs."""
    pill = _recording_pill(["--pad-to-place"])
    pill.window.surface.regions_work = False
    with caplog.at_level("INFO", logger="voice.ui.overlay"):
        pill._render()
        pill.model.push_level(0.9)
        pill._render()
    assert pill.view.paintable.height == 540
    assert caplog.text.count("input region") == 1, "said once, not once a frame"


def test_the_padded_window_is_still_drawn_the_moment_the_surface_appears():
    """The surface only exists once the window is mapped, so the region is
    asked for again there rather than only on a resize."""
    pill = _recording_pill(["--pad-to-place"])
    pill.window.surface = None
    pill._render()
    pill.window.surface = FakeSurface()
    pill._log_mapped()
    assert len(pill.window.surface.regions) == 1
