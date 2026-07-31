"""Fits against synthetic data with known answers (RFC 0004 §7, tier 1).

Each test generates the analytic form the fit assumes, adds noise, and asserts
the fit recovers the parameter it was given. The other half of the file is the
failure behaviour, which matters just as much: a fit that returns zeros on
failure gets written to the device as though it were a measurement.
"""

import numpy as np
import pytest
import xarray as xr
from qpi_driver.tuners.fitting import (
    FitError,
    decaying_cosine,
    exponential_decay,
    fit_chevron,
    fit_conditional_phase,
    fit_drag,
    fit_fine_amplitude,
    fit_punchout,
    fit_qubit_spectroscopy,
    fit_rabi,
    fit_ramsey,
    fit_rb_decay,
    fit_resonator_spectroscopy,
    fit_t1,
    fit_t2,
    lorentzian,
    signal_of,
)
from qpi_driver.tuners.fitting.core import align, require_in_range, require_positive

RNG = np.random.default_rng(20260730)


def _noise(size: int, scale: float = 0.01) -> np.ndarray:
    return RNG.normal(0.0, scale, size)


# --- oscillatory fits ---------------------------------------------------------


def test_rabi_recovers_the_pi_pulse_amplitude():
    amp180 = 0.21
    amplitudes = np.linspace(0.0, 0.5, 81)
    # One full period spans 2*amp180, so the frequency is 1/(2*amp180).
    signal = 0.5 * np.cos(2 * np.pi * amplitudes / (2 * amp180)) + 0.5
    fitted = fit_rabi(amplitudes, signal + _noise(len(amplitudes)))
    assert fitted["amp180"] == pytest.approx(amp180, rel=0.05)


def test_rabi_refuses_a_fit_outside_the_swept_range():
    """A pi pulse the sweep never reached is an extrapolation, not a measurement."""
    amplitudes = np.linspace(0.0, 0.02, 41)
    signal = 0.5 * np.cos(2 * np.pi * amplitudes / (2 * 5.0)) + 0.5
    with pytest.raises(FitError, match="outside the swept range"):
        fit_rabi(amplitudes, signal)


def test_ramsey_recovers_the_detuning_and_t2_star():
    detuning, artificial, t2 = 0.3e6, 1e6, 8e-6
    delays = np.linspace(4e-9, 20e-6, 121)
    signal = decaying_cosine(delays, 0.5, detuning + artificial, 0.0, t2, 0.5)
    fitted = fit_ramsey(delays, signal + _noise(len(delays), 0.005), artificial)
    assert fitted["detuning"] == pytest.approx(detuning, abs=3e4)
    assert fitted["t2_star"] == pytest.approx(t2, rel=0.15)


def test_drag_finds_the_zero_crossing():
    betas = np.linspace(-1.0, 1.0, 41)
    signal = 2.5 * (betas - 0.32)
    fitted = fit_drag(betas, signal + _noise(len(betas), 0.002))
    assert fitted["motzoi"] == pytest.approx(0.32, abs=0.02)


def test_drag_refuses_a_flat_sweep():
    betas = np.linspace(-1.0, 1.0, 21)
    with pytest.raises(FitError, match="flat in beta"):
        fit_drag(betas, np.full(21, 0.5))


def _fine_amplitude_signal(counts: np.ndarray, delta: float) -> np.ndarray:
    """``P(n) = ½(1 + (-1)ⁿ·sin(nδ))`` — a π/2 pre-rotation then n π pulses."""
    return 0.5 * (1 + np.power(-1.0, counts) * np.sin(counts * delta))


def test_fine_amplitude_corrects_a_known_over_rotation():
    delta = 0.01  # radians per pulse of over-rotation
    counts = np.arange(1, 41, dtype=float)
    signal = _fine_amplitude_signal(counts, delta)
    fitted = fit_fine_amplitude(
        counts, signal + _noise(len(counts), 0.001), 0.2, ground=0.0, excited=1.0
    )

    assert fitted["error_per_pulse"] == pytest.approx(delta, abs=0.002)
    assert fitted["amp180"] < 0.2  # over-rotating, so the amplitude comes down


def test_fine_amplitude_recovers_the_sign_of_an_under_rotation():
    """The π/2 pre-rotation is what makes this distinguishable from over-rotation."""
    counts = np.arange(1, 41, dtype=float)
    fitted = fit_fine_amplitude(
        counts, _fine_amplitude_signal(counts, -0.01), 0.2, ground=0.0, excited=1.0
    )

    assert fitted["error_per_pulse"] == pytest.approx(-0.01, abs=0.002)
    assert fitted["amp180"] > 0.2  # under-rotating, so the amplitude goes up


