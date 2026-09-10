import pytest

from voice.ui.overlay_model import (
    AMP,
    BAR_FLOOR,
    BREATH_PERIOD,
    COLLAPSE,
    DONE_HOLD,
    ERROR_HOLD,
    FRAME,
    IDLE_GRACE,
    NOTICE_TTL,
    OverlayModel,
    ease_in_out,
    ease_out,
)


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


# -- easing ---------------------------------------------------------------

@pytest.mark.parametrize("ease", [ease_out, ease_in_out])
def test_easings_run_from_zero_to_one_without_going_backwards(ease):
    assert ease(0.0) == 0.0
    assert ease(1.0) == 1.0
    assert ease(-1.0) == 0.0 and ease(2.0) == 1.0
    samples = [ease(i / 40) for i in range(41)]
    assert all(b >= a - 1e-9 for a, b in zip(samples, samples[1:]))


def test_ease_out_front_loads_its_progress():
    # cubic-bezier(.16, 1, .3, 1): most of the distance is covered early.
    assert ease_out(0.25) > 0.6
    assert ease_in_out(0.25) < 0.25


# -- states ---------------------------------------------------------------

def test_starts_hidden_and_invisible(model):
    assert model.state == "hidden"
    assert model.visible is False
    assert model.bar_heights == [0.0] * 21
    assert model.elapsed_text == "0:00"
    assert model.badge_text == "EN"


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


def test_error_holds_for_its_own_two_seconds(model, clock):
    model.set_state("error", text="no microphone")
    assert model.text == "no microphone"
    model.tick(clock.advance(ERROR_HOLD - 0.05))
    assert model.state == "error"
    model.tick(clock.advance(0.1))
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


def test_unknown_state_is_rejected(model):
    with pytest.raises(ValueError, match="pondering"):
        model.set_state("pondering")


def test_the_model_uses_its_injected_clock_by_default(model, clock):
    model.set_state("recording")        # no explicit now: reads the fake clock
    clock.advance(3.0)
    model.tick()
    assert model.elapsed_text == "0:03"


# -- language and the notice overlay --------------------------------------

def test_the_badge_shows_the_current_language(model):
    model.set_language("sv")
    assert model.badge_text == "SV"
    assert model.prev_lang == "en"


def test_setting_the_same_language_twice_keeps_the_real_previous_one(model):
    model.set_language("sv")
    model.set_language("sv")
    assert (model.prev_lang, model.lang) == ("en", "sv")


def test_an_empty_language_is_ignored(model):
    model.set_language("")
    assert model.lang == "en"


def test_a_notice_defaults_to_the_language_it_switched_from(model):
    model.set_language("sv")
    model.set_state("notice")
    assert model.text == "EN → SV"


def test_a_notice_returns_to_the_state_it_interrupted(model, clock):
    model.set_state("recording")
    model.tick(clock.advance(4.0))
    model.set_state("notice", text="EN → SV", now=clock.t)
    model.tick(clock.advance(NOTICE_TTL - 0.05))
    assert model.state == "notice"
    model.tick(clock.advance(0.1))
    assert model.state == "recording"


def test_a_notice_does_not_reset_the_recording_counter(model, clock):
    model.set_state("recording")
    model.tick(clock.advance(4.0))
    model.set_state("notice", now=clock.t)
    model.tick(clock.advance(NOTICE_TTL + 0.1))
    assert model.state == "recording"
    assert model.elapsed_text == "0:06"          # 4 s of recording plus the 2 s notice
    model.tick(clock.advance(1.0))
    assert model.elapsed_text == "0:07"          # and it keeps running


def test_a_notice_over_an_idle_pill_goes_back_to_hidden(model, clock):
    model.set_state("notice", text="EN → SV")
    assert model.visible is True
    model.tick(clock.advance(NOTICE_TTL + 0.01))
    assert model.state == "hidden" and model.visible is False


def test_the_notice_lifetime_drains_from_one_to_zero(model, clock):
    model.set_state("notice", text="EN → SV")
    assert model.ttl == 1.0
    model.tick(clock.advance(NOTICE_TTL / 2))
    assert model.ttl == pytest.approx(0.5)
    model.tick(clock.advance(NOTICE_TTL / 2 - 0.01))
    assert model.ttl == pytest.approx(0.005, abs=0.001)


def test_the_badge_swaps_out_and_back_in_during_a_notice(model, clock):
    model.set_state("notice", text="EN → SV")
    assert model.badge_swap == 0.0
    model.tick(clock.advance(0.3))
    assert model.badge_swap == pytest.approx(1.0)
    model.set_state("recording", now=clock.t)
    assert model.badge_swap == 1.0               # no swap outside a notice


