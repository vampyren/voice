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
from voice.ui.overlay_model import COLLAPSE, FINISH, FRAME, OverlayModel  # noqa: E402


class Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt
        return self.t


#: A speech-like run of levels: loud syllables, short gaps, one near-silence.
#: A steady level draws a steady wave, which is correct but tells us nothing
#: about whether the bars follow the audio, so the shape tests use this.
SPEECH = (0.35, 0.62, 0.48, 0.71, 0.15, 0.55, 0.28, 0.66, 0.09, 0.58, 0.44)


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
        # `to` is what makes this a language switch, so it is what tells the
        # notice to animate the chip - the model no longer infers that.
        model.set_state(state, text=text, now=clock.t, swaps_language=bool(to))
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
def test_a_surface_too_small_for_a_capsule_draws_nothing(tmp_path, state):
    path = render_png(_model(state, text="x", age=0.4), tmp_path / f"tiny-{state}.png",
                      width=40, height=3)
    assert _png_size(path) == (40, 3)
    assert max(p[3] for p in Image(path).pixels()) == 0


def test_a_zero_scale_surface_does_not_divide_by_zero(tmp_path):
    """At height 0 the checkmark's path length is 0; it used to raise there."""
    from voice.ui.overlay_draw import draw
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 40, 1)
    for state in ("recording", "transcribing", "done", "notice", "error"):
        draw(cairo.Context(surface), 40, 0, _model(state, text="x", age=0.4))


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


def bar_columns(img, s=1.0, n=21):
    """Rendered height in pixels of every bar, measured down its centre line."""
    gap, well = 3.0 * s, 132.0 * s
    bar_w = (well - gap * (n - 1)) / n
    x0 = 36.0 * s
    return [sum(1 for y in range(img.height)
                if wave(img(int(x0 + i * (bar_w + gap) + bar_w / 2), y)))
            for i in range(n)]


def test_the_waveform_tapers_from_the_newest_sample_out_to_the_ends(tmp_path):
    img = Image(render_png(_model("recording"), tmp_path / "rec.png"))
    bars = bar_columns(img)
    assert bars[10] > 20, f"the newest sample should fill the 24 px well, got {bars}"
    assert bars[0] == pytest.approx(bars[10] * 0.65, abs=3), bars
    assert bars[20] == pytest.approx(bars[10] * 0.65, abs=3), bars


def test_neighbouring_bars_differ_because_each_is_a_different_moment(tmp_path):
    """The defect this replaces: one loudness scaling a fixed silhouette, so
    the pill could only ever be the same shape breathing. Real audio must put
    visibly different heights side by side, and move them between frames."""
    model = _model("recording", levels=SPEECH)
    first = bar_columns(Image(render_png(model, tmp_path / "f1.png", height=88)), s=2.0)
    steps = [abs(b - a) for a, b in zip(first, first[1:])]
    assert max(steps) >= 8, f"the bars are all but flat against each other: {first}"
    assert len(set(first)) >= 6, f"only {len(set(first))} distinct heights: {first}"

    model.push_level(0.05)                       # one more chunk: a gap in speech
    second = bar_columns(Image(render_png(model, tmp_path / "f2.png", height=88)), s=2.0)
    assert second != first, "the wave froze between frames"
    ratios = [b / a for a, b in zip(first, second) if a > 0]
    assert max(ratios) - min(ratios) > 0.3, f"the whole shape merely scaled: {ratios}"


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


def test_a_long_transcription_reaches_the_end_and_never_snaps_back(tmp_path):
    """Two frames two seconds apart, two minutes into a slow conversion.

    The old loop had restarted between them - it was back at the left edge at
    122.2 s - which is the bar "stopping in the middle" the owner photographed.
    The fill now crosses the whole track and rests at the end of it: "go all
    the way to the right" was the requirement, and a bar parked short of the
    end reads as stalled however good the reason for it.
    """
    well_x, well_right = 36, 36 + 132

    def reach(age, name):
        img = Image(render_png(_model("transcribing", age=age), tmp_path / name))
        return max(x for x in range(well_x, well_right)
                   for y in (21, 22) if wave(img(x, y)))

    late, later = reach(120.0, "long.png"), reach(122.2, "longer.png")
    assert later >= late, "the fill must never fall back"
    assert later - late <= 1, "and two minutes in it is not moving either"
    assert late >= well_right - 3, "it reaches the end of the track"


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