def test_fine_amplitude_is_scaled_by_the_calibration_points_not_the_sweep():
    """Normalising against the observed range inflates the error by the contrast reached."""
    counts = np.arange(1, 41, dtype=float)
    # Half the contrast: |0> reads 0.25 and |1> reads 0.75.
    signal = 0.25 + 0.5 * _fine_amplitude_signal(counts, 0.01)
    fitted = fit_fine_amplitude(counts, signal, 0.2, ground=0.25, excited=0.75)
    assert fitted["error_per_pulse"] == pytest.approx(0.01, abs=0.002)


def test_fine_amplitude_refuses_indistinguishable_calibration_points():
    counts = np.arange(1, 21, dtype=float)
    with pytest.raises(FitError, match="indistinguishable"):
        fit_fine_amplitude(counts, np.ones(20) * 0.5, 0.2, ground=0.5, excited=0.5)


def test_fine_amplitude_refuses_fractional_repetition_counts():
    with pytest.raises(FitError, match="whole pulse repetitions"):
        fit_fine_amplitude(
            np.array([1.5, 2.5, 3.5, 4.5]),
            np.array([0.1, 0.9, 0.2, 0.8]),
            0.2,
            ground=0.0,
            excited=1.0,
        )


# --- exponential fits ---------------------------------------------------------


def test_t1_recovers_the_relaxation_time():
    t1 = 45e-6
    delays = np.linspace(0.0, 200e-6, 81)
    signal = exponential_decay(delays, 1.0, t1, 0.05)
    fitted = fit_t1(delays, signal + _noise(len(delays), 0.005))
    assert fitted["t1"] == pytest.approx(t1, rel=0.05)


def test_t2_recovers_the_dephasing_time():
    t2 = 30e-6
    delays = np.linspace(0.0, 150e-6, 81)
    signal = exponential_decay(delays, 1.0, t2, 0.05)
    fitted = fit_t2(delays, signal + _noise(len(delays), 0.005))
    assert fitted["t2"] == pytest.approx(t2, rel=0.05)


def test_a_coherence_time_far_beyond_the_window_is_refused():
    """Extrapolating T1 from a window that never sees the decay is not a measurement."""
    delays = np.linspace(0.0, 1e-9, 41)
    with pytest.raises(FitError):
        fit_t1(delays, np.linspace(1.0, 0.999999, 41))


def test_rb_recovers_a_known_fidelity():
    decay = 0.995
    depths = np.array([1, 2, 4, 8, 16, 32, 64, 128], dtype=float)
    survival = 0.5 * decay**depths + 0.5
    fitted = fit_rb_decay(depths, survival + _noise(len(depths), 0.002))
    expected = 1.0 - (1.0 - decay) * (2 - 1) / 2
    assert fitted["fidelity"] == pytest.approx(expected, abs=0.002)
    assert fitted["error_per_gate"] == pytest.approx(1 - expected, abs=0.002)


def test_rb_recovers_the_same_fidelity_from_a_rescaled_signal():
    """The fit must not care about the readout's scale and offset.

    This is what the `rb` routine hands it: the acquisition rescaled to span
    [0, 1], from a decay that has not reached its asymptote by the deepest
    sequence. Bounding the model's amplitude used to make that case come out at
    0.988 whatever the truth was, so every chip better than about 3% error per
    Clifford measured the same — permanently below the default drift threshold.
    """
    decay = 0.999
    depths = np.array([1, 2, 4, 8, 16, 32, 64], dtype=float)
    survival = 0.5 * decay**depths + 0.5
    rescaled = (survival - survival.min()) / (survival.max() - survival.min())

    assert fit_rb_decay(depths, rescaled)["decay_rate"] == pytest.approx(
        fit_rb_decay(depths, survival)["decay_rate"], abs=1e-4
    )
    assert fit_rb_decay(depths, rescaled)["fidelity"] == pytest.approx(
        1.0 - (1.0 - decay) / 2, abs=1e-4
    )


def test_rb_uses_the_right_dimension_for_two_qubits():
    """d = 2^n, so a two-qubit decay maps to a worse fidelity than a one-qubit one."""
    decay = 0.99
    depths = np.array([1, 2, 4, 8, 16, 32], dtype=float)
    survival = 0.5 * decay**depths + 0.5
    one = fit_rb_decay(depths, survival, n_qubits=1)
    two = fit_rb_decay(depths, survival, n_qubits=2)
    assert two["error_per_gate"] > one["error_per_gate"]
    assert two["error_per_gate"] == pytest.approx((1 - decay) * 3 / 4, abs=0.002)


