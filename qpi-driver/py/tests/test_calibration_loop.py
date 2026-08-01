"""Calibrate a simulated chip, then run circuits on it (RFC 0004 §7).

This is the loop the two operations are supposed to form, with nothing faked in
the middle. A `QuantifyTuner` calibrates against
:class:`~qpi_driver.simulation.SimulatedCoordinator`, writes a real
`quantify.device.yml`, and a `QuantifyExecutor` then loads *that file* and runs
circuits against the same simulated chip. Every component is the shipped one:
the real routines, the real fits, the real write-back, the real loader, the real
compiler. Only the cluster is replaced.

What makes it a test rather than a demonstration is that the calibration is
load-bearing. The drive amplitude the executor plays is the `amp180` the tuner
fitted, and the drive frequency is the `f01` it found; the fixture device starts
over 200 MHz off resonance, where an X gate does nothing at all. So an X gate
that lands in |1> is evidence that a number survived the fit, the write-back,
the YAML, the loader and the compiler with its meaning intact — which is the one
thing no single-component test can show.

Needs both a scheduler and the `sim` group:

    make test-py-loop
"""

import shutil
from pathlib import Path

import numpy as np
import pytest
import yaml

pytest.importorskip("scqubits", reason="needs the [sim] dependency group")
pytest.importorskip("qutip", reason="needs the [sim] dependency group")

from qpi_driver.compat.qblox import IS_QBLOX_SCHEDULER_INSTALLED  # noqa: E402
from qpi_driver.compat.quantify import IS_QUANTIFY_INSTALLED  # noqa: E402
from qpi_driver.executors import resolve_executor  # noqa: E402
from qpi_driver.executors.base import CircuitPayload, JobPayload  # noqa: E402
from qpi_driver.executors.utils.coupler_bias import bias_settings  # noqa: E402
from qpi_driver.simulation import GHZ, TransmonSimulator  # noqa: E402
from qpi_driver.tuners.base.config import CalibrationConfig, RoutineConfig  # noqa: E402
from qpi_driver.tuners.base.device import read_path  # noqa: E402
from qpi_driver.tuners.routines import routine_names  # noqa: E402

pytestmark = pytest.mark.scqubits

# Every test here runs under both schedulers. The loop is the same loop —
# calibrate a simulated chip, then run circuits against what the calibration
# wrote — and the point of parametrising rather than duplicating is that a claim
# proved for one scheduler and quietly untested for the other is how qblox came
# to be a stub. quantify-scheduler is being deprecated, so qblox is the one that
# has to keep passing.
SCHEDULERS = [
    pytest.param(
        "quantify",
        marks=pytest.mark.skipif(
            not IS_QUANTIFY_INSTALLED, reason="quantify-scheduler is not installed"
        ),
    ),
    pytest.param(
        "qblox",
        marks=pytest.mark.skipif(
            not IS_QBLOX_SCHEDULER_INSTALLED,
            reason="qblox-scheduler is not installed",
        ),
    ),
]


def close_instruments(scheduler: str) -> None:
    """Each scheduler keeps its own qcodes instrument registry."""
    if scheduler == "qblox":
        from qpi_driver.compat.qblox import Instrument
    else:
        from qpi_driver.compat.quantify import Instrument
    Instrument.close_all()


def tuner_for(scheduler: str, **kwargs):
    if scheduler == "qblox":
        from qpi_driver.tuners.qblox import QbloxTuner

        return QbloxTuner(**kwargs)
    from qpi_driver.tuners.quantify import QuantifyTuner

    return QuantifyTuner(**kwargs)


@pytest.fixture(params=SCHEDULERS, scope="module")
def scheduler(request) -> str:
    return request.param


FIXTURES = Path(__file__).parent / "fixtures"

#: The routines this loop runs. Each writes a parameter the executor then plays,
#: which is the point — a routine that only measures would prove nothing here.
#:
#: Ramsey is not optional. Spectroscopy drives a 20 ns pulse, so its line is
#: Fourier-limited to tens of MHz and it lands within about 10 MHz — enough to
#: find a lost qubit, not enough to drive it, since 10 MHz over a 20 ns gate is
#: most of a radian of phase error. Refining that is what Ramsey is for, and the
#: real graph already orders it after Rabi for exactly this reason.
CALIBRATED = ("qubit_spectroscopy", "rabi", "ramsey")

QASM_HEAD = 'OPENQASM 3.0;\ninclude "stdgates.inc";\nqubit[1] q;\nbit[1] c;\n'
QASM_TAIL = "c[0] = measure q[0];\n"


def circuit(body: str) -> str:
    return QASM_HEAD + body + QASM_TAIL


def calibration_config() -> CalibrationConfig:
    """Only the two routines, with a sweep wide enough to find a lost qubit.

    The span is deliberately large. The fixture device claims 5.0 GHz and the
    simulated transmon is at 5.21 GHz, so a routine scanning the default 40 MHz
    would never see the line — which is the honest starting point for a chip
    nobody has calibrated yet.
    """
    return CalibrationConfig(
        target_qubits=["q0"],
        routines={
            name: RoutineConfig(enabled=name in CALIBRATED, params=_sweep(name))
            for name in routine_names()
        },
    )


def _sweep(name: str) -> dict:
    return {
        "qubit_spectroscopy": {"span": 600e6, "points": 61},
        "rabi": {"amplitudes": [round(0.02 * i, 4) for i in range(26)]},
        # 400 MHz about f01 minus 300, which brackets a transmon's anharmonicity without
        # trusting the f12 already on the device — the fixture's is 134 MHz wrong, which is
        # the honest state of a field nothing ever measured.
        "f12_spectroscopy": {"span": 400e6, "points": 81},
        # Ramsey's sweep is squeezed from both ends, which is worth stating
        # because getting either wrong looks like a broken routine.
        #
        # Fine enough: spectroscopy leaves a residual of several MHz, so the
        # fringe is ~9 MHz and 40 ns steps put Nyquist at 12.5 MHz. The
        # routine's 250 ns default would alias it to something plausible and
        # wrong.
        #
        # Long enough: the fit rejects a T2* outside the window that produced it
        # — rightly — so a sweep shorter than T2 (20 µs here) cannot measure the
        # decay it is being asked for.
        "ramsey": {
            "delays": [round(4e-9 + 4e-8 * i, 11) for i in range(601)],
            "artificial_detuning": 1e6,
        },
    }.get(name, {})


@pytest.fixture(scope="module")
def calibrated_device(
    scheduler, tmp_path_factory
) -> tuple[Path, TransmonSimulator, str]:
    """Run a real calibration against the simulator and return what it wrote."""
    simulator = TransmonSimulator()
    directory = tmp_path_factory.mktemp(f"loop_{scheduler}")
    device = directory / "quantify.device.yml"
    shutil.copy(FIXTURES / "quantify.device.yml", device)

    close_instruments(scheduler)
    tuner = tuner_for(
        scheduler,
        name=f"loop_tuner_{scheduler}",
        quantify_hardware_config=FIXTURES / "quantify.hardware.json",
        quantify_device_config=device,
        is_simulated=True,
        simulator=simulator,
    )
    report = tuner.calibrate(calibration_config())
    assert report.status == "success", report.errors
    tuner.close()
    close_instruments(scheduler)
    return device, simulator, scheduler


def run(
    device: Path,
    simulator: TransmonSimulator,
    scheduler: str,
    qasm: str,
    shots: int = 400,
) -> dict:
    """Counts from the executor, over the calibrated file and the same chip."""
    close_instruments(scheduler)
    executor = resolve_executor(
        scheduler,
        is_simulated=True,
        simulator=simulator,
        quantify_hardware_config=FIXTURES / "quantify.hardware.json",
        quantify_device_config=device,
    )
    dataset = executor.execute(
        JobPayload(circuits=[CircuitPayload(circuit=qasm)], shots=shots)
    )
    return executor.process_result(dataset, "loop-job")["counts"]


# --- what the calibration found ------------------------------------------------


