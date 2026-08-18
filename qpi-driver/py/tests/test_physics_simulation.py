"""Routines against simulated physics (RFC 0004 §7, tier 3).

Tier 1 fits data generated from the fit's own analytic form — a decaying cosine
in, a decaying cosine fitted — which proves the optimiser converges but not that
the model is the right one. Tier 2 proves a schedule compiles, and the dummy
cluster returns no data at all.

These close the gap. The acquisition comes from diagonalising a real transmon
Hamiltonian (`scqubits`) and integrating the Lindblad master equation (`qutip`),
then the routine has to recover the parameter the simulator was built with.
That makes them the first tests where a routine can be wrong in a way the other
tiers cannot see: a schedule that does not produce the physics its fit assumes
still compiles, and still fits its own synthetic data.

Needs the `sim` extra and nothing else — no scheduler extra. The
routines, the fits and the DAG are numpy and scipy; the schedulers are needed to
*run* a schedule, and here the simulator supplies the acquisition instead. That
is why `make test-py-sim` syncs `--extra sim` alone:

    make test-py-sim
"""

import numpy as np
import pytest
from qpi_driver.tuners.fitting import fit_rabi
from qpi_driver.tuners.base.device import read_path
from tests.utils.simulation import SimulatedTuner
from qpi_driver.tuners.base.config import RoutineConfig
from qpi_driver.tuners.base.routines import RoutineError
from qpi_driver.tuners.base.sweep import Sweep
from qpi_driver.tuners.fitting import FitError, fit_rb_decay
from qpi_driver.tuners.routines import all_routines

pytest.importorskip("scqubits", reason="needs the [sim] extra")
qutip = pytest.importorskip("qutip", reason="needs the [sim] extra")

from tests.utils.simulation import (  # noqa: E402
    GHZ,
    SimulatedBackend,
    StubBackend,
    TransmonSimulator,
    _rxy_qobj,
    device_for,
)

pytestmark = pytest.mark.scqubits

# The tolerances below are deliberately tight. A loose one makes a test that
# passes without discriminating: fitting a *Gaussian* decay to this simulator's
# exponential relaxation still recovers T1 to within 7%, so a 15% tolerance
# would accept the wrong physical model — which is the one thing these tests
# exist to reject. Each is set from the measured accuracy of the correct model,
# with margin, and no looser.


@pytest.fixture(scope="module")
def simulator() -> TransmonSimulator:
    return TransmonSimulator()


def routine(name: str):
    return next(r for r in all_routines() if r.name == name)


def test_the_simulated_transmon_is_a_plausible_one(simulator):
    """Check the model before trusting anything fitted from it.

    Without this, a bad simulator would be read as a bad routine.
    """
    assert 4.0 < simulator.f01 < 7.0, "f01 should be a normal transmon frequency"
    assert -0.4 < simulator.anharmonicity < -0.15, (
        "a transmon's anharmonicity is a few hundred MHz, and negative"
    )


@pytest.fixture
def resonator(simulator):
    return simulator.resonator("q0", configured_ghz=6.0)


class TestTheReadoutResonator:
    """The two resonator routines are exercised end to end in `test_calibration_loop`,
    where they sweep a compiled schedule. What belongs here is the model underneath:
    these are the properties those routines are entitled to assume, and each one is
    a number a fit of the simulated data has to come back with.
    """

    def test_the_resonator_response_is_lorentzian_in_its_own_linewidth(self, resonator):
        """Half power at half a linewidth away — which is what `kappa` *means*.

        `fit_resonator_spectroscopy` fits a Lorentzian and reports its width as the
        linewidth, so if the model's response were some other lineshape the routine
        would report a number that is not the parameter it names.
        """
        centre = resonator.resonance_ghz(amplitude=0.0)
        half = resonator.linewidth_ghz / 2.0

        assert resonator.response(centre, 0.0) == pytest.approx(1.0)
        assert resonator.response(centre + half, 0.0) == pytest.approx(0.5)
        assert resonator.response(centre - half, 0.0) == pytest.approx(0.5)
        # And far off it is gone, rather than merely smaller: a readout at the wrong
        # frequency has to lose the signal, not attenuate it.
        assert resonator.response(centre + 20 * half, 0.0) < 0.01

    def test_readout_power_walks_the_resonance_from_dressed_to_bare(self, resonator):
        """Monotone, and bounded by one dispersive shift — which is punchout.

        Monotone because `fit_punchout` reads the crossing of the halfway point, and a
        curve that wandered would give it several. Bounded because the pull it is
        watching collapse is the qubit's, and there is only one of those to lose.
        """
        amplitudes = np.linspace(0.0, 4.0, 40)
        frequencies = np.array([resonator.resonance_ghz(a) for a in amplitudes])

        assert np.all(np.diff(frequencies) <= 0), "the resonance should only walk down"
        total = float(frequencies[0] - frequencies[-1])
        shift = resonator.dispersive_shift_ghz
        assert 0.95 * shift < total < shift
        # Half of it by the crossover amplitude, which is what names that parameter.
        midpoint = resonator.resonance_ghz(resonator.punchout_amplitude)
        assert midpoint == pytest.approx(
            frequencies[0] - resonator.dispersive_shift_ghz / 2
        )

    def test_the_readout_response_scales_with_the_power_it_is_driven_with(
        self, resonator
    ):
        """More power buys more signal, and punch-through is what it costs.

        Without the first half there is nothing to trade: a response that ignored the
        drive amplitude would make readout power purely harmful, since the only thing
        left for it to do is collapse the dispersive pull. Then the optimum power is
        always the smallest one, and `readout_amplitude_two_state` has no question to
        answer.

        The scaling lives in the coordinator's amplifier chain rather than here, so what
        this asserts is the other half — that the pull really does collapse, and that
        the two effects therefore pull opposite ways.
        """
        strong, weak = 1.4, 0.2
        assert resonator.reflection(
            resonator.resonance_ghz(strong, 0), strong, 0
        ) == pytest.approx(
            resonator.reflection(resonator.resonance_ghz(weak, 0), weak, 0)
        )

        def gap(amplitude: float) -> float:
            return abs(
                resonator.resonance_ghz(amplitude, 0)
                - resonator.resonance_ghz(amplitude, 1)
            )

        assert gap(strong) < 0.5 * gap(weak), (
            "the states should be harder to tell apart at high power, or there is no "
            "cost to turning it up"
        )

    def test_a_chip_that_states_its_resonators_is_believed(self, simulator):
        """Otherwise the resonator sits wherever the readout clock is configured.

        Both halves matter. The fallback is what lets `is_simulated` work against any
        device file — a resonator where the config says means spectroscopy confirms it.
        The override is what lets a test put one somewhere the config does not expect,
        without which the routine cannot come out wrong.
        """
        import dataclasses

        stated = dataclasses.replace(simulator, resonator_frequencies_ghz={"q0": 6.004})

        assert stated.resonator("q0", configured_ghz=6.0).frequency_ghz == 6.004
        assert stated.resonator("q1", configured_ghz=6.01).frequency_ghz == 6.01


