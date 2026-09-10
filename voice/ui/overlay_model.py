"""State, waveform and motion model for the recording pill.

Pure Python on purpose: no GTK, no cairo, no real clock. The helper process
feeds it JSON events and a frame timer, the drawing code reads it, and the
tests drive it with an injected clock, so every timing rule below is testable
without a display.

Geometry, colour and motion follow the owner's high-fidelity design
(`.superpowers/sdd/2026-09-10-phase1-dictation-core/pill-design/README.md`);
this module owns the *timing* half of it - every animation is exposed as a
0..1 progress derived from the injected clock - and `overlay_draw` owns the
shapes.
"""
from __future__ import annotations

import math
import time
from collections import deque
from typing import Callable

#: Every state the daemon can put the pill in. `notice` is an overlay state:
#: it remembers what was on screen and goes back to it when it expires.
STATES = ("hidden", "recording", "transcribing", "done", "notice", "error")

#: One frame at the helper's ~30 fps redraw rate.
FRAME = 1.0 / 30.0

#: How long the waveform holds its shape after the last level before it starts
#: to settle. Levels arrive once per captured chunk - about eight times a
#: second - while the helper redraws thirty times a second, so a decay applied
#: on every frame would flatten the history between two chunks and leave a
#: smooth hump instead of a wave. The stored history is therefore left alone
#: while audio is flowing, and fades only once the source has gone quiet on us
#: (capture ended, pw-record died, the helper was starved).
IDLE_GRACE = 0.2

#: State lifetimes (seconds). `done` auto-dismisses, `notice` returns to
#: whatever was on screen before it, `error` hides itself.
DONE_HOLD = 1.2
NOTICE_TTL = 2.0
ERROR_HOLD = 2.0

#: Motion timings, straight from the design.
BREATH_PERIOD = 2.4          # recording dot, ease-in-out, infinite
SWEEP_PERIOD = 2.6           # transcribing fill line, indeterminate loop
SWEEP_HOLD = 0.85            # fraction of the loop spent growing; then it fades
COLLAPSE = 0.2               # bars melting into the track when transcribing starts
POP_IN = 0.35                # checkmark container pop
DASH_DELAY, DASH_DUR = 0.2, 0.5      # checkmark stroke drawing itself in
LABEL_DELAY, LABEL_DUR = 0.5, 0.4    # "Inserted" rising in
RISE = 0.3                   # notice text rising in
SWAP = 0.3                   # language badge swapping out and back in

#: Waveform: 21 bars fed from a rolling history of recent levels. The newest
#: sample is the centre bar and older ones move outward, one bar per chunk, so
#: the pill draws a wave travelling out of the microphone instead of a fixed
#: shape that breathes. 21 bars is 11 distinct moments: bar `i` and bar
#: `20 - i` are the same slot, mirrored, which is how the design's artboards
#: read - we have one broadband RMS per chunk rather than the per-band levels
#: the design assumes, so a mirror is the honest way to fill both halves.
BARS = 21

#: The wave still tapers toward the ends of the well, as the design draws it,
#: but the taper is a mild weight on the *history* rather than a silhouette
#: painted over it: the newest slot keeps all of its height, the oldest 85%.
#: Ten of the twenty-one bars live in that outer half and they are real audio,
#: not decoration - a deeper taper was most of why the wave read as flat.
TAPER = 0.15

#: A recording bar never sits fully flat: the design animates each bar between
#: 22% and 100% of its tapered height, and 22% is what we use. The floor and
#: the taper compose, so the shortest resting bar is `(1 - TAPER) * BAR_FLOOR`
#: of the 24 px well - 4.5 px, a visible dot rather than a hairline - while
#: the audio gets the other 78% of the well to move in.
BAR_FLOOR = 0.22

