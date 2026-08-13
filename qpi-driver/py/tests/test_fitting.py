"""Fits against synthetic data with known answers (RFC 0004 §7, tier 1).

Each test generates the analytic form the fit assumes, adds noise, and asserts
the fit recovers the parameter it was given. The other half of the file is the
failure behaviour, which matters just as much: a fit that returns zeros on
failure gets written to the device as though it were a measurement.
"""

import json

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
    fit_readout_discrimination,
    fit_readout_timing,
    fit_resonator_spectroscopy,
    fit_spectroscopy_power,
    fit_t1,
    fit_t2,
    lorentzian,
    signal_of,
)
from qpi_driver.tuners.fitting.core import (
    MAX_FIT_POINTS,
    align,
    fit_summary,
    require_in_range,
    require_positive,
)

RNG = np.random.default_rng(20260730)


def _noise(size: int, scale: float = 0.01) -> np.ndarray:
    return RNG.normal(0.0, scale, size)


def _fine_amplitude_signal(counts: np.ndarray, delta: float) -> np.ndarray:
    """``P(n) = ½(1 + (-1)ⁿ·sin(nδ))`` — a π/2 pre-rotation then n π pulses."""
    return 0.5 * (1 + np.power(-1.0, counts) * np.sin(counts * delta))


class TestOscillatoryFits:
    """Rabi, Ramsey, DRAG, and fine-amplitude — fits over a sinusoid or a swept phase."""

    def test_rabi_recovers_the_pi_pulse_amplitude(self):
        amp180 = 0.21
        amplitudes = np.linspace(0.0, 0.5, 81)
        # One full period spans 2*amp180, so the frequency is 1/(2*amp180).
        signal = 0.5 * np.cos(2 * np.pi * amplitudes / (2 * amp180)) + 0.5
        fitted = fit_rabi(amplitudes, signal + _noise(len(amplitudes)))
        assert fitted["amp180"] == pytest.approx(amp180, rel=0.05)

    def test_rabi_refuses_a_fit_outside_the_swept_range(self):
        """A pi pulse the sweep never reached is an extrapolation, not a measurement.

        Above the range it is also *actionable* — there is more amplitude to try — so it
        raises `OutOfRange` and a routine can reach further rather than an operator reading
        the message. Still a `FitError`, so a caller that does not escalate is unaffected.
        """
        from qpi_driver.tuners.fitting.core import OutOfRange

        amplitudes = np.linspace(0.0, 0.02, 41)
        signal = 0.5 * np.cos(2 * np.pi * amplitudes / (2 * 5.0)) + 0.5
        with pytest.raises(FitError, match="past the top of the range") as raised:
            fit_rabi(amplitudes, signal)
        assert isinstance(raised.value, OutOfRange)
        assert (raised.value.axis, raised.value.direction) == ("amplitudes", "wider")

    def test_ramsey_recovers_the_detuning_and_t2_star(self):
        detuning, artificial, t2 = 0.3e6, 1e6, 8e-6
        delays = np.linspace(4e-9, 20e-6, 121)
        signal = decaying_cosine(delays, 0.5, detuning + artificial, 0.0, t2, 0.5)
        fitted = fit_ramsey(delays, signal + _noise(len(delays), 0.005), artificial)
        assert fitted["detuning"] == pytest.approx(detuning, abs=3e4)
        assert fitted["t2_star"] == pytest.approx(t2, rel=0.15)

    def test_drag_finds_the_zero_crossing(self):
        betas = np.linspace(-1.0, 1.0, 41)
        signal = 2.5 * (betas - 0.32)
        fitted = fit_drag(betas, signal + _noise(len(betas), 0.002))
        assert fitted["motzoi"] == pytest.approx(0.32, abs=0.02)

    def test_drag_refuses_a_flat_sweep(self):
        betas = np.linspace(-1.0, 1.0, 21)
        with pytest.raises(FitError, match="flat in beta"):
            fit_drag(betas, np.full(21, 0.5))

    def test_fine_amplitude_corrects_a_known_over_rotation(self):
        delta = 0.01  # radians per pulse of over-rotation
        counts = np.arange(1, 41, dtype=float)
        signal = _fine_amplitude_signal(counts, delta)
        fitted = fit_fine_amplitude(
            counts, signal + _noise(len(counts), 0.001), 0.2, ground=0.0, excited=1.0
        )

        assert fitted["error_per_pulse"] == pytest.approx(delta, abs=0.002)
        assert fitted["amp180"] < 0.2  # over-rotating, so the amplitude comes down

    def test_fine_amplitude_recovers_the_sign_of_an_under_rotation(self):
        """The π/2 pre-rotation is what makes this distinguishable from over-rotation."""
        counts = np.arange(1, 41, dtype=float)
        fitted = fit_fine_amplitude(
            counts, _fine_amplitude_signal(counts, -0.01), 0.2, ground=0.0, excited=1.0
        )

        assert fitted["error_per_pulse"] == pytest.approx(-0.01, abs=0.002)
        assert fitted["amp180"] > 0.2  # under-rotating, so the amplitude goes up

    def test_fine_amplitude_is_scaled_by_the_calibration_points_not_the_sweep(self):
        """Normalising against the observed range inflates the error by the contrast reached."""
        counts = np.arange(1, 41, dtype=float)
        # Half the contrast: |0> reads 0.25 and |1> reads 0.75.
        signal = 0.25 + 0.5 * _fine_amplitude_signal(counts, 0.01)
        fitted = fit_fine_amplitude(counts, signal, 0.2, ground=0.25, excited=0.75)
        assert fitted["error_per_pulse"] == pytest.approx(0.01, abs=0.002)

    def test_fine_amplitude_refuses_indistinguishable_calibration_points(self):
        counts = np.arange(1, 21, dtype=float)
        with pytest.raises(FitError, match="indistinguishable"):
            fit_fine_amplitude(counts, np.ones(20) * 0.5, 0.2, ground=0.5, excited=0.5)

    def test_fine_amplitude_refuses_fractional_repetition_counts(self):
        with pytest.raises(FitError, match="whole pulse repetitions"):
            fit_fine_amplitude(
                np.array([1.5, 2.5, 3.5, 4.5]),
                np.array([0.1, 0.9, 0.2, 0.8]),
                0.2,
                ground=0.0,
                excited=1.0,
            )