# -- waveform -------------------------------------------------------------

def test_the_resting_envelope_is_the_designs_amplitude_array(model):
    model.set_state("recording")
    _fill(model, 1.0)
    assert model.bar_heights == pytest.approx(list(AMP))


def test_recording_bars_rest_at_a_fraction_of_the_envelope_when_silent(model):
    model.set_state("recording")
    _fill(model, 0.0)
    assert model.bar_heights == pytest.approx([a * BAR_FLOOR for a in AMP])


def test_push_level_feeds_the_centre_and_pushes_older_values_outward(model):
    model.set_state("recording")
    _fill(model, 0.0)
    model.push_level(1.0)
    centre = 21 // 2
    assert model.bar_heights[centre] == pytest.approx(AMP[centre])
    for _ in range(3):
        model.push_level(0.0)
    assert model.bar_heights[centre] == pytest.approx(AMP[centre] * BAR_FLOOR)
    assert max(model.bar_heights) > AMP[centre] * BAR_FLOOR      # it moved outward


def test_bars_are_mirrored_around_the_centre(model):
    model.set_state("recording")
    for lvl in (0.9, 0.2, 0.7, 0.4):
        model.push_level(lvl)
    heights = [round(h, 9) for h in model.bar_heights]
    assert heights == list(reversed(heights))


def test_levels_are_clamped_into_range(model):
    model.set_state("recording")
    model.push_level(4.2)
    model.push_level(-1.0)
    assert all(0.0 <= h <= 1.0 for h in model.bar_heights)


def test_the_waveform_holds_its_shape_while_levels_keep_arriving(model, clock):
    # Levels arrive ~8x a second and frames tick ~30x: a per-frame decay would
    # flatten the history between two chunks and lose the wave entirely.
    model.set_state("recording")
    for _ in range(40):
        model.push_level(1.0)
        for _ in range(3):
            model.tick(clock.advance(FRAME))
    assert model.bar_heights == pytest.approx(list(AMP))


def test_bars_settle_toward_the_floor_once_the_levels_stop(model, clock):
    model.set_state("recording")
    _fill(model, 1.0)
    model.tick(clock.advance(IDLE_GRACE))
    assert model.bar_heights == pytest.approx(list(AMP)), "the grace holds the shape"
    model.tick(clock.advance(FRAME))
    expected = AMP[10] * (BAR_FLOOR + (1 - BAR_FLOOR) * 0.85)
    assert model.bar_heights[10] == pytest.approx(expected, rel=1e-6)
    for _ in range(60):
        model.tick(clock.advance(FRAME))
    assert model.bar_heights == pytest.approx([a * BAR_FLOOR for a in AMP], abs=0.01)


def test_decay_is_frame_rate_independent(clock):
    a = OverlayModel(clock=clock)
    a.set_state("recording")
    _fill(a, 1.0)
    a.tick(clock.advance(IDLE_GRACE))
    a.tick(clock.advance(FRAME * 4))
    expected = AMP[10] * (BAR_FLOOR + (1 - BAR_FLOOR) * 0.85 ** 4)
    assert a.bar_heights[10] == pytest.approx(expected, rel=1e-6)


def test_a_fresh_recording_starts_its_grace_period(model, clock):
    model.set_state("recording")
    clock.advance(30.0)                 # the pill sat hidden for half a minute
    model.set_state("hidden", now=clock.t)
    model.set_state("recording", now=clock.t)
    _fill(model, 1.0)
    model.tick(clock.advance(FRAME))
    assert model.bar_heights == pytest.approx(list(AMP))


def test_bars_collapse_into_the_track_when_transcribing_starts(model, clock):
    model.set_state("recording")
    _fill(model, 1.0)
    model.set_state("transcribing", now=clock.t)
    assert model.bar_heights == pytest.approx(list(AMP)), "no floor once capture ends"
    model.tick(clock.advance(COLLAPSE / 2))
    half = model.bar_heights
    assert 0.0 < max(half) < max(AMP)
    model.tick(clock.advance(COLLAPSE / 2))
    assert max(model.bar_heights) == pytest.approx(0.0, abs=1e-9)


def test_hiding_clears_the_waveform(model, clock):
    model.set_state("recording")
    _fill(model, 1.0)
    model.set_state("hidden", now=clock.t)
    assert model.bar_heights == [0.0] * 21
    assert model.text is None


