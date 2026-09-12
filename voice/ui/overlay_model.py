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
#: A checkmark that carries words stays up longer - there is something to read,
#: and 1.2 s was not enough time to read it and act on it.
DONE_TEXT_HOLD = 2.4
NOTICE_TTL = 2.0
ERROR_HOLD = 2.0

#: Motion timings, straight from the design.
BREATH_PERIOD = 2.4          # recording dot, ease-in-out, infinite
COLLAPSE = 0.2               # bars melting into the track when transcribing starts

#: The transcribing fill is *progress*, not an indeterminate loop. It used to
#: grow over 2.6 s, fade out and start again from zero, and what the owner saw
#: was a bar that gives up half-way across - a screenshot of a real
#: transcription catches it stopped in the middle of the track.
#:
#: It now advances quickly and then creeps, on the shape every indeterminate
#: progress bar uses: `SWEEP_CEILING * (1 - exp(-age / SWEEP_TAU))`. It is
#: monotonic by construction, so there is no restart to see, and it cannot
#: reach the end of the track on its own - the end belongs to `finish_fill`,
#: which is the one thing that means the transcript exists.
#:
#: SWEEP_TAU 0.9 s is set by how long a transcription actually takes here, not
#: by how long one could take. A sentence is over in one to three seconds on
#: any machine we target, so that is the window the fill has to cross: 64% of
#: the ceiling at one second, 85% at two, 92% at three. The first tuning used
#: 2.2 s, which put the bar at 55% at two seconds - the owner read exactly
#: that as "it stops halfway", because for their transcriptions it does.
#: A minute-long conversion still creeps: the remaining distance stays over a
#: pixel of the 132 px track until ~4.5 s, and the bar never goes backwards.
#:
#: SWEEP_CEILING is 1.0: the fill reaches the end of the track and rests
#: there. Holding it short of the end was a principle nobody asked for - "go
#: all the way to the right, almost close to the counter where the bar
#: finished" is the requirement, and a bar that stops short reads as stalled
#: whatever the reasoning behind it. `finish_fill` still runs it to the end
#: from wherever it stands when a fast transcription ends early.
#: How coarsely the fill advances when the desktop asks for reduced motion:
#: eight visible steps across the track instead of a continuous slide.
REDUCED_STEP = 0.125

SWEEP_TAU = 0.7
SWEEP_CEILING = 1.0

#: The fill stops short of the end of the track on its own (SWEEP_CEILING),
#: so without this the checkmark would replace a half-drawn line - an operation
#: abandoned rather than one completed. When the transcription ends the fill
#: instead runs from wherever it stands to a full track, and only then does
#: the checkmark begin. 0.2 s is the same beat as COLLAPSE, the gesture that
#: melted the bars into this track in the first place, and - not a
#: coincidence, it is why this length was chosen - exactly the DASH_DELAY the
#: checkmark already waits before its stroke starts. The well was blank for
#: those 200 ms (the pop-in scales a zero-length path); the fill now spends
#: them arriving. So the completion costs the ending nothing at all: no hold
#: to give back, no gap where neither the line nor the checkmark is on screen,
#: and the checkmark, its label and the 1.2 s hold keep their original timing.
FINISH = 0.2
POP_IN = 0.35                # checkmark container pop
DASH_DELAY, DASH_DUR = 0.2, 0.5      # checkmark stroke drawing itself in
LABEL_DELAY, LABEL_DUR = 0.5, 0.4    # "Inserted" rising in
RISE = 0.3                   # notice text rising in
SWAP = 0.3                   # language badge swapping out and back in

#: How far the completion pushes the checkmark's presentation back: nothing,
#: as long as the fill lands inside the lead-in the stroke already waits out.
DONE_DELAY = max(0.0, FINISH - DASH_DELAY)

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
#: painted over it: the newest slot keeps all of its height, the oldest 65%.
#: Anything stronger and the older half of the wave stops being readable.
TAPER = 0.35