class TestExponentialFits:
    """T1, RB decay, and what each refuses: a coherence time past the window, data neither can fit."""

    def test_t1_recovers_the_relaxation_time(self):
        t1 = 45e-6
        delays = np.linspace(0.0, 200e-6, 81)
        signal = exponential_decay(delays, 1.0, t1, 0.05)
        fitted = fit_t1(delays, signal + _noise(len(delays), 0.005))
        assert fitted["t1"] == pytest.approx(t1, rel=0.05)

    def test_t2_recovers_the_dephasing_time(self):
        t2 = 30e-6
        delays = np.linspace(0.0, 150e-6, 81)
        signal = exponential_decay(delays, 1.0, t2, 0.05)
        fitted = fit_t2(delays, signal + _noise(len(delays), 0.005))
        assert fitted["t2"] == pytest.approx(t2, rel=0.05)

    def test_a_coherence_time_far_beyond_the_window_is_refused(self):
        """Extrapolating T1 from a window that never sees the decay is not a measurement."""
        delays = np.linspace(0.0, 1e-9, 41)
        with pytest.raises(FitError):
            fit_t1(delays, np.linspace(1.0, 0.999999, 41))

    def test_rb_recovers_a_known_fidelity(self):
        decay = 0.995
        depths = np.array([1, 2, 4, 8, 16, 32, 64, 128], dtype=float)
        survival = 0.5 * decay**depths + 0.5
        fitted = fit_rb_decay(depths, survival + _noise(len(depths), 0.002))
        expected = 1.0 - (1.0 - decay) * (2 - 1) / 2
        assert fitted["fidelity"] == pytest.approx(expected, abs=0.002)
        assert fitted["error_per_gate"] == pytest.approx(1 - expected, abs=0.002)

    def test_rb_recovers_the_same_fidelity_from_a_rescaled_signal(self):
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

    def test_rb_uses_the_right_dimension_for_two_qubits(self):
        """d = 2^n, so a two-qubit decay maps to a worse fidelity than a one-qubit one."""
        decay = 0.99
        depths = np.array([1, 2, 4, 8, 16, 32], dtype=float)
        survival = 0.5 * decay**depths + 0.5
        one = fit_rb_decay(depths, survival, n_qubits=1)
        two = fit_rb_decay(depths, survival, n_qubits=2)
        assert two["error_per_gate"] > one["error_per_gate"]
        assert two["error_per_gate"] == pytest.approx((1 - decay) * 3 / 4, abs=0.002)

    def test_rb_refuses_data_it_cannot_fit(self):
        depths = np.array([1, 2, 4, 8], dtype=float)
        with pytest.raises(FitError):
            fit_rb_decay(depths, np.array([np.nan, np.nan, np.nan, np.nan]))

    #: Survival, as the `rb` routine hands it over — averaged per depth and rescaled to
    #: [0, 1] — from three consecutive runs of a chip whose readout sat a megahertz off
    #: its resonator. Non-monotonic noise, every one of them, and the fit reported
    #: 0.99999, 0.941 and 0.586 with the same confidence it reports a real decay.
    NOISE_FROM_A_DEAD_READOUT = (
        (0.9999970, [0.0, 1.0, 0.5416, 0.6842, 0.7982, 0.4872, 0.3045]),
        (0.9410470, [0.0, 0.5284, 0.0078, 0.3377, 0.8942, 1.0, 0.4301]),
        (0.5858176, [0.0, 0.5013, 0.8732, 0.2509, 1.0, 0.2796, 0.7702]),
    )

    @pytest.mark.parametrize("reported,survival", NOISE_FROM_A_DEAD_READOUT)
    def test_rb_refuses_a_decay_it_cannot_see_above_the_noise(self, reported, survival):
        """A confident number from noise is the one answer worse than no answer.

        These went unremarked through five calibration runs and into the drift check,
        which compares them against a threshold. `reported` is what each one used to
        return, and is here to say what the guard is worth rather than to be asserted.
        """
        depths = np.array([1, 2, 4, 8, 16, 32, 64], dtype=float)
        with pytest.raises(FitError, match="no decay here"):
            fit_rb_decay(depths, np.asarray(survival))

    def test_rb_still_accepts_a_decay_that_has_not_reached_its_asymptote(self):
        """The case the guard must not catch — see the rescaled-signal test above.

        A chip good enough that depth 64 has used only six percent of its decay is the
        chip most worth benchmarking, and its span-to-scatter is large precisely
        because the decay is clean rather than because it is deep.
        """
        depths = np.array([1, 2, 4, 8, 16, 32, 64], dtype=float)
        survival = 0.5 * 0.999**depths + 0.5
        rescaled = (survival - survival.min()) / (survival.max() - survival.min())

        fitted = fit_rb_decay(depths, rescaled + _noise(len(depths), 0.01))
        assert fitted["fidelity"] == pytest.approx(1.0 - (1.0 - 0.999) / 2, abs=0.002)