def test_bar_count_is_configurable_and_resamples_the_envelope(clock):
    m = OverlayModel(bars=9, clock=clock)
    m.set_state("recording")
    _fill(m, 1.0)
    heights = m.bar_heights
    assert len(heights) == 9
    assert heights[0] == pytest.approx(AMP[0])
    assert heights[-1] == pytest.approx(AMP[-1])
    assert heights[2] == pytest.approx(AMP[5])           # resampled, not truncated
    assert heights == list(reversed(heights))


def test_a_waveform_needs_bars(clock):
    with pytest.raises(ValueError, match="at least 3"):
        OverlayModel(bars=2, clock=clock)


# -- motion ---------------------------------------------------------------

def test_the_recording_dot_breathes_once_per_period(model, clock):
    model.set_state("recording")
    assert model.breath == pytest.approx(0.0, abs=1e-9)
    model.tick(clock.advance(BREATH_PERIOD / 2))
    assert model.breath == pytest.approx(1.0)
    model.tick(clock.advance(BREATH_PERIOD / 2))
    assert model.breath == pytest.approx(0.0, abs=1e-9)


def test_the_transcribing_fill_grows_then_fades_and_repeats(model, clock):
    model.set_state("transcribing")
    assert model.sweep == (0.0, 1.0)
    model.tick(clock.advance(1.1))
    grown, alpha = model.sweep
    assert 0.0 < grown < 1.0 and alpha == 1.0
    model.tick(clock.advance(1.1))
    assert model.sweep[0] > grown
    model.tick(clock.advance(0.2))               # inside the trailing fade
    assert model.sweep[0] == pytest.approx(1.0) and model.sweep[1] < 1.0
    model.tick(clock.advance(0.3))               # and round again
    assert model.sweep[0] < 0.5


def test_the_checkmark_pops_then_draws_then_labels(model, clock):
    model.set_state("done")
    assert model.check_draw == 0.0 and model.label_rise == 0.0
    model.tick(clock.advance(0.35))
    scale, opacity = model.check_pop
    assert opacity == pytest.approx(1.0) and scale == pytest.approx(1.0)
    assert 0.0 < model.check_draw < 1.0
    assert model.label_rise == 0.0               # still waiting for its .5 s cue
    model.tick(clock.advance(0.4))
    assert model.check_draw == 1.0
    assert 0.0 < model.label_rise < 1.0
    model.tick(clock.advance(0.25))
    assert model.label_rise == 1.0


def test_the_checkmark_overshoots_on_the_way_in(model, clock):
    model.set_state("done")
    peaks = []
    for _ in range(12):
        model.tick(clock.advance(0.02))
        peaks.append(model.check_pop[0])
    assert max(peaks) > 1.0, "the pop-in scales past 1 before settling"
    model.tick(clock.advance(0.5))
    assert model.check_pop[0] == pytest.approx(1.0)


def test_reduced_motion_stills_the_animations_and_lowers_the_waveform(clock):
    m = OverlayModel(reduced_motion=True, clock=clock)
    m.set_state("recording")
    _fill(m, 1.0)
    assert m.bar_heights == pytest.approx([a * 0.6 for a in AMP])
    still = m.breath
    m.tick(clock.advance(BREATH_PERIOD / 2))
    assert m.breath == still, "the dot must not breathe"
    m.set_state("transcribing", now=clock.t)
    fill = m.sweep
    m.tick(clock.advance(1.3))
    assert m.sweep == fill, "the fill line must not sweep"


# -- review findings ------------------------------------------------------

def test_a_notice_restores_the_error_it_interrupted_intact(model, clock):
    # The message and the remaining 2 s of the error must both survive the notice.
    model.set_state("error", text="pw-record: no such target")
    model.tick(clock.advance(0.5))
    model.set_state("notice", text="EN → SV", now=clock.t)
    model.tick(clock.advance(NOTICE_TTL + 0.01))
    assert model.state == "error"
    assert model.text == "pw-record: no such target"
    model.tick(clock.advance(ERROR_HOLD - 0.6))     # 1.4 s of the 1.5 s left
    assert model.state == "error", "the message was still owed its time"
    model.tick(clock.advance(0.2))
    assert model.state == "hidden", "the error's own 2 s must not restart"


def test_an_interrupted_state_resumes_with_the_time_it_had_left(model, clock):
    model.set_state("done")
    model.tick(clock.advance(0.2))                  # 1.0 s of the done hold left
    model.set_state("notice", now=clock.t)
    model.tick(clock.advance(NOTICE_TTL + 0.01))
    assert model.state == "done"
    model.tick(clock.advance(DONE_HOLD - 0.3))
    assert model.state == "done", "the hold resumed rather than restarting"
    model.tick(clock.advance(0.2))
    assert model.state == "hidden"