class TestSpectroscopy:
    """Qubit spectroscopy against a simulator whose f01 is deliberately off, and what a scan of the wrong window finds instead."""

    def test_qubit_spectroscopy_finds_the_transmons_real_f01(self, simulator):
        """The line comes from a driven, damped steady state — not from a Lorentzian.

        The device is handed an f01 that is 3 MHz off, so the routine has to scan
        around it and find the true one rather than being given it.
        """
        sweep = Sweep("q0")
        device = device_for(simulator)
        spectroscopy = routine("qubit_spectroscopy")
        config = RoutineConfig(params={"span": 30e6, "points": 61})

        spectroscopy.build_schedule("q0", device, config, StubBackend(), sweep)
        acquisition = simulator.qubit_spectroscopy(
            sweep["frequencies"], sweep["drive_amps"]
        )
        fitted = spectroscopy.analyse(acquisition, "q0", device, config, sweep)

        assert fitted["clock_freq_01"] == pytest.approx(simulator.f01 * GHZ, abs=5e4)
        # Chosen from the sweep, not from the config: the master equation broadens the
        # line at the top of the range and buries it in noise at the bottom, so a power
        # in between has to win on its own.
        assert fitted["drive_amplitude"] in sweep["drive_amps"]

        # Applying it moves the device onto the true frequency.
        spectroscopy.apply(device, "q0", fitted)
        assert device.get_element("q0").clock_freqs.f01 == pytest.approx(
            simulator.f01 * GHZ, abs=5e4
        )

    def test_a_configured_f01_hundreds_of_mhz_out_is_still_located(self, simulator):
        """The configured f01 is a prior, so being far wrong has to widen the search.

        This is the node whose job is to measure f01. A design value, or one measured at
        a different flux bias, is routinely a few hundred MHz from where the chip is —
        and before this the sweep only looked +/-20 MHz around whatever it was handed.
        On hardware that read as six runs of "no drive power resolved a line", and left
        the operator to supply by hand the number the node exists to produce.

        Against the wide pass directly, and at its default grid. Driving `measure` end to
        end would make this depend on the narrow pass *refusing* first, which is a
        property of one noise realisation rather than of the search.

        250 MHz rather than the 302 it happened on: the default span is 600 MHz because a
        module reaches only +/-500 MHz either side of its LO, so 302 needs `search_span`.
        """
        import dataclasses

        from qpi_driver.tuners.base.device import write_path

        # Its own copy, so the several hundred shot-noise draws a wide search costs do
        # not shift what the module-scoped simulator hands the tests after this one.
        # `replace` rebuilds the RNG from the same seed.
        chip = dataclasses.replace(simulator)
        device = device_for(chip)
        node = routine("qubit_spectroscopy")
        sweep = Sweep("q0")
        true_f01 = chip.f01 * GHZ
        write_path(device.get_element("q0"), "clock_freqs.f01", true_f01 - 250e6)

        found, width = node._search(
            "q0", device, RoutineConfig(params={}), SimulatedBackend(chip), 300.0, sweep
        )

        # Within a step of the 2 MHz grid. Locating is all this pass owes; the narrow
        # sweep it points at is what has to land on the line.
        assert found == pytest.approx(true_f01, abs=2e6)
        # And roughly how wide, which is what sizes that narrow sweep. Counted in bins, so
        # one step is the floor for a line the search grid cannot resolve.
        assert width >= 2e6

    def test_a_search_that_finds_nothing_says_so_rather_than_fitting_noise(
        self, simulator
    ):
        """Widening is not licence to report whatever the widest window fitted.

        A qubit outside even the search window has to end in a refusal that names the
        range swept, not in a frequency — and the wide pass never writes anything itself,
        so its candidate still has to survive a fine sweep.
        """
        import dataclasses

        from qpi_driver.tuners.base.device import write_path

        chip = dataclasses.replace(simulator)
        device = device_for(chip)
        node = routine("qubit_spectroscopy")
        sweep = Sweep("q0")
        write_path(device.get_element("q0"), "clock_freqs.f01", chip.f01 * GHZ - 3e9)

        with pytest.raises(RoutineError, match="nothing above the noise between"):
            node._search(
                "q0",
                device,
                RoutineConfig(params={"search_points": 101}),
                SimulatedBackend(chip),
                300.0,
                sweep,
            )

    def test_scanning_the_wrong_window_cannot_invent_the_right_answer(self, simulator):
        """A sweep that does not bracket the line gives a bounded wrong answer, or none.

        This is the common mistake on hardware, and it is worth being precise about
        what the routine guarantees. There is no direct check for "you scanned the
        wrong range" — the routine cannot know where the qubit was supposed to be.
        Two things bound the damage instead, and either is an acceptable outcome:

        - The fit refuses. Flat noise fits a spurious Lorentzian, but a spurious one
          is narrow, and a line narrower than the sweep's own step size is one that
          was never measured — which is what the resolution guard rejects.
        - The answer is confined to the window actually scanned, so the routine can
          never report a frequency it did not look at, and the error is bounded by
          the operator's own sweep rather than by the optimiser's imagination.
        """
        sweep = Sweep("q0")
        spectroscopy = routine("qubit_spectroscopy")
        device = device_for(simulator)
        offset = 500e6  # nowhere near the real line
        config = RoutineConfig(
            params={
                "centre_frequency": simulator.f01 * GHZ + offset,
                "span": 20e6,
                "points": 41,
            }
        )

        spectroscopy.build_schedule("q0", device, config, StubBackend(), sweep)
        acquisition = simulator.qubit_spectroscopy(sweep["frequencies"])
        scanned = sweep["frequencies"]

        try:
            fitted = spectroscopy.analyse(acquisition, "q0", device, config, sweep)
        except (FitError, RoutineError):
            return  # refusing outright is the other acceptable outcome

        assert min(scanned) <= fitted["clock_freq_01"] <= max(scanned), (
            "the fit escaped the window it measured"
        )
        assert abs(fitted["clock_freq_01"] - simulator.f01 * GHZ) > offset / 2, (
            "a window 500 MHz away should not somehow land on the true f01"
        )


