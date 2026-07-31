"""Every routine compiles against a real scheduler (RFC 0004 §7, tier 2).

These need a scheduler, so they skip on a base install and run under
``make test-py-quantify`` and ``make test-py-qblox``. What they prove is that
each routine builds a schedule the backend's compiler accepts — the dummy
cluster returns no real data, so analysis is tier 1's job, not this file's.

The one thing asserted about data here is the opposite of a fit: that a dummy
acquisition makes a routine *fail*, loudly, rather than fit zeros and write them
to the device.
"""

from pathlib import Path

import pytest
from qpi_driver.compat.qblox import IS_QBLOX_SCHEDULER_INSTALLED
from qpi_driver.compat.quantify import IS_QUANTIFY_INSTALLED
from qpi_driver.tuners.base.config import CalibrationConfig, RoutineConfig
from qpi_driver.tuners.routines import ROUTINE_CLASSES, all_routines

FIXTURES = Path(__file__).parent / "fixtures"

# Small sweeps: the point is that each routine compiles, not that it is precise.
SMALL_SWEEPS: dict[str, dict] = {
    "resonator_spectroscopy": {"points": 5, "span": 10e6},
    "resonator_punchout": {"amplitudes": [0.05, 0.1, 0.2], "points": 4, "span": 10e6},
    "qubit_spectroscopy": {"points": 5, "span": 20e6},
    "rabi": {"amplitudes": [0.0, 0.1, 0.2, 0.3, 0.4]},
    "ramsey": {"delays": [4e-9, 1e-6, 2e-6, 4e-6]},
    "t1": {"delays": [0, 10e-6, 20e-6, 40e-6]},
    "t2_echo": {"delays": [0, 10e-6, 20e-6, 40e-6]},
    # Seconds, not a dimensionless amplitude: the DRAG parameter scales a time
    # derivative of the envelope, so a 20 ns gate wants values of order 1e-11.
    # At the dimensionless scale this used to use, the derivative term dwarfs
    # the carrier and the waveform exceeds full scale.
    "drag": {"motzois": [-1e-10, 0.0, 1e-10]},
    "fine_amplitude": {"repetitions": [1, 3, 5]},
    "rb": {"depths": [1, 2, 4], "circuits_per_depth": 2},
    "flux_spectroscopy": {"flux_offsets": [-0.1, 0.1], "points": 3, "span": 20e6},
    "cz_chevron": {"amplitudes": [0.2, 0.3], "durations": [40e-9, 80e-9]},
    "conditional_phase": {"phases": [0.0, 90.0, 180.0, 270.0]},
    "interleaved_rb": {"depths": [1, 2], "circuits_per_depth": 2},
}

ROUTINE_NAMES = [cls.name for cls in ROUTINE_CLASSES]


@pytest.fixture(scope="module")
def quantify_tuner(tmp_path_factory):
    if not IS_QUANTIFY_INSTALLED:
        pytest.skip("quantify-scheduler is not installed")
    from qpi_driver.compat.quantify import Instrument
    from qpi_driver.tuners.quantify import QuantifyTuner

    Instrument.close_all()
    device = tmp_path_factory.mktemp("quantify") / "quantify.device.yml"
    device.write_bytes((FIXTURES / "quantify.device.yml").read_bytes())

    tuner = QuantifyTuner(
        quantify_hardware_config=FIXTURES / "quantify.hardware.json",
        quantify_device_config=device,
        is_dummy=True,
    )
    yield tuner
    tuner.close()


@pytest.fixture(scope="module")
def qblox_tuner(tmp_path_factory):
    if not IS_QBLOX_SCHEDULER_INSTALLED:
        pytest.skip("qblox-scheduler is not installed")
    from qpi_driver.compat.qblox import Instrument
    from qpi_driver.tuners.qblox import QbloxTuner

    Instrument.close_all()
    directory = tmp_path_factory.mktemp("qblox")
    device = directory / "quantify.device.yml"
    device.write_bytes((FIXTURES / "quantify.device.yml").read_bytes())

    tuner = QbloxTuner(
        quantify_hardware_config=FIXTURES / "quantify.hardware.json",
        quantify_device_config=device,
        is_dummy=True,
        data_dir=directory / "data",
    )
    yield tuner
    tuner.close()


def routine(name):
    return next(r for r in all_routines() if r.name == name)


def _build(routine, tuner):
    """Build *routine*'s schedule for the right kind of target."""
    target = "q0" if routine.targets == "qubits" else "q0_q1"
    config = RoutineConfig(params=SMALL_SWEEPS.get(routine.name, {}))
    return routine.build_schedule(target, tuner.device, config, tuner.backend)