#: Automatic gain. Drawn against full scale, ordinary speech gave a wave a few
#: pixels tall, so each level is measured against a decaying peak of the recent
#: ones instead: whatever the microphone and the distance, talking fills the
#: well and the quiet between words still reads as quiet.
#:
#: The numbers are in the units `level.rms_level` produces, *not* raw RMS - it
#: has already applied a square root and a 1.25 gain, so a quiet room lands
#: near 0.04 and speech between 0.2 (a distant mic) and 0.8 (a close one).
#: NOISE is what the wave ignores altogether, and the reference never falls
#: below PEAK_FLOOR, so a silent room cannot amplify its own hiss into a
#: waveform; between them they put speech at 60-100% of the well.
#:
#: The half-life is what a *single* loud chunk costs everything said after it.
#: At 2 s one emphatic word held the reference near full scale for some six
#: seconds and drew the rest of the sentence at half height; 1.2 s lets go of
#: it inside a sentence while still spanning several syllables, so the wave
#: is measured against how loudly you are talking now, not a moment ago.
PEAK_HALF_LIFE = 1.2                  # seconds to halve the reference
PEAK_FLOOR = 0.25                     # a quiet talker is still full scale
NOISE = 0.06                          # below this there is nothing to draw

#: Exponent on the normalised level, applied last. Straight-line scaling put
#: the body of a syllable - everything that is loud but not the crest - in the
#: middle of the well, which is the shape the owner read as flat. An exponent
#: below 1 lifts the middle without moving either end (0 stays 0, 1 stays 1),
#: so silence still rests on the floor and only the crest still clips.
GAIN_CURVE = 0.75

#: A jump larger than this means the helper was stalled (or the machine slept);
#: fade to silence rather than raising the decay to an absurd power.
MAX_STEP = 2.0


def _bezier(x: float, x1: float, y1: float, x2: float, y2: float) -> float:
    """Evaluate a CSS cubic-bezier(x1, y1, x2, y2) easing at progress `x`."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    t = x
    for _ in range(8):                      # Newton: the curve is well behaved
        u = 1 - t
        cx = 3 * u * u * t * x1 + 3 * u * t * t * x2 + t ** 3
        dx = 3 * u * u * (x1) + 6 * u * t * (x2 - x1) + 3 * t * t * (1 - x2)
        if abs(cx - x) < 1e-6 or dx == 0:
            break
        t -= (cx - x) / dx
    t = min(1.0, max(0.0, t))
    u = 1 - t
    return 3 * u * u * t * y1 + 3 * u * t * t * y2 + t ** 3


def ease_out(t: float) -> float:
    """The design's cubic-bezier(.16, 1, .3, 1) - everything settles with it."""
    return _bezier(t, .16, 1., .3, 1.)


def ease_in_out(t: float) -> float:
    """cubic-bezier(.4, 0, .2, 1), used by the transcribing fill line."""
    return _bezier(t, .4, 0., .2, 1.)


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def _stage(age: float, delay: float, duration: float) -> float:
    """Eased 0..1 progress of an animation that starts `delay` after entry."""
    return ease_out(_clamp01((age - delay) / duration)) if duration > 0 else 1.0