def test_the_calibration_finds_the_simulated_chip(calibrated_device):
    """Before trusting any circuit, check the numbers the calibration wrote."""
    device, simulator, scheduler = calibrated_device
    written = yaml.safe_load(device.read_text())["q0"]

    # Spectroscopy then Ramsey: the fixture starts 214 MHz out, and what is
    # written has to be good enough to drive with, not merely in the right area.
    assert written["clock_freqs"]["f01"] == pytest.approx(simulator.f01 * GHZ, abs=1e6)
    # The instrument's drive strength puts a pi rotation at 0.2, and nothing
    # told the tuner that — Rabi had to find it.
    assert written["rxy"]["amp180"] == pytest.approx(0.2, rel=0.05)

    # And the power spectroscopy chose for itself. Asserting it is non-zero rather
    # than a value: which power reads best is a property of this chip and this
    # sweep, and pinning it would only record what the fit happened to return. What
    # matters is that the sweep is no longer a hand-set constant — the fixture ships
    # 0, and a config kept working by a hardcoded 3% is how f01 came back 62 MHz out.
    assert written["spec"]["amplitude"] > 0.0


def test_the_executor_loads_exactly_what_the_tuner_wrote(calibrated_device):
    device, simulator, scheduler = calibrated_device
    written = yaml.safe_load(device.read_text())["q0"]

    close_instruments(scheduler)
    executor = resolve_executor(
        scheduler,
        is_simulated=True,
        simulator=simulator,
        quantify_hardware_config=FIXTURES / "quantify.hardware.json",
        quantify_device_config=device,
    )
    element = executor._device.get_element("q0")
    assert read_path(element, "rxy.amp180") == written["rxy"]["amp180"]
    assert read_path(element, "clock_freqs.f01") == written["clock_freqs"]["f01"]


# --- circuits against the calibration ------------------------------------------


def test_an_x_gate_lands_in_the_excited_state(calibrated_device):
    """The pi pulse the tuner calibrated, played by the executor.

    Not exactly 100%: the qubit relaxes during the readout integration, which is
    what a real chip does too.
    """
    counts = run(*calibrated_device, circuit("x q[0];\n"))
    assert counts["1"] / sum(counts.values()) > 0.9


def test_two_x_gates_return_to_the_ground_state(calibrated_device):
    """The control for the test above: a miscalibrated pi is still a fixed
    rotation, so it would fail *here* rather than passing both."""
    counts = run(*calibrated_device, circuit("x q[0];\nx q[0];\n"))
    assert counts["0"] / sum(counts.values()) > 0.95


def test_a_hadamard_is_an_even_superposition(calibrated_device):
    counts = run(*calibrated_device, circuit("h q[0];\n"), shots=800)
    assert 0.4 < counts["1"] / sum(counts.values()) < 0.6


def test_an_uncalibrated_chip_gets_the_answer_wrong(scheduler, tmp_path):
    """The whole loop, negated — this is what makes the tests above mean something.

    The same X gate on the *uncalibrated* fixture device, whose f01 is 214 MHz
    from where the qubit actually is. The pulse compiles, runs and returns
    counts; they are simply wrong. If calibration were not load-bearing, this
    would pass too.
    """
    simulator = TransmonSimulator()
    device = tmp_path / "quantify.device.yml"
    shutil.copy(FIXTURES / "quantify.device.yml", device)

    counts = run(device, simulator, scheduler, circuit("x q[0];\n"))
    assert counts["1"] / sum(counts.values()) < 0.1, (
        "an X gate 214 MHz off resonance should not excite the qubit"
    )


# --- two qubits, through the coordinator ----------------------------------------
#
# Everything above is one qubit at a time. A CZ is the first gate the executor can
# play that needs two held in one state, and the first whose result cannot be
# faked by a pair of independent simulations: entanglement lives between qubits,
# not in either of them.


@pytest.fixture(scope="module")
def two_qubit_device(
    scheduler, tmp_path_factory
) -> tuple[Path, TransmonSimulator, str]:
    """A device already at the simulator's true parameters, edge included.

    Deliberately *not* calibrated by a tuner here. The loop above proves the
    tuner finds a qubit; this fixture isolates the gate, so a failure below is
    the CZ rather than a single-qubit calibration that drifted.
    """
    from qpi_driver.simulation.coupled import CoupledTransmons

    simulator = TransmonSimulator()
    pair = CoupledTransmons()
    directory = tmp_path_factory.mktemp(f"twoqubit_{scheduler}")
    device = directory / "quantify.device.yml"
    shutil.copy(FIXTURES / "quantify.device.yml", device)

    config = yaml.safe_load(device.read_text())
    for qubit in ("q0", "q1", "q2"):
        config[qubit]["clock_freqs"]["f01"] = float(simulator.f01 * GHZ)
        config[qubit]["rxy"]["amp180"] = 0.2
    config["q0_q1"]["cz"]["square_amp"] = float(pair.resonant_amplitude)
    # Whole nanoseconds: the hardware plays pulses on a 1 ns grid and the
    # compiler refuses anything else, so the ideal 110.5 ns round trip is not a
    # duration a CZ can actually have.
    config["q0_q1"]["cz"]["square_duration"] = round(pair.cz_duration_ns) * 1e-9
    device.write_text(yaml.safe_dump(config))
    return device, simulator, scheduler


TWO_QUBIT_HEAD = 'OPENQASM 3.0;\ninclude "stdgates.inc";\nqubit[2] q;\nbit[2] c;\n'
TWO_QUBIT_TAIL = "c[0] = measure q[0];\nc[1] = measure q[1];\n"


def run_pair(
    device: Path,
    simulator: TransmonSimulator,
    scheduler: str,
    body: str,
    shots: int = 400,
):
    return run(
        device,
        simulator,
        scheduler,
        TWO_QUBIT_HEAD + body + TWO_QUBIT_TAIL,
        shots=shots,
    )


def correlation(counts: dict) -> float:
    """|Pearson correlation| between the two measured bits, from 0 to 1.

    Absolute because an uncorrected CZ leaves a local phase on each qubit, and a
    local phase turns ``|00>+|11>`` into ``|01>+|10>`` without touching how
    entangled the pair is. What a Bell state guarantees is that each outcome
    *determines* the other; which of the two pairings carries it is a
    single-qubit rotation, and correcting that is what `conditional_phase`
    writes ``cz.phase_correction`` for.

    Pearson rather than "how often do the bits agree", because that shortcut
    calls a state with lopsided marginals strongly correlated: if one qubit is
    excited nine times in ten, the bits disagree nine times in ten whatever the
    other does. Dividing by the marginals is what separates a correlation from
    a coincidence.
    """
    total = sum(counts.values()) or 1
    probability = {key: value / total for key, value in counts.items()}
    first = sum(p for key, p in probability.items() if key[0] == "1")
    second = sum(p for key, p in probability.items() if key[1] == "1")
    both = probability.get("11", 0.0)

    spread = (first * (1 - first)) * (second * (1 - second))
    if spread <= 0:
        return 0.0  # a bit that never varies cannot be correlated with anything
    return abs((both - first * second) / np.sqrt(spread))


def test_a_cz_entangles_two_qubits(two_qubit_device):
    """H, CZ, H is a CNOT, and on a superposition that is a Bell state.

    Before this the coordinator raised on a flux pulse. The claim now is
    stronger than "it runs": the two qubits come out correlated, which no pair
    of independently simulated qubits can be.
    """
    counts = run_pair(*two_qubit_device, "h q[0];\nh q[1];\ncz q[0], q[1];\nh q[1];\n")
    total = sum(counts.values())

    assert correlation(counts) > 0.85, (
        f"the pair should be nearly perfectly correlated, got {counts}"
    )
    # And each qubit alone is still a coin toss — an entangled pair carries its
    # information between the qubits, not in either marginal.
    q0_excited = (counts.get("01", 0) + counts.get("11", 0)) / total
    assert 0.35 < q0_excited < 0.65, f"q0's marginal should be ~0.5, got {q0_excited}"


def test_a_cz_leaves_basis_states_alone(two_qubit_device):
    """The population half of the gate: |11> must come back, not stay in |02>.

    A CZ works by driving |11> to |02> and back. Stopping half way is a
    perfectly good population transfer and a completely broken gate, and this is
    what tells the two apart.
    """
    counts = run_pair(*two_qubit_device, "x q[0];\nx q[1];\ncz q[0], q[1];\n")
    total = sum(counts.values())
    assert counts.get("11", 0) / total > 0.85, (
        f"|11> should survive a full exchange round trip, got {counts}"
    )