class TestTimeDomainRoutines:
    """Rabi, T1, T2 echo, and Ramsey, recovered from simulated dynamics rather than a closed-form fit target."""

    def test_rabi_finds_the_pi_pulse_from_simulated_dynamics(self, simulator):
        """The oscillation emerges from integrating the drive, not from a cosine."""
        sweep = Sweep("q0")
        rabi = routine("rabi")
        device = device_for(simulator)
        config = RoutineConfig(params={"amplitudes": list(np.linspace(0.0, 0.5, 41))})

        rabi.build_schedule("q0", device, config, StubBackend(), sweep)
        acquisition = simulator.rabi(sweep["amplitudes"])
        fitted = rabi.analyse(acquisition, "q0", device, config, sweep)

        # The simulator is built so a pi rotation lands at 0.2 in the sweep's units.
        assert fitted["amp180"] == pytest.approx(0.2, rel=0.03)

    def test_t1_recovers_the_simulated_relaxation_time(self, simulator):
        """The decay comes from a collapse operator, not from an exponential."""
        sweep = Sweep("q0")
        t1 = routine("t1")
        device = device_for(simulator)
        config = RoutineConfig(params={"delays": list(np.linspace(0.0, 80e-6, 25))})

        t1.build_schedule("q0", device, config, StubBackend(), sweep)
        acquisition = simulator.t1(sweep["delays"])
        fitted = t1.analyse(acquisition, "q0", device, config, sweep)

        # The correct model recovers T1 to 0.01%; a Gaussian decay fitted to this
        # same data lands 7% out, so 2% is what makes this test discriminating
        # rather than merely satisfied.
        assert fitted["t1"] == pytest.approx(simulator.t1_ns * 1e-9, rel=0.02)

    def test_t2_echo_recovers_the_simulated_dephasing_time(self, simulator):
        """A Hahn echo, evolved through both halves with the refocusing pulse between."""
        sweep = Sweep("q0")
        t2 = routine("t2_echo")
        device = device_for(simulator)
        config = RoutineConfig(params={"delays": list(np.linspace(0.0, 60e-6, 25))})

        t2.build_schedule("q0", device, config, StubBackend(), sweep)
        acquisition = simulator.t2_echo(sweep["delays"])
        fitted = t2.analyse(acquisition, "q0", device, config, sweep)

        assert fitted["t2"] == pytest.approx(simulator.t2_ns * 1e-9, rel=0.05)

    def test_ramsey_measures_the_deliberate_detuning_and_reports_no_residual(
        self, simulator
    ):
        """The fringe is the artificial detuning; the qubit itself is on resonance.

        So the *residual* detuning is the real assertion — it is the number that
        gets written to the device, and it should be near zero here.
        """
        sweep = Sweep("q0")
        ramsey = routine("ramsey")
        device = device_for(simulator)
        detuning = 1e6
        config = RoutineConfig(
            params={
                "delays": list(np.linspace(4e-9, 6e-6, 61)),
                "artificial_detuning": detuning,
            }
        )

        ramsey.build_schedule("q0", device, config, StubBackend(), sweep)
        acquisition = simulator.ramsey(sweep["delays"], detuning)
        fitted = ramsey.analyse(acquisition, "q0", device, config, sweep)

        assert fitted["fringe_frequency"] == pytest.approx(detuning, rel=0.01)
        # The qubit is on resonance, so the residual should be kHz, not a
        # fraction of the deliberate detuning.
        assert abs(fitted["detuning"]) < 5e3