#: A recording bar never sits fully flat: the design animates each bar between
#: 22% and 100% of its tapered height. Measured against a real pill, 0.22 left
#: the quiet between words reading as hairlines, so the resting height is 25%.
#: It is what stops the wave collapsing to a row of dots between syllables.
BAR_FLOOR = 0.25

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
PEAK_HALF_LIFE = 2.0                  # seconds to halve the reference
PEAK_FLOOR = 0.25                     # a quiet talker is still full scale
NOISE = 0.06                          # below this there is nothing to draw

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
        #: Whether the notice on screen is a language switch. `notice` was
        #: built for the toggle and the badge reads `prev_lang` for any notice
        #: at all, so once the busy answers started reusing the state, every
        #: ignored keypress also flipped the chip and claimed a switch that had
        #: not happened. Set by the switch itself, consumed by the next notice.
        self.notice_swaps_language = False
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
        #: Where the fill stood when the transcription finished, as (width,
        #: opacity), and when that was: the start of its run to 100%. `None`
        #: whenever there is no completion to run, which is what keeps an
        #: error, a reduced-motion pill and a repeated `done` from animating.
        self._finish_from: tuple[float, float] | None = None
        self._finish_at = self._now

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
        """`level` as a fraction of the loudest thing heard lately."""
        decayed = 0.5 ** (max(0.0, self._now - self._peak_at) / PEAK_HALF_LIFE)
        reference = PEAK_FLOOR + (self._peak - PEAK_FLOOR) * decayed
        self._peak = max(level, reference)
        self._peak_at = self._now
        span = self._peak - NOISE
        return _clamp01((level - NOISE) / span) if span > 0 else 0.0

    def finish_fill(self, now: float | None = None) -> float:
        """Run the fill to the end of its track from wherever it stands now.

        The daemon asks for this the moment the transcription lands, because
        on a desktop that cannot give the pill a surface refusing the keyboard
        the pill is unmapped for the paste chord - and unmapping it mid-sweep
        is exactly the half-drawn line this whole thing exists to stop. The
        checkmark is not due yet: it belongs to the insertion that follows.

        Returns the seconds the fill still needs, so the caller can wait for
        it before taking the pill off screen. Asking again while it is already
        running says how much is left rather than starting it over, and asking
        when there is no sweep on screen - not transcribing, or a pill with
        animations turned off - is nothing and says so.
        """
        now = self._clock() if now is None else now
        if self.state != "transcribing" or self.reduced_motion:
            return 0.0
        if self._finish_from is None:
            self._finish_from = self._sweep_at(max(0.0, now - self._state_since))
            self._finish_at = now
        return max(0.0, self._finish_at + FINISH - now)

    def set_language(self, code: str) -> None:
        """Record a language switch; the badge and any notice read from here."""
        code = (code or "").strip() or self.lang
        if code != self.lang:
            self.prev_lang = self.lang
        self.lang = code

    def set_state(self, state: str, text: str | None = None,
                  now: float | None = None, swaps_language: bool = False) -> None:
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
        if state == "notice":
            # Told by the sender, not inferred from a pending `set_language`.
            # Inferring it leaked twice: the settings window changes the
            # language with no notice at all, leaving the guess armed for
            # whatever notice came next, and notices land on top of each other
            # freely. Set after the repeat check above, never before it, so a
            # notice refused as a repeat leaves the chip exactly as it was.
            self.notice_swaps_language = swaps_language
        self._enter(state, text, now, reset_counter=True)

    def _enter(self, state: str, text: str | None, now: float,
               reset_counter: bool, resuming: bool = False) -> None:
        # A transcription that ends runs its fill to the end before the
        # checkmark. The completion belongs to the fill rather than to the
        # state: `transcribing`, `done` and `notice` are the three states that
        # show it or remember it, so it survives them and nothing else. One
        # already in flight - the daemon asks for it early when it is going to
        # unmap the pill for a paste chord - is inherited, never restarted
        # from further back.
        if state not in ("transcribing", "done", "notice"):
            self._finish_from = None
        elif state == "transcribing" and not resuming:
            # A transcription that is starting has nothing to complete: a fill
            # carried in from the last one renders a motionless full bar where
            # the progress curve belongs. The one exception is the notice
            # expiring back into the state it covered, which is the only way
            # into `transcribing` with a completion genuinely in flight - and
            # `resuming` is that path itself, in `tick`, rather than "a notice
            # is on screen". Asked the second way, a `set_state("transcribing")`
            # under a notice inherited the fill too: a retry started inside the
            # 1.2 s `done` hold, or under a notice raised between the daemon
            # asking for the completion and `done` arriving, drew a full,
            # motionless bar for the whole of that retry.
            self._finish_from = None
        elif (state == "done" and self.state == "transcribing"
                and self._finish_from is None and not self.reduced_motion):
            # Read the sweep where it stands *now*, while the transcribing
            # clock is still the current one.
            self._finish_from = self._sweep_at(max(0.0, now - self._state_since))
            self._finish_at = now
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
            self._enter(state, text, now, reset_counter=False, resuming=True)
            self._state_since = now - age   # resume, do not restart, its hold
            return
        hold = {"done": DONE_TEXT_HOLD if self.text else DONE_HOLD,
                "error": ERROR_HOLD}.get(self.state)
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

    def _sweep_at(self, age: float) -> tuple[float, float]:
        """The progress fill at `age` seconds into the transcribing state.

        Opacity is a constant: the line used to fade out at the end of every
        loop, and a fill that dims is a fill that is giving up. It stays lit
        from the first frame to the checkmark.
        """
        # -expm1(-x) is 1 - exp(-x) without the cancellation at small x, which
        # is exactly where the fill moves fastest.
        width = SWEEP_CEILING * -math.expm1(-max(0.0, age) / SWEEP_TAU)
        if self.reduced_motion:
            # Reduced motion is about decoration - the breathing dot, the
            # waveform's swing - not about information. A progress bar that
            # refuses to progress reads as broken, and did: this used to
            # return a fixed 0.35, so the fill sat at a third of the track
            # for the whole conversion whatever the tuning said. It still
            # advances here, in coarse steps so the repaint is not continuous.
            width = min(1.0, round(width / REDUCED_STEP) * REDUCED_STEP)
        return width, 1.0

    @property
    def sweep(self) -> tuple[float, float]:
        """The fill line as (width 0..1, opacity 0..1).

        While transcribing this is the progress curve, which approaches
        SWEEP_CEILING and stops there. Once the transcription is over it is
        the completion: the same line carried from wherever it stood to a full
        track, eased so a long remainder still reads as quick and a short one
        does not crawl. Only the completion reaches the end of the track,
        which is what makes a full bar mean "done".
        """
        if self._finish_from is not None:
            start_w, start_a = self._finish_from
            done = ease_out(_clamp01(self._finish_age / FINISH)) if FINISH > 0 else 1.0
            return start_w + (1.0 - start_w) * done, start_a + (1.0 - start_a) * done
        return self._sweep_at(self.state_age)

    @property
    def _finish_age(self) -> float:
        """Seconds since the fill was set running to the end of its track."""
        return max(0.0, self._now - self._finish_at)

    @property
    def finishing(self) -> bool:
        """True while the fill is still running to the end of the track.

        What the well shows: the progress line, not the `done` well. The
        checkmark's container may already be popping in underneath - it scales
        a zero-length path and draws nothing until `check_draw` starts, which
        is the moment the fill lands.
        """
        return self._finish_from is not None and self._finish_age < FINISH

    @property
    def _done_age(self) -> float:
        """Seconds the checkmark's own presentation has been running.

        The fill has to land before the stroke starts, and the stroke already
        waits DASH_DELAY, so a completion no longer than that delay is free:
        it fills a window the checkmark was going to spend invisible anyway
        and the presentation is not moved at all (`DONE_DELAY` is 0). Only a
        completion longer than the delay pushes the checkmark back, and then
        by just the excess - never the whole of it.
        """
        start = self._state_since
        if self._finish_from is not None:
            start = max(start, self._finish_at + DONE_DELAY)
        return max(0.0, self._now - start)

    @property
    def check_pop(self) -> tuple[float, float]:
        """Checkmark container as (scale, opacity) during its .35 s pop-in."""
        progress = ease_out(_clamp01(self._done_age / POP_IN))
        scale = 0.6 + (1.08 - 0.6) * (progress / 0.6) if progress < 0.6 else \
            1.08 + (1.0 - 1.08) * ((progress - 0.6) / 0.4)
        return scale, progress

    @property
    def check_draw(self) -> float:
        """0..1 of the checkmark stroke that has been drawn in."""
        return _stage(self._done_age, DASH_DELAY, DASH_DUR)

    @property
    def label_rise(self) -> float:
        """0..1 progress of the "Inserted" label rising into place."""
        return _stage(self._done_age, LABEL_DELAY, LABEL_DUR)

    @property
    def notice_rise(self) -> float:
        return _stage(self.state_age, 0.0, RISE)

    @property
    def badge_swap(self) -> float:
        """0..1 of the badge's out-and-back-in swap while a notice is showing.

        1.0 - settled, no animation - for a notice that is not a language
        switch. The busy answers reuse this state, and a chip that flips out
        and back for "Still working" is claiming something happened to the
        language when nothing did.
        """
        if self.state != "notice" or not self.notice_swaps_language:
            return 1.0
        return _clamp01(self.state_age / SWAP)

    @property
    def ttl(self) -> float:
        """The notice's remaining lifetime, 1 down to 0 over exactly NOTICE_TTL."""
        return _clamp01(1.0 - self.state_age / NOTICE_TTL)