def test_a_mistimed_cz_does_not_entangle(two_qubit_device):
    """The control that makes the test above mean something.

    Half the duration is half a round trip: population sits in |02> instead of
    returning, so there is no conditional phase and no Bell state. If the CZ
    were being applied as a nominal gate rather than integrated, this would pass
    identically to the real one.
    """
    device, simulator, scheduler = two_qubit_device
    broken = device.parent / "mistimed.device.yml"
    config = yaml.safe_load(device.read_text())
    config["q0_q1"]["cz"]["square_duration"] = (
        round(config["q0_q1"]["cz"]["square_duration"] * 1e9 / 2) * 1e-9
    )
    broken.write_text(yaml.safe_dump(config))

    counts = run_pair(
        broken, simulator, scheduler, "h q[0];\nh q[1];\ncz q[0], q[1];\nh q[1];\n"
    )
    assert correlation(counts) < 0.5, (
        f"half a CZ should not produce an entangled pair, got {counts}"
    )


# --- virtual Z ------------------------------------------------------------------


def test_a_virtual_z_actually_rotates_the_frame(calibrated_device):
    """`rz` plays no pulse, and must still change the answer.

    A `z`, `s`, `t` or `rz` compiles to a `ShiftClockPhase`: nothing is played,
    the frame the *next* drive is referenced to simply moves. A simulator that
    walks pulses and ignores frame shifts therefore runs every one of them as an
    identity, silently — and `h; h` and `h; rz(pi); h` come out the same.

    The delays are not padding: the compiler refuses two phase updates closer
    than 4 ns apart, which is a real constraint of the hardware's NCO.
    """
    device, simulator, scheduler = calibrated_device
    gap = "delay[8ns] q[0];\n"

    without = run(
        device,
        simulator,
        scheduler,
        circuit("h q[0];\n" + gap + gap + "h q[0];\n"),
        shots=600,
    )
    with_z = run(
        device,
        simulator,
        scheduler,
        circuit("h q[0];\n" + gap + "rz(pi) q[0];\n" + gap + "h q[0];\n"),
        shots=600,
    )

    assert without["0"] / sum(without.values()) > 0.9, (
        f"h then h should return to |0>, got {without}"
    )
    # H Z H = X, so the same circuit with a virtual Z between the two Hadamards
    # must land in |1> instead.
    assert with_z["1"] / sum(with_z.values()) > 0.9, (
        f"h, rz(pi), h should land in |1>, got {with_z}"
    )


def test_two_virtual_z_gates_compose(calibrated_device):
    """S·S = Z, which only holds if the shifts accumulate rather than replace."""
    device, simulator, scheduler = calibrated_device
    gap = "delay[8ns] q[0];\n"
    counts = run(
        device,
        simulator,
        scheduler,
        circuit(
            "h q[0];\n" + gap + "s q[0];\n" + gap + "s q[0];\n" + gap + "h q[0];\n"
        ),
        shots=600,
    )
    assert counts["1"] / sum(counts.values()) > 0.9, (
        f"two S gates should compose to a Z, got {counts}"
    )


# --- the three measurement levels -----------------------------------------------
#
# A job asks for one of three acquisition protocols, and they do not differ only
# in how much averaging they do — they return different *kinds* of thing. A
# simulator that answers all three with integrated IQ lets a level-0 job produce
# a one-sample waveform and a level-2 job quietly take the software
# discrimination path instead of the hardware one production runs on.


def run_at(
    device: Path,
    simulator: TransmonSimulator,
    scheduler: str,
    qasm: str,
    *,
    meas_level: int,
    meas_return: str = "single",
    shots: int = 200,
) -> dict:
    """The whole result dict, not just counts, since levels 0 and 1 have none."""
    close_instruments(scheduler)
    executor = resolve_executor(
        scheduler,
        is_simulated=True,
        simulator=simulator,
        quantify_hardware_config=FIXTURES / "quantify.hardware.json",
        quantify_device_config=device,
    )
    dataset = executor.execute(
        JobPayload(
            circuits=[CircuitPayload(circuit=qasm)],
            shots=shots,
            meas_level=meas_level,
            meas_return=meas_return,
        )
    )
    assert dataset.attrs.get("meas_level") == meas_level
    return executor.process_result(dataset, "loop-job")


def test_meas_level_0_returns_a_raw_trace(calibrated_device):
    """A Trace is a time series, and it has to ring up rather than just appear.

    The integration window is 1 µs and the digitiser samples at 1 GSa/s, so a
    correct trace is a thousand points. One point is what this used to give.
    """
    result = run_at(*calibrated_device, circuit("x q[0];\n"), meas_level=0)
    memory = np.asarray(result["memory"])

    assert memory.shape == (1, 1000, 2), (
        f"expected one 1 us trace sampled per ns, as [I, Q] pairs, got {memory.shape}"
    )
    trace = memory[0, :, 0] + 1j * memory[0, :, 1]
    # The resonator fills over its own linewidth, so the start of the window is
    # nearer the origin than the end. Averaging over many samples beats the
    # per-sample noise, which is what makes this comparison meaningful.
    assert abs(np.mean(trace[:20])) < abs(np.mean(trace[-200:])), (
        "the trace should ring up, not start settled"
    )


def test_meas_level_1_returns_integrated_iq(calibrated_device):
    """One IQ point per shot, and the excited state lands on the excited blob."""
    excited = run_at(*calibrated_device, circuit("x q[0];\n"), meas_level=1, shots=200)
    ground = run_at(*calibrated_device, circuit(""), meas_level=1, shots=200)

    excited_memory = np.asarray(excited["memory"])
    assert excited_memory.shape == (200, 1, 2), (
        f"expected one IQ pair per shot, got {excited_memory.shape}"
    )

    # The two clouds have to be separable, or no discriminator could work — but not
    # separable *along I*, which is what this used to require. Where the clouds land
    # is the amplifier chain's business: each level sits at its own resonance and so
    # returns its own complex response, and the chain then rotates the pair by
    # whatever the cabling happens to be. Demanding a particular axis was demanding
    # that `acq_rotation` be zero, which is the thing `readout_discrimination` exists
    # to measure.
    ground_memory = np.asarray(ground["memory"])
    excited_iq = complex(*np.mean(excited_memory[:, 0, :], axis=0))
    ground_iq = complex(*np.mean(ground_memory[:, 0, :], axis=0))
    assert abs(excited_iq - ground_iq) > 1.0, (
        f"the clouds should separate: |0> at {ground_iq}, |1> at {excited_iq}"
    )


def test_meas_level_1_averaged_collapses_the_shots(calibrated_device):
    result = run_at(
        *calibrated_device,
        circuit("x q[0];\n"),
        meas_level=1,
        meas_return="avg",
        shots=200,
    )
    assert np.asarray(result["memory"]).shape == (1, 1, 2), (
        "an averaged level-1 acquisition is one point, however many shots ran"
    )


def test_meas_level_2_is_discriminated_on_the_instrument(calibrated_device):
    """Counts, and by the hardware's own rule rather than in software.

    `ThresholdedAcquisition` discriminates on the module and returns 0 or 1. A
    simulator that hands back IQ here still produces the right counts — the
    executor falls through to discriminating in software — so the path a real
    job takes would never be exercised.
    """
    result = run_at(*calibrated_device, circuit("x q[0];\n"), meas_level=2, shots=200)
    counts = result["counts"]
    assert counts["1"] / sum(counts.values()) > 0.9, (
        f"an X gate should read 1: {counts}"
    )


def test_every_measurement_level_agrees_about_the_same_circuit(calibrated_device):
    """The three levels are three views of one experiment, not three experiments.

    Level 2 says the qubit is excited; level 1's IQ has to sit on the excited
    blob and level 0's settled trace has to point the same way. A level that
    disagrees is reporting a different circuit than the one that ran.
    """
    device, simulator, scheduler = calibrated_device
    qasm = circuit("x q[0];\n")

    counts = run_at(device, simulator, scheduler, qasm, meas_level=2, shots=200)[
        "counts"
    ]
    iq = np.asarray(
        run_at(device, simulator, scheduler, qasm, meas_level=1, shots=200)["memory"]
    )
    memory = np.asarray(
        run_at(device, simulator, scheduler, qasm, meas_level=0)["memory"]
    )

    # Against the *same circuit with no gate*, rather than against a sign. Which side
    # of the origin an excited qubit lands on is the readout chain's business, so the
    # question that has content is whether all three levels move the same way when the
    # gate is added.
    reference_iq = np.asarray(
        run_at(device, simulator, scheduler, circuit(""), meas_level=1, shots=200)[
            "memory"
        ]
    )
    reference_trace = np.asarray(
        run_at(device, simulator, scheduler, circuit(""), meas_level=0)["memory"]
    )

    excited_fraction = counts["1"] / sum(counts.values())
    moved_iq = abs(
        complex(*np.mean(iq[:, 0, :], axis=0))
        - complex(*np.mean(reference_iq[:, 0, :], axis=0))
    )
    moved_trace = abs(
        complex(*np.mean(memory[0, -200:, :], axis=0))
        - complex(*np.mean(reference_trace[0, -200:, :], axis=0))
    )

    assert excited_fraction > 0.9
    assert moved_iq > 1.0, (
        f"level 1 should move off the |0> cloud, moved {moved_iq:.2f}"
    )
    assert moved_trace > 1.0, (
        f"level 0's settled trace should move with it, moved {moved_trace:.2f}"
    )