def _power_sweep(
    frequencies: np.ndarray, centre: float, widths_and_depths: list[tuple[float, float]]
) -> np.ndarray:
    """One spectroscopy row per drive power, each with its own linewidth and depth."""
    return np.vstack(
        [
            lorentzian(frequencies, -depth, centre, width, 1.0)
            + _noise(frequencies.size, 0.01)
            for width, depth in widths_and_depths
        ]
    )


class TestLorentzianFits:
    """Resonator and qubit spectroscopy, and punchout's power-dependent dressed shift."""

    def test_resonator_spectroscopy_finds_a_peak(self):
        centre = 6.12e9
        frequencies = np.linspace(6.0e9, 6.3e9, 201)
        signal = lorentzian(frequencies, 1.0, centre, 2e6, 0.1)
        fitted = fit_resonator_spectroscopy(frequencies, signal + _noise(201, 0.005))
        assert fitted["readout_frequency"] == pytest.approx(centre, abs=1e5)
        assert fitted["linewidth"] == pytest.approx(2e6, rel=0.2)

    def test_qubit_spectroscopy_finds_a_dip(self):
        """Which way the feature points depends on the acquisition, so both must fit."""
        centre = 5.03e9
        frequencies = np.linspace(4.9e9, 5.2e9, 201)
        signal = lorentzian(frequencies, -1.0, centre, 3e6, 1.0)
        fitted = fit_qubit_spectroscopy(frequencies, signal + _noise(201, 0.005))
        assert fitted["clock_freq_01"] == pytest.approx(centre, abs=2e5)

    def test_spectroscopy_power_prefers_signal_to_noise_over_depth(self):
        """The deepest row is the most broadened one, so depth is the wrong criterion."""
        centre = 5.03e9
        frequencies = np.linspace(4.9e9, 5.2e9, 201)
        powers = np.array([0.01, 0.02, 0.04])
        # Depth climbs with power, but so does width: the last row is twice as deep and
        # nine times as broad. Choosing on depth takes it; choosing on contrast-over-
        # scatter takes the middle one, which is what resolves the line.
        signal = _power_sweep(
            frequencies, centre, [(3e6, 0.4), (4e6, 1.0), (36e6, 2.0)]
        )
        fitted = fit_spectroscopy_power(powers, frequencies, signal)
        assert fitted["drive_amplitude"] == pytest.approx(0.02)
        assert fitted["clock_freq_01"] == pytest.approx(centre, abs=5e5)

    def test_spectroscopy_power_ignores_a_line_the_sweep_could_not_resolve(self):
        """A noise-fitted spike is the *narrowest* row, so it must not set the reference.

        Left in, it becomes the linewidth every real row is judged against, and a sweep
        that plainly found the qubit returns nothing usable.
        """
        centre = 5.03e9
        frequencies = np.linspace(4.9e9, 5.2e9, 61)  # 5 MHz steps
        powers = np.array([0.005, 0.02])
        signal = _power_sweep(frequencies, centre, [(2e5, 0.05), (12e6, 1.0)])
        fitted = fit_spectroscopy_power(powers, frequencies, signal)
        assert fitted["drive_amplitude"] == pytest.approx(0.02)
        assert fitted["linewidth"] > 5e6

    def test_spectroscopy_power_refuses_a_sweep_with_no_resolved_line(self):
        frequencies = np.linspace(4.9e9, 5.2e9, 61)
        powers = np.array([0.005, 0.01])
        signal = np.vstack([_noise(61, 0.01) + 1.0 for _ in powers])
        with pytest.raises(FitError, match="no drive power"):
            fit_spectroscopy_power(powers, frequencies, signal)

    def test_a_spectroscopy_centre_outside_the_scan_is_refused(self):
        frequencies = np.linspace(6.0e9, 6.1e9, 51)
        with pytest.raises(FitError):
            fit_resonator_spectroscopy(frequencies, np.linspace(0.0, 1.0, 51))

    def test_punchout_picks_the_last_dressed_power(self):
        powers = np.array([0.01, 0.05, 0.1, 0.2, 0.4])
        frequencies = np.array([6.0e9, 6.0e9, 6.0e9, 6.05e9, 6.1e9])
        fitted = fit_punchout(powers, frequencies)
        assert fitted["readout_power"] == pytest.approx(0.1)
        assert fitted["dressed_frequency"] == 6.0e9
        assert fitted["bare_frequency"] == 6.1e9

    def test_punchout_reports_the_frequency_at_the_power_it_chose(self):
        """Not the low-power one. Choosing a power moves the resonance to a new place,
        and the caller has to drive it there rather than where it used to be."""
        powers = np.array([0.01, 0.1, 0.2, 0.4])
        frequencies = np.array([6.000e9, 6.002e9, 6.020e9, 6.040e9])
        fitted = fit_punchout(powers, frequencies)

        assert fitted["readout_power"] == pytest.approx(0.1)
        assert fitted["readout_frequency"] == pytest.approx(6.002e9)
        assert fitted["readout_frequency"] != fitted["dressed_frequency"]

    def test_punchout_refuses_a_sweep_with_no_shift(self):
        powers = np.array([0.01, 0.05, 0.1, 0.2])
        with pytest.raises(FitError, match="no resonator shift"):
            fit_punchout(powers, np.full(4, 6.0e9))