class TestADriveOffResonance:
    """What a wrong `clock_freqs.f01` costs a gate — which until RFC 0007 §14 was nothing.

    Every gate path built its Hamiltonian with no detuning at all, so a drive
    300 MHz off resonance rotated the simulated qubit exactly as well as one on
    it. That is why no suite here could fail for the reason a real chip did.
    """

    #: What the August 2026 chip's `f01` was out by, and far enough that no
    #: calibration on it can work.
    DETUNING_HZ = 302e6

    AMPLITUDES = list(np.linspace(0.0, 0.5, 41))

    def test_a_drive_hundreds_of_mhz_off_resonance_barely_moves_the_qubit(
        self, simulator
    ):
        """A π in 20 ns is a Rabi rate of ~25 MHz, so 302 MHz tilts the rotation
        axis almost onto z: the population reaching |1> is Ω²/(Ω²+δ²), under a
        percent. The sweep has no oscillation in it to find."""
        on_resonance = simulator.rabi(self.AMPLITUDES)
        off_resonance = simulator.rabi(self.AMPLITUDES, self.DETUNING_HZ / GHZ)

        assert np.ptp(on_resonance) > 0.9
        assert np.ptp(off_resonance) < 0.1, (
            "a drive 302 MHz off resonance drove a usable rotation"
        )

    def test_a_rabi_fit_refuses_the_sweep_that_drove_nothing(self, simulator):
        """Refusing is the whole point: an `amp180` read off this would be noise,
        and every later X pulse would play it."""
        sweep = Sweep("q0")
        rabi = routine("rabi")
        device = device_for(simulator)
        config = RoutineConfig(params={"amplitudes": self.AMPLITUDES})

        rabi.build_schedule("q0", device, config, StubBackend(), sweep)
        acquisition = simulator.rabi(sweep["amplitudes"], self.DETUNING_HZ / GHZ)

        with pytest.raises(FitError):
            rabi.analyse(acquisition, "q0", device, config, sweep)

    def test_the_backend_drives_a_gate_at_the_frequency_the_device_configures(
        self, simulator
    ):
        """Read off the device, because a schedule cannot say it.

        Only a `SetClockFrequency` sweep carries a frequency; a gate carries
        none, so a backend that reads only the schedule cannot tell a chip 302 MHz
        out from one on resonance. This is the wiring that makes the two tests
        above reachable from a calibration rather than only from a direct call.
        """
        rabi = routine("rabi")
        config = RoutineConfig(params={"amplitudes": self.AMPLITUDES})

        def run(configured_f01_hz: float):
            sweep = Sweep("q0")
            device = device_for(simulator)
            device.get_element("q0").clock_freqs.f01 = configured_f01_hz
            backend = SimulatedBackend(simulator, device=device)
            schedule = rabi.build_schedule("q0", device, config, backend, sweep)
            return rabi.analyse(backend.run(schedule), "q0", device, config, sweep)

        assert run(simulator.f01 * GHZ)["amp180"] == pytest.approx(0.2, rel=0.03)
        with pytest.raises(FitError):
            run(simulator.f01 * GHZ + self.DETUNING_HZ)


class TestRandomizedBenchmarking:
    """The whole RB stack against real unitaries, composed from this package's own Clifford decomposition."""

    @pytest.mark.parametrize("error_per_gate", [0.004, 0.02])
    def test_rb_recovers_a_known_gate_error(self, simulator, error_per_gate):
        """The whole RB stack against real unitaries.

        Sequences are composed from this package's own Clifford decomposition,
        evolved as unitaries, closed with the computed recovery gate, and
        depolarised by a known amount per Clifford. If the decomposition were wrong,
        or the recovery gate did not invert the sequence, the survival probability
        would not decay from 1 and the fit would not find the injected error.
        """
        depths = [1, 2, 4, 8, 16, 32, 64]
        # 60 sequences per depth, because resolving a sub-percent error needs the
        # averaging — the simulator's shot noise falls as 1/sqrt(N), as it does on
        # hardware, so this is the same trade an experimenter makes.
        survival = simulator.randomized_benchmarking(
            depths, circuits_per_depth=60, error_per_gate=error_per_gate
        )

        # It must actually decay: a broken recovery gate sits at chance from depth 1.
        assert survival[0] > survival[-1] + 0.1

        fitted = fit_rb_decay(np.asarray(depths, dtype=float), survival)
        # A depolarising channel of strength p per Clifford is an average gate error
        # of p·(d−1)/d with d = 2.
        assert fitted["error_per_gate"] == pytest.approx(error_per_gate * 0.5, rel=0.3)

    def test_rb_survival_collapses_to_chance_without_a_recovery_gate(self, simulator):
        """The control for the test above.

        Without it, a fit returning a plausible number on flat data would look like a
        pass. Dropping the recovery gate must destroy the return to |0>.
        """
        import random

        from qpi_driver.tuners.utils.clifford import (
            clifford_to_gates,
            generate_clifford_sequence,
        )

        rng = random.Random(7)
        ground = qutip.basis(2, 0)
        survivals = []
        for depth in (1, 8, 64):
            shots = []
            for _ in range(12):
                # Deliberately no recovery gate appended.
                sequence = generate_clifford_sequence(depth, rng)
                state = ground * ground.dag()
                for clifford in sequence:
                    for theta, phi in clifford_to_gates(clifford):
                        gate = _rxy_qobj(theta, phi)
                        state = gate * state * gate.dag()
                shots.append(float(np.real((state * ground * ground.dag()).tr())))
            survivals.append(float(np.mean(shots)))

        assert max(survivals) < 0.9, (
            "without a recovery gate the sequences should not return to the ground "
            f"state, but got {survivals}"
        )