def test_rb_refuses_data_it_cannot_fit():
    depths = np.array([1, 2, 4, 8], dtype=float)
    with pytest.raises(FitError):
        fit_rb_decay(depths, np.array([np.nan, np.nan, np.nan, np.nan]))


# --- lorentzian fits ----------------------------------------------------------


def test_resonator_spectroscopy_finds_a_peak():
    centre = 6.12e9
    frequencies = np.linspace(6.0e9, 6.3e9, 201)
    signal = lorentzian(frequencies, 1.0, centre, 2e6, 0.1)
    fitted = fit_resonator_spectroscopy(frequencies, signal + _noise(201, 0.005))
    assert fitted["readout_frequency"] == pytest.approx(centre, abs=1e5)
    assert fitted["linewidth"] == pytest.approx(2e6, rel=0.2)


def test_qubit_spectroscopy_finds_a_dip():
    """Which way the feature points depends on the acquisition, so both must fit."""
    centre = 5.03e9
    frequencies = np.linspace(4.9e9, 5.2e9, 201)
    signal = lorentzian(frequencies, -1.0, centre, 3e6, 1.0)
    fitted = fit_qubit_spectroscopy(frequencies, signal + _noise(201, 0.005))
    assert fitted["clock_freq_01"] == pytest.approx(centre, abs=2e5)


def test_a_spectroscopy_centre_outside_the_scan_is_refused():
    frequencies = np.linspace(6.0e9, 6.1e9, 51)
    with pytest.raises(FitError):
        fit_resonator_spectroscopy(frequencies, np.linspace(0.0, 1.0, 51))


def test_punchout_picks_the_last_dressed_power():
    powers = np.array([0.01, 0.05, 0.1, 0.2, 0.4])
    frequencies = np.array([6.0e9, 6.0e9, 6.0e9, 6.05e9, 6.1e9])
    fitted = fit_punchout(powers, frequencies)
    assert fitted["readout_power"] == pytest.approx(0.1)
    assert fitted["dressed_frequency"] == 6.0e9
    assert fitted["bare_frequency"] == 6.1e9


def test_punchout_reports_the_frequency_at_the_power_it_chose():
    """Not the low-power one. Choosing a power moves the resonance to a new place,
    and the caller has to drive it there rather than where it used to be."""
    powers = np.array([0.01, 0.1, 0.2, 0.4])
    frequencies = np.array([6.000e9, 6.002e9, 6.020e9, 6.040e9])
    fitted = fit_punchout(powers, frequencies)

    assert fitted["readout_power"] == pytest.approx(0.1)
    assert fitted["readout_frequency"] == pytest.approx(6.002e9)
    assert fitted["readout_frequency"] != fitted["dressed_frequency"]


def test_punchout_refuses_a_sweep_with_no_shift():
    powers = np.array([0.01, 0.05, 0.1, 0.2])
    with pytest.raises(FitError, match="no resonator shift"):
        fit_punchout(powers, np.full(4, 6.0e9))


# --- two-qubit fits -----------------------------------------------------------


def test_chevron_locates_the_operating_point():
    amplitudes = np.linspace(0.1, 0.5, 9)
    durations = np.linspace(20e-9, 200e-9, 9)
    grid = np.zeros((9, 9))
    grid[5, 6] = 1.0  # the transfer maximum
    fitted = fit_chevron(amplitudes, durations, grid.reshape(-1))
    assert fitted["cz_amplitude"] == pytest.approx(amplitudes[5], abs=0.03)
    assert fitted["cz_duration"] == pytest.approx(durations[6], abs=1e-8)


def test_chevron_reads_past_a_wiggle_in_the_trough():
    """The round trip, not a bump on the way to it.

    The exchange beats against the transitions the flux pulse sits near, so the
    bottom of the round trip is not smooth. Walking to the first local maximum
    after the trough stops on that bump — still deep in |02>, and half the right
    duration, which is a complete population swap rather than a CZ.
    """
    amplitudes = np.linspace(0.3, 0.5, 5)
    durations = np.linspace(10e-9, 200e-9, 20)
    # One resonant row: a cosine round trip of ~110 ns with a 5% ripple on it.
    angle = 2 * np.pi * durations / 110e-9
    row = 0.5 + 0.5 * np.cos(angle) + 0.05 * np.cos(5 * angle)
    grid = np.tile(0.5, (5, 20))
    grid[2, :] = row

    fitted = fit_chevron(amplitudes, durations, grid.reshape(-1))
    assert fitted["cz_amplitude"] == pytest.approx(amplitudes[2], abs=0.03)
    assert fitted["cz_duration"] == pytest.approx(110e-9, rel=0.05), (
        "half of this is a swap, and it is what the turning-point walk returned"
    )


