import pytest

from voice.ui.overlay_model import (
    BAR_FLOOR,
    BREATH_PERIOD,
    COLLAPSE,
    DASH_DELAY,
    DASH_DUR,
    DONE_DELAY,
    DONE_HOLD,
    ERROR_HOLD,
    FINISH,
    FRAME,
    IDLE_GRACE,
    LABEL_DELAY,
    LABEL_DUR,
    NOTICE_TTL,
    POP_IN,
    SWEEP_CEILING,
    SWEEP_TAU,
    OverlayModel,
    TAPER,
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


def taper(bars=21):
    """The end-taper weight of each bar: 1 at the centre, 1 - TAPER at the ends."""
    half = (bars + 1) // 2
    return [1.0 - TAPER * (min(int(abs(i - (bars - 1) / 2.0)), half - 1) / (half - 1))
            for i in range(bars)]


def profile(level=1.0, bars=21, amplitude=1.0):
    """Bar heights for a history holding `level` in every slot."""
    return [min(1.0, t * level * amplitude) for t in taper(bars)]


# -- easing ---------------------------------------------------------------

def test_the_easing_runs_from_zero_to_one_without_going_backwards():
    assert ease_out(0.0) == 0.0
    assert ease_out(1.0) == 1.0
    assert ease_out(-1.0) == 0.0 and ease_out(2.0) == 1.0
    samples = [ease_out(i / 40) for i in range(41)]
    assert all(b >= a - 1e-9 for a, b in zip(samples, samples[1:]))


def test_ease_out_front_loads_its_progress():
    # cubic-bezier(.16, 1, .3, 1): most of the distance is covered early.
    assert ease_out(0.25) > 0.6


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

def test_the_taper_runs_from_full_at_the_centre_to_65_percent_at_the_ends(model):
    model.set_state("recording")
    _fill(model, 1.0)
    heights = model.bar_heights
    assert heights[10] == pytest.approx(1.0)                # newest sample
    assert heights[0] == pytest.approx(1.0 - TAPER)         # oldest, left end
    assert heights[20] == pytest.approx(1.0 - TAPER)        # oldest, right end
    assert heights[5] == pytest.approx(1.0 - TAPER / 2)     # halfway out


def test_a_steady_level_tapers_smoothly_instead_of_drawing_a_fixed_silhouette(model):
    """The shape must come from the audio, not from a hard-coded silhouette.

    The old model multiplied every bar by a fixed jagged amplitude array, so a
    perfectly steady level still drew peaks and troughs at fixed indices and
    the whole wave could only breathe as one shape. A steady level is a steady
    wave: from the centre outward the heights may only fall away.
    """
    model.set_state("recording")
    _fill(model, 1.0)
    heights = model.bar_heights
    centre_out = [heights[10 - k] for k in range(11)]
    assert all(b <= a + 1e-9 for a, b in zip(centre_out, centre_out[1:])), centre_out
    assert heights == pytest.approx(profile(1.0))


def test_recording_bars_rest_at_a_fraction_of_the_taper_when_silent(model):
    model.set_state("recording")
    _fill(model, 0.0)
    assert model.bar_heights == pytest.approx(profile(BAR_FLOOR))


def test_push_level_feeds_the_centre_and_pushes_older_values_outward(model):
    model.set_state("recording")
    _fill(model, 0.0)
    model.push_level(1.0)
    centre = 21 // 2
    assert model.bar_heights[centre] == pytest.approx(1.0)
    for _ in range(3):
        model.push_level(0.0)
    assert model.bar_heights[centre] == pytest.approx(BAR_FLOOR)
    assert max(model.bar_heights) > BAR_FLOOR                   # it moved outward


def test_a_loud_syllable_travels_one_bar_outward_per_chunk(model):
    """The newest sample is the centre bar; each chunk moves it one bar out."""
    model.set_state("recording")
    _fill(model, 0.0)
    model.push_level(1.0)
    for step in range(4):
        heights = model.bar_heights
        assert heights.index(max(heights)) == 10 - step, heights
        model.push_level(0.0)


def test_two_bars_are_not_locked_in_one_ratio(model):
    """A silhouette scaled by one loudness holds every bar in a fixed ratio to
    every other; a real waveform reshapes itself with each chunk."""
    model.set_state("recording")
    for level in (0.35, 0.62, 0.48, 0.71, 0.15, 0.55):
        model.push_level(level)

    def ratio():
        heights = model.bar_heights
        return heights[9] / heights[7]

    before = ratio()
    model.push_level(0.9)
    after_loud = ratio()
    assert after_loud != pytest.approx(before, rel=0.05)
    model.push_level(0.1)
    assert ratio() != pytest.approx(after_loud, rel=0.05)


def test_loud_and_quiet_syllables_draw_a_wave_not_a_single_hump(model):
    """Alternating syllables must leave more than one crest in the well."""
    model.set_state("recording")
    _fill(model, 0.0)
    for level in (0.8, 0.1, 0.75, 0.12, 0.7):
        model.push_level(level)
    half = model.bar_heights[10:]              # centre out to the right end
    crests = [k for k in range(1, len(half) - 1)
              if half[k] > half[k - 1] and half[k] > half[k + 1]]
    assert len(crests) >= 2, half
    assert max(half) > 3 * min(half), half     # and the troughs really are quiet


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
    assert model.bar_heights == pytest.approx(profile(1.0))


def test_bars_settle_toward_the_floor_once_the_levels_stop(model, clock):
    model.set_state("recording")
    _fill(model, 1.0)
    model.tick(clock.advance(IDLE_GRACE))
    assert model.bar_heights == pytest.approx(profile(1.0)), "the grace holds the shape"
    model.tick(clock.advance(FRAME))
    expected = BAR_FLOOR + (1 - BAR_FLOOR) * 0.85
    assert model.bar_heights[10] == pytest.approx(expected, rel=1e-6)
    for _ in range(60):
        model.tick(clock.advance(FRAME))
    assert model.bar_heights == pytest.approx(profile(BAR_FLOOR), abs=0.01)


def test_decay_is_frame_rate_independent(clock):
    a = OverlayModel(clock=clock)
    a.set_state("recording")
    _fill(a, 1.0)
    a.tick(clock.advance(IDLE_GRACE))
    a.tick(clock.advance(FRAME * 4))
    expected = BAR_FLOOR + (1 - BAR_FLOOR) * 0.85 ** 4
    assert a.bar_heights[10] == pytest.approx(expected, rel=1e-6)


def test_a_fresh_recording_starts_its_grace_period(model, clock):
    model.set_state("recording")
    clock.advance(30.0)                 # the pill sat hidden for half a minute
    model.set_state("hidden", now=clock.t)
    model.set_state("recording", now=clock.t)
    _fill(model, 1.0)
    model.tick(clock.advance(FRAME))
    assert model.bar_heights == pytest.approx(profile(1.0))


def test_bars_collapse_into_the_track_when_transcribing_starts(model, clock):
    model.set_state("recording")
    _fill(model, 1.0)
    model.set_state("transcribing", now=clock.t)
    assert model.bar_heights == pytest.approx(profile(1.0)), "no floor once capture ends"
    model.tick(clock.advance(COLLAPSE / 2))
    half = model.bar_heights
    assert 0.0 < max(half) < 1.0
    model.tick(clock.advance(COLLAPSE / 2))
    assert max(model.bar_heights) == pytest.approx(0.0, abs=1e-9)


def test_hiding_clears_the_waveform(model, clock):
    model.set_state("recording")
    _fill(model, 1.0)
    model.set_state("hidden", now=clock.t)
    assert model.bar_heights == [0.0] * 21
    assert model.text is None


def test_bar_count_is_configurable_and_the_taper_stretches_to_fit(clock):
    m = OverlayModel(bars=9, clock=clock)
    m.set_state("recording")
    _fill(m, 1.0)
    heights = m.bar_heights
    assert len(heights) == 9
    assert heights == pytest.approx(profile(1.0, bars=9))
    assert heights[4] == pytest.approx(1.0)              # centre is still newest
    assert heights[0] == pytest.approx(1.0 - TAPER)      # and the ends still taper
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


def test_the_transcribing_fill_reads_as_progress(model, clock):
    """Quick at first, then a creep - and never a step backwards.

    The owner watched the old indeterminate loop give up half-way across and
    start again. Twelve seconds at the helper's frame rate: every frame is at
    least as far along as the one before it, the opacity never fades, and the
    fill is still short of the end of the track.
    """
    model.set_state("transcribing")
    assert model.sweep == (0.0, 1.0)
    widths = [0.0]
    for _ in range(int(12 / FRAME)):
        model.tick(clock.advance(FRAME))
        width, alpha = model.sweep
        assert alpha == 1.0, "the fill never fades out any more"
        widths.append(width)
    assert widths == sorted(widths), "the fill never goes backwards"
    assert widths[-1] > widths[0], "and it does move"
    one_second = widths[int(1 / FRAME)]
    assert one_second > 0.3, "a third of the track inside the first second"
    two_seconds = widths[int(2 / FRAME)]
    assert two_seconds > widths[-1] - two_seconds, \
        "the first two seconds cover more ground than the next ten"
    assert widths[-1] < SWEEP_CEILING, "it approaches the ceiling, never reaches it"


def test_the_transcribing_fill_never_restarts(model, clock):
    """The defect itself: the fill used to fall back to zero every 2.6 s."""
    model.set_state("transcribing")
    last = 0.0
    for _ in range(60):
        model.tick(clock.advance(0.5))           # half a minute, 0.5 s at a time
        width = model.sweep[0]
        assert width >= last, "the fill restarted"
        last = width


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
    assert m.bar_heights == pytest.approx(profile(1.0, amplitude=0.6))
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


# -- the same state twice -------------------------------------------------
def test_repeating_a_state_does_not_restart_it(model, clock):
    """The daemon can say the same thing twice (`done` on the injection and
    again on the idle that follows). Restarting the hold each time would keep
    the checkmark on screen indefinitely and replay its pop-in."""
    model.set_state("done")
    model.tick(clock.advance(DONE_HOLD * 0.9))
    age = model.state_age
    model.set_state("done", now=clock.t)
    assert model.state_age == age                    # the hold kept running
    model.tick(clock.advance(DONE_HOLD * 0.2))
    assert model.state == "hidden"                   # and expired on time


def test_repeating_recording_does_not_restart_the_counter(model, clock):
    model.set_state("recording")
    model.tick(clock.advance(5.0))
    assert model.elapsed == pytest.approx(5.0)
    model.set_state("recording", now=clock.t)
    model.tick(clock.advance(1.0))
    assert model.elapsed == pytest.approx(6.0)       # not back to 1.0


def test_a_repeated_state_with_new_text_is_still_shown(model, clock):
    """Same state, different message: the second error is news, not a repeat."""
    model.set_state("error", text="pw-record died")
    model.tick(clock.advance(1.0))
    model.set_state("error", text="no microphone", now=clock.t)
    assert model.text == "no microphone"
    assert model.state_age == 0.0                    # its own two seconds


# -- automatic gain: real speech is nowhere near full scale -------------------
#: One second of speech-like broadband RMS from a desk microphone: syllables
#: peaking around 0.2, gaps down at 0.05. Straight into the well this drew a
#: wave a few pixels tall - the owner's "the wave is too thin".
SPEECH = [0.06, 0.11, 0.19, 0.24, 0.21, 0.13, 0.07, 0.05, 0.12, 0.20, 0.25, 0.22]


def test_speech_level_audio_fills_the_well(model):
    for level in SPEECH:
        model.push_level(level)
    heights = model.bar_heights
    centre = heights[model.bars // 2 - 2:model.bars // 2 + 3]
    assert max(centre) >= 0.6, heights


def test_silence_still_rests_at_the_floor(model):
    """The gain must not turn a quiet room into a wave: only the loudest
    recent sample defines full scale, and silence is still silence."""
    model.set_state("recording")
    for level in SPEECH:
        model.push_level(level)
    for _ in range(40):
        model.push_level(0.0)
    assert model.bar_heights == pytest.approx(profile(BAR_FLOOR))


def test_the_gain_reference_decays_so_a_quiet_talker_catches_up(model, clock):
    """A shout sets the reference; two half-lives later a third of it is loud."""
    from voice.ui.overlay_model import PEAK_FLOOR, PEAK_HALF_LIFE

    model.set_state("recording")
    model.push_level(1.0)
    model.tick(clock.advance(2 * PEAK_HALF_LIFE))
    reference = PEAK_FLOOR + (1.0 - PEAK_FLOOR) * 0.25
    model.push_level(reference)
    assert model.bar_heights[model.bars // 2] == pytest.approx(1.0, abs=0.02)


def test_a_bar_never_collapses_to_a_hairline_while_recording(model):
    from voice.ui.overlay_model import BAR_FLOOR as floor

    assert floor >= 0.25
    model.set_state("recording")
    model.push_level(0.0)
    assert min(model.bar_heights) >= (1.0 - TAPER) * floor - 1e-9


def test_room_noise_is_not_amplified_into_a_waveform(model):
    """The gain must not draw a wave in an empty room: below the noise gate
    there is nothing, however long the room has been quiet."""
    model.set_state("recording")
    for _ in range(40):
        model.push_level(0.04)             # a quiet room, after rms_level's curve
    assert model.bar_heights == pytest.approx(profile(BAR_FLOOR))


def test_a_close_microphone_is_not_clipped_flat(model):
    """A loud, close mic (0.4-0.8) must still show a wave, not 21 full bars."""
    for level in (0.42, 0.78, 0.55, 0.31, 0.64, 0.80, 0.38, 0.71):
        model.push_level(level)
    heights = model.bar_heights
    assert max(heights) >= 0.8
    assert min(heights[8:13]) < 0.75          # the quiet syllables still dip


# -- finishing the fill ---------------------------------------------------
#: The transcription is usually over before the indeterminate sweep has
#: crossed the track, so the checkmark used to replace a half-drawn line. The
#: fill now runs to the end first - from wherever it stood, never from zero.

def _mid_sweep(model, clock, age=1.0):
    """Transcribing, part-way through a sweep. Returns the live (width, alpha)."""
    model.set_state("transcribing")
    model.tick(clock.advance(age))
    return model.sweep


def test_the_fill_runs_to_the_end_before_the_checkmark_draws(model, clock):
    started = _mid_sweep(model, clock)[0]
    assert 0.0 < started < 1.0, "the sweep should be caught part-way across"
    entry = clock.t
    model.set_state("done", now=entry)
    assert model.finishing is True
    assert model.sweep[0] == pytest.approx(started), "it must not restart at zero"
    widths = [model.sweep[0]]
    while model.state_age < FINISH - FRAME:
        model.tick(clock.advance(FRAME))
        widths.append(model.sweep[0])
        # The container's pop-in may run underneath - it scales a zero-length
        # path and draws nothing. The stroke is the part that must wait.
        assert model.check_draw == 0.0, "the checkmark must wait for the fill"
    assert widths == sorted(widths), "the fill never goes backwards"
    clock.t = entry + FINISH
    model.tick(clock.t)
    assert model.sweep == pytest.approx((1.0, 1.0)), "it has to land on 100%"
    assert model.check_draw == pytest.approx(0.0, abs=1e-9), \
        "and only then does the stroke get its cue"
    model.tick(clock.advance(FRAME))            # the next frame is the checkmark's
    assert model.finishing is False
    assert model.sweep == pytest.approx((1.0, 1.0))


def test_the_completion_starts_from_where_the_sweep_stood(model, clock):
    """Early, mid-loop and inside the trailing fade: always from the live fill."""
    for age in (0.3, 1.4, 2.5):
        m = OverlayModel(clock=clock)
        m.set_state("transcribing", now=clock.t)
        m.tick(clock.advance(age))
        width, alpha = m.sweep
        m.set_state("done", now=clock.t)
        assert m.sweep == pytest.approx((width, alpha))
        m.tick(clock.advance(FINISH / 2))
        half_w, half_a = m.sweep
        assert half_w >= width and half_a >= alpha, f"went backwards from {age}s"
        assert half_w > width or width == pytest.approx(1.0)
        clock.advance(FINISH)


def test_a_long_transcription_fills_the_track_and_rests_there(model, clock):
    """A slow CPU can transcribe for minutes. The fill crosses the track and
    waits at the end of it - the owner asked for a bar that goes all the way
    to the right, and one that stops short reads as stalled."""
    model.set_state("transcribing")
    model.tick(clock.advance(SWEEP_TAU * 10))
    settled = model.sweep[0]
    assert settled == pytest.approx(1.0, abs=0.005)
    model.tick(clock.advance(300.0))                   # five minutes more
    assert model.sweep[0] >= settled, "it never goes backwards"
    assert model.sweep[0] <= 1.0
    assert model.state == "transcribing" and model.finishing is False


def test_the_completion_fills_the_track_after_a_long_transcription(model, clock):
    """From the ceiling, where a slow transcription leaves it, to 100%."""
    model.set_state("transcribing")
    model.tick(clock.advance(0.4))          # caught early, still crossing
    caught = model.sweep[0]
    assert caught < 1.0
    model.set_state("done", now=clock.t)
    assert model.sweep[0] == pytest.approx(caught)
    model.tick(clock.advance(FINISH))
    assert model.sweep == pytest.approx((1.0, 1.0)), \
        "the completed fill ends where the grey track ends"


def test_an_error_skips_the_completion(model, clock):
    """A failure is not an operation to finish: no run to 100% first."""
    _mid_sweep(model, clock)
    model.set_state("error", text="whisper died", now=clock.t)
    assert model.finishing is False
    model.tick(clock.advance(ERROR_HOLD - 0.05))
    assert model.state == "error"
    model.tick(clock.advance(0.1))
    assert model.state == "hidden"


def test_the_completion_does_not_move_the_checkmark_or_its_label(model, clock):
    """It rides inside the lead-in the stroke already waited out, so the
    checkmark, the label and the hold all keep the times they had before."""
    assert DONE_DELAY == 0.0, "a completion within DASH_DELAY costs nothing"
    _mid_sweep(model, clock)
    model.set_state("done", now=clock.t)
    plain = OverlayModel(clock=clock)
    plain.set_state("done", now=clock.t)                 # no transcription behind it
    for step in (DASH_DELAY, DASH_DUR, LABEL_DELAY - DASH_DELAY - DASH_DUR,
                 LABEL_DUR, 0.1):
        now = clock.advance(step)
        model.tick(now)
        plain.tick(now)
        assert model.check_pop == pytest.approx(plain.check_pop)
        assert model.check_draw == pytest.approx(plain.check_draw)
        assert model.label_rise == pytest.approx(plain.label_rise)
    assert model.check_draw == pytest.approx(1.0) and model.label_rise == pytest.approx(1.0)
    assert model.state == "done", "the whole presentation still fits inside the hold"


def test_the_checkmarks_stroke_never_starts_before_the_fill_has_landed(model, clock):
    """The invariant behind the chosen 0.2 s: however the two are tuned, no
    part of the checkmark is drawn while the fill is still on its way."""
    from voice.ui.overlay_model import FINISH as finish

    assert finish <= DASH_DELAY + DONE_DELAY
    _mid_sweep(model, clock)
    model.set_state("done", now=clock.t)
    while model.state_age < DONE_HOLD:
        model.tick(clock.advance(FRAME))
        if model.state != "done":
            break
        if model.check_draw > 0.0:
            assert model.sweep == pytest.approx((1.0, 1.0)), "a half-drawn line"
            assert model.finishing is False


def test_the_completion_comes_out_of_the_done_hold(model, clock):
    """The ending must not grow: `done` still lasts exactly DONE_HOLD from the
    moment the daemon says so, completion included."""
    _mid_sweep(model, clock)
    model.set_state("done", now=clock.t)
    model.tick(clock.advance(DONE_HOLD - 0.05))
    assert model.state == "done"
    model.tick(clock.advance(0.1))
    assert model.state == "hidden"


def test_done_without_a_transcription_has_no_completion(model, clock):
    model.set_state("recording")
    model.set_state("done", now=clock.advance(1.0))
    assert model.finishing is False
    model.tick(clock.advance(POP_IN))
    assert model.check_pop[1] == pytest.approx(1.0)


def test_reduced_motion_has_no_completion(clock):
    m = OverlayModel(reduced_motion=True, clock=clock)
    m.set_state("transcribing")
    m.tick(clock.advance(1.0))
    m.set_state("done", now=clock.t)
    assert m.finishing is False
    m.tick(clock.advance(POP_IN))
    assert m.check_pop[1] == pytest.approx(1.0), "straight to the checkmark"


def test_the_clipboard_wording_gets_the_same_completion(model, clock):
    """However the dictation ends - inserted or only copied - the fill finishes."""
    _mid_sweep(model, clock)
    model.set_state("done", text="Copied · Ctrl+V", now=clock.t)
    assert model.finishing is True
    assert model.text == "Copied · Ctrl+V"


def test_a_notice_does_not_replay_the_completion(model, clock):
    _mid_sweep(model, clock)
    model.set_state("done", now=clock.t)
    model.tick(clock.advance(FINISH + 0.25))
    model.set_state("notice", now=clock.t)
    model.tick(clock.advance(NOTICE_TTL + 0.01))
    assert model.state == "done" and model.finishing is False
    assert model.check_draw > 0.0, "the checkmark comes back, not the fill line"


# -- finishing it on request, before the pill is taken off screen ---------
#: Where the pill cannot refuse the keyboard (no layer shell), the daemon
#: unmaps it for the paste chord - and used to unmap it mid-sweep, so the
#: owner never saw the fill arrive. It now asks for the completion when the
#: transcription lands and waits for it before hiding anything.

def test_finishing_the_fill_on_request_runs_it_from_where_it_stood(model, clock):
    started = _mid_sweep(model, clock)[0]
    assert 0.0 < started < 1.0
    left = model.finish_fill(now=clock.t)
    assert left == pytest.approx(FINISH), "it says how long the fill still needs"
    assert model.state == "transcribing", "the checkmark is not due yet"
    assert model.finishing is True
    assert model.sweep[0] == pytest.approx(started), "from here, not from zero"
    model.tick(clock.advance(FINISH / 2))
    assert started < model.sweep[0] < 1.0
    model.tick(clock.advance(FINISH / 2 + FRAME))            # the frame after it lands
    assert model.sweep == pytest.approx((1.0, 1.0))
    assert model.finishing is False
    assert model.finish_fill(now=clock.t) == 0.0, "nothing left to ask for"


def test_asking_twice_does_not_restart_the_fill(model, clock):
    _mid_sweep(model, clock)
    model.finish_fill(now=clock.t)
    model.tick(clock.advance(FINISH / 2))
    half = model.sweep[0]
    assert model.finish_fill(now=clock.t) == pytest.approx(FINISH / 2, abs=1e-6)
    assert model.sweep[0] == pytest.approx(half), "it carries on, it does not restart"


def test_the_hide_for_paste_sequence_lands_the_fill_then_shows_the_checkmark(model, clock):
    """transcribing -> finish -> hidden (for the chord) -> done."""
    _mid_sweep(model, clock)
    assert model.finish_fill(now=clock.t) == pytest.approx(FINISH)
    model.tick(clock.advance(FINISH))
    assert model.sweep == pytest.approx((1.0, 1.0)), "complete before it goes off screen"
    model.set_state("hidden", now=clock.t)
    model.set_state("done", now=clock.advance(0.25))         # after the chord
    assert model.finishing is False, "it finished before the pill was hidden"
    model.tick(clock.advance(DASH_DELAY + DASH_DUR))
    assert model.check_draw == pytest.approx(1.0), "the checkmark plays at its own time"


def test_done_after_a_requested_finish_does_not_run_a_second_completion(model, clock):
    """If nothing hides the pill, `done` inherits the completion in flight
    rather than starting another one from further back."""
    _mid_sweep(model, clock)
    model.finish_fill(now=clock.t)
    model.tick(clock.advance(FINISH / 2))
    carried = model.sweep[0]
    model.set_state("done", now=clock.t)
    assert model.sweep[0] == pytest.approx(carried), "never backwards"
    model.tick(clock.advance(FINISH / 2 + FRAME))
    assert model.sweep == pytest.approx((1.0, 1.0))
    assert model.finishing is False, \
        "it lands on the original schedule, not FINISH after the checkmark's cue"


@pytest.mark.parametrize("state", ["recording", "done", "hidden", "error"])
def test_finishing_is_a_no_op_where_there_is_no_fill_to_finish(model, clock, state):
    model.set_state(state, text="x" if state == "error" else None)
    assert model.finish_fill(now=clock.t) == 0.0
    assert model.finishing is False


def test_reduced_motion_has_no_fill_to_finish(clock):
    m = OverlayModel(reduced_motion=True, clock=clock)
    m.set_state("transcribing")
    m.tick(clock.advance(1.0))
    assert m.finish_fill(now=clock.t) == 0.0
    assert m.finishing is False


def test_a_notice_does_not_lose_a_fill_that_is_finishing(model, clock):
    """The badge can swap in the middle of it; the line underneath still lands."""
    _mid_sweep(model, clock)
    model.finish_fill(now=clock.t)
    model.set_state("notice", now=clock.advance(0.05))
    model.tick(clock.advance(FINISH))
    assert model.sweep == pytest.approx((1.0, 1.0))


def test_a_second_transcription_does_not_start_on_a_landed_fill(model, clock):
    """A landed completion leaves `sweep` at (1, 1) for as long as it is held,
    and entering `transcribing` did not clear it: a transition into that state
    from anywhere but recording, error or hidden would have drawn a motionless
    full bar instead of the indeterminate loop. Not reachable from the daemon
    today, which is why it has to be pinned here."""
    _mid_sweep(model, clock)
    model.finish_fill(now=clock.t)
    model.tick(clock.advance(FINISH + FRAME))
    assert model.sweep == pytest.approx((1.0, 1.0))          # the fill has landed
    model.set_state("done", now=clock.t)

    model.set_state("transcribing", now=clock.advance(0.1))  # the would-be path

    assert model.finishing is False
    model.tick(clock.advance(FRAME))
    width, alpha = model.sweep
    assert width < 1.0, "a new transcription sweeps; it does not sit at full width"
    assert model.sweep[0] != pytest.approx(1.0)
    model.tick(clock.advance(1.0))
    assert model.sweep[0] > width, "and it is moving"


def test_a_notice_over_a_finishing_fill_still_comes_back_to_it(model, clock):
    """The one way into `transcribing` that must keep the completion: a notice
    covered the pill while the fill was running and then expired."""
    _mid_sweep(model, clock)
    model.finish_fill(now=clock.t)
    caught = model.sweep[0]
    model.set_state("notice", now=clock.advance(0.02))
    model.tick(clock.advance(NOTICE_TTL + 0.01))             # the notice expires

    assert model.state == "transcribing"
    assert model.sweep == pytest.approx((1.0, 1.0)), "the fill landed under the notice"
    assert caught < 1.0


def test_the_fill_crosses_the_track_in_the_time_a_transcription_takes(clock):
    """Tuned for the seconds a real transcription lasts, not for ten of them.

    The first tuning reached 55% at two seconds and the owner reported it as
    stopping halfway - which, for their transcriptions, it did.
    """
    model = OverlayModel(clock=clock)
    started = clock.t
    model.set_state("transcribing", now=started)

    def width_at(seconds):
        clock.t = started + seconds
        model.tick(clock.t)
        return model.sweep[0]

    assert width_at(1.0) >= 0.60, "a one-second conversion must look well under way"
    assert width_at(2.0) >= 0.80, "two seconds is most of a dictation; the bar must be near the end"
    assert width_at(3.0) >= 0.90
    assert width_at(30.0) <= SWEEP_CEILING, "the end belongs to the completion, never to the sweep"