# --- the flux-tunable coupler ---------------------------------------------------
#
# The tests above drive `q0_q1`, a stock `CompositeSquareEdge` whose flux pulse
# goes on a *qubit's* port. `q1_q2` is a `FluxTunableCoupler`: the pulse goes on
# the coupler's own port, `q1_q2:fl`, and the CZ is lowered into a fresh
# subschedule. That is a different path through both the compiler and the
# simulator, and it is the one a working chip's device config actually uses.


@pytest.fixture(scope="module")
def coupler_device(scheduler, tmp_path_factory) -> tuple[Path, TransmonSimulator, str]:
    from qpi_driver.simulation.coupled import CoupledTransmons

    simulator = TransmonSimulator()
    pair = CoupledTransmons()
    directory = tmp_path_factory.mktemp(f"coupler_{scheduler}")
    device = directory / "quantify.device.yml"
    shutil.copy(FIXTURES / "quantify.device.yml", device)

    amplitude = 0.554
    drive, duration = pair.parametric_operating_point(amplitude)
    duration = round(duration)  # the compiler plays on a 1 ns grid
    phases = pair.parametric_phases(amplitude, drive, duration)

    config = yaml.safe_load(device.read_text())
    for qubit in ("q0", "q1", "q2"):
        config[qubit]["clock_freqs"]["f01"] = float(simulator.f01 * GHZ)
        config[qubit]["rxy"]["amp180"] = 0.2
    config["q1_q2"]["clock_freqs"] = {"cz": float(drive * GHZ)}
    config["q1_q2"]["cz"] = {
        "square_amp": amplitude,
        "square_duration": duration * 1e-9,
        # What the coupler's Stark shift left on each qubit, handed back. These
        # are single-qubit phases, so no amount of tuning the amplitude or the
        # duration removes them — only these two parameters do.
        **dict(
            zip(
                coupler_correction_names(scheduler),
                (phases["parent"], phases["child"]),
            )
        ),
    }
    # The coupler's DC parking point, which the SPI rack supplies out of band.
    config["q1_q2"]["bias"] = {"parking_current": 0.0009, "source": "spi"}
    device.write_text(yaml.safe_dump(config))
    return device, simulator, scheduler


def set_bias(edge, name: str, value) -> None:
    """Set a bias parameter, whichever way this scheduler wants it set."""
    parameter = getattr(edge.bias, name)
    if callable(parameter):
        parameter(value)
    else:
        setattr(edge.bias, name, value)


def coupler_correction_names(scheduler: str) -> tuple[str, str]:
    """What this scheduler's CZ calls its two virtual-Z corrections.

    quantify names them after the qubits, qblox after the roles. Written out
    here because the fixture builds the YAML by hand; the routines ask the edge
    itself through `phase_correction_names`.
    """
    if scheduler == "qblox":
        return "parent_phase_correction", "child_phase_correction"
    return "q1_phase_correction", "q2_phase_correction"


def bell_circuit() -> str:
    """H on the control, then H-CZ-H on the target: a CNOT, so a Bell pair."""
    return (
        'OPENQASM 3.0;\ninclude "stdgates.inc";\nqubit[3] q;\nbit[2] c;\n'
        "h q[1];\nh q[2];\ncz q[1], q[2];\nh q[2];\n"
        "c[0] = measure q[1];\nc[1] = measure q[2];\n"
    )


def without_corrections(device: Path, tmp_path: Path) -> Path:
    """The same device with both phase corrections zeroed."""
    other = tmp_path / "uncorrected.device.yml"
    config = yaml.safe_load(device.read_text())
    for name in config["q1_q2"]["cz"]:
        if name.endswith("phase_correction"):
            config["q1_q2"]["cz"][name] = 0.0
    other.write_text(yaml.safe_dump(config))
    return other


def test_the_coupler_carries_its_parking_current(coupler_device):
    """The bias is calibrated state and has to survive the device file.

    It is not scheduled — a seconds-scale DC current is not a pulse — but it is
    as much a part of the CZ's calibration as the pulse amplitude, so it belongs
    in the config the tuner writes and the executor reads.
    """
    device, simulator, scheduler = coupler_device
    close_instruments(scheduler)
    executor = resolve_executor(
        scheduler,
        is_simulated=True,
        simulator=simulator,
        quantify_hardware_config=FIXTURES / "quantify.hardware.json",
        quantify_device_config=device,
    )
    edge = executor._device.get_edge("q1_q2")

    settings = bias_settings(edge)
    assert settings["parking_current"] == pytest.approx(0.0009)
    assert settings["source"] == "spi"


def test_the_parking_current_is_bounded(coupler_device):
    """Outside the validated range is refused rather than driven.

    An S4g will happily source more than the couplers are characterised for,
    and a current that far out is a fat-fingered config rather than an
    operating point.
    """
    device, simulator, scheduler = coupler_device
    close_instruments(scheduler)
    executor = resolve_executor(
        scheduler,
        is_simulated=True,
        simulator=simulator,
        quantify_hardware_config=FIXTURES / "quantify.hardware.json",
        quantify_device_config=device,
    )
    edge = executor._device.get_edge("q1_q2")

    # Both schedulers validate, and both reject the same way — but one takes the
    # value through a call and the other through an assignment.
    with pytest.raises(ValueError):
        set_bias(edge, "parking_current", 5e-3)
    # And a normal operating value is accepted.
    set_bias(edge, "parking_current", 1.1e-3)


def test_a_cz_over_the_coupler_edge_completes_its_exchange(coupler_device):
    """|11> survives a CZ played through the coupler's own port.

    The partner cannot be read off the gate here — the lowered subschedule has
    lost it — so the simulator takes the pair from the port name, which is what
    `q1_q2:fl` is for. Getting that wrong applies the coupling between the wrong
    qubits, which is a working-looking gate on the wrong pair.
    """
    device, simulator, scheduler = coupler_device
    qasm = (
        'OPENQASM 3.0;\ninclude "stdgates.inc";\nqubit[3] q;\nbit[2] c;\n'
        "x q[1];\nx q[2];\ncz q[1], q[2];\n"
        "c[0] = measure q[1];\nc[1] = measure q[2];\n"
    )
    counts = run(device, simulator, scheduler, qasm, shots=300)
    assert counts.get("11", 0) / sum(counts.values()) > 0.9, (
        f"|11> should come back after a full exchange round trip, got {counts}"
    )


def test_the_coupler_makes_the_bell_state_it_should(coupler_device):
    """|00> + |11>, not merely *some* maximally entangled pair.

    Getting the specific state — rather than settling for "correlated up to a
    local rotation" — is what says the two phase corrections were applied, with
    the right sign, to the right qubits. A wrong sign or a swap still produces
    a perfectly entangled pair; it just produces |01> + |10> instead.
    """
    counts = run(*coupler_device, bell_circuit(), shots=400)
    total = sum(counts.values())
    aligned = (counts.get("00", 0) + counts.get("11", 0)) / total

    assert aligned > 0.9, f"expected |00> + |11>, got {counts}"
    assert abs(counts.get("00", 0) - counts.get("11", 0)) / total < 0.15, (
        f"the two outcomes should be roughly equal, got {counts}"
    )