class OverlayModel:
    """What the pill shows, and when it stops showing it.

    The waveform is a history, not a silhouette: `push_level` puts the newest
    sample in the middle and the previous samples ripple outward toward both
    ends, so neighbouring bars are different moments of audio and the shape
    reads as a wave. The only fixed weight left is a mild taper toward the
    ends. The design calls for per-band levels; we have one broadband RMS per
    chunk, so both halves show the same history, mirrored around the centre
    (see the report for the deviation).
    """

    def __init__(self, bars: int = BARS, decay: float = 0.85,
                 lang: str = "en", reduced_motion: bool = False,
                 clock: Callable[[], float] = time.monotonic):
        if bars < 3:
            raise ValueError(f"a waveform needs at least 3 bars, got {bars}")
        self.bars = bars
        self.decay = decay
        self.reduced_motion = reduced_motion
        self.lang = lang
        self.prev_lang = lang
        self._clock = clock
        self._half = (bars + 1) // 2          # history slots: centre out to one end
        self._history = deque([0.0] * self._half, maxlen=self._half)
        self._taper = [self._weight(k) for k in range(self._half)]
        self.state = "hidden"
        self.text: str | None = None
        self._now = clock()
        #: What a notice interrupted: its state, its text, and how long it had
        #: already been showing. A hold counts *visible* time, so the covered
        #: state resumes with the time it had left rather than losing it (an
        #: error and a notice both last 2 s: on wall-clock time an interrupted
        #: error could never come back) or starting over.
        self._return: tuple[str, str | None, float] = ("hidden", None, 0.0)
        self._state_since = self._now
        self._last_tick = self._now
        self._last_push = self._now
        self._started_at = self._now
        self._elapsed = 0.0
        #: The loudest recent level, and when it was set: full scale for the bars.
        self._peak = PEAK_FLOOR
        self._peak_at = self._now

    # -- geometry ---------------------------------------------------------

    def _weight(self, slot: int) -> float:
        """End-taper for history `slot`: 1 at the centre, `1 - TAPER` at the ends."""
        span = self._half - 1
        return 1.0 - TAPER * (slot / span) if span > 0 else 1.0

    def _slot(self, index: int) -> int:
        distance = abs(index - (self.bars - 1) / 2.0)
        return min(int(distance), self._half - 1)

    # -- input ------------------------------------------------------------

    def push_level(self, level: float) -> None:
        """Feed one microphone level (0..1) into the centre of the waveform.

        Every level shifts the whole history one place outward, so the bars
        are a true record of the last `bars // 2` chunks rather than a decayed
        hump - that is what makes the shape read as a wave.
        """
        level = min(1.0, max(0.0, float(level)))
        self._history.appendleft(self._gain(level))   # bounded: drops the oldest
        self._last_push = self._now

    def _gain(self, level: float) -> float:
        """`level` as a fraction of the loudest thing heard lately, curved."""
        decayed = 0.5 ** (max(0.0, self._now - self._peak_at) / PEAK_HALF_LIFE)
        reference = PEAK_FLOOR + (self._peak - PEAK_FLOOR) * decayed
        self._peak = max(level, reference)
        self._peak_at = self._now
        span = self._peak - NOISE
        if span <= 0:
            return 0.0
        return _clamp01((level - NOISE) / span) ** GAIN_CURVE

    def set_language(self, code: str) -> None:
        """Record a language switch; the badge and any notice read from here."""
        code = (code or "").strip() or self.lang
        if code != self.lang:
            self.prev_lang = self.lang
        self.lang = code

    def set_state(self, state: str, text: str | None = None,
                  now: float | None = None) -> None:
        if state not in STATES:
            raise ValueError(f"unknown overlay state: {state!r}")
        now = self._clock() if now is None else now
        if state == "notice":
            if self.state != "notice":
                self._return = (self.state, self.text, self.state_age)
            text = text or f"{self.prev_lang.upper()} → {self.lang.upper()}"
        if state == self.state and text == self.text:
            # The same thing said twice is not a new event: re-entering would
            # replay the pop-in and hand `done` a fresh 1.2 s hold every time.
            # New text under the same state is news, and does re-enter.
            return
        self._enter(state, text, now, reset_counter=True)

    def _enter(self, state: str, text: str | None, now: float,
               reset_counter: bool) -> None:
        self._now = self._last_tick = self._state_since = now
        if state == "recording" and self.state != "recording":
            if reset_counter:                  # a notice restores, it never restarts
                self._started_at = now
                self._elapsed = 0.0
            self._last_push = now
        if state == "hidden":
            self._history = deque([0.0] * self._half, maxlen=self._half)
            self._elapsed = 0.0
        self.state = state
        self.text = text

    def tick(self, now: float | None = None) -> None:
        """Advance one frame: fade the bars, move the clock, expire the state."""
        now = self._clock() if now is None else now
        dt = min(MAX_STEP, max(0.0, now - self._last_tick))
        self._now = self._last_tick = now
        if self._counting:
            self._elapsed = max(0.0, now - self._started_at)
        # Transcribing freezes the last shape on screen (it collapses into the
        # progress track instead); every other state lets it settle back toward
        # the centre line once the levels stop arriving.
        idle = now - self._last_push
        fading = min(dt, max(0.0, idle - IDLE_GRACE))
        if self.state not in ("transcribing", "notice") and fading > 0.0:
            factor = self.decay ** (fading / FRAME)
            self._history = deque((h * factor for h in self._history),
                                  maxlen=self._half)
        if self.state == "notice" and self.state_age >= NOTICE_TTL:
            state, text, age = self._return
            self._enter(state, text, now, reset_counter=False)
            self._state_since = now - age   # resume, do not restart, its hold
            return
        hold = {"done": DONE_HOLD, "error": ERROR_HOLD}.get(self.state)
        if hold is not None and self.state_age >= hold:
            self.set_state("hidden", now=now)

    # -- output -----------------------------------------------------------

    @property
    def visible(self) -> bool:
        return self.state != "hidden"

    @property
    def _counting(self) -> bool:
        """Capture is still running - a notice on top of it does not pause it."""
        return self.state == "recording" or (
            self.state == "notice" and self._return[0] == "recording")

    @property
    def state_age(self) -> float:
        """Seconds spent in the current state, as of the last tick."""
        return max(0.0, self._now - self._state_since)

    @property
    def elapsed(self) -> float:
        return self._elapsed

    @property
    def elapsed_text(self) -> str:
        whole = int(self._elapsed)
        return f"{whole // 60}:{whole % 60:02d}"

    @property
    def badge_text(self) -> str:
        return self.lang.upper()

    @property
    def bar_heights(self) -> list[float]:
        """Bar scale factors 0..1: the level history, mirrored and tapered.

        Bar `i` shows history slot `|i - centre|`, so the centre bar is the
        newest chunk and the ends are the oldest - the height of one bar says
        nothing about the height of the next, which is what makes it a wave.
        While recording the bars rest at `BAR_FLOOR` and rise from there with
        the level, exactly like the design's idle animation.
        """
        live = self.state == "recording"
        amplitude = 0.6 if self.reduced_motion else 1.0
        heights = []
        for i in range(self.bars):
            slot = self._slot(i)
            level = self._history[slot]
            if live:
                level = BAR_FLOOR + (1.0 - BAR_FLOOR) * level
            heights.append(min(1.0, self._taper[slot] * level * amplitude))
        if self.state == "transcribing":
            remaining = 1.0 - ease_out(_clamp01(self.state_age / COLLAPSE))
            heights = [h * remaining for h in heights]
        return heights

    # -- motion (pure functions of the injected clock) ---------------------

    @property
    def breath(self) -> float:
        """0..1 breathing phase of the recording dot: 0 at rest, 1 at the peak."""
        if self.reduced_motion:
            return 0.5
        phase = (self.state_age % BREATH_PERIOD) / BREATH_PERIOD
        return (1.0 - math.cos(2 * math.pi * phase)) / 2

    @property
    def sweep(self) -> tuple[float, float]:
        """Transcribing fill line as (width 0..1, opacity 0..1)."""
        if self.reduced_motion:
            return 0.35, 1.0
        phase = (self.state_age % SWEEP_PERIOD) / SWEEP_PERIOD
        width = ease_in_out(_clamp01(phase / SWEEP_HOLD))
        fade = 1.0 if phase <= SWEEP_HOLD else 1.0 - (phase - SWEEP_HOLD) / (1 - SWEEP_HOLD)
        return width, _clamp01(fade)

    @property
    def check_pop(self) -> tuple[float, float]:
        """Checkmark container as (scale, opacity) during its .35 s pop-in."""
        progress = ease_out(_clamp01(self.state_age / POP_IN))
        scale = 0.6 + (1.08 - 0.6) * (progress / 0.6) if progress < 0.6 else \
            1.08 + (1.0 - 1.08) * ((progress - 0.6) / 0.4)
        return scale, progress

    @property
    def check_draw(self) -> float:
        """0..1 of the checkmark stroke that has been drawn in."""
        return _stage(self.state_age, DASH_DELAY, DASH_DUR)

    @property
    def label_rise(self) -> float:
        """0..1 progress of the "Inserted" label rising into place."""
        return _stage(self.state_age, LABEL_DELAY, LABEL_DUR)

    @property
    def notice_rise(self) -> float:
        return _stage(self.state_age, 0.0, RISE)

    @property
    def badge_swap(self) -> float:
        """0..1 of the badge's out-and-back-in swap while a notice is showing."""
        if self.state != "notice":
            return 1.0
        return _clamp01(self.state_age / SWAP)

    @property
    def ttl(self) -> float:
        """The notice's remaining lifetime, 1 down to 0 over exactly NOTICE_TTL."""
        return _clamp01(1.0 - self.state_age / NOTICE_TTL)