def test_chevron_needs_a_full_grid():
    with pytest.raises(FitError, match="needs 9 points"):
        fit_chevron(np.arange(3.0), np.arange(3.0), np.zeros(4))


@pytest.mark.parametrize("conditional", [180.0, 150.0, 95.0, 220.0])
def test_conditional_phase_recovers_the_offset_between_two_fringes(conditional):
    phases = np.linspace(0.0, 360.0, 73)
    ground = 0.5 + 0.4 * np.cos(np.deg2rad(phases))
    excited = 0.5 + 0.4 * np.cos(np.deg2rad(phases - conditional))

    fitted = fit_conditional_phase(phases, ground, excited)

    assert fitted["conditional_phase"] == pytest.approx(conditional, abs=0.5)
    assert fitted["phase_correction"] == pytest.approx(180.0 - conditional, abs=0.5)


def test_conditional_phase_ignores_a_phase_common_to_both_fringes():
    """The control's dynamical phase over the flux pulse must not reach the answer.

    It is common to both fringes and can be tens of turns; only the offset
    between them is the gate.
    """
    phases = np.linspace(0.0, 360.0, 73)
    common = 117.0
    ground = 0.5 + 0.4 * np.cos(np.deg2rad(phases - common))
    excited = 0.5 + 0.4 * np.cos(np.deg2rad(phases - common - 150.0))

    fitted = fit_conditional_phase(phases, ground, excited)

    assert fitted["conditional_phase"] == pytest.approx(150.0, abs=0.5)


def test_conditional_phase_refuses_a_flat_fringe():
    phases = np.linspace(0.0, 360.0, 21)
    with pytest.raises(FitError, match="flat"):
        fit_conditional_phase(phases, np.full(21, 1.0), np.full(21, 1.0))


# --- the shared machinery -----------------------------------------------------


def test_signal_of_reads_an_xarray_dataset():
    dataset = xr.Dataset({"y": ("x", [1.0, 2.0, 3.0])})
    assert list(signal_of(dataset)) == [1.0, 2.0, 3.0]


def test_signal_of_takes_the_magnitude_of_complex_iq():
    dataset = xr.Dataset({"y": ("x", np.array([3 + 4j, 0 + 1j]))})
    assert list(signal_of(dataset)) == [5.0, 1.0]


def test_signal_of_refuses_an_empty_acquisition():
    with pytest.raises(FitError, match="no data points"):
        signal_of(np.array([]))


def test_signal_of_refuses_an_all_nan_acquisition():
    """The dummy cluster returns NaN; that must fail loudly, not fit to zero."""
    with pytest.raises(FitError, match="no finite data points"):
        signal_of(np.array([np.nan, np.nan]))


def test_signal_of_refuses_a_dataset_with_no_variables():
    with pytest.raises(FitError, match="no data variables"):
        signal_of(xr.Dataset())


def test_signal_of_repairs_a_few_nans_around_finite_data():
    values = signal_of(np.array([1.0, np.nan, 3.0, 4.0]))
    assert np.all(np.isfinite(values))


def test_align_trims_to_the_shorter_axis():
    x, y = align(np.arange(10.0), np.arange(6.0), what="test")
    assert len(x) == len(y) == 6


def test_align_refuses_too_few_points():
    with pytest.raises(FitError, match="at least 4 points"):
        align(np.arange(2.0), np.arange(2.0), what="test")


def test_require_in_range_accepts_a_reversed_window():
    assert require_in_range(5.0, 10.0, 0.0, what="x") == 5.0


def test_require_in_range_refuses_a_non_finite_value():
    with pytest.raises(FitError, match="not finite"):
        require_in_range(float("nan"), 0.0, 1.0, what="x")


def test_require_positive_refuses_zero_and_negatives():
    assert require_positive(1.0, what="x") == 1.0
    with pytest.raises(FitError, match="not physical"):
        require_positive(0.0, what="x")
    with pytest.raises(FitError, match="not physical"):
        require_positive(-1.0, what="x")