def test_without_its_phase_corrections_the_bell_state_is_wrong(
    coupler_device, tmp_path
):
    """The control that makes the test above mean something.

    The coupler Stark-shifts both qubits while it drives them, and the phase
    that leaves is single-qubit rather than conditional. Zero the two
    corrections and the CZ is still a perfectly good conditional-phase gate —
    the Bell state it builds is simply not the one asked for.
    """
    device, simulator, scheduler = coupler_device
    counts = run(
        without_corrections(device, tmp_path), simulator, scheduler, bell_circuit(), 400
    )
    aligned = (counts.get("00", 0) + counts.get("11", 0)) / sum(counts.values())
    assert aligned < 0.8, (
        f"uncorrected single-qubit phases should spoil |00> + |11>, got {counts}"
    )


def test_a_coupler_driven_off_resonance_does_nothing(coupler_device, tmp_path):
    """Frequency is the resonance condition, not a carrier detail.

    A parametric CZ works because a sideband of the coupler's modulation
    bridges |11>-|02>. Move the drive off that transition and the same
    amplitude for the same duration compiles, plays, and performs no gate —
    which is the failure a model with a fixed carrier could not show.
    """
    device, simulator, scheduler = coupler_device
    detuned = tmp_path / "detuned.device.yml"
    config = yaml.safe_load(device.read_text())
    config["q1_q2"]["clock_freqs"]["cz"] += 50e6  # 50 MHz off
    detuned.write_text(yaml.safe_dump(config))

    counts = run(detuned, simulator, scheduler, bell_circuit(), shots=400)
    aligned = (counts.get("00", 0) + counts.get("11", 0)) / sum(counts.values())
    assert aligned < 0.8, (
        f"a drive 50 MHz off the sideband should not make a Bell pair, got {counts}"
    )


def test_a_raw_trace_over_two_qubits_takes_one_run_each(calibrated_device):
    """A Qblox module scopes one sequencer, so two traces need two runs.

    Asking both qubits for a raw trace in one schedule does not compile —
    "Only one sequencer per device can trigger raw trace capture" — which used
    to make `meas_level=0` simply unavailable for any circuit measuring more
    than one qubit. The executor now runs the circuit once per measured qubit
    and captures one trace each time, which is what the instrument allows and
    what a lab does by hand.
    """
    device, simulator, scheduler = calibrated_device
    qasm = (
        'OPENQASM 3.0;\ninclude "stdgates.inc";\nqubit[2] q;\nbit[2] c;\n'
        "x q[0];\nc[0] = measure q[0];\nc[1] = measure q[1];\n"
    )
    memory = np.asarray(
        run_at(device, simulator, scheduler, qasm, meas_level=0)["memory"]
    )

    assert memory.shape == (2, 1000, 2), (
        f"expected a 1 us trace per qubit as [I, Q] pairs, got {memory.shape}"
    )
    # The two qubits are in different states, and their traces have to say so —
    # otherwise the second run measured the first qubit again.
    excited = complex(*np.mean(memory[0, -200:, :], axis=0))
    ground = complex(*np.mean(memory[1, -200:, :], axis=0))
    assert abs(excited - ground) > 1.0, (
        f"q0 was excited and q1 was not, but their traces settle at the same place: "
        f"{excited} and {ground}"
    )


def test_a_coupler_that_declares_its_own_gap_is_believed(coupler_device, tmp_path):
    """The device's `sideband_gap` overrides the simulator's chosen constant.

    `SIDEBAND_GAP_GHZ` exists because this project has no coupler to measure. A
    chip that *has* measured its own gap should not be simulated against a
    guess, so the edge can declare it — and declaring a different one has to
    change the answer, or the field is decoration.

    Moving the gap without moving the drive detunes the gate, so the Bell state
    it built stops being one.
    """
    device, simulator, scheduler = coupler_device
    moved = tmp_path / "moved-gap.device.yml"
    config = yaml.safe_load(device.read_text())
    drive = float(config["q1_q2"]["clock_freqs"]["cz"])
    # 50 MHz away from where this edge's drive is played.
    config["q1_q2"]["clock_freqs"]["sideband_gap"] = drive + 50e6
    moved.write_text(yaml.safe_dump(config))

    counts = run(moved, simulator, scheduler, bell_circuit(), shots=400)
    aligned = (counts.get("00", 0) + counts.get("11", 0)) / sum(counts.values())
    assert aligned < 0.8, (
        f"a declared gap 50 MHz off the drive should spoil the gate, got {counts}"
    )


# --- the whole DAG, through the real stack ---------------------------------------
#
# The tests above calibrate three routines. That was enough to prove the loop
# closes, and not enough to prove much else: `cz_chevron` wrote `cz.amp`, a name
# no device has, and nothing noticed for as long as no test ran it against a real
# `QuantumDevice`. Tier 2 compiles a schedule without calling `apply`; tier 3's
# fake device was given the names the routine used. Both agreed with the code.
#
# This runs *every* routine the simulated chip can answer, through the shipped
# tuner, over a real device loaded from YAML, and requires the report to come back
# `success`. Nothing here is a double except the instrument.

#: Where this chip's readout resonators actually are, in GHz — four megahertz from
#: the 6.000/6.010/6.020 the fixture claims.
#:
#: Without this the resonator routines would be vacuous: a resonator sitting
#: exactly where the config says makes `resonator_spectroscopy` a measurement that
#: cannot come out wrong. Four megahertz is two linewidths, so the fixture's
#: declared frequency finds the line and does not sit on it, and the routine has to
#: move the number for the readout to work.
TRUE_RESONATORS = {"q0": 6.004, "q1": 6.014, "q2": 6.024}

#: Different cables to different qubits, which is what a chip has and what the
#: simulator's single ``readout_phase_deg`` does not give it.
#:
#: With one shared phase the three qubits are identical in their readout — same gain,
#: same linewidth, same pull, each read on its own resonance — so all three come out
#: with *the same* rotation and threshold to four decimal places. That makes a whole
#: class of mistake untestable: applying one qubit's discriminator to another is
#: invisible when they agree, which is how the executors came to do exactly that.
TRUE_READOUT_PHASES = {"q0": 35.0, "q1": 155.0, "q2": 265.0}

#: Sweeps sized for the simulated chip. Every one of these is a property of the
#: simulator's own parameters — T1 of 30 us wants a sweep several times that, and
#: a sweep shorter than the decay cannot measure it.
FULL_DAG_SWEEPS: dict[str, dict] = {
    # Wide enough to bracket resonators four megahertz out, fine enough to resolve
    # a two-megahertz linewidth: a Lorentzian narrower than the step between
    # setpoints is fitted from the noise between them.
    "resonator_spectroscopy": {"span": 30e6, "points": 91},
    # Up to 0.85, because punchout is a *shift* and the shift only appears once
    # there are enough photons to wash out the dispersive pull. Stopping at the
    # 0.5 default leaves it under a megahertz, which fits but says less.
    "resonator_punchout": {
        "amplitudes": [round(0.05 + 0.1 * i, 3) for i in range(9)],
        "span": 30e6,
        "points": 61,
    },
    # A window long enough to hold the dead time *and* several ring-up constants:
    # 148 ns of flight plus 80 ns per constant needs well over half a microsecond
    # before the fit has a settled level to normalise against.
    "time_of_flight": {"window": 2e-6},
    "resonator_relaxation": {"window": 2e-6},
    # Centred above the fixture's claim rather than on it, because q0 is 214 MHz
    # *above* where the config says and a symmetric sweep spends half its points
    # below the qubit. Sweeping [4.85, 5.27] instead of [4.78, 5.22] leaves the
    # line 56 MHz inside the upper edge rather than 6 MHz — and a Lorentzian one
    # setpoint from the boundary has an essentially unconstrained centre, which is
    # a fit that passes or fails on the noise.
    #
    # Not wider: the NCO cannot be pushed past 500 MHz from its intermediate
    # frequency, and 5.27 GHz against q0's 200 MHz IF already asks for 470.
    "qubit_spectroscopy": {
        "centre_frequency": 5.06e9,
        "span": 420e6,
        "points": 85,
        # No drive amplitude: the routine sweeps power itself and picks the row that
        # shows the line most clearly. A single hand-set 3% was needed while it did
        # not — a 1% drive moves the population half a per cent, which against
        # per-shot readout noise is a signal-to-noise of about five, and a Lorentzian
        # fitted at that ratio lands anywhere.
    },
    "rabi": {"amplitudes": [round(0.02 * i, 4) for i in range(26)]},
    "ramsey": {
        "delays": [round(4e-9 + 4e-8 * i, 11) for i in range(601)],
        "artificial_detuning": 1e6,
    },
    "t1": {"delays": [round(6e-6 * i, 9) for i in range(21)]},
    "t2_echo": {"delays": [round(2e-6 * i, 9) for i in range(41)]},
    "fine_amplitude": {"repetitions": [1, 3, 5, 7, 9]},
    "rb": {"depths": [1, 4, 16, 32], "circuits_per_depth": 6},
    "interleaved_rb": {"depths": [1, 4, 10, 20], "circuits_per_depth": 4},
    # Narrow, because the avoided crossing is a few MHz wide and the default grid
    # steps ~75 MHz per point — see `MIN_CHEVRON_CONTRAST`.
    "cz_chevron": {
        "amplitudes": [round(0.365 + i * 0.00115, 5) for i in range(21)],
        "durations": [round(20e-9 + i * 5e-9, 11) for i in range(45)],
    },
    "conditional_phase": {"phases": [round(i * 15.0, 1) for i in range(25)]},
}