@pytest.fixture(scope="module")
def coupled():
    from qpi_driver.simulation.coupled import CoupledTransmons

    return CoupledTransmons()


def chevron_config(low, high, amplitude_points, duration_points):
    return RoutineConfig(
        params={
            "amplitudes": list(np.linspace(low, high, amplitude_points)),
            "durations": list(np.linspace(20e-9, 240e-9, duration_points)),
            "shots": 512,
        }
    )


def two_qubit_tuner(coupled):
    from tests.utils.simulation import SimulatedTuner

    return SimulatedTuner(qubits=("q0", "q1"), edges=("q0_q1",), coupled=coupled)


def angle_apart(left: float, right: float) -> float:
    """Degrees between two angles, the short way round.

    The fits report a phase in [0, 360) and the simulator in (-180, 180]; 359°
    and -1° are the same gate, and a plain subtraction says they are 360° apart.
    """
    return abs((left - right + 180.0) % 360.0 - 180.0)


class TestTwoQubits:
    """A CZ is the first thing here that one transmon cannot have: it needs a joint
    register, because entanglement is not merely absent between two independent
    density matrices, it is unrepresentable. `qpi_driver.simulation.coupled` holds
    both in one 9-dimensional space and couples them.

    These tolerances are looser than the one-qubit ones above, and the reason is
    structural rather than numerical: the exchange coupling and the
    flux-to-detuning curve are numbers chosen to describe a plausible coupler, not
    constants of a Cooper-pair box. See the module docstring of `coupled`, and
    RFC 0004 §7.
    """

    def test_the_coupled_pair_produces_a_cz_and_not_merely_a_rotation(self, coupled):
        """The conditional phase is 180° because of the avoided crossing, not by fiat."""
        assert angle_apart(coupled.cz_conditional_phase(), 180.0) < 2.0

    def test_a_bell_state_comes_out_entangled(self, coupled):
        from qpi_driver.simulation.coupled import (
            computational_block,
            concurrence,
            leakage,
        )

        density = coupled.bell_state()
        block = computational_block(density)
        populations = np.real(np.diag(block))

        # (|00> + |11>)/sqrt(2): half in |00>, half in |11>, nothing in between.
        assert populations[0] == pytest.approx(0.5, abs=0.05)
        assert populations[3] == pytest.approx(0.5, abs=0.05)
        assert populations[1] < 0.02 and populations[2] < 0.02
        assert concurrence(density) > 0.9, (
            "the pair should be very nearly maximally entangled"
        )
        assert leakage(density) < 0.01, "a calibrated CZ should not leave |2> populated"

    def test_a_cz_needs_the_coupling_to_exist(self, coupled):
        """With the qubits uncoupled the same pulse is a pair of single-qubit phases.

        The guard against a simulator that would produce a plausible CZ out of
        bookkeeping: switch off the one term that makes the gate possible and the
        conditional phase must collapse.
        """
        import dataclasses

        uncoupled = dataclasses.replace(coupled, g_mhz=0.0)
        # The same pulse, not the same gate: with g=0 there is no exchange and so no
        # round-trip duration to ask for. Holding the pulse fixed is what isolates
        # the coupling as the cause.
        phase = uncoupled.cz_conditional_phase(duration_ns=coupled.cz_duration_ns)
        assert angle_apart(phase, 0.0) < 1.0

    def test_cz_chevron_finds_the_avoided_crossing(self, coupled):
        sweep = Sweep("q0_q1")
        tuner = two_qubit_tuner(coupled)
        config = chevron_config(0.365, 0.388, 21, 45)
        routine_ = routine("cz_chevron")

        schedule = routine_.build_schedule(
            "q0_q1", tuner.device, config, tuner.backend, sweep
        )
        fit = routine_.analyse(
            tuner.backend.run(schedule), "q0_q1", tuner.device, config, sweep
        )

        assert fit["cz_amplitude"] == pytest.approx(
            coupled.resonant_amplitude, rel=0.01
        )
        assert fit["cz_duration"] / 1e-9 == pytest.approx(
            coupled.cz_duration_ns, rel=0.02
        )

    def test_cz_chevron_refuses_a_sweep_that_stepped_over_the_crossing(self, coupled):
        """The default amplitude range is far too coarse to resolve a few-MHz crossing.

        One step of the default grid moves the control by tens of MHz while the
        avoided crossing is about 4.5 MHz wide, so the surface comes back flat to
        within its noise. A peak-finder would still return a confident answer from
        it, and that answer would go to the device as a CZ.
        """
        sweep = Sweep("q0_q1")
        tuner = two_qubit_tuner(coupled)
        config = chevron_config(0.1, 0.6, 11, 11)
        routine_ = routine("cz_chevron")

        schedule = routine_.build_schedule(
            "q0_q1", tuner.device, config, tuner.backend, sweep
        )
        with pytest.raises(FitError, match="no flux amplitude drove"):
            routine_.analyse(
                tuner.backend.run(schedule), "q0_q1", tuner.device, config, sweep
            )

    def test_conditional_phase_recovers_the_gates_real_phase(self, coupled):
        sweep = Sweep("q0_q1")
        tuner = two_qubit_tuner(coupled)
        config = RoutineConfig(
            params={"phases": list(np.linspace(0.0, 360.0, 25)), "shots": 512}
        )
        routine_ = routine("conditional_phase")

        schedule = routine_.build_schedule(
            "q0_q1", tuner.device, config, tuner.backend, sweep
        )
        fit = routine_.analyse(
            tuner.backend.run(schedule), "q0_q1", tuner.device, config, sweep
        )

        assert (
            angle_apart(fit["conditional_phase"], coupled.cz_conditional_phase()) < 2.0
        )
        # A CZ this good needs almost no correcting, and the correction is the
        # complement of the phase — in degrees, which is what Rxy takes.
        assert fit["phase_correction"] == pytest.approx(
            180.0 - coupled.cz_conditional_phase(), abs=2.0
        )

    def test_conditional_phase_measures_a_gate_that_is_wrong(self, coupled):
        """A miscalibrated CZ must be reported as needing exactly its own error back.

        The one-qubit tier found that a fit which cannot measure a *good* chip
        recalibrates forever; this is the opposite failure, and the more dangerous
        one — a fit that reports every gate as fine leaves a broken CZ in service.
        """
        sweep = Sweep("q0_q1")
        import dataclasses

        detuned = dataclasses.replace(coupled, conditional_phase_offset_deg=40.0)
        tuner = two_qubit_tuner(detuned)
        config = RoutineConfig(
            params={"phases": list(np.linspace(0.0, 360.0, 25)), "shots": 512}
        )
        routine_ = routine("conditional_phase")

        schedule = routine_.build_schedule(
            "q0_q1", tuner.device, config, tuner.backend, sweep
        )
        fit = routine_.analyse(
            tuner.backend.run(schedule), "q0_q1", tuner.device, config, sweep
        )

        assert (
            angle_apart(fit["conditional_phase"], detuned.cz_conditional_phase()) < 2.0
        )
        assert fit["phase_correction"] == pytest.approx(-40.0, abs=3.0)

    def test_the_conditional_phase_survives_the_controls_own_dynamical_phase(
        self, coupled
    ):
        """The single-qubit phase over a flux pulse is huge, and must cancel.

        Detuning the control by ~280 MHz for ~110 ns winds its phase through tens of
        turns. That phase is common to both fringes and carries no information about
        the coupling, so an analysis that does not cancel it reads the winding
        instead of the gate. Lengthening the pulse changes the winding a great deal
        and the conditional phase hardly at all, which is the discriminating test.
        """
        import dataclasses

        from qpi_driver.simulation.coupled import CoupledTransmons

        phases = np.linspace(0.0, 360.0, 25)
        longer: CoupledTransmons = dataclasses.replace(coupled)

        measured = []
        for duration in (coupled.cz_duration_ns, coupled.cz_duration_ns * 3):
            fringes = longer.conditional_phase(
                phases, duration_ns=duration, averages=512
            )
            from qpi_driver.tuners.fitting import fit_conditional_phase

            fit = fit_conditional_phase(phases, fringes[:25], fringes[25:])
            measured.append(fit["conditional_phase"])
            assert (
                angle_apart(
                    fit["conditional_phase"],
                    longer.cz_conditional_phase(duration_ns=duration),
                )
                < 3.0
            )

        # Three round trips is three times the winding, and an odd multiple of a
        # half-exchange either way — so a reader of the winding could not land near
        # the gate's phase twice.
        assert angle_apart(measured[0], 180.0) < 5.0


