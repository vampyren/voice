"""State and waveform model for the recording overlay.

Pure Python on purpose: no GTK, no cairo, no real clock. The helper process
feeds it JSON events and a frame timer, the drawing code reads it, and the
tests drive it with an injected clock, so every timing rule below is testable
without a display.
"""
from __future__ import annotations

import math
import time
from typing import Callable

#: Every state the daemon can put the capsule in.
STATES = ("hidden", "recording", "transcribing", "done", "error")

#: One frame at the helper's ~30 fps redraw rate. Decay is expressed per frame
#: but applied per elapsed second, so a slow or jittery frame still fades by
#: the same visible amount.
FRAME = 1.0 / 30.0

#: How long the finished-checkmark and the error message stay on screen.
DONE_HOLD = 0.6
ERROR_HOLD = 2.0

#: Waveform envelope: the fraction of full height the outermost bar may reach,
#: and the curve that gets it there. Together they give the reference's shape -
#: tall in the middle, tapering to a whisper at both ends.
EDGE = 0.10
TAPER = 0.75

#: A jump larger than this means the helper was stalled (or the machine slept);
#: fade to silence rather than raising the decay to an absurd power.
MAX_STEP = 2.0


class OverlayModel:
    """What the capsule shows, and when it stops showing it.

    The waveform is symmetric: `push_level` puts the newest sample in the
    middle and the previous samples ripple outward toward both ends, so the
    shape reads as a wave travelling out of the microphone.
    """

    def __init__(self, bars: int = 28, decay: float = 0.85,
                 clock: Callable[[], float] = time.monotonic):
        if bars < 3:
            raise ValueError(f"a waveform needs at least 3 bars, got {bars}")
        self.bars = bars
        self.decay = decay
        self._clock = clock
        self._half = (bars + 1) // 2          # history slots: centre out to one end
        self._history = [0.0] * self._half
        self._envelope = [self._taper(i) for i in range(bars)]
        self.state = "hidden"
        self.text: str | None = None
        self._now = clock()
        self._state_since = self._now
        self._last_tick = self._now
        self._started_at = self._now
        self._elapsed = 0.0

    # -- geometry ---------------------------------------------------------

    def _taper(self, index: int) -> float:
        """Envelope weight for bar `index`, 1.0 in the middle down to EDGE."""
        distance = abs(index - (self.bars - 1) / 2.0)
        curve = math.cos(math.pi / 2 * min(1.0, distance / self._half)) ** TAPER
        return EDGE + (1.0 - EDGE) * curve

    def _slot(self, index: int) -> int:
        distance = abs(index - (self.bars - 1) / 2.0)
        return min(int(distance), self._half - 1)

    # -- input ------------------------------------------------------------

    def push_level(self, level: float) -> None:
        """Feed one microphone level (0..1) into the centre of the waveform."""
        level = min(1.0, max(0.0, float(level)))
        self._history.insert(0, level)
        self._history.pop()

    def set_state(self, state: str, text: str | None = None,
                  now: float | None = None) -> None:
        if state not in STATES:
            raise ValueError(f"unknown overlay state: {state!r}")
        now = self._clock() if now is None else now
        self._now = self._last_tick = self._state_since = now
        if state == "recording" and self.state != "recording":
            self._started_at = now
            self._elapsed = 0.0
        if state == "hidden":
            self._history = [0.0] * self._half
            self._elapsed = 0.0
        self.state = state
        self.text = text

    def tick(self, now: float | None = None) -> None:
        """Advance one frame: fade the bars, move the clock, expire the state."""
        now = self._clock() if now is None else now
        dt = min(MAX_STEP, max(0.0, now - self._last_tick))
        self._now = self._last_tick = now
        if self.state == "recording":
            self._elapsed = max(0.0, now - self._started_at)
        # Transcribing freezes the last shape on screen; every other state
        # lets it settle back toward the centre line.
        if self.state != "transcribing" and dt > 0.0:
            factor = self.decay ** (dt / FRAME)
            self._history = [h * factor for h in self._history]
        hold = {"done": DONE_HOLD, "error": ERROR_HOLD}.get(self.state)
        if hold is not None and self.state_age >= hold:
            self.set_state("hidden", now=now)

    # -- output -----------------------------------------------------------

    @property
    def visible(self) -> bool:
        return self.state != "hidden"

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
    def bar_heights(self) -> list[float]:
        """Bar heights 0..1, mirrored around the centre and tapered at the ends."""
        return [min(1.0, self._history[self._slot(i)] * self._envelope[i])
                for i in range(self.bars)]