@pytest.fixture(scope="module")
def fully_calibrated(scheduler, tmp_path_factory):
    """The whole DAG, over two qubits and the edge between them.

    Every routine, with nothing skipped. The readout resonators are deliberately
    not where the config says, so the two resonator routines have something to
    find and the readout frequency they write is load-bearing for everything after.
    """
    simulator = TransmonSimulator(
        resonator_frequencies_ghz=TRUE_RESONATORS,
        readout_phases_deg=TRUE_READOUT_PHASES,
    )
    directory = tmp_path_factory.mktemp(f"fulldag_{scheduler}")
    device = directory / "quantify.device.yml"
    shutil.copy(FIXTURES / "quantify.device.yml", device)

    # q0 stays 214 MHz off, so finding it is still load-bearing. q1 and q2 start
    # roughly characterised, which is what a chip that has been on a fridge before
    # looks like — and what keeps the spectroscopy span inside the NCO's range.
    config = yaml.safe_load(device.read_text())
    for qubit in ("q1", "q2"):
        config[qubit]["clock_freqs"]["f01"] = float(simulator.f01 * GHZ - 5e6)
    device.write_text(yaml.safe_dump(config, sort_keys=False))

    close_instruments(scheduler)
    tuner = tuner_for(
        scheduler,
        name=f"fulldag_{scheduler}",
        quantify_hardware_config=FIXTURES / "quantify.hardware.json",
        quantify_device_config=device,
        is_simulated=True,
        simulator=simulator,
    )
    report = tuner.calibrate(
        CalibrationConfig(
            target_qubits=["q0", "q1"],
            target_edges=["q0_q1"],
            routines={
                name: RoutineConfig(enabled=True, params=FULL_DAG_SWEEPS.get(name, {}))
                for name in routine_names()
            },
        )
    )
    tuner.close()
    close_instruments(scheduler)
    return report, device, simulator, scheduler


def test_the_whole_dag_completes_against_the_simulator(fully_calibrated):
    """Every routine, through the shipped stack, with no failures and none skipped.

    This is the test that would have caught `cz.amp`, `cz.phase_correction`, the
    DRAG units and the qblox write-back — each of which was a routine that
    measured correctly and then failed, or silently declined, to write.

    The equality is the point rather than the success: a routine quietly excluded
    from the run is indistinguishable from one that works.
    """
    report, _device, _simulator, _scheduler = fully_calibrated

    assert report.status == "success", report.errors
    assert report.errors == []

    ran = {result.routine_name for result in report.routine_results}
    expected = set(routine_names())
    assert ran == expected, f"did not run {sorted(expected - ran)}"


def test_the_dispersive_shift_is_measured_and_not_assumed(fully_calibrated):
    """`resonator_spectroscopy_excited` recovers chi, which nothing else measures.

    A characterisation node writes nothing, so the only way it can be wrong is
    quietly: it would keep reporting a number and the calibration would keep
    succeeding. This is the assertion that makes it a measurement — the whole readout
    rests on the two states pulling the resonance to different places, and this is the
    node that says by how much.

    Against the simulator's own chi rather than a constant, and loosely: the routine
    fits a Lorentzian at the *configured* readout power, where punchout has already
    collapsed part of the pull, so it measures what the readout actually sees rather
    than the zero-power value.
    """
    report, _device, simulator, _scheduler = fully_calibrated
    measured = {
        result.target: result.parameters["dispersive_shift"]
        for result in report.routine_results
        if result.routine_name == "resonator_spectroscopy_excited"
    }
    assert measured, "resonator_spectroscopy_excited reported nothing"

    for qubit, shift in measured.items():
        # Negative: |1> pulls the resonance below where |0> leaves it.
        assert shift < 0, f"{qubit} reported a positive dispersive shift, {shift}"
        full = simulator.resonator(qubit).dispersive_shift_ghz * GHZ
        assert abs(shift) < full, (
            f"{qubit}'s measured pull {abs(shift) / 1e6:.3f} MHz exceeds the "
            f"{full / 1e6:.3f} MHz the model has to give, at any power"
        )


def test_the_ef_pi_pulse_is_measured_through_its_own_clock(fully_calibrated):
    """`rabi_12` drives ``.12`` with a raw pulse and recovers the drive strength.

    Neither scheduler has an EF gate — the device config's operations are built for
    ``rxy`` on ``.01`` — so this routine assembles the pulse itself: its own clock,
    its own port, its own envelope. What the assertion covers is that whole path,
    since a pulse that went to the wrong clock would drive nothing and the fit would
    have no oscillation to find.

    Against `rxy.amp180` rather than a constant, and *equal* to it rather than the
    ``1/sqrt(2)`` a chip would give. That is the simulator's documented choice: it
    folds the 1-2 matrix element into the subspace operator instead of taking the
    ladder's ``sqrt(2)``, so the same amplitude turns the same angle on both
    transitions. A real chip's EF pi pulse is weaker than its 0-1 one, and this
    simulator cannot show that.
    """
    _report, device, _simulator, _scheduler = fully_calibrated
    written = yaml.safe_load(device.read_text())

    for qubit in ("q0", "q1"):
        ef = written[qubit]["r12"]["ef_amp180"]
        assert ef > 0, f"{qubit} has no EF pi pulse"
        assert ef == pytest.approx(written[qubit]["rxy"]["amp180"], rel=0.15)


def test_leakage_into_the_third_level_is_measured(fully_calibrated):
    """The number the whole EF chain exists to produce.

    A two-state readout does not lose a leaked shot — it reports it as ``|0>`` or
    ``|1>``, so leakage arrives as an answer and every fidelity built on it is quietly
    optimistic. This is the only node that can see it, and it can only see it because
    the simulated acquisition now reports a third level: before that, a ``|2>``-prepared
    shot came back on ``|0>``'s cloud and the classifier refused to fit.

    Asserting the classifier works rather than pinning a leakage figure. How much a
    given X pulse leaks is a property of this chip and this pulse; what would make the
    node worthless is three states it cannot tell apart.
    """
    report, _device, _simulator, _scheduler = fully_calibrated
    measured = {
        result.target: result.parameters
        for result in report.routine_results
        if result.routine_name == "three_state_discrimination"
    }
    assert measured, "three_state_discrimination reported nothing"

    for qubit, params in measured.items():
        assert params["assignment_fidelity"] > 0.9, (
            f"{qubit}'s three states are not being told apart: {params}"
        )
        # A leakage figure at all, and a physical one. It is a fraction of shots, so
        # anything outside [0, 1] would be a counting mistake rather than a chip.
        assert 0.0 <= params["leakage"] <= 1.0


def test_the_three_state_readout_point_is_not_the_two_state_one(fully_calibrated):
    """Two points because one setting cannot be best at both, measured.

    At the two-state point ``|1>`` and ``|2>`` both return almost nothing and collapse
    together — 2.95 sigma apart on this chip, against 39 where the three-state sweep
    puts them. A run where the two points coincided would mean the sweep found nothing
    to prefer, and the extra submodule would be carrying a duplicate.
    """
    _report, device, _simulator, _scheduler = fully_calibrated
    written = yaml.safe_load(device.read_text())

    for qubit in ("q0", "q1"):
        three = written[qubit]["measure_3state"]
        two = written[qubit]["measure_2state"]
        assert three["frequency"] > 0 and three["pulse_amp"] > 0

        assert (three["frequency"], three["pulse_amp"]) != (
            two["frequency"],
            two["pulse_amp"],
        ), f"{qubit}'s three-state readout landed exactly on its two-state one"