@pytest.mark.parametrize("routine_name", ROUTINE_NAMES)
def test_every_routine_compiles_under_quantify(routine_name, quantify_tuner):
    routine = next(r for r in all_routines() if r.name == routine_name)
    schedule = _build(routine, quantify_tuner)

    compiled = quantify_tuner._compiler.compile(schedule)
    assert compiled is not None
    assert len(schedule.operations) > 0


@pytest.mark.parametrize("routine_name", ROUTINE_NAMES)
def test_every_routine_builds_a_schedule_under_qblox(routine_name, qblox_tuner):
    """And compiles it, which is the half that was missing.

    qblox owns compilation inside its `HardwareAgent` rather than in a separate
    compiler, so this used to assert only that a schedule came back — meaning a
    routine whose schedule built but would not compile passed here while the
    equivalent quantify test caught it. The agent exposes `compile`, so both
    backends are now held to the same standard.
    """
    routine = next(r for r in all_routines() if r.name == routine_name)
    schedule = _build(routine, qblox_tuner)

    assert schedule is not None
    assert len(schedule.operations) > 0

    compiled = qblox_tuner._agent.compile(schedule)
    assert compiled is not None


CHECKABLE = [r.name for r in all_routines() if r.has_check]


@pytest.mark.parametrize("routine_name", CHECKABLE)
def test_every_check_schedule_compiles_under_both_schedulers(
    routine_name, quantify_tuner, qblox_tuner
):
    """A check that cannot be built is silent, not failing — so it needs a test here.

    `diagnose` deliberately treats an unevaluable check as *unknown* rather than as
    drift, because a broken check must not trigger a recalibration. The cost of that
    choice is that a check which raises every time reports nothing, forever, and no
    test fails. One did: the first version of `resonator_spectroscopy`'s read its
    tolerance from ``measure.readout_linewidth``, a path no transmon element has, so
    `read_path` raised `ParameterError` on every call and the check was dead on
    arrival.

    Building the check schedule against a real device is what catches that, because
    that is where a check reads the parameters it is judging.
    """
    routine = next(r for r in all_routines() if r.name == routine_name)
    target = "q0" if routine.targets == "qubits" else "q0_q1"
    config = RoutineConfig(params=SMALL_SWEEPS.get(routine.name, {}))

    for tuner, compile_with in (
        (quantify_tuner, lambda s: quantify_tuner._compiler.compile(s)),
        (qblox_tuner, lambda s: qblox_tuner._agent.compile(s)),
    ):
        schedule = routine.build_check_schedule(
            target, tuner.device, config, tuner.backend
        )
        assert schedule is not None, (
            f"{routine_name} reports has_check but built no check schedule"
        )
        assert compile_with(schedule) is not None


def test_a_dummy_acquisition_fails_the_routine_rather_than_fitting_zeros(
    quantify_tuner,
):
    """The dummy cluster returns no real data, and that must be an error.

    This is the failure mode the whole design turns on: a fit that returns zeros
    on bad data gets written to the device as though it were a measurement.
    """
    config = CalibrationConfig(
        target_qubits=["q0"],
        target_edges=[],
        routines={
            name: RoutineConfig(
                enabled=name in {"rabi", "t1"}, params=SMALL_SWEEPS.get(name, {})
            )
            for name in ROUTINE_NAMES
        },
    )
    report = quantify_tuner.calibrate(config)

    assert report.status == "failed"
    assert report.routine_results == []
    assert len(report.errors) == 2


def test_a_failed_calibration_does_not_touch_the_device_config(quantify_tuner):
    """Overwriting a good config with half-applied parameters is how a QPU is lost."""
    before = quantify_tuner._device_config_path.read_text()

    config = CalibrationConfig(
        target_qubits=["q0"],
        routines={
            name: RoutineConfig(
                enabled=name == "rabi", params=SMALL_SWEEPS.get(name, {})
            )
            for name in ROUTINE_NAMES
        },
    )
    report = quantify_tuner.calibrate(config)

    assert report.status == "failed"
    assert quantify_tuner._device_config_path.read_text() == before