def _readout_trace(
    arrival_ns: float, tau_ns: float = 79.58, samples: int = 1000, sigma: float = 0.008
) -> np.ndarray:
    """A trace that waits *arrival_ns*, then rings up with time constant *tau_ns*.

    ``sigma`` is the *averaged* per-sample noise a Trace comes back with — a shot
    count of 1024 over a single-shot spread of 0.25. At single-shot noise the 10%
    level and the noise floor are the same number and no arrival is findable, which
    is a property of the measurement rather than of this fit.
    """
    times = np.arange(samples, dtype=float)
    envelope = np.where(
        times >= arrival_ns, 1.0 - np.exp(-(times - arrival_ns) / tau_ns), 0.0
    )
    rng = np.random.default_rng(7)
    return (
        (1 + 3j) * envelope
        + rng.normal(0, sigma, samples)
        + 1j * rng.normal(0, sigma, samples)
    )


class TestTheReadoutChainsTimingOffOneRawTrace:
    """Recovering a raw trace's arrival time and ring-up constant, and what a signal-free or already-integrated trace refuses."""

    @pytest.mark.parametrize("arrival", [0.0, 50.0, 148.0, 300.0])
    def test_readout_timing_recovers_the_arrival_and_the_ring_up(self, arrival):
        """Both together, because neither is measurable alone.

        A level crossing finds the arrival biased late by the ring-up, and fitting the
        ring-up needs to know where it started. One straight line through
        ``ln(1 - rise)`` gives both, consistent by construction.
        """
        fitted = fit_readout_timing(_readout_trace(arrival), sampling_rate=1e9)

        assert fitted["time_of_flight"] * 1e9 == pytest.approx(arrival, abs=8.0)
        assert fitted["ring_up_time"] * 1e9 == pytest.approx(79.58, rel=0.05)
        # The linewidth is what nothing else measures, and it is the same number.
        assert fitted["linewidth"] == pytest.approx(2e6, rel=0.05)

    def test_readout_timing_refuses_a_trace_with_no_signal(self):
        """A dead readout chain is not a mismeasured delay, and must not report one."""
        noise = np.random.default_rng(0).normal(0, 0.01, 500) + 0j
        with pytest.raises(FitError, match="never rises out of its own noise"):
            fit_readout_timing(noise, sampling_rate=1e9)

    def test_readout_timing_refuses_an_integrated_acquisition(self):
        """One point per acquisition is the wrong protocol, not a short trace."""
        with pytest.raises(FitError, match="needs a trace"):
            fit_readout_timing(np.array([1 + 1j, 2 + 2j]), sampling_rate=1e9)

    def test_readout_timing_reads_the_baseline_from_the_front_not_a_percentile(self):
        """With the delay already right there is no dead time to take a floor from.

        A low percentile over the whole trace lands part-way up the ring-up in that
        case, reporting a floor about half the swing too high — which moved the
        recovered onset by 50 ns on a true zero. The front of the trace is the baseline
        whether or not the window waited first.
        """
        fitted = fit_readout_timing(_readout_trace(0.0), sampling_rate=1e9)
        assert fitted["time_of_flight"] * 1e9 < 10.0
        # Not near zero — with no dead time the lead window unavoidably averages in the
        # first few per cent of the rise. Far below the settled level is what matters,
        # and is what a percentile over the whole trace would not give.
        assert fitted["noise_floor"] < 0.1 * fitted["settled_amplitude"]


def _clouds(rotation_deg: float, separation: float = 3.0, shots: int = 800):
    """Two Gaussian clouds a given separation apart, at a given chain rotation."""
    rng = np.random.default_rng(11)
    turn = np.exp(1j * np.deg2rad(rotation_deg))
    ground = 0.0 + 0j
    excited = separation * turn

    def noise():
        return rng.normal(0, 0.25, shots) + 1j * rng.normal(0, 0.25, shots)

    return ground + noise(), excited + noise()