def _finishing(sweep=1.0, age=0.0):
    """A pill that has just been told the transcription is over.

    `sweep` is how long it had been transcribing - the fill is caught
    part-way across, exactly as a real transcription leaves it - and `age` is
    how far into the ending we are.
    """
    clock = Clock()
    model = OverlayModel(clock=clock)
    model.set_state("recording")
    for lvl in SPEECH:
        model.push_level(lvl)
    model.set_state("transcribing", now=clock.t)
    model.tick(clock.advance(sweep))
    model.set_state("done", now=clock.t)
    if age:
        model.tick(clock.advance(age))
    return model


def test_the_fill_reaches_the_end_of_the_track_before_the_checkmark(tmp_path):
    """The owner's complaint: "the bar never goes to the end, it stops in the
    middle and then it's done"."""
    def reach(img):
        lit = [x for x in range(img.width) for y in (21, 22) if wave(img(x, y))]
        return max(lit)

    well_x, well_right = 36, 36 + 132
    caught = Image(render_png(_finishing(), tmp_path / "f0.png"))
    landed = Image(render_png(_finishing(age=FINISH - FRAME), tmp_path / "f1.png"))
    after = Image(render_png(_finishing(age=FINISH + 0.45), tmp_path / "f2.png"))

    assert reach(caught) < well_right - 30, "the sweep is caught part-way across"
    assert reach(landed) >= well_right - 2, "the fill has to arrive at the end"
    assert reach(landed) > reach(caught) + 20
    well = dict(x0=well_x, x1=well_right)
    assert caught.count(emerald, **well) == 0, "no checkmark while the fill runs"
    assert landed.count(emerald, **well) == 0
    assert after.count(emerald, **well) > 20, "and then the checkmark draws in"


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


# -- review findings ------------------------------------------------------

def test_the_chip_has_room_for_whichever_code_it_is_showing(tmp_path):
    """During a notice the chip shows the *old* code first: it must fit."""
    model = _model("notice", lang="zh-hant", to="en", age=0.05)
    assert model.prev_lang == "zh-hant"
    img = Image(render_png(model, tmp_path / "swap.png"))
    right = [img(x, y) for y in range(img.height)
             for x in range(img.width - 3, img.width)]
    assert all(max(p[:3]) < 90 for p in right), "the leaving code overran the pill"


def test_a_notice_without_an_arrow_still_rises_in(tmp_path):
    """Text the daemon sends without an arrow gets the same rise as the rest."""
    def top_row(age):
        img = Image(render_png(_model("notice", text="Model reloaded", age=age),
                               tmp_path / f"n-{age}.png"))
        # inside the well band only: rows 0 and 43 carry the pill's own border
        lit = [y for y in range(8, img.height - 8) for x in range(36, 36 + 132)
               if max(img(x, y)[:3]) > 55]
        assert lit, f"nothing drawn at age {age}"
        return min(lit)

    rising, settled = top_row(0.03), top_row(0.4)      # ~2.8 px below its home
    assert rising - settled >= 2, f"text sat at {rising} then {settled}: no rise"


# -- eliding the error message (once per frame, at 30 fps) ----------------

def _elide_one_char_at_a_time(ctx, text, room, size):
    """The original algorithm, kept here as the reference output to match."""
    text = text[:200]
    if od.text_width(ctx, text, size) <= room:
        return text
    while text and od.text_width(ctx, text + "\u2026", size) > room:
        text = text[:-1]
    return text + "\u2026"


class CountingContext:
    """A real context that counts the glyph measurements made through it."""

    def __init__(self, ctx):
        self._ctx = ctx
        self.extents = 0

    def text_extents(self, text):
        self.extents += 1
        return self._ctx.text_extents(text)

    def __getattr__(self, name):
        return getattr(self._ctx, name)


def _context():
    return cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1))