def test_a_write_back_round_trips_through_the_real_loader(quantify_tuner):
    """What a tuner writes, the process driver beside it has to be able to read."""
    from qpi_driver.compat.quantify import Instrument
    from qpi_driver.executors.quantify.config import load_quantum_device
    from qpi_driver.tuners.utils.persistence import save_device_config

    path = quantify_tuner._device_config_path
    quantify_tuner.device.get_element("q0").rxy.amp180(0.1234)
    save_device_config(quantify_tuner.device, path)

    assert "!!python" not in path.read_text()

    Instrument.close_all()
    reloaded = load_quantum_device(name="reloaded", config=path)
    assert reloaded.get_element("q0").rxy.amp180() == 0.1234
    assert reloaded.edges() == ["q0_q1", "q1_q2"]


@pytest.mark.parametrize("tuner_name", ["quantify", "qblox"])
def test_closing_a_live_tuner_does_not_raise(tuner_name, tmp_path):
    """The worker calls `close()` on shutdown, so it has to survive a live coordinator.

    Its own tuner rather than the module fixture, deliberately. That fixture is
    torn down *after* a test that has already called `Instrument.close_all()`,
    so its `close()` runs against a coordinator that is already gone — the one
    arrangement in which a broken shutdown looks fine.
    """
    if not {
        "quantify": IS_QUANTIFY_INSTALLED,
        "qblox": IS_QBLOX_SCHEDULER_INSTALLED,
    }[tuner_name]:
        pytest.skip(f"the {tuner_name} scheduler is not installed")

    from qpi_driver.compat.quantify import Instrument
    from qpi_driver.tuners import resolve_tuner

    device = tmp_path / "quantify.device.yml"
    device.write_bytes((FIXTURES / "quantify.device.yml").read_bytes())

    Instrument.close_all()
    tuner = resolve_tuner(
        tuner_name,
        quantify_hardware_config=FIXTURES / "quantify.hardware.json",
        quantify_device_config=device,
        is_dummy=True,
        data_dir=tmp_path / "data",
    )
    tuner.close()
    tuner.close()  # and again: a driver failing mid-shutdown closes twice


@pytest.mark.parametrize("tuner_name", ["quantify", "qblox"])
def test_the_two_qubit_routines_write_parameters_the_edge_actually_has(tuner_name):
    """`apply` against a real edge, not a fake shaped like the routine.

    Both two-qubit routines wrote names no device has — `cz.amp` and
    `cz.duration`, which raised, and `cz.phase_correction`, which was skipped by
    a `hasattr` guard that was never true. So a chevron that had measured the
    gate correctly failed at its last step, and the conditional phase applied
    nothing at all.

    Neither tier-2 nor tier-3 could catch it: tier 2 builds and compiles a
    schedule without ever calling `apply`, and tier 3's fake device was given
    the names the routine used. A fake shaped to the code cannot contradict it,
    so this asserts against the real `QuantumDevice`.
    """
    # Its own device rather than the module-scoped tuner's: an earlier test in
    # this file calls `Instrument.close_all()`, which invalidates the shared
    # one's edges. Loading here keeps the test independent of its neighbours.
    if tuner_name == "qblox":
        if not IS_QBLOX_SCHEDULER_INSTALLED:
            pytest.skip("qblox-scheduler is not installed")
        from qpi_driver.compat.qblox import Instrument
        from qpi_driver.executors.qblox.config import load_quantum_device
    else:
        if not IS_QUANTIFY_INSTALLED:
            pytest.skip("quantify-scheduler is not installed")
        from qpi_driver.compat.quantify import Instrument
        from qpi_driver.executors.quantify.config import load_quantum_device

    Instrument.close_all()
    device = load_quantum_device(
        name=f"apply_{tuner_name}", config=FIXTURES / "quantify.device.yml"
    )
    edge = device.get_edge("q0_q1")

    routine(  # noqa: B018 - the call is the assertion
        "cz_chevron"
    ).apply(device, "q0_q1", {"cz_amplitude": 0.377, "cz_duration": 110e-9})

    from qpi_driver.tuners.base.device import phase_correction_names, read_path

    assert read_path(edge, "cz.square_amp") == pytest.approx(0.377)
    assert read_path(edge, "cz.square_duration") == pytest.approx(110e-9)

    names = phase_correction_names(edge)
    assert names is not None, "a CZ edge must expose its two phase corrections"

    routine("conditional_phase").apply(
        device,
        "q0_q1",
        {"parent_phase_correction": 12.0, "child_phase_correction": -34.0},
    )
    parent_name, child_name = names
    assert read_path(edge, f"cz.{parent_name}") == pytest.approx(12.0)
    assert read_path(edge, f"cz.{child_name}") == pytest.approx(-34.0)