class TestTheReadoutDiscriminator:
    """The rotation and threshold that separate two IQ clouds, at any chain rotation, and what an inseparable pair refuses."""

    @pytest.mark.parametrize("rotation", [0.0, 35.0, 120.0, -153.7, 206.3, -20.0])
    def test_discrimination_returns_a_rotation_the_instrument_accepts(self, rotation):
        """In [0, 360), because the hardware says so and says it late.

        `np.angle` returns (-180, 180], so half of all readout chains produce a negative
        rotation — which the compiler refuses with "the hardware requires it to be between
        0 and 360", in *every schedule after* the one that wrote it rather than in the
        routine at fault.
        """
        fitted = fit_readout_discrimination(*_clouds(rotation))
        assert 0.0 <= fitted["acq_rotation"] < 360.0

    @pytest.mark.parametrize("rotation", [0.0, 35.0, 120.0, -153.7, 250.0])
    def test_discrimination_separates_the_clouds_at_any_chain_rotation(self, rotation):
        """The rotation is what makes one real threshold enough.

        Whatever angle the chain leaves the clouds at, rotating by the direction between
        their centres puts all the state information in the real part — so the fitted rule
        assigns nearly every shot correctly.
        """
        ground, excited = _clouds(rotation)
        fitted = fit_readout_discrimination(ground, excited)

        turn = np.exp(-1j * np.deg2rad(fitted["acq_rotation"]))
        assigned_ground = np.real(ground * turn) >= fitted["acq_threshold"]
        assigned_excited = np.real(excited * turn) >= fitted["acq_threshold"]
        assert assigned_ground.mean() < 0.01
        assert assigned_excited.mean() > 0.99
        assert fitted["assignment_fidelity"] > 0.98

    def test_discrimination_refuses_clouds_it_cannot_separate(self):
        """A readout that does not resolve the qubit has no discriminator.

        Fitting one anyway would put a confident, meaningless threshold on the device, and
        every count afterwards would be a coin toss with no symptom.
        """
        rng = np.random.default_rng(3)
        same = rng.normal(0, 0.25, 500) + 1j * rng.normal(0, 0.25, 500)
        other = rng.normal(0, 0.25, 500) + 1j * rng.normal(0, 0.25, 500)
        with pytest.raises(FitError, match="no separation at all"):
            fit_readout_discrimination(same, other)

    def test_discrimination_needs_single_shots(self):
        with pytest.raises(FitError, match="needs single shots"):
            fit_readout_discrimination(np.array([1 + 1j]), np.array([2 + 2j]))

    def test_discrimination_refuses_a_readout_at_chance_however_many_shots(self):
        """The significance test alone is not a usability test, and cannot be.

        Its threshold carries a 1/sqrt(n), so averaging more shots *lowers* the bar: on the
        August 2026 B chip the same chance-level readout was refused by
        `readout_discrimination` at 2000 shots and accepted by `readout_operating_point` at
        300, which then wrote the operating point. Whether single shots can be assigned is
        the question a readout has to answer, and it does not improve with averaging.

        The clouds here are separated by a quarter of their own scatter — a real difference
        of means at any decent shot count, and useless for assigning a shot.
        """
        rng = np.random.default_rng(11)
        for shots in (300, 2000, 20000):
            ground = rng.normal(0, 1.0, shots) + 1j * rng.normal(0, 1.0, shots)
            excited = (
                ground * 0
                + rng.normal(0.25, 1.0, shots)
                + 1j * rng.normal(0, 1.0, shots)
            )
            with pytest.raises(FitError, match="assigned correctly") as raised:
                fit_readout_discrimination(ground, excited)
            assert "60%" in str(raised.value), str(raised.value)

    def test_discrimination_still_accepts_a_poor_but_usable_readout(self):
        """The floor must not refuse a readout worth optimising — that is the node's job."""
        rng = np.random.default_rng(12)
        shots = 4000
        ground = rng.normal(0, 1.0, shots) + 1j * rng.normal(0, 1.0, shots)
        excited = rng.normal(3.0, 1.0, shots) + 1j * rng.normal(0, 1.0, shots)

        fitted = fit_readout_discrimination(ground, excited)

        assert 0.6 < fitted["assignment_fidelity"] < 0.99


def _chevron_grid(
    durations: np.ndarray,
    rows: int,
    resonant: int,
    round_trip: float = 110e-9,
    reference: float = 1.0,
    swing: float = 1.0,
    ripple: float = 0.0,
) -> np.ndarray:
    """A chevron shaped the way one measures.

    The off-resonant rows sit at *reference* — the level the readout gives when the
    flux pulse drove no exchange, so the pair is still ``|11>``. The resonant row
    leaves that level, reaches full transfer half a round trip in, and comes back.

    *swing* may be negative, which is the case that matters: whether the control's
    ``|z|`` rises or falls as it empties depends on which side of the resonator's line
    the readout sits, and nothing in a chevron says which.

    *ripple* adds the beat against neighbouring transitions that makes the far side of
    the round trip lumpy — shaped to vanish at zero duration so it does not move the
    reference.
    """
    angle = 2 * np.pi * durations / round_trip
    departure = (1 - np.cos(angle)) / 2
    beat = (1 - np.cos(5 * angle)) / 2
    row = reference - swing * departure + ripple * beat
    grid = np.full((rows, durations.size), reference)
    grid[resonant, :] = row
    return grid