def _acquisition(populations, clouds, **kwargs):
    from qpi_driver.simulation.coordinator import _Acquisition

    return _Acquisition(
        channel=0,
        index=0,
        protocol=kwargs.pop("protocol", "SSBIntegrationComplex"),
        bin_mode="append",
        populations=tuple(populations),
        clouds=tuple(clouds),
        **kwargs,
    )


def _coordinator(simulator, shots=4000):
    from qpi_driver.simulation.coordinator import SimulatedCoordinator

    coordinator = SimulatedCoordinator(simulator)
    coordinator._repetitions = shots
    return coordinator


class TestTheAcquisitionWhichIsWhereTheThirdLevelWasBeingLost:
    """The acquisition path for a chip with a real second excited state, and the shot noise that lands on each cloud it populates."""

    def test_a_shot_in_the_second_excited_state_lands_on_its_own_cloud(self, simulator):
        """The acquisition used to carry one excited fraction, so it could not.

        A `|2>` population was reported as one of the other two — measured, and by the
        widest possible margin: the dynamics put 98.5% of the population in `|2>` and the
        acquisition returned `|0>`'s cloud. The clouds were already derived per level;
        the sampler was not, and that is what made three-state readout unmeasurable while
        the physics underneath was already right.
        """
        clouds = (5 + 0j, 1 + 0j, 0 + 3j)
        coordinator = _coordinator(simulator)

        for level, expected in enumerate(clouds):
            shots = coordinator._single_shots(
                _acquisition([1.0 if n == level else 0.0 for n in range(3)], clouds)
            )
            assert complex(shots.mean()) == pytest.approx(expected, abs=0.05), (
                f"a shot prepared in |{level}> did not land on |{level}>'s cloud"
            )

    def test_a_mixed_state_lands_shots_on_every_cloud_it_populates(self, simulator):
        """And in the right proportions, which is what a leakage number is made of."""
        clouds = (5 + 0j, 1 + 0j, 0 + 3j)
        coordinator = _coordinator(simulator, shots=8000)
        shots = coordinator._single_shots(_acquisition([0.2, 0.5, 0.3], clouds))

        nearest = np.argmin(
            np.abs(np.asarray(shots)[:, None] - np.asarray(clouds)[None, :]), axis=1
        )
        fractions = [float(np.mean(nearest == level)) for level in range(3)]
        assert fractions == pytest.approx([0.2, 0.5, 0.3], abs=0.03)

    def test_an_averaged_acquisition_weights_every_level_it_populates(self, simulator):
        """`|2>` used to be folded into `1 - P(excited)` and so read as `|0>`.

        That is what made `f12_spectroscopy` look easy: transferring population into
        `|2>` swung the averaged point across the whole readout axis, because `|2>` was
        being reported at the ground cloud. Weighted correctly the same transfer is a
        much smaller move, which is the honest contrast an EF measurement has.
        """
        clouds = (5 + 0j, 1 + 0j, 0 + 3j)
        coordinator = _coordinator(simulator, shots=100_000)
        centre = coordinator._averaged(_acquisition([0.2, 0.5, 0.3], clouds))

        assert centre == pytest.approx(0.2 * 5 + 0.5 * 1 + 0.3 * 3j, abs=0.05)

    def test_the_draw_is_unchanged_for_a_chip_with_no_second_excited_state(
        self, simulator
    ):
        """The stream had to survive this change, or every number measured against it moves.

        Pi pulse amplitudes, coherence times and gate fidelities were all measured against
        `rng.random(n) < P(excited)`. Drawing a level from a cumulative distribution
        ordered `|1>` first, then `|0>`, consumes the same uniforms and makes the same
        decision — so a test that fails afterwards is reporting physics rather than a
        reshuffled random stream.
        """
        populations = [0.3, 0.7, 0.0]
        drawn = _coordinator(simulator)._draw_levels(_acquisition(populations, ()))

        expected = (
            np.random.default_rng(20260731).random(4000) < populations[1]
        ).astype(int)
        assert np.array_equal(drawn, expected)


