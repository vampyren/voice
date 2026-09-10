import pytest

from voice.ui.overlay_model import DONE_HOLD, ERROR_HOLD, FRAME, OverlayModel


class Clock:
    """An injected clock: the model never reads real time in these tests."""

    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def model(clock):
    return OverlayModel(clock=clock)


def _fill(model, level=1.0, times=40):
    for _ in range(times):
        model.push_level(level)


def test_starts_hidden_and_invisible(model):
    assert model.state == "hidden"
    assert model.visible is False
    assert model.bar_heights == [0.0] * 28
    assert model.elapsed_text == "0:00"


def test_recording_is_visible_and_counts_up(model, clock):
    model.set_state("recording")
    assert model.visible is True
    assert model.elapsed_text == "0:00"
    model.tick(clock.advance(2.4))
    assert model.elapsed_text == "0:02"
    model.tick(clock.advance(62.0))
    assert model.elapsed_text == "1:04"


def test_elapsed_freezes_when_recording_ends(model, clock):
    model.set_state("recording")
    model.tick(clock.advance(5.0))
    model.set_state("transcribing", now=clock.t)
    model.tick(clock.advance(9.0))
    assert model.elapsed_text == "0:05"


def test_a_second_recording_restarts_the_counter(model, clock):
    model.set_state("recording")
    model.tick(clock.advance(7.0))
    model.set_state("done", now=clock.t)
    model.set_state("recording", now=clock.advance(1.0))
    assert model.elapsed_text == "0:00"


def test_done_hides_itself_after_the_hold(model, clock):
    model.set_state("recording")
    model.set_state("done", now=clock.advance(1.0))
    model.tick(clock.advance(DONE_HOLD - 0.05))
    assert model.state == "done" and model.visible is True
    model.tick(clock.advance(0.1))
    assert model.state == "hidden" and model.visible is False


def test_error_holds_much_longer_than_done(model, clock):
    model.set_state("error", text="no microphone")
    assert model.text == "no microphone"
    model.tick(clock.advance(DONE_HOLD + 0.2))
    assert model.state == "error"          # not the 0.6 s done timer
    model.tick(clock.advance(ERROR_HOLD))
    assert model.state == "hidden"
    assert model.visible is False


def test_transcribing_never_times_out_on_its_own(model, clock):
    model.set_state("transcribing")
    model.tick(clock.advance(30.0))
    assert model.state == "transcribing" and model.visible is True


def test_state_age_tracks_time_in_the_current_state(model, clock):
    model.set_state("done")
    model.tick(clock.advance(0.3))
    assert model.state_age == pytest.approx(0.3)


def test_push_level_feeds_the_centre_and_pushes_older_values_outward(model):
    model.set_state("recording")
    model.push_level(1.0)
    heights = model.bar_heights
    centre = len(heights) // 2
    assert heights[centre] > 0.0
    assert heights[0] == 0.0 and heights[-1] == 0.0     # nothing has reached the ends yet
    for _ in range(3):
        model.push_level(0.0)
    heights = model.bar_heights
    assert heights[centre] == 0.0                        # the loud sample moved outward
    assert max(heights) > 0.0


def test_bars_are_mirrored_around_the_centre(model):
    model.set_state("recording")
    for lvl in (0.9, 0.2, 0.7, 0.4):
        model.push_level(lvl)
    heights = model.bar_heights
    assert heights == list(reversed(heights))


def test_taper_envelope_dims_the_ends_for_a_flat_input(model):
    model.set_state("recording")
    _fill(model, 1.0)
    heights = model.bar_heights
    centre = len(heights) // 2
    assert heights[centre] == pytest.approx(1.0, abs=0.02)
    assert heights[0] < 0.5 * heights[centre]
    half = heights[centre:]
    assert all(a >= b - 1e-9 for a, b in zip(half, half[1:]))   # never rises toward the end
    assert all(0.0 <= h <= 1.0 for h in heights)


def test_bars_decay_toward_zero_while_recording(model, clock):
    model.set_state("recording")
    _fill(model, 1.0)
    before = model.bar_heights
    model.tick(clock.advance(FRAME))
    one_frame = model.bar_heights
    assert one_frame[14] == pytest.approx(before[14] * 0.85, rel=1e-6)
    for _ in range(60):
        model.tick(clock.advance(FRAME))
    assert max(model.bar_heights) < 0.01


def test_decay_is_frame_rate_independent(clock):
    a = OverlayModel(clock=clock)
    a.set_state("recording")
    _fill(a, 1.0)
    a.tick(clock.advance(FRAME * 4))
    assert max(a.bar_heights) == pytest.approx(max(_flat(1.0)) * 0.85 ** 4, rel=1e-6)


def _flat(level):
    m = OverlayModel()
    m.set_state("recording", now=0.0)
    _fill(m, level)
    return m.bar_heights


def test_bars_freeze_while_transcribing(model, clock):
    model.set_state("recording")
    _fill(model, 1.0)
    frozen = model.bar_heights
    model.set_state("transcribing", now=clock.t)
    model.tick(clock.advance(2.0))
    assert model.bar_heights == frozen


def test_hiding_clears_the_waveform(model, clock):
    model.set_state("recording")
    _fill(model, 1.0)
    model.set_state("hidden", now=clock.t)
    assert model.bar_heights == [0.0] * 28
    assert model.text is None


def test_levels_are_clamped_into_range(model):
    model.set_state("recording")
    model.push_level(4.2)
    model.push_level(-1.0)
    assert all(0.0 <= h <= 1.0 for h in model.bar_heights)
    assert max(model.bar_heights) > 0.0


def test_bar_count_is_configurable(clock):
    m = OverlayModel(bars=9, clock=clock)
    m.set_state("recording")
    _fill(m, 1.0)
    heights = m.bar_heights
    assert len(heights) == 9
    assert heights == list(reversed(heights))
    assert heights[4] > heights[0]


def test_unknown_state_is_rejected(model):
    with pytest.raises(ValueError, match="pondering"):
        model.set_state("pondering")


def test_the_model_uses_its_injected_clock_by_default(model, clock):
    model.set_state("recording")        # no explicit now: reads the fake clock
    clock.advance(3.0)
    model.tick()
    assert model.elapsed_text == "0:03"