class TestTwoQubitFits:
    """The CZ chevron's operating point, and the conditional-phase fringe's offset."""

    @pytest.mark.parametrize("swing", [1.0, -1.0], ids=["falls", "rises"])
    def test_chevron_locates_the_operating_point(self, swing):
        """And does so whichever way the readout's magnitude moves.

        The polarity is not knowable from a chevron: `resonator_spectroscopy` leaves the
        readout on the ground-state resonance, where an *excited* qubit reflects less, so
        an emptying control can read either up or down depending on the chain. Hunting an
        extreme picked the wrong one and returned half the round trip — a complete
        population swap, which is a perfectly good gate and not a CZ.
        """
        amplitudes = np.linspace(0.3, 0.5, 5)
        durations = np.linspace(10e-9, 200e-9, 20)
        grid = _chevron_grid(durations, rows=5, resonant=2, swing=swing)

        fitted = fit_chevron(amplitudes, durations, grid.reshape(-1))
        assert fitted["cz_amplitude"] == pytest.approx(amplitudes[2], abs=0.03)
        assert fitted["cz_duration"] == pytest.approx(110e-9, rel=0.05)

    @pytest.mark.parametrize("swing", [1.0, -1.0], ids=["falls", "rises"])
    def test_chevron_reads_past_a_wiggle_in_the_far_side(self, swing):
        """The round trip, not a bump on the way through it.

        The exchange beats against the transitions the flux pulse sits near, so the far
        side of the round trip is not smooth. A turning-point walk stops on that bump,
        still deep in ``|02>`` and at half the right duration.
        """
        amplitudes = np.linspace(0.3, 0.5, 5)
        durations = np.linspace(10e-9, 200e-9, 20)
        grid = _chevron_grid(
            durations, rows=5, resonant=2, swing=swing, ripple=0.05 * swing
        )

        fitted = fit_chevron(amplitudes, durations, grid.reshape(-1))
        assert fitted["cz_duration"] == pytest.approx(110e-9, rel=0.05), (
            "half of this is a swap, and it is what a turning-point walk returned"
        )

    def test_chevron_needs_a_full_grid(self):
        with pytest.raises(FitError, match="needs 9 points"):
            fit_chevron(np.arange(3.0), np.arange(3.0), np.zeros(4))

    @pytest.mark.parametrize("conditional", [180.0, 150.0, 95.0, 220.0])
    def test_conditional_phase_recovers_the_offset_between_two_fringes(
        self, conditional
    ):
        phases = np.linspace(0.0, 360.0, 73)
        ground = 0.5 + 0.4 * np.cos(np.deg2rad(phases))
        excited = 0.5 + 0.4 * np.cos(np.deg2rad(phases - conditional))

        fitted = fit_conditional_phase(phases, ground, excited)

        assert fitted["conditional_phase"] == pytest.approx(conditional, abs=0.5)
        assert fitted["phase_correction"] == pytest.approx(180.0 - conditional, abs=0.5)

    def test_conditional_phase_ignores_a_phase_common_to_both_fringes(self):
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

    def test_conditional_phase_refuses_a_flat_fringe(self):
        phases = np.linspace(0.0, 360.0, 21)
        with pytest.raises(FitError, match="flat"):
            fit_conditional_phase(phases, np.full(21, 1.0), np.full(21, 1.0))


class TestTheSharedMachinery:
    """signal_of, align, require_in_range, and require_positive — the machinery every fit above is built on."""

    def test_signal_of_reads_an_xarray_dataset(self):
        dataset = xr.Dataset({"y": ("x", [1.0, 2.0, 3.0])})
        assert list(signal_of(dataset)) == [1.0, 2.0, 3.0]

    def test_signal_of_takes_the_magnitude_of_complex_iq(self):
        dataset = xr.Dataset({"y": ("x", np.array([3 + 4j, 0 + 1j]))})
        assert list(signal_of(dataset)) == [5.0, 1.0]

    def test_signal_of_refuses_an_empty_acquisition(self):
        with pytest.raises(FitError, match="no data points"):
            signal_of(np.array([]))

    def test_signal_of_refuses_an_all_nan_acquisition(self):
        """The dummy cluster returns NaN; that must fail loudly, not fit to zero."""
        with pytest.raises(FitError, match="no finite data points"):
            signal_of(np.array([np.nan, np.nan]))

    def test_signal_of_refuses_a_dataset_with_no_variables(self):
        with pytest.raises(FitError, match="no data variables"):
            signal_of(xr.Dataset())

    def test_signal_of_repairs_a_few_nans_around_finite_data(self):
        values = signal_of(np.array([1.0, np.nan, 3.0, 4.0]))
        assert np.all(np.isfinite(values))

    def test_align_trims_to_the_shorter_axis(self):
        x, y = align(np.arange(10.0), np.arange(6.0), what="test")
        assert len(x) == len(y) == 6

    def test_align_refuses_too_few_points(self):
        with pytest.raises(FitError, match="at least 4 points"):
            align(np.arange(2.0), np.arange(2.0), what="test")

    def test_require_in_range_accepts_a_reversed_window(self):
        assert require_in_range(5.0, 10.0, 0.0, what="x") == 5.0

    def test_require_in_range_refuses_a_non_finite_value(self):
        with pytest.raises(FitError, match="not finite"):
            require_in_range(float("nan"), 0.0, 1.0, what="x")

    def test_require_positive_refuses_zero_and_negatives(self):
        assert require_positive(1.0, what="x") == 1.0
        with pytest.raises(FitError, match="not physical"):
            require_positive(0.0, what="x")
        with pytest.raises(FitError, match="not physical"):
            require_positive(-1.0, what="x")


