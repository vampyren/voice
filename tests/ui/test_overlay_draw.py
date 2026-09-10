"""Pixel-level checks on the pill rendering, against the design's values.

pycairo lives in the system Python (it ships with PyGObject) and is not a
project dependency, so these skip in a bare uv venv. Run them the way the
helper process runs, or with `uv run --with pycairo pytest tests/ui`.
"""
import struct

import pytest

cairo = pytest.importorskip("cairo")

from voice.ui import overlay_draw as od                        # noqa: E402
from voice.ui.overlay_draw import natural_width, render_png    # noqa: E402
from voice.ui.overlay_model import AMP, COLLAPSE, OverlayModel  # noqa: E402


class Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt
        return self.t


def _model(state, levels=(1.0,) * 30, text=None, age=0.0, lang="en", to=None,
           elapsed=0.0):
    clock = Clock()
    model = OverlayModel(lang=lang, clock=clock)
    model.set_state("recording")
    for lvl in levels:
        model.push_level(lvl)
    if elapsed:
        model.tick(clock.advance(elapsed))
    if state != "recording":
        if to:
            model.set_language(to)
        model.set_state(state, text=text, now=clock.t)
    if age:
        model.tick(clock.advance(age))
    return model


class Image:
    """A rendered PNG, addressable as (r, g, b, a) with alpha un-multiplied."""

    def __init__(self, path):
        surface = cairo.ImageSurface.create_from_png(str(path))
        self.width, self.height = surface.get_width(), surface.get_height()
        self._stride = surface.get_stride()
        self._data = bytes(surface.get_data())

    def __call__(self, x, y):
        off = y * self._stride + x * 4
        b, g, r, a = self._data[off:off + 4]
        if a in (0, 255):
            return r, g, b, a
        return min(255, r * 255 // a), min(255, g * 255 // a), min(255, b * 255 // a), a

    def pixels(self, x0=0, y0=0, x1=None, y1=None):
        x1 = self.width if x1 is None else x1
        y1 = self.height if y1 is None else y1
        return [self(x, y) for y in range(y0, y1) for x in range(x0, x1)]

    def count(self, predicate, **box):
        return sum(1 for p in self.pixels(**box) if predicate(p))


def _png_size(path):
    with open(path, "rb") as fh:
        head = fh.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", head[16:24])


def wave(p):
    """Anywhere on the design's violet -> teal ramp: blue stays high throughout."""
    return p[3] > 60 and p[2] > 150 and p[2] - min(p[0], p[1]) > 40


def violet(p):
    return wave(p) and p[0] - p[1] > 30


def teal(p):
    return wave(p) and p[1] - p[0] > 30


def coral(p):
    # The dot is 55% opaque at the trough of its breath, so match the hue.
    return p[3] > 60 and p[0] > 100 and p[0] - p[1] > 35 and p[0] - p[2] > 25


def emerald(p):
    return p[3] > 60 and p[1] > 130 and p[0] < 120 and p[1] - p[2] > 20


def amberish(p):
    return p[3] > 60 and p[0] > 150 and 80 < p[1] < 200 and p[2] < 120


def light(p):
    return p[3] > 60 and min(p[0], p[1], p[2]) > 150


# -- size and layout ------------------------------------------------------

def test_the_font_is_a_real_monospace():
    probe = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1))
    probe.select_font_face(od.font_family(), cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    probe.set_font_size(24)
    assert probe.text_extents("i").x_advance == pytest.approx(probe.text_extents("M").x_advance)


def test_the_pill_is_the_designs_44_px_capsule_with_an_automatic_width(tmp_path):
    model = _model("recording")
    path = render_png(model, tmp_path / "rec.png")
    width, height = _png_size(path)
    assert height == 44
    assert 250 <= width <= 320, f"the design calls for a ~300 px pill, got {width}"
    assert width == natural_width(model, 44)


def test_the_pill_grows_for_a_longer_counter_and_a_longer_badge():
    short = _model("recording")
    long_counter = _model("recording", elapsed=754.0)          # 12:34
    assert long_counter.elapsed_text == "12:34"
    assert natural_width(long_counter, 44) > natural_width(short, 44)
    assert natural_width(_model("recording", lang="sv-se"), 44) > natural_width(short, 44)


def test_every_state_renders_at_one_and_two_times(tmp_path):
    for state, kw in [("recording", {}), ("transcribing", {"age": 1.0}),
                      ("done", {"age": 0.9}), ("notice", {"age": 0.5, "to": "sv"}),
                      ("error", {"age": 0.2, "text": "microphone is busy"})]:
        model = _model(state, **kw)
        one = render_png(model, tmp_path / f"{state}.png")
        assert _png_size(one) == (natural_width(model, 44), 44)
        two = render_png(_model(state, **kw), tmp_path / f"{state}-2x.png",
                         width=natural_width(model, 88), height=88)
        assert _png_size(two) == (natural_width(model, 88), 88)


def test_drawing_accepts_an_explicit_size(tmp_path):
    path = render_png(_model("recording"), tmp_path / "small.png", width=200, height=22)
    assert _png_size(path) == (200, 22)


@pytest.mark.parametrize("state", ["recording", "transcribing", "done", "notice", "error"])
def test_a_degenerate_size_draws_nothing_instead_of_crashing(tmp_path, state):
    # A 3 px tall pill has no room for anything; it must not divide by zero.
    path = render_png(_model(state, text="x", age=0.4), tmp_path / f"tiny-{state}.png",
                      width=40, height=3)
    assert _png_size(path) == (40, 3)
    assert max(p[3] for p in Image(path).pixels()) == 0


def test_the_bars_are_the_designs_thin_bars_with_3_px_gaps(tmp_path):
    """21 bars, gap 3, flex:1 across the 132 px well -> 3.43 px bars, 6.43 px pitch."""
    img = Image(render_png(_model("recording"), tmp_path / "rec.png"))
    lit = [x for x in range(36, 36 + 132) if wave(img(x, 22))]
    runs = []
    for x in lit:
        if runs and x == runs[-1][-1] + 1:
            runs[-1].append(x)
        else:
            runs.append([x])
    assert len(runs) == 21, f"expected 21 separate bars, found {len(runs)}"
    widths = [len(r) for r in runs]
    assert all(3 <= w <= 5 for w in widths), f"bar widths {widths}"
    gaps = [b[0] - a[-1] - 1 for a, b in zip(runs, runs[1:])]
    assert all(2 <= g <= 4 for g in gaps), f"gaps {gaps}"
    pitch = (runs[-1][0] - runs[0][0]) / 20
    assert pitch == pytest.approx((132 - 3 * 20) / 21 + 3, abs=0.2)


def test_the_capsule_fill_is_the_designs_translucent_obsidian(tmp_path):
    """rgba(15,16,20,0.88) over the design's #08090b background."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 280, 44)
    ctx = cairo.Context(surface)
    ctx.set_source_rgb(0x08 / 255, 0x09 / 255, 0x0b / 255)      # obsidian backdrop
    ctx.paint()
    from voice.ui.overlay_draw import draw
    draw(ctx, 280, 44, _model("recording"))
    path = tmp_path / "composited.png"
    surface.write_to_png(str(path))
    img = Image(path)
    r, g, b, a = img(img.width // 2, 3)          # pill body, above the bars
    assert a == 255
    expected = tuple(round(0.88 * fg + 0.12 * bg)
                     for fg, bg in ((15, 8), (16, 9), (20, 11)))
    assert (r, g, b) == pytest.approx(expected, abs=1), f"{(r, g, b)} != {expected}"


# -- the capsule itself ---------------------------------------------------

def test_the_capsule_body_is_near_black_and_mostly_opaque(tmp_path):
    img = Image(render_png(_model("recording"), tmp_path / "rec.png"))
    r, g, b, a = img(img.width // 2, 3)          # inside the pill, above the bars
    assert a > 200, "the body should be close to the design's 0.88 fill"
    assert max(r, g, b) < 70, f"expected a near-black body, got {(r, g, b)}"


def test_the_corners_outside_the_capsule_stay_transparent(tmp_path):
    img = Image(render_png(_model("recording"), tmp_path / "rec.png"))
    for corner in [(0, 0), (img.width - 1, 0), (0, img.height - 1),
                   (img.width - 1, img.height - 1)]:
        assert img(*corner)[3] < 40, f"corner {corner} is not rounded away"


def test_hidden_renders_nothing(tmp_path):
    model = _model("recording")
    model.set_state("hidden", now=0.0)
    img = Image(render_png(model, tmp_path / "hidden.png", width=280))
    assert max(p[3] for p in img.pixels()) == 0


# -- recording ------------------------------------------------------------

def test_recording_draws_a_waveform_that_runs_violet_to_teal(tmp_path):
    img = Image(render_png(_model("recording"), tmp_path / "rec.png"))
    well_x, well_end = 36, 36 + 132
    left = img.count(violet, x0=well_x, x1=well_x + 40)
    right = img.count(teal, x0=well_end - 40, x1=well_end)
    assert left > 30, f"the left of the waveform should be violet, found {left}"
    assert right > 30, f"the right of the waveform should be teal, found {right}"
    assert img.count(teal, x0=well_x, x1=well_x + 20) == 0
    assert img.count(violet, x0=well_end - 20, x1=well_end) == 0


def test_the_waveform_follows_the_amplitude_envelope(tmp_path):
    img = Image(render_png(_model("recording"), tmp_path / "rec.png"))

    def bar_height(index):
        gap, bar_w = 3.0, (132 - 20 * 3) / 21
        x = int(36 + index * (bar_w + gap) + bar_w / 2)
        return sum(1 for y in range(img.height) if wave(img(x, y)))

    tall = bar_height(AMP.index(1.0))            # a full-height bar in the design
    short = bar_height(0)                        # the .18 bar at the very left
    assert tall > 20, f"the tallest bar should fill the 24 px well, got {tall}"
    assert short < tall / 2


def test_a_silent_recording_rests_at_the_floor_instead_of_going_flat(tmp_path):
    loud = Image(render_png(_model("recording"), tmp_path / "loud.png"))
    quiet = Image(render_png(_model("recording", levels=(0.0,) * 30), tmp_path / "quiet.png"))
    assert 0 < quiet.count(wave) < loud.count(wave) / 2


def test_the_recording_dot_breathes_with_a_halo(tmp_path):
    trough = Image(render_png(_model("recording"), tmp_path / "trough.png"))
    peak = Image(render_png(_model("recording", age=1.2), tmp_path / "peak.png"))
    box = dict(x0=0, x1=30)
    assert trough.count(coral, **box) > 0
    assert peak.count(coral, **box) > trough.count(coral, **box), "no breathing"


def test_the_counter_and_badge_sit_on_the_right(tmp_path):
    img = Image(render_png(_model("recording", elapsed=12.0), tmp_path / "rec.png"))
    digits = img.count(light, x0=int(img.width * 0.62), x1=img.width - 40)
    assert digits > 20, f"expected white digits before the badge, found {digits}"
    badge_box = dict(x0=img.width - 40, x1=img.width - 6)
    frame = img.count(lambda p: p[3] > 200 and 30 < max(p[:3]) < 200, **badge_box)
    assert frame > 20, "expected the bordered language chip on the far right"


# -- transcribing ---------------------------------------------------------

def test_transcribing_collapses_the_bars_onto_a_track(tmp_path):
    starting = Image(render_png(_model("transcribing"), tmp_path / "t0.png"))
    settled = Image(render_png(_model("transcribing", age=COLLAPSE + 0.05), tmp_path / "t1.png"))
    off_axis = dict(y0=0, y1=17)                 # above the 2 px track at y=22
    assert starting.count(wave, **off_axis) > 20
    assert settled.count(wave, **off_axis) == 0
    band = settled.count(wave, y0=21, y1=23)
    assert band > 20, "the gradient fill line should remain on the track"


def test_the_transcribing_fill_sweeps_from_the_left(tmp_path):
    early = Image(render_png(_model("transcribing", age=0.6), tmp_path / "a.png"))
    late = Image(render_png(_model("transcribing", age=1.9), tmp_path / "b.png"))

    def reach(img):
        lit = [x for x in range(img.width) for y in (21, 22) if wave(img(x, y))]
        return min(lit), max(lit)

    assert reach(early)[0] == pytest.approx(reach(late)[0], abs=2)   # both start left
    assert reach(late)[1] > reach(early)[1] + 20                     # and it grows


def test_the_transcribing_counter_is_dimmed(tmp_path):
    live = Image(render_png(_model("recording", elapsed=12.0), tmp_path / "rec.png"))
    frozen = Image(render_png(_model("transcribing", elapsed=12.0, age=0.5), tmp_path / "tra.png"))
    box = dict(x0=int(live.width * 0.62), x1=live.width - 40)
    assert live.count(light, **box) > 20
    assert frozen.count(light, **box) == 0, "the frozen counter is #71717a, not white"


# -- done -----------------------------------------------------------------

def test_the_checkmark_draws_itself_in_then_the_label_rises(tmp_path):
    def shot(age):
        return Image(render_png(_model("done", age=age), tmp_path / f"done-{age}.png"))

    early, mid, late = shot(0.25), shot(0.5), shot(0.95)
    check = dict(x0=36, x1=36 + 60)
    assert 0 < early.count(emerald, **check) < mid.count(emerald, **check)
    # the design's 18x18 icon: the drawn path is ~12 x 9 px inside it
    lit = [(x, y) for y in range(late.height) for x in range(36, 96)
           if emerald(late(x, y))]
    span_x = max(x for x, _ in lit) - min(x for x, _ in lit) + 1
    span_y = max(y for _, y in lit) - min(y for _, y in lit) + 1
    assert 10 <= span_x <= 15, f"checkmark is {span_x} px wide, expected ~12"
    assert 7 <= span_y <= 12, f"checkmark is {span_y} px tall, expected ~9"
    label = dict(x0=36 + 60, x1=36 + 132)
    assert early.count(lambda p: p[3] > 60 and max(p[:3]) > 90, **label) == 0
    assert late.count(lambda p: p[3] > 60 and max(p[:3]) > 90, **label) > 20


def test_done_shows_an_emerald_status_dot(tmp_path):
    img = Image(render_png(_model("done", age=0.9), tmp_path / "done.png"))
    assert img.count(emerald, x0=0, x1=30) > 20


# -- notice ---------------------------------------------------------------

def test_the_notice_shows_where_the_language_went(tmp_path):
    img = Image(render_png(_model("notice", to="sv", age=0.5), tmp_path / "n.png"))
    well = dict(x0=36, x1=36 + 132)
    assert img.count(light, **well) > 20, "the destination language is bright"
    assert img.count(lambda p: p[3] > 60 and 60 < max(p[:3]) < 150, **well) > 20


def test_the_notice_lifetime_hairline_shrinks(tmp_path):
    def reach(age):
        img = Image(render_png(_model("notice", to="sv", age=age), tmp_path / f"n-{age}.png"))
        lit = [x for x in range(img.width) if wave(img(x, img.height - 1))]
        return max(lit) if lit else 0

    assert reach(0.2) > reach(1.5) > 0
    assert reach(1.99) < reach(0.2) / 4


def test_the_notice_well_accepts_an_ascii_arrow(tmp_path):
    unicode_arrow = render_png(_model("notice", to="sv", text="EN → SV", age=0.5),
                               tmp_path / "u.png")
    ascii_arrow = render_png(_model("notice", to="sv", text="EN -> SV", age=0.5),
                             tmp_path / "a.png")
    assert open(unicode_arrow, "rb").read() == open(ascii_arrow, "rb").read()


def test_the_badge_still_reads_the_old_language_on_its_way_out(tmp_path):
    leaving = Image(render_png(_model("notice", to="sv", age=0.05), tmp_path / "out.png"))
    arrived = Image(render_png(_model("notice", to="sv", age=0.5), tmp_path / "in.png"))
    box = dict(x0=leaving.width - 40, x1=leaving.width - 6)
    # "EN" leaving and "SV" arriving put ink in different places
    assert leaving.count(lambda p: p[3] > 60 and max(p[:3]) > 90, **box) > 0
    assert [p[:3] for p in leaving.pixels(**box)] != [p[:3] for p in arrived.pixels(**box)]


def test_the_notice_badge_shows_the_new_language(tmp_path):
    before = Image(render_png(_model("recording"), tmp_path / "before.png"))
    during = Image(render_png(_model("notice", to="sv", age=0.5), tmp_path / "during.png"))
    box = dict(x0=before.width - 40, x1=before.width - 6)
    # the notice chip is brighter: #f4f4f5 text on a rgba(255,255,255,.18) border
    assert during.count(light, **box) > before.count(light, **box)


# -- error (kept from the daemon protocol; the design has no error artboard)

def test_error_shows_the_message_in_amber(tmp_path):
    img = Image(render_png(_model("error", text="microphone is busy", age=0.2),
                           tmp_path / "err.png"))
    assert img.count(amberish) > 40


def test_a_long_error_message_stays_inside_the_pill(tmp_path):
    img = Image(render_png(_model("error", text="x" * 300, age=0.2), tmp_path / "long.png"))
    edge = [img(x, y) for y in range(img.height) for x in (0, 1, img.width - 2, img.width - 1)]
    assert all(max(p[:3]) < 90 for p in edge), "text spilled over the pill edge"
