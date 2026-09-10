"""Cairo rendering of the recording pill.

A pixel recreation of the owner's design
(`.superpowers/sdd/2026-09-10-phase1-dictation-core/pill-design/README.md`):
44 px capsule, 8 px status dot, 132x24 centre well, tabular counter, language
badge. Every length below is in design pixels and is multiplied by
`height / PILL_H`, so the same code draws the real pill and the 2x screenshots
the tests inspect. `overlay_model` owns the timing of the animations; this
module owns their shapes.
"""
from __future__ import annotations

import math

import cairo

from voice.ui.overlay_model import OverlayModel

# -- palette (Penelope tokens from the design) ----------------------------
FILL = (15 / 255, 16 / 255, 20 / 255, 0.88)
BORDER = (1.0, 1.0, 1.0, 0.09)
INSET_HIGHLIGHT = (1.0, 1.0, 1.0, 0.05)
TEXT = (0xf4 / 255, 0xf4 / 255, 0xf5 / 255)
DIM = (0xa1 / 255, 0xa1 / 255, 0xaa / 255)
DIMMER = (0x71 / 255, 0x71 / 255, 0x7a / 255)
CORAL = (0xe0 / 255, 0x6c / 255, 0x75 / 255)        # recording
CYAN = (0x06 / 255, 0xb6 / 255, 0xd4 / 255)         # transcribing
EMERALD = (0x10 / 255, 0xb9 / 255, 0x81 / 255)      # done
VIOLET = (0xa8 / 255, 0x55 / 255, 0xf7 / 255)       # waveform, left end
TEAL = (0x22 / 255, 0xd3 / 255, 0xee / 255)         # waveform, right end
TRACK = (1.0, 1.0, 1.0, 0.08)
BADGE_BORDER = (1.0, 1.0, 1.0, 0.10)
BADGE_BORDER_BRIGHT = (1.0, 1.0, 1.0, 0.18)
AMBER = (0.941, 0.706, 0.353)                       # error, not in the design

# -- geometry, in design pixels (the pill is 44 tall) ---------------------
PILL_H = 44.0
PAD_L, PAD_R, GAP = 14.0, 16.0, 14.0
DOT = 8.0
WELL_W, WELL_H = 132.0, 24.0
BAR_GAP, BAR_RADIUS = 3.0, 2.0
COUNTER_SIZE, COUNTER_MIN, COUNTER_TRACK = 12.0, 32.0, 0.02
BADGE_SIZE, BADGE_TRACK = 10.0, 0.12
BADGE_PAD_X, BADGE_PAD_Y, BADGE_RADIUS = 5.0, 2.0, 3.0
LABEL_SIZE, LABEL_TRACK, LABEL_GAP = 11.0, 0.04, 10.0
NOTICE_SIZE, NOTICE_TRACK, NOTICE_GAP = 12.0, 0.06, 8.0
CHECK_BOX, CHECK_STROKE = 18.0, 2.2 * 18.0 / 24.0   # 18px box drawn from a 24 viewBox
LINE_H, LINE_RADIUS = 2.0, 1.0                      # transcribing track and fill
TTL_INSET, TTL_H = 18.0, 1.0
RISE_PX = 5.0                                       # translateY of the rise-in
SWAP_PX = 6.0                                       # translateY of the badge swap
ERROR_SIZE = 11.0

#: The design asks for JetBrains Mono. Fall back to whatever real monospace
#: the system has - a proportional fallback would wreck the tabular counter.
FONT_STACK = ("JetBrains Mono", "DejaVu Sans Mono", "Liberation Mono",
              "Noto Sans Mono", "monospace")
_FAMILY: str | None = None
_PROBE: cairo.Context | None = None


def _probe_context() -> cairo.Context:
    """A 1x1 scratch context for measuring text, reused across frames."""
    global _PROBE
    if _PROBE is None:
        _PROBE = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1))
    return _PROBE


def font_family() -> str:
    """First family in the stack that is actually monospaced, measured once."""
    global _FAMILY
    if _FAMILY is None:
        probe = _probe_context()
        for family in FONT_STACK:
            probe.select_font_face(family, cairo.FONT_SLANT_NORMAL,
                                   cairo.FONT_WEIGHT_NORMAL)
            probe.set_font_size(32)
            narrow = probe.text_extents("i").x_advance
            wide = probe.text_extents("M").x_advance
            if narrow and abs(narrow - wide) < 0.01:
                _FAMILY = family
                break
        else:
            _FAMILY = "monospace"
    return _FAMILY