@pytest.fixture
def coupler():
    from qpi_driver.simulation.coupled import TunableCoupler

    return TunableCoupler()


class TestTheTunableCoupler:
    """The tunable coupler's parking spot, and how a bias pushes a qubit's frequency around it."""

    def test_the_coupler_tunes_down_from_a_sweet_spot(self, coupler):
        """Quadratically, and only downward — which is what a flux sweet spot means.

        The same shape `FLUX_CURVATURE_GHZ` gives a qubit, for the same reason: at the
        sweet spot the first derivative of frequency with flux vanishes, so the leading
        behaviour is second order and the sign cannot change.
        """
        currents = np.linspace(0.0, 4e-3, 40)
        frequencies = np.array([coupler.frequency_at(i) for i in currents])

        assert frequencies[0] == coupler.frequency_ghz
        assert np.all(np.diff(frequencies) < 0), "the coupler should only tune down"
        # Symmetric in the sign of the current, because it is quadratic in it.
        assert coupler.frequency_at(-2e-3) == pytest.approx(coupler.frequency_at(2e-3))

    def test_an_unbiased_coupler_pushes_a_qubit_nowhere(self, coupler, simulator):
        """The push is measured from zero bias, and that is not a convenience.

        A qubit beside a coupler is always repelled; what a *bias* changes is by how
        much. Defining the shift as the difference from the unbiased push keeps the
        simulator's `f01` meaning what it always meant, so every expectation measured
        before this model existed still holds for a chip whose couplers are unparked.
        """
        assert coupler.push_ghz(simulator.f01, 0.0) == 0.0

    def test_the_push_runs_away_and_changes_sign_across_the_crossing(
        self, coupler, simulator
    ):
        """The anticrossing, which is the landmark `coupler_anticrossing` looks for.

        Level repulsion pushes a qubit *away* from the coupler, so a coupler above it
        presses it down and a coupler below it lifts it up. Tuning through the qubit
        therefore flips the sign, and the magnitude diverges on the way — that pair of
        facts is the whole signature, and neither alone would identify a crossing.
        """
        crossing = np.sqrt(
            (coupler.frequency_ghz - simulator.f01) / coupler.curvature_ghz_per_a2
        )
        below = coupler.push_ghz(simulator.f01, crossing * 0.97)
        above = coupler.push_ghz(simulator.f01, crossing * 1.02)
        far = coupler.push_ghz(simulator.f01, crossing * 0.5)

        assert below < 0 and above > 0, "the push must change sign across the crossing"
        assert abs(below) > 10 * abs(far), "and run away as the crossing is approached"

    def test_a_parked_coupler_moves_the_qubit_the_simulator_reports(self, simulator):
        """The plumbing, not the model: a current in the device config reaches the qubit.

        `bias.parking_current` has been carried, validated and applied since RFC 0004,
        and nothing in the simulator responded to it — so a routine could write any
        current at all and no measurement would contradict it. This is what makes the
        bias measurable rather than merely stored.
        """
        from qpi_driver.simulation.coordinator import SimulatedCoordinator

        unparked = SimulatedCoordinator(simulator)
        parked = SimulatedCoordinator(simulator, parking_currents={"q1_q2": 2.5e-3})

        assert unparked._qubit_frequency_hz("q1") == pytest.approx(simulator.f01 * GHZ)
        # q1 is on the edge, so it moves; q0 is not, so it does not.
        assert parked._qubit_frequency_hz("q1") != pytest.approx(simulator.f01 * GHZ)
        assert parked._qubit_frequency_hz("q0") == pytest.approx(simulator.f01 * GHZ)

    def test_a_missing_sim_extra_says_what_to_install(self, monkeypatch):
        """Otherwise the first sign is a ModuleNotFoundError from inside a fit."""
        import importlib.util

        from qpi_driver.simulation import require_simulation_deps

        real = importlib.util.find_spec
        monkeypatch.setattr(
            importlib.util,
            "find_spec",
            lambda name, *a, **k: None if name == "scqubits" else real(name, *a, **k),
        )

        with pytest.raises(ImportError) as excinfo:
            require_simulation_deps()

        message = str(excinfo.value)
        assert "scqubits" in message
        assert "qpi-driver[sim]" in message, "it has to name the extra, not the package"


