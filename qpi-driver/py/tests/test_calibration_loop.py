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

pytest.importorskip("quantify_scheduler", reason="needs a scheduler extra")
pytest.importorskip("scqubits", reason="needs the [sim] dependency group")
pytest.importorskip("qutip", reason="needs the [sim] dependency group")

from qpi_driver.compat.quantify import Instrument  # noqa: E402
from qpi_driver.executors import resolve_executor  # noqa: E402
from qpi_driver.executors.base import CircuitPayload, JobPayload  # noqa: E402
from qpi_driver.simulation import GHZ, TransmonSimulator  # noqa: E402
from qpi_driver.tuners.base.config import CalibrationConfig, RoutineConfig  # noqa: E402
from qpi_driver.tuners.base.device import read_path  # noqa: E402
from qpi_driver.tuners.routines import routine_names  # noqa: E402

pytestmark = pytest.mark.scqubits

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
def calibrated_device(tmp_path_factory) -> tuple[Path, TransmonSimulator]:
    """Run a real calibration against the simulator and return what it wrote."""
    simulator = TransmonSimulator()
    directory = tmp_path_factory.mktemp("loop")
    device = directory / "quantify.device.yml"
    shutil.copy(FIXTURES / "quantify.device.yml", device)

    from qpi_driver.tuners.quantify import QuantifyTuner

    Instrument.close_all()
    tuner = QuantifyTuner(
        name="loop_tuner",
        quantify_hardware_config=FIXTURES / "quantify.hardware.json",
        quantify_device_config=device,
        is_simulated=True,
        simulator=simulator,
    )
    report = tuner.calibrate(calibration_config())
    assert report.status == "success", report.errors
    tuner.close()
    Instrument.close_all()
    return device, simulator