class TestTheFitSummary:
    """The sweep a fit ships for the dashboard to draw (RFC 0006 §7)."""

    def test_it_carries_two_traces_over_one_axis(self):
        summary = fit_summary(
            [1.0, 2.0, 3.0], [1.0, 0.5, 0.25], [1.1, 0.55, 0.2], x_label="depth"
        )
        assert summary["x"] == [1.0, 2.0, 3.0]
        assert summary["measured"] == [1.0, 0.5, 0.25]
        assert summary["fitted"] == [1.1, 0.55, 0.2]
        assert summary["x_label"] == "depth"
        assert summary["x_scale"] == "linear"

    def test_a_long_sweep_is_thinned_to_the_ceiling_keeping_both_ends(self):
        x = np.linspace(0.0, 1.0, 5000)
        summary = fit_summary(x, x, x)
        assert len(summary["x"]) == MAX_FIT_POINTS
        assert summary["x"][0] == 0.0
        assert summary["x"][-1] == 1.0
        assert len(summary["measured"]) == len(summary["fitted"]) == MAX_FIT_POINTS

    def test_a_sweep_inside_the_ceiling_keeps_every_point(self):
        summary = fit_summary(np.arange(41.0), np.arange(41.0), np.arange(41.0))
        assert len(summary["x"]) == 41

    def test_a_non_finite_point_is_dropped_rather_than_sent(self):
        """JSON has no NaN, so one would fail to unmarshal instead of showing a gap."""
        summary = fit_summary(
            [1.0, 2.0, 3.0], [1.0, np.nan, 3.0], [1.0, 2.0, float("inf")]
        )
        assert summary["x"] == [1.0]
        assert json.dumps(summary)

    def test_the_traces_stay_the_same_length_when_one_is_short(self):
        summary = fit_summary([1.0, 2.0, 3.0], [1.0, 2.0], [1.0, 2.0, 3.0])
        assert len(summary["x"]) == len(summary["measured"]) == 2

    def test_every_fit_that_produces_one_labels_its_axes(self):
        """A tick with no unit is a number nobody can act on."""
        x = np.linspace(0.0, 0.5, 41)
        summary = fit_rabi(x, np.cos(2 * np.pi * 2.0 * x))["fit"]
        assert summary["x_label"] and summary["y_label"]

    def test_rb_asks_for_a_log_axis_because_its_depths_double(self):
        depths = np.array([1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0])
        survival = 0.9 * np.power(0.99, depths) + 0.05
        assert fit_rb_decay(depths, survival)["fit"]["x_scale"] == "log"


class TestFineAmplitudeNeedsRealContrast:
    """`amp180` is the amplitude every X pulse uses, so this fit must not guess it.

    The demodulated sweep is ``sin(n*delta)`` — bounded at one. It is reached by
    dividing by the measured |0>-|1> contrast, so when the readout is not resolving the
    qubit the divisor collapses, the quotient explodes, and the slope through it is
    fitted from noise. Twice on hardware, the second time writing an `amp180` that
    broke every node after it.
    """

    #: Peak |demodulated| from the two hardware runs, and from the simulated chip for
    #: scale. The sim spans 0.108 to 0.659 across the loop suite.
    def _sweep(self, reach):
        counts = np.arange(1, 26, dtype=float)
        rng = np.random.default_rng(4)
        return counts, reach * rng.uniform(-1.0, 1.0, counts.size)

    @pytest.mark.parametrize("reach", [8.9, 144.3])
    def test_it_refuses_a_sweep_past_its_own_bound(self, reach):
        counts, demodulated = self._sweep(reach)
        # Reconstruct what the routine hands over: signal = centre + demodulated*(c/2)*(-1)^n
        contrast, centre = 2.0, 0.5
        signal = centre + demodulated * (contrast / 2) * np.power(-1.0, counts)
        with pytest.raises(FitError, match="not resolving the qubit"):
            fit_fine_amplitude(
                counts, signal, 0.03, centre - contrast / 2, centre + contrast / 2
            )

    def test_it_accepts_the_range_the_simulated_chip_reaches(self):
        """0.66 is the worst the sim shows, and a refinement's slope is small by design."""
        counts = np.arange(1, 26, dtype=float)
        delta = 0.02
        demodulated = np.sin(counts * delta)
        contrast, centre = 2.0, 0.5
        signal = centre + demodulated * (contrast / 2) * np.power(-1.0, counts)
        fitted = fit_fine_amplitude(
            counts, signal, 0.03, centre - contrast / 2, centre + contrast / 2
        )
        assert fitted["error_per_pulse"] == pytest.approx(delta, rel=0.1)


class TestRabiNeedsAVisibleOscillation:
    """`amp180` is the amplitude of every X pulse, and a wrong one is self-perpetuating.

    A dead X gate guarantees the next Rabi sweep is flat, which writes another dead
    amplitude. On this chip a first sweep through a starved readout wrote 0.0134 where
    the calibrated value was 0.5683, and six runs later the X pulse was still rotating
    five degrees instead of 180 — with every 0-1 measurement in the graph blind as a
    result, and nothing failing to say so.
    """

    def test_a_real_oscillation_is_accepted(self):
        amp180 = 0.21
        amplitudes = np.linspace(0.0, 0.5, 81)
        signal = 0.5 * np.cos(2 * np.pi * amplitudes / (2 * amp180)) + 0.5
        fitted = fit_rabi(amplitudes, signal + _noise(len(amplitudes), 0.01))
        assert fitted["amp180"] == pytest.approx(amp180, rel=0.05)

    def test_a_sweep_no_taller_than_its_noise_is_refused(self):
        """Contrast 8e-5 against 5e-5 of scatter — what the chip actually returned."""
        rng = np.random.default_rng(11)
        amplitudes = np.linspace(0.0, 0.5, 41)
        # a token oscillation buried in noise, as the flat hardware sweeps were
        signal = 0.008 + 4e-5 * np.cos(2 * np.pi * amplitudes / 0.03)
        signal = signal + rng.normal(0.0, 5e-5, amplitudes.size)
        with pytest.raises(FitError, match="no pi amplitude"):
            fit_rabi(amplitudes, signal)