def test_the_discriminated_readout_point_is_calibrated_and_sane(fully_calibrated):
    """The two readouts want different things, and the graph now says so separately.

    `resonator_spectroscopy` and `resonator_punchout` find where the most signal comes
    back, which is what every routine reducing an acquisition to a magnitude needs.
    `readout_operating_point` finds where the two states look least alike, which is
    what a discriminator needs, and it is not the same place — most of the complex
    separation off resonance is phase, and a magnitude sees none of it.

    On this chip the two differ mostly in *power*: the frequency optimum sits only
    about 200 kHz off the resonance, a tenth of a linewidth, while the amplitude has a
    genuine interior optimum because signal grows linearly with drive and
    punch-through only bends it. The frequency is swept alongside not because it moves
    far but because it cannot be chosen separately — the resonance walks with power.
    """
    _report, device, _simulator, _scheduler = fully_calibrated
    written = yaml.safe_load(device.read_text())

    for qubit in ("q0", "q1"):
        point = written[qubit]["measure_2state"]
        assert point["frequency"] > 0, f"{qubit} has no discriminated readout point"
        assert point["pulse_amp"] > 0

        # Inside the bracket it swept, which is what says the value came from the
        # sweep rather than from a default left in place. Deliberately *not* that it
        # differs from punchout's power: the grid is coarse and its optimum can land
        # on punchout's answer, which is the sweep agreeing rather than failing. Two
        # earlier versions of this test demanded a difference the chip does not
        # guarantee, and both failed on a routine that was working.
        punchout = written[qubit]["measure"]["pulse_amp"]
        assert 0.5 * punchout <= point["pulse_amp"] <= 1.5 * punchout

        # A refinement rather than a second opinion: the optimum sits about 200 kHz
        # off the resonance here, a tenth of a linewidth, so what this rules out is
        # the sweep wandering rather than choosing.
        offset = abs(point["frequency"] - written[qubit]["clock_freqs"]["readout"])
        assert offset < 2e6, (
            f"{qubit}'s discriminated readout sits {offset / 1e3:.1f} kHz from the "
            f"calibration one, further than a linewidth — that is not a refinement"
        )


def test_each_qubit_is_discriminated_against_its_own_line(fully_calibrated):
    """Two qubits, two readout chains, two rotations — and X on one of them.

    The executors used to take the rotation and threshold from whichever element had
    them first and apply that pair to every qubit in the circuit. Nothing caught it
    because nothing measured those numbers: an uncalibrated chip carries zero
    everywhere, and one qubit's zero is as good as another's. Once
    `readout_discrimination` puts a real line on each qubit, q0's applied to q1 reads
    q1's shots against a boundary drawn somewhere else.

    ``x q[0]`` and nothing on q[1], so the two qubits must come back *different*. A
    collapsed discriminator does not merely lose accuracy here, it assigns q1 with a
    threshold placed for q0's cloud positions.
    """
    _report, device, simulator, scheduler = fully_calibrated
    written = yaml.safe_load(device.read_text())

    # From `measure_2state`, where the discriminator now lives: it is fitted at the
    # operating point discriminated shots are actually taken at, which is not the one
    # the calibration routines read on. The fixture's hand-set `measure` pair stays
    # put as the fallback for an element with no `measure_2state`.
    lines = {
        qubit: (
            written[qubit]["measure_2state"]["acq_rotation"],
            written[qubit]["measure_2state"]["acq_threshold"],
        )
        for qubit in ("q0", "q1")
    }
    # Far apart, not merely unequal. An earlier version of this guard asserted only
    # that the two differ, and passed on the last digits of shot noise while the
    # qubits were physically identical — so it would have gone on passing with the
    # collapse reinstated. `TRUE_READOUT_PHASES` is what makes them really differ.
    apart = abs(lines["q0"][0] - lines["q1"][0]) % 360.0
    assert 30.0 < apart < 330.0, (
        f"the two readout lines are {apart:.2f} degrees apart, which is too close for "
        f"this test to tell a per-qubit discriminator from a collapsed one: {lines}"
    )

    counts = run_pair(device, simulator, scheduler, "x q[0];\n", shots=400)
    total = sum(counts.values()) or 1
    # Bit order is little-endian: c[1]c[0], so q0 is the right-hand character.
    excited = sum(value for key, value in counts.items() if key[1] == "1") / total
    ground = sum(value for key, value in counts.items() if key[0] == "1") / total

    assert excited > 0.9, f"q0 was driven to |1> and should read 1: {counts}"
    assert ground < 0.1, f"q1 was left alone and should read 0: {counts}"


def test_the_two_qubit_gate_is_written_and_playable(fully_calibrated):
    """The CZ the chevron found reached the file, on the hardware's time grid.

    A duration refined between setpoints is sub-nanosecond, which the instrument
    cannot play — and writing it made every *later* schedule containing this CZ
    fail to compile, some distance from the routine that caused it.
    """
    from qpi_driver.simulation.coupled import CoupledTransmons

    _report, device, _simulator, _scheduler = fully_calibrated
    written = yaml.safe_load(device.read_text())["q0_q1"]["cz"]

    assert written["square_amp"] == pytest.approx(0.376, abs=0.01)
    duration_ns = written["square_duration"] * 1e9
    assert duration_ns == pytest.approx(round(duration_ns), abs=1e-6), (
        f"the CZ duration must be a whole number of nanoseconds, got {duration_ns}"
    )
    # A *full* round trip, which is the assertion this test used to be missing.
    # Half of one is complete population transfer into |02>: a perfectly good gate,
    # measured just as confidently, and not a CZ. The chevron wrote 55 ns against a
    # round trip of 110 for as long as nothing checked the number itself.
    assert duration_ns == pytest.approx(CoupledTransmons().cz_duration_ns, rel=0.05), (
        f"the CZ should be one |11>-|02>-|11> round trip, got {duration_ns:.1f} ns"
    )

    corrections = [
        value for name, value in written.items() if name.endswith("phase_correction")
    ]
    assert len(corrections) == 2, f"expected two virtual-Z corrections in {written}"
    assert any(value != 0 for value in corrections), (
        "the conditional-phase routine measured its corrections and wrote none"
    )


def test_the_readout_lands_on_the_resonance_at_the_power_it_chose(fully_calibrated):
    """Both halves of the readout operating point, and consistent with each other.

    The strong claim is the pairing. The resonance moves with readout power, so a
    frequency and a power are not two independent numbers — the frequency is only
    right *at* that power. Punchout picks the power, so punchout has to supply the
    frequency too; leaving `resonator_spectroscopy`'s answer in place pointed the
    readout half a linewidth off and cost a fifth of the contrast everywhere
    downstream, which no assertion about either number alone would catch.

    Checked against the model rather than a constant, to a tenth of a linewidth.
    """
    _report, device, simulator, _scheduler = fully_calibrated
    config = yaml.safe_load(device.read_text())
    declared = yaml.safe_load((FIXTURES / "quantify.device.yml").read_text())

    for qubit in ("q0", "q1"):
        written = config[qubit]["clock_freqs"]["readout"]
        power = config[qubit]["measure"]["pulse_amp"]
        resonator = simulator.resonator(qubit)
        expected = resonator.resonance_ghz(power) * GHZ

        assert written == pytest.approx(expected, abs=200e3), (
            f"{qubit}'s readout is at {written / 1e6:.3f} MHz but its resonance at "
            f"power {power} is {expected / 1e6:.3f} MHz"
        )
        # And it moved, rather than the fixture having been right all along.
        was = float(declared[qubit]["clock_freqs"]["readout"])
        assert abs(written - was) > 2e6, f"{qubit}'s readout never moved off {was}"