# -- text -----------------------------------------------------------------

def _face(ctx: cairo.Context, size: float) -> None:
    ctx.select_font_face(font_family(), cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    ctx.set_font_size(size)


def text_width(ctx: cairo.Context, text: str, size: float, tracking: float = 0.0) -> float:
    """Advance width of `text` including CSS-style letter-spacing (in em)."""
    _face(ctx, size)
    return sum(ctx.text_extents(ch).x_advance + tracking * size for ch in text)


def _show_text(ctx: cairo.Context, x: float, baseline: float, text: str,
               size: float, tracking: float = 0.0) -> float:
    """Draw `text` one glyph at a time so letter-spacing is honoured."""
    _face(ctx, size)
    for ch in text:
        ctx.move_to(x, baseline)
        ctx.show_text(ch)
        x += ctx.text_extents(ch).x_advance + tracking * size
    return x


def _cap_height(ctx: cairo.Context, size: float) -> float:
    """Height of a digit, for centring text on a line rather than on a baseline."""
    _face(ctx, size)
    return ctx.text_extents("0").height


def _centred_text(ctx, cx, cy, text, size, tracking, colour, alpha=1.0) -> None:
    width = text_width(ctx, text, size, tracking)
    ctx.set_source_rgba(*colour, alpha)
    _show_text(ctx, cx - width / 2, cy + _cap_height(ctx, size) / 2, text, size, tracking)


# -- layout ---------------------------------------------------------------

def _counter_width(ctx: cairo.Context, model: OverlayModel, s: float) -> float:
    return max(COUNTER_MIN * s, text_width(ctx, model.elapsed_text, COUNTER_SIZE * s, COUNTER_TRACK))


def _badge_codes(model: OverlayModel) -> tuple[str, ...]:
    """Codes the chip can show right now - during a notice it shows both."""
    if model.state == "notice":
        return (model.prev_lang.upper(), model.badge_text)
    return (model.badge_text,)


def _badge_width(ctx: cairo.Context, model: OverlayModel, s: float) -> float:
    widest = max(text_width(ctx, code, BADGE_SIZE * s, BADGE_TRACK)
                 for code in _badge_codes(model))
    return widest + 2 * BADGE_PAD_X * s + 2 * s


def natural_width(model: OverlayModel, height: float = PILL_H) -> int:
    """Width the pill needs for this model's counter and badge, in pixels."""
    s = height / PILL_H
    ctx = _probe_context()
    return int(math.ceil(
        (PAD_L + DOT + GAP + WELL_W + GAP + GAP + PAD_R) * s
        + _counter_width(ctx, model, s) + _badge_width(ctx, model, s)))


# -- pieces ---------------------------------------------------------------

def _capsule_path(ctx: cairo.Context, x: float, y: float, w: float, h: float) -> None:
    r = h / 2.0
    ctx.new_path()
    ctx.arc(x + r, y + r, r, math.pi / 2, math.pi * 1.5)
    ctx.arc(x + w - r, y + r, r, math.pi * 1.5, math.pi / 2)
    ctx.close_path()


def _rounded_rect(ctx: cairo.Context, x: float, y: float, w: float, h: float, r: float) -> None:
    r = min(r, w / 2, h / 2)
    ctx.new_path()
    ctx.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    ctx.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    ctx.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    ctx.arc(x + r, y + r, r, math.pi, math.pi * 1.5)
    ctx.close_path()


def _mix(a, b, t: float):
    return tuple(p + (q - p) * t for p, q in zip(a, b))


def _gradient(x0: float, x1: float, alpha: float = 1.0) -> cairo.LinearGradient:
    """The waveform gradient, violet to teal, spanning the drawn width."""
    grad = cairo.LinearGradient(x0, 0, x1, 0)
    grad.add_color_stop_rgba(0, *VIOLET, alpha)
    grad.add_color_stop_rgba(1, *TEAL, alpha)
    return grad


def _dot(ctx: cairo.Context, cx: float, cy: float, s: float, model: OverlayModel) -> None:
    """The status dot: coral and breathing while recording, static otherwise."""
    colour, alpha, scale = {
        "recording": (CORAL, 1.0, 1.0),
        "transcribing": (CYAN, 0.8, 1.0),
        "done": (EMERALD, 0.9, 1.0),
        "notice": (DIMMER, 0.7, 1.0),
        "error": (AMBER, 0.9, 1.0),
    }.get(model.state, (DIMMER, 0.7, 1.0))
    radius = DOT / 2 * s
    if model.state == "recording":
        breath = model.breath
        alpha = 0.55 + 0.45 * breath
        scale = 0.85 + 0.15 * breath
        if breath > 0.01:                      # box-shadow 0 0 10px 1px at the peak
            halo = cairo.RadialGradient(cx, cy, radius * scale, cx, cy, radius * scale + 10 * s)
            halo.add_color_stop_rgba(0, *CORAL, 0.45 * breath)
            halo.add_color_stop_rgba(1, *CORAL, 0.0)
            ctx.set_source(halo)
            ctx.arc(cx, cy, radius * scale + 10 * s, 0, 2 * math.pi)
            ctx.fill()
    ctx.set_source_rgba(*colour, alpha)
    ctx.arc(cx, cy, radius * scale, 0, 2 * math.pi)
    ctx.fill()


def _bars(ctx, model: OverlayModel, x: float, cy: float, s: float) -> None:
    heights = model.bar_heights
    n = len(heights)
    gap = BAR_GAP * s
    bar_w = (WELL_W * s - gap * (n - 1)) / n
    for i, height in enumerate(heights):
        px = height * WELL_H * s
        if px < 0.35:
            continue
        colour = _mix(VIOLET, TEAL, i / (n - 1))
        ctx.set_source_rgba(*colour, 0.92)
        _rounded_rect(ctx, x + i * (bar_w + gap), cy - px / 2, bar_w, px,
                      min(BAR_RADIUS * s, px / 2))
        ctx.fill()


def _progress_line(ctx, model: OverlayModel, x: float, cy: float, s: float) -> None:
    """Faint full-width track with the gradient fill sweeping across it."""
    height = LINE_H * s
    ctx.set_source_rgba(*TRACK)
    _rounded_rect(ctx, x, cy - height / 2, WELL_W * s, height, LINE_RADIUS * s)
    ctx.fill()
    width, alpha = model.sweep
    drawn = width * WELL_W * s
    if drawn < 0.5 or alpha <= 0.01:
        return
    ctx.set_source(_gradient(x, x + drawn, alpha))
    _rounded_rect(ctx, x, cy - height / 2, drawn, height, LINE_RADIUS * s)
    ctx.fill()


def _check_path(ctx, x: float, y: float, s: float, progress: float) -> None:
    """lucide check `M5 12.5l4.5 4.5L19 7`, drawn in as far as `progress`."""
    unit = CHECK_BOX * s / 24.0
    p0 = (x + 5 * unit, y + 12.5 * unit)
    p1 = (x + 9.5 * unit, y + 17 * unit)
    p2 = (x + 19 * unit, y + 7 * unit)
    first, second = math.dist(p0, p1), math.dist(p1, p2)
    drawn = (first + second) * progress
    ctx.new_path()
    ctx.move_to(*p0)
    if drawn <= first:
        t = drawn / first
        ctx.line_to(p0[0] + (p1[0] - p0[0]) * t, p0[1] + (p1[1] - p0[1]) * t)
    else:
        t = (drawn - first) / second
        ctx.line_to(*p1)
        ctx.line_to(p1[0] + (p2[0] - p1[0]) * t, p1[1] + (p2[1] - p1[1]) * t)


def _done_well(ctx, model: OverlayModel, x: float, cy: float, s: float) -> None:
    """Checkmark popping in, then the label rising in beside it."""
    label = "Inserted"
    label_w = text_width(ctx, label, LABEL_SIZE * s, LABEL_TRACK)
    box = CHECK_BOX * s
    group_w = box + LABEL_GAP * s + label_w
    left = x + (WELL_W * s - group_w) / 2
    scale, opacity = model.check_pop
    progress = model.check_draw
    if opacity > 0.01 and progress > 0.0:
        ctx.save()
        ctx.translate(left + box / 2, cy)           # pop-in scales about the centre
        ctx.scale(scale, scale)
        ctx.translate(-box / 2, -box / 2)
        ctx.set_source_rgba(*EMERALD, opacity)
        ctx.set_line_width(CHECK_STROKE * s)
        ctx.set_line_cap(cairo.LINE_CAP_ROUND)
        ctx.set_line_join(cairo.LINE_JOIN_ROUND)
        _check_path(ctx, 0, 0, s, progress)
        ctx.stroke()
        ctx.restore()
    rise = model.label_rise
    if rise > 0.01:
        ctx.set_source_rgba(*DIM, rise)
        _show_text(ctx, left + box + LABEL_GAP * s,
                   cy + _cap_height(ctx, LABEL_SIZE * s) / 2 + RISE_PX * s * (1 - rise),
                   label, LABEL_SIZE * s, LABEL_TRACK)


def _notice_well(ctx, model: OverlayModel, x: float, cy: float, s: float) -> None:
    """`EN -> SV`: where we came from, dim; where we are going, bright."""
    text = model.text or f"{model.prev_lang.upper()} → {model.lang.upper()}"
    parts = [p.strip() for p in text.replace("->", "→").split("→")]
    if len(parts) != 2:
        rise = model.notice_rise
        _centred_text(ctx, x + WELL_W * s / 2, cy + RISE_PX * s * (1 - rise), text,
                      NOTICE_SIZE * s, NOTICE_TRACK, DIM, rise)
        return
    size, gap = NOTICE_SIZE * s, NOTICE_GAP * s
    pieces = [(parts[0], DIMMER), ("→", DIM), (parts[1], TEXT)]
    widths = [text_width(ctx, t, size, NOTICE_TRACK) for t, _ in pieces]
    rise = model.notice_rise
    baseline = cy + _cap_height(ctx, size) / 2 + RISE_PX * s * (1 - rise)
    pen = x + (WELL_W * s - sum(widths) - 2 * gap) / 2
    for (label, colour), width in zip(pieces, widths):
        ctx.set_source_rgba(*colour, rise)
        _show_text(ctx, pen, baseline, label, size, NOTICE_TRACK)
        pen += width + gap


def _error_well(ctx, model: OverlayModel, x: float, cy: float, s: float,
                room: float) -> None:
    """The message gets the counter's and badge's room too - they are hidden."""
    size = ERROR_SIZE * s
    text = _elide(ctx, model.text or "error", room, size)
    ctx.set_source_rgba(*AMBER, 0.95)
    _show_text(ctx, x, cy + _cap_height(ctx, size) / 2, text, size)


def _elide(ctx, text: str, room: float, size: float) -> str:
    text = text[:200]                       # no point measuring a runaway message
    if text_width(ctx, text, size) <= room:
        return text
    while text and text_width(ctx, text + "…", size) > room:
        text = text[:-1]
    return text + "…"


def _counter(ctx, model: OverlayModel, right: float, cy: float, s: float) -> None:
    colour = {"recording": TEXT, "transcribing": DIMMER,
              "done": DIM, "notice": DIMMER}.get(model.state, DIM)
    size = COUNTER_SIZE * s
    text = model.elapsed_text
    width = text_width(ctx, text, size, COUNTER_TRACK)
    ctx.set_source_rgba(*colour, 1.0)
    _show_text(ctx, right - width, cy + _cap_height(ctx, size) / 2, text, size, COUNTER_TRACK)


def _badge(ctx, model: OverlayModel, x: float, cy: float, s: float, width: float) -> None:
    """The language chip, which flips out and back in during a notice."""
    swap = model.badge_swap
    if swap < 0.5:                          # leaving: rises and fades out
        offset, alpha = -SWAP_PX * s * (swap / 0.5), 1 - swap / 0.5
    else:                                   # arriving: comes up from below
        offset, alpha = SWAP_PX * s * (1 - (swap - 0.5) / 0.5), (swap - 0.5) / 0.5
    if alpha <= 0.01:
        return
    size = BADGE_SIZE * s
    height = size + 2 * BADGE_PAD_Y * s + 2 * s
    leaving = model.state == "notice" and swap < 0.5
    text = model.prev_lang.upper() if leaving else model.badge_text
    border = BADGE_BORDER if leaving or model.state != "notice" else BADGE_BORDER_BRIGHT
    colour = DIM if leaving or model.state != "notice" else TEXT
    top = cy - height / 2 + offset
    ctx.set_line_width(s)
    ctx.set_source_rgba(*border[:3], border[3] * alpha)
    _rounded_rect(ctx, x + s / 2, top + s / 2, width - s, height - s, BADGE_RADIUS * s)
    ctx.stroke()
    ctx.set_source_rgba(*colour, alpha)
    # the chip is sized for the wider of the two codes, so centre whichever shows
    inset = (width - text_width(ctx, text, size, BADGE_TRACK)) / 2
    _show_text(ctx, x + inset, top + height / 2 + _cap_height(ctx, size) / 2,
               text, size, BADGE_TRACK)


def _ttl_hairline(ctx, model: OverlayModel, x: float, y: float, w: float, s: float) -> None:
    """The notice's remaining life, draining left to right along the bottom."""
    inset = TTL_INSET * s
    width = (w - 2 * inset) * model.ttl
    if width < 0.5:
        return
    ctx.set_source(_gradient(x + inset, x + inset + width, 0.6))
    ctx.rectangle(x + inset, y - TTL_H * s, width, TTL_H * s)
    ctx.fill()


# -- the whole pill -------------------------------------------------------

def draw(ctx: cairo.Context, width: int, height: int, model: OverlayModel) -> None:
    """Paint the pill onto `ctx`, filling a `width` x `height` surface."""
    if not model.visible:
        return
    s = height / PILL_H
    w, h = float(width), float(height)
    if h < 4 or w <= h:               # too small for a capsule; nothing to draw
        return
    cy = h / 2.0

    _capsule_path(ctx, 0, 0, w, h)
    ctx.set_source_rgba(*FILL)
    ctx.fill_preserve()
    ctx.save()
    ctx.clip()                                  # nothing may leave the capsule
    ctx.set_source_rgba(*INSET_HIGHLIGHT)       # inset 0 1px 0 highlight
    ctx.rectangle(0, 0, w, s)
    ctx.fill()

    badge_w = _badge_width(ctx, model, s)
    well_x = PAD_L * s + DOT * s + GAP * s
    counter_right = w - PAD_R * s - badge_w - GAP * s
    badge_x = w - PAD_R * s - badge_w

    _dot(ctx, PAD_L * s + DOT * s / 2, cy, s, model)
    if model.state == "recording":
        _bars(ctx, model, well_x, cy, s)
    elif model.state == "transcribing":
        _progress_line(ctx, model, well_x, cy, s)
        _bars(ctx, model, well_x, cy, s)        # still collapsing into the track
    elif model.state == "done":
        _done_well(ctx, model, well_x, cy, s)
    elif model.state == "notice":
        _notice_well(ctx, model, well_x, cy, s)
    elif model.state == "error":
        _error_well(ctx, model, well_x, cy, s, w - PAD_R * s - well_x)
    if model.state != "error":
        _counter(ctx, model, counter_right, cy, s)
        _badge(ctx, model, badge_x, cy, s, badge_w)
    if model.state == "notice":
        _ttl_hairline(ctx, model, 0, h, w, s)

    ctx.restore()
    ctx.set_line_width(s)                       # the 1 px border sits inside the edge
    ctx.set_source_rgba(*BORDER)
    _capsule_path(ctx, s / 2, s / 2, w - s, h - s)
    ctx.stroke()


def render_surface(model: OverlayModel, width: int | None = None,
                   height: int = int(PILL_H)) -> cairo.ImageSurface:
    """One frame on its own ARGB32 surface.

    The helper uploads this as a GDK texture rather than drawing into a
    `Gtk.DrawingArea` context: PyGObject can only hand a `cairo.Context` to a
    draw function when its cairo foreign-struct converter is installed
    (Debian's `python3-gi-cairo`), and this way the pill renders identically
    with or without it.
    """
    if width is None:
        width = natural_width(model, height)
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(width), int(height))
    draw(cairo.Context(surface), int(width), int(height), model)
    surface.flush()
    return surface


def render_png(model: OverlayModel, path, width: int | None = None,
               height: int = int(PILL_H)):
    """Render one frame to a PNG - used by the tests and to eyeball the design."""
    render_surface(model, width, height).write_to_png(str(path))
    return path