class TestCoherenceAndRamseyNeedAVisibleCurve:
    """Both write to the device — T1/T2 to the report a drift check reads, ramsey to f01.

    `require_in_range` allows a time constant up to ten times the window, which lets a
    fit through flat noise return a plausible-looking coherence time. This chip returned
    T1 = 169 us from a *rising* curve inside a 100 us window, and a ramsey detuning of
    173 kHz from a sweep with no fringe in it — which moved f01.
    """

    def test_a_coherence_fit_through_flat_noise_is_refused(self):
        rng = np.random.default_rng(17)
        delays = np.linspace(0.0, 100e-6, 41)
        flat = 0.00815 + rng.normal(0.0, 9e-5, delays.size)
        with pytest.raises(FitError, match="never seen in this window"):
            fit_t1(delays, flat)

    def test_a_real_decay_is_still_accepted_through_the_same_noise(self):
        """5% noise on a decay that fits the window: 20x, well clear of the 3x floor."""
        rng = np.random.default_rng(18)
        delays = np.linspace(0.0, 100e-6, 41)
        signal = exponential_decay(delays, 1.0, 30e-6, 0.05)
        fitted = fit_t1(delays, signal + rng.normal(0.0, 0.05, delays.size))
        assert fitted["t1"] == pytest.approx(30e-6, rel=0.3)

    def test_a_ramsey_with_no_fringe_is_refused(self):
        rng = np.random.default_rng(19)
        delays = np.linspace(4e-9, 10e-6, 41)
        flat = 0.00817 + rng.normal(0.0, 8e-5, delays.size)
        with pytest.raises(FitError, match="no detuning to take from it"):
            fit_ramsey(delays, flat, 1e6)


class TestOnlyARowWithALineMayJudgeTheOthers:
    """`fit_spectroscopy_power` compares broadening against its narrowest row.

    That reference has been wrong three times. Dropping rows that did not converge was
    not enough; dropping rows narrower than the sweep step was not either. What survives
    both is a row that converges, clears the step, and still shows nothing — and being
    weak it is the *narrowest*, so `MAX_BROADENING` then rejects every row that does show
    the line, and the answer comes from the one row with no line in it.

    Measured on the simulated chip through the loop path, at a 40 MHz window centred on a
    line the search had just located:

        drive   linewidth    reach
        0.005     0.01 MHz     2.7   dropped: narrower than the step
        0.010     0.01 MHz     2.7   dropped: narrower than the step
        0.020     3.11 MHz     2.6   became the reference
        0.040    90.97 MHz     3.6   rejected as broadened
        0.080   197.83 MHz    17.0   rejected as broadened

    Widening the window does not fix it: at 200 MHz a 6.24 MHz row took the same role and
    rejected three good rows. Only a row that shows a line may be the reference.
    """

    CENTRE = 5.214e9
    FREQUENCIES = [5.214e9 - 20e6 + 1e6 * i for i in range(41)]

    def _row(self, linewidth, amplitude, noise, seed):
        import numpy as np

        f = np.asarray(self.FREQUENCIES)
        half = linewidth / 2.0
        curve = amplitude * half**2 / ((f - self.CENTRE) ** 2 + half**2)
        return 0.02 + curve + np.random.default_rng(seed).normal(0.0, noise, f.size)

    def test_a_weak_row_cannot_become_the_broadening_reference(self):
        import numpy as np
        from qpi_driver.tuners.fitting import fit_spectroscopy_power

        # A faint narrow row that clears the 1 MHz step, and two rows with real lines.
        rows = np.vstack(
            [
                self._row(3.1e6, 0.0006, 2.0e-4, 1),
                self._row(60e6, 0.05, 2.0e-4, 2),
                self._row(120e6, 0.20, 2.0e-4, 3),
            ]
        )
        fitted = fit_spectroscopy_power([0.02, 0.04, 0.08], self.FREQUENCIES, rows)

        assert fitted["drive_amplitude"] in (0.04, 0.08), (
            "the answer came from the row with no line in it"
        )
        assert fitted["clock_freq_01"] == pytest.approx(self.CENTRE, abs=5e6)
        assert fitted["reach"] >= 5.0

    def test_a_sweep_where_no_power_shows_a_line_is_refused(self):
        """And says so as a power sweep, rather than reporting the least bad row."""
        import numpy as np
        from qpi_driver.tuners.fitting import fit_spectroscopy_power

        rows = np.vstack([self._row(3.1e6, 0.0006, 2.0e-4, seed) for seed in (4, 5, 6)])
        with pytest.raises(FitError, match="showed a line above its own scatter"):
            fit_spectroscopy_power([0.02, 0.04, 0.08], self.FREQUENCIES, rows)