def run(
    device: Path, simulator: TransmonSimulator, qasm: str, shots: int = 400
) -> dict:
    """Counts from the executor, over the calibrated file and the same chip."""
    Instrument.close_all()
    executor = resolve_executor(
        "quantify",
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
    device, simulator = calibrated_device
    written = yaml.safe_load(device.read_text())["q0"]

    # Spectroscopy then Ramsey: the fixture starts 214 MHz out, and what is
    # written has to be good enough to drive with, not merely in the right area.
    assert written["clock_freqs"]["f01"] == pytest.approx(simulator.f01 * GHZ, abs=1e6)
    # The instrument's drive strength puts a pi rotation at 0.2, and nothing
    # told the tuner that — Rabi had to find it.
    assert written["rxy"]["amp180"] == pytest.approx(0.2, rel=0.05)


def test_the_executor_loads_exactly_what_the_tuner_wrote(calibrated_device):
    device, simulator = calibrated_device
    written = yaml.safe_load(device.read_text())["q0"]

    Instrument.close_all()
    executor = resolve_executor(
        "quantify",
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


def test_an_uncalibrated_chip_gets_the_answer_wrong(tmp_path):
    """The whole loop, negated — this is what makes the tests above mean something.

    The same X gate on the *uncalibrated* fixture device, whose f01 is 214 MHz
    from where the qubit actually is. The pulse compiles, runs and returns
    counts; they are simply wrong. If calibration were not load-bearing, this
    would pass too.
    """
    simulator = TransmonSimulator()
    device = tmp_path / "quantify.device.yml"
    shutil.copy(FIXTURES / "quantify.device.yml", device)

    counts = run(device, simulator, circuit("x q[0];\n"))
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
def two_qubit_device(tmp_path_factory) -> tuple[Path, TransmonSimulator]:
    """A device already at the simulator's true parameters, edge included.

    Deliberately *not* calibrated by a tuner here. The loop above proves the
    tuner finds a qubit; this fixture isolates the gate, so a failure below is
    the CZ rather than a single-qubit calibration that drifted.
    """
    from qpi_driver.simulation.coupled import CoupledTransmons

    simulator = TransmonSimulator()
    pair = CoupledTransmons()
    directory = tmp_path_factory.mktemp("twoqubit")
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
    return device, simulator


TWO_QUBIT_HEAD = 'OPENQASM 3.0;\ninclude "stdgates.inc";\nqubit[2] q;\nbit[2] c;\n'
TWO_QUBIT_TAIL = "c[0] = measure q[0];\nc[1] = measure q[1];\n"


def run_pair(device: Path, simulator: TransmonSimulator, body: str, shots: int = 400):
    return run(device, simulator, TWO_QUBIT_HEAD + body + TWO_QUBIT_TAIL, shots=shots)


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
    device, simulator = two_qubit_device
    broken = device.parent / "mistimed.device.yml"
    config = yaml.safe_load(device.read_text())
    config["q0_q1"]["cz"]["square_duration"] = (
        round(config["q0_q1"]["cz"]["square_duration"] * 1e9 / 2) * 1e-9
    )
    broken.write_text(yaml.safe_dump(config))

    counts = run_pair(broken, simulator, "h q[0];\nh q[1];\ncz q[0], q[1];\nh q[1];\n")
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
    device, simulator = calibrated_device
    gap = "delay[8ns] q[0];\n"

    without = run(
        device, simulator, circuit("h q[0];\n" + gap + gap + "h q[0];\n"), shots=600
    )
    with_z = run(
        device,
        simulator,
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
    device, simulator = calibrated_device
    gap = "delay[8ns] q[0];\n"
    counts = run(
        device,
        simulator,
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
    qasm: str,
    *,
    meas_level: int,
    meas_return: str = "single",
    shots: int = 200,
) -> dict:
    """The whole result dict, not just counts, since levels 0 and 1 have none."""
    Instrument.close_all()
    executor = resolve_executor(
        "quantify",
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

    # The two states have to be separable, or no discriminator could work.
    excited_i = float(np.mean(excited_memory[:, 0, 0]))
    ground_i = float(np.mean(np.asarray(ground["memory"])[:, 0, 0]))
    assert excited_i > ground_i + 1.0, (
        f"the blobs should separate along I: ground {ground_i:.2f}, excited {excited_i:.2f}"
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
    device, simulator = calibrated_device
    qasm = circuit("x q[0];\n")

    counts = run_at(device, simulator, qasm, meas_level=2, shots=200)["counts"]
    iq = np.asarray(run_at(device, simulator, qasm, meas_level=1, shots=200)["memory"])
    memory = np.asarray(run_at(device, simulator, qasm, meas_level=0)["memory"])

    excited_fraction = counts["1"] / sum(counts.values())
    mean_i = float(np.mean(iq[:, 0, 0]))
    settled_i = float(np.mean(memory[0, -200:, 0]))

    assert excited_fraction > 0.9
    assert mean_i > 0, f"level 1 should sit on the excited blob, got I={mean_i:.2f}"
    assert settled_i > 0, f"level 0's settled trace should agree, got I={settled_i:.2f}"


# --- the flux-tunable coupler ---------------------------------------------------
#
# The tests above drive `q0_q1`, a stock `CompositeSquareEdge` whose flux pulse
# goes on a *qubit's* port. `q1_q2` is a `FluxTunableCoupler`: the pulse goes on
# the coupler's own port, `q1_q2:fl`, and the CZ is lowered into a fresh
# subschedule. That is a different path through both the compiler and the
# simulator, and it is the one the lab's device config actually uses.


@pytest.fixture(scope="module")
def coupler_device(tmp_path_factory) -> tuple[Path, TransmonSimulator]:
    from qpi_driver.simulation.coupled import CoupledTransmons

    simulator = TransmonSimulator()
    pair = CoupledTransmons()
    directory = tmp_path_factory.mktemp("coupler")
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
        "q1_phase_correction": phases["parent"],
        "q2_phase_correction": phases["child"],
    }
    # The coupler's DC parking point, which the SPI rack supplies out of band.
    config["q1_q2"]["bias"] = {"parking_current": 0.0009, "source": "spi"}
    device.write_text(yaml.safe_dump(config))
    return device, simulator


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
    config["q1_q2"]["cz"]["q1_phase_correction"] = 0.0
    config["q1_q2"]["cz"]["q2_phase_correction"] = 0.0
    other.write_text(yaml.safe_dump(config))
    return other


def test_the_coupler_carries_its_parking_current(coupler_device):
    """The bias is calibrated state and has to survive the device file.

    It is not scheduled — a seconds-scale DC current is not a pulse — but it is
    as much a part of the CZ's calibration as the pulse amplitude, so it belongs
    in the config the tuner writes and the executor reads.
    """
    device, simulator = coupler_device
    Instrument.close_all()
    executor = resolve_executor(
        "quantify",
        is_simulated=True,
        simulator=simulator,
        quantify_hardware_config=FIXTURES / "quantify.hardware.json",
        quantify_device_config=device,
    )
    edge = executor._device.get_edge("q1_q2")

    assert edge.bias.parking_current() == pytest.approx(0.0009)
    assert edge.bias.source() == "spi"


def test_the_parking_current_is_bounded(coupler_device):
    """Outside the validated range is refused rather than driven.

    An S4g will happily source more than the couplers are characterised for,
    and a current that far out is a fat-fingered config rather than an
    operating point.
    """
    device, simulator = coupler_device
    Instrument.close_all()
    executor = resolve_executor(
        "quantify",
        is_simulated=True,
        simulator=simulator,
        quantify_hardware_config=FIXTURES / "quantify.hardware.json",
        quantify_device_config=device,
    )
    edge = executor._device.get_edge("q1_q2")

    with pytest.raises(ValueError):
        edge.bias.parking_current(5e-3)
    # And a normal operating value is accepted.
    edge.bias.parking_current(1.1e-3)


def test_a_cz_over_the_coupler_edge_completes_its_exchange(coupler_device):
    """|11> survives a CZ played through the coupler's own port.

    The partner cannot be read off the gate here — the lowered subschedule has
    lost it — so the simulator takes the pair from the port name, which is what
    `q1_q2:fl` is for. Getting that wrong applies the coupling between the wrong
    qubits, which is a working-looking gate on the wrong pair.
    """
    device, simulator = coupler_device
    qasm = (
        'OPENQASM 3.0;\ninclude "stdgates.inc";\nqubit[3] q;\nbit[2] c;\n'
        "x q[1];\nx q[2];\ncz q[1], q[2];\n"
        "c[0] = measure q[1];\nc[1] = measure q[2];\n"
    )
    counts = run(device, simulator, qasm, shots=300)
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
    device, simulator = coupler_device
    counts = run(without_corrections(device, tmp_path), simulator, bell_circuit(), 400)
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
    device, simulator = coupler_device
    detuned = tmp_path / "detuned.device.yml"
    config = yaml.safe_load(device.read_text())
    config["q1_q2"]["clock_freqs"]["cz"] += 50e6  # 50 MHz off
    detuned.write_text(yaml.safe_dump(config))

    counts = run(detuned, simulator, bell_circuit(), shots=400)
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
    device, simulator = calibrated_device
    qasm = (
        'OPENQASM 3.0;\ninclude "stdgates.inc";\nqubit[2] q;\nbit[2] c;\n'
        "x q[0];\nc[0] = measure q[0];\nc[1] = measure q[1];\n"
    )
    memory = np.asarray(run_at(device, simulator, qasm, meas_level=0)["memory"])

    assert memory.shape == (2, 1000, 2), (
        f"expected a 1 us trace per qubit as [I, Q] pairs, got {memory.shape}"
    )
    # The two qubits are in different states, and their traces have to say so —
    # otherwise the second run measured the first qubit again.
    excited = float(np.mean(memory[0, -200:, 0]))
    ground = float(np.mean(memory[1, -200:, 0]))
    assert excited > ground, (
        f"q0 was excited and q1 was not, but their traces settle at "
        f"{excited:.2f} and {ground:.2f}"
    )