class TestTheEfLadderComesOutOfThePhysics:
    """Three-level physics for the 1-2 transition, so the EF chain is testable at all.

    Before this the simulator refused `rabi_12` — correctly, since returning data that
    means nothing is worse — which meant the one routine the whole EF chain bootstraps
    from could only ever be tested on a chip. `levels` was already 3 and the ladder
    already came from diagonalising a Cooper-pair box; what was missing was a frame in
    which 1-2 is resonant, and a readout that can tell ``|2>`` from ``|0>``.
    """

    PI_AMPLITUDE = 0.2
    #: The 0-1 pulse length the amplitude-to-rate mapping is defined against.
    PULSE_NS = 20.0

    def _ladder(self) -> float:
        return self.PI_AMPLITUDE / np.sqrt(2.0)

    def test_the_ef_pi_pulse_is_the_0_1_one_over_root_two(self):
        """Nothing in the model says sqrt(2): the drive is (a + a-dagger) on a real ladder.

        Its 1-2 matrix element is sqrt(2) times its 0-1 one, so the pi amplitude comes out
        smaller by that factor. This is what makes `_require_ef_ladder` a measurement of the
        transmon rather than a comment about it.
        """
        simulator = TransmonSimulator(shot_noise=0.005, seed=11)
        amplitudes = np.linspace(0.0, 0.5, 41)

        signal = simulator.rabi_12(
            amplitudes,
            ef_duration_ns=self.PULSE_NS,
            pi_amplitude=self.PI_AMPLITUDE,
        )

        fitted = fit_rabi(amplitudes, signal)
        assert fitted["amp180"] == pytest.approx(self._ladder(), rel=0.05)

    def test_mapping_back_doubles_the_contrast(self):
        """|1> against |2> is one dispersive step; |0> against |2> is two.

        A dispersive readout is linear in the shift, and for a transmon the shifts go as
        chi(1-2n) — so the number operator is the observable and the map-back moves the
        oscillation from the 1-to-2 interval onto the 0-to-2 one.
        """
        simulator = TransmonSimulator(shot_noise=0.005, seed=11)
        amplitudes = np.linspace(0.0, 0.4, 41)

        def contrast(map_back: bool) -> float:
            signal = simulator.rabi_12(
                amplitudes,
                ef_duration_ns=self.PULSE_NS,
                pi_amplitude=self.PI_AMPLITUDE,
                map_back=map_back,
            )
            return float(signal.max() - signal.min())

        assert contrast(map_back=True) == pytest.approx(
            2 * contrast(map_back=False), rel=0.1
        )

    def test_mapping_back_is_what_survives_a_noisier_readout(self):
        """And this is the whole reason `rabi_12` plays the extra pulse.

        It does not change the answer where the line is clean — both fit the ladder to
        under a percent — so its value is entirely in how much readout noise the fit
        tolerates. At this level the plain sequence is refused and the mapped-back one is
        not, which is the claim the routine's comment makes.

        The level is 0.5 where it was 0.3, because `require_resolved_curve` now takes its
        refusal floor from what noise fakes at this many points rather than from a fixed
        3x. Forty-one points tolerate more than that constant assumed, so both sequences
        survive further and the window where they differ sits higher. The window is the
        claim; where it sits is a property of the guard.
        """
        amplitudes = np.linspace(0.0, 0.5, 41)

        def fit_at(map_back: bool):
            simulator = TransmonSimulator(shot_noise=0.5, seed=3)
            signal = simulator.rabi_12(
                amplitudes,
                ef_duration_ns=self.PULSE_NS,
                pi_amplitude=self.PI_AMPLITUDE,
                map_back=map_back,
            )
            return fit_rabi(amplitudes, signal)

        with pytest.raises((FitError, RoutineError)):
            fit_at(map_back=False)
        assert fit_at(map_back=True)["amp180"] == pytest.approx(self._ladder(), rel=0.1)

    def test_the_routine_itself_now_runs_against_the_simulator(self):
        """The point of the build: `rabi_12` was the one node no test could exercise.

        Loose on the ratio because the routine's own EF duration need not match the 0-1
        pulse length the rate mapping is defined against, and the amplitude scales inversely
        with it — which is also why `MAX_EF_LADDER_ERROR` allows a factor of two rather than
        the few percent the relation itself holds to.
        """
        sweep = Sweep("q0")
        tuner = SimulatedTuner()
        amp180 = float(read_path(tuner.device.get_element("q0"), "rxy.amp180"))
        node = next(r for r in all_routines() if r.name == "rabi_12")
        config = RoutineConfig(params={})

        schedule = node.build_schedule("q0", tuner.device, config, tuner.backend, sweep)
        params = node.analyse(
            tuner.backend.run(schedule, timeout_s=120),
            "q0",
            tuner.device,
            config,
            sweep,
        )

        assert tuner.backend._maps_back(schedule), (
            "the routine should map back before reading"
        )
        ratio = params["ef_amp180"] / (amp180 / np.sqrt(2.0))
        assert 0.5 <= ratio <= 2.0, (
            f"the ladder guard would refuse this at {ratio:.2f}x"
        )