@pytest.mark.parametrize("text", [
    "", "x", "ok", "microphone is busy", "x" * 22, "x" * 23, "x" * 300,
    "\u00e5\u00e4\u00f6 mikrofonen \u00e4r upptagen just nu, f\u00f6rs\u00f6k igen",
    "WWWWWWWWWWWWWWWWWWWWWWWWWWWWWW", "iiiiiiiiiiiiiiiiiiiiiiiiiiiiii",
])
def test_elide_renders_exactly_what_the_original_algorithm_did(text):
    """Faster, and pixel-for-pixel the same string - including the edges: no
    room at all, room for the ellipsis alone, and room to spare."""
    ctx = _context()
    for size in (11.0, 22.0):
        for room in (0.0, 1.0, 5.0, 40.0, 121.0, 160.0, 4000.0):
            assert od._elide(ctx, text, room, size) == \
                _elide_one_char_at_a_time(ctx, text, room, size), (text, room, size)


def test_eliding_a_long_message_measures_a_bounded_number_of_glyphs():
    """The error well holds ~22 characters at 11 px, and `last_error` can be
    hundreds; truncating one character at a time re-measured the whole prefix
    each round - ~20k text_extents calls, every frame, 30 times a second for
    the whole 2 s hold. The cost must follow the room, not the message."""
    ctx = CountingContext(_context())
    text = "x" * 300
    assert od._elide(ctx, text, 150.0, od.ERROR_SIZE).endswith("\u2026")
    assert ctx.extents < 60, f"{ctx.extents} glyph measurements for a 300-char error"


def test_eliding_costs_the_same_whether_the_message_is_long_or_very_long():
    """No dependence on the part of the message that cannot be shown."""
    def measured(n):
        ctx = CountingContext(_context())
        od._elide(ctx, "x" * n, 150.0, od.ERROR_SIZE)
        return ctx.extents

    assert measured(300) == measured(60) > 0


def test_bars_are_drawn_taller_than_the_model_says_but_never_past_the_well():
    """The owner asked for a taller wave once the shape was right.

    BAR_LIFT scales pixels, not the model, so silence still rests where it did
    and a crest clips at the top instead of the middle of the wave squaring off.
    """
    from voice.ui import overlay_draw as d

    assert d.BAR_LIFT > 1.0
    well = d.WELL_H
    lifted = lambda h: min(well, h * well * d.BAR_LIFT)
    assert lifted(0.5) == pytest.approx(0.5 * well * d.BAR_LIFT)
    assert lifted(0.5) > 0.5 * well, "ordinary speech must gain height"
    assert lifted(1.0) == pytest.approx(well), "a crest is clamped to the well"
    assert lifted(0.0) == 0.0, "silence is untouched"


def test_a_done_state_with_its_own_words_draws_those_words(tmp_path):
    """"Inserted" is a lie for a copy-only insertion, so the daemon sends the
    wording with the state - and it has to fit inside the well."""
    from voice.ui.overlay_draw import natural_width

    model = _model("done", age=0.95)
    model.text = "Copied · Ctrl+V"
    img = Image(render_png(model, tmp_path / "done-copied.png"))
    plain = Image(render_png(_model("done", age=0.95), tmp_path / "done-plain.png"))
    label = dict(x0=36 + 60, x1=36 + 132)
    lit = lambda p: p[3] > 60 and max(p[:3]) > 90
    assert img.count(lit, **label) > 20                      # something was drawn
    assert img.count(lit, **label) != plain.count(lit, **label)   # and it is not "Inserted"
    # Inside the pill: a label wider than the well would be clipped by the capsule.
    assert img.count(lit, x0=natural_width(model) - 4, x1=natural_width(model)) == 0


def test_a_long_done_label_is_cut_down_rather_than_run_through_the_timer():
    """The daemon's wording carries a user-configurable chord.

    "Copied · Ctrl+Shift+V" measured 156 px against a 132 px well and drew
    straight over the elapsed time to its right.
    """
    from voice.ui.overlay_draw import done_label

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 600, 100)
    ctx = cairo.Context(surface)
    room = od.WELL_W

    for text in ("Inserted", "Copied · Ctrl+V", "Use Ctrl+Shift+V"):
        assert done_label(ctx, text, 1.0) == text, f"{text!r} should not be touched"

    monster = "Use Ctrl+Shift+Alt+Super+Backspace"
    cut = done_label(ctx, monster, 1.0)
    assert cut != monster and cut.endswith("…")
    # Measured the way it is drawn, tracking included - no tolerance needed.
    cut_w = od.text_width(ctx, cut, od.LABEL_SIZE, od.LABEL_TRACK)
    assert cut_w <= room
    assert cut_w < od.text_width(ctx, monster, od.LABEL_SIZE, od.LABEL_TRACK)