def test_the_acquisition_window_opens_when_the_signal_arrives(fully_calibrated):
    """`time_of_flight` measured the wiring's delay, which nothing used to.

    A working chip's config carries 200 ns here with nothing having measured it. The
    simulated chip's delay is 148 ns, and the routine has to find that by opening the
    window *with* the readout pulse so the dead time lands inside the trace — which
    is the whole trick, since leaving the configured delay in place would hide
    exactly the quantity being measured.

    Tight tolerance on purpose: the arrival and the ring-up are recovered from one
    straight line, and a level crossing — the obvious alternative — is biased late by
    a quarter of the ring-up, some 20 ns here. Ten nanoseconds would pass with that
    bug present; two does not.
    """
    _report, device, simulator, _scheduler = fully_calibrated
    config = yaml.safe_load(device.read_text())

    for qubit in ("q0", "q1"):
        written = config[qubit]["measure"]["acq_delay"]
        assert written * 1e9 == pytest.approx(simulator.time_of_flight_ns, abs=2.0), (
            f"{qubit}'s acq_delay is {written * 1e9:.1f} ns against a true flight of "
            f"{simulator.time_of_flight_ns} ns"
        )
        # And on the instrument's 1 ns grid. The fit reports an arrival to a fraction
        # of a sample, and `acq_delay` shifts the acquisition: writing 149.327 ns
        # compiled here and then broke punchout, qubit spectroscopy, Rabi, Ramsey, T1
        # and flux spectroscopy, none of which is the routine at fault. Same shape as
        # the CZ duration below.
        assert written * 1e9 == pytest.approx(round(written * 1e9), abs=1e-6), (
            f"acq_delay must be a whole number of nanoseconds, got {written * 1e9}"
        )


def test_resonator_relaxation_measures_the_linewidth_and_writes_nothing(
    fully_calibrated,
):
    """A characterisation, deliberately — see the routine for why.

    The tempting parameter is `measure.integration_time`, and the ring-up is only a
    floor on it: the optimum trades signal-to-noise against relaxation during the
    window, which nothing here measures yet. So the routine reports the linewidth —
    which nothing else does, and which `resonator_spectroscopy`'s check currently
    approximates with a constant — and leaves the window alone.
    """
    report, device, simulator, _scheduler = fully_calibrated
    results = [
        r for r in report.routine_results if r.routine_name == "resonator_relaxation"
    ]
    assert results, "resonator_relaxation did not run"

    for result in results:
        assert result.parameters["linewidth"] == pytest.approx(
            simulator.readout_linewidth_ghz * GHZ, rel=0.1
        )

    # And the window is untouched, at whatever the fixture set.
    declared = yaml.safe_load((FIXTURES / "quantify.device.yml").read_text())
    written = yaml.safe_load(device.read_text())
    for qubit in ("q0", "q1"):
        assert written[qubit]["measure"]["integration_time"] == pytest.approx(
            float(declared[qubit]["measure"]["integration_time"])
        )


def test_the_discriminator_is_measured_rather_than_defaulted(fully_calibrated):
    """The line that assigns every meas_level=2 shot, and nothing used to produce it.

    Two clouds land wherever the amplifier chain puts them — each qubit level sits at
    its own resonance and so returns its own complex response, and the chain then
    rotates the pair. On the simulated chip both land with *positive* real parts, so at
    the defaults of `acq_rotation=0` and `acq_threshold=0` every shot reads ``|1>``.
    A working chip's device config does not carry either field at all.

    So the assertion is the fidelity: the fitted rule has to assign shots correctly,
    which the defaults cannot. And the rotation has to be in ``[0, 360)``, because the
    instrument refuses anything else — in every schedule *after* the one that wrote it.
    """
    report, device, _simulator, _scheduler = fully_calibrated
    written = yaml.safe_load(device.read_text())

    for qubit in ("q0", "q1"):
        measure = written[qubit]["measure"]
        assert 0.0 <= measure["acq_rotation"] < 360.0, (
            f"{qubit}'s acq_rotation is {measure['acq_rotation']}, which the "
            "instrument will refuse"
        )
        # Not the defaults, or the routine measured nothing.
        assert measure["acq_rotation"] != 0.0
        assert measure["acq_threshold"] != 0.0

    fidelities = [
        r.parameters["assignment_fidelity"]
        for r in report.routine_results
        if r.routine_name == "readout_discrimination"
    ]
    assert fidelities, "readout_discrimination did not run"
    assert min(fidelities) > 0.95, f"poor readout assignment: {fidelities}"


def test_f12_was_measured_rather_than_inherited(fully_calibrated):
    """The |1>-|2> transition, which the element carried and nothing produced.

    The fixture pins `clock_freqs.f12` at 4.8 GHz and the simulated transmon's is at
    4.931 — 131 MHz out, unnoticed for as long as nothing read the field. It is the
    input to three-state readout and it sets where |02> sits for a CZ, so a wrong value
    is not harmless, only silent.

    Driving it needs a |1> to start from, which is why the routine depends on `rabi`
    and why this cannot be part of the readout bring-up.
    """
    report, device, simulator, _scheduler = fully_calibrated
    written = yaml.safe_load(device.read_text())
    declared = yaml.safe_load((FIXTURES / "quantify.device.yml").read_text())
    true_f12 = (simulator.f01 + simulator.anharmonicity) * GHZ

    for qubit in ("q0", "q1"):
        found = written[qubit]["clock_freqs"]["f12"]
        assert found == pytest.approx(true_f12, abs=5e6), (
            f"{qubit}'s f12 is {found / 1e9:.4f} GHz against a true "
            f"{true_f12 / 1e9:.4f}"
        )
        was = float(declared[qubit]["clock_freqs"]["f12"])
        assert abs(found - was) > 50e6, f"{qubit}'s f12 never moved off {was}"

    # And the anharmonicity it implies is a transmon's, which is the sanity check on
    # the whole measurement: a fit that found the 0-1 line again would report zero.
    anharmonicities = [
        r.parameters["anharmonicity"]
        for r in report.routine_results
        if r.routine_name == "f12_spectroscopy"
    ]
    assert anharmonicities
    for value in anharmonicities:
        assert -400e6 < value < -150e6, f"not a transmon anharmonicity: {value}"


def test_punchout_wrote_a_readout_power_inside_its_sweep(fully_calibrated):
    """The last power still in the dressed regime, which is a real choice here.

    Punchout only means something because pushing more photons in *costs* the thing
    readout measures: past the crossover the dispersive pull washes out and the
    resonance walks to bare. A simulator where power did nothing would let this
    routine write the top of its sweep and look just as successful.
    """
    _report, device, _simulator, _scheduler = fully_calibrated
    amplitudes = FULL_DAG_SWEEPS["resonator_punchout"]["amplitudes"]
    written = yaml.safe_load(device.read_text())["q0"]["measure"]["pulse_amp"]

    assert any(abs(written - amplitude) < 1e-9 for amplitude in amplitudes), (
        f"the readout power should be one of the swept amplitudes, got {written}"
    )
    assert written < max(amplitudes), (
        "punchout picked the highest power it tried, which is what it would do if "
        "power had no effect on the resonator"
    )


def test_drag_wrote_the_optimum_its_scheduler_spells(fully_calibrated):
    """The Motzoi optimum, in whichever units and under whichever name.

    The two schedulers disagree twice over: quantify's `rxy.motzoi` is a
    dimensionless ratio and qblox's `rxy.beta` is that ratio times the pulse sigma,
    in seconds. So the same chip has two right answers differing by 2.5 ns, and a
    routine with one hardcoded name and one hardcoded sweep gets both wrong.

    The physical value is about ``-1/(2*alpha)``, which for this transmon's -280 MHz
    is a ratio near 0.11.
    """
    _report, device, _simulator, scheduler = fully_calibrated
    written = yaml.safe_load(device.read_text())["q0"]["rxy"]

    name = "beta" if scheduler == "qblox" else "motzoi"
    assert name in written, f"drag wrote nothing readable: {written}"
    # Back to the dimensionless ratio, so one number covers both schedulers.
    sigma = 20e-9 / 8  # the default 20 ns gate, cut at four sigma
    ratio = written[name] / (sigma if scheduler == "qblox" else 1.0)
    assert ratio == pytest.approx(0.11, abs=0.03), (
        f"expected a DRAG ratio near 0.11, got {ratio} from {name}={written[name]}"
    )


def test_circuits_run_against_what_the_whole_dag_wrote(fully_calibrated):
    """The point of calibrating: the file it produced drives the chip."""
    _report, device, simulator, scheduler = fully_calibrated

    counts = run(device, simulator, scheduler, circuit("x q[0];\n"), shots=400)
    assert counts["1"] / sum(counts.values()) > 0.9, (
        f"an X gate should land in |1> after a full calibration, got {counts}"
    )
