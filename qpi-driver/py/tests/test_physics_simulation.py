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

Needs the `sim` dependency group and nothing else — no scheduler extra. The
routines, the fits and the DAG are numpy and scipy; the schedulers are needed to
*run* a schedule, and here the simulator supplies the acquisition instead. That
is why `make test-py-sim` syncs `--group sim` alone:

    make test-py-sim
"""

import numpy as np
import pytest
from qpi_driver.tuners.base.config import RoutineConfig
from qpi_driver.tuners.base.routines import RoutineError
from qpi_driver.tuners.fitting import FitError, fit_rb_decay
from qpi_driver.tuners.routines import all_routines

pytest.importorskip("scqubits", reason="needs the [sim] dependency group")
qutip = pytest.importorskip("qutip", reason="needs the [sim] dependency group")

from tests.fixtures.simulation import (  # noqa: E402
    GHZ,
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


# --- the simulator itself ----------------------------------------------------


def test_the_simulated_transmon_is_a_plausible_one(simulator):
    """Check the model before trusting anything fitted from it.

    Without this, a bad simulator would be read as a bad routine.
    """
    assert 4.0 < simulator.f01 < 7.0, "f01 should be a normal transmon frequency"
    assert -0.4 < simulator.anharmonicity < -0.15, (
        "a transmon's anharmonicity is a few hundred MHz, and negative"
    )


# --- spectroscopy ------------------------------------------------------------


def test_qubit_spectroscopy_finds_the_transmons_real_f01(simulator):
    """The line comes from a driven, damped steady state — not from a Lorentzian.

    The device is handed an f01 that is 3 MHz off, so the routine has to scan
    around it and find the true one rather than being given it.
    """
    device = device_for(simulator)
    spectroscopy = routine("qubit_spectroscopy")
    config = RoutineConfig(params={"span": 30e6, "points": 61})

    spectroscopy.build_schedule("q0", device, config, StubBackend())
    acquisition = simulator.qubit_spectroscopy(spectroscopy._frequencies)
    fitted = spectroscopy.analyse(acquisition, "q0", device, config)

    assert fitted["clock_freq_01"] == pytest.approx(simulator.f01 * GHZ, abs=5e4)

    # Applying it moves the device onto the true frequency.
    spectroscopy.apply(device, "q0", fitted)
    assert device.get_element("q0").clock_freqs.f01 == pytest.approx(
        simulator.f01 * GHZ, abs=5e4
    )


def test_scanning_the_wrong_window_cannot_invent_the_right_answer(simulator):
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

    spectroscopy.build_schedule("q0", device, config, StubBackend())
    acquisition = simulator.qubit_spectroscopy(spectroscopy._frequencies)
    scanned = spectroscopy._frequencies

    try:
        fitted = spectroscopy.analyse(acquisition, "q0", device, config)
    except (FitError, RoutineError):
        return  # refusing outright is the other acceptable outcome

    assert min(scanned) <= fitted["clock_freq_01"] <= max(scanned), (
        "the fit escaped the window it measured"
    )
    assert abs(fitted["clock_freq_01"] - simulator.f01 * GHZ) > offset / 2, (
        "a window 500 MHz away should not somehow land on the true f01"
    )


# --- time-domain routines ----------------------------------------------------


def test_rabi_finds_the_pi_pulse_from_simulated_dynamics(simulator):
    """The oscillation emerges from integrating the drive, not from a cosine."""
    rabi = routine("rabi")
    device = device_for(simulator)
    config = RoutineConfig(params={"amplitudes": list(np.linspace(0.0, 0.5, 41))})

    rabi.build_schedule("q0", device, config, StubBackend())
    acquisition = simulator.rabi(rabi._amplitudes)
    fitted = rabi.analyse(acquisition, "q0", device, config)

    # The simulator is built so a pi rotation lands at 0.2 in the sweep's units.
    assert fitted["amp180"] == pytest.approx(0.2, rel=0.03)


def test_t1_recovers_the_simulated_relaxation_time(simulator):
    """The decay comes from a collapse operator, not from an exponential."""
    t1 = routine("t1")
    device = device_for(simulator)
    config = RoutineConfig(params={"delays": list(np.linspace(0.0, 80e-6, 25))})

    t1.build_schedule("q0", device, config, StubBackend())
    acquisition = simulator.t1(t1._delays)
    fitted = t1.analyse(acquisition, "q0", device, config)

    # The correct model recovers T1 to 0.01%; a Gaussian decay fitted to this
    # same data lands 7% out, so 2% is what makes this test discriminating
    # rather than merely satisfied.
    assert fitted["t1"] == pytest.approx(simulator.t1_ns * 1e-9, rel=0.02)


def test_t2_echo_recovers_the_simulated_dephasing_time(simulator):
    """A Hahn echo, evolved through both halves with the refocusing pulse between."""
    t2 = routine("t2_echo")
    device = device_for(simulator)
    config = RoutineConfig(params={"delays": list(np.linspace(0.0, 60e-6, 25))})

    t2.build_schedule("q0", device, config, StubBackend())
    acquisition = simulator.t2_echo(t2._delays)
    fitted = t2.analyse(acquisition, "q0", device, config)

    assert fitted["t2"] == pytest.approx(simulator.t2_ns * 1e-9, rel=0.05)


def test_ramsey_measures_the_deliberate_detuning_and_reports_no_residual(simulator):
    """The fringe is the artificial detuning; the qubit itself is on resonance.

    So the *residual* detuning is the real assertion — it is the number that
    gets written to the device, and it should be near zero here.
    """
    ramsey = routine("ramsey")
    device = device_for(simulator)
    detuning = 1e6
    config = RoutineConfig(
        params={
            "delays": list(np.linspace(4e-9, 6e-6, 61)),
            "artificial_detuning": detuning,
        }
    )

    ramsey.build_schedule("q0", device, config, StubBackend())
    acquisition = simulator.ramsey(ramsey._delays, detuning)
    fitted = ramsey.analyse(acquisition, "q0", device, config)

    assert fitted["fringe_frequency"] == pytest.approx(detuning, rel=0.01)
    # The qubit is on resonance, so the residual should be kHz, not a
    # fraction of the deliberate detuning.
    assert abs(fitted["detuning"]) < 5e3


# --- randomized benchmarking --------------------------------------------------


@pytest.mark.parametrize("error_per_gate", [0.004, 0.02])
def test_rb_recovers_a_known_gate_error(simulator, error_per_gate):
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


def test_rb_survival_collapses_to_chance_without_a_recovery_gate(simulator):
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


# --- two qubits ---------------------------------------------------------------
#
# A CZ is the first thing here that one transmon cannot have: it needs a joint
# register, because entanglement is not merely absent between two independent
# density matrices, it is unrepresentable. `qpi_driver.simulation.coupled` holds
# both in one 9-dimensional space and couples them.
#
# These tolerances are looser than the one-qubit ones above, and the reason is
# structural rather than numerical: the exchange coupling and the
# flux-to-detuning curve are numbers chosen to describe a plausible coupler, not
# constants of a Cooper-pair box. See the module docstring of `coupled`, and
# RFC 0004 §7.


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
    from tests.fixtures.simulation import SimulatedTuner

    return SimulatedTuner(qubits=("q0", "q1"), edges=("q0_q1",), coupled=coupled)


def angle_apart(left: float, right: float) -> float:
    """Degrees between two angles, the short way round.

    The fits report a phase in [0, 360) and the simulator in (-180, 180]; 359°
    and -1° are the same gate, and a plain subtraction says they are 360° apart.
    """
    return abs((left - right + 180.0) % 360.0 - 180.0)


def test_the_coupled_pair_produces_a_cz_and_not_merely_a_rotation(coupled):
    """The conditional phase is 180° because of the avoided crossing, not by fiat."""
    assert angle_apart(coupled.cz_conditional_phase(), 180.0) < 2.0


def test_a_bell_state_comes_out_entangled(coupled):
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


def test_a_cz_needs_the_coupling_to_exist(coupled):
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


def test_cz_chevron_finds_the_avoided_crossing(coupled):
    tuner = two_qubit_tuner(coupled)
    config = chevron_config(0.365, 0.388, 21, 45)
    routine_ = routine("cz_chevron")

    schedule = routine_.build_schedule("q0_q1", tuner.device, config, tuner.backend)
    fit = routine_.analyse(tuner.backend.run(schedule), "q0_q1", tuner.device, config)

    assert fit["cz_amplitude"] == pytest.approx(coupled.resonant_amplitude, rel=0.01)
    assert fit["cz_duration"] / 1e-9 == pytest.approx(coupled.cz_duration_ns, rel=0.02)


def test_cz_chevron_refuses_a_sweep_that_stepped_over_the_crossing(coupled):
    """The default amplitude range is far too coarse to resolve a few-MHz crossing.

    One step of the default grid moves the control by tens of MHz while the
    avoided crossing is about 4.5 MHz wide, so the surface comes back flat to
    within its noise. A peak-finder would still return a confident answer from
    it, and that answer would go to the device as a CZ.
    """
    tuner = two_qubit_tuner(coupled)
    config = chevron_config(0.1, 0.6, 11, 11)
    routine_ = routine("cz_chevron")

    schedule = routine_.build_schedule("q0_q1", tuner.device, config, tuner.backend)
    with pytest.raises(FitError, match="no flux amplitude drove"):
        routine_.analyse(tuner.backend.run(schedule), "q0_q1", tuner.device, config)


def test_conditional_phase_recovers_the_gates_real_phase(coupled):
    tuner = two_qubit_tuner(coupled)
    config = RoutineConfig(
        params={"phases": list(np.linspace(0.0, 360.0, 25)), "shots": 512}
    )
    routine_ = routine("conditional_phase")

    schedule = routine_.build_schedule("q0_q1", tuner.device, config, tuner.backend)
    fit = routine_.analyse(tuner.backend.run(schedule), "q0_q1", tuner.device, config)

    assert angle_apart(fit["conditional_phase"], coupled.cz_conditional_phase()) < 2.0
    # A CZ this good needs almost no correcting, and the correction is the
    # complement of the phase — in degrees, which is what Rxy takes.
    assert fit["phase_correction"] == pytest.approx(
        180.0 - coupled.cz_conditional_phase(), abs=2.0
    )


def test_conditional_phase_measures_a_gate_that_is_wrong(coupled):
    """A miscalibrated CZ must be reported as needing exactly its own error back.

    The one-qubit tier found that a fit which cannot measure a *good* chip
    recalibrates forever; this is the opposite failure, and the more dangerous
    one — a fit that reports every gate as fine leaves a broken CZ in service.
    """
    import dataclasses

    detuned = dataclasses.replace(coupled, conditional_phase_offset_deg=40.0)
    tuner = two_qubit_tuner(detuned)
    config = RoutineConfig(
        params={"phases": list(np.linspace(0.0, 360.0, 25)), "shots": 512}
    )
    routine_ = routine("conditional_phase")

    schedule = routine_.build_schedule("q0_q1", tuner.device, config, tuner.backend)
    fit = routine_.analyse(tuner.backend.run(schedule), "q0_q1", tuner.device, config)

    assert angle_apart(fit["conditional_phase"], detuned.cz_conditional_phase()) < 2.0
    assert fit["phase_correction"] == pytest.approx(-40.0, abs=3.0)


def test_the_conditional_phase_survives_the_controls_own_dynamical_phase(coupled):
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
        fringes = longer.conditional_phase(phases, duration_ns=duration, averages=512)
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
